"""Media Agent 媒体服务器连通性测试。"""
from __future__ import annotations

import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests
from fastapi.testclient import TestClient

from app.agent.config_actions import media_server_arguments, test_media_server as run_media_server_test
from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import AgentOrchestrator, is_media_server_test_message
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


def _identity(arguments):
    return dict(arguments)


def _config_values(server_type: str, *, enabled: bool = True) -> dict[str, str]:
    if server_type == "jellyfin":
        return {
            "JELLYFIN_ENABLED": "1" if enabled else "0",
            "JELLYFIN_URL": "http://jellyfin.internal:8096",
            "JELLYFIN_API_KEY": "jellyfin-secret-token",
        }
    return {
        "EMBY_ENABLED": "1" if enabled else "0",
        "EMBY_URL": "https://emby.internal/base",
        "EMBY_TOKEN": "emby-secret-token",
    }


def _config_get(values: dict[str, str]):
    return lambda key, default="": values.get(key, default)


class AgentConfigConnectionTests(unittest.TestCase):
    def test_arguments_are_strict_and_normalized(self):
        self.assertEqual(media_server_arguments({"server_type": " JellyFin "}), {"server_type": "jellyfin"})
        self.assertEqual(media_server_arguments({"server_type": "ＥＭＢＹ"}), {"server_type": "emby"})
        invalid = (
            {},
            {"server_type": 1},
            {"server_type": "plex"},
            {"server_type": "jellyfin", "url": "http://attacker.invalid"},
            {"server_type": "emby", "token": "secret"},
            {"server_type": "emby", "headers": {}},
            {"server_type": "emby", "timeout": 30},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                media_server_arguments(arguments)

    def test_disabled_and_incomplete_configuration_do_not_send_requests(self):
        cases = (
            ("jellyfin", False, _config_values("jellyfin", enabled=False), "disabled"),
            ("jellyfin", True, {"JELLYFIN_ENABLED": "1", "JELLYFIN_URL": "", "JELLYFIN_API_KEY": "x"}, "not_configured"),
            ("emby", True, {"EMBY_ENABLED": "1", "EMBY_URL": "file:///tmp/media", "EMBY_TOKEN": "x"}, "not_configured"),
            ("emby", True, {"EMBY_ENABLED": "1", "EMBY_URL": "https://user:pass@emby.internal", "EMBY_TOKEN": "x"}, "not_configured"),
            ("emby", True, {"EMBY_ENABLED": "1", "EMBY_URL": "https://emby.internal/base?next=/admin", "EMBY_TOKEN": "x"}, "not_configured"),
            ("emby", True, {"EMBY_ENABLED": "1", "EMBY_URL": "https://emby.internal/base#fragment", "EMBY_TOKEN": "x"}, "not_configured"),
            ("emby", True, {"EMBY_ENABLED": "1", "EMBY_URL": "https://emby.internal:bad", "EMBY_TOKEN": "x"}, "not_configured"),
            ("emby", True, {"EMBY_ENABLED": "1", "EMBY_URL": "http://[invalid", "EMBY_TOKEN": "x"}, "not_configured"),
        )
        for server_type, enabled, values, expected in cases:
            with self.subTest(server_type=server_type, expected=expected), patch(
                "app.agent.config_actions.config.get_bool", return_value=enabled
            ), patch("app.agent.config_actions.config.get", side_effect=_config_get(values)), patch(
                "app.agent.config_actions.requests.get"
            ) as request_get:
                result = run_media_server_test({"server_type": server_type})
            self.assertFalse(result.ok)
            self.assertEqual(result.status, expected)
            request_get.assert_not_called()

    def test_jellyfin_success_uses_server_configuration_and_returns_safe_identity(self):
        values = _config_values("jellyfin")
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "ServerName": " Living\nRoom ",
            "ProductName": "Jellyfin Server",
            "Version": "12.0.1",
            "Secret": "must-not-leak",
        }
        with patch("app.agent.config_actions.config.get_bool", return_value=True), patch(
            "app.agent.config_actions.config.get", side_effect=_config_get(values)
        ), patch("app.agent.config_actions.requests.get", return_value=response) as request_get:
            result = run_media_server_test({"server_type": "jellyfin"})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.data["server_name"], "Living Room")
        self.assertEqual(result.data["product"], "Jellyfin")
        self.assertEqual(result.data["version"], "12.0.1")
        self.assertGreaterEqual(result.data["latency_ms"], 1)
        call = request_get.call_args
        self.assertEqual(call.args[0], "http://jellyfin.internal:8096/System/Info")
        self.assertEqual(call.kwargs["timeout"], (3.5, 8))
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertIsNone(call.kwargs["params"])
        self.assertIn("jellyfin-secret-token", call.kwargs["headers"]["Authorization"])
        serialized = repr(result.to_dict())
        self.assertNotIn("jellyfin.internal", serialized)
        self.assertNotIn("jellyfin-secret-token", serialized)
        self.assertNotIn("must-not-leak", serialized)

    def test_emby_compatible_auth_identifies_legacy_jellyfin(self):
        values = _config_values("emby")
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "ServerName": "Legacy Room",
            "ProductName": "Jellyfin Server",
            "Version": "10.11.11",
        }
        with patch("app.agent.config_actions.config.get_bool", return_value=True), patch(
            "app.agent.config_actions.config.get", side_effect=_config_get(values)
        ), patch("app.agent.config_actions.requests.get", return_value=response) as request_get:
            result = run_media_server_test({"server_type": "emby"})

        self.assertTrue(result.ok)
        self.assertEqual(result.data["product"], "Jellyfin")
        call = request_get.call_args
        self.assertEqual(call.args[0], "https://emby.internal/base/System/Info")
        self.assertEqual(call.kwargs["headers"]["X-Emby-Token"], "emby-secret-token")
        self.assertIn("emby-secret-token", call.kwargs["headers"]["Authorization"])
        self.assertNotIn("emby.internal", repr(result.to_dict()))
        self.assertNotIn("emby-secret-token", repr(result.to_dict()))

    def test_jellyfin_slot_reports_detected_emby_product_for_compatibility_checks(self):
        values = _config_values("jellyfin")
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "ServerName": "Media Room",
            "ProductName": "Emby Server",
            "Version": "4.9.0",
        }
        with patch("app.agent.config_actions.config.get_bool", return_value=True), patch(
            "app.agent.config_actions.config.get", side_effect=_config_get(values)
        ), patch("app.agent.config_actions.requests.get", return_value=response):
            result = run_media_server_test({"server_type": "jellyfin"})

        self.assertTrue(result.ok)
        self.assertEqual(result.data["product"], "Emby")
        self.assertTrue(result.data["product_detected"])
        self.assertEqual(result.data["version"], "4.9.0")

    def test_product_field_is_checked_when_product_name_is_generic(self):
        values = _config_values("jellyfin")
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "ServerName": "Media Room",
            "ProductName": "Media Server",
            "Product": "Jellyfin",
            "Version": "12.0.1",
        }
        with patch("app.agent.config_actions.config.get_bool", return_value=True), patch(
            "app.agent.config_actions.config.get", side_effect=_config_get(values)
        ), patch("app.agent.config_actions.requests.get", return_value=response):
            result = run_media_server_test({"server_type": "jellyfin"})

        self.assertTrue(result.ok)
        self.assertEqual(result.data["product"], "Jellyfin")
        self.assertTrue(result.data["product_detected"])

    def test_unknown_product_keeps_display_fallback_but_marks_identity_unverified(self):
        values = _config_values("jellyfin")
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"ServerName": "Media Room", "Version": "12.0.1"}
        with patch("app.agent.config_actions.config.get_bool", return_value=True), patch(
            "app.agent.config_actions.config.get", side_effect=_config_get(values)
        ), patch("app.agent.config_actions.requests.get", return_value=response):
            result = run_media_server_test({"server_type": "jellyfin"})

        self.assertTrue(result.ok)
        self.assertEqual(result.data["product"], "Jellyfin")
        self.assertFalse(result.data["product_detected"])

    def test_failure_mapping_is_stable_and_never_leaks_exception_text(self):
        values = _config_values("jellyfin")
        http_cases = (
            (requests.Timeout("secret timeout details"), "timeout"),
            (requests.ConnectionError("secret host details"), "connection"),
            (requests.HTTPError("secret auth details", response=SimpleNamespace(status_code=401)), "authentication"),
            (requests.HTTPError("secret path details", response=SimpleNamespace(status_code=404)), "not_found"),
            (requests.HTTPError("secret upstream details", response=SimpleNamespace(status_code=503)), "http_error"),
            (RuntimeError("secret internal details"), "unavailable"),
        )
        for error, expected in http_cases:
            response = Mock()
            response.raise_for_status.side_effect = error
            with self.subTest(expected=expected), patch(
                "app.agent.config_actions.config.get_bool", return_value=True
            ), patch("app.agent.config_actions.config.get", side_effect=_config_get(values)), patch(
                "app.agent.config_actions.requests.get", return_value=response
            ):
                result = run_media_server_test({"server_type": "jellyfin"})
            self.assertFalse(result.ok)
            self.assertEqual(result.status, expected)
            serialized = repr(result.to_dict())
            self.assertNotIn("secret", serialized)
            self.assertNotIn("jellyfin.internal", serialized)

    def test_redirect_is_rejected_without_following_or_exposing_location(self):
        values = _config_values("jellyfin")
        response = Mock(status_code=302)
        response.headers = {"Location": "http://169.254.169.254/latest/secret"}
        with patch("app.agent.config_actions.config.get_bool", return_value=True), patch(
            "app.agent.config_actions.config.get", side_effect=_config_get(values)
        ), patch("app.agent.config_actions.requests.get", return_value=response) as request_get:
            result = run_media_server_test({"server_type": "jellyfin"})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "redirect_not_allowed")
        self.assertFalse(request_get.call_args.kwargs["allow_redirects"])
        self.assertNotIn("169.254.169.254", repr(result.to_dict()))
        response.raise_for_status.assert_not_called()

    def test_invalid_json_shape_is_safe(self):
        values = _config_values("jellyfin")
        for payload in (["not", "a", "dict"], ValueError("secret json body")):
            response = Mock()
            response.raise_for_status.return_value = None
            if isinstance(payload, Exception):
                response.json.side_effect = payload
            else:
                response.json.return_value = payload
            with patch("app.agent.config_actions.config.get_bool", return_value=True), patch(
                "app.agent.config_actions.config.get", side_effect=_config_get(values)
            ), patch("app.agent.config_actions.requests.get", return_value=response):
                result = run_media_server_test({"server_type": "jellyfin"})
            self.assertEqual(result.status, "invalid_response")
            self.assertNotIn("secret", repr(result.to_dict()))

    def test_registry_and_natural_language_routing_keep_static_diagnosis_separate(self):
        capabilities = {item["name"]: item for item in build_tool_registry().capabilities()}
        self.assertEqual(capabilities["config.test_media_server"]["risk"], "read")
        self.assertEqual(
            set(capabilities["config.test_media_server"]["parameters"]["properties"]),
            {"server_type"},
        )

        calls: list[tuple[str, dict]] = []
        registry = ToolRegistry()
        for name in ("config.test_media_server", "config.diagnose"):
            registry.register(ToolSpec(
                name=name,
                description=name,
                risk=RiskLevel.READ,
                parameters={},
                handler=lambda arguments, tool=name: (
                    calls.append((tool, dict(arguments))) or ToolResult(True, "success", tool)
                ),
                validator=_identity,
            ))
        agent = AgentOrchestrator(registry)
        for message, server_type in (
            ("测试 Jellyfin 连接", "jellyfin"),
            ("检查 Emby 服务器连接", "emby"),
            ("校验 Jellyfin 配置", "jellyfin"),
            ("诊断 Emby 服务", "emby"),
        ):
            with self.subTest(message=message):
                self.assertTrue(is_media_server_test_message(message))
                response = agent.query(message)
                self.assertEqual(response["tool_call"]["name"], "config.test_media_server")
                self.assertEqual(response["tool_call"]["arguments"], {"server_type": server_type})
        self.assertEqual(agent.query("检查项目配置")["tool_call"]["name"], "config.diagnose")
        self.assertFalse(is_media_server_test_message("Jellyfin 媒体库里有什么"))
        self.assertFalse(is_media_server_test_message("测试 Jellyfin STRM 连接"))
        self.assertIn(("config.test_media_server", {"server_type": "jellyfin"}), calls)


class AgentConfigConnectionAPITests(IsolatedDatabaseTestCase):
    def setUp(self):
        self.agent_gate_patches = (
            patch("app.routes.agent_api.is_agent_enabled", return_value=True),
            patch("app.agent.feature_gate.is_agent_enabled", return_value=True),
        )
        for gate_patch in self.agent_gate_patches:
            gate_patch.start()
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.client = TestClient(create_app(start_background=False), raise_server_exceptions=False)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for gate_patch in reversed(self.agent_gate_patches):
            gate_patch.stop()
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    @staticmethod
    def _token(html: str) -> str:
        match = re.search(r'name="csrf_token"\s+(?:value|content)="([^"]+)"', html)
        if not match:
            match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not match:
            raise AssertionError("CSRF token missing")
        return match.group(1)

    def login(self) -> str:
        token = self._token(self.client.get("/login").text)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)
        return self._token(self.client.get("/settings").text)

    def test_direct_tool_and_natural_language_api_do_not_expose_configuration(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        values = _config_values("jellyfin")
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"ServerName": "Media Room", "Version": "12.0.1"}
        with patch("app.agent.config_actions.config.get_bool", return_value=True), patch(
            "app.agent.config_actions.config.get", side_effect=_config_get(values)
        ), patch("app.agent.config_actions.requests.get", return_value=response):
            direct = self.client.post(
                "/api/agent/tools/config.test_media_server",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"server_type": "jellyfin"}},
            )
            self.assertEqual(direct.status_code, 200, direct.text)
            self.assertEqual(direct.json()["result"]["status"], "success")
            self.assertNotIn("jellyfin.internal", direct.text)
            self.assertNotIn("jellyfin-secret-token", direct.text)

            query = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "测试 Jellyfin 连接"},
            )
            self.assertEqual(query.status_code, 200, query.text)
            self.assertEqual(query.json()["tool_call"]["name"], "config.test_media_server")
            self.assertNotIn("jellyfin.internal", query.text)
            self.assertNotIn("jellyfin-secret-token", query.text)

    def test_direct_tool_is_limited_to_six_requests_per_minute(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        with patch("app.agent.config_actions.config.get_bool", return_value=False):
            for _ in range(6):
                response = self.client.post(
                    "/api/agent/tools/config.test_media_server",
                    headers=headers,
                    json={"session_id": "test_session_identifier_0001", "arguments": {"server_type": "jellyfin"}},
                )
                self.assertEqual(response.status_code, 200, response.text)
            limited = self.client.post(
                "/api/agent/tools/config.test_media_server",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"server_type": "jellyfin"}},
            )
        self.assertEqual(limited.status_code, 429, limited.text)

    def test_direct_and_natural_language_entries_share_one_rate_limit_bucket(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        with patch("app.agent.config_actions.config.get_bool", return_value=False):
            for _ in range(3):
                direct = self.client.post(
                    "/api/agent/tools/config.test_media_server",
                    headers=headers,
                    json={"session_id": "test_session_identifier_0001", "arguments": {"server_type": "jellyfin"}},
                )
                query = self.client.post(
                    "/api/agent/query",
                    headers=headers,
                    json={"session_id": "test_session_identifier_0001", "message": "测试 Jellyfin 连接"},
                )
                self.assertEqual(direct.status_code, 200, direct.text)
                self.assertEqual(query.status_code, 200, query.text)
            limited = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "测试 Jellyfin 连接"},
            )
        self.assertEqual(limited.status_code, 429, limited.text)

    def test_natural_language_test_is_limited_to_six_requests_per_minute(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        with patch("app.agent.config_actions.config.get_bool", return_value=False):
            for _ in range(6):
                response = self.client.post(
                    "/api/agent/query",
                    headers=headers,
                    json={"session_id": "test_session_identifier_0001", "message": "检查 Emby 服务器连接"},
                )
                self.assertEqual(response.status_code, 200, response.text)
            limited = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "检查 Emby 服务器连接"},
            )
        self.assertEqual(limited.status_code, 429, limited.text)


if __name__ == "__main__":
    unittest.main()
