"""qB 任务与本地媒体移动的安全调用顺序。"""
from __future__ import annotations

import os
import sqlite3
from typing import cast
from app.modules.scraper import TMDBScraper

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.modules import telegram_notification_center as center
from app.modules.local_media_notifications import notify_local_media_task
from app.notifier import TelegramSendResult
from app.repositories import download_requests as download_requests_repo
from app.repositories import local_media as local_media_repo
from app.modules.local_media_service import (
    LocalMediaPostMoveError,
    LocalMediaService,
    LocalMovePlan,
)
from app.modules.local_storage import LocalFilesystemAdapter
from app.modules.scraper import MatchResult
from tests.support import IsolatedDatabaseTestCase, media_refresh_providers
from tests.test_local_media_service import FakeScraper


class FakeQB:
    def __init__(self, delete_error=None):
        self.calls = []
        self.delete_error = delete_error

    def pause_torrents(self, value): self.calls.append(("pause", value))
    def resume_torrents(self, value): self.calls.append(("resume", value))
    def delete_torrents(self, value, delete_files=False):
        self.calls.append(("delete", value, delete_files))
        if self.delete_error:
            raise self.delete_error


class LocalQBLifecycleTests(IsolatedDatabaseTestCase):
    def _fixture(self):
        root = tempfile.TemporaryDirectory()
        base = Path(root.name); source = base / "source"; target = base / "target"
        source.mkdir(); target.mkdir(); (source / "Movie.2025.mkv").write_bytes(b"movie")
        (source / "下载必看.txt").write_bytes(b"ad")
        (source / "fonts.zip").write_bytes(b"font")
        source_id = local_media_repo.create_local_media_source(
            name=f"qb-{base.name}", qb_profile="configured:qb", qb_path_prefix="/downloads",
            local_root=str(source), stable_seconds=0, owner="admin",
        )
        local_media_repo.upsert_local_library_target(source_id, "movie", str(target), owner="admin")
        task_id, _created = local_media_repo.admit_local_media_task(source_id, "hash-1", str(source), owner="admin")
        local_media_repo.claim_local_media_task(task_id, expected="waiting_stable", owner="admin")
        scraper = FakeScraper(MatchResult(tmdb_id="1", title="Movie", year="2025", media_type="movie", confidence=1))
        return root, source, target, task_id, LocalMediaService(scraper=cast(TMDBScraper, scraper))

    def test_qb_task_is_removed_only_after_verified_move(self):
        root, source, target, task_id, service = self._fixture()
        self.addCleanup(root.cleanup)
        qb = FakeQB()
        with media_refresh_providers():
            result = service.execute_task("admin", task_id, qb_client=qb)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(qb.calls, [("pause", "hash-1"), ("delete", "hash-1", False)])
        self.assertFalse((source / "Movie.2025.mkv").exists())
        self.assertFalse((source / "下载必看.txt").exists())
        self.assertTrue((source / "fonts.zip").exists())
        self.assertTrue(any(target.rglob("*.mkv")))

    def test_move_failure_resumes_and_never_deletes_qb(self):
        root, _source, _target, task_id, service = self._fixture()
        self.addCleanup(root.cleanup)
        qb = FakeQB()
        with patch("app.modules.local_media_service.LocalMoveTransaction.execute", side_effect=OSError("move failed")):
            with self.assertRaisesRegex(OSError, "move failed"):
                service.execute_task("admin", task_id, qb_client=qb)
        self.assertEqual(qb.calls, [("pause", "hash-1"), ("resume", "hash-1")])
        get_local_media_task_result = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert get_local_media_task_result is not None
        self.assertEqual(get_local_media_task_result.status, "failed")

    def test_qb_delete_failure_keeps_junk_files(self):
        root, source, _target, task_id, service = self._fixture()
        self.addCleanup(root.cleanup)
        qb = FakeQB(delete_error=OSError("delete failed"))
        with media_refresh_providers():
            result = service.execute_task("admin", task_id, qb_client=qb)
        self.assertEqual(result["status"], "completed")
        self.assertTrue((source / "下载必看.txt").exists())
        self.assertTrue(result["qb_cleanup_pending"])
        self.assertEqual(qb.calls, [("pause", "hash-1"), ("delete", "hash-1", False)])
        self.assertTrue(any("任务保持暂停" in item for item in result["warnings"]))
        self.assertNotIn("入库", "；".join(result["warnings"]))

        with (
            patch.dict(os.environ, {
                "TG_NOTIFICATION_ENABLED": "1", "TG_NOTIFICATION_LEVEL": "essential",
                "GY_ORGANIZE_NOTIFY_ENABLED": "1", "GY_ORGANIZE_LIBRARY_NOTIFY": "1",
            }),
            patch.object(center, "send_event_result", side_effect=[
                TelegramSendResult(True, message_id=71), TelegramSendResult(True, message_id=72),
            ]) as send,
            patch.object(center, "edit_event_result") as edit,
            patch.object(center, "wake_telegram_notification_dispatcher"),
        ):
            self.assertTrue(notify_local_media_task(task_id, result, chat_id="100"))
            request_id, _ = download_requests_repo.create_download_request(
                "qb-retirement-failed", "magnet", title="Movie", chat_id="100",
            )
            download_requests_repo.update_download_request(
                request_id, status="completed", qb_status="completed",
                local_import_status="completed", local_import_target=f"local-media-task:{task_id}",
            )
            self.assertTrue(notify_local_media_task(task_id))
        self.assertEqual(send.call_count, 2)
        edit.assert_not_called()
        for call in send.call_args_list:
            event = call.args[0]
            self.assertIn("部分完成", str(event.title))
            self.assertIn("手动移除", str(dict(event.fields)["qB 收尾"]))


    def test_post_commit_database_failure_keeps_qb_paused_and_requires_manual(self):
        root, source, target, task_id, service = self._fixture()
        self.addCleanup(root.cleanup)
        qb = FakeQB()
        original_update = local_media_repo.update_local_media_task

        def fail_verifying(*args, **kwargs):
            if kwargs.get("status") == "verifying":
                raise RuntimeError("injected post-commit db failure")
            return original_update(*args, **kwargs)

        with patch(
            "app.repositories.local_media.update_local_media_task",
            side_effect=fail_verifying,
        ):
            with self.assertRaisesRegex(Exception, "已完成移动.*收尾失败"):
                service.execute_task("admin", task_id, qb_client=qb)

        self.assertEqual(qb.calls, [("pause", "hash-1")])
        self.assertFalse((source / "Movie.2025.mkv").exists())
        moved = list(target.rglob("*.mkv"))
        self.assertEqual(len(moved), 1)
        task = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert task is not None
        self.assertEqual(task.status, "requires_manual")
        self.assertIn(str(moved[0]), task.error)

    def test_post_commit_cleanup_failure_never_resumes_qb(self):
        root, source, target, task_id, service = self._fixture()
        self.addCleanup(root.cleanup)
        qb = FakeQB()

        with patch(
            "app.modules.local_media_service.delete_cleanup_items",
            side_effect=RuntimeError("injected cleanup failure"),
        ):
            with self.assertRaisesRegex(Exception, "已完成移动.*收尾失败"):
                service.execute_task("admin", task_id, qb_client=qb)

        self.assertEqual(
            qb.calls,
            [("pause", "hash-1"), ("delete", "hash-1", False)],
        )
        self.assertFalse((source / "Movie.2025.mkv").exists())
        self.assertTrue(any(target.rglob("*.mkv")))
        get_local_media_task_result_2 = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert get_local_media_task_result_2 is not None
        self.assertEqual(
            get_local_media_task_result_2.status,
            "requires_manual",
        )

    def test_step_audit_failure_does_not_interrupt_rollback_or_resume_qb(self):
        root, source, target, task_id, service = self._fixture()
        self.addCleanup(root.cleanup)
        video = source / "Movie.2025.mkv"
        subtitle = source / "Movie.2025.zh.srt"
        subtitle.write_bytes(b"subtitle")
        qb = FakeQB()
        update_step = local_media_repo.update_local_media_operation_step
        started_steps: list[int] = []

        def update(step_id, status, **fields):
            if status == "running":
                started_steps.append(step_id)
            elif len(started_steps) == 2 and step_id == started_steps[-1]:
                raise sqlite3.OperationalError("injected step audit outage")
            return update_step(step_id, status, **fields)

        with patch.object(local_media_repo, "update_local_media_operation_step", side_effect=update):
            with self.assertRaises(Exception) as raised:
                service.execute_task("admin", task_id, qb_client=qb)

        task = local_media_repo.get_local_media_task(task_id)
        assert task is not None
        self.assertEqual(len(started_steps), 2)
        self.assertEqual(
            (video.exists(), subtitle.exists(), [p.name for p in target.rglob("*.mkv")], qb.calls, task.status),
            (True, True, [], [("pause", "hash-1")], "requires_manual"),
        )
        self.assertIsInstance(raised.exception, LocalMediaPostMoveError)
        self.assertIn("injected step audit outage", task.error)

    def test_all_skipped_after_qb_delete_failure_never_resumes_and_requires_manual(self):
        root, source, target, task_id, service = self._fixture()
        self.addCleanup(root.cleanup)
        source_file = source / "Movie.2025.mkv"
        snapshot = LocalFilesystemAdapter(source).snapshot(source_file)
        preview = {
            "status": "planned",
            "digest": "all-skipped",
            "pending_confirmations": [],
            "_move_plans": [
                LocalMovePlan(
                    source=snapshot,
                    target=target / "Movie (2025)" / "Movie.2025.mkv",
                    role="video",
                    media_group="movie:1",
                    action="skip",
                    note="保留现有版本",
                )
            ],
            "_cleanup_candidates": [object()],
            "_retained_paths": [],
            "matches": [{
                "tmdb_id": "1",
                "title": "Movie",
                "year": "2025",
                "media_type": "movie",
            }],
        }
        qb = FakeQB()

        with patch.object(service, "preview", return_value=preview), patch(
            "app.modules.local_media_service.delete_cleanup_items",
            side_effect=RuntimeError("injected cleanup failure"),
        ), self.assertRaisesRegex(
            Exception, "qB 任务已完成移除.*收尾失败",
        ):
            service.execute_task("admin", task_id, qb_client=qb)

        self.assertEqual(
            qb.calls,
            [("pause", "hash-1"), ("delete", "hash-1", False)],
        )
        self.assertTrue(source_file.exists())
        task = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert task is not None
        self.assertEqual(task.status, "requires_manual")
        self.assertIn("qB 任务已完成移除", task.error)

    def test_qb_task_without_client_fails_before_any_move(self):
        root, source, _target, task_id, service = self._fixture()
        self.addCleanup(root.cleanup)
        with self.assertRaisesRegex(Exception, "缺少可用客户端"):
            service.execute_task("admin", task_id, qb_client=None)
        self.assertTrue((source / "Movie.2025.mkv").exists())
        get_local_media_task_result_3 = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert get_local_media_task_result_3 is not None
        self.assertEqual(get_local_media_task_result_3.status, "failed")


if __name__ == "__main__":
    unittest.main()
