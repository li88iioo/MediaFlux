from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import database as db
from app.agent.models import ToolResult
from app.agent.rate_limit import agent_rate_limiter
from app.agent.orchestrator import AgentOrchestrator, is_discovery_search_message, is_web_search_message
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.tools import build_tool_registry
from app.agent.web_search_actions import (
    _map_response,
    _provider_error,
    _search_tavily,
    reset_web_search_cache_for_tests,
    search_web,
    web_search_arguments,
)
from tests.support import IsolatedDatabaseTestCase


def _config(values):
    return lambda key, default="": values.get(key, default)


class WebSearchArgumentTests(unittest.TestCase):
    def test_arguments_are_strict_and_normalized(self):
        self.assertEqual(
            web_search_arguments({"query": "  Jellyfin １２  ", "max_results": 3, "topic": "NEWS", "time_range": "week"}),
            {"query": "Jellyfin 12", "max_results": 3, "topic": "news", "time_range": "week"},
        )
        for value in ({}, {"query": ""}, {"query": "x\ny"}, {"query": "x", "max_results": True}, {"query": "x", "max_results": 11}, {"query": "x", "url": "https://example.com"}):
            with self.subTest(value=value), self.assertRaises(AgentToolError):
                web_search_arguments(value)

    def test_arguments_reject_secret_like_query(self):
        messages = (
            "api_key=abcdefgh123456",
            'api_key="abcdefgh123456"',
            "authorization: Basic dXNlcjpwYXNzd29yZA==",
            "password=$abcdefgh123456",
            "Bearer abcdefghijklmnop",
            "请搜索 sk-abcdefghijklmnopqrstuv",
            "密码：测试凭据",
        )
        for message in messages:
            with self.subTest(message=message), self.assertRaisesRegex(
                AgentToolError, "疑似包含凭据"
            ) as raised:
                web_search_arguments({"query": message})
            self.assertEqual(raised.exception.code, "sensitive_external_input")

    def test_arguments_allow_public_security_document_queries(self):
        messages = (
            "token management best practices",
            "token authentication explained",
            "password security guide",
            "api key rotation guide",
            "secret handling policy",
            "What does Authorization: Basic mean?",
            "Authorization: Basic authentication documentation",
            "api_key=YOUR_API_KEY configuration documentation",
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(
                    web_search_arguments({"query": message})["query"], message
                )

    def test_explicit_route_does_not_steal_discovery_search(self):
        self.assertTrue(is_web_search_message("联网搜索 Jellyfin 12 API"))
        self.assertFalse(is_web_search_message("在网上找《沙丘2》电影"))
        self.assertTrue(is_discovery_search_message("在网上找《沙丘2》电影"))

    def test_registry_exposes_read_tool(self):
        capabilities = {item["name"]: item for item in build_tool_registry().capabilities()}
        self.assertEqual(capabilities["web.search"]["risk"], "read")
        self.assertFalse(capabilities["web.search"]["requires_confirmation"])


class WebSearchDatabaseFacadeCompatibilityTests(unittest.TestCase):
    def test_private_usage_date_validator_remains_available(self):
        self.assertEqual(
            db._validate_agent_web_search_usage_date("2026-08-08"),
            "2026-08-08",
        )
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            db._validate_agent_web_search_usage_date("2026-8-8")


class WebSearchExecutionTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        reset_web_search_cache_for_tests()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_web_search_daily_usage")
        self.values = {
            "WEB_SEARCH_ENABLED": "1", "TAVILY_API_KEY": "secret-key",
            "TAVILY_SEARCH_DEPTH": "basic", "TAVILY_MAX_RESULTS": "5",
            "TAVILY_CACHE_TTL_SECONDS": "900", "TAVILY_DAILY_CREDIT_LIMIT": "2",
        }

    def test_disabled_or_missing_key_never_calls_provider(self):
        with patch("app.agent.web_search_actions.get", side_effect=_config({"WEB_SEARCH_ENABLED": "0"})), patch("app.agent.web_search_actions._search_tavily") as provider:
            self.assertEqual(search_web({"query": "x"}).status, "disabled")
            provider.assert_not_called()
        with patch("app.agent.web_search_actions.get", side_effect=_config({"WEB_SEARCH_ENABLED": "1", "TAVILY_API_KEY": ""})), patch("app.agent.web_search_actions._search_tavily") as provider:
            self.assertEqual(search_web({"query": "x"}).status, "configuration_missing")
            provider.assert_not_called()

    def test_sensitive_query_never_calls_provider_or_consumes_budget(self):
        messages = (
            "access_token: abcdefghijklmnop",
            'api_key="abcdefgh123456"',
            "authorization: Basic dXNlcjpwYXNzd29yZA==",
            "password=$abcdefgh123456",
        )
        with patch(
            "app.agent.web_search_actions.get", side_effect=_config(self.values)
        ), patch("app.agent.web_search_actions._search_tavily") as provider:
            for message in messages:
                with self.subTest(message=message), self.assertRaisesRegex(
                    AgentToolError, "疑似包含凭据"
                ):
                    search_web({"query": message})
        provider.assert_not_called()
        self.assertEqual(
            db.get_agent_web_search_daily_usage(
                provider="tavily", usage_date=date.today().isoformat()
            ),
            0,
        )

    def test_success_is_cached_and_charged_once(self):
        result = ToolResult(True, "ok", "找到 1 条网页结果", data={"results": [{"title": "Demo"}]})
        async def provider(*args, **kwargs):
            return result
        with patch("app.agent.web_search_actions.get", side_effect=_config(self.values)), patch("app.agent.web_search_actions._search_tavily", side_effect=provider) as call:
            first = search_web({"query": "Jellyfin 12", "max_results": 10})
            second = search_web({"query": "Jellyfin 12", "max_results": 10})
        self.assertTrue(first.ok)
        self.assertFalse(first.data["cached"])
        self.assertTrue(second.data["cached"])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(db.get_agent_web_search_daily_usage(provider="tavily", usage_date=date.today().isoformat()), 1)

    def test_daily_budget_fails_closed_before_provider(self):
        self.values["TAVILY_DAILY_CREDIT_LIMIT"] = "1"
        self.assertTrue(db.reserve_agent_web_search_credits(provider="tavily", usage_date=date.today().isoformat(), cost=1, daily_limit=1))
        with patch("app.agent.web_search_actions.get", side_effect=_config(self.values)), patch("app.agent.web_search_actions._search_tavily") as provider:
            result = search_web({"query": "different"})
        self.assertEqual(result.status, "budget_exhausted")
        provider.assert_not_called()

    def test_natural_language_invokes_web_tool_only_for_explicit_marker(self):
        registry = build_tool_registry()
        with patch("app.agent.web_search_actions.get", side_effect=_config({"WEB_SEARCH_ENABLED": "0"})):
            for message in (
                "联网搜索 Jellyfin 12 API",
                "联网搜索如何开启 Jellyfin DLNA",
                "网页搜索开启 CORS 的方法",
            ):
                with self.subTest(message=message):
                    result = AgentOrchestrator(registry).query(message)
                    self.assertEqual(result["tool_call"]["name"], "web.search")

    def test_query_originated_web_search_enforces_tool_budget(self):
        agent_rate_limiter.reset()
        registry = build_tool_registry()
        service = AgentOrchestrator(registry)
        identity = "tg:v1:100\x1f200"
        with patch(
            "app.agent.web_search_actions.get",
            side_effect=_config({"WEB_SEARCH_ENABLED": "0"}),
        ):
            for _ in range(6):
                service.query(
                    "联网搜索 Jellyfin 12 API",
                    query_tool_rate_identity=identity,
                )
            with self.assertRaises(AgentToolError) as raised:
                service.query(
                    "联网搜索 Jellyfin 12 API",
                    query_tool_rate_identity=identity,
                )
        self.assertEqual(raised.exception.code, "rate_limited")
        agent_rate_limiter.reset()

    def test_provider_results_drop_unsafe_urls(self):
        result = _map_response(
            {
                "results": [
                    {"title": "Public", "content": "safe", "url": "https://example.com/page#fragment", "score": 0.8},
                    {"title": "Private", "content": "hidden", "url": "http://127.0.0.1/admin", "score": 1},
                    {"title": "Credentials", "content": "hidden", "url": "https://user:pass@example.com/", "score": 1},
                    {"title": "Port", "content": "hidden", "url": "https://example.com:8443/", "score": 1},
                    {"title": "Script", "content": "hidden", "url": "javascript:alert(1)", "score": 1},
                ]
            },
            {"query": "security", "topic": "general", "max_results": 10},
            12,
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(result.data["results"]), 1)
        self.assertEqual(result.data["results"][0]["url"], "https://example.com/page")

    def test_provider_statuses_have_stable_public_mapping(self):
        self.assertEqual(_provider_error(429).status, "rate_limited")
        self.assertEqual(_provider_error(401).status, "authentication")
        self.assertEqual(_provider_error(503).status, "unavailable")

    def test_provider_timeout_is_safely_mapped(self):
        class TimeoutClient:
            async def post_json(self, *_args, **_kwargs):
                import httpx
                raise httpx.ReadTimeout("private upstream detail")

            async def aclose(self):
                return None

        result = asyncio.run(
            _search_tavily(
                {"query": "Jellyfin", "topic": "general", "max_results": 5},
                api_key="secret-key",
                depth="basic",
                client_factory=lambda **_kwargs: TimeoutClient(),
            )
        )
        self.assertEqual(result.status, "timeout")
        self.assertNotIn("private upstream detail", result.summary)

    def test_tavily_client_pins_validated_address(self):
        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def post_json(self, *_args, **_kwargs):
                return SimpleNamespace(status_code=200, text='{"results": []}')

            async def aclose(self):
                return None

        result = asyncio.run(
            _search_tavily(
                {
                    "query": "Jellyfin 12",
                    "topic": "general",
                    "max_results": 5,
                    "time_range": "",
                },
                api_key="secret-key",
                depth="basic",
                client_factory=FakeClient,
            )
        )
        self.assertTrue(result.ok)
        self.assertTrue(captured["pin_resolved_address"])
        self.assertEqual(captured["allowed_hosts"], {"api.tavily.com"})
