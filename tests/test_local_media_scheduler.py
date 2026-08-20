"""本地媒体调度器去重、稳定等待和生命周期测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import database as db
from app.clients.qbittorrent import TorrentTask
from app.modules.local_media_scheduler import LocalMediaScheduler
from app.modules.local_storage import LocalFilesystemAdapter
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
                local_root=str(fast_root), stable_seconds=0, owner="admin",
            )
            service = FakeService(); qb = object()
            scheduler = LocalMediaScheduler(service=service, qb_factory=lambda: qb)
            first = scheduler.enqueue_completed_torrent(self.torrent("/downloads/1/Movie.mkv"))
            second = scheduler.enqueue_completed_torrent(self.torrent("/downloads/1/Movie.mkv"))
            self.assertEqual(first, second)
            self.assertEqual(db.get_local_media_task(first, owner="admin").source_id, fast_id)
            self.assertEqual(scheduler.run_once(), 1)
            self.assertEqual(service.calls, [("admin", first, qb)])

    def test_changed_snapshot_resets_stability_wait(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source = root / "source"; source.mkdir(); movie = source / "Movie.mkv"
            movie.write_bytes(b"one")
            source_id = db.create_local_media_source(
                name="stable", qb_profile="", qb_path_prefix="", local_root=str(source),
                stable_seconds=300, owner="admin",
            )
            task_id = db.create_local_media_task(source_id, "", str(movie), owner="admin", trigger="manual")
            scheduler = LocalMediaScheduler(service=FakeService())
            self.assertEqual(scheduler.run_once(), 0)
            first = db.get_local_media_task(task_id, owner="admin")
            movie.write_bytes(b"changed")
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE local_media_tasks SET stable_since='2000-01-01 00:00:00' WHERE id=?",
                    (task_id,),
                )
            self.assertEqual(scheduler.run_once(), 0)
            second = db.get_local_media_task(task_id, owner="admin")
            self.assertNotEqual(first.snapshot_digest, second.snapshot_digest)
            self.assertEqual(second.status, "waiting_stable")


    def test_stability_wait_skips_repeated_full_scan_before_deadline(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source = root / "source"; source.mkdir()
            movie = source / "Movie.mkv"; movie.write_bytes(b"movie")
            source_id = db.create_local_media_source(
                name="stable", qb_profile="", qb_path_prefix="", local_root=str(source),
                stable_seconds=300, owner="admin",
            )
            db.create_local_media_task(source_id, "", str(movie), owner="admin", trigger="manual")
            scheduler = LocalMediaScheduler(service=FakeService())
            original_scan = LocalFilesystemAdapter.scan
            scan_calls = []
            def counted_scan(adapter, content_path):
                scan_calls.append(str(content_path))
                return original_scan(adapter, content_path)
            with patch("app.modules.local_media_scheduler.LocalFilesystemAdapter.scan", new=counted_scan):
                self.assertEqual(scheduler.run_once(), 0)
                self.assertEqual(scheduler.run_once(), 0)
            self.assertEqual(len(scan_calls), 1)

    def test_preview_only_source_is_manual_only_and_not_auto_enqueued(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source = root / "source"; source.mkdir()
            (source / "Movie.mkv").write_bytes(b"movie")
            db.create_local_media_source(
                name="preview", qb_profile="", qb_path_prefix="", local_root=str(source),
                scan_enabled=True, stable_seconds=0, mode="preview_only", owner="admin",
            )
            scheduler = LocalMediaScheduler(service=FakeService(), clock=lambda: 100.0)
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
            self.assertEqual(recovered.status, "failed")
            self.assertIn("人工核验", recovered.error)
            self.assertEqual(steps[0]["status"], "failed")
            self.assertIn("人工核验", steps[0]["error"])

    def test_scan_interval_and_trigger_switches_are_independent(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "source"; source_root.mkdir()
            movie = source_root / "Movie.mkv"; movie.write_bytes(b"movie")
            source_id = db.create_local_media_source(
                name="scan-only", qb_profile="", qb_path_prefix="", local_root=str(source_root),
                enabled=False, scan_enabled=True, scan_interval_minutes=10, stable_seconds=0, owner="admin",
            )
            now = [100.0]
            service = FakeService()
            scheduler = LocalMediaScheduler(service=service, clock=lambda: now[0])
            self.assertEqual(scheduler.run_once(), 1)
            self.assertEqual(len(service.calls), 1)
            self.assertEqual(scheduler._last_scan_at[source_id], 100.0)

            now[0] = 120.0
            scheduler.run_once()
            self.assertEqual(scheduler._last_scan_at[source_id], 100.0)

            now[0] = 701.0
            scheduler.run_once()
            self.assertEqual(scheduler._last_scan_at[source_id], 701.0)


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
                stable_seconds=0, owner="admin",
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

    def test_start_stop_is_reentrant_and_leaves_no_thread(self):
        scheduler = LocalMediaScheduler(service=FakeService(), interval=0.2)
        self.assertEqual(scheduler.status(), {"running": False, "interval_seconds": 0.2})
        scheduler.start(); scheduler.start(); scheduler.stop(); scheduler.stop()
        self.assertIsNone(scheduler._thread)
        self.assertEqual(scheduler.status(), {"running": False, "interval_seconds": 0.2})

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
