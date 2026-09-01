"""索引器就绪诊断的本地聚合、安全、路由与 API 契约。"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.agent.indexer_readiness_actions import (
    diagnose_indexer_readiness,
    indexer_readiness_arguments,
)
from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import (
    AgentOrchestrator,
    is_indexer_readiness_diagnosis_message,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from app.main import create_app
from app.indexers.models import IndexerCapabilities
from tests.support import IsolatedDatabaseTestCase


class _Adapter:
    def __init__(self, site_id: str, site_name: str, *, pagination: bool, kinds: tuple[str, ...]):
        self.site_id = site_id
        self.site_name = site_name
        self.base_url = f"https://{site_id}.INDEXER_ADAPTER_SECRET.invalid/"
        self.capabilities = IndexerCapabilities(pagination, kinds)
        self.http = SimpleNamespace(cookies={"session": "INDEXER_COOKIE_SECRET"})


class _Registry:
    def __init__(self, adapters: dict[str, _Adapter], *, broken: set[str] | None = None):
        self._adapters = adapters
        self._broken = set(broken or ())

    def ids(self):
        return tuple(self._adapters)

    def get(self, site_id: str):
        if site_id in self._broken:
            raise RuntimeError("INDEXER_EXCEPTION_SECRET /srv/indexer/private")
        return self._adapters[site_id]


class _Service:
    def __init__(self, adapters: dict[str, _Adapter], enabled: set[str], *, broken=None):
        self.registry = _Registry(adapters, broken=broken)
        self.enabled_site_ids = frozenset(enabled)

    async def search(self, *_args, **_kwargs):
        raise AssertionError("readiness diagnosis must not search providers")

    async def search_media(self, *_args, **_kwargs):
        raise AssertionError("readiness diagnosis must not search providers")


class IndexerReadinessUnitTests(IsolatedDatabaseTestCase):
    def test_arguments_reject_extra_fields(self):
        self.assertEqual(indexer_readiness_arguments({}), {})
        with self.assertRaisesRegex(
            AgentToolError, r"^indexer\.diagnose_readiness 不接受参数$"
        ):
            indexer_readiness_arguments({"debug": True})

    def test_disabled_state_does_not_construct_service(self):
        with patch(
            "app.agent.indexer_readiness_actions.config.get_bool", return_value=False
        ), patch(
            "app.agent.indexer_readiness_actions.get_indexer_service"
        ) as service_factory:
            result = diagnose_indexer_readiness({})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "disabled")
        self.assertFalse(result.data["indexer_enabled"])
        self.assertFalse(result.data["network_accessed"])
        self.assertFalse(result.data["filesystem_accessed"])
        service_factory.assert_not_called()

    def test_ready_projection_reports_effective_capabilities_without_network(self):
        service = _Service(
            {
                "nyaa": _Adapter(
                    "nyaa", "Nyaa", pagination=True, kinds=("magnet", "torrent")
                ),
                "search-only": _Adapter(
                    "search-only", "Search-only", pagination=False, kinds=()
                ),
                "sukebei": _Adapter(
                    "sukebei", "Sukebei", pagination=True, kinds=("magnet", "torrent")
                ),
            },
            {"nyaa", "search-only"},
        )
        with patch(
            "app.agent.indexer_readiness_actions.config.get_bool", return_value=True
        ), patch(
            "app.agent.indexer_readiness_actions.get_indexer_service", return_value=service
        ):
            result = diagnose_indexer_readiness({})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "ready")
        self.assertEqual(
            result.data["counts"],
            {"registered": 3, "enabled": 2, "searchable": 2, "downloadable": 1, "attention": 0},
        )
        by_id = {site["site_id"]: site for site in result.data["sites"]}
        self.assertEqual(by_id["nyaa"]["download_kinds"], ["magnet", "torrent"])
        self.assertEqual(by_id["search-only"]["reason"], "search_only")
        self.assertTrue(by_id["search-only"]["search_available"])
        self.assertFalse(by_id["search-only"]["download_available"])
        self.assertTrue(by_id["sukebei"]["sensitive"])
        self.assertFalse(by_id["sukebei"]["enabled"])
        self.assertEqual(result.data["transport"], {
            "mode": "direct", "outbound_proxy_applied": False,
        })

    def test_no_enabled_sites_and_partial_registry_failure_are_explicit(self):
        adapters = {
            "nyaa": _Adapter("nyaa", "Nyaa", pagination=True, kinds=("magnet",)),
            "mikan": _Adapter("mikan", "Mikan", pagination=False, kinds=("magnet",)),
        }
        with patch(
            "app.agent.indexer_readiness_actions.config.get_bool", return_value=True
        ), patch(
            "app.agent.indexer_readiness_actions.get_indexer_service",
            return_value=_Service(adapters, set()),
        ):
            empty = diagnose_indexer_readiness({})
        self.assertEqual(empty.status, "no_enabled_sites")
        self.assertEqual(empty.data["counts"]["enabled"], 0)

        with patch(
            "app.agent.indexer_readiness_actions.config.get_bool", return_value=True
        ), patch(
            "app.agent.indexer_readiness_actions.get_indexer_service",
            return_value=_Service(adapters, {"nyaa", "mikan"}, broken={"mikan"}),
        ):
            partial = diagnose_indexer_readiness({})
        self.assertEqual(partial.status, "attention")
        self.assertEqual(partial.data["counts"]["enabled"], 2)
        self.assertEqual(partial.data["counts"]["attention"], 1)
        mikan = next(site for site in partial.data["sites"] if site["site_id"] == "mikan")
        self.assertEqual(mikan["reason"], "registry_unavailable")
        self.assertFalse(mikan["search_available"])

    def test_output_omits_urls_cookies_config_values_and_raw_errors(self):
        service = _Service(
            {"nyaa": _Adapter("nyaa", "Nyaa", pagination=True, kinds=("magnet",))},
            {"nyaa"},
        )
        with patch(
            "app.agent.indexer_readiness_actions.config.get_bool", return_value=True
        ), patch(
            "app.agent.indexer_readiness_actions.get_indexer_service", return_value=service
        ):
            result = diagnose_indexer_readiness({})
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for secret in (
            "INDEXER_ADAPTER_SECRET", "INDEXER_COOKIE_SECRET", "base_url", "cookies",
            "allowed_hosts", "INDEXER_USER_AGENT",
        ):
            self.assertNotIn(secret, serialized)

        with patch(
            "app.agent.indexer_readiness_actions.config.get_bool", return_value=True
        ), patch(
            "app.agent.indexer_readiness_actions.get_indexer_service",
            side_effect=RuntimeError("INDEXER_EXCEPTION_SECRET /srv/indexer/private"),
        ):
            unavailable = diagnose_indexer_readiness({})
        serialized = json.dumps(unavailable.to_dict(), ensure_ascii=False)
        self.assertFalse(unavailable.ok)
        self.assertEqual(unavailable.status, "unavailable")
        self.assertNotIn("INDEXER_EXCEPTION_SECRET", serialized)
        self.assertNotIn("/srv/indexer/private", serialized)

        with patch(
            "app.agent.indexer_readiness_actions.config.get_bool",
            side_effect=RuntimeError("INDEXER_CONFIG_SECRET /srv/config/private"),
        ):
            unavailable = diagnose_indexer_readiness({})
        serialized = json.dumps(unavailable.to_dict(), ensure_ascii=False)
        self.assertEqual(unavailable.status, "unavailable")
        self.assertIsNone(unavailable.data["indexer_enabled"])
        self.assertNotIn("INDEXER_CONFIG_SECRET", serialized)
        self.assertNotIn("/srv/config/private", serialized)

    def test_registry_metadata_and_natural_language_routing(self):
        capabilities = {item["name"]: item for item in get_agent_service().capabilities()["tools"]}
        spec = capabilities["indexer.diagnose_readiness"]
        self.assertEqual(spec["risk"], "read")
        self.assertFalse(spec["requires_confirmation"])
        self.assertFalse(spec["parameters"]["additionalProperties"])

        for message in (
            "诊断资源站是否就绪",
            "检查资源站健康状态",
            "资源站搜索功能是否可用",
            "多站资源搜索能用吗",
            "查看索引站状态",
            "索引器有异常吗",
            "检查索引站状态",
            "为什么没搜到资源",
            "为什么搜不到资源",
            "资源站连不上怎么办",
            "站点连接失败了",
        ):
            self.assertTrue(is_indexer_readiness_diagnosis_message(message), message)
        for message in (
            "搜索《沙丘2》的资源",
            "全局搜索《资源站》状态",
            "在网上检查资源站",
            "检查资源站配置",
            "开启多站资源搜索",
            "检查索引",
            "索引健康",
        ):
            self.assertFalse(is_indexer_readiness_diagnosis_message(message), message)

        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="indexer.diagnose_readiness",
            description="readiness",
            risk=RiskLevel.READ,
            parameters={},
            handler=lambda arguments: ToolResult(True, "ready", "ready"),
            validator=lambda arguments: arguments,
        ))
        response = AgentOrchestrator(registry).query("检查索引站状态")
        self.assertEqual(response["tool_call"]["name"], "indexer.diagnose_readiness")
        self.assertEqual(response["tool_call"]["arguments"], {})


class IndexerReadinessAPITests(IsolatedDatabaseTestCase):
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
        path = "/api/agent/tools/indexer.diagnose_readiness"
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code, 401)
        csrf = self._login()
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code, 403)
        headers = {"X-CSRF-Token": csrf}

        invalid = self.client.post(path, headers=headers, json={"session_id": "test_session_identifier_0001", "unexpected": 1})
        self.assertEqual(invalid.status_code, 400, invalid.text)
        invalid = self.client.post(path, headers=headers, json={"session_id": "test_session_identifier_0001", "arguments": {"debug": True}})
        self.assertEqual(invalid.status_code, 400, invalid.text)
        agent_rate_limiter.reset()

        service = _Service(
            {"nyaa": _Adapter("nyaa", "Nyaa", pagination=True, kinds=("magnet",))},
            {"nyaa"},
        )
        with patch(
            "app.agent.indexer_readiness_actions.config.get_bool", return_value=True
        ), patch(
            "app.agent.indexer_readiness_actions.get_indexer_service", return_value=service
        ):
            for _ in range(4):
                response = self.client.post(path, headers=headers, json={"session_id": "test_session_identifier_0001", "arguments": {}})
                self.assertEqual(response.status_code, 200, response.text)
            limited = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "诊断资源站是否就绪"},
            )
        self.assertEqual(limited.status_code, 429, limited.text)


if __name__ == "__main__":
    import unittest
    unittest.main()
