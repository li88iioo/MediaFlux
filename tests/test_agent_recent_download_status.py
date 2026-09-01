"""最近资源提交后的安全任务状态续接测试。"""
from __future__ import annotations

import re
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database as db
from app.agent.confirmation import ConfirmationStore
from app.agent.ingest_actions import ingest_submit_arguments
from app.agent.models import Evidence, RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import (
    AgentOrchestrator,
    is_recent_download_status_message,
    recent_download_status_request,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.recent_download_submissions import (
    RecentDownloadSubmissionStore,
    build_recent_download_status,
    sanitize_submission_confirmation_result,
)
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.service import reset_agent_service_for_tests
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


def _contains_key(value, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
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
        result_status == "accepted",
        result_status,
        "submitted",
        data=data,
    )


def _submission_agent(
    result: ToolResult,
    *,
    store: RecentDownloadSubmissionStore | None = None,
    confirmation_store: ConfirmationStore | None = None,
    context: dict[str, str] | None = None,
):
    calls: list[dict] = []
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="indexer.search_resources",
        description="search",
        risk=RiskLevel.READ,
        parameters={},
        validator=lambda arguments: dict(arguments),
        handler=lambda _arguments: ToolResult(
            True,
            "success",
            "searched",
            data={
                "items": [
                    {
                        "result_id": "safe-result-00000001",
                        "title": "Safe Resource",
                        "site_id": "test",
                        "site_name": "Test",
                        "size_text": "1 GiB",
                        "download_state": "ready",
                        "download_kinds": ["magnet"],
                    }
                ]
            },
        ),
    ))
    def confirmation_context(arguments: dict) -> str:
        if context is not None:
            return context["value"]
        return f"{arguments['source_type']}:{arguments['positions']}:{arguments['target']}"

    def prepare_submission(arguments: dict) -> tuple[ToolResult, str]:
        return (
            ToolResult(
                True,
                "confirmation_required",
                "preview",
                data=dict(arguments),
            ),
            confirmation_context(arguments),
        )

    def confirm_submission(arguments: dict, expected_context: str) -> ToolResult:
        if confirmation_context(arguments) != expected_context:
            raise AgentToolError(
                "资源提交上下文已变化，请重新确认",
                code="confirmation_stale",
            )
        calls.append(dict(arguments))
        return result

    registry.register(ToolSpec(
        name="ingest.submit",
        description="submit",
        risk=RiskLevel.DANGER,
        parameters={},
        validator=ingest_submit_arguments,
        requires_confirmation=True,
        context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(prepare_submission),
        context_confirmed_handler=ToolSpec.context_free_confirmed_handler(confirm_submission),
    ))
    service = AgentOrchestrator(
        registry,
        confirmation_store or ConfirmationStore(
            token_factory=lambda: "confirm-download-status-0001"
        ),
        recent_download_store=store,
    )
    return service, calls


class RecentDownloadSubmissionStoreTests(unittest.TestCase):
    def test_owner_ttl_and_per_owner_limit(self):
        now = [10.0]
        store = RecentDownloadSubmissionStore(
            ttl_seconds=5,
            max_owners=2,
            max_items_per_owner=2,
            clock=lambda: now[0],
        )
        self.assertTrue(store.capture(owner="session-a", result=_submission_result(request_id=1)))
        self.assertTrue(store.capture(owner="session-a", result=_submission_result(request_id=2)))
        self.assertTrue(store.capture(owner="session-a", result=_submission_result(request_id=3)))
        self.assertEqual([item.request_id for item in store.get(owner="session-a")], [3, 2])
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
        self.assertEqual([item.request_id for item in store.get(owner="session-a")], [2])

    def test_rejects_invalid_or_unrelated_results(self):
        store = RecentDownloadSubmissionStore()
        self.assertFalse(store.capture(owner="", result=_submission_result(request_id=1)))
        self.assertFalse(store.capture(
            owner="session-a",
            result=ToolResult(True, "ok", "other", data={"target": "qb", "status": "submitted"}),
        ))
        self.assertFalse(store.capture(
            owner="session-a",
            result=_submission_result(request_id=1, target="invalid"),
        ))
        self.assertEqual(store.get(owner="session-a"), ())


class RecentDownloadStatusIntentTests(unittest.TestCase):
    def test_parser_accepts_recent_and_ordinal_status_queries(self):
        self.assertEqual(recent_download_status_request("刚才下载到哪了"), {"position": None})
        self.assertEqual(recent_download_status_request("上次推送成功了吗"), {"position": None})
        self.assertEqual(recent_download_status_request("第 1 个任务状态"), {"position": 1})
        self.assertTrue(is_recent_download_status_message("刚才提交的资源完成了吗"))

    def test_parser_does_not_steal_existing_routes_or_write_intent(self):
        for message in (
            "检查 qB 下载状态",
            "刚才 qB 下载状态",
            "刚才qb下载状态",
            "光鸭整理任务状态",
            "查看 STRM 同步进度",
            "全局搜索《下载任务》的状态",
            "下载刚才推荐的第 1 个到 qB",
            "刚才下载为什么失败",
            "最近下载队列状态",
            "最近 RSS 失败任务状态",
            "最近 STRM 任务状态",
            "检查系统运行状态和最近失败任务",
            "查看项目最近后台任务状态",
        ):
            self.assertIsNone(recent_download_status_request(message), message)


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
            "TOPSECRET", "PRIVATE-HASH", "/private/media",
            "SECRET-ERROR", "SECRET-URL", "PRIVATE-TASK-ID", "PRIVATE-LOG-ERROR",
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
            "RAW-SUMMARY", "candidate-secret", "secret.invalid", "task-secret",
            "task-secret-evidence", "/private/download", "RAW-SUGGESTION",
            "backend raw error", "raw evidence",
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
            request_id,
            status="completed",
            gy_status="completed",
            organize_started=0,
        )
        result = build_recent_download_status(
            self._record(request_id, target="guangya"), position=1
        )
        self.assertEqual(result.status, "in_progress")
        self.assertEqual(result.data["phase"], "post_processing")
        self.assertFalse(result.data["terminal"])

    def test_immediate_failure_does_not_read_database(self):
        store = RecentDownloadSubmissionStore()
        store.capture(owner="session-a", result=_submission_result(
            request_id=None,
            dispatch_status="failed",
            result_status="unavailable",
            created=False,
            succeeded=(),
            failed=("qb",),
        ))
        with patch(
            "app.agent.recent_download_submissions.db.get_download_request_status_snapshot"
        ) as reader:
            result = build_recent_download_status(store.get(owner="session-a")[0], position=1)
        reader.assert_not_called()
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["phase"], "failed")

    def test_immediate_duplicate_does_not_read_database(self):
        store = RecentDownloadSubmissionStore()
        store.capture(owner="session-a", result=_submission_result(
            request_id=123,
            dispatch_status="duplicate",
            result_status="conflict",
            created=False,
            duplicate=True,
            succeeded=(),
        ))
        with patch(
            "app.agent.recent_download_submissions.db.get_download_request_status_snapshot"
        ) as reader:
            record = store.get(owner="session-a")[0]
            result = build_recent_download_status(record, position=1)
        reader.assert_not_called()
        self.assertIsNone(record.request_id)
        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.data["phase"], "already_submitted")


class RecentDownloadStatusOrchestratorTests(IsolatedDatabaseTestCase):
    def test_prepare_does_not_capture_confirm_does_and_owner_is_bound(self):
        request_id, _ = db.create_download_request(
            "recent-status-confirm",
            "magnet",
            source_value="magnet:?xt=SECRET-CONFIRM",
            origin="agent",
        )
        db.update_download_request(
            request_id, targets="qb", status="downloading", qb_status="downloading"
        )
        service, calls = _submission_agent(_submission_result(request_id=request_id))
        service.invoke(
            "indexer.search_resources", {"title": "Safe Resource"}, owner="session-a"
        )
        prepared = service.prepare(
            "ingest.submit",
            {"source_type": "resource_candidates", "positions": [1], "target": "qb"},
            owner="session-a",
        )
        before = service.query("刚才下载到哪了", owner="session-a")
        self.assertEqual(before["result"]["status"], "precondition_failed")
        self.assertEqual(calls, [])

        confirmed = service.confirm(prepared["action_plan"]["plan_id"], owner="session-a")
        self.assertEqual(len(calls), 1)
        self.assertFalse(_contains_key(confirmed["result"]["data"], "request_id"))

        own = service.query("刚才下载到哪了", owner="session-a")
        other = service.query("刚才下载到哪了", owner="session-b")
        self.assertEqual(own["tool_call"]["name"], "downloads.recent_submission_status")
        self.assertEqual(own["result"]["data"]["phase"], "downloading")
        self.assertEqual(other["result"]["status"], "precondition_failed")
        self.assertEqual(len(calls), 1)

    def test_invalid_expired_replayed_and_stale_confirmations_do_not_capture(self):
        service, calls = _submission_agent(_submission_result(request_id=1))
        service.invoke(
            "indexer.search_resources", {"title": "Safe Resource"}, owner="session-a"
        )
        prepared = service.prepare(
            "ingest.submit",
            {"source_type": "resource_candidates", "positions": [1], "target": "qb"},
            owner="session-a",
        )
        confirmation_id = prepared["action_plan"]["plan_id"]
        with self.assertRaises(AgentToolError) as wrong_owner:
            service.confirm(confirmation_id, owner="session-b")
        self.assertEqual(wrong_owner.exception.code, "confirmation_invalid")
        self.assertEqual(calls, [])
        self.assertEqual(service.recent_download_store.get(owner="session-a"), ())
        service.confirm(confirmation_id, owner="session-a")
        self.assertEqual(len(service.recent_download_store.get(owner="session-a")), 1)
        with self.assertRaises(AgentToolError) as replay:
            service.confirm(confirmation_id, owner="session-a")
        self.assertEqual(replay.exception.code, "confirmation_invalid")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(service.recent_download_store.get(owner="session-a")), 1)

        now = [0.0]
        expiring_store = ConfirmationStore(
            ttl_seconds=5,
            clock=lambda: now[0],
            token_factory=lambda: "confirm-expired-download-status",
        )
        expired_service, expired_calls = _submission_agent(
            _submission_result(request_id=2), confirmation_store=expiring_store
        )
        expired_service.invoke(
            "indexer.search_resources", {"title": "Safe Resource"}, owner="session-a"
        )
        expired = expired_service.prepare(
            "ingest.submit",
            {"source_type": "resource_candidates", "positions": [1], "target": "qb"},
            owner="session-a",
        )
        now[0] = 6.0
        with self.assertRaises(AgentToolError) as expired_error:
            expired_service.confirm(expired["action_plan"]["plan_id"], owner="session-a")
        self.assertEqual(expired_error.exception.code, "confirmation_invalid")
        self.assertEqual(expired_calls, [])
        self.assertEqual(expired_service.recent_download_store.get(owner="session-a"), ())

        context = {"value": "one"}
        stale_service, stale_calls = _submission_agent(
            _submission_result(request_id=3), context=context
        )
        stale_service.invoke(
            "indexer.search_resources", {"title": "Safe Resource"}, owner="session-a"
        )
        stale = stale_service.prepare(
            "ingest.submit",
            {"source_type": "resource_candidates", "positions": [1], "target": "qb"},
            owner="session-a",
        )
        context["value"] = "two"
        with self.assertRaises(AgentToolError) as stale_error:
            stale_service.confirm(stale["action_plan"]["plan_id"], owner="session-a")
        self.assertEqual(stale_error.exception.code, "confirmation_stale")
        self.assertEqual(stale_calls, [])
        self.assertEqual(stale_service.recent_download_store.get(owner="session-a"), ())

    def test_generic_download_status_prefers_recent_session_record(self):
        store = RecentDownloadSubmissionStore()
        request_id = self._create_request("generic", "downloading")
        store.capture(owner="session-a", result=_submission_result(request_id=request_id))
        service, _ = _submission_agent(_submission_result(request_id=request_id), store=store)

        response = service.query("下载完成了吗", owner="session-a")

        self.assertEqual(response["tool_call"]["name"], "downloads.recent_submission_status")
        self.assertEqual(response["result"]["data"]["phase"], "downloading")

    def test_default_is_latest_and_out_of_range_fails_closed(self):
        store = RecentDownloadSubmissionStore()
        first = self._create_request("one", "submitted")
        second = self._create_request("two", "downloading")
        store.capture(owner="session-a", result=_submission_result(request_id=first))
        store.capture(owner="session-a", result=_submission_result(request_id=second))
        service, _ = _submission_agent(_submission_result(request_id=second), store=store)

        latest = service.query("最近下载任务状态", owner="session-a")
        missing = service.query("第 3 个任务状态", owner="session-a")
        self.assertEqual(latest["result"]["data"]["phase"], "downloading")
        self.assertEqual(missing["result"]["status"], "selection_required")
        self.assertEqual(missing["result"]["data"]["available_positions"], [1, 2])

    @staticmethod
    def _create_request(suffix: str, status: str) -> int:
        request_id, _ = db.create_download_request(
            f"recent-status-order-{suffix}", "magnet", origin="agent"
        )
        db.update_download_request(
            request_id, targets="qb", status=status, qb_status=status
        )
        return request_id


class RecentDownloadStatusAPITests(IsolatedDatabaseTestCase):
    def setUp(self):
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.client_a = TestClient(create_app(start_background=False), raise_server_exceptions=False)
        self.client_b = TestClient(create_app(start_background=False), raise_server_exceptions=False)
        self.client_a.__enter__()
        self.client_b.__enter__()

    def tearDown(self):
        self.client_b.__exit__(None, None, None)
        self.client_a.__exit__(None, None, None)
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    @staticmethod
    def _token(html: str) -> str:
        matched = re.search(r'name="csrf_token"\s+(?:value|content)="([^"]+)"', html)
        if not matched:
            matched = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not matched:
            raise AssertionError("CSRF token missing")
        return matched.group(1)

    def _login(self, client: TestClient) -> str:
        token = self._token(client.get("/login").text)
        response = client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)
        return self._token(client.get("/settings").text)

    def test_http_status_is_session_bound_after_confirm(self):
        request_id, _ = db.create_download_request(
            "recent-status-http", "magnet", source_value="magnet:?xt=HTTP-SECRET", origin="agent"
        )
        db.update_download_request(
            request_id, targets="qb", status="submitted", qb_status="submitted"
        )
        service, _ = _submission_agent(_submission_result(request_id=request_id))
        token_a = self._login(self.client_a)
        token_b = self._login(self.client_b)
        headers_a = {"X-CSRF-Token": token_a}
        headers_b = {"X-CSRF-Token": token_b}

        with patch("app.routes.agent_api.get_agent_service", return_value=service):
            # 新查询会按 latest-wins 语义撤销同会话旧确认票据，因此先读取
            # “提交前”状态，再创建并消费本次确认票据。
            before = self.client_a.post(
                "/api/agent/query", headers=headers_a, json={"session_id": "test_session_identifier_0001", "message": "刚才下载到哪了"}
            )
            searched = self.client_a.post(
                "/api/agent/tools/indexer.search_resources",
                headers=headers_a,
                json={
                    "session_id": "test_session_identifier_0001",
                    "arguments": {"title": "Safe Resource"},
                },
            )
            self.assertEqual(searched.status_code, 200, searched.text)

            prepared = self.client_a.post(
                "/api/agent/actions/ingest.submit/prepare",
                headers=headers_a,
                json={"session_id": "test_session_identifier_0001", "arguments": {"source_type": "resource_candidates", "positions": [1], "target": "qb"}},
            )
            confirmed = self.client_a.post(
                "/api/agent/actions/confirm",
                headers=headers_a,
                json={"session_id": "test_session_identifier_0001", "plan_id": prepared.json()["action_plan"]["plan_id"]},
            )
            own = self.client_a.post(
                "/api/agent/query", headers=headers_a, json={"session_id": "test_session_identifier_0001", "message": "刚才下载到哪了"}
            )
            other = self.client_b.post(
                "/api/agent/query", headers=headers_b, json={"session_id": "test_session_identifier_0001", "message": "刚才下载到哪了"}
            )

        self.assertEqual(before.status_code, 200, before.text)
        self.assertEqual(before.json()["result"]["status"], "precondition_failed")
        self.assertEqual(confirmed.status_code, 202, confirmed.text)
        self.assertNotIn("request_id", confirmed.json()["result"]["data"])
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual(own.json()["result"]["data"]["phase"], "submitted")
        self.assertFalse(_contains_key(own.json()["result"]["data"], "request_id"))
        self.assertEqual(other.status_code, 200, other.text)
        self.assertEqual(other.json()["result"]["status"], "precondition_failed")
        self.assertNotIn("HTTP-SECRET", confirmed.text)
        self.assertNotIn("HTTP-SECRET", own.text)

    def test_recent_download_status_has_independent_rate_limit(self):
        request_id, _ = db.create_download_request(
            "recent-status-rate-limit", "magnet", origin="agent"
        )
        db.update_download_request(
            request_id, targets="qb", status="submitted", qb_status="submitted"
        )
        store = RecentDownloadSubmissionStore()
        store.capture(owner="session-a", result=_submission_result(request_id=request_id))
        service, _ = _submission_agent(
            _submission_result(request_id=request_id), store=store
        )
        token = self._login(self.client_a)
        headers = {"X-CSRF-Token": token}
        service.recent_download_store.capture(
            owner=token,
            result=_submission_result(request_id=request_id),
        )
        with patch("app.routes.agent_api.get_agent_service", return_value=service):
            responses = [
                self.client_a.post(
                    "/api/agent/query",
                    headers=headers,
                    json={"session_id": "test_session_identifier_0001", "message": "刚才下载到哪了"},
                )
                for _ in range(13)
            ]
            generic = self.client_a.post(
                "/api/agent/query", headers=headers, json={"session_id": "test_session_identifier_0001", "message": "你好"}
            )
        self.assertTrue(all(item.status_code == 200 for item in responses[:12]))
        self.assertEqual(responses[12].status_code, 429)
        self.assertNotEqual(generic.status_code, 429)


if __name__ == "__main__":
    unittest.main()
