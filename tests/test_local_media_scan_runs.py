"""扫描通知的精确成员、结果快照与工作区隔离。"""

import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, local
from unittest.mock import Mock, patch

from app import database as db
from app.repositories import local_media as local_media_repo
from app.modules import local_media_scan_runs
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
            finish_local_media_scan_report(ref, {}, owner="beta")
        self.assertNotIn("reported_at", resolve_local_media_scan(ref, owner="alpha")["summary"])
        with self.assertRaises(LookupError):
            resolve_local_media_scan(owner="old-version")

    def test_other_run_kind_is_not_a_local_scan(self):
        run_id = db.add_task_run("rss_refresh", "manual", status="success")
        with self.assertRaises(LookupError):
            resolve_local_media_scan(f"LM{run_id}")
        with self.assertRaises(ValueError):
            resolve_local_media_scan("rss-123")
        with self.assertRaises(ValueError):
            finish_local_media_scan_report("rss-123", {})
        with self.assertRaisesRegex(LookupError, "本次扫描回执保存失败"):
            finish_local_media_scan_report(UNRECORDED_LOCAL_SCAN, {})

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

    def test_concurrent_report_finalization_keeps_first_committed_outcome(self):
        ref = record_local_media_scan({"task_ids": [17]})
        both_read = Barrier(2, timeout=5)
        first_committed = Event()
        call = local()
        decode = local_media_scan_runs._payload

        def read_before_write(row, *, owner):
            result = decode(row, owner=owner)
            assert result is not None
            self.assertNotIn("reported_at", result["summary"])
            both_read.wait()
            if call.moved == 2:
                self.assertTrue(first_committed.wait(timeout=5))
            return result

        def finish(moved):
            call.moved = moved
            finish_local_media_scan_report(ref, {"moved_items": moved})
            if moved == 1:
                first_committed.set()

        with (
            patch.object(local_media_scan_runs, "_payload", side_effect=read_before_write) as reads,
            ThreadPoolExecutor(max_workers=2) as workers,
        ):
            first = workers.submit(finish, 1)
            second = workers.submit(finish, 2)
            first.result(timeout=8)
            second.result(timeout=8)
            self.assertEqual(reads.call_count, 2)

        saved = resolve_local_media_scan(ref)
        self.assertEqual(saved["summary"]["moved_items"], 1)
        self.assertTrue(saved["summary"]["reported_at"])

    def test_report_for_deleted_scan_never_uses_newer_history(self):
        ref = record_local_media_scan({"task_ids": [17]})
        newer = record_local_media_scan({"task_ids": [42]})
        decode = local_media_scan_runs._payload

        def read_then_delete(row, *, owner):
            result = decode(row, owner=owner)
            with db.get_conn() as conn:
                conn.execute("DELETE FROM task_runs WHERE id=?", (row["id"],))
            return result

        with (
            patch.object(local_media_scan_runs, "_payload", side_effect=read_then_delete),
            self.assertRaisesRegex(LookupError, "本地扫描回执已移除"),
        ):
            finish_local_media_scan_report(ref, {"moved_items": 1})
        with self.assertRaisesRegex(LookupError, "没有找到本地扫描回执"):
            finish_local_media_scan_report(ref, {"moved_items": 1})
        self.assertNotIn("reported_at", resolve_local_media_scan(newer)["summary"])

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
            [reused_task_id] = result["task_ids"]
            local_media_repo.update_local_media_task(reused_task_id, status="requires_manual")
            (download / "Another.Movie.2026.mkv").write_bytes(b"test")

            mixed = scheduler.enqueue_manual_scan_candidates(silent=True)
            self.assertEqual(mixed["queued_count"], 1)
            self.assertEqual(mixed["reused_count"], 1)
            self.assertEqual((result["queued_count"], result["reused_count"]), (1, 0))
            self.assertEqual(len(mixed["task_ids"]), 2)
            self.assertIn(reused_task_id, mixed["task_ids"])
            [source] = mixed["sources"]
            self.assertEqual((source["queued"], source["reused"]), (1, 1))
            saved = seen[1]
            self.assertEqual(saved["task_ids"], mixed["task_ids"])
            self.assertEqual((saved["summary"]["queued_count"], saved["summary"]["reused_count"]), (1, 1))
            with patch(
                "app.modules.local_media_scheduler.record_local_media_scan",
                side_effect=OSError("receipt unavailable"),
            ):
                failed_receipt = scheduler.enqueue_manual_scan_candidates(silent=True)
            self.assertEqual(len(seen), 3)  # 保存附属回执失败不阻止 Worker 唤醒。
            self.assertEqual((failed_receipt["queued_count"], failed_receipt["reused_count"]), (0, 2))
            self.assertEqual(failed_receipt["task_ids"], mixed["task_ids"])
            self.assertFalse(failed_receipt["scan_recorded"])
            self.assertEqual(failed_receipt["scan_ref"], UNRECORDED_LOCAL_SCAN)
            with self.assertRaises(LookupError):
                resolve_local_media_scan(failed_receipt["scan_ref"])
            self.assertEqual(resolve_local_media_scan()["scan_ref"], mixed["scan_ref"])

    def test_agent_does_not_substitute_previous_scan_after_receipt_write_failure(self):
        from app.modules.local_media_tasks import local_tasks
        record_local_media_scan({"task_ids": [17]})
        result = local_tasks(scan_ref=UNRECORDED_LOCAL_SCAN)
        self.assertEqual(result["status"], "not_recorded")
        self.assertFalse(result["scan_recorded"])
        self.assertEqual(result["items"], [])
