"""Media Agent 配置组件说明的安全投影、路由与 API 契约。"""
from __future__ import annotations

import json
import re
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.agent.config_explain_actions import (
    CONFIG_COMPONENTS,
    config_component_arguments,
    explain_config_component,
)
from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import AgentOrchestrator, config_component_explain_request
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


def _config_get(values: dict[str, object]):
    return lambda key, default="": values.get(key, default)


def _explain_with_values(component: str, values: dict[str, object]) -> ToolResult:
    with patch(
        "app.agent.config_explain_actions.config.all_items",
        return_value=dict(values),
    ), patch(
        "app.agent.config_explain_actions.config.get",
        side_effect=_config_get(values),
    ), patch(
        "app.agent.config_explain_actions.config.has_external_override",
        return_value=False,
    ):
        return explain_config_component({"component": component})


class ConfigComponentExplainUnitTests(unittest.TestCase):
    def test_arguments_are_strict_lowercase_whitelist(self):
        self.assertEqual(
            config_component_arguments({"component": " jellyfin "}),
            {"component": "jellyfin"},
        )
        invalid = (
            None,
            {},
            {"component": ""},
            {"component": 1},
            {"component": "JELLYFIN"},
            {"component": "plex"},
            {"component": "jellyfin", "url": "http://attacker.invalid"},
            {"component": "jellyfin", "value": "secret"},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                config_component_arguments(arguments)  # type: ignore[arg-type]

    def test_incomplete_component_returns_labels_without_configuration_values(self):
        values = {
            "QB_URL": "http://private-qb.example:8080",
            "QB_USERNAME": "",
            "QB_PASSWORD": "TOP_SECRET_PASSWORD",
            "QB_API_KEY": "",
            "UNRELATED_PATH": "/srv/private/media",
        }
        result = _explain_with_values("qbittorrent", values)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.summary, "qBittorrent：配置不完整")
        self.assertEqual(result.data["required_field_labels"], ["服务地址", "API Key 或用户名/密码"])
        self.assertEqual(result.data["missing_field_labels"], ["API Key 或用户名/密码"])
        self.assertIn("提交资源到 qBittorrent", result.data["blocked_capabilities"])

        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for forbidden in (
            "QB_URL",
            "QB_PASSWORD",
            "TOP_SECRET_PASSWORD",
            "private-qb.example",
            "/srv/private/media",
            "UNRELATED_PATH",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_disabled_switch_wins_over_retained_credentials(self):
        result = _explain_with_values("jellyfin", {
            "JELLYFIN_ENABLED": "0",
            "JELLYFIN_URL": "http://jellyfin.internal:8096",
            "JELLYFIN_API_KEY": "retained-secret",
        })

        self.assertEqual(result.status, "disabled")
        self.assertEqual(result.summary, "Jellyfin：已关闭")
        self.assertFalse(result.data["enabled"])
        self.assertEqual(result.data["missing_field_labels"], [])
        self.assertTrue(result.data["blocked_capabilities"])
        self.assertNotIn("retained-secret", json.dumps(result.to_dict(), ensure_ascii=False))

    def test_base_component_reports_environment_management_without_keys(self):
        values = {
            "JELLYFIN_ENABLED": "1",
            "JELLYFIN_URL": "http://jellyfin.internal:8096",
            "JELLYFIN_API_KEY": "",
        }
        with patch(
            "app.agent.config_explain_actions.config.all_items",
            return_value=dict(values),
        ), patch(
            "app.agent.config_explain_actions.config.get",
            side_effect=_config_get(values),
        ), patch(
            "app.agent.config_explain_actions.config.has_external_override",
            side_effect=lambda key: key == "JELLYFIN_API_KEY",
        ):
            result = explain_config_component({"component": "jellyfin"})

        self.assertEqual(result.status, "incomplete")
        self.assertTrue(result.data["managed_by_environment"])
        self.assertIn("运行环境", "".join(result.suggestions))
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("JELLYFIN_API_KEY", serialized)
        self.assertNotIn("jellyfin.internal", serialized)

    def test_ready_component_has_no_blocked_capabilities(self):
        result = _explain_with_values("jellyfin", {
            "JELLYFIN_ENABLED": "1",
            "JELLYFIN_URL": "http://jellyfin.internal:8096",
            "JELLYFIN_API_KEY": "secret",
        })

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.summary, "Jellyfin：已就绪")
        self.assertTrue(result.data["enabled"])
        self.assertEqual(result.data["missing_field_labels"], [])
        self.assertEqual(result.data["blocked_capabilities"], [])
        self.assertIn("测试Jellyfin连接", "".join(result.suggestions))

    def test_ai_recognition_reuses_media_agent_provider(self):
        result = _explain_with_values("ai_recognition", {
            "AI_RECOGNITION_ENABLED": "1",
            "AGENT_LLM_API_URL": "https://agent.invalid/v1/chat/completions",
            "AGENT_LLM_MODEL": "shared-model",
        })

        self.assertEqual(result.status, "ready")
        self.assertEqual(
            result.data["required_field_labels"],
            ["Media Agent 模型连接配置"],
        )
        self.assertEqual(result.data["missing_field_labels"], [])

    def test_ai_recognition_points_incomplete_setup_to_media_agent(self):
        result = _explain_with_values("ai_recognition", {
            "AI_RECOGNITION_ENABLED": "1",
        })

        self.assertEqual(result.status, "incomplete")
        self.assertEqual(
            result.data["missing_field_labels"],
            ["Media Agent 模型连接配置"],
        )
        self.assertIn("Media Agent", "".join(result.suggestions))

    def test_disabled_feature_offers_only_confirmed_safe_action(self):
        feature_summary = ToolResult(
            True,
            "attention",
            "feature summary",
            data={
                "features": [{
                    "feature": "douban",
                    "label": "豆瓣探索",
                    "enabled": False,
                    "availability": "disabled",
                    "reason_codes": ["feature_disabled"],
                    "managed_by_environment": False,
                }],
            },
        )
        with patch(
            "app.agent.config_explain_actions.summarize_feature_states",
            return_value=feature_summary,
        ):
            result = explain_config_component({"component": "douban"})

        self.assertEqual(result.status, "disabled")
        self.assertEqual(result.data["agent_action"], {
            "supported": True,
            "tool": "config.set_feature_state",
            "feature": "douban",
            "enabled": True,
            "requires_confirmation": True,
            "prompt": "开启豆瓣探索",
        })

    def test_environment_managed_feature_never_offers_agent_write(self):
        feature_summary = ToolResult(
            True,
            "attention",
            "feature summary",
            data={
                "features": [{
                    "feature": "douban",
                    "label": "豆瓣探索",
                    "enabled": True,
                    "availability": "blocked",
                    "reason_codes": ["parent_disabled"],
                    "managed_by_environment": True,
                }],
            },
        )
        with patch(
            "app.agent.config_explain_actions.summarize_feature_states",
            return_value=feature_summary,
        ):
            result = explain_config_component({"component": "douban"})

        self.assertEqual(result.status, "blocked")
        self.assertTrue(result.data["managed_by_environment"])
        self.assertIsNone(result.data["agent_action"])
        self.assertIn("运行环境", "".join(result.suggestions))

    def test_registry_contract_is_read_only_and_schema_matches_validator(self):
        registry = build_tool_registry()
        capability = next(
            item for item in registry.capabilities()
            if item["name"] == "config.explain_component"
        )
        self.assertEqual(capability["risk"], RiskLevel.READ.value)
        self.assertFalse(capability["requires_confirmation"])
        self.assertEqual(
            tuple(capability["parameters"]["properties"]["component"]["enum"]),
            CONFIG_COMPONENTS,
        )

    def test_natural_language_routing_is_specific_and_does_not_steal_actions(self):
        cases = (
            ("为什么 STRM 配置不完整", {"component": "strm"}),
            ("Jellyfin 配置需要什么", {"component": "jellyfin"}),
            ("为什么豆瓣探索不可用", {"component": "douban"}),
            ("Jellyfin 10.x 怎么配置", {"component": "emby"}),
            ("Emby 和 Jellyfin 怎么配置", None),
            ("怎么配置", None),
            ("开启豆瓣探索", None),
            ("校验 Jellyfin 配置", None),
            ("为什么 Jellyfin 连接测试不可用", None),
            ("为什么 STRM 连通诊断失败", None),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(config_component_explain_request(message), expected)

        registry = ToolRegistry()
        for name in (
            "config.explain_component",
            "config.diagnose",
            "config.test_media_server",
        ):
            registry.register(ToolSpec(
                name=name,
                description=name,
                risk=RiskLevel.READ,
                parameters={},
                handler=lambda arguments, tool=name: ToolResult(
                    True, "ready", tool, data=dict(arguments)
                ),
                validator=lambda arguments: dict(arguments),
            ))
        agent = AgentOrchestrator(registry)
        explained = agent.query("Jellyfin 配置需要什么")
        self.assertEqual(explained["tool_call"]["name"], "config.explain_component")
        self.assertEqual(explained["tool_call"]["arguments"], {"component": "jellyfin"})
        self.assertEqual(agent.query("检查项目配置")["tool_call"]["name"], "config.diagnose")
        self.assertEqual(
            agent.query("校验 Jellyfin 配置")["tool_call"]["name"],
            "config.test_media_server",
        )
        self.assertEqual(
            agent.query("为什么 Jellyfin 连接测试不可用")["tool_call"]["name"],
            "config.test_media_server",
        )


class ConfigComponentExplainAPITests(IsolatedDatabaseTestCase):
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

    def test_api_auth_csrf_strict_arguments_safe_projection_and_shared_rate_limit(self):
        path = "/api/agent/tools/config.explain_component"
        arguments = {"session_id": "test_session_identifier_0001", "arguments": {"component": "jellyfin"}}
        self.assertEqual(self.client.post(path, json=arguments).status_code, 401)
        csrf = self._login()
        self.assertEqual(self.client.post(path, json=arguments).status_code, 403)
        headers = {"X-CSRF-Token": csrf}

        for invalid_arguments in (
            {"component": "JELLYFIN"},
            {"component": "jellyfin", "value": "secret"},
        ):
            invalid = self.client.post(
                path,
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": invalid_arguments},
            )
            self.assertEqual(invalid.status_code, 400, invalid.text)

        values = {
            "AGENT_ENABLED": "1",
            "JELLYFIN_ENABLED": "1",
            "JELLYFIN_URL": "http://private-jellyfin.example:8096",
            "JELLYFIN_API_KEY": "TOP_SECRET_API_KEY",
        }
        agent_rate_limiter.reset()
        with patch(
            "app.agent.config_explain_actions.config.all_items",
            return_value=dict(values),
        ), patch(
            "app.agent.config_explain_actions.config.get",
            side_effect=_config_get(values),
        ), patch(
            "app.agent.config_explain_actions.config.has_external_override",
            return_value=False,
        ):
            for _ in range(12):
                response = self.client.post(path, headers=headers, json=arguments)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["tool_call"]["name"], "config.explain_component")
                self.assertNotIn("private-jellyfin.example", response.text)
                self.assertNotIn("TOP_SECRET_API_KEY", response.text)

            limited = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "Jellyfin 配置需要什么"},
            )
        self.assertEqual(limited.status_code, 429, limited.text)

    def test_natural_language_api_uses_safe_component_projection(self):
        csrf = self._login()
        values = {
            "AGENT_ENABLED": "1",
            "QB_URL": "http://private-qb.example:8080",
            "QB_USERNAME": "admin",
            "QB_PASSWORD": "TOP_SECRET_PASSWORD",
        }
        with patch(
            "app.agent.config_explain_actions.config.all_items",
            return_value=dict(values),
        ), patch(
            "app.agent.config_explain_actions.config.get",
            side_effect=_config_get(values),
        ), patch(
            "app.agent.config_explain_actions.config.has_external_override",
            return_value=False,
        ):
            response = self.client.post(
                "/api/agent/query",
                headers={"X-CSRF-Token": csrf},
                json={"session_id": "test_session_identifier_0001", "message": "qBittorrent 配置缺什么"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["tool_call"]["name"], "config.explain_component")
        self.assertEqual(payload["tool_call"]["arguments"], {"component": "qbittorrent"})
        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in (
            "QB_URL",
            "QB_USERNAME",
            "QB_PASSWORD",
            "private-qb.example",
            "TOP_SECRET_PASSWORD",
        ):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
