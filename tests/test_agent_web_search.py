from __future__ import annotations

import asyncio
import hashlib
import threading
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from app import database as db
from app.agent.errors import AgentToolError
from app.agent.models import ToolResult
from app.agent.rate_limit import AgentRateLimiter
from app.agent.web_request_singleflight import web_request_singleflight
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
            web_search_arguments(
                {
                    "query": "  Jellyfin １２  ",
                    "max_results": 3,
                    "topic": "NEWS",
                    "time_range": "week",
                }
            ),
            {
                "query": "Jellyfin 12",
                "max_results": 3,
                "topic": "news",
                "time_range": "week",
            },
        )
        for value in (
            {},
            {"query": ""},
            {"query": "x\ny"},
            {"query": "x", "max_results": True},
            {"query": "x", "max_results": 11},
            {"query": "x", "url": "https://example.com"},
        ):
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
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(AgentToolError, "疑似包含凭据") as raised,
            ):
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


class WebSearchDatabaseFacadeCompatibilityTests(unittest.TestCase):
    def test_shared_usage_date_uses_the_local_calendar_day(self):
        with patch("app.repositories.agent_web_search.date") as local_date:
            local_date.today.return_value = date(2026, 9, 5)
            self.assertEqual(
                db.current_agent_web_search_usage_date(),
                "2026-09-05",
            )

    def test_private_usage_date_validator_remains_available(self):
        self.assertEqual(
            db._validate_agent_web_search_usage_date("2026-08-08"), "2026-08-08"
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
            "WEB_SEARCH_ENABLED": "1",
            "TAVILY_API_KEY": "secret-key",
            "TAVILY_SEARCH_DEPTH": "basic",
            "TAVILY_MAX_RESULTS": "5",
            "TAVILY_CACHE_TTL_SECONDS": "900",
            "TAVILY_DAILY_CREDIT_LIMIT": "2",
        }

    def test_disabled_or_missing_key_never_calls_provider(self):
        with (
            patch(
                "app.agent.web_search_actions.get",
                side_effect=_config({"WEB_SEARCH_ENABLED": "0"}),
            ),
            patch("app.agent.web_search_actions._search_tavily") as provider,
        ):
            self.assertEqual(search_web({"query": "x"}).status, "disabled")
            provider.assert_not_called()
        with (
            patch(
                "app.agent.web_search_actions.get",
                side_effect=_config({"WEB_SEARCH_ENABLED": "1", "TAVILY_API_KEY": ""}),
            ),
            patch("app.agent.web_search_actions._search_tavily") as provider,
        ):
            self.assertEqual(search_web({"query": "x"}).status, "configuration_missing")
            provider.assert_not_called()

    def test_sensitive_query_never_calls_provider_or_consumes_budget(self):
        messages = (
            "access_token: abcdefghijklmnop",
            'api_key="abcdefgh123456"',
            "authorization: Basic dXNlcjpwYXNzd29yZA==",
            "password=$abcdefgh123456",
        )
        with (
            patch("app.agent.web_search_actions.get", side_effect=_config(self.values)),
            patch("app.agent.web_search_actions._search_tavily") as provider,
        ):
            for message in messages:
                with (
                    self.subTest(message=message),
                    self.assertRaisesRegex(AgentToolError, "疑似包含凭据"),
                ):
                    search_web({"query": message})
        provider.assert_not_called()
        self.assertEqual(
            db.get_agent_web_search_daily_usage(
                provider="tavily", usage_date=db.current_agent_web_search_usage_date()
            ),
            0,
        )

    def test_success_is_cached_and_charged_once(self):
        result = ToolResult(
            True, "ok", "找到 1 条网页结果", data={"results": [{"title": "Demo"}]}
        )

        async def provider(*args, **kwargs):
            return result

        with (
            patch("app.agent.web_search_actions.get", side_effect=_config(self.values)),
            patch(
                "app.agent.web_search_actions._search_tavily", side_effect=provider
            ) as call,
        ):
            first = search_web({"query": "Jellyfin 12", "max_results": 10})
            second = search_web({"query": "Jellyfin 12", "max_results": 10})
        self.assertTrue(first.ok)
        self.assertFalse(first.data["cached"])
        self.assertTrue(second.data["cached"])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(
            db.get_agent_web_search_daily_usage(
                provider="tavily", usage_date=db.current_agent_web_search_usage_date()
            ),
            1,
        )

    def _run_two_threads(self, action):
        start = threading.Barrier(2)
        results = [None, None]
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                start.wait(timeout=2)
                results[index] = action()
            except BaseException as exc:  # pragma: no cover - 线程错误回传
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        return results

    def test_concurrent_identical_search_is_single_flight_and_charged_once(self):
        both_reserved = threading.Event()
        reserve_lock = threading.Lock()
        reserve_count = 0
        provider_calls = 0
        original_reserve = web_request_singleflight.reserve

        def reserve(key):
            nonlocal reserve_count
            lease = original_reserve(key)
            with reserve_lock:
                reserve_count += 1
                if reserve_count == 2:
                    both_reserved.set()
            return lease

        async def provider(*_args, **_kwargs):
            nonlocal provider_calls
            provider_calls += 1
            if not await asyncio.to_thread(both_reserved.wait, 2):
                raise RuntimeError("concurrent waiter did not reserve")
            return ToolResult(
                True,
                "ok",
                "找到 1 条网页结果",
                data={"results": [{"title": "Demo"}]},
            )

        with (
            patch("app.agent.web_search_actions.get", side_effect=_config(self.values)),
            patch("app.agent.web_search_actions._search_tavily", side_effect=provider),
            patch.object(web_request_singleflight, "reserve", side_effect=reserve),
        ):
            results = self._run_two_threads(lambda: search_web({"query": "same"}))

        self.assertEqual(provider_calls, 1)
        self.assertTrue(all(result is not None and result.ok for result in results))
        self.assertEqual(sorted(result.data["cached"] for result in results), [False, True])
        self.assertEqual(
            db.get_agent_web_search_daily_usage(
                provider="tavily", usage_date=db.current_agent_web_search_usage_date()
            ),
            1,
        )

    def test_concurrent_owner_failure_releases_waiter_and_refunds_credit(self):
        both_reserved = threading.Event()
        reserve_lock = threading.Lock()
        reserve_count = 0
        provider_calls = 0
        original_reserve = web_request_singleflight.reserve

        def reserve(key):
            nonlocal reserve_count
            lease = original_reserve(key)
            with reserve_lock:
                reserve_count += 1
                if reserve_count == 2:
                    both_reserved.set()
            return lease

        async def provider(*_args, **_kwargs):
            nonlocal provider_calls
            provider_calls += 1
            if not await asyncio.to_thread(both_reserved.wait, 2):
                raise RuntimeError("concurrent waiter did not reserve")
            return ToolResult(False, "timeout", "网页搜索服务响应超时")

        with (
            patch("app.agent.web_search_actions.get", side_effect=_config(self.values)),
            patch("app.agent.web_search_actions._search_tavily", side_effect=provider),
            patch.object(web_request_singleflight, "reserve", side_effect=reserve),
        ):
            results = self._run_two_threads(lambda: search_web({"query": "failure"}))

        self.assertEqual(provider_calls, 1)
        self.assertEqual(sorted(result.status for result in results), ["timeout", "unavailable"])
        self.assertEqual(
            db.get_agent_web_search_daily_usage(
                provider="tavily", usage_date=db.current_agent_web_search_usage_date()
            ),
            0,
        )

    def test_provider_failure_refunds_reserved_credits(self):

        async def provider(*args, **kwargs):
            return ToolResult(False, "timeout", "网页搜索服务响应超时")

        with (
            patch("app.agent.web_search_actions.get", side_effect=_config(self.values)),
            patch("app.agent.web_search_actions._search_tavily", side_effect=provider),
        ):
            result = search_web({"query": "temporary failure"})
        self.assertEqual(result.status, "timeout")
        self.assertEqual(
            db.get_agent_web_search_daily_usage(
                provider="tavily", usage_date=db.current_agent_web_search_usage_date()
            ),
            0,
        )

    def test_shared_rate_bucket_is_consistent_across_instances(self):
        first = AgentRateLimiter(shared=True)
        second = AgentRateLimiter(shared=True)
        first.reset()
        self.assertTrue(first.allow("same-user", limit=3, window_seconds=60))
        self.assertTrue(second.allow("same-user", limit=3, window_seconds=60))
        self.assertTrue(first.allow("same-user", limit=3, window_seconds=60))
        self.assertFalse(second.allow("same-user", limit=3, window_seconds=60))

    def test_shared_rate_bucket_reclaims_expired_rows_and_bounds_identities(self):
        limiter = AgentRateLimiter(shared=True, max_keys=2)
        limiter.reset()
        with patch("app.agent.rate_limit.time.time", return_value=120.0):
            self.assertTrue(limiter.allow("owner-a", limit=1, window_seconds=60))
            self.assertTrue(limiter.allow("owner-b", limit=1, window_seconds=60))
            self.assertFalse(limiter.allow("owner-c", limit=1, window_seconds=60))
            self.assertEqual(limiter.tracked_keys(), 2)
        with patch("app.agent.rate_limit.time.time", return_value=240.0):
            self.assertEqual(limiter.tracked_keys(), 0)
            self.assertTrue(limiter.allow("owner-c", limit=1, window_seconds=60))
        with db.get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_rate_limit_buckets"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_shared_rate_bucket_migration_preserves_active_legacy_budget(self):
        key = "legacy-active-owner"
        digest = hashlib.sha256(
            b"mediaflux-agent-rate:v1\x00" + key.encode("utf-8")
        ).hexdigest()
        with db.get_conn() as conn:
            conn.execute("DROP TABLE agent_rate_limit_buckets")
            conn.execute(
                "CREATE TABLE agent_rate_limit_buckets(limiter_key TEXT PRIMARY KEY,window_start INTEGER NOT NULL,count INTEGER NOT NULL DEFAULT 0 CHECK(count>=0),updated_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO agent_rate_limit_buckets(limiter_key,window_start,count,updated_at) VALUES(?,?,?,?)",
                (digest, 120, 3, db.now()),
            )
        limiter = AgentRateLimiter(shared=True)
        with patch("app.agent.rate_limit.time.time", return_value=130.0):
            self.assertFalse(limiter.allow(key, limit=3, window_seconds=60))
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT count,expires_at FROM agent_rate_limit_buckets WHERE limiter_key=?",
                (digest,),
            ).fetchone()
        self.assertEqual(int(row["count"]), 3)
        self.assertGreater(int(row["expires_at"]), 130)

    def test_daily_budget_fails_closed_before_provider(self):
        self.values["TAVILY_DAILY_CREDIT_LIMIT"] = "1"
        self.assertTrue(
            db.reserve_agent_web_search_credits(
                provider="tavily",
                usage_date=db.current_agent_web_search_usage_date(),
                cost=1,
                daily_limit=1,
            )
        )
        with (
            patch("app.agent.web_search_actions.get", side_effect=_config(self.values)),
            patch("app.agent.web_search_actions._search_tavily") as provider,
        ):
            result = search_web({"query": "different"})
        self.assertEqual(result.status, "budget_exhausted")
        provider.assert_not_called()

    def test_provider_results_drop_unsafe_urls(self):
        result = _map_response(
            {
                "results": [
                    {
                        "title": "Public",
                        "content": "safe",
                        "url": "https://example.com/page#fragment",
                        "score": 0.8,
                    },
                    {
                        "title": "Private",
                        "content": "hidden",
                        "url": "http://127.0.0.1/admin",
                        "score": 1,
                    },
                    {
                        "title": "Credentials",
                        "content": "hidden",
                        "url": "https://user:pass@example.com/",
                        "score": 1,
                    },
                    {
                        "title": "Port",
                        "content": "hidden",
                        "url": "https://example.com:8443/",
                        "score": 1,
                    },
                    {
                        "title": "Script",
                        "content": "hidden",
                        "url": "javascript:alert(1)",
                        "score": 1,
                    },
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
