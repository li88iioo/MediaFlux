"""扫描通知的精确成员、结果快照与工作区隔离。"""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from app import database as db
from app.repositories import local_media as local_media_repo
from app.modules.local_media_scan_runs import (
    UNRECORDED_LOCAL_SCAN,
    finish_local_media_scan_report,
    record_local_media_scan,
    resolve_local_media_scan,
)
from app.modules.local_media_scheduler import LocalMediaScheduler
from tests.support import IsolatedDatabaseTestCase


class LocalMediaScanRunTests(IsolatedDatabaseTestCase):
    def test_scan_ref_preserves_members_including_reused_old_task(self):
        ref = record_local_media_scan({"task_ids": [17, 2, 17], "candidate_count": 2})
        run = resolve_local_media_scan(ref)
        self.assertEqual(run["task_ids"], [17, 2])
        self.assertEqual(run["summary"]["candidate_count"], 2)
        self.assertEqual(resolve_local_media_scan()["scan_ref"], ref)

    def test_latest_scan_is_owner_scoped_and_never_falls_back_to_history(self):
        ref = record_local_media_scan({"task_ids": [17]}, owner="alpha")
        record_local_media_scan({"task_ids": [30]}, owner="beta")
        self.assertEqual(resolve_local_media_scan(owner="alpha")["scan_ref"], ref)
        with self.assertRaises(LookupError):
            resolve_local_media_scan(ref, owner="beta")
        with self.assertRaises(LookupError):
            resolve_local_media_scan(owner="old-version")

    def test_other_run_kind_is_not_a_local_scan(self):
        run_id = db.add_task_run("rss_refresh", "manual", status="success")
        with self.assertRaises(LookupError):
            resolve_local_media_scan(f"LM{run_id}")
        with self.assertRaises(ValueError):
            resolve_local_media_scan("rss-123")

    def test_empty_latest_scan_cannot_reuse_previous_nonempty_scan(self):
        record_local_media_scan({"task_ids": [17]})
        empty = record_local_media_scan({"task_ids": [], "candidate_count": 0})
        run = resolve_local_media_scan()
        self.assertEqual(run["scan_ref"], empty)
        self.assertEqual(run["task_ids"], [])

    def test_notified_results_are_frozen_and_cannot_include_unrelated_tasks(self):
        ref = record_local_media_scan({"task_ids": [17, 2]})
        summary = {
            "completed": 1,
            "requires_manual": 1,
            "moved_items": 0,
            "skipped_items": 1,
            "task_outcomes": [
                {"task_id": 17, "status": "completed", "file_outcome": "conflict_skipped"},
                {"task_id": 2, "status": "requires_manual", "file_outcome": "pending"},
                {"task_id": 99, "status": "completed"},
            ],
        }
        finish_local_media_scan_report(ref, summary)
        saved = resolve_local_media_scan(ref)["summary"]
        self.assertEqual(len(saved["task_outcomes"]), 2)
        self.assertEqual(saved["task_outcomes"][0]["file_outcome"], "conflict_skipped")
        self.assertTrue(saved["reported_at"])
        finish_local_media_scan_report(ref, {"task_outcomes": [], "moved_items": 2})
        self.assertEqual(resolve_local_media_scan(ref)["summary"], saved)

    def test_scheduler_registers_members_before_notifying_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            download = root / "downloads"
            library = root / "library"
            download.mkdir()
            library.mkdir()
            (download / "Demo.S01E01.mkv").write_bytes(b"test")
            source_id = local_media_repo.create_local_media_source(
                name="Downloads", qb_profile="", qb_path_prefix="",
                local_root=str(download), stable_seconds=0, owner="admin"
            )
            local_media_repo.upsert_local_library_target(source_id, "default", str(library), owner="admin")
            scheduler = LocalMediaScheduler(service=Mock())
            seen = []
            scheduler.reload = lambda: seen.append(resolve_local_media_scan())
            result = scheduler.enqueue_manual_scan_candidates(silent=True)
            self.assertTrue(result["scan_ref"].startswith("LM"))
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0]["task_ids"], result["task_ids"])
            with patch(
                "app.modules.local_media_scheduler.record_local_media_scan",
                side_effect=OSError("receipt unavailable"),
            ):
                failed_receipt = scheduler.enqueue_manual_scan_candidates(silent=True)
            self.assertEqual(len(seen), 2)  # 保存附属回执失败不阻止 Worker 唤醒。
            self.assertEqual(failed_receipt["queued_count"], 1)
            self.assertFalse(failed_receipt["scan_recorded"])
            self.assertEqual(failed_receipt["scan_ref"], UNRECORDED_LOCAL_SCAN)
            with self.assertRaises(LookupError):
                resolve_local_media_scan(failed_receipt["scan_ref"])
            self.assertEqual(resolve_local_media_scan()["scan_ref"], result["scan_ref"])

    def test_agent_does_not_substitute_previous_scan_after_receipt_write_failure(self):
        from app.modules.local_media_tasks import local_tasks
        record_local_media_scan({"task_ids": [17]})
        result = local_tasks(scan_ref=UNRECORDED_LOCAL_SCAN)
        self.assertEqual(result["status"], "not_recorded")
        self.assertFalse(result["scan_recorded"])
        self.assertEqual(result["items"], [])
