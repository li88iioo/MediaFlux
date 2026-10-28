from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.indexers.models import IndexerMediaSearchRequest, indexer_site_status
from app.indexers.providers.base import magnet_infohash, report_page
from app.indexers.providers.kpkuang import KPkuangAdapter
from app.indexers.registry import IndexerRegistry
from app.indexers.result_store import IndexerResultStore
from app.indexers.service import IndexerService
from tests.test_indexer_kpkuang import (
    API,
    FakeHttp,
    api,
    candidate,
    detail_html,
    detail_url,
    down_url,
    download_html,
    magnet,
    response,
)


class KPkuangServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 14, tzinfo=timezone.utc)
        self.store = IndexerResultStore(clock=lambda: self.now)
        # 只移除站点请求间隔；HTTP、预检、解析、查询计划仍走真实实现。
        interval = patch("app.indexers.providers.kpkuang._SEARCH_INTERVAL_SECONDS", 0)
        interval.start()
        self.addCleanup(interval.stop)

    def service(self, http, **kwargs):
        service = IndexerService(
            registry=IndexerRegistry({"kpkuang": KPkuangAdapter(http=http)}),
            result_store=self.store,
            clock=lambda: self.now,
            site_timeout_seconds=20,
            total_timeout_seconds=30,
            **kwargs,
        )
        self.addAsyncCleanup(service.aclose)
        return service

    async def test_candidate_limit_keeps_coverage_warning_through_merge_and_cache(self):
        for found in (True, False):
            with self.subTest(found=found):
                routes = {API: response(API, api([candidate(i, "Example Show", 2023) for i in range(1, 5)]))}
                for i in range(1, 4):
                    routes[detail_url(i)] = response(detail_url(i), detail_html(i))
                    routes[down_url(i)] = response(down_url(i), download_html(
                        [("Example Show 1080p", magnet(f"{i:040x}"))] if found else []))
                http = FakeHttp(routes)
                service = self.service(http, max_results_per_site=100)
                result = await service.search("Example Show", scope="agent")
                self.assertTrue(result.complete)  # 本轮结束，不等于检索已穷尽。
                self.assertTrue(result.partial)
                self.assertEqual(result.sites_succeeded, ("kpkuang",))
                self.assertEqual(len(result.items), 3 if found else 0)
                self.assertEqual([e.code for e in result.errors], ["coverage_limited"])
                self.assertIn("3/4", result.errors[0].message)
                self.assertEqual(indexer_site_status(result, "kpkuang", len(result.items)), "partial")
                self.assertNotIn(detail_url(4), http.calls)
                self.assertNotIn("kpkuang", service._breaker_states)
                if found:
                    calls = list(http.calls)
                    cached = await service.search("Example Show", scope="agent")
                    self.assertTrue(cached.cached)
                    self.assertTrue(cached.partial)
                    self.assertTrue(cached.complete)
                    self.assertEqual(cached.errors, result.errors)
                    self.assertEqual(http.calls, calls)
                else:
                    self.assertFalse(service._cache)  # 未覆盖范围不能缓存成全站无结果。

    async def test_short_query_tries_valid_alias_without_http_or_breaker_failure(self):
        infohash = "a" * 40
        for found in (False, True):
            with self.subTest(found=found):
                queries = []

                def search_response(_url, kwargs, *, queries=queries, found=found):
                    queries.append(kwargs["params"]["kw"])
                    return response(API, api([candidate(1917, "一九一七", 2019)] if found else []))

                http = FakeHttp({
                    API: search_response,
                    detail_url(1917): response(detail_url(1917), detail_html(1917)),
                    down_url(1917): response(
                        down_url(1917), download_html([("一九一七 (2019) 1080p", magnet(infohash))])
                    ),
                })
                service = self.service(http, breaker_failure_threshold=1)
                result = await service.search_media(
                    IndexerMediaSearchRequest.create(
                        title="1917", aliases=["一九一七"], year=2019, media_type="movie"
                    ),
                    scope="agent",
                )

                self.assertEqual(queries, ["一九一七"])
                self.assertEqual(http.calls.count(API), 1)
                self.assertEqual([magnet_infohash(item.magnet) for item in result.items], [infohash] if found else [])
                self.assertEqual(result.site_queries, {"kpkuang": "一九一七"})
                self.assertEqual(result.site_attempt_counts, {"kpkuang": 2})
                self.assertEqual(result.sites_succeeded, ("kpkuang",))
                self.assertFalse(result.errors)
                self.assertFalse(result.partial)
                self.assertTrue(result.complete)
                self.assertNotIn("kpkuang", service._breaker_states)

    async def test_rejected_query_without_alias_is_explicit_and_never_cools_site(self):
        http = FakeHttp({})
        service = self.service(http, breaker_failure_threshold=1)
        for title in ("1917", "1917", "Dune", "三"):
            with self.subTest(title=title):
                result = await service.search_media(
                    IndexerMediaSearchRequest.create(title=title), scope="agent"
                )
                self.assertEqual(result.items, [])
                self.assertEqual([(error.site_id, error.code) for error in result.errors], [("kpkuang", "query_rejected")])
                self.assertTrue(result.errors[0].message)
                self.assertEqual(result.sites_succeeded, ())
                self.assertTrue(result.partial)
                self.assertFalse(result.cached)
                self.assertEqual(result.site_attempt_counts, {"kpkuang": 1})
                self.assertEqual(http.calls, [])
                self.assertNotIn("kpkuang", service._breaker_states)
                self.assertFalse(service._cache)

    async def test_real_http_failure_accumulates_breaker_but_rejection_does_not(self):
        for upstream, code in (
            (TimeoutError("fixture timeout"), "timeout"),
            (OSError("fixture connection failure"), "unavailable"),
            (response(API, "upstream failure", status=503), "unavailable"),
        ):
            with self.subTest(code=code, upstream=type(upstream).__name__):
                queries = []

                def failed_search(_url, kwargs, *, queries=queries, upstream=upstream):
                    queries.append(kwargs["params"]["kw"])
                    return upstream

                http = FakeHttp({API: failed_search})
                service = self.service(http, breaker_failure_threshold=2)
                request = IndexerMediaSearchRequest.create(title="1917", aliases=["一九一七"])
                first = await service.search_media(request, scope="agent")
                self.assertEqual([error.code for error in first.errors], [code])
                self.assertEqual(service._breaker_states["kpkuang"].failures, 1)
                self.assertIsNone(service._breaker_states["kpkuang"].open_until)

                rejected = await service.search_media(
                    IndexerMediaSearchRequest.create(title="1917"), scope="agent"
                )
                self.assertEqual([error.code for error in rejected.errors], ["query_rejected"])
                self.assertEqual(service._breaker_states["kpkuang"].failures, 1)
                self.assertEqual(queries, ["一九一七"])

                second = await service.search_media(request, scope="agent")
                self.assertEqual([error.code for error in second.errors], [code])
                self.assertEqual(service._breaker_states["kpkuang"].failures, 2)
                open_until = service._breaker_states["kpkuang"].open_until
                assert open_until is not None
                self.assertGreater(open_until, self.now)
                blocked = await service.search_media(request, scope="agent")
                self.assertEqual([error.code for error in blocked.errors], ["unavailable"])
                self.assertEqual(blocked.site_attempt_counts, {"kpkuang": 0})
                self.assertEqual(queries, ["一九一七", "一九一七"])
                self.assertEqual(http.calls, [API, API])

    async def test_explicit_second_season_beats_original_year_catalog(self):
        infohash = "b" * 40
        http = FakeHttp({
            API: response(API, api([
                candidate(201, "Example Show", 2023, score=100),
                candidate(202, "Example Show S02", 2026, score=1),
                candidate(203, "Example Show S01", 2023, score=100),
            ])),
            detail_url(202): response(detail_url(202), detail_html(202)),
            down_url(202): response(
                down_url(202), download_html([("Example Show S02E12.1080p", magnet(infohash))])
            ),
        })
        result = await self.service(http).search_media(
            IndexerMediaSearchRequest.create(
                title="Example Show", year=2023, media_type="tv", season=2, episode=12
            ),
            scope="agent",
        )

        self.assertEqual([magnet_infohash(item.magnet) for item in result.items], [infohash])
        self.assertIn("episode_exact", result.items[0].match_reasons)
        self.assertEqual(http.calls, [API, detail_url(202), down_url(202)])
        self.assertFalse(result.errors)
        self.assertFalse(result.partial)
        self.assertTrue(result.complete)

    async def test_movie_first_season_and_unmarked_catalog_keep_strict_year_filter(self):
        for media_type, season, episode, catalog_title in (
            ("movie", None, None, "Example Show S02"),
            ("tv", 1, 1, "Example Show S01"),
            ("tv", 2, 12, "Example Show"),
            ("tv", None, None, "Example Show S02"),
        ):
            with self.subTest(media_type=media_type, season=season, catalog_title=catalog_title):
                http = FakeHttp({API: response(API, api([candidate(301, catalog_title, 2026)]))})
                result = await self.service(http).search_media(
                    IndexerMediaSearchRequest.create(
                        title="Example Show", year=2023, media_type=media_type,
                        season=season, episode=episode,
                    ),
                    scope="agent",
                )
                self.assertEqual(result.items, [])
                self.assertEqual(http.calls, [API])
                self.assertFalse(result.errors)
                self.assertFalse(result.partial)
                self.assertTrue(result.complete)

    async def test_caller_timeout_during_alias_keeps_bare_exact_episode_and_no_complete_cache(self):
        infohash = "c" * 40
        alias_started = asyncio.Event()
        alias_cancelled = asyncio.Event()
        partial_arrived = asyncio.Event()
        frames = []
        queries = []

        async def search_response(_url, kwargs):
            query = kwargs["params"]["kw"]
            queries.append(query)
            if query == "Alternate Show":
                alias_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    alias_cancelled.set()
            return response(API, api([
                candidate(401, "Example Show S01", 2023),
                candidate(402, "Example Show S01", 2023),
            ]))

        http = FakeHttp({
            API: search_response,
            detail_url(401): response(detail_url(401), detail_html(401)),
            down_url(401): response(
                down_url(401), download_html([("S01E01.1080p", magnet(infohash))])
            ),
        })
        service = self.service(http)
        request = IndexerMediaSearchRequest.create(
            title="Example Show", aliases=["Alternate Show"], year=2023,
            media_type="tv", season=1, episode=1,
        )

        def progress(frame):
            if frame.items:
                frames.append(frame)
                partial_arrived.set()

        with patch("app.indexers.service.report_page", wraps=report_page) as published:
            owner = asyncio.create_task(service.search_media(request, scope="agent", on_progress=progress))
            try:
                await asyncio.wait_for(alias_started.wait(), timeout=2)
                # 明确验证 service 的 alias 间发布，不能只靠适配器恰好上报而通过。
                published.assert_called_once()
                site_id, page = published.call_args.args
                self.assertEqual(site_id, "kpkuang")
                self.assertFalse(page.complete)
                self.assertEqual([magnet_infohash(item.magnet) for item in page.items], [infohash])
                self.assertIn("episode_exact", page.items[0].match_reasons)
                await asyncio.wait_for(partial_arrived.wait(), timeout=2)
                self.assertFalse(frames[-1].complete)
                self.assertFalse(owner.done())

                # 等别名已阻塞、快照已发布后才启动短 caller，解析速度不影响断言。
                result = await service.search_media(request, scope="agent", timeout_seconds=0.02)
                self.assertEqual([magnet_infohash(item.magnet) for item in result.items], [infohash])
                self.assertTrue(result.partial)
                self.assertFalse(result.complete)
                self.assertFalse(result.cached)
                self.assertEqual(result.items[0].result_id, frames[-1].items[0].result_id)
                assert result.items[0].result_id is not None
                self.assertEqual(magnet_infohash(self.store.get(result.items[0].result_id).magnet), infohash)
                self.assertEqual(queries, ["Example Show", "Alternate Show"])
                self.assertNotIn(detail_url(402), http.calls)
                self.assertFalse(service._cache)
                self.assertFalse(owner.done())
                self.assertFalse(alias_cancelled.is_set())
            finally:
                owner.cancel()
                await asyncio.gather(owner, return_exceptions=True)

        self.assertTrue(alias_cancelled.is_set())
        self.assertFalse(service._cache)
        self.assertFalse(service._inflight)
        self.assertNotIn("kpkuang", service._breaker_states)

        # 未完成快照不能冒充完整缓存；下一次必须重新请求，完成后才能命中缓存。
        http.routes[down_url(401)] = response(
            down_url(401), download_html([("Example Show S01E01.1080p", magnet(infohash))])
        )
        retried = await service.search_media(request, scope="agent")
        self.assertEqual(queries, ["Example Show", "Alternate Show", "Example Show"])
        self.assertFalse(retried.cached)
        self.assertFalse(retried.partial)
        self.assertTrue(retried.complete)
        self.assertEqual([magnet_infohash(item.magnet) for item in retried.items], [infohash])
        calls = list(http.calls)
        cached = await service.search_media(request, scope="agent")
        self.assertTrue(cached.cached)
        self.assertTrue(cached.complete)
        self.assertFalse(cached.partial)
        self.assertEqual(http.calls, calls)


if __name__ == "__main__":
    unittest.main()
