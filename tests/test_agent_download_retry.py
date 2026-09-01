"""Media Agent 下载待处理请求重投测试。"""
from __future__ import annotations

import json
import unittest
import uuid
from unittest.mock import patch

from app import database as db
from app.agent.download_retry_actions import download_retry_submission_arguments
from app.agent.orchestrator import (
    AgentOrchestrator,
    download_retry_submission_request,
    is_download_retry_submission_message,
)
from app.agent.registry import AgentToolError
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from tests.support import IsolatedDatabaseTestCase


_CAPABILITIES = {
    "qb": {"enabled": True, "reason": ""},
    "guangya": {"enabled": True, "reason": ""},
    "both": {"enabled": True, "reason": ""},
}


class DownloadRetryTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        reset_agent_service_for_tests()

    def tearDown(self) -> None:
        reset_agent_service_for_tests()

    @staticmethod
    def _create_attention_request() -> int:
        token = uuid.uuid4().hex
        request_id, created = db.create_download_request(
            f"agent-retry-secret-key-{token}",
            "magnet",
            title=f"Private.Show.{token}",
            source_value=f"magnet:?xt=urn:btih:{token[:40]}",
            chat_id="private-chat",
            user_id="private-user",
            message_id="private-message",
            origin="agent",
        )
        assert created
        db.update_download_request(
            request_id,
            status="manual_review",
            qb_status="failed",
            gy_status="failed",
            error="private backend error",
        )
        return int(request_id)

    def test_arguments_and_natural_language_are_strict(self):
        self.assertEqual(
            download_retry_submission_arguments({"request_id": 12, "target": "QB"}),
            {"request_id": 12, "target": "qb"},
        )
        for invalid in (
            {},
            {"request_id": True, "target": "qb"},
            {"request_id": 0, "target": "qb"},
            {"request_id": 1, "target": "unknown"},
            {"request_id": 1, "target": "qb", "source": "secret"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(AgentToolError):
                download_retry_submission_arguments(invalid)

        self.assertEqual(
            download_retry_submission_request("重试下载请求 12 到 qB"),
            {"request_id": 12, "target": "qb"},
        )
        self.assertEqual(
            download_retry_submission_request("把下载请求 #12 重新提交到光鸭"),
            {"request_id": 12, "target": "guangya"},
        )
        self.assertEqual(
            download_retry_submission_request("重新提交下载待处理记录 12 到 qBittorrent 与光鸭"),
            {"request_id": 12, "target": "both"},
        )
        self.assertIsNone(download_retry_submission_request("重试下载请求 12"))
        self.assertTrue(is_download_retry_submission_message("重试下载请求 12"))

    def test_registry_is_dangerous_and_confirmation_gated(self):
        registry = get_agent_service().registry
        capabilities = {item["name"]: item for item in registry.capabilities()}
        tool = capabilities["downloads.retry_submission"]
        self.assertEqual(tool["risk"], "danger")
        self.assertTrue(tool["requires_confirmation"])
        with self.assertRaises(AgentToolError) as caught:
            registry.execute(
                "downloads.retry_submission", {"request_id": 1, "target": "qb"}
            )
        self.assertEqual(caught.exception.code, "confirmation_required")

    def test_prepare_does_not_dispatch_and_does_not_leak_secrets(self):
        request_id = self._create_attention_request()
        with (
            patch(
                "app.agent.download_retry_actions.download_resubmit_capabilities",
                return_value=_CAPABILITIES,
            ),
            patch("app.agent.download_retry_actions.resubmit_download_request") as dispatch,
        ):
            prepared = get_agent_service().prepare(
                "downloads.retry_submission",
                {"request_id": request_id, "target": "qb"},
                owner="owner",
            )
        self.assertEqual(prepared["mode"], "confirmation_required")
        dispatch.assert_not_called()
        serialized = json.dumps(prepared, ensure_ascii=False)
        for secret in (
            "agent-retry-secret-key",
            "Private.Show.",
            "magnet:?xt=urn:btih",
            "private-chat",
            "private-user",
            "private backend error",
        ):
            self.assertNotIn(secret, serialized)

    def test_confirm_dispatches_once_and_projects_counts_only(self):
        request_id = self._create_attention_request()
        result = {
            "ok": True,
            "status": "submitted",
            "created": True,
            "duplicate": False,
            "succeeded": ["qb"],
            "failed": [],
            "source_attention_preserved": False,
            "request_id": 999,
            "source_request_id": request_id,
            "results": {"qb": {"task_id": "private-task-id"}},
        }
        with (
            patch(
                "app.agent.download_retry_actions.download_resubmit_capabilities",
                return_value=_CAPABILITIES,
            ),
            patch(
                "app.agent.download_retry_actions.resubmit_download_request",
                return_value=result,
            ) as dispatch,
        ):
            service = get_agent_service()
            prepared = service.prepare(
                "downloads.retry_submission",
                {"request_id": request_id, "target": "qb"},
                owner="owner",
            )
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertTrue(confirmed["result"]["ok"])
        self.assertEqual(confirmed["result"]["status"], "completed")
        self.assertEqual(confirmed["result"]["data"]["succeeded"], 1)
        self.assertEqual(confirmed["result"]["data"]["failed"], 0)
        dispatch.assert_called_once_with(request_id, "qb")
        serialized = json.dumps(confirmed, ensure_ascii=False)
        self.assertNotIn("private-task-id", serialized)
        self.assertNotIn('"request_id": 999', serialized)

    def test_state_change_invalidates_confirmation_without_dispatch(self):
        request_id = self._create_attention_request()
        with (
            patch(
                "app.agent.download_retry_actions.download_resubmit_capabilities",
                return_value=_CAPABILITIES,
            ),
            patch("app.agent.download_retry_actions.resubmit_download_request") as dispatch,
        ):
            service = get_agent_service()
            prepared = service.prepare(
                "downloads.retry_submission",
                {"request_id": request_id, "target": "guangya"},
                owner="owner",
            )
            db.update_download_request(request_id, title="Changed title")
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertFalse(confirmed["result"]["ok"])
        self.assertEqual(confirmed["result"]["status"], "conflict")
        dispatch.assert_not_called()

    def test_partial_and_failure_keep_safe_attention_result(self):
        request_id = self._create_attention_request()
        partial_result = {
            "ok": True,
            "status": "submitted",
            "created": True,
            "succeeded": ["qb"],
            "failed": ["guangya"],
            "source_attention_preserved": False,
        }
        with (
            patch(
                "app.agent.download_retry_actions.download_resubmit_capabilities",
                return_value=_CAPABILITIES,
            ),
            patch(
                "app.agent.download_retry_actions.resubmit_download_request",
                return_value=partial_result,
            ),
        ):
            service = get_agent_service()
            prepared = service.prepare(
                "downloads.retry_submission",
                {"request_id": request_id, "target": "both"},
                owner="owner",
            )
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertTrue(confirmed["result"]["ok"])
        self.assertEqual(confirmed["result"]["status"], "partial")
        self.assertEqual(confirmed["result"]["data"]["failed"], 1)

    def test_orchestrator_requires_explicit_target_or_prepares_action(self):
        agent = AgentOrchestrator(get_agent_service().registry)
        vague = agent.query("重试下载请求 12", owner="owner")
        self.assertEqual(vague["mode"], "clarification")
        self.assertIn("编号", vague["result"]["summary"])

        request_id = self._create_attention_request()
        with patch(
            "app.agent.download_retry_actions.download_resubmit_capabilities",
            return_value=_CAPABILITIES,
        ):
            prepared = agent.query(
                f"把下载请求 {request_id} 重新提交到 qBittorrent", owner="owner"
            )
        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(prepared["tool_call"]["name"], "downloads.retry_submission")


if __name__ == "__main__":
    unittest.main()
