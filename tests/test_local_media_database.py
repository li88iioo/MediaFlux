"""本地媒体来源、目标和任务数据库契约。"""
from __future__ import annotations

import sqlite3
import threading
import unittest
from typing import Iterable, cast

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.repositories import local_media as local_media_repo
from app.modules.local_media_models import LocalMediaSource, LocalMediaTask
from app.modules.process_lock import CrossProcessLock
from tests.support import IsolatedDatabaseTestCase


def _request_snapshot(request_id: int, columns: Iterable[str]) -> tuple:
    row = download_requests_repo.get_download_request(request_id)
    assert row is not None
    return tuple(row[column] for column in columns)


class LocalMediaDatabaseTests(IsolatedDatabaseTestCase):
    def test_active_lookup_resolves_legacy_symlink_paths_on_each_query(self):
        import tempfile
        from pathlib import Path
        from app.repositories import local_media as repository

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first, second, alias = root / "First", root / "Second", root / "Alias"
            first.mkdir(); second.mkdir(); alias.symlink_to(first, target_is_directory=True)
            source_id = local_media_repo.create_local_media_source(
                name="dynamic-path", qb_profile="", qb_path_prefix="", local_root=str(root),
            )
            task_id = local_media_repo.create_local_media_task(source_id, "", str(first / "Movie.mkv"))
            with db.get_conn() as conn:
                conn.execute("UPDATE local_media_tasks SET content_path=? WHERE id=?", (str(alias / "Movie.mkv"), task_id))
                match = repository._active_local_media_task_for_path(conn, source_id=source_id, owner="admin", content_path=str(first / "Movie.mkv"))
                self.assertEqual(match["id"] if match else None, task_id)
                alias.unlink(); alias.symlink_to(second, target_is_directory=True)
                match = repository._active_local_media_task_for_path(conn, source_id=source_id, owner="admin", content_path=str(second / "Movie.mkv"))
                self.assertEqual(match["id"] if match else None, task_id)
                self.assertIsNone(repository._active_local_media_task_for_path(conn, source_id=source_id, owner="admin", content_path=str(first / "Movie.mkv")))

    def test_active_lookup_streams_candidates_and_resolves_each_path_once(self):
        from unittest.mock import patch
        from app.modules import local_media_models as models
        from app.repositories import local_media as repository

        owner = "stream-owner"
        source_id = local_media_repo.create_local_media_source(
            name="stream-source", qb_profile="", qb_path_prefix="",
            local_root="/tmp/stream-source", owner=owner,
        )
        calls = {"fetchall": 0, "iterated": 0}

        class Cursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchall(self):
                rows = self.cursor.fetchall()
                calls["fetchall"] += len(rows)
                return rows

            def __iter__(self):
                for row in self.cursor:
                    calls["iterated"] += 1
                    yield row

        class Connection:
            def execute(self, *args):
                return Cursor(conn.execute(*args))

        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO local_media_tasks(owner,source_id,content_path,operation_token,status,created_at,updated_at) VALUES(?,?,?,?,'recognizing',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                [(owner, source_id, f"/tmp/stream-source/Movie-{index}.mkv", f"stream-{index}") for index in range(30)],
            )
            with patch.object(models, "canonical_local_media_content_path", wraps=models.canonical_local_media_content_path) as canonical:
                result = repository._active_local_media_task_for_path(
                    cast(sqlite3.Connection, Connection()), source_id=source_id, owner=owner,
                    content_path="/tmp/stream-source/Missing.mkv",
                )
        self.assertIsNone(result)
        self.assertEqual(calls, {"fetchall": 0, "iterated": 30})
        self.assertEqual(canonical.call_count, 30)

    def test_waiting_task_queue_is_oldest_first_beyond_scheduler_batch_limit(self):
        owner = "queue-order-owner"
        source_id = local_media_repo.create_local_media_source(
            name="queue-order", qb_profile="", qb_path_prefix="",
            local_root="/tmp/queue-order", owner=owner,
        )
        task_ids = [
            local_media_repo.create_local_media_task(
                source_id,
                "",
                f"/tmp/queue-order/Movie-{index:03d}.mkv",
                owner=owner,
                trigger="manual",
            )
            for index in range(501)
        ]

        waiting = local_media_repo.list_waiting_local_media_tasks(owner=owner, limit=500)

        self.assertEqual([task.id for task in waiting], task_ids[:500])
        self.assertIn(task_ids[0], {task.id for task in waiting})
        self.assertNotIn(task_ids[-1], {task.id for task in waiting})

    def test_source_media_type_accepts_explicit_nsfw_and_rejects_unknown_values(self):
        source_id = local_media_repo.create_local_media_source(
            name="adult-source", qb_profile="", qb_path_prefix="",
            local_root="/tmp/adult-source", media_type="nsfw", owner="admin",
        )
        get_local_media_source_result = local_media_repo.get_local_media_source(source_id, owner="admin")
        assert get_local_media_source_result is not None
        self.assertEqual(
            get_local_media_source_result.media_type,
            "nsfw",
        )
        with self.assertRaises(ValueError):
            local_media_repo.create_local_media_source(
                name="bad-source", qb_profile="", qb_path_prefix="",
                local_root="/tmp/bad-source", media_type="unknown", owner="admin",
            )

    def test_source_target_and_task_round_trip(self):
        source_id = local_media_repo.create_local_media_source(
            name="qB 下载目录 1",
            qb_profile="configured:qb",
            qb_path_prefix="/downloads/1",
            local_root="/mnt/downloads/1",
            enabled=1,
            stable_seconds=300,
            scan_enabled=1,
            scan_interval_minutes=10,
            owner="admin",
        )
        target_id = local_media_repo.upsert_local_library_target(
            source_id, "movie", "/mnt/media/Movies",
            provider="jellyfin", library_name="电影", library_id="movies",
            server_path="//NAS/Media/Movies", owner="admin",
        )
        task_id = local_media_repo.create_local_media_task(
            source_id, "hash-1", "/mnt/downloads/1/Movie.mkv", owner="admin"
        )

        source = local_media_repo.get_local_media_source(source_id, owner="admin")
        task = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert isinstance(source, LocalMediaSource)
        assert isinstance(task, LocalMediaTask)
        self.assertEqual(source.mode, "move")
        self.assertEqual(
            (source.stable_seconds, source.scan_enabled, source.scan_interval_minutes),
            (0, False, 10),
        )
        self.assertEqual(task.status, "waiting_stable")
        target = local_media_repo.list_local_library_targets(source_id, owner="admin")[0]
        self.assertEqual(target.id, target_id)
        self.assertEqual(target.library_id, "movies")
        self.assertEqual(target.library_name, "电影")
        self.assertEqual(target.server_path, "//NAS/Media/Movies")
        self.assertTrue(local_media_repo.claim_local_media_task(task_id, expected="waiting_stable", owner="admin"))
        self.assertFalse(local_media_repo.claim_local_media_task(task_id, expected="waiting_stable", owner="admin"))
        get_local_media_task_result = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert get_local_media_task_result is not None
        self.assertEqual(get_local_media_task_result.status, "recognizing")

    def test_concurrent_scan_task_creation_reuses_one_active_task(self):
        source_id = local_media_repo.create_local_media_source(
            name="concurrent-scan", qb_profile="", qb_path_prefix="",
            local_root="/tmp/concurrent-scan", owner="admin",
        )
        workers = 8
        barrier = threading.Barrier(workers + 1)
        task_ids: list[int] = []
        errors: list[BaseException] = []
        result_lock = threading.Lock()

        def create_task() -> None:
            barrier.wait()
            try:
                task_id = local_media_repo.create_local_media_task(
                    source_id, "", "/tmp/concurrent-scan/Movie.mkv",
                    owner="admin", trigger="scan",
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                with result_lock:
                    errors.append(exc)
            else:
                with result_lock:
                    task_ids.append(task_id)

        threads = [threading.Thread(target=create_task) for _ in range(workers)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(task_ids), workers)
        self.assertEqual(len(set(task_ids)), 1)
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT id FROM local_media_tasks WHERE source_id=? AND content_path=?",
                (source_id, "/tmp/concurrent-scan/Movie.mkv"),
            ).fetchall()
        self.assertEqual([int(row["id"]) for row in rows], [task_ids[0]])

    def test_active_task_admission_rejects_ancestor_and_descendant_overlap(self):
        source_id = local_media_repo.create_local_media_source(
            name="path-overlap", qb_profile="", qb_path_prefix="",
            local_root="/tmp/path-overlap", owner="admin",
        )
        parent_id = local_media_repo.create_local_media_task(
            source_id, "", "/tmp/path-overlap/Show", owner="admin", trigger="manual",
        )

        with self.assertRaisesRegex(ValueError, "范围重叠"):
            local_media_repo.create_local_media_task(
                source_id, "", "/tmp/path-overlap/Show/E01.mkv",
                owner="admin", trigger="manual",
            )

        local_media_repo.update_local_media_task(parent_id, owner="admin", status="completed")
        child_id = local_media_repo.create_local_media_task(
            source_id, "", "/tmp/path-overlap/Show/E01.mkv",
            owner="admin", trigger="manual",
        )
        with self.assertRaisesRegex(ValueError, "范围重叠"):
            local_media_repo.create_local_media_task(
                source_id, "", "/tmp/path-overlap/Show",
                owner="admin", trigger="manual",
            )

        local_media_repo.update_local_media_task(child_id, owner="admin", status="failed")
        retried_parent = local_media_repo.create_local_media_task(
            source_id, "", "/tmp/path-overlap/Show",
            owner="admin", trigger="manual",
        )
        self.assertNotEqual(retried_parent, parent_id)

    def test_generic_task_update_cannot_mutate_content_path_identity(self):
        source_id = local_media_repo.create_local_media_source(
            name="immutable-task-path", qb_profile="", qb_path_prefix="",
            local_root="/tmp/immutable-task-path", owner="admin",
        )
        task_id = local_media_repo.create_local_media_task(
            source_id, "", "/tmp/immutable-task-path/Movie.mkv", owner="admin",
        )

        with self.assertRaisesRegex(ValueError, "原子准入接口"):
            local_media_repo.update_local_media_task(
                task_id, owner="admin",
                content_path="/tmp/immutable-task-path/Other.mkv",
            )

        get_local_media_task_result_2 = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert get_local_media_task_result_2 is not None
        self.assertEqual(
            get_local_media_task_result_2.content_path,
            "/tmp/immutable-task-path/Movie.mkv",
        )

    def test_qb_binding_reuses_manual_confirmation_without_resetting_state(self):
        source_id = local_media_repo.create_local_media_source(
            name="manual-qb-single-track", qb_profile="configured:qb",
            qb_path_prefix="/downloads", local_root="/tmp/single-track", owner="admin",
        )
        task_id = local_media_repo.create_local_media_task(
            source_id, "", "/tmp/single-track/season/../Movie.mkv",
            owner="admin", trigger="manual", operation_token="manual-operation-token",
        )
        local_media_repo.update_local_media_task(
            task_id,
            owner="admin",
            status="requires_manual",
            snapshot_digest="snapshot-v1",
            rules_snapshot='{"rule":"manual"}',
            recognition_summary='{"candidate":"42"}',
            tmdb_id="42",
            media_type="tv",
            season_override=2,
            episode_override=7,
            numbering_mode="season_continuous",
            title="保留人工选择",
            year="2026",
            error="等待人工确认",
            warning="保留提示",
        )
        local_media_repo.add_local_media_task_item(
            task_id,
            "/tmp/single-track/Movie.mkv",
            "/tmp/library/Movie.mkv",
            role="video",
            owner="admin",
        )
        before = local_media_repo.get_local_media_task(task_id, owner="admin")
        request_id, _ = download_requests_repo.create_download_request("manual-qb-binding", "magnet")

        linked_id, restarted = local_media_repo.create_and_link_qb_local_media_task(
            request_id,
            source_id,
            "HASH-MANUAL-BINDING",
            "/tmp/single-track/./Movie.mkv",
            owner="admin",
        )

        self.assertEqual(linked_id, task_id)
        self.assertFalse(restarted)
        after = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert after is not None
        self.assertEqual(after.qb_hash, "hash-manual-binding")
        self.assertEqual(after.content_path, "/tmp/single-track/Movie.mkv")
        assert before is not None
        self.assertEqual(
            (
                after.status,
                after.trigger,
                after.operation_token,
                after.snapshot_digest,
                after.rules_snapshot,
                after.recognition_summary,
                after.tmdb_id,
                after.media_type,
                after.season_override,
                after.episode_override,
                after.numbering_mode,
                after.title,
                after.year,
                after.error,
                after.warning,
                after.version,
            ),
            (
                before.status,
                before.trigger,
                before.operation_token,
                before.snapshot_digest,
                before.rules_snapshot,
                before.recognition_summary,
                before.tmdb_id,
                before.media_type,
                before.season_override,
                before.episode_override,
                before.numbering_mode,
                before.title,
                before.year,
                before.error,
                before.warning,
                before.version,
            ),
        )
        self.assertEqual(len(local_media_repo.list_local_media_task_items(task_id, owner="admin")), 1)
        get_download_request_result = download_requests_repo.get_download_request(request_id)
        assert get_download_request_result is not None
        self.assertEqual(
            get_download_request_result["local_import_target"],
            f"local-media-task:{task_id}",
        )

    def test_manual_admission_reuses_active_qb_task_by_canonical_path(self):
        source_id = local_media_repo.create_local_media_source(
            name="qb-manual-single-track", qb_profile="configured:qb",
            qb_path_prefix="/downloads", local_root="/tmp/qb-manual", owner="admin",
        )
        request_id, _ = download_requests_repo.create_download_request("qb-manual-binding", "magnet")
        qb_task_id, restarted = local_media_repo.create_and_link_qb_local_media_task(
            request_id,
            source_id,
            "HASH-QB-FIRST",
            "/tmp/qb-manual/Movie.mkv",
            owner="admin",
        )
        manual_task_id = local_media_repo.create_local_media_task(
            source_id,
            "",
            "/tmp/qb-manual/subdir/../Movie.mkv",
            owner="admin",
            trigger="manual",
        )

        self.assertFalse(restarted)
        self.assertEqual(manual_task_id, qb_task_id)
        task = local_media_repo.get_local_media_task(qb_task_id, owner="admin")
        assert task is not None
        self.assertEqual(task.qb_hash, "hash-qb-first")
        self.assertEqual(task.trigger, "qb_completed")

    def test_active_content_path_cannot_be_admitted_by_another_source(self):
        first_source = local_media_repo.create_local_media_source(
            name="path-owner-one", qb_profile="", qb_path_prefix="",
            local_root="/tmp/shared-path", owner="admin",
        )
        second_source = local_media_repo.create_local_media_source(
            name="path-owner-two", qb_profile="", qb_path_prefix="",
            local_root="/tmp/shared-path/subdir", owner="admin",
        )
        task_id = local_media_repo.create_local_media_task(
            first_source,
            "",
            "/tmp/shared-path/Movie.mkv",
            owner="admin",
            trigger="manual",
        )

        with self.assertRaisesRegex(ValueError, "其他本地媒体来源"):
            local_media_repo.create_local_media_task(
                second_source,
                "",
                "/tmp/shared-path/child/../Movie.mkv",
                owner="admin",
                trigger="manual",
            )
        self.assertEqual(len(local_media_repo.list_local_media_tasks(owner="admin")), 1)
        self.assertEqual(local_media_repo.list_local_media_tasks(owner="admin")[0].id, task_id)

    def test_terminal_task_projects_legacy_local_import_states(self):
        source_id = local_media_repo.create_local_media_source(
            name="terminal-projection", qb_profile="configured:qb",
            qb_path_prefix="/downloads", local_root="/tmp/terminal-projection",
            owner="admin",
        )

        for index, request_status in enumerate(("pending", "requires_manual", "failed")):
            for task_status in ("completed", "failed", "requires_manual", "planned"):
                with self.subTest(request_status=request_status, task_status=task_status):
                    content_path = f"/tmp/terminal-projection/{index}-{task_status}.mkv"
                    task_id = local_media_repo.create_local_media_task(
                        source_id, "", content_path, owner="admin", trigger="manual",
                    )
                    request_id, _ = download_requests_repo.create_download_request(
                        f"terminal-projection-{index}-{task_status}", "magnet",
                    )
                    self.assertTrue(
                        download_requests_repo.link_download_request_to_local_media_task(
                            request_id, task_id, content_path,
                        )
                    )

                    legacy_error = f"legacy-{request_status}"
                    legacy_completed_at = f"legacy-{request_status}-completed-at"
                    with db.get_conn() as conn:
                        conn.execute(
                            "UPDATE download_requests SET local_import_status=?,"
                            "local_import_error=?,local_import_completed_at=? WHERE id=?",
                            (request_status, legacy_error, legacy_completed_at, request_id),
                        )

                    task_error = f"task-{task_status}-error"
                    task_completed_at = (
                        None if task_status == "planned"
                        else f"task-{task_status}-completed-at"
                    )
                    fields = {"status": task_status, "error": task_error}
                    if task_completed_at is not None:
                        fields["completed_at"] = task_completed_at
                    self.assertTrue(
                        local_media_repo.update_local_media_task(task_id, owner="admin", **fields)
                    )

                    task = local_media_repo.get_local_media_task(task_id, owner="admin")
                    request = download_requests_repo.get_download_request(request_id)
                    assert request is not None
                    self.assertEqual(request["local_import_status"], task_status)
                    self.assertEqual(request["local_import_error"], task_error)
                    self.assertEqual(
                        request["local_import_completed_at"], task_completed_at,
                    )
                    assert task is not None
                    self.assertEqual(request["updated_at"], task.updated_at)

    def test_reused_task_does_not_rewrite_historical_completed_or_cancelled_requests(self):
        source_id = local_media_repo.create_local_media_source(
            name="terminal-history", qb_profile="configured:qb",
            qb_path_prefix="/downloads", local_root="/tmp/terminal-history",
            owner="admin",
        )
        first_request, _ = download_requests_repo.create_download_request("terminal-history-first", "magnet")
        task_id, restarted = local_media_repo.create_and_link_qb_local_media_task(
            first_request, source_id, "HASH-TERMINAL-HISTORY",
            "/tmp/terminal-history/Movie.mkv", owner="admin",
        )
        self.assertFalse(restarted)
        first_completed_at = "2026-09-12 10:00:00"
        self.assertTrue(local_media_repo.update_local_media_task(
            task_id,
            owner="admin",
            status="completed",
            error="first terminal result",
            completed_at=first_completed_at,
        ))
        download_requests_repo.update_download_request(
            first_request,
            status="completed",
            completed_at="2026-09-12 10:01:00",
        )

        cancelled_request, _ = download_requests_repo.create_download_request(
            "terminal-history-cancelled", "magnet",
        )
        self.assertTrue(download_requests_repo.link_download_request_to_local_media_task(
            cancelled_request, task_id, "/tmp/terminal-history/Movie.mkv",
        ))
        download_requests_repo.update_download_request(
            cancelled_request,
            status="cancelled",
            local_import_status="failed",
            local_import_error="keep cancelled history",
            local_import_completed_at="2026-09-12 10:02:00",
            completed_at="2026-09-12 10:03:00",
        )
        historical_before = {
            request_id: _request_snapshot(request_id, (
                "status", "local_import_status", "local_import_error",
                "local_import_completed_at", "completed_at", "updated_at",
            ))
            for request_id in (first_request, cancelled_request)
        }

        successor_request, _ = download_requests_repo.create_download_request(
            "terminal-history-successor", "magnet",
        )
        reused_id, restarted = local_media_repo.create_and_link_qb_local_media_task(
            successor_request, source_id, "hash-terminal-history",
            "/tmp/terminal-history/readded/Movie.mkv", owner="admin",
        )
        self.assertEqual(reused_id, task_id)
        self.assertTrue(restarted)
        self.assertTrue(local_media_repo.update_local_media_task(
            task_id,
            owner="admin",
            status="completed",
            error="successor terminal result",
            completed_at="2026-09-12 10:04:00",
        ))

        historical_after = {
            request_id: _request_snapshot(request_id, (
                "status", "local_import_status", "local_import_error",
                "local_import_completed_at", "completed_at", "updated_at",
            ))
            for request_id in (first_request, cancelled_request)
        }
        self.assertEqual(historical_after, historical_before)

    def test_wrong_owner_or_missing_task_does_not_project_download_request(self):
        source_id = local_media_repo.create_local_media_source(
            name="projection-owner", qb_profile="", qb_path_prefix="",
            local_root="/tmp/projection-owner", owner="admin",
        )
        task_id = local_media_repo.create_local_media_task(
            source_id, "", "/tmp/projection-owner/Movie.mkv", owner="admin",
        )
        request_id, _ = download_requests_repo.create_download_request("projection-owner-request", "magnet")
        self.assertTrue(download_requests_repo.link_download_request_to_local_media_task(
            request_id, task_id, "/tmp/projection-owner/Movie.mkv",
        ))
        columns = (
            "status", "local_import_status", "local_import_error",
            "local_import_completed_at", "updated_at",
        )
        before = _request_snapshot(request_id, columns)

        self.assertFalse(local_media_repo.update_local_media_task(
            task_id, owner="other", status="completed", error="wrong owner",
            completed_at="2026-09-12 11:00:00",
        ))
        self.assertFalse(local_media_repo.update_local_media_task(
            10**9, owner="admin", status="completed", error="missing task",
            completed_at="2026-09-12 11:01:00",
        ))

        get_local_media_task_result_3 = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert get_local_media_task_result_3 is not None
        self.assertEqual(get_local_media_task_result_3.status, "waiting_stable")
        after = _request_snapshot(request_id, columns)
        self.assertEqual(after, before)

    def test_request_projection_failure_rolls_back_local_media_task_update(self):
        source_id = local_media_repo.create_local_media_source(
            name="projection-rollback", qb_profile="", qb_path_prefix="",
            local_root="/tmp/projection-rollback", owner="admin",
        )
        content_path = "/tmp/projection-rollback/Movie.mkv"
        task_id = local_media_repo.create_local_media_task(source_id, "", content_path, owner="admin")
        request_id, _ = download_requests_repo.create_download_request("projection-rollback-request", "magnet")
        self.assertTrue(download_requests_repo.link_download_request_to_local_media_task(
            request_id, task_id, content_path,
        ))
        task_before = local_media_repo.get_local_media_task(task_id, owner="admin")
        request_before = download_requests_repo.get_download_request(request_id)
        trigger_name = "abort_local_media_projection_test"
        with db.get_conn() as conn:
            conn.execute(
                f"CREATE TRIGGER {trigger_name} "
                "BEFORE UPDATE OF local_import_status,local_import_error,"
                "local_import_completed_at ON download_requests "
                f"WHEN NEW.local_import_target='local-media-task:{task_id}' "
                "BEGIN SELECT RAISE(ABORT, 'injected request update failure'); END"
            )
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "injected request update failure"):
                local_media_repo.update_local_media_task(
                    task_id,
                    owner="admin",
                    status="completed",
                    error="must rollback",
                    completed_at="2026-09-12 12:00:00",
                )
        finally:
            with db.get_conn() as conn:
                conn.execute(f"DROP TRIGGER {trigger_name}")

        task_after = local_media_repo.get_local_media_task(task_id, owner="admin")
        request_after = download_requests_repo.get_download_request(request_id)
        assert task_after is not None
        assert task_before is not None
        self.assertEqual(
            (task_after.status, task_after.error, task_after.completed_at, task_after.version),
            (task_before.status, task_before.error, task_before.completed_at, task_before.version),
        )
        assert request_after is not None
        assert request_before is not None
        self.assertEqual(
            tuple(request_after[column] for column in (
                "local_import_status", "local_import_error",
                "local_import_completed_at", "updated_at",
            )),
            tuple(request_before[column] for column in (
                "local_import_status", "local_import_error",
                "local_import_completed_at", "updated_at",
            )),
        )

    def test_init_db_reconciles_legacy_terminal_tasks_idempotently(self):
        source_id = local_media_repo.create_local_media_source(
            name="startup-projection", qb_profile="", qb_path_prefix="",
            local_root="/tmp/startup-projection", owner="admin",
        )
        legacy_cases = (
            ("completed", "pending", "legacy completed error", "2026-09-12 13:00:00"),
            ("failed", "requires_manual", "legacy failed error", "2026-09-12 13:01:00"),
            ("requires_manual", "failed", "legacy manual error", "2026-09-12 13:02:00"),
        )
        expected = []
        for index, (task_status, request_status, task_error, completed_at) in enumerate(
            legacy_cases
        ):
            content_path = f"/tmp/startup-projection/{index}.mkv"
            task_id = local_media_repo.create_local_media_task(
                source_id, "", content_path, owner="admin", trigger="scan",
            )
            request_id, _ = download_requests_repo.create_download_request(
                f"startup-projection-{index}", "magnet",
            )
            self.assertTrue(download_requests_repo.link_download_request_to_local_media_task(
                request_id, task_id, content_path,
            ))
            task_updated_at = f"2026-09-12 13:0{index}:30"
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE local_media_tasks SET status=?,error=?,completed_at=?,updated_at=? "
                    "WHERE id=?",
                    (task_status, task_error, completed_at, task_updated_at, task_id),
                )
                conn.execute(
                    "UPDATE download_requests SET local_import_status=?,"
                    "local_import_error=?,local_import_completed_at=?,updated_at=? WHERE id=?",
                    (request_status, f"stale-{index}", f"stale-{index}", "stale-updated", request_id),
                )
            expected.append((
                request_id, task_status, task_error, completed_at, task_updated_at,
            ))

        unrelated_completed, _ = download_requests_repo.create_download_request(
            "startup-projection-unrelated-completed", "magnet",
        )
        download_requests_repo.update_download_request(
            unrelated_completed,
            status="completed",
            local_import_status="completed",
            local_import_error="keep completed",
            local_import_completed_at="keep completed at",
            completed_at="keep root completed at",
        )
        unrelated_cancelled, _ = download_requests_repo.create_download_request(
            "startup-projection-unrelated-cancelled", "magnet",
        )
        download_requests_repo.update_download_request(
            unrelated_cancelled,
            status="cancelled",
            local_import_status="failed",
            local_import_target="local-media-task:999999999",
            local_import_error="keep cancelled",
            local_import_completed_at="keep cancelled at",
            completed_at="keep root cancelled at",
        )
        untouched_columns = (
            "status", "local_import_status", "local_import_target",
            "local_import_error", "local_import_completed_at", "completed_at", "updated_at",
        )
        untouched_before = {
            request_id: _request_snapshot(request_id, untouched_columns)
            for request_id in (unrelated_completed, unrelated_cancelled)
        }

        db.init_db()
        for request_id, task_status, task_error, completed_at, task_updated_at in expected:
            request = download_requests_repo.get_download_request(request_id)
            assert request is not None
            self.assertEqual(
                (request["local_import_status"], request["local_import_error"],
                 request["local_import_completed_at"], request["updated_at"]),
                (task_status, task_error, completed_at, task_updated_at),
            )
        untouched_after_first = {
            request_id: _request_snapshot(request_id, untouched_columns)
            for request_id in (unrelated_completed, unrelated_cancelled)
        }
        self.assertEqual(untouched_after_first, untouched_before)

        db.init_db()
        for request_id, task_status, task_error, completed_at, task_updated_at in expected:
            request = download_requests_repo.get_download_request(request_id)
            assert request is not None
            self.assertEqual(
                (request["local_import_status"], request["local_import_error"],
                 request["local_import_completed_at"], request["updated_at"]),
                (task_status, task_error, completed_at, task_updated_at),
            )
        untouched_after_second = {
            request_id: _request_snapshot(request_id, untouched_columns)
            for request_id in (unrelated_completed, unrelated_cancelled)
        }
        self.assertEqual(untouched_after_second, untouched_before)

    def test_new_qb_request_transfers_hash_to_active_path_task(self):
        source_id = local_media_repo.create_local_media_source(
            name="terminal-hash-transfer", qb_profile="configured:qb",
            qb_path_prefix="/downloads", local_root="/tmp/hash-transfer", owner="admin",
        )
        first_request, _ = download_requests_repo.create_download_request("hash-transfer-first", "magnet")
        terminal_id, _ = local_media_repo.create_and_link_qb_local_media_task(
            first_request,
            source_id,
            "HASH-TRANSFER",
            "/tmp/hash-transfer/Movie.mkv",
            owner="admin",
        )
        local_media_repo.update_local_media_task(
            terminal_id, owner="admin", status="completed", completed_at=db.now()
        )
        active_id = local_media_repo.create_local_media_task(
            source_id,
            "",
            "/tmp/hash-transfer/./Movie.mkv",
            owner="admin",
            trigger="manual",
            operation_token="preserved-manual-token",
        )
        local_media_repo.update_local_media_task(
            active_id,
            owner="admin",
            status="requires_manual",
            tmdb_id="99",
            title="人工任务",
        )
        before = local_media_repo.get_local_media_task(active_id, owner="admin")
        second_request, _ = download_requests_repo.create_download_request("hash-transfer-second", "magnet")

        linked_id, restarted = local_media_repo.create_and_link_qb_local_media_task(
            second_request,
            source_id,
            "hash-transfer",
            "/tmp/hash-transfer/Movie.mkv",
            owner="admin",
        )

        self.assertEqual(linked_id, active_id)
        self.assertFalse(restarted)
        active = local_media_repo.get_local_media_task(active_id, owner="admin")
        terminal = local_media_repo.get_local_media_task(terminal_id, owner="admin")
        assert active is not None
        self.assertEqual(active.qb_hash, "hash-transfer")
        assert before is not None
        self.assertEqual(
            (active.status, active.operation_token, active.tmdb_id, active.title, active.version),
            (before.status, before.operation_token, before.tmdb_id, before.title, before.version),
        )
        assert terminal is not None
        self.assertEqual(terminal.qb_hash, "")
        get_download_request_result_2 = download_requests_repo.get_download_request(second_request)
        assert get_download_request_result_2 is not None
        self.assertEqual(
            get_download_request_result_2["local_import_target"],
            f"local-media-task:{active_id}",
        )

    def test_concurrent_manual_and_qb_admission_create_one_path_task(self):
        source_id = local_media_repo.create_local_media_source(
            name="concurrent-manual-qb", qb_profile="configured:qb",
            qb_path_prefix="/downloads", local_root="/tmp/concurrent-manual-qb", owner="admin",
        )
        request_id, _ = download_requests_repo.create_download_request("concurrent-manual-qb", "magnet")
        barrier = threading.Barrier(3)
        task_ids: list[int] = []
        errors: list[BaseException] = []
        result_lock = threading.Lock()

        def admit_manual() -> None:
            barrier.wait()
            try:
                task_id = local_media_repo.create_local_media_task(
                    source_id,
                    "",
                    "/tmp/concurrent-manual-qb/a/../Movie.mkv",
                    owner="admin",
                    trigger="manual",
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                with result_lock:
                    errors.append(exc)
            else:
                with result_lock:
                    task_ids.append(task_id)

        def admit_qb() -> None:
            barrier.wait()
            try:
                task_id, _ = local_media_repo.create_and_link_qb_local_media_task(
                    request_id,
                    source_id,
                    "HASH-CONCURRENT-PATH",
                    "/tmp/concurrent-manual-qb/./Movie.mkv",
                    owner="admin",
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                with result_lock:
                    errors.append(exc)
            else:
                with result_lock:
                    task_ids.append(task_id)

        threads = [threading.Thread(target=admit_manual), threading.Thread(target=admit_qb)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(task_ids), 2)
        self.assertEqual(len(set(task_ids)), 1)
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT id,qb_hash,content_path FROM local_media_tasks "
                "WHERE owner=? AND source_id=? AND status NOT IN ('completed','failed')",
                ("admin", source_id),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0]["qb_hash"]), "hash-concurrent-path")
        self.assertEqual(str(rows[0]["content_path"]), "/tmp/concurrent-manual-qb/Movie.mkv")
        get_download_request_result_3 = download_requests_repo.get_download_request(request_id)
        assert get_download_request_result_3 is not None
        self.assertEqual(
            get_download_request_result_3["local_import_target"],
            f"local-media-task:{task_ids[0]}",
        )

    def test_init_db_purges_deprecated_source_fields_but_keeps_schema_columns(self):
        source_id = local_media_repo.create_local_media_source(
            name="legacy-smb", qb_profile="", qb_path_prefix="",
            local_root="/media/downloads", owner="admin",
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE local_media_sources SET smb_user=?,smb_pass=?,stable_seconds=300,"
                "scan_enabled=1,scan_interval_minutes=30 WHERE id=?",
                ("nas-user", "nas-secret", source_id),
            )
        db.init_db()
        source = local_media_repo.get_local_media_source(source_id, owner="admin")
        assert source is not None
        self.assertEqual((source.smb_user, source.smb_pass), ("", ""))
        self.assertEqual(
            (source.stable_seconds, source.scan_enabled, source.scan_interval_minutes),
            (0, False, 10),
        )
        self.assertNotIn(
            "scan_enabled",
            local_media_repo.get_local_media_diagnostic_summary(owner="admin")["sources"],
        )
        with db.get_conn() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(local_media_sources)")}
        self.assertTrue({
            "smb_user", "smb_pass", "stable_seconds", "scan_enabled", "scan_interval_minutes",
        }.issubset(columns))

    def test_qb_task_link_is_atomic_and_new_request_restarts_terminal_attempt(self):
        source_id = local_media_repo.create_local_media_source(
            name="qb-atomic", qb_profile="configured:qb", qb_path_prefix="/downloads",
            local_root="/mnt/downloads", owner="admin",
        )
        first_request, _ = download_requests_repo.create_download_request("qb-atomic-first", "magnet")
        task_id, restarted = local_media_repo.create_and_link_qb_local_media_task(
            first_request, source_id, "HASH-ATOMIC", "/mnt/downloads/Movie.mkv", owner="admin",
        )
        self.assertFalse(restarted)
        first_download = download_requests_repo.get_download_request(first_request)
        assert first_download is not None
        self.assertEqual(
            first_download["local_import_target"], f"local-media-task:{task_id}",
        )
        self.assertTrue(first_download["local_import_started_at"])

        first_attempt = local_media_repo.get_local_media_task(task_id, owner="admin")
        local_media_repo.add_local_media_task_item(
            task_id, "/mnt/downloads/Movie.mkv", "/mnt/library/Movie.mkv",
            role="video", owner="admin",
        )
        local_media_repo.update_local_media_task(
            task_id, owner="admin", status="completed", completed_at=db.now(),
        )

        second_request, _ = download_requests_repo.create_download_request("qb-atomic-second", "magnet")
        reused_id, restarted = local_media_repo.create_and_link_qb_local_media_task(
            second_request, source_id, "hash-atomic",
            "/mnt/downloads/readded/Movie.mkv", owner="admin",
        )
        self.assertEqual(reused_id, task_id)
        self.assertTrue(restarted)
        retried = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert retried is not None
        self.assertEqual(retried.status, "waiting_stable")
        self.assertEqual(retried.content_path, "/mnt/downloads/readded/Movie.mkv")
        assert first_attempt is not None
        self.assertNotEqual(retried.operation_token, first_attempt.operation_token)
        self.assertEqual(local_media_repo.list_local_media_task_items(task_id, owner="admin"), [])
        get_download_request_result_4 = download_requests_repo.get_download_request(second_request)
        assert get_download_request_result_4 is not None
        self.assertEqual(
            get_download_request_result_4["local_import_target"],
            f"local-media-task:{task_id}",
        )
        get_download_request_result_5 = download_requests_repo.get_download_request(first_request)
        assert get_download_request_result_5 is not None
        self.assertEqual(
            get_download_request_result_5["local_import_status"], "completed",
        )

        local_media_repo.update_local_media_task(
            task_id, owner="admin", status="completed", completed_at=db.now(),
        )
        same_id, restarted = local_media_repo.create_and_link_qb_local_media_task(
            second_request, source_id, "hash-atomic",
            "/mnt/downloads/readded/Movie.mkv", owner="admin",
        )
        self.assertEqual(same_id, task_id)
        self.assertFalse(restarted)
        get_local_media_task_result_4 = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert get_local_media_task_result_4 is not None
        self.assertEqual(get_local_media_task_result_4.status, "completed")

    def test_qb_hash_is_idempotent_and_owner_isolated(self):
        source_id = local_media_repo.create_local_media_source(
            name="source-idempotent", qb_profile="qb", qb_path_prefix="/downloads",
            local_root="/mnt/downloads", owner="admin",
        )
        first = local_media_repo.create_local_media_task(source_id, "HASH", "/mnt/downloads/A", owner="admin")
        second = local_media_repo.create_local_media_task(source_id, "hash", "/mnt/downloads/A", owner="admin")
        self.assertEqual(first, second)
        self.assertIsNone(local_media_repo.get_local_media_source(source_id, owner="other"))
        self.assertIsNone(local_media_repo.get_local_media_task(first, owner="other"))
        self.assertEqual(local_media_repo.list_local_media_tasks(owner="other"), [])

    def test_task_items_and_atomic_concurrent_claim(self):
        source_id = local_media_repo.create_local_media_source(
            name="source-items", qb_profile="", qb_path_prefix="",
            local_root="/tmp/downloads-items", owner="admin",
        )
        task_id = local_media_repo.create_local_media_task(
            source_id, "", "/tmp/downloads/A.mkv", owner="admin", trigger="manual"
        )
        item_id = local_media_repo.add_local_media_task_item(
            task_id, "/tmp/downloads/A.mkv", "/tmp/library/A.mkv",
            role="video", size=123, owner="admin",
        )
        self.assertGreater(item_id, 0)
        self.assertEqual(local_media_repo.list_local_media_task_items(task_id, owner="admin")[0]["role"], "video")

        results: list[bool] = []
        barrier = threading.Barrier(2)

        def claim() -> None:
            barrier.wait()
            results.append(local_media_repo.claim_local_media_task(task_id, expected="waiting_stable", owner="admin"))

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [False, True])

    def test_selected_log_clear_skips_busy_tasks_and_cascades_children(self):
        source_id = local_media_repo.create_local_media_source(
            name="source-clear", qb_profile="", qb_path_prefix="",
            local_root="/tmp/downloads-clear", owner="admin",
        )
        completed_id = local_media_repo.create_local_media_task(
            source_id, "", "/tmp/downloads-clear/Done.mkv", owner="admin", trigger="manual",
        )
        local_media_repo.add_local_media_task_item(
            completed_id, "/tmp/downloads-clear/Done.mkv", "/tmp/library/Done.mkv",
            role="video", owner="admin",
        )
        completed = local_media_repo.get_local_media_task(completed_id, owner="admin")
        assert completed is not None
        local_media_repo.add_local_media_operation_step(
            completed_id, completed.operation_token, 0, "move", owner="admin",
        )
        local_media_repo.update_local_media_task(completed_id, owner="admin", status="completed")
        busy_id = local_media_repo.create_local_media_task(
            source_id, "", "/tmp/downloads-clear/Busy.mkv", owner="admin", trigger="scan",
        )

        result = local_media_repo.delete_local_media_tasks([completed_id, busy_id, 999999], owner="admin")

        self.assertEqual(result, {"requested": 3, "deleted": 1, "skipped_busy": 1, "missing": 1})
        self.assertIsNone(local_media_repo.get_local_media_task(completed_id, owner="admin"))
        self.assertEqual(local_media_repo.list_local_media_task_items(completed_id, owner="admin"), [])
        self.assertEqual(local_media_repo.list_local_media_operation_steps(completed_id, owner="admin"), [])
        assert (local_media_repo.get_local_media_task(busy_id, owner="admin")) is not None

    def test_invalid_status_category_and_cross_owner_target_are_rejected(self):
        source_id = local_media_repo.create_local_media_source(
            name="source-validation", qb_profile="", qb_path_prefix="",
            local_root="/tmp/downloads-validation", owner="admin",
        )
        with self.assertRaisesRegex(ValueError, "分类"):
            local_media_repo.upsert_local_library_target(source_id, "music", "/tmp/music", owner="admin")
        with self.assertRaisesRegex(LookupError, "来源"):
            local_media_repo.upsert_local_library_target(source_id, "movie", "/tmp/movies", owner="other")
        task_id = local_media_repo.create_local_media_task(source_id, "x", "/tmp/downloads/x", owner="admin")
        with self.assertRaisesRegex(ValueError, "状态"):
            local_media_repo.update_local_media_task(task_id, owner="admin", status="running")

    def test_source_bundle_rolls_back_fields_and_targets_on_database_failure(self):
        before_ids = {item.id for item in local_media_repo.list_local_media_sources(owner="admin")}
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER fail_tv_target BEFORE INSERT ON local_library_targets "
                "WHEN NEW.category='tv' BEGIN SELECT RAISE(ABORT, 'simulated target failure'); END"
            )
        try:
            with self.assertRaisesRegex(Exception, "simulated target failure"):
                local_media_repo.save_local_media_source_bundle(
                    name="atomic", qb_profile="configured:qb", qb_path_prefix="/downloads",
                    local_root="/mnt/downloads", enabled=True, stable_seconds=0, scan_enabled=False,
                    scan_interval_minutes=10, media_type="auto", mode="move", owner="admin",
                    targets=[
                        {"category": "movie", "path": "/media/movies"},
                        {"category": "tv", "path": "/media/tv"},
                    ],
                )
        finally:
            with db.get_conn() as conn:
                conn.execute("DROP TRIGGER IF EXISTS fail_tv_target")
        sources = local_media_repo.list_local_media_sources(owner="admin")
        self.assertEqual({item.id for item in sources}, before_ids)
        self.assertNotIn("atomic", {item.name for item in sources})

    def test_active_source_allows_name_only_change_but_freezes_runtime_configuration(self):
        source_id = local_media_repo.save_local_media_source_bundle(
            name="active-source", qb_profile="configured:qb", qb_path_prefix="/downloads",
            local_root="/tmp/active-source", enabled=True, media_type="auto", mode="move",
            owner="admin",
            targets=[{"category": "movie", "path": "/tmp/library/movies"}],
        )
        task_id = local_media_repo.create_local_media_task(
            source_id, "", "/tmp/active-source/Movie.mkv", owner="admin",
        )

        self.assertEqual(
            local_media_repo.save_local_media_source_bundle(
                source_id=source_id, name="renamed-source",
                qb_profile="configured:qb", qb_path_prefix="/downloads",
                local_root="/tmp/active-source", enabled=True,
                media_type="auto", mode="move", owner="admin",
                targets=[{"category": "movie", "path": "/tmp/library/movies"}],
            ),
            source_id,
        )
        get_local_media_source_result_2 = local_media_repo.get_local_media_source(source_id, owner="admin")
        assert get_local_media_source_result_2 is not None
        self.assertEqual(
            get_local_media_source_result_2.name,
            "renamed-source",
        )

        with self.assertRaisesRegex(ValueError, "不能修改运行配置"):
            local_media_repo.save_local_media_source_bundle(
                source_id=source_id, name="renamed-source",
                qb_profile="configured:qb", qb_path_prefix="/downloads",
                local_root="/tmp/active-source-new", enabled=True,
                media_type="auto", mode="move", owner="admin",
                targets=[{"category": "movie", "path": "/tmp/library/movies"}],
            )
        with self.assertRaisesRegex(ValueError, "不能修改运行配置"):
            local_media_repo.save_local_media_source_bundle(
                source_id=source_id, name="renamed-source",
                qb_profile="configured:qb", qb_path_prefix="/downloads",
                local_root="/tmp/active-source", enabled=True,
                media_type="auto", mode="move", owner="admin",
                targets=[{"category": "movie", "path": "/tmp/library/movies-new"}],
            )
        with self.assertRaisesRegex(ValueError, "不能修改运行配置"):
            local_media_repo.update_local_media_source(source_id, owner="admin", media_type="movie")

        local_media_repo.update_local_media_task(task_id, owner="admin", status="completed")
        self.assertTrue(
            local_media_repo.update_local_media_source(source_id, owner="admin", media_type="movie")
        )
        self.assertEqual(
            local_media_repo.save_local_media_source_bundle(
                source_id=source_id, name="renamed-source",
                qb_profile="configured:qb", qb_path_prefix="/downloads",
                local_root="/tmp/active-source", enabled=True,
                media_type="movie", mode="move", owner="admin",
                targets=[{"category": "movie", "path": "/tmp/library/movies-new"}],
            ),
            source_id,
        )

    def test_manual_task_persists_position_overrides_and_retry_keeps_selection(self):
        source_id = local_media_repo.create_local_media_source(
            name="manual-position", qb_profile="", qb_path_prefix="",
            local_root="/tmp/manual-position", media_type="tv", owner="admin",
        )
        task_id = local_media_repo.prepare_manual_local_media_task(
            source_id, "/tmp/manual-position/Show.S01E07.mkv", snapshot_digest="preview:fixture", owner="admin",
            tmdb_id="42", media_type="tv", season_override=2, episode_override=7,
            numbering_mode="season_continuous",
        )
        task = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert task is not None
        self.assertEqual((task.season_override, task.episode_override), (2, 7))
        self.assertEqual(task.numbering_mode, "season_continuous")
        local_media_repo.add_local_media_task_item(
            task_id, "/tmp/manual-position/Show.S01E07.mkv",
            "/tmp/library/Show.S01E07.mkv", role="video", owner="admin",
        )
        local_media_repo.update_local_media_task(
            task_id, owner="admin", status="requires_manual", title="旧标题", year="2025",
            recognition_summary='{"schema_version":1,"media":[]}',
        )
        get_local_media_task_result_5 = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert get_local_media_task_result_5 is not None
        previous_token = get_local_media_task_result_5.operation_token
        self.assertTrue(local_media_repo.reset_local_media_task(task_id, owner="admin"))
        retried = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert retried is not None
        self.assertNotEqual(retried.operation_token, previous_token)
        self.assertEqual(local_media_repo.list_local_media_task_items(task_id, owner="admin"), [])
        self.assertEqual(retried.recognition_summary, "")
        self.assertEqual((retried.title, retried.year), ("", ""))
        self.assertEqual(retried.tmdb_id, "42")
        self.assertEqual(retried.media_type, "tv")
        self.assertEqual((retried.season_override, retried.episode_override), (2, 7))
        self.assertEqual(retried.numbering_mode, "season_continuous")

    def test_interrupted_write_retry_requires_explicit_confirmation(self):
        source_id = local_media_repo.create_local_media_source(
            name="interrupted-write", qb_profile="", qb_path_prefix="",
            local_root="/tmp/interrupted-write", owner="admin",
        )
        task_id = local_media_repo.create_local_media_task(
            source_id, "", "/tmp/interrupted-write/Movie.mkv", owner="admin",
        )
        local_media_repo.add_local_media_task_item(
            task_id, "/tmp/interrupted-write/Movie.mkv",
            "/tmp/library/Movie.mkv", role="video", owner="admin",
        )
        local_media_repo.update_local_media_task(task_id, owner="admin", status="moving")

        db.init_db()

        recovered = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert recovered is not None
        self.assertEqual(recovered.status, "requires_manual")
        self.assertTrue(db.is_interrupted_local_media_write_error(recovered.error))
        self.assertFalse(recovered.completed_at)
        self.assertFalse(local_media_repo.reset_local_media_task(task_id, owner="admin"))
        self.assertEqual(len(local_media_repo.list_local_media_task_items(task_id, owner="admin")), 1)

        self.assertTrue(local_media_repo.reset_local_media_task(
            task_id, owner="admin", confirm_interrupted_write=True,
        ))
        get_local_media_task_result_6 = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert get_local_media_task_result_6 is not None
        self.assertEqual(
            get_local_media_task_result_6.status,
            "waiting_stable",
        )
        self.assertEqual(local_media_repo.list_local_media_task_items(task_id, owner="admin"), [])

    def test_init_db_defers_local_recovery_until_pipeline_writer_releases(self):
        source_id = local_media_repo.create_local_media_source(
            name="live-writer", qb_profile="", qb_path_prefix="",
            local_root="/tmp/live-writer", owner="admin",
        )
        task_id = local_media_repo.create_local_media_task(
            source_id, "a" * 40, "/tmp/live-writer/Movie.mkv", owner="admin",
        )
        task = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert task is not None
        step_id = local_media_repo.add_local_media_operation_step(
            task_id, task.operation_token, 0, "move", owner="admin",
        )
        local_media_repo.update_local_media_task(task_id, owner="admin", status="moving")
        local_media_repo.update_local_media_operation_step(step_id, "running")
        live_writer = CrossProcessLock(
            "local-media-pipeline-write", directory=db.resolve_db_path().parent,
        )
        self.assertTrue(live_writer.acquire(blocking=False))
        try:
            db.init_db()
            active = local_media_repo.get_local_media_task(task_id, owner="admin")
            active_steps = local_media_repo.list_local_media_operation_steps(task_id, owner="admin")
            assert active is not None
            self.assertEqual(active.status, "moving")
            self.assertEqual(active_steps[0]["status"], "running")
        finally:
            live_writer.release()

        # 下一次启动检查能够拿到 writer，才把真正的 orphan 收束为人工核验。
        db.init_db()
        recovered = local_media_repo.get_local_media_task(task_id, owner="admin")
        recovered_steps = local_media_repo.list_local_media_operation_steps(task_id, owner="admin")
        assert recovered is not None
        self.assertEqual(recovered.status, "requires_manual")
        self.assertTrue(db.is_interrupted_local_media_write_error(recovered.error))
        self.assertEqual(recovered_steps[0]["status"], "failed")

    def test_init_db_keeps_all_postwrite_interruptions_for_manual_review(self):
        source_id = local_media_repo.create_local_media_source(
            name="postwrite", qb_profile="", qb_path_prefix="",
            local_root="/tmp/postwrite", owner="admin",
        )
        task_ids = []
        for status in ("moving", "verifying", "refreshing", "rolling_back"):
            task_id = local_media_repo.create_local_media_task(
                source_id, "", f"/tmp/postwrite/{status}.mkv", owner="admin",
            )
            local_media_repo.add_local_media_task_item(
                task_id, f"/tmp/postwrite/{status}.mkv",
                f"/tmp/library/{status}.mkv", role="video", owner="admin",
            )
            local_media_repo.update_local_media_task(task_id, owner="admin", status=status)
            task_ids.append(task_id)

        db.init_db()

        for task_id in task_ids:
            recovered = local_media_repo.get_local_media_task(task_id, owner="admin")
            assert recovered is not None
            self.assertEqual(recovered.status, "requires_manual")
            self.assertTrue(db.is_interrupted_local_media_write_error(recovered.error))
            self.assertEqual(
                len(local_media_repo.list_local_media_task_items(task_id, owner="admin")), 1,
            )

    def test_init_db_keeps_prewrite_interruptions_retryable(self):
        source_id = local_media_repo.create_local_media_source(
            name="prewrite", qb_profile="", qb_path_prefix="",
            local_root="/tmp/prewrite", owner="admin",
        )
        task_ids = []
        for status in ("recognizing", "planned"):
            task_id = local_media_repo.create_local_media_task(
                source_id, "", f"/tmp/prewrite/{status}.mkv", owner="admin",
            )
            local_media_repo.update_local_media_task(task_id, owner="admin", status=status)
            task_ids.append(task_id)

        db.init_db()

        for task_id in task_ids:
            recovered = local_media_repo.get_local_media_task(task_id, owner="admin")
            assert recovered is not None
            self.assertEqual(recovered.status, "failed")
            self.assertIn("可重试", recovered.error)
            self.assertTrue(local_media_repo.reset_local_media_task(task_id, owner="admin"))

    def test_task_recognition_summary_round_trip_is_versioned(self):
        source_id = local_media_repo.create_local_media_source(
            name="recognition-summary", qb_profile="", qb_path_prefix="",
            local_root="/tmp/recognition-summary", owner="admin",
        )
        task_id = local_media_repo.create_local_media_task(
            source_id, "", "/tmp/recognition-summary/Show.S02E08.mkv",
            owner="admin", trigger="scan",
        )
        before = local_media_repo.get_local_media_task(task_id, owner="admin")
        summary = '{"schema_version":1,"media":[]}'
        self.assertTrue(local_media_repo.update_local_media_task(
            task_id, owner="admin", recognition_summary=summary,
        ))
        after = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert after is not None
        self.assertEqual(after.recognition_summary, summary)
        assert before is not None
        self.assertEqual(after.version, before.version + 1)
        self.assertEqual(
            local_media_repo.list_local_media_tasks(owner="admin")[0].recognition_summary, summary,
        )

    def test_local_confirmation_claim_is_versioned_and_atomic(self):
        source_id = local_media_repo.create_local_media_source(
            name="telegram-confirm", qb_profile="", qb_path_prefix="",
            local_root="/tmp/telegram-confirm", media_type="movie", owner="admin",
        )
        task_id = local_media_repo.create_local_media_task(
            source_id, "", "/tmp/telegram-confirm/Movie.mkv",
            owner="admin", trigger="scan",
        )
        local_media_repo.update_local_media_task(
            task_id, owner="admin", status="requires_manual", snapshot_digest="digest-1"
        )
        task = local_media_repo.get_local_media_task(task_id, owner="admin")

        assert task is not None
        self.assertFalse(local_media_repo.claim_local_media_confirmation_task(
            task_id,
            confirmed_snapshot_digest="confirmed-source-digest", owner="admin",
            expected_version=task.version + 1,
            expected_snapshot_digest="digest-1",
            tmdb_id="101",
            media_type="movie",
            rules_snapshot="{}",
        ))
        self.assertTrue(local_media_repo.claim_local_media_confirmation_task(
            task_id,
            confirmed_snapshot_digest="confirmed-source-digest", owner="admin",
            expected_version=task.version,
            expected_snapshot_digest="digest-1",
            tmdb_id="101",
            media_type="movie",
            rules_snapshot="{}",
            title="电影甲",
            year="2026",
        ))
        claimed = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert claimed is not None
        self.assertEqual(claimed.status, "recognizing")
        self.assertEqual((claimed.tmdb_id, claimed.media_type), ("101", "movie"))
        self.assertEqual((claimed.title, claimed.year), ("电影甲", "2026"))

    def test_prepare_manual_retry_clears_previous_attempt_items(self):
        source_id = local_media_repo.create_local_media_source(
            name="manual-retry", qb_profile="", qb_path_prefix="",
            local_root="/tmp/manual-retry", owner="admin",
        )
        content_path = "/tmp/manual-retry/Movie.mkv"
        task_id = local_media_repo.prepare_manual_local_media_task(
            source_id, content_path, snapshot_digest="preview:fixture", owner="admin", tmdb_id="1", media_type="movie",
        )
        original = local_media_repo.get_local_media_task(task_id, owner="admin")
        local_media_repo.add_local_media_task_item(
            task_id, content_path, "/tmp/library/Movie.mkv", role="video", owner="admin",
        )
        local_media_repo.update_local_media_task(
            task_id, owner="admin", status="failed", error="move failed",
            title="旧电影", year="2025",
        )

        reused_id = local_media_repo.prepare_manual_local_media_task(
            source_id, content_path, snapshot_digest="preview:fixture", owner="admin", tmdb_id="2", media_type="movie",
        )
        retried = local_media_repo.get_local_media_task(reused_id, owner="admin")
        self.assertEqual(reused_id, task_id)
        assert retried is not None
        self.assertEqual(retried.status, "waiting_stable")
        self.assertEqual(retried.tmdb_id, "2")
        self.assertEqual((retried.title, retried.year), ("", ""))
        assert original is not None
        self.assertNotEqual(retried.operation_token, original.operation_token)
        self.assertEqual(local_media_repo.list_local_media_task_items(task_id, owner="admin"), [])

    def test_manual_task_prepare_never_resets_an_active_task(self):
        source_id = local_media_repo.create_local_media_source(
            name="manual-race", qb_profile="", qb_path_prefix="", local_root="/tmp/manual", owner="admin"
        )
        task_id = local_media_repo.prepare_manual_local_media_task(
            source_id, "/tmp/manual/Movie.mkv", snapshot_digest="preview:fixture", owner="admin", tmdb_id="1", media_type="movie"
        )
        self.assertTrue(local_media_repo.claim_local_media_task(task_id, owner="admin"))
        with self.assertRaisesRegex(ValueError, "正在处理中"):
            local_media_repo.prepare_manual_local_media_task(
                source_id, "/tmp/manual/Movie.mkv", snapshot_digest="preview:fixture", owner="admin", tmdb_id="2", media_type="movie"
            )
        task = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert task is not None
        self.assertEqual(task.status, "recognizing")
        self.assertEqual(task.tmdb_id, "1")


if __name__ == "__main__":
    unittest.main()
