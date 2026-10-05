from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

import tests  # noqa: F401
from app.indexers.errors import IndexerRateLimited, IndexerSecurityError
from app.indexers.models import (
    IndexerCapabilities,
    IndexerItem,
    IndexerPage,
    IndexerSearchRequest,
)
from app.indexers.providers.base import DirectResultAdapter
from app.indexers.providers.general import GeneralAdapter
from app.indexers.registry import IndexerRegistry
from app.indexers.result_store import IndexerResultStore
from app.indexers.service import IndexerService


class Member(DirectResultAdapter):
    def __init__(self, site_id, *, delay=0, error=None, paginated=False):
        self.site_id = self.site_name = site_id
        self.base_url = f"https://{site_id}.example/"
        self.default_enabled = True
        self.capabilities = IndexerCapabilities(paginated, ("magnet",))
        self.http = type("Http", (), {"aclose": AsyncMock()})()
        self.delay, self.error = delay, error
        self.calls = 0
        self.cancelled = False
        self.items = [IndexerItem(
            site_id=site_id, site_name=site_id, title="Example S01E01 1080p",
            detail_url=f"{self.base_url}movie/1", magnet="magnet:?xt=urn:btih:" + "a"*40,
            download_state="ready", download_kinds=("magnet",),
        )]

    async def search(self, request):
        self.calls += 1
        try:
            await asyncio.sleep(self.delay)
            if self.error:
                raise self.error
            return IndexerPage(self.items, request.page, self.capabilities.pagination_supported, self.capabilities.pagination_supported)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class GeneralTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_partial_failure_preserves_items_and_independent_cooldown(self):
        healthy, slow, limited = Member("healthy"), Member("slow", delay=1), Member("limited", error=IndexerRateLimited())
        adapter = GeneralAdapter((healthy, slow, limited), timeout_seconds=.02)
        page = await adapter.search(IndexerSearchRequest.create("Example"))
        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0].site_id, "btbtla")
        self.assertEqual(page.items[0].site_name, "healthy")
        self.assertEqual({e.code for e in page.errors}, {"timeout", "rate_limited"})
        self.assertTrue(slow.cancelled)
        await adapter.search(IndexerSearchRequest.create("Example"))
        self.assertEqual((healthy.calls, slow.calls, limited.calls), (2, 1, 1))

    async def test_followup_pages_only_query_paginated_sources(self):
        first, second = Member("first", paginated=True), Member("second")
        page = await GeneralAdapter((first, second)).search(IndexerSearchRequest.create("Example", 2))
        self.assertEqual((first.calls, second.calls), (1, 0))
        self.assertTrue(page.has_more)

    async def test_origin_routed_resolve_and_legacy_btbtla_origin(self):
        first, second = Member("first"), Member("btbtla")
        adapter = GeneralAdapter((first, second))
        for member in (first, second):
            result = await adapter.resolve(replace(member.items[0], site_id="btbtla"))
            self.assertEqual(result.value, member.items[0].magnet)
        with self.assertRaises(IndexerSecurityError):
            await adapter.resolve(replace(first.items[0], site_id="btbtla", detail_url="https://evil.example/movie/1"))
        with self.assertRaises(IndexerSecurityError):
            await adapter.resolve(first.items[0])

    async def test_general_torrent_bytes_and_url_use_origin_client_and_size_limit(self):
        from types import SimpleNamespace
        import httpx
        from app.indexers.downloads import _resolved_download_input
        from app.indexers.errors import IndexerResponseTooLarge
        from app.indexers.http import FixedHostHttpClient
        from app.indexers.models import ResolvedDownload
        from app.indexers.providers.aipan import AipanAdapter
        from app.indexers.providers.btbtla import BTBtlaAdapter

        torrent = b"d4:infod4:name4:Testee"
        requests = []
        def serve(request):
            requests.append(str(request.url))
            return httpx.Response(200, content=torrent, headers={"content-type": "application/x-bittorrent"})
        client = FixedHostHttpClient(allowed_hosts={"www.btbtlb.com"}, transport=httpx.MockTransport(serve),
            resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))])
        wrong_client = SimpleNamespace(get=AsyncMock(), allowed_hosts={"www.aipan.me"})
        registry = IndexerRegistry({"btbtla": GeneralAdapter((AipanAdapter(http=wrong_client), BTBtlaAdapter(http=client)))})
        service = SimpleNamespace(registry=registry)
        stored = IndexerItem(site_id="btbtla", site_name="not used for routing", title="Test", detail_url="https://www.btbtlb.com/tdown/1.htm")
        try:
            for value in ("https://www.btbtlb.com/file.torrent", torrent):
                result = await _resolved_download_input(service, stored, ResolvedDownload(kind="torrent", value=value))
                self.assertEqual(result.torrent_data, torrent)
                self.assertEqual(result.title, "Test")
            self.assertEqual(requests, ["https://www.btbtlb.com/file.torrent"])
            wrong_client.get.assert_not_awaited()
            client.max_response_bytes = len(torrent) - 1
            with self.assertRaises(IndexerResponseTooLarge):
                await _resolved_download_input(service, stored, ResolvedDownload(kind="torrent", value=torrent))
            with self.assertRaises(IndexerSecurityError):
                await _resolved_download_input(service, stored, ResolvedDownload(kind="torrent", value="https://www.aipan.me/file.torrent"))
        finally:
            await registry.aclose()

    async def test_cancellation_cancels_all_member_requests(self):
        members = (Member("a", delay=1), Member("b", delay=1))
        task = asyncio.create_task(GeneralAdapter(members).search(IndexerSearchRequest.create("Example")))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(all(m.cancelled for m in members))

    async def test_service_keeps_versions_on_same_detail_and_propagates_errors(self):
        member, failed = Member("aipan"), Member("dygang", error=IndexerRateLimited())
        member.items.append(replace(member.items[0], title="Example S02E01 4K", magnet="magnet:?xt=urn:btih:"+"b"*40))
        adapter = GeneralAdapter((member, failed))
        service = IndexerService(registry=IndexerRegistry({"btbtla":adapter}), result_store=IndexerResultStore())
        result = await service.search("Example", site_ids=("btbtla",))
        self.assertEqual(len(result.items), 2)
        self.assertTrue(result.partial)
        self.assertEqual(result.sites_succeeded, ("btbtla",))
        self.assertIn("dygang", result.errors[0].message)
        cached = await service.search("Example", site_ids=("btbtla",))
        self.assertTrue(cached.partial)
        self.assertEqual(member.calls, 1)
        await service.aclose()
        member.http.aclose.assert_awaited_once()
        failed.http.aclose.assert_awaited_once()

    async def test_same_hash_is_deduplicated_across_sources_with_provenance(self):
        first, second = Member("aipan"), Member("ys5266")
        second.items[0].seeders = 20
        adapter = GeneralAdapter((first, second))
        service = IndexerService(registry=IndexerRegistry({"btbtla": adapter}), result_store=IndexerResultStore())
        result = await service.search("Example", site_ids=("btbtla",))
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].site_name, "ys5266")
        resolved = await service.resolve(result.items[0].result_id)
        self.assertEqual(resolved.value, second.items[0].magnet)
        await service.aclose()

    async def test_empty_failed_search_is_not_cached_as_success(self):
        member = Member("dygang", error=IndexerRateLimited())
        adapter = GeneralAdapter((member,))
        service = IndexerService(registry=IndexerRegistry({"btbtla":adapter}), result_store=IndexerResultStore())
        result = await service.search("Example", site_ids=("btbtla",))
        self.assertTrue(result.partial)
        self.assertEqual(result.sites_succeeded, ())
        self.assertFalse((await service.search("Example", site_ids=("btbtla",))).cached)
        await service.aclose()
