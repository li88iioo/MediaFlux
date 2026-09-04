"""搜索、网页读取与识别线索共用缓存生命周期的竞态回归。"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import Mock, patch

from app import database as db
from app.agent import web_read_actions as read, web_search_actions as search
from app.agent.models import ToolResult
from app.agent.web_request_singleflight import web_request_singleflight
from app.modules import recognition_web_hints as hints
from tests.support import IsolatedDatabaseTestCase


class WebRequestLifecycleTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        search.clear_web_search_cache()
        hints.clear_recognition_web_hint_cache()
        self.addCleanup(search.clear_web_search_cache)
        self.addCleanup(hints.clear_recognition_web_hint_cache)
        stack = self.enterContext(ExitStack())
        values = {
            "WEB_SEARCH_ENABLED": "1",
            "TAVILY_API_KEY": "test-key",
            "TAVILY_DAILY_CREDIT_LIMIT": "100",
            "TAVILY_MAX_RESULTS": "5",
        }
        for module in (read, search, hints):
            stack.enter_context(
                patch.object(
                    module, "get", side_effect=lambda k, d="": values.get(k, d)
                )
            )
        stack.enter_context(patch.object(hints, "get_bool", return_value=True))

    def cases(self):
        return (
            (
                search,
                "_cached",
                "_search_tavily",
                lambda: search.search_web({"query": "demo"}),
                search.clear_web_search_cache,
                web_request_singleflight,
            ),
            (
                read,
                "_restore_cached_result",
                "_extract_tavily",
                lambda: read.read_web({"url": "https://example.com/article"}),
                search.clear_web_search_cache,
                web_request_singleflight,
            ),
            (
                hints,
                "_cached",
                "_request_titles",
                lambda: hints.search_recognition_titles(
                    "Demo", reserve_daily=Mock(return_value=True)
                ),
                hints.clear_recognition_web_hint_cache,
                hints._singleflight,
            ),
        )

    @staticmethod
    def result(module, *, ok=True):
        if module is hints:
            return hints.RecognitionWebHintResult(
                titles=("Demo",), attempted=True, status="ok"
            )
        return ToolResult(
            ok,
            "ok" if ok else "unavailable",
            "demo",
            data={"url": "https://example.com/article"},
            model_data={"chunks": ["demo"]},
        )

    def test_owner_rechecks_cache_after_another_request_publishes(self):
        for module, cache_name, provider_name, request, clear, flight in self.cases():
            with self.subTest(module=module.__name__):
                clear()
                original_cache = getattr(module, cache_name)
                first_lookup = True

                def raced_lookup(key):
                    nonlocal first_lookup
                    if first_lookup:
                        first_lookup = False
                        # A 已错过缓存，B 在 A 登记租约前完成并发布结果。
                        request()
                        return None
                    return original_cache(key)

                async def provider(*args, **kwargs):
                    return self.result(module)

                with (
                    patch.object(module, cache_name, side_effect=raced_lookup),
                    patch.object(module, provider_name, side_effect=provider) as call,
                ):
                    result = request()
                self.assertEqual(call.call_count, 1)
                self.assertTrue(
                    result.cached if module is hints else result.data["cached"]
                )
                self.assertEqual(flight.active_count, 0)

    def test_clear_during_provider_request_prevents_stale_cache_publication(self):
        for module, _, provider_name, request, clear, flight in self.cases():
            with self.subTest(module=module.__name__):
                clear()

                async def provider(*args, **kwargs):
                    clear()
                    return self.result(module)

                with patch.object(module, provider_name, side_effect=provider) as call:
                    request()
                    result = request()
                self.assertEqual(call.call_count, 2)
                self.assertFalse(
                    result.cached if module is hints else result.data["cached"]
                )
                self.assertEqual(flight.active_count, 0)

    def test_budget_refund_failure_still_releases_request_lease(self):
        for module, _, provider_name, request, clear, flight in self.cases()[:2]:
            with self.subTest(module=module.__name__):
                clear()

                async def provider(*args, **kwargs):
                    return self.result(module, ok=False)

                with (
                    patch.object(module, provider_name, side_effect=provider),
                    patch.object(
                        db,
                        "refund_agent_web_search_credits",
                        side_effect=RuntimeError("database busy"),
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "database busy"):
                        request()
                self.assertEqual(flight.active_count, 0)
