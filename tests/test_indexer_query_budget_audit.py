"""合法媒体标题追加季集查询时仍必须满足共享输入预算。"""

from __future__ import annotations

import unittest

from app.indexers.errors import IndexerValidationError
from app.indexers.models import (
    IndexerMediaSearchRequest,
    IndexerPage,
    IndexerSearchRequest,
)
from app.indexers.query_plan import build_site_queries
from app.indexers.registry import IndexerRegistry
from app.indexers.result_store import IndexerResultStore
from app.indexers.service import IndexerService


class IndexerQueryBudgetAuditTests(unittest.TestCase):
    def test_long_aliases_with_positions_fit_all_provider_query_limits(self):
        for season, episode in (
            (1, 1),
            (None, 999),
            (99, None),
            (99, 999),
            (100, 1000),
        ):
            request = IndexerMediaSearchRequest.create(
                title="中" * 120,
                original_title="あ" * 120,
                english_title="E" * 120,
                aliases=["Alias " + "a" * 114],
                season=season,
                episode=episode,
            )
            for provider in ("nyaa", "mikan", "btbtla", "tpb", "sukebei"):
                with self.subTest(provider=provider, season=season, episode=episode):
                    queries = build_site_queries(provider, request)
                    self.assertGreater(len(queries), 0)
                    self.assertLessEqual(len(queries), 3)
                    for query in queries:
                        self.assertLessEqual(len(query), 120)
                        self.assertEqual(
                            IndexerSearchRequest.create(query).query, query
                        )
                    if episode is not None:
                        self.assertIn(f"{episode:02d}" if provider == "mikan" else f"E{episode:02d}", queries[0])
                    else:
                        self.assertTrue(queries[0].endswith(f"S{season:02d}"))
            self.assertEqual(request.title, "中" * 120)
            self.assertEqual(request.english_title, "E" * 120)

    def test_queries_without_appended_position_retain_full_title(self):
        for title in ("中" * 120, "A" * 112 + " S01E001"):
            with self.subTest(title=title):
                request = IndexerMediaSearchRequest.create(
                    title=title, season=1, episode=1
                )
                if "S01E001" in title:
                    self.assertEqual(build_site_queries("nyaa", request), (title,))
                broad = IndexerMediaSearchRequest.create(title=title)
                self.assertEqual(build_site_queries("mikan", broad), (title,))

    def test_public_request_limits_are_not_relaxed_for_internal_suffixes(self):
        for make_request in (
            lambda: IndexerMediaSearchRequest.create(title="A" * 121),
            lambda: IndexerSearchRequest.create("A" * 121),
        ):
            with self.assertRaises(IndexerValidationError):
                make_request()


class IndexerQueryBudgetServiceAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_validated_boundary_title_reaches_provider_and_preserves_broad_fallback(
        self,
    ):
        class Provider:
            site_id = "nyaa"
            site_name = "Offline Nyaa"
            default_enabled = True

            def __init__(self):
                self.calls = []

            async def search(self, request):
                self.calls.append(request.query)
                return IndexerPage(
                    items=[],
                    page=request.page,
                    has_more=False,
                    pagination_supported=True,
                )

        provider = Provider()
        service = IndexerService(
            registry=IndexerRegistry({"nyaa": provider}),
            result_store=IndexerResultStore(),
        )
        try:
            title = ("A Documentary About Space and Time " * 4)[:120]
            request = IndexerMediaSearchRequest.create(title=title, season=1, episode=1)
            result = await service.search_media(request, ["nyaa"])
            self.assertEqual(result.sites_succeeded, ("nyaa",))
            self.assertEqual(len(provider.calls), 2)
            self.assertTrue(provider.calls[0].endswith("S01E01"))
            self.assertEqual(provider.calls[1], title)
            self.assertEqual(request.title, title)
        finally:
            await service.aclose()
