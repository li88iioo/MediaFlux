"""本地媒体调度器去重、稳定等待和生命周期测试。"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import database as db
from app.clients.qbittorrent import TorrentTask
import app.modules.local_media_scheduler as local_media_scheduler_module
from app.modules.local_media_scheduler import (
    LocalMediaProbeRetryable,
    LocalMediaScheduler,
    LocalMediaSourceAmbiguous,
    LocalMediaSourceMigrationRequired,
)
from app.modules.local_storage import LocalFilesystemAdapter, LocalScanLimitExceeded
from tests.support import IsolatedDatabaseTestCase


class FakeService:
    def __init__(self): self.calls = []
    def execute_task(self, owner, task_id, qb_client=None):
        self.calls.append((owner, task_id, qb_client))
        db.update_local_media_task(task_id, owner=owner, status="completed", completed_at=db.now())
        return {"status": "completed"}


class LocalMediaSchedulerTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM local_media_tasks")
            conn.execute("DELETE FROM local_media_sources")

    @staticmethod
    def torrent(path: str, hash_value: str = "hash-1") -> TorrentTask:
        return TorrentTask(hash_value, "Movie", 1.0, "uploading", "/downloads", path,
                           1, 1, 0, 0, 0, 0, "", 0)

    def test_completed_torrent_longest_mapping_is_deduplicated_and_processed(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); all_root = root / "all"; fast_root = root / "fast"
            all_root.mkdir(); fast_root.mkdir(); movie = fast_root / "Movie.mkv"; movie.write_bytes(b"movie")
            db.create_local_media_source(
                name="all", qb_profile="configured:qb", qb_path_prefix="/downloads",
                local_root=str(all_root), stable_seconds=0, owner="admin",
            )
            fast_id = db.create_local_media_source(
                name="fast", qb_profile="configured:qb", qb_path_prefix="/downloads/1",
                local_root=str(fast_root), stable_seconds=300, owner="admin",
            )
            service = FakeService(); qb = Mock()
            scheduler = LocalMediaScheduler(service=service, qb_factory=lambda: qb)
            first = scheduler.enqueue_completed_torrent(self.torrent("/downloads/1/Movie.mkv"))
            second = scheduler.enqueue_completed_torrent(self.torrent("/downloads/1/Movie.mkv"))
            self.assertEqual(first, second)
            self.assertEqual(db.get_local_media_task(first, owner="admin").source_id, fast_id)
            self.assertEqual(scheduler.run_once(), 1)
            self.assertEqual(service.calls, [("admin", first, qb)])
            qb.close.assert_called_once_with()

    def test_owned_qb_client_is_closed_when_task_execution_fails(self):
        class FailingService:
            @staticmethod
            def execute_task(owner, task_id, qb_client=None):
                del owner, task_id, qb_client
                raise RuntimeError("injected execution failure")

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            movie = root / "Movie.mkv"
            movie.write_bytes(b"movie")
            source_id = db.create_local_media_source(
                name="qb-close-error",
                qb_profile="configured:qb",
                qb_path_prefix="/downloads",
                local_root=str(root),
                stable_seconds=0,
                owner="admin",
            )
            task_id = db.create_local_media_task(
                source_id,
                "hash-close-error",
                str(movie),
                owner="admin",
                trigger="qb_completed",
            )
            qb = Mock()
            scheduler = LocalMediaScheduler(
                service=FailingService(), qb_factory=lambda: qb
            )

            self.assertEqual(scheduler.run_once(), 0)

            qb.close.assert_called_once_with()
            self.assertEqual(
                db.get_local_media_task(task_id, owner="admin").status, "failed"
            )

    def test_post_move_error_falls_back_to_requires_manual(self):
        from app.modules.local_media_service import LocalMediaPostMoveError

        class PostMoveFailingService:
            @staticmethod
            def execute_task(owner, task_id, qb_client=None):
                del owner, task_id, qb_client
                raise LocalMediaPostMoveError("移动已提交，收尾失败")

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            movie = root / "Movie.mkv"
            movie.write_bytes(b"movie")
            source_id = db.create_local_media_source(
                name="qb-post-move-error",
                qb_profile="configured:qb",
                qb_path_prefix="/downloads",
                local_root=str(root),
                stable_seconds=0,
                owner="admin",
            )
            task_id = db.create_local_media_task(
                source_id,
                "hash-post-move-error",
                str(movie),
                owner="admin",
                trigger="qb_completed",
            )
            qb = Mock()
            scheduler = LocalMediaScheduler(
                service=PostMoveFailingService(), qb_factory=lambda: qb
            )

            self.assertEqual(scheduler.run_once(), 0)

            qb.close.assert_called_once_with()
            self.assertEqual(
                db.get_local_media_task(task_id, owner="admin").status,
                "requires_manual",
            )

    def test_completed_torrent_request_is_bound_before_scheduler_wakeup(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            movie = root / "Movie.mkv"
            movie.write_bytes(b"movie")
            db.create_local_media_source(
                name="atomic-link", qb_profile="configured:qb",
                qb_path_prefix="/downloads", local_root=str(root),
                stable_seconds=0, owner="admin",
            )
            request_id, _ = db.create_download_request("scheduler-atomic", "magnet")
            scheduler = LocalMediaScheduler(service=FakeService())

            task_id = scheduler.enqueue_completed_torrent(
                self.torrent("/downloads/Movie.mkv", hash_value="scheduler-atomic"),
                wake=False, request_id=request_id,
            )

            request = db.get_download_request(request_id)
            self.assertEqual(request["local_import_status"], "pending")
            self.assertEqual(request["local_import_target"], f"local-media-task:{task_id}")
            self.assertEqual(request["qb_content_path"], str(movie))
            self.assertTrue(request["local_import_started_at"])

    def test_completed_torrent_matching_legacy_source_requires_container_path_migration(self):
        db.create_local_media_source(
            name="legacy", qb_profile="configured:qb", qb_path_prefix=r"D:\Downloads",
            local_root=r"D:\Downloads", stable_seconds=0, owner="admin",
        )
        scheduler = LocalMediaScheduler(service=FakeService())

        with self.assertRaisesRegex(
            LocalMediaSourceMigrationRequired, "Windows/UNC.*Docker 容器路径",
        ):
            scheduler.enqueue_completed_torrent(
                self.torrent(r"D:\Downloads\Movie.mkv", hash_value="legacy-source")
            )

        self.assertEqual(db.list_local_media_tasks(owner="admin"), [])

    def test_completed_torrent_matching_relative_source_requires_absolute_container_path(self):
        db.create_local_media_source(
            name="relative", qb_profile="configured:qb", qb_path_prefix="/downloads",
            local_root="relative-downloads", stable_seconds=0, owner="admin",
        )
        scheduler = LocalMediaScheduler(service=FakeService())

        with self.assertRaisesRegex(
            LocalMediaSourceMigrationRequired, "Docker 容器内绝对路径",
        ):
            scheduler.enqueue_completed_torrent(
                self.torrent("/downloads/Movie.mkv", hash_value="relative-source")
            )

        self.assertEqual(db.list_local_media_tasks(owner="admin"), [])

    def test_windows_and_unc_qb_prefixes_map_into_container_sources(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            drive_root = root / "drive"
            unc_root = root / "unc"
            drive_root.mkdir()
            unc_root.mkdir()
            (drive_root / "Movie.mkv").write_bytes(b"movie")
            (unc_root / "Show.mkv").write_bytes(b"show")
            drive_id = db.create_local_media_source(
                name="drive", qb_profile="configured:qb", qb_path_prefix=r"D:\Downloads",
                local_root=str(drive_root), stable_seconds=0, owner="admin",
            )
            unc_id = db.create_local_media_source(
                name="unc", qb_profile="configured:qb", qb_path_prefix=r"\\NAS\Media",
                local_root=str(unc_root), stable_seconds=0, owner="admin",
            )
            scheduler = LocalMediaScheduler(service=FakeService())

            drive_task = scheduler.enqueue_completed_torrent(
                self.torrent(r"d:\DOWNLOADS\Movie.mkv", hash_value="drive-prefix")
            )
            unc_task = scheduler.enqueue_completed_torrent(
                self.torrent(r"\\nas\media\Show.mkv", hash_value="unc-prefix")
            )

            stored_drive = db.get_local_media_task(drive_task, owner="admin")
            stored_unc = db.get_local_media_task(unc_task, owner="admin")
            self.assertEqual(stored_drive.source_id, drive_id)
            self.assertEqual(Path(stored_drive.content_path), drive_root / "Movie.mkv")
            self.assertEqual(stored_unc.source_id, unc_id)
            self.assertEqual(Path(stored_unc.content_path), unc_root / "Show.mkv")

    def test_migrated_source_wins_when_legacy_source_shares_qb_prefix(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            migrated_root = root / "downloads"
            migrated_root.mkdir()
            (migrated_root / "Movie.mkv").write_bytes(b"movie")
            db.create_local_media_source(
                name="legacy", qb_profile="configured:qb",
                qb_path_prefix=r"D:\Downloads", local_root=r"D:\Downloads",
                stable_seconds=0, owner="admin",
            )
            migrated_id = db.create_local_media_source(
                name="migrated", qb_profile="configured:qb",
                qb_path_prefix=r"D:\Downloads", local_root=str(migrated_root),
                stable_seconds=0, owner="admin",
            )
            scheduler = LocalMediaScheduler(service=FakeService())

            task_id = scheduler.enqueue_completed_torrent(
                self.torrent(r"d:\downloads\Movie.mkv", hash_value="migrated-source")
            )

            task = db.get_local_media_task(task_id, owner="admin")
            self.assertEqual(task.source_id, migrated_id)
            self.assertEqual(Path(task.content_path), migrated_root / "Movie.mkv")

    def test_equal_prefix_checks_all_sources_and_selects_only_actual_match(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            empty_root = root / "empty"
            actual_root = root / "actual"
            empty_root.mkdir()
            actual_root.mkdir()
            (actual_root / "Movie.mkv").write_bytes(b"movie")
            db.create_local_media_source(
                name="empty", qb_profile="configured:qb", qb_path_prefix="/downloads",
                local_root=str(empty_root), stable_seconds=0, owner="admin",
            )
            actual_id = db.create_local_media_source(
                name="actual", qb_profile="configured:qb", qb_path_prefix="/downloads",
                local_root=str(actual_root), stable_seconds=0, owner="admin",
            )
            scheduler = LocalMediaScheduler(service=FakeService())

            task_id = scheduler.enqueue_completed_torrent(
                self.torrent("/downloads/Movie.mkv", hash_value="equal-prefix")
            )

            task = db.get_local_media_task(task_id, owner="admin")
            self.assertEqual(task.source_id, actual_id)
            self.assertEqual(Path(task.content_path), actual_root / "Movie.mkv")

    def test_equal_prefix_multiple_actual_matches_are_explicitly_ambiguous(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            for name in ("first", "second"):
                source_root = root / name
                source_root.mkdir()
                (source_root / "Movie.mkv").write_bytes(b"movie")
                db.create_local_media_source(
                    name=name, qb_profile="configured:qb", qb_path_prefix="/downloads",
                    local_root=str(source_root), stable_seconds=0, owner="admin",
                )
            scheduler = LocalMediaScheduler(service=FakeService())

            with self.assertRaisesRegex(
                LocalMediaSourceAmbiguous, "同时命中多个本地媒体来源",
            ):
                scheduler.enqueue_completed_torrent(
                    self.torrent("/downloads/Movie.mkv", hash_value="ambiguous-prefix")
                )

            self.assertEqual(db.list_local_media_tasks(owner="admin"), [])

    def test_symlink_source_is_a_visible_configuration_failure(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            actual = root / "actual"
            link = root / "downloads"
            actual.mkdir()
            (actual / "Movie.mkv").write_bytes(b"movie")
            try:
                link.symlink_to(actual, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("当前平台不支持符号链接测试")
            db.create_local_media_source(
                name="symlink", qb_profile="configured:qb", qb_path_prefix="/downloads",
                local_root=str(link), stable_seconds=0, owner="admin",
            )
            scheduler = LocalMediaScheduler(service=FakeService())

            with self.assertRaisesRegex(LocalMediaSourceMigrationRequired, "符号链接"):
                scheduler.enqueue_completed_torrent(
                    self.torrent("/downloads/Movie.mkv", hash_value="symlink-source")
                )

            self.assertEqual(db.list_local_media_tasks(owner="admin"), [])

    def test_completed_torrent_filters_non_media_content(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source = root / "downloads"
            source.mkdir()
            notes = source / "readme.txt"
            notes.write_text("not media")
            db.create_local_media_source(
                name="local", qb_profile="configured:qb", qb_path_prefix="/downloads",
                local_root=str(source), stable_seconds=0, owner="admin",
            )
            scheduler = LocalMediaScheduler(service=FakeService())

            task_id = scheduler.enqueue_completed_torrent(
                self.torrent("/downloads/readme.txt", hash_value="non-media")
            )

            self.assertIsNone(task_id)
            self.assertEqual(db.list_local_media_tasks(owner="admin"), [])

    def test_completed_torrent_missing_path_is_retryable(self):
        with tempfile.TemporaryDirectory() as root_raw:
            source = Path(root_raw) / "downloads"
            source.mkdir()
            db.create_local_media_source(
                name="local", qb_profile="configured:qb", qb_path_prefix="/downloads",
                local_root=str(source), stable_seconds=0, owner="admin",
            )
            scheduler = LocalMediaScheduler(service=FakeService())

            with self.assertRaisesRegex(LocalMediaProbeRetryable, "扫描路径不存在"):
                scheduler.enqueue_completed_torrent(
                    self.torrent("/downloads/Movie.mkv", hash_value="mount-late")
                )

            self.assertEqual(db.list_local_media_tasks(owner="admin"), [])

    def test_completed_torrent_scan_limit_is_retryable(self):
        with tempfile.TemporaryDirectory() as root_raw:
            source = Path(root_raw) / "downloads"
            source.mkdir()
            movie = source / "Movie.mkv"
            movie.write_bytes(b"movie")
            db.create_local_media_source(
                name="local", qb_profile="configured:qb", qb_path_prefix="/downloads",
                local_root=str(source), stable_seconds=0, owner="admin",
            )
            scheduler = LocalMediaScheduler(service=FakeService())

            with patch.object(
                LocalFilesystemAdapter,
                "contains_video",
                side_effect=LocalScanLimitExceeded("目录文件数量超过安全上限"),
            ):
                with self.assertRaisesRegex(LocalMediaProbeRetryable, "目录文件数量超过安全上限"):
                    scheduler.enqueue_completed_torrent(
                        self.torrent("/downloads/Movie.mkv", hash_value="too-many")
                    )

            self.assertEqual(db.list_local_media_tasks(owner="admin"), [])

    def test_legacy_windows_source_tasks_fail_with_actionable_migration_guidance(self):
        source_id = db.create_local_media_source(
            name="旧 Windows 来源", qb_profile="", qb_path_prefix=r"D:\Downloads",
            local_root=r"D:\Downloads", stable_seconds=0, owner="admin",
        )
        target_root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(target_root, ignore_errors=True))
        db.upsert_local_library_target(
            source_id, "default", str(target_root), owner="admin",
        )
        task_id = db.create_local_media_task(
            source_id, "", r"D:\Downloads\Movie.mkv", owner="admin", trigger="manual",
        )
        scheduler = LocalMediaScheduler(service=FakeService())

        self.assertEqual(scheduler.run_once(), 0)
        task = db.get_local_media_task(task_id, owner="admin")
        self.assertEqual(task.status, "failed")
        self.assertIn("Windows/UNC", task.error)
        self.assertIn("Docker 容器路径", task.error)

        manual_result = scheduler.enqueue_manual_scan_candidates()
        self.assertEqual(manual_result["queued_count"], 0)
        self.assertIn("Windows/UNC", manual_result["sources"][0]["error"])

    def test_relative_source_tasks_fail_before_filesystem_access(self):
        source_id = db.create_local_media_source(
            name="旧相对路径来源", qb_profile="", qb_path_prefix="/downloads",
            local_root="relative-downloads", stable_seconds=0, owner="admin",
        )
        task_id = db.create_local_media_task(
            source_id, "", "relative-downloads/Movie.mkv",
            owner="admin", trigger="manual",
        )
        scheduler = LocalMediaScheduler(service=FakeService())

        with patch("app.modules.local_media_scheduler.LocalFilesystemAdapter.scan") as scan:
            self.assertEqual(scheduler.run_once(), 0)
        scan.assert_not_called()
        task = db.get_local_media_task(task_id, owner="admin")
        self.assertEqual(task.status, "failed")
        self.assertIn("Docker 容器内绝对路径", task.error)

    def test_legacy_stable_seconds_do_not_delay_explicit_manual_task(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source = root / "source"; source.mkdir()
            movie = source / "Movie.mkv"; movie.write_bytes(b"movie")
            source_id = db.create_local_media_source(
                name="legacy-stable", qb_profile="", qb_path_prefix="",
                local_root=str(source), stable_seconds=300, owner="admin",
            )
            task_id = db.create_local_media_task(
                source_id, "", str(movie), owner="admin", trigger="manual",
            )
            service = FakeService()
            scheduler = LocalMediaScheduler(service=service)

            with patch("app.modules.local_media_scheduler.LocalFilesystemAdapter.scan") as scan:
                self.assertEqual(scheduler.run_once(), 1)
            scan.assert_not_called()
            self.assertEqual(db.get_local_media_task(task_id, owner="admin").status, "completed")
            self.assertEqual(service.calls, [("admin", task_id, None)])

    def test_qb_completed_task_uses_no_scheduler_prescan(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); movie = root / "Movie.mkv"; movie.write_bytes(b"movie")
            db.create_local_media_source(
                name="qb", qb_profile="configured:qb", qb_path_prefix="/downloads",
                local_root=str(root), stable_seconds=300, owner="admin",
            )
            scheduler = LocalMediaScheduler(service=FakeService())
            task_id = scheduler.enqueue_completed_torrent(self.torrent("/downloads/Movie.mkv"))

            with patch("app.modules.local_media_scheduler.LocalFilesystemAdapter.scan") as scan:
                self.assertEqual(scheduler.run_once(), 1)
            scan.assert_not_called()
            self.assertEqual(db.get_local_media_task(task_id, owner="admin").status, "completed")

    def test_notification_failure_does_not_overwrite_completed_task(self):
        with tempfile.TemporaryDirectory() as root_raw:
            source_root = Path(root_raw) / "source"
            source_root.mkdir()
            movie = source_root / "Movie.mkv"
            movie.write_bytes(b"movie")
            source_id = db.create_local_media_source(
                name="local", qb_profile="", qb_path_prefix="", local_root=str(source_root),
                stable_seconds=0, owner="admin",
            )
            task_id = db.create_local_media_task(
                source_id, "", str(movie), owner="admin", trigger="manual",
            )
            request_id, _ = db.create_download_request("notify-completed", "magnet")
            db.link_download_request_to_local_media_task(request_id, task_id, str(movie))
            scheduler = LocalMediaScheduler(service=FakeService())

            with patch(
                "app.modules.local_media_scheduler.notify_local_media_task",
                side_effect=RuntimeError("telegram unavailable"),
            ):
                self.assertEqual(scheduler.run_once(), 1)

            task = db.get_local_media_task(task_id, owner="admin")
            request = db.get_download_request(request_id)
            self.assertEqual(task.status, "completed")
            self.assertEqual(request["local_import_status"], "completed")

    def test_notification_failure_does_not_overwrite_manual_review_task(self):
        class ManualReviewService:
            @staticmethod
            def execute_task(owner, task_id, qb_client=None):
                del qb_client
                db.update_local_media_task(
                    task_id, owner=owner, status="requires_manual", error="需要人工确认",
                )
                return {
                    "status": "requires_manual",
                    "preview": {"reason": "需要人工确认"},
                }

        with tempfile.TemporaryDirectory() as root_raw:
            source_root = Path(root_raw) / "source"
            source_root.mkdir()
            movie = source_root / "Movie.mkv"
            movie.write_bytes(b"movie")
            source_id = db.create_local_media_source(
                name="local", qb_profile="", qb_path_prefix="", local_root=str(source_root),
                stable_seconds=0, owner="admin",
            )
            task_id = db.create_local_media_task(
                source_id, "", str(movie), owner="admin", trigger="manual",
            )
            request_id, _ = db.create_download_request("notify-manual", "magnet")
            db.link_download_request_to_local_media_task(request_id, task_id, str(movie))
            scheduler = LocalMediaScheduler(service=ManualReviewService())

            with patch(
                "app.modules.local_media_scheduler.notify_local_media_task",
                side_effect=RuntimeError("telegram unavailable"),
            ):
                self.assertEqual(scheduler.run_once(), 1)

            task = db.get_local_media_task(task_id, owner="admin")
            request = db.get_download_request(request_id)
            self.assertEqual(task.status, "requires_manual")
            self.assertEqual(request["local_import_status"], "requires_manual")

    def test_legacy_stable_wait_executes_once_without_repeated_scan(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source = root / "source"; source.mkdir()
            movie = source / "Movie.mkv"; movie.write_bytes(b"movie")
            source_id = db.create_local_media_source(
                name="stable", qb_profile="", qb_path_prefix="", local_root=str(source),
                stable_seconds=300, owner="admin",
            )
            task_id = db.create_local_media_task(
                source_id, "", str(movie), owner="admin", trigger="manual",
            )
            scheduler = LocalMediaScheduler(service=FakeService())

            with patch("app.modules.local_media_scheduler.LocalFilesystemAdapter.scan") as scan:
                self.assertEqual(scheduler.run_once(), 1)
                self.assertEqual(scheduler.run_once(), 0)
            scan.assert_not_called()
            self.assertEqual(db.get_local_media_task(task_id, owner="admin").status, "completed")

    def test_preview_only_source_is_manual_only_and_not_auto_enqueued(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source = root / "source"; source.mkdir()
            (source / "Movie.mkv").write_bytes(b"movie")
            db.create_local_media_source(
                name="preview", qb_profile="", qb_path_prefix="", local_root=str(source),
                scan_enabled=True, stable_seconds=0, mode="preview_only", owner="admin",
            )
            scheduler = LocalMediaScheduler(service=FakeService())
            self.assertEqual(scheduler.run_once(), 0)
            self.assertEqual(db.list_local_media_tasks(owner="admin"), [])

    def test_init_db_marks_interrupted_local_write_for_manual_review(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source = root / "source"; source.mkdir()
            source_id = db.create_local_media_source(
                name="move", qb_profile="", qb_path_prefix="", local_root=str(source), owner="admin",
            )
            task_id = db.create_local_media_task(source_id, "hash-interrupted", str(source), owner="admin")
            task = db.get_local_media_task(task_id, owner="admin")
            step_id = db.add_local_media_operation_step(task_id, task.operation_token, 0, "move", owner="admin")
            db.update_local_media_task(task_id, owner="admin", status="moving")
            db.update_local_media_operation_step(step_id, "running")
            db.init_db()
            recovered = db.get_local_media_task(task_id, owner="admin")
            steps = db.list_local_media_operation_steps(task_id, owner="admin")
            self.assertEqual(recovered.status, "requires_manual")
            self.assertFalse(recovered.completed_at)
            self.assertIn("人工核验", recovered.error)
            self.assertEqual(steps[0]["status"], "failed")
            self.assertIn("人工核验", steps[0]["error"])

    def test_legacy_timed_scan_settings_do_not_enqueue_automatically(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "source"; source_root.mkdir()
            (source_root / "Movie.mkv").write_bytes(b"movie")
            db.create_local_media_source(
                name="legacy-scan", qb_profile="", qb_path_prefix="",
                local_root=str(source_root), enabled=False, scan_enabled=True,
                scan_interval_minutes=10, stable_seconds=300, owner="admin",
            )
            scheduler = LocalMediaScheduler(service=FakeService())

            self.assertEqual(scheduler.run_once(), 0)
            self.assertEqual(db.list_local_media_tasks(owner="admin"), [])

    def test_preexisting_automatic_scan_task_is_stopped_with_migration_message(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); movie = root / "Movie.mkv"; movie.write_bytes(b"movie")
            source_id = db.create_local_media_source(
                name="legacy-scan", qb_profile="", qb_path_prefix="", local_root=str(root),
                scan_enabled=True, stable_seconds=0, owner="admin",
            )
            task_id = db.create_local_media_task(
                source_id, "", str(movie), owner="admin", trigger="scan",
            )

            self.assertEqual(LocalMediaScheduler(service=FakeService()).run_once(), 0)
            task = db.get_local_media_task(task_id, owner="admin")
            self.assertEqual(task.status, "failed")
            self.assertIn("定时扫描已移除", task.error)

    def test_manual_scan_enqueues_existing_media_without_scan_switch_and_suppresses_bulk_notice(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "downloads"
            target_root = root / "library"
            source_root.mkdir()
            target_root.mkdir()
            movie = source_root / "Existing.Movie.2026.mkv"
            movie.write_bytes(b"movie")
            source_id = db.create_local_media_source(
                name="已有下载", qb_profile="", qb_path_prefix="",
                local_root=str(source_root), enabled=False, scan_enabled=False,
                stable_seconds=300, owner="admin",
            )
            db.upsert_local_library_target(
                source_id, "default", str(target_root), owner="admin",
            )
            service = FakeService()
            scheduler = LocalMediaScheduler(service=service)

            with patch(
                "app.modules.local_media_scheduler.notify_local_media_task"
            ) as notify:
                result = scheduler.enqueue_manual_scan_candidates(silent=True)
                self.assertEqual(result["source_count"], 1)
                self.assertEqual(result["scanned_sources"], 1)
                self.assertEqual(result["candidate_count"], 1)
                self.assertEqual(result["queued_count"], 1)
                task = db.get_local_media_task(result["task_ids"][0], owner="admin")
                self.assertEqual(task.trigger, "scan")
                self.assertTrue(task.operation_token.startswith("silent-manual-scan:"))
                self.assertEqual(scheduler.run_once(), 1)

            notify.assert_not_called()
            self.assertEqual(len(service.calls), 1)
            self.assertEqual(
                db.get_local_media_task(result["task_ids"][0], owner="admin").status,
                "completed",
            )

    def test_silent_manual_review_can_capture_result_for_telegram_card(self):
        class ManualReviewService:
            @staticmethod
            def execute_task(owner, task_id, qb_client=None):
                del qb_client
                db.update_local_media_task(
                    task_id, owner=owner, status="requires_manual", error="匹配置信度不足",
                )
                return {
                    "status": "requires_manual",
                    "preview": {
                        "reason": "匹配置信度不足",
                        "snapshot_digest": "digest-1",
                        "rules_snapshot": "{}",
                        "files": [{"name": "Movie.mkv"}],
                        "candidate": {
                            "tmdb_id": "1", "media_type": "movie",
                            "title": "候选电影", "provider": "tmdb",
                        },
                    },
                }

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "downloads"
            target_root = root / "library"
            source_root.mkdir()
            target_root.mkdir()
            (source_root / "Movie.mkv").write_bytes(b"movie")
            source_id = db.create_local_media_source(
                name="已有下载", qb_profile="", qb_path_prefix="",
                local_root=str(source_root), enabled=False, scan_enabled=False,
                stable_seconds=0, owner="admin",
            )
            db.upsert_local_library_target(
                source_id, "default", str(target_root), owner="admin",
            )
            scheduler = LocalMediaScheduler(service=ManualReviewService())
            batch = scheduler.enqueue_manual_scan_candidates(
                silent=True, capture_results=True,
            )
            task_id = batch["task_ids"][0]

            self.assertEqual(scheduler.run_once(), 1)
            captured = scheduler.take_captured_task_result(task_id)

        self.assertEqual(captured["status"], "requires_manual")
        self.assertEqual(captured["preview"]["candidate"]["tmdb_id"], "1")
        self.assertIsNone(scheduler.take_captured_task_result(task_id))

    def test_reused_manual_review_rebuilds_preview_for_telegram_card(self):
        class RecoverableReviewService:
            @staticmethod
            def inspect_task(owner, task_id):
                self.assertEqual(owner, "admin")
                self.assertGreater(task_id, 0)
                return {"inspection_id": "inspection-1", "digest": "digest-1"}

            @staticmethod
            def preview(owner, inspection_id, tmdb_id, media_type, **kwargs):
                self.assertEqual(owner, "admin")
                self.assertEqual(inspection_id, "inspection-1")
                self.assertEqual(tmdb_id, "")
                self.assertEqual(media_type, "")
                self.assertTrue(kwargs["automatic"])
                return {
                    "status": "requires_manual",
                    "reason": "匹配置信度不足",
                    "snapshot_digest": "digest-1",
                    "rules_snapshot": "{}",
                    "files": [{"name": "Movie.mkv"}],
                    "candidate": {
                        "tmdb_id": "1", "media_type": "movie",
                        "title": "候选电影", "provider": "tmdb",
                    },
                    "candidates": [{
                        "tmdb_id": "1", "media_type": "movie",
                        "title": "候选电影", "provider": "tmdb",
                    }],
                }

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "downloads"
            target_root = root / "library"
            source_root.mkdir()
            target_root.mkdir()
            movie = source_root / "Movie.mkv"
            movie.write_bytes(b"movie")
            source_id = db.create_local_media_source(
                name="已有下载", qb_profile="", qb_path_prefix="",
                local_root=str(source_root), enabled=False, scan_enabled=False,
                stable_seconds=0, owner="admin",
            )
            db.upsert_local_library_target(
                source_id, "default", str(target_root), owner="admin",
            )
            task_id = db.create_local_media_task(
                source_id, "", str(movie), owner="admin", trigger="scan",
            )
            db.update_local_media_task(
                task_id, owner="admin", status="requires_manual",
                snapshot_digest="digest-1", error="匹配置信度不足",
            )
            scheduler = LocalMediaScheduler(service=RecoverableReviewService())

            batch = scheduler.enqueue_manual_scan_candidates(
                silent=True, capture_results=True,
            )
            captured = scheduler.take_captured_task_result(task_id)

        self.assertEqual(batch["task_ids"], [task_id])
        self.assertEqual(captured["status"], "requires_manual")
        self.assertEqual(captured["preview"]["candidate"]["tmdb_id"], "1")
        self.assertIsNone(scheduler.take_captured_task_result(task_id))

    def test_manual_scan_filters_non_media_files_and_directories(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "downloads"
            target_root = root / "library"
            media_dir = source_root / "Movie.2026"
            non_media_dir = source_root / "Documents"
            media_dir.mkdir(parents=True)
            non_media_dir.mkdir()
            target_root.mkdir()
            (media_dir / "Movie.2026.mkv").write_bytes(b"movie")
            (media_dir / "readme.txt").write_text("ignored")
            (non_media_dir / "archive.zip").write_bytes(b"archive")
            (source_root / "notes.txt").write_text("ignored")
            source_id = db.create_local_media_source(
                name="本地下载", qb_profile="", qb_path_prefix="",
                local_root=str(source_root), enabled=False, scan_enabled=False,
                stable_seconds=0, owner="admin",
            )
            db.upsert_local_library_target(
                source_id, "default", str(target_root), owner="admin",
            )

            result = LocalMediaScheduler(service=FakeService()).enqueue_manual_scan_candidates()

            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(result["queued_count"], 1)
            task = db.get_local_media_task(result["task_ids"][0], owner="admin")
            self.assertEqual(Path(task.content_path), media_dir / "Movie.2026.mkv")

    def test_manual_scan_splits_mixed_collection_into_independent_media_units(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "downloads"
            target_root = root / "library"
            source_root.mkdir()
            target_root.mkdir()

            loose_root = source_root / "Loose.Movie.2026.mkv"
            loose_root.write_bytes(b"movie")
            movie_dir = source_root / "Movie (2026)"
            movie_dir.mkdir()
            (movie_dir / "Movie.2026.mkv").write_bytes(b"movie")

            anime = source_root / "动漫"
            anime.mkdir()
            loose_anime = anime / "Loose.Anime.S01E03.mkv"
            loose_anime.write_bytes(b"episode")
            show_a = anime / "Show A (2026) {tmdb-101}"
            show_b = anime / "Show B (2026) {tmdb-102}"
            (show_a / "Season 01").mkdir(parents=True)
            (show_b / "Season 02").mkdir(parents=True)
            (show_a / "Season 01" / "Show.A.S01E01.mkv").write_bytes(b"episode")
            (show_b / "Season 02" / "Show.B.S02E02.mkv").write_bytes(b"episode")

            wrapper = source_root / "Unsorted"
            wrapped_show = wrapper / "Show C (2026)"
            (wrapped_show / "Season 01").mkdir(parents=True)
            (wrapped_show / "Season 01" / "Show.C.S01E04.mkv").write_bytes(b"episode")

            source_id = db.create_local_media_source(
                name="混合下载", qb_profile="", qb_path_prefix="",
                local_root=str(source_root), enabled=False, scan_enabled=False,
                stable_seconds=0, owner="admin",
            )
            db.upsert_local_library_target(
                source_id, "default", str(target_root), owner="admin",
            )

            result = LocalMediaScheduler(service=FakeService()).enqueue_manual_scan_candidates()

            expected = {
                loose_root,
                movie_dir / "Movie.2026.mkv",
                loose_anime,
                show_a / "Season 01" / "Show.A.S01E01.mkv",
                show_b / "Season 02" / "Show.B.S02E02.mkv",
                wrapped_show / "Season 01" / "Show.C.S01E04.mkv",
            }
            tasks = [
                db.get_local_media_task(task_id, owner="admin")
                for task_id in result["task_ids"]
            ]
            self.assertEqual(result["candidate_count"], len(expected))
            self.assertEqual(result["queued_count"], len(expected))
            self.assertEqual({Path(task.content_path) for task in tasks}, expected)
            self.assertNotIn(anime, {Path(task.content_path) for task in tasks})
            self.assertNotIn(wrapper, {Path(task.content_path) for task in tasks})

    def test_manual_scan_skips_preview_and_missing_targets_without_false_errors(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            preview_root = root / "preview"
            missing_target_root = root / "missing-target"
            preview_root.mkdir()
            missing_target_root.mkdir()
            (preview_root / "Preview.mkv").write_bytes(b"movie")
            (missing_target_root / "No.Target.mkv").write_bytes(b"movie")
            preview_id = db.create_local_media_source(
                name="仅预览", qb_profile="", qb_path_prefix="",
                local_root=str(preview_root), mode="preview_only", owner="admin",
            )
            db.upsert_local_library_target(
                preview_id, "default", str(root / "library"), owner="admin",
            )
            db.create_local_media_source(
                name="无目标", qb_profile="", qb_path_prefix="",
                local_root=str(missing_target_root), owner="admin",
            )

            result = LocalMediaScheduler(service=FakeService()).enqueue_manual_scan_candidates()

            self.assertEqual(result["source_count"], 2)
            self.assertEqual(result["scanned_sources"], 0)
            self.assertEqual(result["candidate_count"], 0)
            self.assertEqual(result["queued_count"], 0)
            self.assertEqual(result["task_ids"], [])
            self.assertTrue(all(item["skipped"] for item in result["sources"]))
            self.assertTrue(all(not item["error"] for item in result["sources"]))
            self.assertEqual(
                {item["reason"] for item in result["sources"]},
                {"来源处于仅预览模式", "尚未配置归档目标"},
            )

    def test_parallel_planning_uses_two_workers_and_one_stable_writer(self):
        class ParallelService:
            def __init__(self):
                self.lock = threading.Lock()
                self.barrier = threading.Barrier(2)
                self.planning_active = 0
                self.planning_peak = 0
                self.writer_active = 0
                self.writer_peak = 0
                self.commit_order: list[int] = []
                self.closed_workers = 0

            @staticmethod
            def parallel_planning_safe():
                return True

            def create_planning_worker(self):
                parent = self

                class Worker:
                    def prepare_task(self, owner, task_id):
                        del owner, task_id
                        with parent.lock:
                            parent.planning_active += 1
                            parent.planning_peak = max(
                                parent.planning_peak, parent.planning_active,
                            )
                        try:
                            parent.barrier.wait(timeout=2)
                            time.sleep(0.02)
                        finally:
                            with parent.lock:
                                parent.planning_active -= 1
                        return {"status": "planned"}

                    def close(self):
                        with parent.lock:
                            parent.closed_workers += 1
                        return True

                return Worker()

            def execute_task(self, owner, task_id, qb_client=None):
                del qb_client
                with self.lock:
                    self.writer_active += 1
                    self.writer_peak = max(self.writer_peak, self.writer_active)
                    self.commit_order.append(int(task_id))
                try:
                    time.sleep(0.01)
                    db.update_local_media_task(
                        task_id, owner=owner, status="completed",
                        completed_at=db.now(),
                    )
                    return {"status": "completed"}
                finally:
                    with self.lock:
                        self.writer_active -= 1

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            files = [root / "A.mkv", root / "B.mkv"]
            for media in files:
                media.write_bytes(b"movie")
            source_id = db.create_local_media_source(
                name="parallel", qb_profile="", qb_path_prefix="",
                local_root=str(root), stable_seconds=0, owner="admin",
            )
            task_ids = [
                db.create_local_media_task(
                    source_id, "", str(media), owner="admin", trigger="manual",
                )
                for media in files
            ]
            service = ParallelService()
            scheduler = LocalMediaScheduler(service=service)

            with patch(
                "app.modules.local_media_scheduler.resolve_local_media_organize_workers",
                return_value=2,
            ), patch(
                "app.modules.local_media_scheduler.notify_local_media_task",
            ):
                self.assertEqual(scheduler.run_once(), 2)

        self.assertEqual(service.planning_peak, 2)
        self.assertEqual(service.writer_peak, 1)
        self.assertEqual(service.commit_order, task_ids)
        self.assertEqual(service.closed_workers, 2)
        self.assertTrue(all(
            db.get_local_media_task(task_id, owner="admin").status == "completed"
            for task_id in task_ids
        ))

    def test_parallel_planning_defers_ancestor_descendant_path_overlap(self):
        class PlanningService:
            def __init__(self):
                self.prepared: list[int] = []

            @staticmethod
            def parallel_planning_safe():
                return True

            def create_planning_worker(self):
                parent = self

                class Worker:
                    def prepare_task(self, owner, task_id):
                        del owner
                        parent.prepared.append(int(task_id))
                        return {"status": "planned"}

                    @staticmethod
                    def close():
                        return True

                return Worker()

            @staticmethod
            def execute_task(owner, task_id, qb_client=None):
                del qb_client
                db.update_local_media_task(
                    task_id, owner=owner, status="completed",
                    completed_at=db.now(),
                )
                return {"status": "completed"}

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            child = root / "Movie.mkv"
            child.write_bytes(b"movie")
            source_id = db.create_local_media_source(
                name="overlap", qb_profile="", qb_path_prefix="",
                local_root=str(root), stable_seconds=0, owner="admin",
            )
            parent_task = db.create_local_media_task(
                source_id, "", str(root), owner="admin", trigger="manual",
            )
            # Public admission rejects overlapping paths. Insert one legacy row
            # directly to prove the scheduler still serializes pre-upgrade data.
            timestamp = db.now()
            with db.get_conn() as conn:
                cursor = conn.execute(
                    "INSERT INTO local_media_tasks("
                    "owner,source_id,qb_hash,content_path,trigger,status,"
                    "operation_token,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,'waiting_stable',?,?,?)",
                    (
                        "admin", source_id, None, str(child), "manual",
                        "legacy-overlap-child", timestamp, timestamp,
                    ),
                )
                child_task = int(cursor.lastrowid)
            service = PlanningService()
            scheduler = LocalMediaScheduler(service=service)

            with patch(
                "app.modules.local_media_scheduler.resolve_local_media_organize_workers",
                return_value=2,
            ), patch(
                "app.modules.local_media_scheduler.notify_local_media_task",
            ):
                self.assertEqual(scheduler.run_once(), 1)
                self.assertEqual(
                    db.get_local_media_task(child_task, owner="admin").status,
                    "waiting_stable",
                )
                self.assertEqual(scheduler.run_once(), 1)

        self.assertEqual(service.prepared, [parent_task, child_task])

    def test_parallel_planning_failure_falls_back_to_authoritative_writer(self):
        class FallbackService(FakeService):
            def __init__(self):
                super().__init__()
                self.prepared: list[int] = []
                self.closed_workers = 0

            @staticmethod
            def parallel_planning_safe():
                return True

            def create_planning_worker(self):
                parent = self

                class Worker:
                    def prepare_task(self, owner, task_id):
                        del owner
                        parent.prepared.append(int(task_id))
                        raise RuntimeError("injected warmup failure")

                    def close(self):
                        parent.closed_workers += 1
                        return True

                return Worker()

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            media = root / "Fallback.mkv"
            media.write_bytes(b"movie")
            source_id = db.create_local_media_source(
                name="fallback", qb_profile="", qb_path_prefix="",
                local_root=str(root), stable_seconds=0, owner="admin",
            )
            task_id = db.create_local_media_task(
                source_id, "", str(media), owner="admin", trigger="manual",
            )
            service = FallbackService()
            scheduler = LocalMediaScheduler(service=service)

            with patch(
                "app.modules.local_media_scheduler.resolve_local_media_organize_workers",
                return_value=2,
            ), patch(
                "app.modules.local_media_scheduler.notify_local_media_task",
            ):
                self.assertEqual(scheduler.run_once(), 1)

        self.assertEqual(service.prepared, [task_id])
        self.assertEqual(service.calls[0][1], task_id)
        self.assertEqual(service.closed_workers, 1)
        self.assertEqual(
            db.get_local_media_task(task_id, owner="admin").status,
            "completed",
        )
        self.assertEqual(scheduler._path_locks, set())

    def test_parallel_planning_creates_qb_client_only_for_writer_and_closes_it(self):
        qb_created = threading.Event()
        qb_client = Mock()

        class QbService(FakeService):
            @staticmethod
            def parallel_planning_safe():
                return True

            @staticmethod
            def create_planning_worker():
                class Worker:
                    @staticmethod
                    def prepare_task(owner, task_id):
                        del owner, task_id
                        if qb_created.is_set():
                            raise AssertionError("qB client created during read-only planning")
                        return {"status": "planned"}

                    @staticmethod
                    def close():
                        return True

                return Worker()

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            media = root / "Qb.Movie.mkv"
            media.write_bytes(b"movie")
            source_id = db.create_local_media_source(
                name="qb-planning", qb_profile="configured:qb",
                qb_path_prefix="/downloads", local_root=str(root),
                stable_seconds=0, owner="admin",
            )
            task_id = db.create_local_media_task(
                source_id, "hash-planning", str(media), owner="admin",
                trigger="qb_completed",
            )

            def qb_factory():
                qb_created.set()
                return qb_client

            service = QbService()
            scheduler = LocalMediaScheduler(
                service=service, qb_factory=qb_factory,
            )
            with patch(
                "app.modules.local_media_scheduler.resolve_local_media_organize_workers",
                return_value=2,
            ), patch(
                "app.modules.local_media_scheduler.notify_local_media_task",
            ):
                self.assertEqual(scheduler.run_once(), 1)

        self.assertTrue(qb_created.is_set())
        self.assertIs(service.calls[0][2], qb_client)
        qb_client.close.assert_called_once_with()
        self.assertEqual(
            db.get_local_media_task(task_id, owner="admin").status,
            "completed",
        )

    def test_out_of_order_parallel_reviews_are_captured_by_task_id_once(self):
        class ReviewService:
            def __init__(self):
                self.completed_prepares: list[int] = []
                self.lock = threading.Lock()

            @staticmethod
            def parallel_planning_safe():
                return True

            def create_planning_worker(self):
                parent = self

                class Worker:
                    def prepare_task(self, owner, task_id):
                        del owner
                        time.sleep({task_ids[0]: 0.05, task_ids[1]: 0.01}.get(
                            int(task_id), 0.0,
                        ))
                        with parent.lock:
                            parent.completed_prepares.append(int(task_id))
                        return {"status": "requires_manual"}

                    @staticmethod
                    def close():
                        return True

                return Worker()

            @staticmethod
            def execute_task(owner, task_id, qb_client=None):
                del qb_client
                db.update_local_media_task(
                    task_id, owner=owner, status="requires_manual",
                    error="人工确认", completed_at=None,
                )
                return {
                    "status": "requires_manual",
                    "task_id": int(task_id),
                    "preview": {
                        "candidate": {"tmdb_id": str(task_id)},
                        "reason": "人工确认",
                    },
                }

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            files = [root / f"Review-{index}.mkv" for index in range(3)]
            for media in files:
                media.write_bytes(b"movie")
            source_id = db.create_local_media_source(
                name="reviews", qb_profile="", qb_path_prefix="",
                local_root=str(root), stable_seconds=0, owner="admin",
            )
            task_ids = [
                db.create_local_media_task(
                    source_id, "", str(media), owner="admin", trigger="manual",
                )
                for media in files
            ]
            service = ReviewService()
            scheduler = LocalMediaScheduler(service=service)
            scheduler._capture_result_task_ids.update(task_ids)

            with patch(
                "app.modules.local_media_scheduler.resolve_local_media_organize_workers",
                return_value=2,
            ), patch(
                "app.modules.local_media_scheduler.notify_local_media_task",
            ):
                self.assertEqual(scheduler.run_once(), 3)

            captured = {
                task_id: scheduler.take_captured_task_result(task_id)
                for task_id in task_ids
            }

        self.assertNotEqual(service.completed_prepares[0], task_ids[0])
        self.assertEqual(
            {
                task_id: result["preview"]["candidate"]["tmdb_id"]
                for task_id, result in captured.items()
            },
            {task_id: str(task_id) for task_id in task_ids},
        )
        self.assertTrue(all(
            scheduler.take_captured_task_result(task_id) is None
            for task_id in task_ids
        ))
        self.assertEqual(scheduler._capture_result_task_ids, set())

    def test_nsfw_source_keeps_serial_execution_without_planning_workers(self):
        class SerialNsfwService(FakeService):
            def __init__(self):
                super().__init__()
                self.worker_calls = 0

            @staticmethod
            def parallel_planning_safe():
                return True

            def create_planning_worker(self):
                self.worker_calls += 1
                raise AssertionError("NSFW must stay serial")

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            media = root / "FJIN-140.mp4"
            media.write_bytes(b"movie")
            source_id = db.create_local_media_source(
                name="nsfw", qb_profile="", qb_path_prefix="",
                local_root=str(root), stable_seconds=0, owner="admin",
                media_type="nsfw",
            )
            task_id = db.create_local_media_task(
                source_id, "", str(media), owner="admin", trigger="manual",
            )
            service = SerialNsfwService()
            scheduler = LocalMediaScheduler(service=service)

            with patch(
                "app.modules.local_media_scheduler.resolve_local_media_organize_workers",
                return_value=2,
            ), patch(
                "app.modules.local_media_scheduler.notify_local_media_task",
            ):
                self.assertEqual(scheduler.run_once(), 1)

        self.assertEqual(service.worker_calls, 0)
        self.assertEqual(service.calls[0][1], task_id)

    def test_shutdown_waits_for_inflight_parallel_planners_before_service_close(self):
        class OwnedParallelService:
            def __init__(self):
                self.lock = threading.Lock()
                self.release = threading.Event()
                self.planners_entered = threading.Event()
                self.active_planners = 0
                self.close_calls = 0
                self.closed_workers = 0

            @staticmethod
            def parallel_planning_safe():
                return True

            def create_planning_worker(self):
                parent = self

                class Worker:
                    def prepare_task(self, owner, task_id):
                        del owner, task_id
                        with parent.lock:
                            parent.active_planners += 1
                            if parent.active_planners == 2:
                                parent.planners_entered.set()
                        parent.release.wait(timeout=2)
                        return {"status": "planned"}

                    def close(self):
                        with parent.lock:
                            parent.closed_workers += 1
                        return True

                return Worker()

            @staticmethod
            def execute_task(owner, task_id, qb_client=None):
                del qb_client
                db.update_local_media_task(
                    task_id, owner=owner, status="completed",
                    completed_at=db.now(),
                )
                return {"status": "completed"}

            def close(self):
                with self.lock:
                    self.close_calls += 1
                return True

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            files = [root / "One.mkv", root / "Two.mkv"]
            for media in files:
                media.write_bytes(b"movie")
            source_id = db.create_local_media_source(
                name="shutdown-parallel", qb_profile="", qb_path_prefix="",
                local_root=str(root), stable_seconds=0, owner="admin",
            )
            task_ids = [
                db.create_local_media_task(
                    source_id, "", str(media), owner="admin", trigger="manual",
                )
                for media in files
            ]
            service = OwnedParallelService()
            scheduler = LocalMediaScheduler(
                service=service, interval=0.2,
            )
            scheduler._owns_service = True
            shutdown_result: list[bool] = []
            shutdown_thread = threading.Thread(
                target=lambda: shutdown_result.append(scheduler.shutdown(timeout=2)),
            )
            try:
                with patch(
                    "app.modules.local_media_scheduler.resolve_local_media_organize_workers",
                    return_value=2,
                ), patch(
                    "app.modules.local_media_scheduler.notify_local_media_task",
                ):
                    scheduler.start()
                    self.assertTrue(service.planners_entered.wait(timeout=1))
                    shutdown_thread.start()
                    time.sleep(0.05)
                    self.assertTrue(shutdown_thread.is_alive())
                    self.assertEqual(service.close_calls, 0)
                    service.release.set()
                    shutdown_thread.join(timeout=2)
            finally:
                service.release.set()
                shutdown_thread.join(timeout=2)
                scheduler.stop(timeout=2)

        self.assertFalse(shutdown_thread.is_alive())
        self.assertEqual(shutdown_result, [True])
        self.assertEqual(service.close_calls, 1)
        self.assertEqual(service.closed_workers, 2)
        self.assertIsNone(scheduler._thread)
        self.assertTrue(all(
            db.get_local_media_task(task_id, owner="admin").status == "completed"
            for task_id in task_ids
        ))

    def test_start_stop_is_reentrant_and_leaves_no_thread(self):
        scheduler = LocalMediaScheduler(service=FakeService(), interval=0.2)
        self.assertEqual(scheduler.status(), {"running": False, "interval_seconds": 0.2})
        scheduler.start(); scheduler.start(); scheduler.stop(); scheduler.stop()
        self.assertIsNone(scheduler._thread)
        self.assertEqual(scheduler.status(), {"running": False, "interval_seconds": 0.2})

    def test_shutdown_blocks_restart_while_joining_old_thread(self):
        service = Mock()
        service.close.return_value = True
        scheduler = LocalMediaScheduler(service=service, interval=0.2)
        scheduler._owns_service = True

        class OldThread:
            alive = True

            def is_alive(self):
                return self.alive

            def join(self, timeout=None):
                del timeout
                self.alive = False
                scheduler.start()

        old_thread = OldThread()
        scheduler._thread = old_thread

        self.assertTrue(scheduler.shutdown())
        self.assertIsNone(scheduler._thread)
        service.close.assert_called_once_with()

    def test_successful_global_shutdown_releases_instance_for_rebuild(self):
        previous = local_media_scheduler_module._scheduler
        self.addCleanup(
            setattr, local_media_scheduler_module, "_scheduler", previous,
        )
        service = Mock()
        service.close.return_value = True
        scheduler = LocalMediaScheduler(service=service)
        scheduler._owns_service = True
        local_media_scheduler_module._scheduler = scheduler

        self.assertTrue(scheduler.shutdown())
        self.assertIsNone(local_media_scheduler_module._scheduler)

        replacement = LocalMediaScheduler(service=FakeService())
        with patch.object(
            local_media_scheduler_module, "LocalMediaScheduler", return_value=replacement,
        ):
            self.assertIs(
                local_media_scheduler_module.get_local_media_scheduler(), replacement,
            )

    def test_incomplete_service_close_keeps_global_scheduler_reference(self):
        previous = local_media_scheduler_module._scheduler
        self.addCleanup(
            setattr, local_media_scheduler_module, "_scheduler", previous,
        )
        service = Mock()
        service.close.return_value = False
        scheduler = LocalMediaScheduler(service=service)
        scheduler._owns_service = True
        local_media_scheduler_module._scheduler = scheduler

        self.assertFalse(scheduler.shutdown())
        self.assertIs(local_media_scheduler_module._scheduler, scheduler)
        service.close.assert_called_once_with()

    def test_status_reports_running_without_starting_additional_work(self):
        scheduler = LocalMediaScheduler(service=FakeService(), interval=0.2)
        scheduler.run_once = Mock(return_value=0)
        scheduler.start()
        try:
            self.assertTrue(scheduler.status()["running"])
        finally:
            scheduler.stop()
        self.assertFalse(scheduler.status()["running"])


if __name__ == "__main__": unittest.main()
