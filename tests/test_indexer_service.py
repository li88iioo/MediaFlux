from __future__ import annotations

import asyncio
import threading
import unittest
from datetime import datetime, timedelta, timezone

from app.indexers.errors import (
    IndexerRateLimited,
    IndexerSecurityError,
    IndexerUnavailable,
    IndexerValidationError,
)
from app.indexers.models import IndexerItem, IndexerMediaSearchRequest, IndexerPage, ResolvedDownload
from app.indexers.registry import IndexerRegistry
from app.indexers.result_store import IndexerResultStore
from app.indexers.service import IndexerService


HASH = "0123456789abcdef0123456789abcdef01234567"


class FakeAdapter:
    def __init__(
        self,
        site_id,
        items=None,
        *,
        delay=0,
        error=None,
        resolve_error=None,
        default_enabled=True,
        tracker=None,
        has_more=False,
    ):
        self.site_id = site_id
        self.site_name = site_id.upper()
        self.default_enabled = default_enabled
        self.items = list(items or [])
        self.delay = delay
        self.error = error
        self.resolve_error = resolve_error
        self.calls = 0
        self.resolve_calls = []
        self.tracker = tracker
        self.has_more = has_more

    async def search(self, request):
        self.calls += 1
        if self.tracker is not None:
            self.tracker["active"] += 1
            self.tracker["maximum"] = max(self.tracker["maximum"], self.tracker["active"])
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error:
                raise self.error
            return IndexerPage(
                items=list(self.items),
                page=request.page,
                has_more=self.has_more,
                pagination_supported=True,
            )
        finally:
            if self.tracker is not None:
                self.tracker["active"] -= 1

    async def resolve(self, stored_result):
        self.resolve_calls.append(stored_result)
        if self.resolve_error:
            raise self.resolve_error
        return ResolvedDownload(kind="magnet", value=f"magnet:?xt=urn:btih:{HASH}")


class CrossLoopAdapter(FakeAdapter):
    def __init__(self, site_id, items, barrier):
        super().__init__(site_id, items)
        self.barrier = barrier
        self._calls_lock = threading.Lock()

    async def search(self, request):
        with self._calls_lock:
            self.calls += 1
        await asyncio.to_thread(self.barrier.wait, 1)
        await asyncio.sleep(0.01)
        return IndexerPage(
            items=list(self.items),
            page=request.page,
            has_more=False,
            pagination_supported=True,
        )


class SlotDelayedAdapter(FakeAdapter):
    def __init__(self, site_id, items, *, slot_delay):
        super().__init__(site_id, items)
        self.slot_delay = slot_delay
        self.slot_calls = 0

    async def wait_for_search_slot(self, request):
        self.slot_calls += 1
        await asyncio.sleep(self.slot_delay)


class TimeoutOverheadAdapter(FakeAdapter):
    def __init__(self, site_id, items, *, delay, overhead_seconds):
        super().__init__(site_id, items, delay=delay)
        self.overhead_seconds = overhead_seconds

    def search_timeout_overhead_seconds(self):
        return self.overhead_seconds


class QueryAwareAdapter(FakeAdapter):
    def __init__(self, site_id, items_by_query):
        super().__init__(site_id)
        self.items_by_query = {query: list(items) for query, items in items_by_query.items()}
        self.queries = []

    async def search(self, request):
        self.calls += 1
        self.queries.append(request.query)
        return IndexerPage(
            items=list(self.items_by_query.get(request.query, [])),
            page=request.page,
            has_more=False,
            pagination_supported=True,
        )


def item(site_id, title, *, magnet=None, seeders=0):
    return IndexerItem(
        site_id=site_id,
        site_name=site_id.upper(),
        title=title,
        magnet=magnet,
        seeders=seeders,
        download_state="ready" if magnet else "unavailable",
        download_kinds=("magnet",) if magnet else (),
    )


class IndexerServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
        self.store = IndexerResultStore(ttl_seconds=300, clock=lambda: self.now)

    def service(self, adapters, **kwargs):
        return IndexerService(
            registry=IndexerRegistry({adapter.site_id: adapter for adapter in adapters}),
            result_store=self.store,
            clock=lambda: self.now,
            cache_ttl_seconds=kwargs.pop("cache_ttl_seconds", 30),
            site_timeout_seconds=kwargs.pop("site_timeout_seconds", 0.2),
            total_timeout_seconds=kwargs.pop("total_timeout_seconds", 0.4),
            max_results_per_site=kwargs.pop("max_results_per_site", 2),
            max_concurrency=kwargs.pop("max_concurrency", 2),
            **kwargs,
        )

    async def test_partial_success_caps_each_site_and_returns_safe_error(self):
        good = FakeAdapter("nyaa", [item("nyaa", "one"), item("nyaa", "two"), item("nyaa", "three")])
        bad = FakeAdapter("mikan", error=IndexerUnavailable("internal upstream detail must not leak"))
        service = self.service([good, bad])

        result = await service.search("Frieren", page=1, site_ids=("nyaa", "mikan"))

        self.assertTrue(result.partial)
        self.assertEqual(result.sites_attempted, ("nyaa", "mikan"))
        self.assertEqual(result.sites_succeeded, ("nyaa",))
        self.assertEqual([entry.site_id for entry in result.errors], ["mikan"])
        self.assertEqual(result.errors[0].code, "unavailable")
        self.assertNotIn("internal upstream", result.errors[0].message)
        self.assertEqual([entry.title for entry in result.items], ["one", "two"])
        self.assertTrue(all(entry.result_id for entry in result.items))
        self.assertEqual(self.store.get(result.items[0].result_id).title, "one")

    async def test_media_search_uses_site_queries_and_stops_after_hit(self):
        media = IndexerMediaSearchRequest.create(
            title="奇招百出的维多利亚",
            original_title="手札が多めのビクトリア",
            english_title="Victoria of Many Faces",
            aliases=["Tefuda ga Oome no Victoria"],
            year=2026,
            media_type="tv",
        )
        nyaa = QueryAwareAdapter(
            "nyaa",
            {"手札が多めのビクトリア": [item("nyaa", "Japanese hit")]},
        )
        mikan = QueryAwareAdapter(
            "mikan",
            {"奇招百出的维多利亚": [item("mikan", "Chinese hit")]},
        )
        service = self.service([nyaa, mikan])

        result = await service.search_media(media, site_ids=("nyaa", "mikan"))

        self.assertEqual(
            nyaa.queries,
            ["Tefuda ga Oome no Victoria", "手札が多めのビクトリア"],
        )
        self.assertEqual(mikan.queries, ["奇招百出的维多利亚"] )
        self.assertEqual([entry.title for entry in result.items], ["Japanese hit", "Chinese hit"] )
        self.assertEqual(
            result.site_queries,
            {"nyaa": "手札が多めのビクトリア", "mikan": "奇招百出的维多利亚"},
        )
        self.assertEqual(result.site_attempt_counts, {"nyaa": 2, "mikan": 1})
        self.assertEqual(result.query, "奇招百出的维多利亚")

    async def test_btbtla_failure_is_marked_as_onelou_fallback_only_when_both_are_selected(self):
        btbtla = FakeAdapter("btbtla", error=IndexerUnavailable("tls failed"))
        onelou = FakeAdapter("1lou", [item("1lou", "fallback resource")])
        service = self.service([btbtla, onelou])

        result = await service.search("Demo", 1, ("btbtla", "1lou"))

        self.assertEqual(result.site_fallbacks, {"btbtla": "1lou"})
        self.assertEqual([entry.title for entry in result.items], ["fallback resource"] )

        isolated = self.service([FakeAdapter("btbtla", error=IndexerUnavailable("tls failed"))])
        isolated_result = await isolated.search("Demo", 1, ("btbtla",))
        self.assertEqual(isolated_result.site_fallbacks, {})

    async def test_magnet_infohash_dedupe_is_case_insensitive_and_keeps_first_stable_result(self):
        first = item("nyaa", "first", magnet=f"magnet:?dn=one&xt=urn:btih:{HASH.upper()}", seeders=1)
        duplicate = item("mikan", "duplicate", magnet=f"magnet:?xt=urn:btih:{HASH}&dn=two", seeders=999)
        unique = item("mikan", "unique", magnet="magnet:?xt=urn:btih:89abcdef0123456789abcdef0123456789abcdef")
        service = self.service([FakeAdapter("nyaa", [first]), FakeAdapter("mikan", [duplicate, unique])])

        result = await service.search("Frieren", 1, ("nyaa", "mikan"))

        self.assertEqual([entry.title for entry in result.items], ["first", "unique"])
        self.assertEqual(result.site_item_counts, {"nyaa": 1, "mikan": 2})

    async def test_provider_search_slot_wait_does_not_consume_site_timeout(self):
        adapter = SlotDelayedAdapter(
            "1lou",
            [item("1lou", "paced", magnet=f"magnet:?xt=urn:btih:{HASH}")],
            slot_delay=0.05,
        )
        service = self.service(
            [adapter],
            site_timeout_seconds=0.01,
            total_timeout_seconds=0.2,
        )

        result = await service.search("paced", 1, ("1lou",))

        self.assertEqual(adapter.slot_calls, 1)
        self.assertEqual([entry.title for entry in result.items], ["paced"])
        self.assertEqual(result.errors, [])

    async def test_provider_timeout_overhead_preserves_primary_site_budget(self):
        adapter = TimeoutOverheadAdapter(
            "1lou",
            [item("1lou", "google fallback", magnet=f"magnet:?xt=urn:btih:{HASH}")],
            delay=0.02,
            overhead_seconds=0.02,
        )
        service = self.service(
            [adapter],
            site_timeout_seconds=0.01,
            total_timeout_seconds=0.1,
        )

        result = await service.search("fallback", 1, ("1lou",))

        self.assertEqual([entry.title for entry in result.items], ["google fallback"])
        self.assertEqual(result.errors, [])

    async def test_site_timeout_and_total_timeout_preserve_completed_results(self):
        fast = FakeAdapter("nyaa", [item("nyaa", "fast")], delay=0.01)
        slow = FakeAdapter("mikan", [item("mikan", "slow")], delay=0.3)
        service = self.service([fast, slow], site_timeout_seconds=0.5, total_timeout_seconds=0.05)

        result = await service.search("Frieren", 1, ("nyaa", "mikan"))

        self.assertEqual([entry.title for entry in result.items], ["fast"])
        self.assertEqual(result.sites_succeeded, ("nyaa",))
        self.assertEqual(result.errors[0].site_id, "mikan")
        self.assertEqual(result.errors[0].code, "timeout")

    async def test_max_concurrency_is_enforced(self):
        tracker = {"active": 0, "maximum": 0}
        adapters = [FakeAdapter(f"site{i}", [item(f"site{i}", str(i))], delay=0.02, tracker=tracker) for i in range(4)]
        service = self.service(adapters, max_concurrency=2, max_results_per_site=1)

        result = await service.search("query", 1, tuple(adapter.site_id for adapter in adapters))

        self.assertEqual(len(result.items), 4)
        self.assertEqual(tracker["maximum"], 2)

    async def test_short_cache_skips_providers_then_expires(self):
        adapter = FakeAdapter("nyaa", [item("nyaa", "one")])
        service = self.service([adapter])

        first = await service.search("Frieren", 1, ("nyaa",))
        second = await service.search("  Frieren  ", 1, ("nyaa",))
        self.now += timedelta(seconds=31)
        third = await service.search("Frieren", 1, ("nyaa",))

        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertFalse(third.cached)
        self.assertEqual(adapter.calls, 2)

    async def test_cache_ttl_depends_on_result_health(self):
        healthy = FakeAdapter("nyaa", [item("nyaa", "hit")])
        healthy_service = self.service([healthy], cache_ttl_seconds=60)
        await healthy_service.search("healthy", 1, ("nyaa",))
        self.now += timedelta(seconds=31)
        healthy_cached = await healthy_service.search("healthy", 1, ("nyaa",))
        self.assertTrue(healthy_cached.cached)
        self.assertEqual(healthy.calls, 1)

        self.now = datetime(2026, 7, 26, 13, 0, tzinfo=timezone.utc)
        empty = FakeAdapter("nyaa", [])
        empty_service = self.service([empty], cache_ttl_seconds=60)
        await empty_service.search("empty", 1, ("nyaa",))
        self.now += timedelta(seconds=31)
        empty_expired = await empty_service.search("empty", 1, ("nyaa",))
        self.assertFalse(empty_expired.cached)
        self.assertEqual(empty.calls, 2)

        self.now = datetime(2026, 7, 26, 14, 0, tzinfo=timezone.utc)
        partial_good = FakeAdapter("nyaa", [item("nyaa", "partial hit")])
        partial_bad = FakeAdapter("mikan", error=IndexerUnavailable("temporary"))
        partial_service = self.service([partial_good, partial_bad], cache_ttl_seconds=60)
        await partial_service.search("partial", 1, ("nyaa", "mikan"))
        self.now += timedelta(seconds=16)
        partial_expired = await partial_service.search("partial", 1, ("nyaa", "mikan"))
        self.assertFalse(partial_expired.cached)
        self.assertEqual(partial_good.calls, 2)
        self.assertEqual(partial_bad.calls, 2)

    async def test_empty_partial_results_are_not_cached(self):
        adapter = FakeAdapter("nyaa", error=IndexerUnavailable("temporary"))
        service = self.service([adapter], cache_ttl_seconds=60)

        first = await service.search("failure", 1, ("nyaa",))
        second = await service.search("failure", 1, ("nyaa",))

        self.assertFalse(first.cached)
        self.assertFalse(second.cached)
        self.assertEqual(adapter.calls, 2)

    async def test_has_more_reflects_provider_pages_and_survives_cache_hits(self):
        adapter = FakeAdapter("nyaa", [item("nyaa", "one")], has_more=False)
        service = self.service([adapter])

        first = await service.search("Frieren", 1, ("nyaa",))
        cached = await service.search("Frieren", 1, ("nyaa",))

        self.assertIs(first.has_more, False)
        self.assertIs(cached.has_more, False)

    async def test_has_more_is_true_when_any_successful_provider_has_more(self):
        first = FakeAdapter("nyaa", [item("nyaa", "one")], has_more=False)
        second = FakeAdapter("mikan", [item("mikan", "two")], has_more=True)
        service = self.service([first, second])

        result = await service.search("Frieren", 1, ("nyaa", "mikan"))

        self.assertIs(result.has_more, True)

    async def test_page_100_never_reports_more_even_if_provider_has_next_page(self):
        adapter = FakeAdapter("nyaa", [item("nyaa", "one")], has_more=True)
        service = self.service([adapter])

        result = await service.search("Frieren", 100, ("nyaa",))

        self.assertIs(result.has_more, False)

    async def test_resolve_uses_only_opaque_stored_result_and_registered_adapter(self):
        deferred = IndexerItem(
            site_id="btbtla",
            site_name="BTBtla",
            title="Frieren",
            detail_url="https://www.btbtla.com/detail/frieren",
            download_state="resolvable",
            download_kinds=("magnet",),
        )
        adapter = FakeAdapter("btbtla", [deferred])
        service = self.service([adapter])
        search = await service.search("Frieren", 1, ("btbtla",))

        resolved = await service.resolve(search.items[0].result_id)

        self.assertEqual(resolved.kind, "magnet")
        self.assertEqual(len(adapter.resolve_calls), 1)
        self.assertEqual(adapter.resolve_calls[0].detail_url, "https://www.btbtla.com/detail/frieren")
        self.assertIsNone(adapter.resolve_calls[0].result_id)

    async def test_resolve_rejects_disabled_stored_provider_without_calling_adapter(self):
        adapter = FakeAdapter("1lou", default_enabled=False)
        stored_id = self.store.put(
            IndexerItem(
                site_id="1lou",
                site_name="1lou",
                title="Frieren",
                detail_url="https://1lou.me/thread.htm",
                download_state="resolvable",
                download_kinds=("torrent",),
            )
        )
        service = self.service([adapter])

        with self.assertRaises(IndexerSecurityError):
            await service.resolve(stored_id)
        self.assertEqual(adapter.resolve_calls, [])

    async def test_resolve_masks_provider_domain_error_detail(self):
        adapter = FakeAdapter(
            "btbtla",
            resolve_error=IndexerUnavailable("secret upstream detail must not leak"),
        )
        stored_id = self.store.put(
            IndexerItem(
                site_id="btbtla",
                site_name="BTBtla",
                title="Frieren",
                detail_url="https://www.btbtla.com/detail.htm",
                download_state="resolvable",
                download_kinds=("magnet",),
            )
        )
        service = self.service([adapter])

        with self.assertRaises(IndexerUnavailable) as caught:
            await service.resolve(stored_id)
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(str(caught.exception), "索引站点暂不可用")

    async def test_resolve_masks_unexpected_provider_exception(self):
        adapter = FakeAdapter("btbtla", resolve_error=RuntimeError("secret upstream URL must not leak"))
        stored_id = self.store.put(
            IndexerItem(
                site_id="btbtla",
                site_name="BTBtla",
                title="Frieren",
                detail_url="https://www.btbtla.com/detail.htm",
                download_state="resolvable",
                download_kinds=("magnet",),
            )
        )
        service = self.service([adapter])

        with self.assertRaises(IndexerUnavailable) as caught:
            await service.resolve(stored_id)
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(caught.exception.public_message, "索引站点暂不可用")

    async def test_rate_limited_site_cools_down_immediately(self):
        """限流站点继续重试只会触发更严格封锁，首个失败即进入冷却。"""
        adapter = FakeAdapter("nyaa", error=IndexerRateLimited("slow down"))
        service = self.service([adapter], breaker_cooldown_seconds=300)

        first = await service.search("rate-0", 1, ("nyaa",))
        self.assertTrue(first.partial)
        cooled = await service.search("rate-1", 1, ("nyaa",))

        self.assertEqual(adapter.calls, 1)
        self.assertEqual(cooled.errors[0].code, "rate_limited")
        self.assertEqual(cooled.errors[0].message, "索引站点请求过于频繁，已暂时冷却")
        self.now += timedelta(seconds=301)
        adapter.error = None
        adapter.items = [item("nyaa", "recovered")]
        recovered = await service.search("rate-2", 1, ("nyaa",))
        self.assertEqual([entry.title for entry in recovered.items], ["recovered"])

    async def test_security_error_site_cools_down_immediately(self):
        adapter = FakeAdapter("1lou", error=IndexerSecurityError("challenge"))
        service = self.service([adapter], breaker_cooldown_seconds=300)

        await service.search("sec-0", 1, ("1lou",))
        cooled = await service.search("sec-1", 1, ("1lou",))

        self.assertEqual(adapter.calls, 1)
        self.assertEqual(cooled.errors[0].code, "security_error")
        self.assertEqual(cooled.errors[0].message, "上游地址未通过安全校验，已暂时冷却")

    async def test_transient_unavailable_still_needs_threshold(self):
        adapter = FakeAdapter("mikan", error=IndexerUnavailable("blip"))
        service = self.service([adapter], breaker_cooldown_seconds=300)

        await service.search("u-0", 1, ("mikan",))
        await service.search("u-1", 1, ("mikan",))

        # 普通不可用属于可能瞬时的故障，未达阈值前仍应继续尝试。
        self.assertEqual(adapter.calls, 2)

    async def test_provider_circuit_opens_after_three_failures_and_recovers_after_cooldown(self):
        adapter = FakeAdapter("nyaa", error=IndexerUnavailable("temporary"))
        service = self.service([adapter], breaker_cooldown_seconds=300)

        for index in range(3):
            result = await service.search(f"failure-{index}", 1, ("nyaa",))
            self.assertTrue(result.partial)
        opened = await service.search("failure-open", 1, ("nyaa",))

        self.assertEqual(adapter.calls, 3)
        self.assertEqual(opened.errors[0].message, "索引站点连续失败，已暂时冷却")
        self.now += timedelta(seconds=301)
        adapter.error = None
        adapter.items = [item("nyaa", "recovered")]
        recovered = await service.search("recovered", 1, ("nyaa",))
        self.assertEqual(adapter.calls, 4)
        self.assertEqual([entry.title for entry in recovered.items], ["recovered"] )

    async def test_unknown_or_disabled_site_is_rejected_before_search(self):
        disabled = FakeAdapter("sukebei", default_enabled=False)
        service = self.service([disabled])

        with self.assertRaisesRegex(IndexerValidationError, "unknown or disabled"):
            await service.search("query", 1, ("missing",))
        with self.assertRaisesRegex(IndexerValidationError, "unknown or disabled"):
            await service.search("query", 1, ("sukebei",))
        self.assertEqual(disabled.calls, 0)

    async def test_ranking_clusters_and_page_state_are_exposed(self):
        first = item("nyaa", "[A] Frieren S01E01 1080p", magnet="magnet:?xt=urn:btih:" + "1" * 40, seeders=2)
        second = item("mikan", "[B] Frieren S01E01 WEB-DL", magnet="magnet:?xt=urn:btih:" + "2" * 40, seeders=20)
        unrelated = item("nyaa", "Unrelated Release", magnet="magnet:?xt=urn:btih:" + "3" * 40, seeders=999)
        nyaa = FakeAdapter("nyaa", [unrelated, first], has_more=True)
        mikan = FakeAdapter("mikan", [second])
        service = self.service([nyaa, mikan])
        media = IndexerMediaSearchRequest.create(title="Frieren", year=2026, media_type="tv")

        result = await service.search_media(media, ("nyaa", "mikan"))

        self.assertNotEqual(result.items[0].title, "Unrelated Release")
        clustered = [entry for entry in result.items if "Frieren" in entry.title]
        self.assertEqual(len(clustered), 2)
        self.assertEqual(clustered[0].cluster_id, clustered[1].cluster_id)
        self.assertEqual(clustered[0].cluster_size, 2)
        self.assertIn("title_contains", clustered[0].match_reasons)
        self.assertTrue(result.site_page_states["nyaa"].has_more)
        self.assertEqual(result.site_page_states["nyaa"].next_page, 2)
        self.assertFalse(result.site_page_states["mikan"].has_more)

    async def test_same_service_can_search_from_independent_event_loops(self):
        barrier = threading.Barrier(2)
        adapter = CrossLoopAdapter(
            "nyaa", [item("nyaa", "Frieren")], barrier
        )
        service = self.service([adapter])

        def run_search():
            return asyncio.run(service.search("Frieren", 1, ("nyaa",)))

        first, second = await asyncio.gather(
            asyncio.to_thread(run_search),
            asyncio.to_thread(run_search),
        )

        self.assertEqual(adapter.calls, 2)
        self.assertEqual([entry.title for entry in first.items], ["Frieren"])
        self.assertEqual([entry.title for entry in second.items], ["Frieren"])
        self.assertNotEqual(first.items[0].result_id, second.items[0].result_id)

    async def test_identical_concurrent_searches_are_single_flight(self):
        adapter = FakeAdapter("nyaa", [item("nyaa", "Frieren")], delay=0.03)
        service = self.service([adapter])

        first, second = await asyncio.gather(
            service.search("Frieren", 1, ("nyaa",)),
            service.search("Frieren", 1, ("nyaa",)),
        )

        self.assertEqual(adapter.calls, 1)
        self.assertNotEqual(first.items[0].result_id, second.items[0].result_id)


if __name__ == "__main__":
    unittest.main()
