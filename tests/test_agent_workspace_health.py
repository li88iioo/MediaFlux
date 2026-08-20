"""Media Agent 媒体系统健康总检的聚合、路由与 API 安全测试。"""
from __future__ import annotations

import json
import re
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.agent.media_health_actions import diagnose_workspace_health, workspace_health_arguments
from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import AgentOrchestrator, is_workspace_health_message
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


def _workspace_result(*, status: str = "healthy", attention: int = 0) -> ToolResult:
    return ToolResult(
        ok=status != "unavailable",
        status=status,
        summary="workspace private summary",
        data={
            "attention_total": attention,
            "active_total": 2,
            "waiting_total": 3,
            "coverage": {"unavailable": ["rss"] if status == "partial" else []},
            "secret": "PRIVATE-WORKSPACE",
        },
        error="PRIVATE-WORKSPACE-ERROR",
    )


def _config_result(*, errors: int = 0, warnings: int = 0) -> ToolResult:
    status = "healthy" if not errors and not warnings else ("attention" if errors else "degraded")
    return ToolResult(
        ok=errors == 0,
        status=status,
        summary="config private summary",
        data={
            "counts": {"errors": errors, "warnings": warnings},
            "components": [
                {"name": "jellyfin", "status": "ready"},
                {"name": "tmdb", "status": "not_configured" if warnings else "ready"},
            ],
            "issues": [{"message": "PRIVATE-CONFIG"}],
        },
        error="PRIVATE-CONFIG-ERROR",
    )


def _media_result(
    *, status: str = "healthy", enabled: int = 1, configured: int = 1,
    online: int = 1, compatible: int = 1, attention: int = 0,
    network_accessed: bool = True, nodes: list[dict] | None = None,
) -> ToolResult:
    if nodes is None:
        nodes = [{"slot": "jellyfin", "secret": "PRIVATE-NODE"}]
    return ToolResult(
        ok=status not in {"unavailable"},
        status=status,
        summary="media private summary",
        data={
            "network_accessed": network_accessed,
            "counts": {
                "enabled": enabled,
                "configured": configured,
                "online": online,
                "compatible": compatible,
                "attention": attention,
            },
            "nodes": nodes,
            "url": "https://private.internal",
        },
        error="PRIVATE-MEDIA-ERROR",
    )


class WorkspaceHealthUnitTests(IsolatedDatabaseTestCase):
    def test_arguments_and_registry_contract_are_strict(self):
        self.assertEqual(workspace_health_arguments({}), {})
        with self.assertRaisesRegex(AgentToolError, r"^workspace\.health 不接受参数$"):
            workspace_health_arguments({"debug": True})

        capabilities = {item["name"]: item for item in build_tool_registry().capabilities()}
        spec = capabilities["workspace.health"]
        self.assertEqual(spec["risk"], "read")
        self.assertFalse(spec["requires_confirmation"])
        self.assertEqual(spec["parameters"]["properties"], {})
        self.assertFalse(spec["parameters"]["additionalProperties"])

    def test_aggregates_fixed_safe_schema_and_real_network_flag(self):
        with patch(
            "app.agent.media_health_actions.summarize_workspace_briefing",
            return_value=_workspace_result(),
        ), patch(
            "app.agent.media_health_actions.diagnose_config",
            return_value=_config_result(),
        ), patch(
            "app.agent.media_health_actions.diagnose_media_servers",
            return_value=_media_result(),
        ):
            result = diagnose_workspace_health({})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "healthy")
        self.assertEqual(result.data["probe_mode"], "local_and_media_endpoints")
        self.assertTrue(result.data["network_accessed"])
        self.assertFalse(result.data["content_filesystem_scanned"])
        self.assertEqual(result.data["attention_total"], 0)
        self.assertEqual(result.data["coverage"]["requested"], [
            "workspace", "configuration", "media_servers",
        ])
        self.assertEqual(result.data["coverage"]["available"], [
            "workspace", "configuration", "media_servers",
        ])
        self.assertEqual(result.data["coverage"]["unavailable"], [])
        self.assertEqual(result.data["coverage"]["not_probed"], [
            "cloud_directory_content_scan",
            "per_title_episode_audit",
            "per_title_update_check",
            "indexer_network_search",
            "download_submission",
        ])
        self.assertEqual([item["source"] for item in result.data["areas"]], [
            "workspace", "configuration", "media_servers",
        ])
        self.assertEqual(result.data["areas"][0]["active_count"], 2)
        self.assertEqual(result.data["areas"][1]["ready_component_count"], 2)
        self.assertEqual(result.data["areas"][2]["online_count"], 1)

        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for secret in (
            "PRIVATE-WORKSPACE", "PRIVATE-CONFIG", "PRIVATE-NODE", "private.internal",
            "private summary", "PRIVATE-MEDIA-ERROR",
        ):
            self.assertNotIn(secret, serialized)

    def test_partial_child_failure_is_explicit_and_other_areas_survive(self):
        with patch(
            "app.agent.media_health_actions.summarize_workspace_briefing",
            side_effect=RuntimeError("PRIVATE database failure"),
        ), patch(
            "app.agent.media_health_actions.diagnose_config",
            return_value=_config_result(warnings=1),
        ), patch(
            "app.agent.media_health_actions.diagnose_media_servers",
            return_value=_media_result(network_accessed=False, enabled=0, configured=0, online=0, compatible=0),
        ):
            result = diagnose_workspace_health({})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertFalse(result.data["network_accessed"])
        self.assertEqual(result.data["coverage"]["unavailable"], ["workspace"])
        self.assertEqual(result.data["areas"][0]["reason_codes"], ["workspace_snapshot_unavailable"])
        self.assertEqual(result.data["areas"][1]["reason_codes"], ["configuration_warnings"])
        self.assertEqual(result.data["areas"][2]["reason_codes"], ["media_server_not_configured"])
        self.assertGreaterEqual(result.data["attention_total"], 2)
        self.assertNotIn("PRIVATE database failure", json.dumps(result.to_dict(), ensure_ascii=False))

    def test_structured_offline_media_diagnosis_is_attention_not_missing_coverage(self):
        with patch(
            "app.agent.media_health_actions.summarize_workspace_briefing",
            return_value=_workspace_result(),
        ), patch(
            "app.agent.media_health_actions.diagnose_config",
            return_value=_config_result(),
        ), patch(
            "app.agent.media_health_actions.diagnose_media_servers",
            return_value=_media_result(
                status="unavailable", online=0, compatible=0, attention=1,
                nodes=[{"slot": "jellyfin", "connection_status": "unavailable"}],
            ),
        ):
            result = diagnose_workspace_health({})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["coverage"]["unavailable"], [])
        media = result.data["areas"][2]
        self.assertEqual(media["status"], "attention")
        self.assertIn("media_server_offline", media["reason_codes"])
        self.assertIn("media_server_compatibility_attention", media["reason_codes"])

    def test_all_child_failures_return_safe_unavailable(self):
        with patch(
            "app.agent.media_health_actions.summarize_workspace_briefing",
            side_effect=RuntimeError("PRIVATE-1"),
        ), patch(
            "app.agent.media_health_actions.diagnose_config",
            side_effect=RuntimeError("PRIVATE-2"),
        ), patch(
            "app.agent.media_health_actions.diagnose_media_servers",
            side_effect=RuntimeError("PRIVATE-3"),
        ):
            result = diagnose_workspace_health({})

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.data["coverage"]["unavailable"], [
            "workspace", "configuration", "media_servers",
        ])
        self.assertFalse(result.data["network_accessed"])
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("PRIVATE-", serialized)


class WorkspaceHealthRoutingTests(IsolatedDatabaseTestCase):
    def test_predicate_is_precise(self):
        positives = (
            "媒体健康总检",
            "媒体系统健康",
            "媒体系统总检",
            "全面检查整个媒体系统",
            "整个媒体系统怎么样",
        )
        negatives = (
            "全面检查",
            "系统怎么样",
            "媒体怎么样",
            "媒体系统配置怎么样",
            "刷新媒体健康总检",
            "检查媒体服务器状态",
            "系统简报",
            "检查下载队列状态",
            "媒体系统健康，测试 Jellyfin",
            "媒体系统健康，STRM 状态",
            "整个媒体系统怎么样，光鸭整理任务状态",
        )
        for message in positives:
            self.assertTrue(is_workspace_health_message(message), message)
        for message in negatives:
            self.assertFalse(is_workspace_health_message(message), message)

    def test_orchestrator_routes_health_without_stealing_existing_intents(self):
        calls: list[tuple[str, dict]] = []
        registry = ToolRegistry()
        for name in (
            "workspace.health", "workspace.briefing", "workspace.todo",
            "downloads.diagnose_queue", "config.diagnose_media_servers", "config.diagnose",
            "config.test_media_server", "strm.status", "guangya.organize.status",
        ):
            registry.register(ToolSpec(
                name=name,
                description=name,
                risk=RiskLevel.READ,
                parameters={},
                handler=lambda arguments, tool=name: (
                    calls.append((tool, dict(arguments))) or ToolResult(True, "healthy", tool)
                ),
                validator=lambda arguments: arguments,
            ))
        agent = AgentOrchestrator(registry)

        self.assertEqual(agent.query("媒体健康总检")["tool_call"]["name"], "workspace.health")
        self.assertEqual(
            agent.query("媒体系统健康总检，系统简报")["tool_call"]["name"],
            "workspace.health",
        )
        self.assertEqual(agent.query("系统简报")["tool_call"]["name"], "workspace.briefing")
        self.assertEqual(agent.query("工作区待办")["tool_call"]["name"], "workspace.todo")
        self.assertEqual(agent.query("检查下载队列状态")["tool_call"]["name"], "downloads.diagnose_queue")
        self.assertEqual(agent.query("检查媒体服务器状态")["tool_call"]["name"], "config.diagnose_media_servers")
        self.assertEqual(agent.query("检查项目配置")["tool_call"]["name"], "config.diagnose")
        self.assertEqual(agent.query("媒体系统健康，测试 Jellyfin")["tool_call"]["name"], "config.test_media_server")
        self.assertEqual(agent.query("媒体系统健康，STRM 状态")["tool_call"]["name"], "strm.status")
        self.assertEqual(
            agent.query("整个媒体系统怎么样，光鸭整理任务状态")["tool_call"]["name"],
            "guangya.organize.status",
        )
        self.assertIn(("workspace.health", {}), calls)


class WorkspaceHealthAPITests(IsolatedDatabaseTestCase):
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

    def test_auth_csrf_strict_body_and_shared_direct_query_rate_limit(self):
        path = "/api/agent/tools/workspace.health"
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code, 401)
        csrf = self._login()
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code, 403)
        headers = {"X-CSRF-Token": csrf}

        invalid = self.client.post(path, headers=headers, json={"session_id": "test_session_identifier_0001", "unexpected": 1})
        self.assertEqual(invalid.status_code, 400, invalid.text)
        invalid = self.client.post(path, headers=headers, json={"session_id": "test_session_identifier_0001", "arguments": {"debug": True}})
        self.assertEqual(invalid.status_code, 400, invalid.text)
        agent_rate_limiter.reset()

        with patch(
            "app.agent.media_health_actions.summarize_workspace_briefing",
            return_value=_workspace_result(),
        ), patch(
            "app.agent.media_health_actions.diagnose_config",
            return_value=_config_result(),
        ), patch(
            "app.agent.media_health_actions.diagnose_media_servers",
            return_value=_media_result(),
        ):
            for _ in range(4):
                response = self.client.post(path, headers=headers, json={"session_id": "test_session_identifier_0001", "arguments": {}})
                self.assertEqual(response.status_code, 200, response.text)
            limited = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "媒体健康总检"},
            )
        self.assertEqual(limited.status_code, 429, limited.text)


if __name__ == "__main__":
    import unittest
    unittest.main()
