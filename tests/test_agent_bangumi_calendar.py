"""Media Agent Bangumi 放送日历测试。"""
from __future__ import annotations

import re
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.agent.discovery_actions import bangumi_calendar, calendar_arguments
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
        "provider": "bangumi",
        "external_id": str(3000 + index),
        "media_type": "tv",
        "title": f"放送动画 {index}",
        "original_title": f"Calendar Anime {index}",
        "year": "2026",
        "overview": "安全简介\x00" * 80,
        "poster_key": "https://image.example/poster?token=secret",
        "backdrop_key": "https://image.example/backdrop?api_key=secret",
        "rating": 8.2,
        "rating_source": "bangumi",
        "release_date": "2026-08-01",
        "bangumi_id": str(3000 + index),
        "weekday": 6,
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


class AgentBangumiCalendarTests(unittest.TestCase):
    def test_arguments_default_and_fail_closed(self):
        self.assertEqual(calendar_arguments({}), {"page": 1, "limit": 10})
        self.assertEqual(
            calendar_arguments({"weekday": 7, "page": 2, "limit": 8}),
            {"weekday": 7, "page": 2, "limit": 8},
        )
        invalid = (
            None,
            {"weekday": None},
            {"page": None},
            {"limit": None},
            {"weekday": True},
            {"weekday": "6"},
            {"weekday": 0},
            {"weekday": 8},
            {"page": False},
            {"page": 101},
            {"limit": 0},
            {"limit": 21},
            {"provider": "bangumi"},
            {"category": "today"},
            {"url": "https://example.invalid"},
            {"token": "secret"},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                calendar_arguments(arguments)  # type: ignore[arg-type]

    def test_disabled_feature_does_not_create_service(self):
        service = Mock()
        with patch("app.agent.discovery_actions.config.get_bool", return_value=False), patch(
            "app.agent.discovery_actions.get_discovery_service", return_value=service
        ) as getter:
            result = bangumi_calendar({"weekday": 6})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "disabled")
        getter.assert_not_called()
        service.list_items.assert_not_called()

    def test_fixed_service_mapping_and_safe_public_payload(self):
        page = DiscoveryPage(
            items=(_card(1), _card(2, weekday=7)),
            page=2,
            has_more=True,
            cached=True,
            stale=True,
            provider=ProviderHealth(name="bangumi", status="degraded", retry_after=12),
        )
        service = FakeDiscoveryService(page)
        with patch("app.agent.discovery_actions.config.get_bool", return_value=True), patch(
            "app.agent.discovery_actions.get_discovery_service", return_value=service
        ):
            result = bangumi_calendar({"weekday": 6, "page": 2, "limit": 1})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "success")
        self.assertEqual(service.calls, [("bangumi", "calendar", "tv", 2, {"weekday": "6"})])
        self.assertEqual(result.data["weekday"], 6)
        self.assertEqual(result.data["total"], 2)
        self.assertEqual(result.data["returned"], 1)
        self.assertTrue(result.data["has_more"])
        self.assertTrue(result.data["cached"])
        self.assertTrue(result.data["stale"])
        self.assertEqual(result.data["provider_status"], "degraded")
        self.assertEqual(result.data["retry_after"], 12)
        self.assertEqual(result.data["items"][0]["weekday"], 6)
        serialized = repr(result.to_dict()).casefold()
        for secret in ("poster_key", "backdrop_key", "watchlisted", "api_key", "token=secret", "\\x00"):
            self.assertNotIn(secret, serialized)

    def test_all_week_uses_empty_filters_and_empty_status(self):
        service = FakeDiscoveryService(DiscoveryPage(items=(), page=1))
        with patch("app.agent.discovery_actions.config.get_bool", return_value=True), patch(
            "app.agent.discovery_actions.get_discovery_service", return_value=service
        ):
            result = bangumi_calendar({})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "empty")
        self.assertEqual(service.calls, [("bangumi", "calendar", "tv", 1, {})])
        self.assertIsNone(result.data["weekday"])

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
                result = bangumi_calendar({"weekday": 6})
            self.assertFalse(result.ok)
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(result.data["retry_after"], expected_retry)
            self.assertNotIn("should-not-leak", repr(result.to_dict()))

    def test_registry_and_natural_language_routing(self):
        capabilities = {item["name"]: item for item in build_tool_registry().capabilities()}
        tool = capabilities["bangumi.calendar"]
        self.assertEqual(tool["risk"], "read")
        self.assertFalse(tool["parameters"]["additionalProperties"])
        self.assertEqual(tool["parameters"]["properties"]["weekday"]["maximum"], 7)

        calls: list[tuple[str, dict]] = []
        registry = ToolRegistry()
        for name in (
            "bangumi.calendar",
            "discovery.recommend",
            "discovery.search",
            "indexer.search_resources",
            "library.search",
        ):
            registry.register(ToolSpec(
                name=name,
                description=name,
                risk=RiskLevel.READ,
                parameters={},
                handler=lambda arguments, tool_name=name: (
                    calls.append((tool_name, dict(arguments)))
                    or ToolResult(True, "success", tool_name)
                ),
                validator=_identity,
            ))
        agent = AgentOrchestrator(registry)
        expected = {
            "今天有什么番剧": {"weekday": 6, "page": 1, "limit": 10},
            "今日新番": {"weekday": 6, "page": 1, "limit": 10},
            "查看 Bangumi 今日放送": {"weekday": 6, "page": 1, "limit": 10},
            "周三放送的动画": {"weekday": 3, "page": 1, "limit": 10},
            "Bangumi 番剧日历": {"page": 1, "limit": 10},
            "这周有哪些新番放送": {"page": 1, "limit": 10},
            "推荐今天放送的番剧": {"weekday": 6, "page": 1, "limit": 10},
        }
        with patch("app.agent.orchestrator._today_iso_weekday", return_value=6):
            for message, arguments in expected.items():
                with self.subTest(message=message):
                    response = agent.query(message)
                    self.assertEqual(response["tool_call"]["name"], "bangumi.calendar")
                    self.assertEqual(response["tool_call"]["arguments"], arguments)

        self.assertEqual(agent.query("Bangumi 搜《孤独摇滚》")["tool_call"]["name"], "discovery.search")
        self.assertEqual(agent.query("Bangumi 搜 周一的丰满")["tool_call"]["name"], "discovery.search")
        self.assertEqual(agent.query("找《周一的丰满》动画")["tool_call"]["name"], "library.search")
        self.assertEqual(agent.query("找《孤独摇滚》的种子")["tool_call"]["name"], "indexer.search_resources")
        self.assertNotEqual(
            (agent.query("Jellyfin 媒体库今天入库的动画").get("tool_call") or {}).get("name"),
            "bangumi.calendar",
        )
        self.assertNotEqual(
            (agent.query("推荐几部番剧").get("tool_call") or {}).get("name"),
            "bangumi.calendar",
        )
        self.assertIn(("bangumi.calendar", expected["今天有什么番剧"]), calls)


class AgentBangumiCalendarAPITests(IsolatedDatabaseTestCase):
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
        self.assertIn(
            self.client.post("/api/agent/tools/bangumi.calendar", json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code,
            (401, 403),
        )
        csrf = self.login()
        without_csrf = self.client.post("/api/agent/tools/bangumi.calendar", json={"session_id": "test_session_identifier_0001", "arguments": {}})
        self.assertIn(without_csrf.status_code, (400, 403))

        service = FakeDiscoveryService(DiscoveryPage(items=(_card(),)))
        with patch("app.agent.discovery_actions.config.get_bool", return_value=True), patch(
            "app.agent.discovery_actions.get_discovery_service", return_value=service
        ), patch("app.agent.orchestrator._today_iso_weekday", return_value=6):
            direct = self.client.post(
                "/api/agent/tools/bangumi.calendar",
                headers={"X-CSRF-Token": csrf},
                json={"session_id": "test_session_identifier_0001", "arguments": {"weekday": 6}},
            )
            query = self.client.post(
                "/api/agent/query",
                headers={"X-CSRF-Token": csrf},
                json={"session_id": "test_session_identifier_0001", "message": "今天有什么番剧"},
            )
        self.assertEqual(direct.status_code, 200, direct.text)
        self.assertEqual(query.status_code, 200, query.text)
        self.assertEqual(query.json()["tool_call"]["name"], "bangumi.calendar")

    def test_direct_and_query_share_six_request_limit(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        service = FakeDiscoveryService(DiscoveryPage())
        with patch("app.agent.discovery_actions.config.get_bool", return_value=True), patch(
            "app.agent.discovery_actions.get_discovery_service", return_value=service
        ), patch("app.agent.orchestrator._today_iso_weekday", return_value=6):
            for _ in range(5):
                response = self.client.post(
                    "/api/agent/tools/bangumi.calendar", headers=headers, json={"session_id": "test_session_identifier_0001", "arguments": {}}
                )
                self.assertEqual(response.status_code, 200, response.text)
            sixth = self.client.post(
                "/api/agent/query", headers=headers, json={"session_id": "test_session_identifier_0001", "message": "今天有什么番剧"}
            )
            limited = self.client.post(
                "/api/agent/query", headers=headers, json={"session_id": "test_session_identifier_0001", "message": "今天有什么番剧"}
            )
        self.assertEqual(sixth.status_code, 200, sixth.text)
        self.assertEqual(limited.status_code, 429, limited.text)


if __name__ == "__main__":
    unittest.main()
