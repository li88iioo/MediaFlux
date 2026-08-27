"""本地媒体调度器去重、稳定等待和生命周期测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import database as db
from app.clients.qbittorrent import TorrentTask
from app.modules.local_media_scheduler import (
    LocalMediaProbeRetryable,
    LocalMediaScheduler,
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


    def test_non_media_changes_do_not_reset_stability_wait(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source = root / "source"
            source.mkdir()
            movie = source / "Movie.mkv"
            notes = source / "readme.txt"
            movie.write_bytes(b"movie")
            notes.write_text("first")
            source_id = db.create_local_media_source(
                name="stable", qb_profile="", qb_path_prefix="", local_root=str(source),
                stable_seconds=300, owner="admin",
            )
            task_id = db.create_local_media_task(
                source_id, "", str(source), owner="admin", trigger="manual",
            )
            service = FakeService()
            scheduler = LocalMediaScheduler(service=service)

            self.assertEqual(scheduler.run_once(), 0)
            first_digest = db.get_local_media_task(task_id, owner="admin").snapshot_digest
            notes.write_text("changed non-media content")
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE local_media_tasks SET stable_since='2000-01-01 00:00:00' WHERE id=?",
                    (task_id,),
                )

            self.assertEqual(scheduler.run_once(), 1)
            completed = db.get_local_media_task(task_id, owner="admin")
            self.assertEqual(completed.snapshot_digest, first_digest)
            self.assertEqual(completed.status, "completed")

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
