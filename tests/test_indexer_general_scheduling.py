from __future__ import annotations

import asyncio
import hashlib
import unittest
from dataclasses import replace
from unittest.mock import patch

import tests  # noqa: F401
from app.indexers.errors import IndexerRateLimited
from app.indexers.models import (
    IndexerCapabilities,
    IndexerItem,
    IndexerPage,
    IndexerProviderError,
    IndexerSearchRequest,
)
from app.indexers.providers.base import page_observer
from app.indexers.providers.base import DirectResultAdapter
from app.indexers.providers.general import GeneralAdapter


class ScheduledMember(DirectResultAdapter):
    def __init__(
        self,
        site_id: str,
        *,
        items: list[IndexerItem] | None = None,
        gate: asyncio.Event | None = None,
        started: asyncio.Event | None = None,
        tracker: dict | None = None,
        error: Exception | None = None,
        page_errors: tuple[IndexerProviderError, ...] = (),
        paginated: bool = False,
    ):
        self.site_id = self.site_name = site_id
        self.base_url = f"https://{site_id}.example/"
        self.default_enabled = True
        self.capabilities = IndexerCapabilities(paginated, ("magnet",))
        self.http = type("Http", (), {})()
        self.gate = gate
        self.started = started
        self.tracker = tracker
        self.error = error
        self.page_errors = page_errors
        self.calls = 0
        self.cancelled = False
        self.items = list(items) if items is not None else [self._item(site_id, "S01E01")]

    @staticmethod
    def _item(site_id: str, position: str) -> IndexerItem:
        digest = hashlib.sha1(f"{site_id}-{position}".encode()).hexdigest()
        return IndexerItem(
            site_id=site_id,
            site_name=site_id,
            title=f"Example {position} 1080p",
            detail_url=f"https://{site_id}.example/movie/1",
            magnet=f"magnet:?xt=urn:btih:{digest}",
            download_state="ready",
            download_kinds=("magnet",),
        )

    async def search(self, request: IndexerSearchRequest) -> IndexerPage:
        self.calls += 1
        if self.tracker is not None:
            self.tracker["active"] += 1
            self.tracker["peak"] = max(self.tracker["peak"], self.tracker["active"])
            self.tracker["starts"].append(self.site_id)
        if self.started is not None:
            self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.error is not None:
                raise self.error
            return IndexerPage(
                items=[replace(item) for item in self.items],
                page=request.page,
                has_more=self.capabilities.pagination_supported,
                pagination_supported=self.capabilities.pagination_supported,
                errors=self.page_errors,
            )
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            if self.tracker is not None:
                self.tracker["active"] -= 1


class GeneralSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_cache_obeys_configured_ttl_instead_of_fixed_two_minutes(self):
        member = ScheduledMember("aipan")
        adapter = GeneralAdapter((member,), cache_ttl_seconds=.02)
        request = IndexerSearchRequest.create("Example")
        await adapter.search(request)
        await asyncio.sleep(.03)
        await adapter.search(request)
        self.assertEqual(member.calls, 2)

    async def test_partial_expiry_does_not_turn_good_candidates_into_empty_cooldown(self):
        member = ScheduledMember("dygang", page_errors=(IndexerProviderError("btbtla", "unavailable", "one detail failed"),))
        adapter = GeneralAdapter((member,))
        request = IndexerSearchRequest.create("Example")
        first = await adapter.search(request)
        adapter._cache[(member.site_id, request)].expires_at = 0
        second = await adapter.search(request)
        self.assertEqual(member.calls, 2)
        self.assertEqual([item.magnet for item in first.items], [item.magnet for item in second.items])
        self.assertEqual(second.source_statuses[0].status, "partial")

    async def test_failed_refresh_keeps_bounded_old_results_without_refreshing_their_age(self):
        from app.indexers.errors import IndexerUnavailable
        member = ScheduledMember("dygang")
        adapter = GeneralAdapter((member,))
        request = IndexerSearchRequest.create("Example")
        first = await adapter.search(request)
        cached = adapter._cache[(member.site_id, request)]
        cached.expires_at = 0
        stale_until = cached.stale_until
        member.error = IndexerUnavailable()
        second = await adapter.search(request)
        self.assertEqual([item.magnet for item in second.items], [item.magnet for item in first.items])
        self.assertTrue(second.source_statuses[0].cached)
        self.assertEqual(second.source_statuses[0].status, "partial")
        self.assertEqual(cached.stale_until, stale_until)
        await adapter.search(request)
        self.assertEqual(member.calls, 2)
        cached.stale_until = 0
        final = await adapter.search(request)
        self.assertFalse(final.items)
        self.assertEqual(final.source_statuses[0].status, "error")

    async def test_cold_results_match_legacy_member_flattening_and_keep_all_versions(self):
        def make_members():
            return (
                ScheduledMember("btbtla", items=[
                    ScheduledMember._item("btbtla", "S02E30"),
                    ScheduledMember._item("btbtla", "S02E31"),
                ]),
                ScheduledMember("aipan", items=[ScheduledMember._item("aipan", "S02E30 2160p")]),
                ScheduledMember("dygang", items=[ScheduledMember._item("dygang", "S01E01")]),
            )

        request = IndexerSearchRequest.create("Example", season=2, episode=30)
        legacy_members = make_members()
        legacy_pages = await asyncio.gather(*(member.search(request) for member in legacy_members))
        legacy = IndexerPage(
            items=[replace(item, site_id="btbtla") for old_page in legacy_pages for item in old_page.items],
            page=request.page,
            has_more=any(old_page.has_more for old_page in legacy_pages),
            pagination_supported=True,
            errors=tuple(dict.fromkeys(error for old_page in legacy_pages for error in old_page.errors)),
        )
        page = await GeneralAdapter(make_members()).search(request)

        self.assertEqual([item.title for item in page.items], [item.title for item in legacy.items])
        self.assertEqual([item.magnet for item in page.items], [item.magnet for item in legacy.items])
        self.assertEqual([item.site_id for item in page.items], [item.site_id for item in legacy.items])
        self.assertEqual(len(page.items), 4)
        self.assertTrue(page.complete)
        self.assertEqual([status.status for status in page.source_statuses], ["success"] * 3)

    async def test_starts_two_then_hedges_to_three_and_never_exceeds_limit(self):
        gate = asyncio.Event()
        tracker = {"active": 0, "peak": 0, "starts": []}
        started = {site_id: asyncio.Event() for site_id in ("aipan", "btbtla", "ys5266", "dygang")}
        members = tuple(
            ScheduledMember(site_id, gate=gate, started=started[site_id], tracker=tracker)
            for site_id in ("dygang", "btbtla", "aipan", "ys5266")
        )
        adapter = GeneralAdapter(members)
        progress = []
        token = page_observer.set(lambda site_id, page: progress.append((site_id, page)))
        search = asyncio.create_task(adapter.search(IndexerSearchRequest.create("Example")))
        try:
            await asyncio.wait_for(asyncio.gather(started["aipan"].wait(), started["btbtla"].wait()), 1)
            self.assertEqual(tracker["starts"], ["aipan", "btbtla"])
            await asyncio.sleep(0.03)
            self.assertEqual(len(tracker["starts"]), 2)
            await asyncio.wait_for(started["dygang"].wait(), 1)
            self.assertEqual(tracker["peak"], 3)
            gate.set()
            page = await asyncio.wait_for(search, 2)
        finally:
            gate.set()
            if not search.done():
                search.cancel()
                await asyncio.gather(search, return_exceptions=True)
            page_observer.reset(token)

        self.assertEqual(set(tracker["starts"]), {member.site_id for member in members})
        self.assertLessEqual(tracker["peak"], 3)
        self.assertTrue(progress)
        self.assertTrue(all(site_id == "btbtla" and not item.complete for site_id, item in progress))
        self.assertTrue(any(any(s.status == "searching" for s in item.source_statuses) for _, item in progress))
        self.assertTrue(page.complete)
        self.assertTrue(all(status.status == "success" for status in page.source_statuses))

    async def test_concurrent_requests_singleflight_and_cached_pages_are_cloned(self):
        member = ScheduledMember("aipan")
        original_title = member.items[0].title
        adapter = GeneralAdapter((member,))
        request = IndexerSearchRequest.create("Example")

        first_task = asyncio.create_task(adapter.search(request))
        second_task = asyncio.create_task(adapter.search(request))
        first, second = await asyncio.gather(first_task, second_task)
        self.assertEqual(member.calls, 1)
        self.assertIsNot(first.items[0], second.items[0])
        first.items[0].title = "mutated by first caller"
        self.assertEqual(second.items[0].title, original_title)

        cached = await adapter.search(request)
        self.assertEqual(member.calls, 1)
        self.assertTrue(cached.source_statuses[0].cached)
        self.assertEqual(cached.items[0].title, original_title)
        await adapter.search(IndexerSearchRequest.create("Example", season=1))
        await adapter.search(IndexerSearchRequest.create("Example", season=2))
        self.assertEqual(member.calls, 3)

    async def test_empty_has_short_cache_but_failures_remain_errors_and_uncached(self):
        empty = ScheduledMember("aipan", items=[])
        adapter = GeneralAdapter((empty,))
        request = IndexerSearchRequest.create("Nothing")
        with patch("app.indexers.providers.general._EMPTY_TTL", 0.02):
            first = await adapter.search(request)
            cached = await adapter.search(request)
            self.assertEqual(empty.calls, 1)
            self.assertEqual(first.source_statuses[0].status, "empty")
            self.assertTrue(cached.source_statuses[0].cached)
            await asyncio.sleep(0.03)
            await adapter.search(request)
            self.assertEqual(empty.calls, 2)

        failed = ScheduledMember("dygang", error=IndexerRateLimited())
        failed_adapter = GeneralAdapter((failed,))
        with patch("app.indexers.providers.general._COOLDOWN_SECONDS", 0):
            one = await failed_adapter.search(request)
            two = await failed_adapter.search(request)
        self.assertEqual(failed.calls, 2)
        self.assertEqual(one.source_statuses[0].status, "error")
        self.assertEqual(two.source_statuses[0].status, "error")
        self.assertFalse(two.source_statuses[0].cached)
        self.assertEqual(two.source_statuses[0].code, "rate_limited")

    async def test_cancelled_last_singleflight_subscriber_cancels_and_reaps_source(self):
        started = asyncio.Event()
        member = ScheduledMember("aipan", gate=asyncio.Event(), started=started)
        adapter = GeneralAdapter((member,))
        request = IndexerSearchRequest.create("Example")
        first = asyncio.create_task(adapter.search(request))
        second = asyncio.create_task(adapter.search(request))
        await asyncio.wait_for(started.wait(), 1)
        for _ in range(100):
            if any(flight.subscribers == 2 for flight in adapter._inflight.values()):
                break
            await asyncio.sleep(0)
        self.assertTrue(any(flight.subscribers == 2 for flight in adapter._inflight.values()))

        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        self.assertFalse(member.cancelled)
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
        await asyncio.sleep(0)
        self.assertTrue(member.cancelled)
        self.assertFalse(adapter._inflight)

    async def test_old_season_does_not_count_as_precise_coverage_for_backup_launch(self):
        release = ScheduledMember._item("aipan", "S01E30")
        old = ScheduledMember("aipan", items=[release])
        bt_gate = asyncio.Event()
        bt = ScheduledMember("btbtla", gate=bt_gate)
        dygang_started = asyncio.Event()
        dygang_gate = asyncio.Event()
        dygang = ScheduledMember("dygang", gate=dygang_gate, started=dygang_started)
        request = IndexerSearchRequest.create("Example", season=2, episode=30)
        adapter = GeneralAdapter((bt, dygang, old))
        old_page = IndexerPage([release], 1, False, False)
        self.assertFalse(adapter._has_exact_coverage(request, {"aipan": old_page}))
        target_item = ScheduledMember._item("aipan", "S02E30")
        target_page = IndexerPage([target_item], 1, False, False)
        self.assertTrue(adapter._has_exact_coverage(request, {"aipan": target_page}))
        wrong_show = replace(target_item, title="Unrelated Show S02E30")
        self.assertFalse(
            adapter._has_exact_coverage(request, {"aipan": IndexerPage([wrong_show], 1, False, False)})
        )
        unavailable = replace(
            target_item, download_state="unavailable", magnet=None, detail_url=None
        )
        self.assertFalse(
            adapter._has_exact_coverage(request, {"aipan": IndexerPage([unavailable], 1, False, False)})
        )

        resolvable_detail_only = IndexerItem(
            site_id="btbtla",
            site_name="btbtla",
            title="Example S02E30",
            detail_url="https://www.btbtlb.com/tdown/1.htm",
            download_state="resolvable",
        )
        self.assertTrue(
            adapter._has_valid_results(
                request, {"btbtla": IndexerPage([resolvable_detail_only], 1, False, False)}
            )
        )

        with patch("app.indexers.providers.general._HEDGE_DELAY_SECONDS", 0.5):
            task = asyncio.create_task(adapter.search(request))
            try:
                await asyncio.wait_for(dygang_started.wait(), 0.15)
            finally:
                bt_gate.set()
                dygang_gate.set()
            page = await asyncio.wait_for(task, 1)

        self.assertTrue(page.complete)
        self.assertIn(release.title, [item.title for item in page.items])
        self.assertEqual({status.site_id for status in page.source_statuses}, {"aipan", "btbtla", "dygang"})

    async def test_page_two_queries_every_pagination_capable_member(self):
        aipan = ScheduledMember("aipan", paginated=True)
        btbtla = ScheduledMember("btbtla", paginated=True)
        unsupported = ScheduledMember("unsupported")
        page = await GeneralAdapter((aipan, btbtla, unsupported)).search(
            IndexerSearchRequest.create("Example", 2)
        )
        self.assertEqual((aipan.calls, btbtla.calls, unsupported.calls), (1, 1, 0))
        self.assertEqual([status.site_id for status in page.source_statuses], ["aipan", "btbtla"])
        self.assertTrue(page.has_more)

    async def test_source_errors_are_isolated_and_partial_items_are_preserved(self):
        failed = ScheduledMember("aipan", error=RuntimeError("raw private exception text"))
        good = ScheduledMember("btbtla")
        partial_error = IndexerProviderError("dygang", "detail_timeout", "detail timed out")
        partial = ScheduledMember("dygang", page_errors=(partial_error,))
        page = await GeneralAdapter((failed, good, partial)).search(IndexerSearchRequest.create("Example"))
        self.assertEqual(len(page.items), 2)
        self.assertEqual({item.site_name for item in page.items}, {"btbtla", "dygang"})
        self.assertEqual(len(page.errors), 2)
        self.assertNotIn("raw private exception text", " ".join(error.message for error in page.errors))
        self.assertEqual(
            {status.site_id: status.status for status in page.source_statuses},
            {"aipan": "error", "btbtla": "success", "dygang": "partial"},
        )
        self.assertEqual(
            {status.site_id: status.code for status in page.source_statuses},
            {"aipan": "unavailable", "btbtla": "", "dygang": "detail_timeout"},
        )
        self.assertEqual(next(s for s in page.source_statuses if s.site_id == "dygang").count, 1)

    async def test_partial_page_is_cached_briefly_with_items_and_error_intact(self):
        partial_error = IndexerProviderError("dygang", "detail_timeout", "detail timed out")
        member = ScheduledMember("dygang", page_errors=(partial_error,))
        adapter = GeneralAdapter((member,))
        request = IndexerSearchRequest.create("Example")
        with (
            patch("app.indexers.providers.general._PARTIAL_TTL", 0.02),
            patch("app.indexers.providers.general._COOLDOWN_SECONDS", 0),
        ):
            first = await adapter.search(request)
            cached = await adapter.search(request)
            self.assertEqual(member.calls, 1)
            self.assertEqual(first.items[0].title, cached.items[0].title)
            self.assertEqual(first.errors, cached.errors)
            self.assertEqual(cached.source_statuses[0].status, "partial")
            self.assertTrue(cached.source_statuses[0].cached)
            await asyncio.sleep(0.03)
            expired = await adapter.search(request)

        self.assertEqual(member.calls, 2)
        self.assertEqual(expired.source_statuses[0].status, "partial")
        self.assertFalse(expired.source_statuses[0].cached)
        self.assertEqual(expired.errors, (partial_error,))


if __name__ == "__main__":
    unittest.main()
