from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import tests  # noqa: F401
from app.indexers.models import IndexerCapabilities, IndexerItem, IndexerPage, IndexerProviderError, IndexerSourceStatus
from app.indexers.providers.base import report_page
from app.indexers.providers.base import DirectResultAdapter
from app.indexers.registry import IndexerRegistry
from app.indexers.result_store import IndexerResultStore
from app.indexers.service import IndexerService
from app.routes import indexers_api
from tests import test_indexer_api


class ProgressiveSource(DirectResultAdapter):
    site_id = "btbtla"
    site_name = "综合"
    base_url = "https://example.com/"
    default_enabled = True
    capabilities = IndexerCapabilities(False, ("magnet",))

    def __init__(self, *, delayed=False, failure=False):
        self.http = SimpleNamespace(allowed_hosts={"example.com"})
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.delayed, self.failure = delayed, failure
        self.calls = 0
        self.cancelled = False
        self.first = IndexerItem(site_id="btbtla", site_name="Source A", title="Demo S01E01", detail_url="https://example.com/1", magnet="magnet:?xt=urn:btih:" + "a" * 40, download_state="ready", download_kinds=("magnet",))
        self.second = IndexerItem(site_id="btbtla", site_name="Source B", title="Demo S01E02", detail_url="https://example.com/2", magnet="magnet:?xt=urn:btih:" + "b" * 40, download_state="ready", download_kinds=("magnet",))

    async def search(self, request):
        self.calls += 1
        report_page("btbtla", IndexerPage([self.first], 1, False, False, source_statuses=(
            IndexerSourceStatus("a", "Source A", "success", 1), IndexerSourceStatus("b", "Source B", "searching"),
        ), complete=False))
        self.started.set()
        try:
            if self.delayed:
                await self.release.wait()
            else:
                await asyncio.sleep(.01)
            return IndexerPage([self.first, self.second] if not self.failure else [self.first], 1, False, False,
                errors=(IndexerProviderError("btbtla", "timeout", "Source B timed out"),) if self.failure else (),
                source_statuses=(IndexerSourceStatus("a", "Source A", "success", 1), IndexerSourceStatus("b", "Source B", "error" if self.failure else "success", 0 if self.failure else 1, code="timeout" if self.failure else "")))
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def service_for(source):
    return IndexerService(registry=IndexerRegistry({"btbtla": source}), result_store=IndexerResultStore())


class SearchProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_snapshot_precedes_finish_and_reference_is_frozen(self):
        source = ProgressiveSource(delayed=True)
        service = service_for(source)
        arrived = asyncio.Event()
        frames = []
        def observe(result):
            frames.append(result)
            arrived.set()
        task = asyncio.create_task(service.search("Demo", on_progress=observe))
        try:
            await asyncio.wait_for(arrived.wait(), 1)
            self.assertFalse(task.done())
            self.assertFalse(frames[0].complete)
            first_id = frames[0].items[0].result_id
            source.release.set()
            result = await task
            self.assertTrue(result.complete)
            self.assertEqual(len(result.items), 2)
            self.assertEqual(next(i.result_id for i in result.items if i.magnet == source.first.magnet), first_id)
            self.assertEqual(service.result_store.get(first_id).magnet, source.first.magnet)
        finally:
            source.release.set()
            await service.aclose()

    async def test_coalesced_consumers_receive_replay_without_duplicate_work(self):
        source = ProgressiveSource(delayed=True)
        service = service_for(source)
        a, b = [], []
        one = asyncio.create_task(service.search("Demo", on_progress=a.append))
        await source.started.wait()
        for _ in range(20):
            if a:break
            await asyncio.sleep(.005)
        two = asyncio.create_task(service.search("Demo", on_progress=b.append))
        for _ in range(20):
            if b:break
            await asyncio.sleep(.005)
        self.assertTrue(a and b)
        self.assertEqual(a[0].items[0].result_id, b[0].items[0].result_id)
        one.cancel()
        with self.assertRaises(asyncio.CancelledError):await one
        self.assertFalse(source.cancelled)
        source.release.set()
        result = await two
        self.assertEqual(source.calls, 1)
        self.assertTrue(result.complete)
        self.assertFalse(service._progress_callbacks)
        self.assertFalse(service._search_waiters)
        await service.aclose()

    async def test_stream_pins_do_not_degrade_normal_final_results_or_cache(self):
        from dataclasses import replace
        source = ProgressiveSource(delayed=True)
        original = source.first
        service = service_for(source)
        frames = []
        streamed = asyncio.create_task(service.search("Demo", on_progress=frames.append))
        await source.started.wait()
        for _ in range(20):
            if frames:break
            await asyncio.sleep(.005)
        normal = asyncio.create_task(service.search("Demo"))
        await asyncio.sleep(.005)
        source.first = replace(original, title="Demo S01E01 4K More metadata", seeders=99)
        source.release.set()
        streamed_result, normal_result = await asyncio.gather(streamed, normal)
        self.assertEqual(next(i.title for i in streamed_result.items if i.magnet == original.magnet), source.first.title)
        shown = next(i for i in streamed_result.items if i.magnet == original.magnet)
        self.assertEqual(shown.seeders, 99)
        self.assertEqual(shown.result_id, frames[0].items[0].result_id)
        self.assertEqual(shown.magnet, original.magnet)
        self.assertEqual(next(i.title for i in normal_result.items if i.magnet == original.magnet), source.first.title)
        cached = await service.search("Demo")
        self.assertTrue(cached.cached)
        self.assertEqual(next(i.title for i in cached.items if i.magnet == original.magnet), source.first.title)
        self.assertEqual(service.result_store.get(frames[0].items[0].result_id).title, original.title)
        await service.aclose()

    def test_display_update_never_rebinds_the_download_target(self):
        from dataclasses import replace
        source = ProgressiveSource()
        anchor = replace(source.first, result_id="frozen", size_bytes=123)
        better = replace(anchor, title="Demo 2026 2160p", site_name="Another source",
                         detail_url="https://example.com/other", magnet=anchor.magnet + "&dn=updated",
                         download_kinds=("torrent",), size_bytes=None, seeders=22, cluster_size=3)
        view = IndexerService._with_frozen_target(better, anchor)
        self.assertEqual((view.result_id, view.detail_url, view.magnet, view.download_kinds),
                         (anchor.result_id, anchor.detail_url, anchor.magnet, anchor.download_kinds))
        self.assertEqual(view.site_name, anchor.site_name)
        self.assertEqual((view.title, view.seeders, view.size_bytes, view.cluster_size), (better.title, 22, 123, 3))

    async def test_last_disconnect_cancels_underlying_search(self):
        source = ProgressiveSource(delayed=True)
        service = service_for(source)
        task = asyncio.create_task(service.search("Demo", on_progress=lambda _:None))
        await source.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(source.cancelled)
        self.assertFalse(service._inflight)
        self.assertFalse(service._progress_callbacks)
        await service.aclose()

    async def test_partial_success_is_not_a_combined_outage(self):
        service = service_for(ProgressiveSource(failure=True))
        result = await service.search("Demo")
        payload = indexers_api._search_payload(service, result)
        self.assertTrue(payload["partial"])
        status = payload["site_statuses"][0]
        self.assertEqual(status["status"], "success")
        self.assertEqual(status["message"], "")
        self.assertEqual(status["diagnostics"][1]["status"], "error")
        self.assertEqual(status["diagnostics"][1]["site_name"], "Source B")
        await service.aclose()

    async def test_in_progress_does_not_invent_queries_for_unselected_sites(self):
        from app.indexers.registry import build_default_registry
        source = ProgressiveSource()
        service = service_for(source)
        result = await service.search("Demo")
        registry = build_default_registry()
        result.complete = False
        result.items.clear()
        result.site_item_counts = {"btbtla": 0}
        result.sites_succeeded = ()
        result.source_statuses["btbtla"] = (IndexerSourceStatus("aipan", "爱盼", "searching"),)
        payload = indexers_api._search_payload(SimpleNamespace(registry=registry), result)
        active = [row for row in payload["site_statuses"] if row["status"] != "disabled"]
        self.assertEqual([row["site_id"] for row in active], ["btbtla"])
        self.assertEqual(active[0]["status"], "searching")
        await registry.aclose()
        await service.aclose()

    async def test_web_and_telegram_share_combined_partial_success_state(self):
        from app.modules.telegram_resource_search import _search_snapshot
        service = service_for(ProgressiveSource(failure=True))
        result = await service.search("Demo")
        web = indexers_api._search_payload(service, result)
        telegram = _search_snapshot(service, result)
        self.assertEqual(web["site_statuses"][0]["status"], "success")
        self.assertEqual(telegram["sites"][0]["status"], "success")
        self.assertEqual(telegram["sites"][0]["message"], "")
        self.assertTrue(telegram["partial"])
        result.items.clear()
        result.site_item_counts["btbtla"] = 0
        self.assertEqual(_search_snapshot(service, result)["sites"][0]["status"], "partial")
        await service.aclose()

    async def test_site_timeout_keeps_already_published_candidates(self):
        source = ProgressiveSource(delayed=True)
        service = service_for(source)
        service.site_timeout_seconds = .02
        frames = []
        result = await service.search("Demo", on_progress=frames.append)
        self.assertTrue(frames)
        self.assertTrue(result.partial)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].result_id, frames[0].items[0].result_id)
        self.assertTrue(source.cancelled)
        rows = result.source_statuses["btbtla"]
        self.assertEqual(rows[1].status, "error")
        self.assertEqual(rows[1].code, "timeout")
        self.assertEqual(indexers_api._search_payload(service, result)["site_statuses"][0]["status"], "success")
        await service.aclose()

    async def test_empty_partial_and_total_failure_are_distinct(self):
        source = ProgressiveSource(failure=True)
        service = service_for(source)
        result = await service.search("Demo")
        result.items.clear()
        result.site_item_counts["btbtla"] = 0
        result.source_statuses["btbtla"] = (
            IndexerSourceStatus("a", "Source A", "empty"), IndexerSourceStatus("b", "Source B", "error", code="timeout"),
        )
        self.assertEqual(indexers_api._search_payload(service, result)["site_statuses"][0]["status"], "partial")
        result.source_statuses["btbtla"] = tuple(IndexerSourceStatus(row.site_id, row.site_name, "error", code="timeout") for row in result.source_statuses["btbtla"])
        self.assertEqual(indexers_api._search_payload(service, result)["site_statuses"][0]["status"], "error")
        await service.aclose()

    async def test_stream_generator_emits_progress_before_real_completion(self):
        source = ProgressiveSource(delayed=True)
        service = service_for(source)
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        stream = indexers_api._stream_search(request, service, lambda cb: service.search("Demo", on_progress=cb))
        first = await asyncio.wait_for(anext(stream), 1)
        self.assertTrue(first.startswith("event: progress\n"))
        self.assertFalse(json.loads(first.split("data: ",1)[1])["complete"])
        source.release.set()
        last = await asyncio.wait_for(anext(stream), 1)
        self.assertTrue(last.startswith("event: complete\n"))
        self.assertTrue(json.loads(last.split("data: ",1)[1])["complete"])
        await stream.aclose()
        await service.aclose()

    async def test_stream_close_cancels_subscriber(self):
        source = ProgressiveSource(delayed=True)
        service = service_for(source)
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        stream = indexers_api._stream_search(request, service, lambda cb: service.search("Demo", on_progress=cb))
        await asyncio.wait_for(anext(stream), 1)
        await stream.aclose()
        self.assertTrue(source.cancelled)
        await service.aclose()


class StreamingAPIContractTests(unittest.TestCase):
    def setUp(self):
        self.api = test_indexer_api.IndexerAPITests()
        self.api.setUp()
    def tearDown(self):
        self.api.tearDown()

    def test_same_authenticated_endpoint_negotiates_stream_and_keeps_json(self):
        headers = self.api.authenticate()
        service = service_for(ProgressiveSource())
        try:
            with patch.object(indexers_api, "get_indexer_service", return_value=service):
                streamed = self.api.client.post("/api/indexers/search", json={"title":"Demo", "sites":["btbtla"]}, headers={**headers,"Accept":"text/event-stream"})
                normal = self.api.client.get("/api/indexers/search?q=Demo&sites=btbtla")
            self.assertEqual(streamed.status_code, 200, streamed.text)
            self.assertTrue(streamed.headers["content-type"].startswith("text/event-stream"))
            events = [block for block in streamed.text.split("\n\n") if block.startswith("event:")]
            self.assertTrue(events[0].startswith("event: progress"))
            self.assertTrue(events[-1].startswith("event: complete"))
            self.assertEqual(normal.status_code,200)
            self.assertTrue(normal.json()["complete"])
        finally:
            asyncio.run(service.aclose())
