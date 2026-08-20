"""光鸭离线提交部分成功/未知终态的防重复提交回归测试。"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.modules.download_dispatcher import dispatch_missing_targets, dispatch_request
from app.modules.download_tracker import DownloadTracker


class GuangYaSubmissionOutcomeTests(unittest.TestCase):
    @staticmethod
    def _request_row(request_id: int = 91) -> dict:
        return {
            "id": request_id,
            "title": "Partial Batch",
            "source_value": "magnet:?xt=urn:btih:partial",
            "kind": "magnet",
            "torrent_data": None,
            "qb_status": "",
            "gy_status": "",
        }

    @staticmethod
    def _partial_result() -> dict:
        return {
            "ok": False,
            "partial_success": True,
            "outcome_unknown": True,
            "task_ids": ["gy-accepted"],
            "batch_count": 2,
            "selected_count": 24,
            "error": "第 2 批返回超时，部分任务可能已经提交",
            "decision": {"target_dir_id": "target", "target_dir_name": "下载"},
            "staging": {"isolated": True, "parent_id": "parent", "name": "MF-91"},
        }

    def test_dispatch_request_preserves_partial_submission_as_outcome_unknown(self):
        row = self._request_row()
        partial = self._partial_result()
        with patch("app.database.get_download_request", return_value=row), patch(
            "app.database.claim_download_request", return_value=True
        ), patch(
            "app.modules.download_dispatcher._submit_guangya", return_value=partial
        ), patch(
            "app.database.update_download_request_and_sync_media_admission"
        ) as update, patch("app.database.add_download_log") as add_log:
            result = dispatch_request(91, "guangya")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "submitted")
        self.assertTrue(result["outcome_unknown"])
        self.assertTrue(result["review_required"])
        self.assertEqual(update.call_args.kwargs["gy_status"], "outcome_unknown")
        self.assertEqual(update.call_args.kwargs["status"], "submitted")
        self.assertNotIn("completed_at", update.call_args.kwargs)
        self.assertEqual(json.loads(update.call_args.kwargs["gy_task_ids"]), ["gy-accepted"])
        self.assertEqual(add_log.call_args.kwargs["status"], "outcome_unknown")

    def test_dispatch_missing_target_preserves_partial_submission_as_outcome_unknown(self):
        row = self._request_row(92)
        partial = self._partial_result()
        with patch("app.database.get_download_request", return_value=row), patch(
            "app.database.claim_download_request_targets", return_value=["guangya"]
        ), patch(
            "app.modules.download_dispatcher._submit_guangya", return_value=partial
        ), patch(
            "app.database.update_download_request_and_sync_media_admission"
        ) as update, patch("app.database.add_download_log") as add_log:
            result = dispatch_missing_targets(92, "guangya")

        self.assertTrue(result["handled"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "submitted")
        self.assertTrue(result["outcome_unknown"])
        self.assertEqual(update.call_args.kwargs["gy_status"], "outcome_unknown")
        self.assertEqual(add_log.call_args.kwargs["status"], "outcome_unknown")

    def test_tracker_reconciles_unknown_submission_with_known_task_id(self):
        tracker = DownloadTracker()
        row = {
            **self._request_row(93),
            "status": "submitted",
            "gy_status": "outcome_unknown",
            "gy_task_id": "gy-accepted",
            "gy_task_ids": json.dumps(["gy-accepted"]),
            "gy_batch_count": 1,
            "organize_started": 1,
        }
        task = {"id": "gy-accepted", "status": "completed", "progress": 1.0}
        with patch(
            "app.database.update_download_request_and_sync_media_admission"
        ) as update, patch.object(tracker, "_update_backend_log"), patch.object(
            tracker, "_notify_completion"
        ):
            tracker._update_request(row, [], [task], qb_available=False, gy_available=True)

        self.assertEqual(update.call_args.kwargs["gy_status"], "completed")
        self.assertEqual(update.call_args.kwargs["status"], "completed")

    def test_tracker_routes_incomplete_batch_identity_to_manual_review(self):
        tracker = DownloadTracker()
        row = {
            **self._request_row(94),
            "status": "submitted",
            "gy_status": "outcome_unknown",
            "gy_task_id": "gy-accepted",
            "gy_task_ids": json.dumps(["gy-accepted"]),
            "gy_batch_count": 2,
            "organize_started": 0,
        }
        task = {"id": "gy-accepted", "status": "downloading", "progress": 0.5}
        with patch(
            "app.database.update_download_request_and_sync_media_admission"
        ) as update, patch.object(tracker, "_update_backend_log"), patch.object(
            tracker, "_notify_completion"
        ):
            tracker._update_request(row, [], [task], qb_available=False, gy_available=True)

        self.assertEqual(update.call_args.kwargs["gy_status"], "manual_review")
        self.assertEqual(update.call_args.kwargs["status"], "manual_review")
        self.assertIn("勿直接重复提交", update.call_args.kwargs["error"])


if __name__ == "__main__":
    unittest.main()
