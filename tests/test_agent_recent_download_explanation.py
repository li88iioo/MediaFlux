"""最近资源提交异常的安全解释测试。"""

from __future__ import annotations

from unittest.mock import patch

from app import database as db
from app.agent.recent_download_submissions import (
    RecentDownloadSubmissionStore,
    explain_recent_download_status,
)
from tests.support import IsolatedDatabaseTestCase
from tests.test_agent_recent_download_status import _submission_result

SENSITIVE_VALUES = (
    "TOPSECRET",
    "PRIVATE-HASH",
    "PRIVATE-TASK-ID",
    "/private/media",
    "SECRET-ERROR",
    "SECRET-URL",
    "PRIVATE-LOG-ERROR",
)


class RecentDownloadExplanationProjectionTests(IsolatedDatabaseTestCase):
    def _request(self, suffix: str, *, target: str = "qb") -> int:
        request_id, _ = db.create_download_request(
            f"recent-explanation-{suffix}",
            "magnet",
            title="PRIVATE-TITLE",
            source_value="magnet:?xt=urn:btih:TOPSECRET",
            origin="agent",
        )
        db.update_download_request(request_id, targets=target, status="submitted")
        return request_id

    @staticmethod
    def _record(
        request_id: int | None,
        *,
        target: str = "qb",
        dispatch_status: str = "submitted",
        result_status: str = "accepted",
        succeeded: tuple[str, ...] | None = None,
        failed: tuple[str, ...] = (),
    ):
        if succeeded is None:
            succeeded = ("qb", "guangya") if target == "both" else (target,)
        store = RecentDownloadSubmissionStore()
        store.capture(
            owner="session-a",
            result=_submission_result(
                request_id=request_id,
                target=target,
                dispatch_status=dispatch_status,
                result_status=result_status,
                created=request_id is not None,
                succeeded=succeeded,
                failed=failed,
            ),
        )
        return store.get(owner="session-a")[0]

    def test_immediate_submission_failure_is_limited_and_does_not_read_database(self):
        record = self._record(
            None,
            dispatch_status="failed",
            result_status="unavailable",
            succeeded=(),
            failed=("qb",),
        )
        with patch(
            "app.agent.recent_download_submissions.db.get_download_request_status_snapshot"
        ) as reader:
            result = explain_recent_download_status(record, position=1)
        reader.assert_not_called()
        explanation = result.data["explanation"]
        self.assertEqual(explanation["classification"], "submission_rejected")
        self.assertEqual(explanation["certainty"], "limited")
        self.assertFalse(explanation["automatic_retry"])
        self.assertNotIn("认证失败", str(result.to_dict()))

    def test_failed_partial_and_active_states_have_safe_fixed_explanations(self):
        failed_id = self._request("failed", target="both")
        db.update_download_request(
            failed_id, status="failed", qb_status="failed", gy_status="cancelled"
        )
        failed = explain_recent_download_status(
            self._record(failed_id, target="both", failed=("qb", "guangya")), position=1
        )
        self.assertEqual(
            failed.data["explanation"]["classification"], "all_targets_failed"
        )
        self.assertEqual(failed.status, "attention")
        partial_id = self._request("partial-active", target="both")
        db.update_download_request(
            partial_id, status="partial", qb_status="failed", gy_status="downloading"
        )
        partial = explain_recent_download_status(
            self._record(
                partial_id,
                target="both",
                dispatch_status="partial",
                succeeded=("guangya",),
                failed=("qb",),
            ),
            position=1,
        )
        self.assertEqual(
            partial.data["explanation"]["classification"], "partial_failure_in_progress"
        )
        self.assertIn("qBittorrent：失败。", partial.data["explanation"]["details"])
        self.assertIn("光鸭：下载中。", partial.data["explanation"]["details"])
        active_id = self._request("active")
        db.update_download_request(
            active_id, status="downloading", qb_status="downloading"
        )
        active = explain_recent_download_status(self._record(active_id), position=1)
        self.assertEqual(
            active.data["explanation"]["classification"], "still_in_progress"
        )
        self.assertIn("并非失败状态", active.summary)

    def test_partial_failed_manual_review_and_post_processing_are_distinct(self):
        partial_id = self._request("partial-failed", target="both")
        db.update_download_request(
            partial_id, status="partial", qb_status="completed", gy_status="failed"
        )
        partial = explain_recent_download_status(
            self._record(
                partial_id,
                target="both",
                dispatch_status="partial",
                failed=("guangya",),
            ),
            position=1,
        )
        self.assertEqual(
            partial.data["explanation"]["classification"], "partial_failure"
        )
        manual_id = self._request("manual")
        db.update_download_request(
            manual_id,
            status="completed",
            qb_status="completed",
            local_import_status="requires_manual",
            local_import_error="SECRET-ERROR",
        )
        manual = explain_recent_download_status(self._record(manual_id), position=1)
        self.assertEqual(
            manual.data["explanation"]["classification"], "manual_review_required"
        )
        post_id = self._request("post")
        db.update_download_request(
            post_id,
            status="completed",
            qb_status="completed",
            local_import_status="pending",
        )
        post = explain_recent_download_status(self._record(post_id), position=1)
        self.assertEqual(post.data["explanation"]["classification"], "post_processing")
        self.assertEqual(post.status, "in_progress")
        self.assertIn("未检测到下载失败", post.summary)

    def test_missing_snapshot_is_not_reported_as_download_failure(self):
        request_id = self._request("missing")
        record = self._record(request_id)
        with db.get_conn() as conn:
            conn.execute("DELETE FROM download_requests WHERE id=?", (request_id,))
        result = explain_recent_download_status(record, position=1)
        self.assertFalse(result.ok)
        self.assertEqual(
            result.data["explanation"]["classification"], "tracking_unavailable"
        )
        self.assertIn("不等同于下载失败", result.data["explanation"]["details"][0])

    def test_explanation_never_exposes_sensitive_request_or_log_columns(self):
        request_id = self._request("secrets")
        db.update_download_request(
            request_id,
            status="failed",
            qb_status="failed",
            qb_task_id="PRIVATE-HASH",
            qb_content_path="/private/media/secret.mkv",
            error="SECRET-ERROR",
        )
        db.add_download_log(
            "qb",
            title="PRIVATE-TITLE",
            path="magnet:?xt=SECRET-URL",
            request_id=request_id,
            backend_task_id="PRIVATE-TASK-ID",
            progress=0.3,
            error="PRIVATE-LOG-ERROR",
        )
        result = explain_recent_download_status(self._record(request_id), position=1)
        serialized = str(result.to_dict())
        for secret in SENSITIVE_VALUES:
            self.assertNotIn(secret, serialized)
        self.assertNotIn("request_id", result.data)

    def test_completed_duplicate_and_persisted_unknown_have_distinct_meanings(self):
        completed_id = self._request("completed")
        db.update_download_request(
            completed_id, status="completed", qb_status="completed"
        )
        completed = explain_recent_download_status(
            self._record(completed_id), position=1
        )
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.data["phase"], "completed")
        self.assertEqual(completed.data["explanation"]["classification"], "completed")
        self.assertFalse(completed.data["explanation"]["automatic_retry"])
        duplicate = self._record(
            123, dispatch_status="duplicate", result_status="conflict", succeeded=()
        )
        with patch(
            "app.agent.recent_download_submissions.db.get_download_request_status_snapshot"
        ) as reader:
            duplicate_result = explain_recent_download_status(duplicate, position=1)
        reader.assert_not_called()
        self.assertEqual(duplicate_result.status, "conflict")
        self.assertEqual(duplicate_result.data["phase"], "already_submitted")
        self.assertEqual(
            duplicate_result.data["explanation"]["classification"], "already_submitted"
        )
        self.assertEqual(duplicate_result.data["explanation"]["certainty"], "limited")
        unknown_id = self._request("persisted-unknown")
        db.update_download_request(
            unknown_id, status="completed", qb_status="unexpected-backend-value"
        )
        unknown = explain_recent_download_status(self._record(unknown_id), position=1)
        self.assertTrue(unknown.ok)
        self.assertEqual(unknown.status, "attention")
        self.assertEqual(unknown.data["phase"], "unknown")
        self.assertEqual(
            unknown.data["explanation"]["classification"], "state_indeterminate"
        )
        self.assertEqual(unknown.data["explanation"]["certainty"], "limited")
