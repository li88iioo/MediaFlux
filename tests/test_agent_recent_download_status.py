"""最近资源提交后的安全任务状态续接测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app import database as db
from app.agent.models import Evidence, ToolResult
from app.agent.recent_download_submissions import (
    RecentDownloadSubmissionStore,
    build_recent_download_status,
    sanitize_submission_confirmation_result,
)
from tests.support import IsolatedDatabaseTestCase


def _contains_key(value, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(
            _contains_key(item, key) for item in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_key(item, key) for item in value)
    return False


def _submission_result(
    *,
    request_id: int | None,
    target: str = "qb",
    dispatch_status: str = "submitted",
    result_status: str = "accepted",
    created: bool = True,
    duplicate: bool = False,
    succeeded: tuple[str, ...] = ("qb",),
    failed: tuple[str, ...] = (),
) -> ToolResult:
    data = {
        "result_id": "safe-result-00000001",
        "created": created,
        "target": target,
        "status": dispatch_status,
        "succeeded": list(succeeded),
        "failed": list(failed),
        "duplicate": duplicate,
    }
    if request_id is not None:
        data["request_id"] = request_id
    return ToolResult(
        result_status == "accepted", result_status, "submitted", data=data
    )


class RecentDownloadSubmissionStoreTests(unittest.TestCase):
    def test_owner_ttl_and_per_owner_limit(self):
        now = [10.0]
        store = RecentDownloadSubmissionStore(
            ttl_seconds=5, max_owners=2, max_items_per_owner=2, clock=lambda: now[0]
        )
        self.assertTrue(
            store.capture(owner="session-a", result=_submission_result(request_id=1))
        )
        self.assertTrue(
            store.capture(owner="session-a", result=_submission_result(request_id=2))
        )
        self.assertTrue(
            store.capture(owner="session-a", result=_submission_result(request_id=3))
        )
        self.assertEqual(
            [item.request_id for item in store.get(owner="session-a")], [3, 2]
        )
        self.assertEqual(store.get(owner="session-b"), ())
        store.capture(owner="session-b", result=_submission_result(request_id=4))
        store.capture(owner="session-c", result=_submission_result(request_id=5))
        self.assertEqual(store.get(owner="session-a"), ())
        now[0] = 16.0
        self.assertEqual(store.get(owner="session-c"), ())

    def test_each_submission_expires_independently(self):
        now = [0.0]
        store = RecentDownloadSubmissionStore(ttl_seconds=5, clock=lambda: now[0])
        store.capture(owner="session-a", result=_submission_result(request_id=1))
        now[0] = 4.0
        store.capture(owner="session-a", result=_submission_result(request_id=2))
        now[0] = 6.0
        self.assertEqual(
            [item.request_id for item in store.get(owner="session-a")], [2]
        )

    def test_rejects_invalid_or_unrelated_results(self):
        store = RecentDownloadSubmissionStore()
        self.assertFalse(
            store.capture(owner="", result=_submission_result(request_id=1))
        )
        self.assertFalse(
            store.capture(
                owner="session-a",
                result=ToolResult(
                    True, "ok", "other", data={"target": "qb", "status": "submitted"}
                ),
            )
        )
        self.assertFalse(
            store.capture(
                owner="session-a",
                result=_submission_result(request_id=1, target="invalid"),
            )
        )
        self.assertEqual(store.get(owner="session-a"), ())


class RecentDownloadStatusProjectionTests(IsolatedDatabaseTestCase):
    def _request(self, suffix: str, *, target: str = "qb") -> int:
        request_id, _ = db.create_download_request(
            f"recent-status-{suffix}",
            "magnet",
            title="safe title",
            source_value="magnet:?xt=urn:btih:TOPSECRET",
            origin="agent",
        )
        db.update_download_request(request_id, targets=target, status="submitted")
        return request_id

    def _record(self, request_id: int, *, target: str = "qb", partial: bool = False):
        result = _submission_result(
            request_id=request_id,
            target=target,
            dispatch_status="partial" if partial else "submitted",
            succeeded=("qb",) if target == "both" else (target,),
            failed=("guangya",) if partial else (),
        )
        store = RecentDownloadSubmissionStore()
        store.capture(owner="session-a", result=result)
        return store.get(owner="session-a")[0]

    def test_qb_progress_and_sensitive_columns_are_not_exposed(self):
        request_id = self._request("progress")
        db.update_download_request(
            request_id,
            status="downloading",
            qb_status="downloading",
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
            progress=0.42,
            error="PRIVATE-LOG-ERROR",
        )
        result = build_recent_download_status(self._record(request_id), position=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["phase"], "downloading")
        self.assertEqual(result.data["backends"][0]["progress_percent"], 42)
        serialized = str(result.to_dict())
        self.assertFalse(_contains_key(result.data, "request_id"))
        for secret in (
            "TOPSECRET",
            "PRIVATE-HASH",
            "/private/media",
            "SECRET-ERROR",
            "SECRET-URL",
            "PRIVATE-TASK-ID",
            "PRIVATE-LOG-ERROR",
        ):
            self.assertNotIn(secret, serialized)

    def test_both_backends_preserve_partial_failure(self):
        request_id = self._request("partial", target="both")
        db.update_download_request(
            request_id,
            status="completed",
            qb_status="completed",
            gy_status="failed",
            completed_at=db.now(),
        )
        result = build_recent_download_status(
            self._record(request_id, target="both", partial=True), position=1
        )
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["phase"], "partial_failed")
        self.assertTrue(result.data["terminal"])
        self.assertEqual(
            [(item["name"], item["status"]) for item in result.data["backends"]],
            [("qb", "completed"), ("guangya", "failed")],
        )

    def test_mixed_backend_states_never_report_terminal_completed(self):
        request_id = self._request("mixed-active", target="both")
        db.update_download_request(
            request_id,
            status="completed",
            qb_status="completed",
            gy_status="downloading",
            local_import_status="pending",
        )
        result = build_recent_download_status(
            self._record(request_id, target="both"), position=1
        )
        self.assertEqual(result.status, "in_progress")
        self.assertEqual(result.data["phase"], "downloading")
        self.assertFalse(result.data["terminal"])
        db.update_download_request(
            request_id,
            qb_status="failed",
            gy_status="downloading",
            local_import_status="",
        )
        partial = build_recent_download_status(
            self._record(request_id, target="both"), position=1
        )
        self.assertEqual(partial.status, "attention")
        self.assertEqual(partial.data["phase"], "partial_in_progress")
        self.assertFalse(partial.data["terminal"])
        self.assertTrue(partial.data["needs_attention"])

    def test_confirmation_result_uses_strict_safe_projection(self):
        raw = ToolResult(
            False,
            "unavailable",
            "magnet:?xt=RAW-SUMMARY",
            data={
                "request_id": 42,
                "result_id": "candidate-secret",
                "target": "both",
                "status": "failed",
                "created": True,
                "duplicate": False,
                "succeeded": ["qb", "unknown"],
                "failed": ["guangya"],
                "nested": {
                    "url": "https://secret.invalid/file",
                    "backend_task_id": "task-secret",
                    "path": "/private/download",
                },
            },
            evidence=[
                Evidence(
                    "backend",
                    "raw evidence https://secret.invalid/evidence task-secret-evidence",
                    "2026-08-03T19:00:00+08:00",
                )
            ],
            suggestions=["retry magnet:?xt=RAW-SUGGESTION"],
            error="backend raw error /private/download",
        )
        public = sanitize_submission_confirmation_result(raw)
        serialized = str(public.to_dict())
        self.assertFalse(public.ok)
        self.assertEqual(public.status, "unavailable")
        self.assertEqual(
            set(public.data),
            {"target", "status", "created", "duplicate", "succeeded", "failed"},
        )
        for secret in (
            "RAW-SUMMARY",
            "candidate-secret",
            "secret.invalid",
            "task-secret",
            "task-secret-evidence",
            "/private/download",
            "RAW-SUGGESTION",
            "backend raw error",
            "raw evidence",
        ):
            self.assertNotIn(secret, serialized)
        self.assertFalse(_contains_key(public.data, "request_id"))

    def test_both_backends_complete_can_enter_post_processing(self):
        request_id = self._request("both-post", target="both")
        db.update_download_request(
            request_id,
            status="completed",
            qb_status="completed",
            gy_status="completed",
            organize_started=1,
            local_import_status="pending",
        )
        result = build_recent_download_status(
            self._record(request_id, target="both"), position=1
        )
        self.assertEqual(result.status, "in_progress")
        self.assertEqual(result.data["phase"], "post_processing")
        self.assertFalse(result.data["terminal"])
        self.assertFalse(result.data["needs_attention"])

    def test_post_processing_and_missing_record(self):
        request_id = self._request("post")
        db.update_download_request(
            request_id,
            status="completed",
            qb_status="completed",
            local_import_status="pending",
        )
        record = self._record(request_id)
        result = build_recent_download_status(record, position=1)
        self.assertEqual(result.status, "in_progress")
        self.assertEqual(result.data["phase"], "post_processing")
        self.assertFalse(result.data["terminal"])
        self.assertEqual(result.data["local_processing"]["local_import"], "in_progress")
        with db.get_conn() as conn:
            conn.execute("DELETE FROM download_requests WHERE id=?", (request_id,))
        missing = build_recent_download_status(record, position=1)
        self.assertFalse(missing.ok)
        self.assertEqual(missing.status, "unavailable")
        self.assertFalse(_contains_key(missing.data, "request_id"))

    def test_guangya_completed_waiting_for_organize_is_not_terminal(self):
        request_id = self._request("gy-organize", target="guangya")
        db.update_download_request(
            request_id, status="completed", gy_status="completed", organize_started=0
        )
        result = build_recent_download_status(
            self._record(request_id, target="guangya"), position=1
        )
        self.assertEqual(result.status, "in_progress")
        self.assertEqual(result.data["phase"], "post_processing")
        self.assertFalse(result.data["terminal"])

    def test_immediate_failure_does_not_read_database(self):
        store = RecentDownloadSubmissionStore()
        store.capture(
            owner="session-a",
            result=_submission_result(
                request_id=None,
                dispatch_status="failed",
                result_status="unavailable",
                created=False,
                succeeded=(),
                failed=("qb",),
            ),
        )
        with patch(
            "app.agent.recent_download_submissions.db.get_download_request_status_snapshot"
        ) as reader:
            result = build_recent_download_status(
                store.get(owner="session-a")[0], position=1
            )
        reader.assert_not_called()
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["phase"], "failed")

    def test_immediate_duplicate_does_not_read_database(self):
        store = RecentDownloadSubmissionStore()
        store.capture(
            owner="session-a",
            result=_submission_result(
                request_id=123,
                dispatch_status="duplicate",
                result_status="conflict",
                created=False,
                duplicate=True,
                succeeded=(),
            ),
        )
        with patch(
            "app.agent.recent_download_submissions.db.get_download_request_status_snapshot"
        ) as reader:
            record = store.get(owner="session-a")[0]
            result = build_recent_download_status(record, position=1)
        reader.assert_not_called()
        self.assertIsNone(record.request_id)
        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.data["phase"], "already_submitted")
