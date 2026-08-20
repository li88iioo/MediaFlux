"""Media Agent 外部影视探索搜索测试。"""
from __future__ import annotations

import re
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.agent.discovery_actions import search_arguments, search_discovery
from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import AgentOrchestrator
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.discovery.models import MediaCard
from app.discovery.search import DiscoverySearchResult
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase

_SECRET_POSTER = "https://image.example/private/poster?api_key=secret"
_SECRET_BACKDROP = "https://image.example/private/backdrop?token=secret"


def _identity(arguments):
    return dict(arguments)


def _card(index: int = 1, **overrides) -> MediaCard:
    values = {
        "provider": "tmdb",
        "external_id": str(1000 + index),
        "media_type": "movie",
        "title": f"示例影片 {index}",
        "original_title": f"Demo Movie {index}",
        "year": "2026",
        "overview": "剧情简介" * 180,
        "poster_key": _SECRET_POSTER,
        "backdrop_key": _SECRET_BACKDROP,
        "rating": 8.2,
        "rating_source": "tmdb",
        "release_date": "2026-08-01",
        "tmdb_id": str(1000 + index),
    }
    values.update(overrides)
    return MediaCard(**values)


def _result(
    *,
    items=(),
    attempted=("tmdb",),
    succeeded=("tmdb",),
    errors=(),
    has_more=False,
) -> DiscoverySearchResult:
    return DiscoverySearchResult(
        query="沙丘2",
        page=1,
        items=tuple(items),
        has_more=has_more,
        providers_attempted=tuple(attempted),
        providers_succeeded=tuple(succeeded),
        errors=tuple(errors),
    )


class FakeDiscoverySearchService:
    def __init__(self, result: DiscoverySearchResult):
        self.result = result
        self.calls: list[tuple[str, int, list[str] | None]] = []

    def search(self, query: str, page: int, providers):
        self.calls.append((query, page, providers))
        return self.result


class AgentDiscoverySearchTests(unittest.TestCase):
    def test_arguments_normalize_and_reject_unsafe_fields(self):
        self.assertEqual(
            search_arguments({
                "query": "  沙丘２  ",
                "page": 2,
                "providers": ["TMDB", "tmdb", "Bangumi"],
                "limit": 10,
            }),
            {
                "query": "沙丘2",
                "page": 2,
                "providers": ["tmdb", "bangumi"],
                "limit": 10,
            },
        )
        invalid = (
            {},
            {"query": ""},
            {"query": "x\ny"},
            {"query": "x", "page": True},
            {"query": "x", "page": 101},
            {"query": "x", "limit": 0},
            {"query": "x", "providers": []},
            {"query": "x", "providers": ["unknown"]},
            {"query": "x", "url": "https://example.invalid"},
            {"query": "x", "token": "secret"},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                search_arguments(arguments)

    def test_disabled_feature_does_not_create_or_call_service(self):
        service = Mock()
        with patch("app.agent.discovery_actions.config.get_bool", return_value=False), patch(
            "app.agent.discovery_actions.get_discovery_search_service",
            return_value=service,
        ) as getter:
            result = search_discovery({"query": "沙丘2"})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "disabled")
        getter.assert_not_called()
        service.search.assert_not_called()

    def test_success_uses_safe_allowlist_and_limit(self):
        service = FakeDiscoverySearchService(_result(items=(_card(1), _card(2)), has_more=True))
        with patch("app.agent.discovery_actions.config.get_bool", return_value=True), patch(
            "app.agent.discovery_actions.get_discovery_search_service", return_value=service
        ):
            result = search_discovery({
                "query": "沙丘2",
                "page": 1,
                "providers": ["tmdb"],
                "limit": 1,
            })
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "success")
        self.assertEqual(service.calls, [("沙丘2", 1, ["tmdb"])])
        self.assertEqual(result.data["total"], 2)
        self.assertEqual(result.data["returned"], 1)
        self.assertEqual(len(result.data["items"][0]["overview"]), 500)
        serialized = repr(result.to_dict())
        self.assertNotIn("poster_key", serialized)
        self.assertNotIn("backdrop_key", serialized)
        self.assertNotIn(_SECRET_POSTER, serialized)
        self.assertNotIn(_SECRET_BACKDROP, serialized)

    def test_error_retry_after_is_bounded(self):
        result = _result(
            attempted=("tmdb",),
            succeeded=(),
            errors=({"provider": "tmdb", "code": "rate_limited", "retry_after": 999999},),
        )
        service = FakeDiscoverySearchService(result)
        with patch("app.agent.discovery_actions.config.get_bool", return_value=True), patch(
            "app.agent.discovery_actions.get_discovery_search_service", return_value=service
        ):
            response = search_discovery({"query": "沙丘2"})
        self.assertEqual(response.data["errors"][0]["retry_after"], 86400)

    def test_partial_empty_and_full_failure_semantics(self):
        cases = (
            (
                _result(
                    items=(_card(),),
                    attempted=("tmdb", "douban"),
                    succeeded=("tmdb",),
                    errors=({
                        "provider": "douban",
                        "code": "unavailable",
                        "message": "token=message-should-not-leak",
                        "retry_after": 0,
                        "detail": "token=should-not-leak",
                    },),
                ),
                True,
                "partial",
            ),
            (_result(items=()), True, "empty"),
            (
                _result(
                    items=(),
                    attempted=("tmdb",),
                    succeeded=(),
                    errors=({
                        "provider": "tmdb",
                        "code": "authentication",
                        "message": "api_key=message-should-not-leak",
                        "retry_after": 0,
                        "detail": "api_key=should-not-leak",
                    },),
                ),
                False,
                "unavailable",
            ),
        )
        for raw, ok, status in cases:
            with self.subTest(status=status), patch(
                "app.agent.discovery_actions.config.get_bool", return_value=True
            ), patch(
                "app.agent.discovery_actions.get_discovery_search_service",
                return_value=FakeDiscoverySearchService(raw),
            ):
                result = search_discovery({"query": "沙丘2"})
            self.assertEqual(result.ok, ok)
            self.assertEqual(result.status, status)
            self.assertNotIn("should-not-leak", repr(result.to_dict()))

    def test_registry_and_natural_language_routing_keep_boundaries(self):
        capabilities = {item["name"]: item for item in build_tool_registry().capabilities()}
        self.assertEqual(capabilities["discovery.search"]["risk"], "read")

        calls: list[tuple[str, dict]] = []
        registry = ToolRegistry()
        for name in ("discovery.search", "indexer.search_resources", "library.search"):
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

        for message in (
            "在网上找《沙丘2》电影",
            "用 TMDB 搜《黑镜》",
            "豆瓣查找《霸王别姬》",
            "豆瓣查询霸王别姬",
            "Bangumi 搜《孤独摇滚》",
        ):
            with self.subTest(message=message):
                self.assertEqual(agent.query(message)["tool_call"]["name"], "discovery.search")
        self.assertEqual(
            agent.query("搜索《沙丘2》的资源")["tool_call"]["name"],
            "indexer.search_resources",
        )
        self.assertEqual(
            agent.query("媒体库里有没有《沙丘2》")["tool_call"]["name"],
            "library.search",
        )
        self.assertEqual(agent.query("帮我找《沙丘2》")["tool_call"]["name"], "library.search")
        local_cases = {
            "媒体库中是否存在沙丘2": "沙丘2",
            "Jellyfin 里有黑镜吗": "黑镜",
            "本地库是否有沙丘2": "沙丘2",
            "库里能找到沙丘2吗": "沙丘2",
            "沙丘2是否存在": "沙丘2",
            "沙丘2在不在 Jellyfin": "沙丘2",
            "沙丘2是否在媒体库里": "沙丘2",
            "沙丘2存在于媒体库吗": "沙丘2",
            "沙丘2在 Jellyfin 里吗": "沙丘2",
            "在我的库里沙丘2是否存在": "沙丘2",
        }
        for message, expected_query in local_cases.items():
            with self.subTest(message=message):
                response = agent.query(message)
                self.assertEqual(response["tool_call"]["name"], "library.search")
                self.assertEqual(response["tool_call"]["arguments"], {
                    "query": expected_query,
                    "limit": 8,
                })
        self.assertEqual(
            agent.query("库里有没有《沙丘2》的资源")["tool_call"]["name"],
            "indexer.search_resources",
        )
        self.assertIn(("discovery.search", {"query": "沙丘2", "limit": 20}), calls)


class AgentDiscoverySearchAPITests(IsolatedDatabaseTestCase):
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

    def test_direct_tool_and_natural_language_api(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        service = FakeDiscoverySearchService(_result(items=(_card(),)))
        with patch("app.agent.discovery_actions.config.get_bool", return_value=True), patch(
            "app.agent.discovery_actions.get_discovery_search_service", return_value=service
        ):
            direct = self.client.post(
                "/api/agent/tools/discovery.search",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"query": "沙丘2", "providers": ["tmdb"]}},
            )
            self.assertEqual(direct.status_code, 200, direct.text)
            self.assertEqual(direct.json()["result"]["status"], "success")
            self.assertNotIn("poster", direct.text.casefold())

            query = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "在网上找《沙丘2》电影"},
            )
            self.assertEqual(query.status_code, 200, query.text)
            self.assertEqual(query.json()["tool_call"]["name"], "discovery.search")

    def test_direct_tool_is_limited_to_six_requests_per_minute(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        service = FakeDiscoverySearchService(_result(items=()))
        with patch("app.agent.discovery_actions.config.get_bool", return_value=True), patch(
            "app.agent.discovery_actions.get_discovery_search_service", return_value=service
        ):
            for _ in range(6):
                response = self.client.post(
                    "/api/agent/tools/discovery.search",
                    headers=headers,
                    json={"session_id": "test_session_identifier_0001", "arguments": {"query": "沙丘2"}},
                )
                self.assertEqual(response.status_code, 200, response.text)
            limited = self.client.post(
                "/api/agent/tools/discovery.search",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"query": "沙丘2"}},
            )
        self.assertEqual(limited.status_code, 429, limited.text)


if __name__ == "__main__":
    unittest.main()
