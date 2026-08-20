"""自动化链路 Agent 诊断的状态、脱敏与 API 契约。"""
from __future__ import annotations

import json
import re
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.agent.automation_actions import (
    automation_pipeline_arguments,
    diagnose_automation_pipeline,
)
from app.agent.models import ToolContext
from app.agent.orchestrator import (
    AgentOrchestrator,
    is_automation_pipeline_diagnosis_message,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


def _aggregate(**overrides):
    value = {
        "downloads_active": 0,
        "downloads_review": 0,
        "rss_subscriptions": 0,
        "rss_pending": 0,
        "rss_failed": 0,
        "organize_issues": 0,
        "strm_failures": 0,
        "strm_last_status": "",
        "strm_last_at": "",
    }
    value.update(overrides)
    return value


class AutomationPipelineUnitTests(IsolatedDatabaseTestCase):
    def test_arguments_reject_every_extra_field(self):
        self.assertEqual(automation_pipeline_arguments({}), {})
        with self.assertRaisesRegex(
            AgentToolError,
            r"^automation\.diagnose_pipeline 不接受参数$",
        ):
            automation_pipeline_arguments({"token": "PIPELINE_SECRET"})

    def test_empty_local_database_is_explicitly_not_configured(self):
        result = diagnose_automation_pipeline({})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "not_configured")
        self.assertEqual(result.data["probe_mode"], "local")
        self.assertFalse(result.data["network_accessed"])
        self.assertEqual(result.data["attention"], {"total": 0, "blockers": []})
        self.assertEqual(result.data["stages"]["rss"]["status"], "not_configured")
        self.assertEqual(result.data["stages"]["strm"]["last_run"], "not_observed")

    def test_active_work_without_issues_is_healthy(self):
        snapshot = _aggregate(
            downloads_active=3,
            rss_subscriptions=2,
            rss_pending=7,
            strm_last_status="completed",
        )
        with patch(
            "app.agent.automation_actions.db.get_dashboard_automation_summary",
            return_value=snapshot,
        ):
            result = diagnose_automation_pipeline({})

        self.assertEqual(result.status, "healthy")
        stages = result.data["stages"]
        self.assertEqual(stages["downloads"], {
            "status": "active", "active": 3, "needs_review": 0,
        })
        self.assertEqual(stages["rss"]["status"], "active")
        self.assertEqual(stages["guangya_organize"]["status"], "healthy")
        self.assertEqual(stages["strm"], {
            "status": "healthy", "open_failures": 0, "last_run": "completed",
        })

    def test_attention_is_bounded_to_counts_and_fixed_machine_codes(self):
        snapshot = _aggregate(
            downloads_active=1,
            downloads_review=2,
            rss_subscriptions=1,
            rss_failed=3,
            organize_issues=4,
            strm_failures=5,
            strm_last_status="failed",
        )
        with patch(
            "app.agent.automation_actions.db.get_dashboard_automation_summary",
            return_value=snapshot,
        ):
            result = diagnose_automation_pipeline({})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["attention"]["total"], 14)
        self.assertEqual(result.data["attention"]["blockers"], [
            {"code": "downloads_need_review", "stage": "downloads", "count": 2},
            {"code": "rss_failed_entries", "stage": "rss", "count": 3},
            {"code": "organize_historical_issues", "stage": "guangya_organize", "count": 4},
            {"code": "strm_open_failures", "stage": "strm", "count": 5},
        ])
        self.assertEqual(result.data["stages"]["strm"]["last_run"], "failed")

    def test_failed_last_strm_run_without_open_failure_marks_pipeline_attention(self):
        snapshot = _aggregate(rss_subscriptions=1, strm_last_status="failed")
        with patch(
            "app.agent.automation_actions.db.get_dashboard_automation_summary",
            return_value=snapshot,
        ):
            result = diagnose_automation_pipeline({})

        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["attention"], {
            "total": 1,
            "blockers": [
                {"code": "strm_last_run_failed", "stage": "strm", "count": 1},
            ],
        })
        self.assertEqual(result.data["stages"]["strm"]["status"], "attention")

    def test_malformed_values_are_sanitized_and_sensitive_fields_are_not_projected(self):
        snapshot = _aggregate(
            downloads_active=-8,
            downloads_review="not-a-count",
            rss_subscriptions="2",
            rss_pending=True,
            strm_last_status="TOKEN_9f /srv/private 192.0.2.10",
            token="TOKEN_9f",
            path="/srv/private",
            url="http://192.0.2.10",
            host="private-host",
            hash="HASH_SECRET",
            guid="GUID_SECRET",
            id="ID_SECRET",
            title="TITLE_SECRET",
        )
        with patch(
            "app.agent.automation_actions.db.get_dashboard_automation_summary",
            return_value=snapshot,
        ):
            result = diagnose_automation_pipeline({})

        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("TOKEN_9f", serialized)
        self.assertNotIn("/srv/private", serialized)
        self.assertNotIn("192.0.2.10", serialized)
        self.assertNotIn('"token"', serialized)
        self.assertNotIn('"path"', serialized)
        self.assertNotIn('"url"', serialized)
        for secret in ("private-host", "HASH_SECRET", "GUID_SECRET", "ID_SECRET", "TITLE_SECRET"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(result.data["stages"]["strm"]["last_run"], "unknown")

    def test_database_exception_returns_fixed_sanitized_error(self):
        with patch(
            "app.agent.automation_actions.db.get_dashboard_automation_summary",
            side_effect=RuntimeError("PIPELINE_SECRET /srv/hidden"),
        ):
            result = diagnose_automation_pipeline({})

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")
        self.assertFalse(result.data["network_accessed"])
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("PIPELINE_SECRET", serialized)
        self.assertNotIn("/srv/hidden", serialized)

    def test_registry_and_natural_language_route_are_read_only_and_explicit(self):
        capabilities = {item["name"]: item for item in get_agent_service().capabilities()["tools"]}
        spec = capabilities["automation.diagnose_pipeline"]
        self.assertEqual(spec["risk"], "read")
        self.assertFalse(spec["requires_confirmation"])
        self.assertFalse(spec["parameters"]["additionalProperties"])

        for message in (
            "诊断自动化链路",
            "检查媒体自动化是否正常",
            "查看自动化流程状态",
            "自动化任务有异常吗",
        ):
            self.assertTrue(is_automation_pipeline_diagnosis_message(message), message)
        for message in (
            "配置自动化链路",
            "立即运行自动化流程",
            "检查 RSS 订阅",
            "诊断下载队列",
            "检查项目配置",
        ):
            self.assertFalse(is_automation_pipeline_diagnosis_message(message), message)

        registry = Mock()
        registry.execute.return_value = (diagnose_automation_pipeline({}), 1)
        agent = AgentOrchestrator(registry)
        response = agent.query("诊断自动化链路")
        self.assertEqual(response["tool_call"]["name"], "automation.diagnose_pipeline")
        registry.execute.assert_called_once_with(
            "automation.diagnose_pipeline", {}, context=ToolContext(owner="")
        )

        for message, expected_tool in (
            ("查看 STRM 自动化流程状态", "strm.status"),
            ("查看光鸭整理自动化流程状态", "guangya.organize.status"),
            ("检查 RSS 自动化流程状态", "rss.diagnose"),
            ("检查下载自动化流程状态", "downloads.diagnose_queue"),
            ("查看下载自动化任务状态", "downloads.diagnose_queue"),
            ("诊断下载自动化链路", "downloads.diagnose_queue"),
        ):
            registry.reset_mock()
            response = agent.query(message)
            self.assertEqual(response["tool_call"]["name"], expected_tool, message)
            registry.execute.assert_called_once_with(
                expected_tool, {}, context=ToolContext(owner="")
            )


class AutomationPipelineAPITests(IsolatedDatabaseTestCase):
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

    def test_api_auth_csrf_and_shared_direct_query_rate_limit(self):
        path = "/api/agent/tools/automation.diagnose_pipeline"
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code, 401)
        csrf = self._login()
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code, 403)
        headers = {"X-CSRF-Token": csrf}

        for _ in range(4):
            response = self.client.post(path, headers=headers, json={"session_id": "test_session_identifier_0001", "arguments": {}})
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()["result"]
            self.assertEqual(result["data"]["probe_mode"], "local")
            self.assertFalse(result["data"]["network_accessed"])
        limited = self.client.post(
            "/api/agent/query",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "message": "诊断自动化链路"},
        )
        self.assertEqual(limited.status_code, 429, limited.text)


if __name__ == "__main__":
    import unittest
    unittest.main()
