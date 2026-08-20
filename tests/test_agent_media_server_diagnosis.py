"""Media Agent 媒体服务器版本与兼容槽位诊断。"""
from __future__ import annotations

import json
import re
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.agent.media_server_actions import (
    diagnose_media_servers,
    media_server_diagnosis_arguments,
)
from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import AgentOrchestrator, is_media_server_diagnosis_message
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


def _probe(
    slot: str,
    *,
    status: str = "success",
    product: str = "Jellyfin",
    version: str = "12.0.1",
    latency_ms: object = 7,
    product_detected: bool = True,
) -> ToolResult:
    ok = status == "success"
    data = {"server_type": slot, "connection_status": status}
    if ok:
        data.update({
            "product": product,
            "product_detected": product_detected,
            "version": version,
            "latency_ms": latency_ms,
        })
    return ToolResult(ok=ok, status=status, summary=status, data=data, error="" if ok else status)


class AgentMediaServerDiagnosisTests(unittest.TestCase):
    def test_arguments_are_strict(self):
        self.assertEqual(media_server_diagnosis_arguments({}), {})
        for arguments in ({"debug": True}, {"url": "http://attacker.invalid"}, {"token": "secret"}):
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                media_server_diagnosis_arguments(arguments)

    def test_disabled_nodes_return_fixed_safe_shape_without_network_claim(self):
        with patch(
            "app.agent.media_server_actions._probe_all",
            return_value=[_probe("jellyfin", status="disabled"), _probe("emby", status="disabled")],
        ):
            result = diagnose_media_servers({})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "not_configured")
        self.assertFalse(result.data["network_accessed"])
        self.assertEqual(result.data["counts"], {
            "enabled": 0, "configured": 0, "online": 0, "compatible": 0, "attention": 0,
        })
        self.assertEqual([node["slot"] for node in result.data["nodes"]], ["jellyfin", "emby"])
        self.assertTrue(all(node["compatibility"] == "disabled" for node in result.data["nodes"]))

    def test_supported_jellyfin12_emby_and_legacy_jellyfin_are_classified(self):
        cases = (
            (
                [_probe("jellyfin", product="Jellyfin", version="12.1.0"), _probe("emby", status="disabled")],
                "jellyfin12_slot_compatible",
            ),
            (
                [_probe("jellyfin", status="disabled"), _probe("emby", product="Emby", version="4.9.1")],
                "emby_legacy_slot_compatible",
            ),
            (
                [_probe("jellyfin", status="disabled"), _probe("emby", product="Jellyfin", version="10.11.11")],
                "jellyfin10_legacy_slot_compatible",
            ),
        )
        for probes, reason in cases:
            with self.subTest(reason=reason), patch(
                "app.agent.media_server_actions._probe_all", return_value=probes
            ):
                result = diagnose_media_servers({})
            self.assertTrue(result.ok)
            self.assertEqual(result.status, "healthy")
            node = next(item for item in result.data["nodes"] if item["online"])
            self.assertEqual(node["compatibility"], "compatible")
            self.assertEqual(node["reason_code"], reason)

    def test_wrong_slot_and_unknown_version_require_attention(self):
        cases = (
            (
                [_probe("jellyfin", product="Jellyfin", version="10.10.7"), _probe("emby", status="disabled")],
                "use_legacy_slot",
            ),
            (
                [_probe("jellyfin", status="disabled"), _probe("emby", product="Jellyfin", version="12.0.0")],
                "use_jellyfin12_slot",
            ),
            (
                [_probe("jellyfin", product="Jellyfin", version="token=other-secret"), _probe("emby", status="disabled")],
                "version_unrecognized",
            ),
            (
                [_probe("jellyfin", status="disabled"), _probe("emby", product="Jellyfin", version="9.1.0")],
                "jellyfin_major_not_classified",
            ),
        )
        for probes, reason in cases:
            with self.subTest(reason=reason), patch(
                "app.agent.media_server_actions._probe_all", return_value=probes
            ):
                result = diagnose_media_servers({})
            self.assertTrue(result.ok)
            self.assertEqual(result.status, "attention")
            node = next(item for item in result.data["nodes"] if item["online"])
            self.assertEqual(node["reason_code"], reason)
            if reason == "version_unrecognized":
                self.assertIsNone(node["version"])
                self.assertNotIn("other-secret", json.dumps(result.to_dict(), ensure_ascii=False))

    def test_unverified_product_identity_never_fails_open_from_slot_fallback(self):
        with patch(
            "app.agent.media_server_actions._probe_all",
            return_value=[
                _probe(
                    "jellyfin",
                    product="Jellyfin",
                    product_detected=False,
                    version="12.0.1",
                ),
                _probe("emby", status="disabled"),
            ],
        ):
            result = diagnose_media_servers({})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "attention")
        node = result.data["nodes"][0]
        self.assertEqual(node["product"], "unknown")
        self.assertEqual(node["compatibility"], "review")
        self.assertEqual(node["reason_code"], "product_unrecognized")

    def test_connection_failures_are_fixed_and_do_not_leak_probe_payload(self):
        secret_result = ToolResult(
            ok=False,
            status="connection",
            summary="http://192.168.1.9/?token=secret",
            data={
                "server_type": "jellyfin",
                "connection_status": "connection",
                "server_name": "Authorization: Bearer secret",
                "url": "http://192.168.1.9",
            },
            error="secret exception",
        )
        with patch(
            "app.agent.media_server_actions._probe_all",
            return_value=[secret_result, _probe("emby", status="disabled")],
        ):
            result = diagnose_media_servers({})

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for forbidden in ("192.168.1.9", "Bearer", "secret", "url", "server_name"):
            self.assertNotIn(forbidden, serialized)
        node = result.data["nodes"][0]
        self.assertEqual(node["reason_code"], "connection_failed")
        self.assertFalse(node["online"])

    def test_version_suffixes_are_not_reflected(self):
        for unsafe in ("12.0.0+tokensecret123", "12.0.0-secretvalue"):
            with self.subTest(version=unsafe), patch(
                "app.agent.media_server_actions._probe_all",
                return_value=[_probe("jellyfin", version=unsafe), _probe("emby", status="disabled")],
            ):
                result = diagnose_media_servers({})
            serialized = json.dumps(result.to_dict(), ensure_ascii=False)
            self.assertEqual(result.data["nodes"][0]["reason_code"], "version_unrecognized")
            self.assertIsNone(result.data["nodes"][0]["version"])
            self.assertNotIn(unsafe, serialized)

    def test_busy_and_unexpected_probe_failures_use_safe_fixed_results(self):
        semaphore = Mock()
        semaphore.acquire.return_value = False
        with patch("app.agent.media_server_actions._DIAGNOSTIC_SEMAPHORE", semaphore):
            busy = diagnose_media_servers({})
        self.assertEqual(busy.status, "unavailable")
        self.assertFalse(busy.data["network_accessed"])
        semaphore.release.assert_not_called()

        with patch(
            "app.agent.media_server_actions._probe_all",
            side_effect=RuntimeError("secret http://192.168.1.9"),
        ):
            failed = diagnose_media_servers({})
        self.assertEqual(failed.status, "unavailable")
        self.assertTrue(failed.data["network_accessed"])
        serialized = json.dumps(failed.to_dict(), ensure_ascii=False)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("192.168.1.9", serialized)

    def test_version_and_latency_are_strictly_bounded(self):
        with patch(
            "app.agent.media_server_actions._probe_all",
            return_value=[
                _probe("jellyfin", version="12.0.1", latency_ms=999999),
                _probe("emby", product="Emby", version="4.9.0", latency_ms=True),
            ],
        ):
            result = diagnose_media_servers({})
        self.assertEqual(result.data["nodes"][0]["latency_ms"], 11_500)
        self.assertIsNone(result.data["nodes"][1]["latency_ms"])

    def test_registry_metadata_and_natural_language_routing(self):
        capabilities = {item["name"]: item for item in get_agent_service().capabilities()["tools"]}
        spec = capabilities["config.diagnose_media_servers"]
        self.assertEqual(spec["risk"], "read")
        self.assertFalse(spec["requires_confirmation"])
        self.assertFalse(spec["parameters"]["additionalProperties"])

        positives = (
            "检查媒体服务器状态",
            "我的 Jellyfin 版本兼容吗",
            "Jellyfin 12 配置对了吗",
            "Emby / Jellyfin 10.x 节点能用吗",
            "诊断 Jellyfin 和 Emby 节点",
        )
        negatives = (
            "测试 Jellyfin 连接",
            "检查 Emby 服务器连接",
            "Emby 服务能用吗",
            "Jellyfin 服务正常吗",
            "Jellyfin 媒体库里有什么",
            "Jellyfin 媒体库的版本",
            "检查 Jellyfin 电影版本",
            "检查 Jellyfin STRM 服务",
            "搜索 Jellyfin 资源",
            "关闭 Jellyfin",
        )
        for message in positives:
            self.assertTrue(is_media_server_diagnosis_message(message), message)
        for message in negatives:
            self.assertFalse(is_media_server_diagnosis_message(message), message)

        calls: list[tuple[str, dict]] = []
        registry = ToolRegistry()
        for name in (
            "config.diagnose_media_servers", "config.test_media_server", "config.diagnose",
            "strm.diagnose", "indexer.search_resources",
        ):
            registry.register(ToolSpec(
                name=name,
                description=name,
                risk=RiskLevel.READ,
                parameters={},
                handler=lambda arguments, tool=name: (
                    calls.append((tool, dict(arguments))) or ToolResult(True, "success", tool)
                ),
                validator=lambda arguments: arguments,
            ))
        agent = AgentOrchestrator(registry)
        for message in positives:
            self.assertEqual(agent.query(message)["tool_call"]["name"], "config.diagnose_media_servers")
        self.assertEqual(agent.query("测试 Jellyfin 连接")["tool_call"]["name"], "config.test_media_server")
        self.assertEqual(agent.query("Emby 服务能用吗")["tool_call"]["name"], "config.test_media_server")
        self.assertEqual(agent.query("Jellyfin 服务正常吗")["tool_call"]["name"], "config.test_media_server")
        self.assertEqual(agent.query("检查项目配置")["tool_call"]["name"], "config.diagnose")
        self.assertEqual(agent.query("检查媒体服务器配置")["tool_call"]["name"], "config.diagnose")
        self.assertEqual(agent.query("诊断 Jellyfin STRM 服务")["tool_call"]["name"], "strm.diagnose")
        self.assertEqual(agent.query("搜索 Jellyfin 资源")["tool_call"]["name"], "indexer.search_resources")
        self.assertIn(("config.diagnose_media_servers", {}), calls)


class AgentMediaServerDiagnosisAPITests(IsolatedDatabaseTestCase):
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

    def test_api_auth_csrf_strict_body_and_shared_direct_query_rate_limit(self):
        path = "/api/agent/tools/config.diagnose_media_servers"
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code, 401)
        csrf = self._login()
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code, 403)
        headers = {"X-CSRF-Token": csrf}

        invalid = self.client.post(path, headers=headers, json={"session_id": "test_session_identifier_0001", "unexpected": 1})
        self.assertEqual(invalid.status_code, 400, invalid.text)
        invalid = self.client.post(path, headers=headers, json={"session_id": "test_session_identifier_0001", "arguments": {"debug": True}})
        self.assertEqual(invalid.status_code, 400, invalid.text)
        agent_rate_limiter.reset()

        for _ in range(4):
            response = self.client.post(path, headers=headers, json={"session_id": "test_session_identifier_0001", "arguments": {}})
            self.assertEqual(response.status_code, 200, response.text)
        limited = self.client.post(
            "/api/agent/query",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "message": "检查媒体服务器状态"},
        )
        self.assertEqual(limited.status_code, 429, limited.text)


if __name__ == "__main__":
    unittest.main()
