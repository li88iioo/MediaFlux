"""Media Agent 功能状态摘要的投影、路由、安全与 API 契约。"""
from __future__ import annotations

import json
import re
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.agent.feature_actions import feature_summary_arguments, summarize_feature_states
from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import AgentOrchestrator, is_feature_summary_message
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


class FeatureSummaryUnitTests(IsolatedDatabaseTestCase):
    def test_arguments_are_strictly_empty(self):
        self.assertEqual(feature_summary_arguments({}), {})
        for arguments in (None, {"feature": "discovery"}, {"debug": True}):
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                feature_summary_arguments(arguments)  # type: ignore[arg-type]

    def test_summary_projects_dependency_state_without_values(self):
        values = {
            "DISCOVERY_ENABLED": False,
            "DISCOVERY_DOUBAN_ENABLED": True,
            "DISCOVERY_RESOURCE_RESULTS_ENABLED": True,
            "INDEXER_SEARCH_ENABLED": False,
            "WEB_SEARCH_ENABLED": True,
        }
        secret = "FEATURE_SUMMARY_SECRET"

        def get_bool(key: str, default: bool = False) -> bool:
            return values.get(key, default)

        def get_value(key: str, default: str = "") -> str:
            if key == "INDEXER_ENABLED_SITES":
                return f"unknown,{secret}"
            return default

        with patch("app.agent.feature_actions.config.get_bool", side_effect=get_bool), patch(
            "app.agent.feature_actions.config.get",
            side_effect=get_value,
        ), patch(
            "app.agent.feature_actions.config.has_external_override",
            side_effect=lambda key: key == "DISCOVERY_DOUBAN_ENABLED",
        ):
            result = summarize_feature_states({})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["feature_count"], 10)
        self.assertEqual(result.data["enabled_count"], 6)
        self.assertEqual(result.data["available_count"], 3)
        self.assertEqual(result.data["disabled_count"], 4)
        self.assertEqual(result.data["attention_count"], 3)

        by_feature = {item["feature"]: item for item in result.data["features"]}
        self.assertEqual(by_feature["discovery"]["availability"], "disabled")
        self.assertEqual(by_feature["discovery"]["reason_codes"], ["feature_disabled"])
        self.assertEqual(by_feature["douban"]["availability"], "blocked")
        self.assertEqual(by_feature["douban"]["reason_codes"], ["parent_disabled"])
        self.assertTrue(by_feature["douban"]["managed_by_environment"])
        self.assertEqual(
            by_feature["resource_results"]["reason_codes"],
            ["parent_disabled", "search_disabled", "no_enabled_sites"],
        )
        self.assertEqual(by_feature["indexer_search"]["availability"], "disabled")
        self.assertEqual(by_feature["web_search"]["availability"], "blocked")
        self.assertEqual(
            by_feature["web_search"]["reason_codes"],
            ["provider_not_configured"],
        )

        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for forbidden in (
            secret,
            "DISCOVERY_ENABLED",
            "INDEXER_ENABLED_SITES",
            "unknown",
            "TMDB_API_KEY",
            "http://",
            "/srv/",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_ready_summary_and_registry_contract(self):
        with patch("app.agent.feature_actions.config.get_bool", return_value=True), patch(
            "app.agent.feature_actions.config.get", return_value="nyaa"
        ), patch(
            "app.agent.feature_actions.config.has_external_override", return_value=False
        ):
            result = summarize_feature_states({})

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.data["available_count"], 10)
        self.assertEqual(result.data["attention_count"], 0)
        self.assertFalse(result.suggestions)

        registry = build_tool_registry()
        capability = next(
            item for item in registry.capabilities()
            if item["name"] == "config.feature_summary"
        )
        self.assertEqual(capability["risk"], RiskLevel.READ.value)
        self.assertFalse(capability["requires_confirmation"])

    def test_sukebei_opt_in_counts_as_an_effective_site_and_all_disabled_is_explicit(self):
        enabled = {
            "DISCOVERY_ENABLED": True,
            "DISCOVERY_DOUBAN_ENABLED": True,
            "DISCOVERY_RESOURCE_RESULTS_ENABLED": True,
            "INDEXER_SEARCH_ENABLED": True,
            "INDEXER_SUKEBEI_ENABLED": True,
        }
        with patch(
            "app.agent.feature_actions.config.get_bool",
            side_effect=lambda key, default=False: enabled.get(key, default),
        ), patch(
            "app.agent.feature_actions.config.get", return_value=""
        ), patch(
            "app.agent.feature_actions.config.has_external_override", return_value=False
        ):
            result = summarize_feature_states({})
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.data["available_count"], 7)

        with patch("app.agent.feature_actions.config.get_bool", return_value=False), patch(
            "app.agent.feature_actions.config.get", return_value=""
        ), patch(
            "app.agent.feature_actions.config.has_external_override", return_value=False
        ):
            disabled = summarize_feature_states({})
        self.assertEqual(disabled.status, "disabled")
        self.assertEqual(disabled.data["available_count"], 0)
        self.assertEqual(disabled.data["disabled_count"], 10)

    def test_natural_language_routes_status_questions_without_stealing_actions(self):
        for message in (
            "媒体探索现在是什么状态",
            "豆瓣探索是否可用",
            "多站资源搜索有没有开启",
            "联网搜索是否可用",
            "站点资源结果健康吗",
            "STRM 元数据同步是什么状态",
            "STRM 伴随元数据同步是否开启",
            "查看功能开关状态",
        ):
            with self.subTest(message=message):
                self.assertTrue(is_feature_summary_message(message))

        for message in (
            "请关闭媒体探索",
            "启用多站资源搜索",
            "开启联网搜索",
            "搜索《媒体探索》",
            "检查项目配置",
            "索引器是否健康",
            "找媒体探索的资源有没有 1080p",
            "搜索媒体探索有没有资源",
            "联网搜索如何开启 Jellyfin DLNA",
            "网页搜索开启 CORS 的方法",
        ):
            with self.subTest(message=message):
                self.assertFalse(is_feature_summary_message(message))

        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="config.feature_summary",
            description="feature summary",
            risk=RiskLevel.READ,
            parameters={},
            handler=lambda arguments: ToolResult(True, "ready", "ready", data=arguments),
            validator=feature_summary_arguments,
        ))
        response = AgentOrchestrator(registry).query("豆瓣探索是否可用")
        self.assertEqual(response["tool_call"]["name"], "config.feature_summary")
        self.assertEqual(response["tool_call"]["arguments"], {})

        routing_registry = ToolRegistry()
        for name in (
            "config.feature_summary",
            "indexer.diagnose_readiness",
            "indexer.search_resources",
        ):
            routing_registry.register(ToolSpec(
                name=name,
                description=name,
                risk=RiskLevel.READ,
                parameters={},
                handler=lambda arguments, tool=name: ToolResult(True, "ready", tool),
                validator=lambda arguments: arguments,
            ))
        routed = AgentOrchestrator(routing_registry)
        self.assertEqual(
            routed.query("多站资源搜索是否可用")["tool_call"]["name"],
            "indexer.diagnose_readiness",
        )
        self.assertEqual(
            routed.query("多站资源搜索有没有开启")["tool_call"]["name"],
            "config.feature_summary",
        )
        self.assertEqual(
            routed.query("找媒体探索的资源有没有 1080p")["tool_call"]["name"],
            "indexer.search_resources",
        )
        self.assertEqual(
            routed.query("STRM 元数据同步是什么状态")["tool_call"]["name"],
            "config.feature_summary",
        )


class FeatureSummaryAPITests(IsolatedDatabaseTestCase):
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

    def test_api_auth_csrf_strict_arguments_and_shared_rate_limit(self):
        path = "/api/agent/tools/config.feature_summary"
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code, 401)
        csrf = self._login()
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code, 403)
        headers = {"X-CSRF-Token": csrf}

        invalid = self.client.post(path, headers=headers, json={"session_id": "test_session_identifier_0001", "arguments": {"debug": True}})
        self.assertEqual(invalid.status_code, 400, invalid.text)

        agent_rate_limiter.reset()
        for _ in range(12):
            response = self.client.post(path, headers=headers, json={"session_id": "test_session_identifier_0001", "arguments": {}})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["tool_call"]["name"], "config.feature_summary")
        limited = self.client.post(
            "/api/agent/query",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "message": "媒体探索现在是什么状态"},
        )
        self.assertEqual(limited.status_code, 429, limited.text)

    def test_query_uses_feature_summary_and_returns_only_safe_projection(self):
        csrf = self._login()
        response = self.client.post(
            "/api/agent/query",
            headers={"X-CSRF-Token": csrf},
            json={"session_id": "test_session_identifier_0001", "message": "多站资源搜索有没有开启"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["tool_call"]["name"], "config.feature_summary")
        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in (
            "DISCOVERY_ENABLED",
            "INDEXER_ENABLED_SITES",
            "TMDB_API_KEY",
            "DOUBAN_DBCL2",
        ):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    import unittest

    unittest.main()
