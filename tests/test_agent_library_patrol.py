"""全库剧集巡检工具契约、路由与 API 限流。"""
from __future__ import annotations

from datetime import date, timedelta
import re
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import (
    AgentOrchestrator,
    is_explicit_background_library_episode_patrol_message,
    is_library_episode_patrol_message,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


def _identity(arguments):
    return dict(arguments)


class LibraryPatrolContractTests(unittest.TestCase):
    def test_registry_schema_is_read_only_and_strict(self):
        registry = build_tool_registry()
        capability = {item["name"]: item for item in registry.capabilities()}[
            "library.audit_library_episodes"
        ]
        self.assertEqual(capability["risk"], "read")
        self.assertFalse(capability["requires_confirmation"])
        self.assertFalse(capability["parameters"]["additionalProperties"])
        self.assertEqual(
            set(capability["parameters"]["properties"]),
            {"as_of", "max_series"},
        )

        invalid = (
            {"max_series": 0},
            {"max_series": 101},
            {"max_series": True},
            {"token": "secret"},
            {"as_of": (date.today() + timedelta(days=1)).isoformat()},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                registry.execute("library.audit_library_episodes", arguments)

    def test_natural_language_routes_full_library_before_single_series(self):
        calls = []
        registry = ToolRegistry()
        for name in (
            "library.audit_library_episodes",
            "library.audit_episodes",
            "library.check_updates",
        ):
            registry.register(ToolSpec(
                name=name,
                description=name,
                risk=RiskLevel.READ,
                parameters={},
                handler=lambda arguments, tool=name: (
                    calls.append((tool, dict(arguments)))
                    or ToolResult(True, "success", tool)
                ),
                validator=_identity,
            ))
        agent = AgentOrchestrator(registry)

        for message in (
            "巡检整个媒体库有没有缺集",
            "检查媒体库有没有缺集",
            "查看媒体库有没有缺集",
            "检查我的 Jellyfin 库所有剧集是否缺集",
            "全库剧集缺集巡检",
        ):
            with self.subTest(message=message):
                response = agent.query(message)
                self.assertEqual(
                    response["tool_call"]["name"],
                    "library.audit_library_episodes",
                )
                self.assertEqual(response["tool_call"]["arguments"], {})

        for message in (
            "检查《示例剧》第 2 季有没有缺集",
            "检查媒体库里的《示例剧》第 2 季有没有缺集",
        ):
            with self.subTest(single_message=message):
                single = agent.query(message)
                self.assertEqual(single["tool_call"]["name"], "library.audit_episodes")
        self.assertNotIn(("library.check_updates", {}), calls)

    def test_full_registry_keeps_lightweight_check_read_only_and_explicit_patrol_durable(self):
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="library.audit_library_episodes",
            description="read",
            risk=RiskLevel.READ,
            parameters={},
            handler=lambda arguments: ToolResult(True, "success", "read", data=arguments),
            validator=_identity,
        ))
        registry.register(ToolSpec(
            name="library.start_episode_audit",
            description="durable",
            risk=RiskLevel.LOW_WRITE,
            parameters={},
            handler=lambda arguments: ToolResult(True, "accepted", "durable", data=arguments),
            validator=_identity,
            requires_confirmation=True,
            confirmation_preparer=lambda arguments: (
                ToolResult(True, "confirmation_required", "confirm", data=arguments),
                "ctx",
            ),
        ))
        agent = AgentOrchestrator(registry)

        immediate = agent.query("查看媒体库有没有缺集", owner="web:user")
        self.assertEqual(immediate["tool_call"]["name"], "library.audit_library_episodes")
        self.assertEqual(immediate["mode"], "read_only")

        durable = agent.query("巡检整个媒体库有没有缺集", owner="web:user")
        self.assertEqual(durable["tool_call"]["name"], "library.start_episode_audit")
        self.assertEqual(durable["mode"], "confirmation_required")

        self.assertFalse(is_explicit_background_library_episode_patrol_message("检查媒体库有没有缺集"))
        self.assertTrue(is_explicit_background_library_episode_patrol_message("检查我的 Jellyfin 库所有剧集是否缺集"))

    def test_natural_language_accepts_bounded_count_and_date(self):
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="library.audit_library_episodes",
            description="patrol",
            risk=RiskLevel.READ,
            parameters={},
            handler=lambda arguments: ToolResult(True, "success", "done", data=arguments),
            validator=_identity,
        ))
        agent = AgentOrchestrator(registry)
        response = agent.query("巡检整个媒体库前 20 部剧，截至 2026-08-01")
        self.assertEqual(response["tool_call"]["arguments"], {
            "max_series": 20,
            "as_of": "2026-08-01",
        })

    def test_non_episode_library_operations_do_not_trigger_patrol(self):
        for message in (
            "检查全库扫描的完整性配置",
            "刷新整个媒体库",
            "诊断整个媒体库服务器状态",
            "检查全部剧集的完整性设置",
            "如何配置全库剧集完整性检查",
            "全库剧集缺集检测功能支持什么格式",
            "全库剧集缺集资源怎么配置",
        ):
            with self.subTest(message=message):
                self.assertFalse(is_library_episode_patrol_message(message))


class LibraryPatrolAPITests(IsolatedDatabaseTestCase):
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

    def test_direct_and_natural_language_share_two_per_minute_limit(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        result = ToolResult(True, "up_to_date", "done", data={"checked_series_count": 1})
        with patch("app.agent.tools.audit_library_episodes", return_value=result):
            direct = self.client.post(
                "/api/agent/tools/library.audit_library_episodes",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"max_series": 1}},
            )
            natural = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "巡检整个媒体库有没有缺集"},
            )
            limited = self.client.post(
                "/api/agent/tools/library.audit_library_episodes",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"max_series": 1}},
            )
        self.assertEqual(direct.status_code, 200, direct.text)
        self.assertEqual(natural.status_code, 200, natural.text)
        self.assertEqual(limited.status_code, 429, limited.text)



if __name__ == "__main__":
    unittest.main()
