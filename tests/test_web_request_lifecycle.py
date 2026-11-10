"""搜索、网页读取与识别线索共用缓存生命周期的竞态回归。"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import Mock, patch

from app.repositories import agent_web_search as agent_web_search_repo
from app.modules import web_evidence as read
from app.modules import web_evidence as search
from app.modules.web_evidence import (
    web_request_singleflight,
)
from app.modules.web_evidence import _result as make_result
from app.modules import recognition_web_hints as hints
from tests.support import IsolatedDatabaseTestCase


class WebRequestLifecycleTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        search.clear_web_cache()
        hints.clear_recognition_web_hint_cache()
        self.addCleanup(search.clear_web_cache)
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
                "_cached_result",
                "_search_tavily",
                lambda: search.search_web("demo"),
                search.clear_web_cache,
                web_request_singleflight,
            ),
            (
                read,
                "_cached_result",
                "_read_local",
                lambda: read.read_web("https://example.com/article"),
                search.clear_web_cache,
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
        return make_result(
            ok,
            "ok" if ok else "unavailable",
            "demo",
            data={"url": "https://example.com/article", "content": "demo", "cached": False},
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
                    result.cached if isinstance(result, hints.RecognitionWebHintResult) else result["data"]["cached"]
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
                    result.cached if isinstance(result, hints.RecognitionWebHintResult) else result["data"]["cached"]
                )
                self.assertEqual(flight.active_count, 0)

    def test_budget_refund_failure_still_releases_request_lease(self):
        # 本地读取不再预留或退回 Tavily 额度；只有搜索需要验证额度异常。
        for module, _, provider_name, request, clear, flight in self.cases()[:1]:
            with self.subTest(module=module.__name__):
                clear()

                async def provider(*args, **kwargs):
                    return self.result(module, ok=False)

                with (
                    patch.object(module, provider_name, side_effect=provider),
                    patch.object(
                        agent_web_search_repo,
                        "refund_agent_web_search_credits",
                        side_effect=RuntimeError("database busy"),
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "database busy"):
                        request()
                self.assertEqual(flight.active_count, 0)
