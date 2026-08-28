"""Media Agent 外部影视默认推荐列表测试。"""
from __future__ import annotations

import re
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.agent.discovery_actions import recommend_arguments, recommend_discovery
from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import AgentOrchestrator
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.discovery.models import DiscoveryPage, MediaCard, ProviderHealth, ProviderRateLimited
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


def _identity(arguments):
    return dict(arguments)


def _card(index: int = 1, **overrides) -> MediaCard:
    values = {
        "provider": "tmdb",
        "external_id": str(2000 + index),
        "media_type": "movie",
        "title": f"推荐影片 {index}",
        "original_title": f"Recommended Movie {index}",
        "year": "2026",
        "overview": "安全简介\x00" * 80,
        "poster_key": "https://image.example/poster?token=secret",
        "backdrop_key": "https://image.example/backdrop?api_key=secret",
        "rating": 8.6,
        "rating_source": "tmdb",
        "release_date": "2026-08-01",
        "tmdb_id": str(2000 + index),
        "state": "watchlisted",
    }
    values.update(overrides)
    return MediaCard(**values)


class FakeDiscoveryService:
    def __init__(self, page: DiscoveryPage):
        self.page = page
        self.calls: list[tuple[str, str, str, int, dict]] = []

    def list_items(self, provider, category, media_type, page, filters):
        self.calls.append((provider, category, media_type, page, dict(filters)))
        return self.page


class AgentDiscoveryRecommendTests(unittest.TestCase):
    def test_arguments_normalize_defaults_and_reject_unsafe_fields(self):
        self.assertEqual(
            recommend_arguments({"provider": " ＤＯＵＢＡＮ ", "media_type": " ＴＶ ", "page": 2, "limit": 8}),
            {"provider": "douban", "media_type": "tv", "page": 2, "limit": 8},
        )
        self.assertEqual(
            recommend_arguments({}),
            {"provider": "tmdb", "media_type": "movie", "page": 1, "limit": 10},
        )
        invalid = (
            None,
            {"provider": "bangumi"},
            {"provider": 1},
            {"media_type": "all"},
            {"media_type": False},
            {"page": True},
            {"page": 101},
            {"limit": 0},
            {"limit": 21},
            {"filters": {"sort_by": "vote_average.desc"}},
            {"url": "https://example.invalid"},
            {"token": "secret"},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                recommend_arguments(arguments)  # type: ignore[arg-type]

    def test_disabled_feature_does_not_create_or_call_service(self):
        service = Mock()
        with patch("app.agent.discovery_actions.config.get_bool", return_value=False), patch(
            "app.agent.discovery_actions.get_discovery_service", return_value=service
        ) as getter:
            result = recommend_discovery({})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "disabled")
        getter.assert_not_called()
        service.list_items.assert_not_called()

    def test_tmdb_and_douban_use_fixed_categories_and_safe_allowlist(self):
        for provider, category in (("tmdb", "discover"), ("douban", "recommend")):
            page = DiscoveryPage(
                items=(_card(1, provider=provider), _card(2, provider=provider)),
                page=2,
                has_more=True,
                cached=True,
                stale=True,
                provider=ProviderHealth(name=provider, status="degraded", retry_after=12),
            )
            service = FakeDiscoveryService(page)
            with self.subTest(provider=provider), patch(
                "app.agent.discovery_actions.config.get_bool", return_value=True
            ), patch("app.agent.discovery_actions.get_discovery_service", return_value=service):
                result = recommend_discovery({
                    "provider": provider,
                    "media_type": "movie",
                    "page": 2,
                    "limit": 1,
                })
            self.assertTrue(result.ok)
            self.assertEqual(result.status, "success")
            self.assertEqual(service.calls, [(provider, category, "movie", 2, {})])
            self.assertEqual(result.data["total"], 2)
            self.assertEqual(result.data["returned"], 1)
            self.assertTrue(result.data["has_more"])
            self.assertTrue(result.data["cached"])
            self.assertTrue(result.data["stale"])
            self.assertEqual(result.data["provider_status"], "degraded")
            self.assertEqual(result.data["retry_after"], 12)
            serialized = repr(result.to_dict()).casefold()
            for secret in ("poster_key", "backdrop_key", "watchlisted", "api_key", "token=secret", "\\x00"):
                self.assertNotIn(secret, serialized)

    def test_provider_and_unexpected_errors_are_generic(self):
        class FailingService:
            def __init__(self, error):
                self.error = error

            def list_items(self, *_args, **_kwargs):
                raise self.error

        for error, expected_retry in (
            (ProviderRateLimited("请求受限", retry_after=42, detail="Bearer should-not-leak"), 42),
            (RuntimeError("should-not-leak"), 0),
        ):
            with self.subTest(error=type(error).__name__), patch(
                "app.agent.discovery_actions.config.get_bool", return_value=True
            ), patch(
                "app.agent.discovery_actions.get_discovery_service",
                return_value=FailingService(error),
            ):
                result = recommend_discovery({"provider": "tmdb"})
            self.assertFalse(result.ok)
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(result.data["retry_after"], expected_retry)
            self.assertIn(result.data["provider_status"], {
                "healthy", "degraded", "disabled", "unavailable", "not_configured"
            })
            self.assertNotIn("should-not-leak", repr(result.to_dict()))

    def test_registry_and_natural_language_routing_keep_boundaries(self):
        capabilities = {item["name"]: item for item in build_tool_registry().capabilities()}
        tool = capabilities["discovery.recommend"]
        self.assertEqual(tool["risk"], "read")
        self.assertFalse(tool["parameters"]["additionalProperties"])

        calls: list[tuple[str, dict]] = []
        registry = ToolRegistry()
        for name in ("discovery.recommend", "discovery.search", "indexer.search_resources", "library.search"):
            registry.register(ToolSpec(
                name=name,
                description=name,
                risk=RiskLevel.READ,
                parameters={},
                handler=lambda arguments, tool_name=name: (
                    calls.append((tool_name, dict(arguments))) or ToolResult(True, "success", tool_name)
                ),
                validator=_identity,
            ))
        agent = AgentOrchestrator(registry)
        expected = {
            "给我推荐几部电影": {"provider": "tmdb", "media_type": "movie", "page": 1, "limit": 10},
            "豆瓣推荐电视剧": {"provider": "douban", "media_type": "tv", "page": 1, "limit": 10},
            "最近有什么好看的电影": {"provider": "tmdb", "media_type": "movie", "page": 1, "limit": 10},
            "剧荒了，推荐几部电视剧": {"provider": "tmdb", "media_type": "tv", "page": 1, "limit": 10},
            "推荐几部追剧": {"provider": "tmdb", "media_type": "tv", "page": 1, "limit": 10},
            "推荐几个追番": {"provider": "tmdb", "media_type": "tv", "page": 1, "limit": 10},
        }
        for message, arguments in expected.items():
            with self.subTest(message=message):
                response = agent.query(message)
                self.assertEqual(response["tool_call"], {
                    "name": "discovery.recommend",
                    "arguments": arguments,
                    "elapsed_ms": response["tool_call"]["elapsed_ms"],
                })

        filtered = agent.query("2025 科幻剧推荐")
        self.assertEqual(filtered["tool_call"]["name"], "discovery.search")
        self.assertEqual(filtered["tool_call"]["arguments"], {
            "query": "2025 科幻",
            "page": 1,
            "limit": 20,
            "media_type": "tv",
            "year": "2025",
            "genre": "科幻",
        })
        self.assertEqual(
            agent.query("2025 欧美悬疑电影推荐")["tool_call"]["arguments"],
            {
                "query": "2025 欧美 悬疑",
                "page": 1,
                "limit": 20,
                "media_type": "movie",
                "year": "2025",
                "region": "欧美",
                "genre": "悬疑",
            },
        )

        self.assertEqual(agent.query("在网上找《沙丘2》电影")["tool_call"]["name"], "discovery.search")
        self.assertEqual(
            agent.query("用 TMDB 搜索《黑镜》，推荐类似的")["tool_call"]["name"],
            "discovery.search",
        )
        self.assertEqual(
            agent.query("豆瓣查询《霸王别姬》并推荐")["tool_call"]["name"],
            "discovery.search",
        )
        self.assertEqual(agent.query("搜索《沙丘2》的资源")["tool_call"]["name"], "indexer.search_resources")
        local_response = agent.query("媒体库里推荐我看什么")
        self.assertNotEqual((local_response.get("tool_call") or {}).get("name"), "discovery.recommend")
        contextual_response = agent.query("推荐几部类似《沙丘2》的电影")
        self.assertIsNone(contextual_response.get("tool_call"))
        self.assertEqual(contextual_response["result"]["status"], "unsupported")
        self.assertIn(("discovery.recommend", expected["豆瓣推荐电视剧"]), calls)


class AgentDiscoveryRecommendAPITests(IsolatedDatabaseTestCase):
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

    def test_auth_csrf_direct_and_query(self):
        self.assertIn(self.client.post(
            "/api/agent/tools/discovery.recommend", json={"session_id": "test_session_identifier_0001", "arguments": {}}
        ).status_code, (401, 403))
        csrf = self.login()
        without_csrf = self.client.post(
            "/api/agent/tools/discovery.recommend", json={"session_id": "test_session_identifier_0001", "arguments": {}}
        )
        self.assertIn(without_csrf.status_code, (400, 403))

        service = FakeDiscoveryService(DiscoveryPage(items=(_card(),)))
        with patch("app.agent.discovery_actions.config.get_bool", return_value=True), patch(
            "app.agent.discovery_actions.get_discovery_service", return_value=service
        ):
            direct = self.client.post(
                "/api/agent/tools/discovery.recommend",
                headers={"X-CSRF-Token": csrf},
                json={"session_id": "test_session_identifier_0001", "arguments": {"provider": "tmdb"}},
            )
            query = self.client.post(
                "/api/agent/query",
                headers={"X-CSRF-Token": csrf},
                json={"session_id": "test_session_identifier_0001", "message": "豆瓣推荐电视剧"},
            )
        self.assertEqual(direct.status_code, 200, direct.text)
        self.assertEqual(query.status_code, 200, query.text)
        self.assertEqual(query.json()["tool_call"]["name"], "discovery.recommend")

    def test_direct_and_query_share_six_request_limit(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        service = FakeDiscoveryService(DiscoveryPage())
        with patch("app.agent.discovery_actions.config.get_bool", return_value=True), patch(
            "app.agent.discovery_actions.get_discovery_service", return_value=service
        ):
            for _ in range(5):
                response = self.client.post(
                    "/api/agent/tools/discovery.recommend",
                    headers=headers,
                    json={"session_id": "test_session_identifier_0001", "arguments": {}},
                )
                self.assertEqual(response.status_code, 200, response.text)
            sixth = self.client.post(
                "/api/agent/query", headers=headers, json={"session_id": "test_session_identifier_0001", "message": "推荐几部电影"}
            )
            limited = self.client.post(
                "/api/agent/query", headers=headers, json={"session_id": "test_session_identifier_0001", "message": "推荐几部电影"}
            )
        self.assertEqual(sixth.status_code, 200, sixth.text)
        self.assertEqual(limited.status_code, 429, limited.text)


if __name__ == "__main__":
    unittest.main()
