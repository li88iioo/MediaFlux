"""Media Agent 本地系统简报的聚合、路由与 API 安全测试。"""
from __future__ import annotations

import json
import re
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.agent.models import Evidence, RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import AgentOrchestrator, is_workspace_briefing_message
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.agent.workspace_briefing_actions import (
    summarize_workspace_briefing,
    workspace_briefing_arguments,
)
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


def _todo_result(*, status: str = "attention") -> ToolResult:
    areas = [
        {
            "source": "downloads",
            "status": "attention",
            "attention_count": 2,
            "active_count": 1,
            "waiting_count": 0,
            "reason_codes": ["download_needs_review"],
            "next_tool": "downloads.diagnose_queue",
        },
        {
            "source": "rss",
            "status": "waiting",
            "attention_count": 0,
            "active_count": 0,
            "waiting_count": 3,
            "reason_codes": ["rss_pending"],
            "next_tool": "rss.diagnose",
        },
    ]
    if status == "unavailable":
        areas = []
    return ToolResult(
        ok=status != "unavailable",
        status=status,
        summary="workspace",
        data={"areas": areas},
        evidence=[Evidence("workspace", "local", "2026-08-01T10:00:00+08:00")],
    )


def _indexer_result(*, status: str = "ready", attention: int = 0) -> ToolResult:
    return ToolResult(
        ok=status != "unavailable",
        status=status,
        summary="indexer",
        data={
            "counts": {
                "enabled": 4,
                "searchable": 4,
                "downloadable": 3,
                "attention": attention,
            }
        },
    )


class WorkspaceBriefingUnitTests(IsolatedDatabaseTestCase):
    def test_arguments_and_registry_contract_are_strict(self):
        self.assertEqual(workspace_briefing_arguments({}), {})
        with self.assertRaisesRegex(AgentToolError, r"^workspace\.briefing 不接受参数$"):
            workspace_briefing_arguments({"token": "PRIVATE"})

        capabilities = {item["name"]: item for item in build_tool_registry().capabilities()}
        spec = capabilities["workspace.briefing"]
        self.assertEqual(spec["risk"], "read")
        self.assertFalse(spec["requires_confirmation"])
        self.assertFalse(spec["parameters"]["additionalProperties"])
        self.assertEqual(spec["parameters"]["properties"], {})

    def test_aggregates_local_snapshot_without_sensitive_values(self):
        config_values = {
            "JELLYFIN_ENABLED": "1",
            "JELLYFIN_URL": "https://private.internal",
            "JELLYFIN_API_KEY": "PRIVATE-TOKEN",
            "EMBY_ENABLED": "0",
        }
        with patch(
            "app.agent.workspace_briefing_actions.summarize_workspace_todo",
            return_value=_todo_result(),
        ), patch(
            "app.agent.workspace_briefing_actions.diagnose_indexer_readiness",
            return_value=_indexer_result(),
        ), patch(
            "app.agent.workspace_briefing_actions.config.get",
            side_effect=lambda key, default="": config_values.get(key, default),
        ):
            result = summarize_workspace_briefing({})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["probe_mode"], "local_snapshot")
        self.assertFalse(result.data["network_accessed"])
        self.assertFalse(result.data["content_filesystem_scanned"])
        self.assertEqual(result.data["attention_total"], 2)
        self.assertEqual(result.data["active_total"], 1)
        self.assertEqual(result.data["waiting_total"], 3)
        self.assertEqual([item["source"] for item in result.data["areas"]], [
            "downloads", "rss", "indexers", "media_servers",
        ])
        media = result.data["areas"][-1]
        self.assertEqual(media["status"], "ready")
        self.assertEqual(media["ready_count"], 1)
        self.assertEqual(media["connectivity"], "not_probed")
        self.assertEqual(result.data["coverage"]["not_probed"], [
            "media_server_connectivity", "cloud_directory_pending_scan",
        ])
        self.assertEqual(result.suggestions, ["检查下载队列里的异常"])
        self.assertNotIn("downloads.diagnose_queue", " ".join(result.suggestions))

        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("private.internal", serialized)
        self.assertNotIn("PRIVATE-TOKEN", serialized)

    def test_partial_failures_are_not_reported_as_zero_or_healthy(self):
        with patch(
            "app.agent.workspace_briefing_actions.summarize_workspace_todo",
            side_effect=RuntimeError("PRIVATE database error"),
        ), patch(
            "app.agent.workspace_briefing_actions.diagnose_indexer_readiness",
            return_value=_indexer_result(),
        ), patch(
            "app.agent.workspace_briefing_actions.config.get",
            side_effect=RuntimeError("PRIVATE config error"),
        ):
            result = summarize_workspace_briefing({})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertEqual(set(result.data["coverage"]["unavailable"]), {
            "downloads", "rss", "organize", "strm", "local_media",
            "download_verification", "library_patrol", "media_servers",
        })
        self.assertIn("indexers", result.data["coverage"]["available"])
        self.assertNotEqual(result.summary, "系统本地状态未发现待处理事项")
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("PRIVATE", serialized)


    def test_unavailable_todo_result_uses_attempt_evidence(self):
        unavailable_areas = [
            {
                "source": source,
                "status": "unavailable",
                "attention_count": 0,
                "active_count": 0,
                "waiting_count": 0,
                "reason_codes": ["local_snapshot_unavailable"],
                "next_tool": next_tool,
            }
            for source, next_tool in (
                ("downloads", "downloads.diagnose_queue"),
                ("rss", "rss.diagnose"),
                ("organize", "guangya.organize.status"),
                ("strm", "strm.triage_failures"),
                ("local_media", "local_media.diagnose"),
                ("download_verification", "downloads.diagnose_queue"),
                ("library_patrol", "library.patrol_status"),
            )
        ]
        todo = ToolResult(False, "unavailable", "unavailable", data={"areas": unavailable_areas})
        with patch(
            "app.agent.workspace_briefing_actions.summarize_workspace_todo",
            return_value=todo,
        ), patch(
            "app.agent.workspace_briefing_actions.diagnose_indexer_readiness",
            return_value=_indexer_result(),
        ), patch(
            "app.agent.workspace_briefing_actions.config.get",
            return_value="0",
        ):
            result = summarize_workspace_briefing({})

        workspace_evidence = next(
            item for item in result.evidence if item.source == "workspace_local_snapshot"
        )
        self.assertIn("尝试读取", workspace_evidence.description)
        self.assertNotIn("读取下载", workspace_evidence.description)

    def test_disabled_and_not_configured_are_distinct_from_unavailable(self):
        with patch(
            "app.agent.workspace_briefing_actions.summarize_workspace_todo",
            return_value=ToolResult(True, "empty", "empty", data={"areas": [{
                "source": "downloads",
                "status": "idle",
                "attention_count": 0,
                "active_count": 0,
                "waiting_count": 0,
                "reason_codes": [],
                "next_tool": "downloads.diagnose_queue",
            }]}),
        ), patch(
            "app.agent.workspace_briefing_actions.diagnose_indexer_readiness",
            return_value=_indexer_result(status="disabled"),
        ), patch(
            "app.agent.workspace_briefing_actions.config.get",
            return_value="0",
        ):
            result = summarize_workspace_briefing({})

        self.assertEqual(result.status, "healthy")
        self.assertEqual(result.data["coverage"]["disabled"], ["indexers"])
        self.assertEqual(result.data["coverage"]["not_configured"], ["media_servers"])
        self.assertEqual(result.data["coverage"]["unavailable"], [])


class WorkspaceBriefingRoutingTests(IsolatedDatabaseTestCase):
    def _agent(self) -> AgentOrchestrator:
        registry = ToolRegistry()
        for name in (
            "workspace.briefing",
            "workspace.todo",
            "downloads.diagnose_queue",
            "rss.diagnose",
            "local_media.diagnose",
            "automation.diagnose_pipeline",
            "config.diagnose",
            "strm.diagnose",
            "library.search",
        ):
            registry.register(ToolSpec(
                name=name,
                description=name,
                risk=RiskLevel.READ,
                parameters={},
                handler=lambda arguments, tool=name: ToolResult(True, "success", tool, data=dict(arguments)),
                validator=lambda arguments: dict(arguments),
            ))
        return AgentOrchestrator(registry)

    def test_high_confidence_briefing_intents_route_even_with_domain_names(self):
        agent = self._agent()
        for message in (
            "给我每日简报",
            "查看系统简报",
            "今天的系统简报，包含 RSS 和下载",
            "工作区简报",
            "查看全局状态概览",
        ):
            with self.subTest(message=message):
                self.assertTrue(is_workspace_briefing_message(message))
                self.assertEqual(agent.query(message)["tool_call"]["name"], "workspace.briefing")

    def test_write_intents_and_specialized_diagnostics_are_not_stolen(self):
        agent = self._agent()
        for message in ("刷新系统简报", "系统简报里执行一次 STRM"):
            with self.subTest(message=message):
                self.assertFalse(is_workspace_briefing_message(message))
                response = agent.query(message)
                if response.get("tool_call"):
                    self.assertNotEqual(response["tool_call"]["name"], "workspace.briefing")

        cases = (
            ("诊断下载任务", "downloads.diagnose_queue"),
            ("系统简报，诊断下载任务", "downloads.diagnose_queue"),
            ("查看 RSS 待处理", "rss.diagnose"),
            ("系统简报，查看 RSS 待处理", "rss.diagnose"),
            ("系统简报，检查本地媒体状态", "local_media.diagnose"),
            ("诊断自动化链路", "automation.diagnose_pipeline"),
            ("检查项目配置", "config.diagnose"),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(agent.query(message)["tool_call"]["name"], expected)


class WorkspaceBriefingAPITests(IsolatedDatabaseTestCase):
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

    def test_auth_csrf_and_direct_then_query_shared_rate_limit(self):
        path = "/api/agent/tools/workspace.briefing"
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code, 401)
        csrf = self._login()
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code, 403)
        headers = {"X-CSRF-Token": csrf}
        for _ in range(4):
            response = self.client.post(path, headers=headers, json={"session_id": "test_session_identifier_0001", "arguments": {}})
            self.assertEqual(response.status_code, 200, response.text)
        limited = self.client.post(
            "/api/agent/query",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "message": "查看系统简报"},
        )
        self.assertEqual(limited.status_code, 429, limited.text)

    def test_invalid_arguments_fail_before_reading_sources(self):
        csrf = self._login()
        with patch(
            "app.agent.workspace_briefing_actions.summarize_workspace_todo"
        ) as todo, patch(
            "app.agent.workspace_briefing_actions.diagnose_indexer_readiness"
        ) as indexers:
            response = self.client.post(
                "/api/agent/tools/workspace.briefing",
                headers={"X-CSRF-Token": csrf},
                json={"session_id": "test_session_identifier_0001", "arguments": {"token": "PRIVATE"}},
            )
        self.assertEqual(response.status_code, 400, response.text)
        todo.assert_not_called()
        indexers.assert_not_called()


if __name__ == "__main__":
    import unittest
    unittest.main()
