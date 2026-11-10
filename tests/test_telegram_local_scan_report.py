"""本地整理批次的通知必须区分任务结束与文件归档。"""

from unittest.mock import Mock, patch

from app import database as db
from app.repositories import local_media as local_media_repo
from app.bot import handlers
from app.modules.local_media_scan_runs import (
    record_local_media_scan,
    resolve_local_media_scan,
)
from app.notifier import render_event
from tests.support import IsolatedDatabaseTestCase


class TelegramLocalScanReportTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM local_media_operation_steps")
            conn.execute("DELETE FROM local_media_task_items")
            conn.execute("DELETE FROM local_media_tasks")
            conn.execute("DELETE FROM local_library_targets")
            conn.execute("DELETE FROM local_media_sources")

    def _task(self, name, status, action=None):
        source_id = local_media_repo.create_local_media_source(
            name=f"下载-{name}", qb_profile="", qb_path_prefix="", local_root="/private/downloads"
        )
        path = f"/private/downloads/{name}"
        task_id = local_media_repo.create_local_media_task(source_id, "", path, trigger="scan")
        local_media_repo.update_local_media_task(task_id, status=status, title="示例剧集")
        if action:
            local_media_repo.add_local_media_task_item(task_id, path, f"/private/library/{name}", role="video", action=action)
        return task_id

    def test_notice_freezes_exact_batch_and_does_not_call_skipped_file_archived(self):
        skipped = self._task("Show.S02E11.mkv", "completed", "skip")
        pending = self._task("Unmatched.S01E11.mp4", "requires_manual")
        old = self._task("Yesterday.mkv", "completed", "move")
        scan: dict = {
            "ok": True, "task_ids": [skipped, pending], "candidate_count": 2,
            "source_count": 1, "scanned_sources": 1, "queued_count": 2,
        }
        scan["scan_ref"] = record_local_media_scan(scan)
        scheduler = Mock()
        scheduler.status.return_value = {"running": True}
        scheduler.enqueue_manual_scan_candidates.return_value = scan
        scheduler.take_captured_task_result.return_value = None
        with patch("app.modules.local_media_scheduler.get_local_media_scheduler", return_value=scheduler):
            result = handlers._run_local_organize_stage(None, progress_title="本地整理")
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["skipped_items"], 1)
        self.assertEqual(result["moved_items"], 0)
        self.assertEqual(result["requires_manual"], 1)
        saved = resolve_local_media_scan(scan["scan_ref"])
        self.assertNotIn(old, saved["task_ids"])
        self.assertEqual(saved["summary"]["task_outcomes"][0]["file_outcome"], "conflict_skipped")
        rendered = render_event(handlers._local_organize_event(result))
        self.assertIn(scan["scan_ref"], rendered)
        self.assertIn("1 已结束", rendered)
        self.assertIn("0 已归档", rendered)
        self.assertIn("Show.S02E11.mkv：冲突跳过", rendered)
        self.assertIn("Unmatched.S01E11.mp4：待确认", rendered)
        self.assertNotIn("Yesterday", rendered)
        self.assertNotIn("/private", rendered)
        scheduler.start.assert_not_called()

    def test_combined_organize_keeps_local_scan_reference_and_skip_counts(self):
        summary = {
            "scan_ref": "LM20", "completed": 1, "skipped_items": 1,
            "requires_manual": 0, "moved_items": 0,
        }
        rendered = render_event(handlers._all_organize_event({"status": "completed"}, summary))
        self.assertIn("LM20", rendered)
        self.assertIn("1 已结束", rendered)
        self.assertIn("0 已归档", rendered)
        self.assertIn("1 冲突跳过", rendered)

    def test_filenames_in_scan_notice_are_escaped_and_do_not_reveal_directories(self):
        task = self._task("<script>alert(1)</script>.mkv", "requires_manual")
        from app.modules.local_media_outcomes import local_media_task_outcome
        item = local_media_task_outcome(local_media_repo.get_local_media_task(task), [])
        rendered = render_event(handlers._local_organize_event({
            "task_outcomes": [{"status": "requires_manual", **item}],
            "candidate_count": 1, "requires_manual": 1,
        }))
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("/private", rendered)

    def test_receipt_failure_keeps_real_results_but_explicitly_marks_missing_batch(self):
        summary = {
            "scan_ref": "LM-UNRECORDED", "scan_recorded": False,
            "completed": 1, "skipped_items": 1, "moved_items": 0,
            "candidate_count": 1,
        }
        rendered = render_event(handlers._local_organize_event(summary))
        self.assertIn("1 已结束", rendered)
        self.assertIn("1 按冲突策略跳过", rendered)
        self.assertIn("回执未保存", rendered)
        self.assertIn("请勿用上一次扫描替代", rendered)
