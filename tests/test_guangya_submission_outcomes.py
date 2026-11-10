"""光鸭离线提交部分成功/未知终态的防重复提交回归测试。"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.modules.download_dispatcher import (
    _submit_guangya,
    dispatch_missing_targets,
    dispatch_request,
    public_dispatch_summary,
)
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
        with patch("app.repositories.download_requests.get_download_request", return_value=row), patch(
            "app.repositories.download_requests.claim_download_request", return_value=True
        ), patch(
            "app.modules.download_dispatcher._submit_guangya", return_value=partial
        ), patch(
            "app.repositories.download_requests.finalize_download_request_submission", return_value="submitted"
        ) as finalize, patch("app.repositories.download_requests.add_download_log") as add_log:
            result = dispatch_request(91, "guangya")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "submitted")
        self.assertTrue(result["outcome_unknown"])
        self.assertTrue(result["review_required"])
        self.assertEqual(finalize.call_args.kwargs["gy_status"], "outcome_unknown")
        # 执行器只提交后端事实，根状态由仓储事务中的唯一归并逻辑返回。
        self.assertNotIn("status", finalize.call_args.kwargs)
        self.assertNotIn("completed_at", finalize.call_args.kwargs)
        self.assertEqual(json.loads(finalize.call_args.kwargs["gy_task_ids"]), ["gy-accepted"])
        self.assertEqual(add_log.call_args.kwargs["status"], "outcome_unknown")

    def test_dispatch_missing_target_preserves_partial_submission_as_outcome_unknown(self):
        row = self._request_row(92)
        partial = self._partial_result()
        with patch("app.repositories.download_requests.get_download_request", return_value=row), patch(
            "app.repositories.download_requests.claim_download_request_targets", return_value=["guangya"]
        ), patch(
            "app.modules.download_dispatcher._submit_guangya", return_value=partial
        ), patch(
            "app.repositories.download_requests.finalize_download_request_submission", return_value="submitted"
        ) as finalize, patch("app.repositories.download_requests.add_download_log") as add_log:
            result = dispatch_missing_targets(92, "guangya")

        self.assertTrue(result["handled"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "submitted")
        self.assertTrue(result["outcome_unknown"])
        self.assertEqual(finalize.call_args.kwargs["gy_status"], "outcome_unknown")
        self.assertEqual(add_log.call_args.kwargs["status"], "outcome_unknown")

    def test_torrent_submission_passes_original_bytes_to_guangya_resolver(self):
        torrent_data = b"private-torrent-bytes"
        row = {
            "id": 95,
            "title": "Torrent upload",
            "source_value": "magnet:?xt=urn:btih:" + "a" * 40,
            "kind": "torrent",
            "torrent_data": torrent_data,
        }
        with patch(
            "app.modules.download_dispatcher.submit_offline",
            return_value={"ok": True, "task_ids": ["gy-task"]},
        ) as submit:
            result = _submit_guangya(row)

        self.assertTrue(result["ok"])
        self.assertEqual(result["task_id"], "gy-task")
        self.assertEqual(submit.call_args.kwargs["torrent_data"], torrent_data)

    def test_staging_is_persisted_before_offline_provider_submission(self):
        row = self._request_row(96)

        def submit_side_effect(*_args, **kwargs):
            kwargs["on_staging_created"]({
                "id": "stage-96",
                "parent_id": "parent",
                "parent_name": "下载",
                "name": "MF-96-Partial-Batch",
            })
            return {"ok": True, "task_ids": ["gy-task"]}

        with patch(
            "app.modules.download_dispatcher.submit_offline",
            side_effect=submit_side_effect,
        ), patch(
            "app.repositories.download_requests.bind_download_request_guangya_staging",
            return_value=True,
        ) as bind:
            result = _submit_guangya(row)

        self.assertTrue(result["ok"])
        bind.assert_called_once_with(
            96,
            staging_id="stage-96",
            parent_id="parent",
            staging_name="MF-96-Partial-Batch",
            target_name="下载 / MF-96-Partial-Batch",
        )

    def test_staging_persistence_rejection_stops_offline_submission(self):
        row = self._request_row(97)

        def submit_side_effect(*_args, **kwargs):
            kwargs["on_staging_created"]({
                "id": "stage-97",
                "parent_id": "parent",
                "parent_name": "下载",
                "name": "MF-97-Partial-Batch",
            })

        with patch(
            "app.modules.download_dispatcher.submit_offline",
            side_effect=submit_side_effect,
        ), patch(
            "app.repositories.download_requests.bind_download_request_guangya_staging",
            return_value=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "请求已失效"):
                _submit_guangya(row)

    def test_public_failure_keeps_content_rejection_without_raw_provider_details(self):
        for succeeded in ([], ["qb"]):
            for error in (
                "文件违规",
                "guangya: 文件违规",
                "guangya: 光鸭任务创建失败: 文件违规 <b>private</b> "
                "https://private.example/file?token=secret magnet:?xt=urn:btih:private",
            ):
                with self.subTest(succeeded=succeeded, error=error):
                    summary = public_dispatch_summary({
                        "succeeded": succeeded, "failed": ["guangya"], "error": error,
                    })
                    self.assertEqual(summary["error"], "光鸭返回：文件违规")
                    self.assertEqual(summary["status"], "partial" if succeeded else "failed")
                    self.assertEqual(summary["succeeded"], succeeded)
                    self.assertEqual(summary["failed"], ["guangya"])
                    self.assertEqual(summary["ok"], bool(succeeded))
                    self.assertEqual(public_dispatch_summary(summary), summary)
                    self.assertNotIn("private", json.dumps(summary))
                    self.assertNotIn("secret", json.dumps(summary))

    def test_content_rejection_does_not_override_unknown_or_duplicate_outcome(self):
        for outcome in (
            {"outcome_unknown": True},
            {"review_required": True},
            {"status": "manual_review"},
            {"duplicate": True, "existing_status": "submitted"},
        ):
            with self.subTest(outcome=outcome):
                summary = public_dispatch_summary({
                    "succeeded": [], "failed": ["guangya"],
                    "error": "guangya: 文件违规", **outcome,
                })
                self.assertEqual(summary["status"], "duplicate" if outcome.get("duplicate") else "manual_review")
                self.assertNotIn("文件违规", summary["error"])
                if not outcome.get("duplicate"):
                    self.assertIn("勿直接重复提交", summary["error"])

    def test_public_failure_distinguishes_torrent_from_magnet_resolution(self):
        summary = public_dispatch_summary({
            "ok": False,
            "status": "failed",
            "succeeded": [],
            "failed": ["guangya"],
            "error": "guangya: 种子文件未解析到可验证文件列表（尝试 1 次）",
        })

        self.assertEqual(summary["error"], "光鸭种子未解析到有效文件列表")

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
            "app.modules.download_tracker.apply_download_tracker_update", side_effect=lambda snapshot, **fields: {**snapshot, **fields}
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
            "app.modules.download_tracker.apply_download_tracker_update", side_effect=lambda snapshot, **fields: {**snapshot, **fields}
        ) as update, patch.object(tracker, "_update_backend_log"), patch.object(
            tracker, "_notify_completion"
        ):
            tracker._update_request(row, [], [task], qb_available=False, gy_available=True)

        self.assertEqual(update.call_args.kwargs["gy_status"], "manual_review")
        self.assertEqual(update.call_args.kwargs["status"], "manual_review")
        self.assertIn("勿直接重复提交", update.call_args.kwargs["error"])


if __name__ == "__main__":
    unittest.main()
