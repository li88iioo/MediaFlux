"""最近缺集下载完成后的媒体库核验续接测试。"""
from __future__ import annotations

import re
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database as db
from app.agent.confirmation import ConfirmationStore
from app.agent.models import Evidence, RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import (
    AgentOrchestrator,
    is_recent_download_library_verification_message,
    recent_download_library_verification_request,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.recent_download_submissions import (
    RecentDownloadSubmission,
    RecentDownloadSubmissionStore,
    RecentDownloadVerification,
    build_recent_download_library_verification,
)
from app.agent.registry import ToolRegistry
from app.agent.service import reset_agent_service_for_tests
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


_VERIFICATION_CONTEXT = {
    "title": "The Show",
    "tmdb_id": "12345",
    "season": 2,
    "episode": 3,
    "as_of": "2026-08-03",
    "library_name": "动漫库",
}


def _submission_result(request_id: int | None) -> ToolResult:
    data = {
        "result_id": "safe-result-00000001",
        "created": True,
        "target": "qb",
        "status": "submitted",
        "succeeded": ["qb"],
        "failed": [],
        "duplicate": False,
    }
    if request_id is not None:
        data["request_id"] = request_id
    return ToolResult(True, "accepted", "submitted", data=data)


def _record(*, verification: bool = True) -> RecentDownloadSubmission:
    return RecentDownloadSubmission(
        request_id=1,
        target="qb",
        dispatch_status="submitted",
        succeeded=("qb",),
        failed=(),
        created=True,
        duplicate=False,
        result_status="accepted",
        captured_at="2026-08-03T12:00:00+08:00",
        verification=(
            RecentDownloadVerification(**_VERIFICATION_CONTEXT)
            if verification else None
        ),
    )


def _service(
    *,
    store: RecentDownloadSubmissionStore | None = None,
    audit_result: ToolResult | None = None,
):
    audit_calls: list[dict] = []
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="library.audit_episodes",
        description="audit",
        risk=RiskLevel.READ,
        parameters={},
        validator=lambda arguments: {
            "query": str(arguments.get("query") or ""),
            "tmdb_id": str(arguments.get("tmdb_id") or ""),
            "season": arguments.get("season"),
            "target_episode": arguments.get("target_episode"),
            "as_of": str(arguments.get("as_of") or ""),
            "library_name": str(arguments.get("library_name") or ""),
        },
        handler=lambda arguments: (
            audit_calls.append(dict(arguments))
            or audit_result
            or ToolResult(True, "up_to_date", "audit", data={"missing_count": 0})
        ),
    ))
    return AgentOrchestrator(
        registry,
        ConfirmationStore(token_factory=lambda: "confirm-library-verify-0001"),
        recent_download_store=store,
    ), audit_calls


class RecentDownloadLibraryVerificationIntentTests(unittest.TestCase):
    def test_parser_accepts_recent_and_ordinal_verification_queries(self):
        self.assertEqual(
            recent_download_library_verification_request("核验刚才下载的缺集是否已补齐"),
            {"position": None},
        )
        self.assertEqual(
            recent_download_library_verification_request("检查刚才下载的第 2 个任务是否已入库"),
            {"position": 2},
        )
        self.assertTrue(is_recent_download_library_verification_message(
            "刚才下载的第 1 个任务补齐了吗"
        ))

    def test_parser_rejects_status_explanation_domain_and_write_requests(self):
        for message in (
            "刚才下载到哪了",
            "刚才下载为什么失败",
            "帮我补齐刚才下载的缺集",
            "暂停刚才下载的任务并检查是否入库",
            "检查 qB 下载状态",
            "最近 RSS 失败任务是否补齐",
        ):
            self.assertIsNone(recent_download_library_verification_request(message), message)


class RecentDownloadLibraryVerificationProjectionTests(unittest.TestCase):
    def test_missing_context_fails_closed(self):
        result = build_recent_download_library_verification(
            _record(verification=False),
            ToolResult(True, "up_to_date", "audit"),
            position=1,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "precondition_failed")
        self.assertEqual(result.data, {})

    def test_up_to_date_marks_target_visible(self):
        evidence = Evidence("Jellyfin", "已检查剧集库存", "2026-08-03T12:00:00+08:00")
        result = build_recent_download_library_verification(
            _record(),
            ToolResult(
                True,
                "up_to_date",
                "audit",
                data={"missing_count": 0, "private_path": "/secret"},
                evidence=[evidence],
            ),
            position=1,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "up_to_date")
        self.assertEqual(result.data["verification"], "visible")
        self.assertEqual(result.data["missing_count"], 0)
        self.assertNotIn("private_path", result.data)
        self.assertEqual(result.evidence, [evidence])

    def test_up_to_date_with_exact_target_requires_local_visibility(self):
        inconclusive = build_recent_download_library_verification(
            _record(),
            ToolResult(True, "up_to_date", "audit", data={
                "missing_count": 0,
                "target_aired": False,
                "target_local": False,
                "target_missing": False,
            }),
            position=1,
        )
        self.assertFalse(inconclusive.ok)
        self.assertEqual(inconclusive.status, "inconclusive")
        self.assertEqual(inconclusive.data["verification"], "inconclusive")

        visible = build_recent_download_library_verification(
            _record(),
            ToolResult(True, "up_to_date", "audit", data={
                "missing_count": 0,
                "target_aired": True,
                "target_local": True,
                "target_missing": False,
            }),
            position=1,
        )
        self.assertTrue(visible.ok)
        self.assertEqual(visible.status, "up_to_date")
        self.assertEqual(visible.data["verification"], "visible")

    def test_missing_complete_sample_and_truncated_sample_are_distinguished(self):
        missing = build_recent_download_library_verification(
            _record(),
            ToolResult(True, "updates_available", "audit", data={
                "missing_count": 2,
                "missing_sample": [{"season": 2, "episode": 3}],
                "missing_sample_truncated": False,
            }),
            position=1,
        )
        self.assertEqual(missing.status, "updates_available")
        self.assertEqual(missing.data["verification"], "missing")

        visible = build_recent_download_library_verification(
            _record(),
            ToolResult(True, "updates_available", "audit", data={
                "missing_count": 1,
                "missing_sample": [{"season": 2, "episode": 4}],
                "missing_sample_truncated": False,
            }),
            position=1,
        )
        self.assertEqual(visible.status, "up_to_date")
        self.assertEqual(visible.data["verification"], "visible")

        inconclusive = build_recent_download_library_verification(
            _record(),
            ToolResult(True, "updates_available", "audit", data={
                "missing_count": 20,
                "missing_sample": [{"season": 2, "episode": 4}],
                "missing_sample_truncated": True,
            }),
            position=1,
        )
        self.assertFalse(inconclusive.ok)
        self.assertEqual(inconclusive.status, "inconclusive")
        self.assertEqual(inconclusive.data["verification"], "inconclusive")

    def test_exact_target_projection_wins_when_missing_sample_is_truncated(self):
        missing = build_recent_download_library_verification(
            _record(),
            ToolResult(True, "updates_available", "audit", data={
                "missing_count": 180,
                "missing_sample": [{"season": 2, "episode": 1}],
                "missing_sample_truncated": True,
                "target_aired": True,
                "target_local": False,
                "target_missing": True,
            }),
            position=1,
        )
        self.assertEqual(missing.data["verification"], "missing")
        self.assertEqual(missing.data["library_name"], "动漫库")

        visible = build_recent_download_library_verification(
            _record(),
            ToolResult(True, "updates_available", "audit", data={
                "missing_count": 179,
                "missing_sample": [{"season": 2, "episode": 1}],
                "missing_sample_truncated": True,
                "target_aired": True,
                "target_local": True,
                "target_missing": False,
            }),
            position=1,
        )
        self.assertEqual(visible.data["verification"], "visible")
        self.assertEqual(visible.status, "up_to_date")


class RecentDownloadLibraryVerificationOrchestratorTests(IsolatedDatabaseTestCase):
    def _request(self, suffix: str) -> int:
        request_id, _ = db.create_download_request(
            f"recent-library-verification-{suffix}", "magnet", origin="agent"
        )
        db.update_download_request(
            request_id,
            targets="qb",
            status="submitted",
            qb_status="submitted",
        )
        return request_id

    def _captured_service(self, request_id: int, *, verification: bool = True, audit=None):
        store = RecentDownloadSubmissionStore()
        store.capture(
            owner="session-a",
            result=_submission_result(request_id),
            verification_context=_VERIFICATION_CONTEXT if verification else None,
        )
        service, calls = _service(store=store, audit_result=audit)
        return service, calls

    def test_only_completed_task_reaudits_with_exact_safe_arguments(self):
        request_id = self._request("completed")
        db.update_download_request(
            request_id,
            status="completed",
            qb_status="completed",
            local_import_status="completed",
            completed_at=db.now(),
        )
        audit = ToolResult(True, "up_to_date", "audit", data={"missing_count": 0})
        service, calls = self._captured_service(request_id, audit=audit)
        expected = {
            "query": "The Show",
            "tmdb_id": "12345",
            "season": 2,
            "target_episode": 3,
            "as_of": "2026-08-03",
            "library_name": "动漫库",
        }
        with patch("app.agent.orchestrator.invalidate_episode_audit_cache") as invalidate:
            response = service.query("核验刚才下载的缺集是否已补齐", owner="session-a")
        self.assertEqual(response["tool_call"]["name"], "downloads.verify_recent_submission_library")
        self.assertEqual(response["result"]["status"], "up_to_date")
        self.assertEqual(response["result"]["data"]["verification"], "visible")
        self.assertEqual(calls, [expected])
        invalidate.assert_called_once_with(expected)

    def test_active_failed_unverified_and_cross_owner_records_fail_closed(self):
        request_id = self._request("gated")
        service, calls = self._captured_service(request_id)
        with patch("app.agent.orchestrator.invalidate_episode_audit_cache") as invalidate:
            active = service.query("刚才下载的缺集入库了吗", owner="session-a")
        self.assertEqual(active["result"]["status"], "in_progress")
        self.assertEqual(active["result"]["data"]["phase"], "submitted")
        self.assertEqual(calls, [])
        invalidate.assert_not_called()

        db.update_download_request(request_id, status="failed", qb_status="failed")
        failed = service.query("刚才下载的缺集入库了吗", owner="session-a")
        self.assertEqual(failed["result"]["status"], "precondition_failed")
        self.assertEqual(calls, [])

        unverified, unverified_calls = self._captured_service(request_id, verification=False)
        missing_context = unverified.query("刚才下载的缺集入库了吗", owner="session-a")
        self.assertEqual(missing_context["result"]["status"], "precondition_failed")
        self.assertEqual(unverified_calls, [])

        other = service.query("刚才下载的缺集入库了吗", owner="session-b")
        self.assertEqual(other["result"]["status"], "precondition_failed")


class RecentDownloadLibraryVerificationAPITests(IsolatedDatabaseTestCase):
    def setUp(self):
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.client = TestClient(create_app(start_background=False), raise_server_exceptions=False)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
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

    def _login(self) -> str:
        token = self._token(self.client.get("/login").text)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)
        return self._token(self.client.get("/settings").text)

    def test_verification_scope_is_independent_and_wins_over_status_overlap(self):
        service, _ = _service(store=RecentDownloadSubmissionStore())
        token = self._login()
        headers = {"X-CSRF-Token": token}
        with patch("app.routes.agent_api.get_agent_service", return_value=service):
            responses = [
                self.client.post(
                    "/api/agent/query",
                    headers=headers,
                    json={"session_id": "test_session_identifier_0001", "message": "刚才下载的缺集入库完成了吗"},
                )
                for _ in range(7)
            ]
            status = self.client.post(
                "/api/agent/query", headers=headers, json={"session_id": "test_session_identifier_0001", "message": "刚才下载到哪了"}
            )
            explanation = self.client.post(
                "/api/agent/query", headers=headers, json={"session_id": "test_session_identifier_0001", "message": "刚才下载为什么失败"}
            )
            generic = self.client.post(
                "/api/agent/query", headers=headers, json={"session_id": "test_session_identifier_0001", "message": "你好"}
            )
        self.assertTrue(all(item.status_code == 200 for item in responses[:6]))
        self.assertEqual(responses[6].status_code, 429)
        self.assertNotEqual(status.status_code, 429)
        self.assertNotEqual(explanation.status_code, 429)
        self.assertNotEqual(generic.status_code, 429)


if __name__ == "__main__":
    unittest.main()
