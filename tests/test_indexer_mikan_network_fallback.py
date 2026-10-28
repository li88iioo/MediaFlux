"""Mikan 为每个端点保留完整读取预算，超时回落仍保持串行与取消安全。"""
from __future__ import annotations

import asyncio
import socket
import unittest

import httpx

from app.clients.http import FixedHostHttpClient, HttpResponse, HttpSecurityError
from app.indexers.errors import IndexerTimeout, IndexerUnavailable
from app.indexers.models import IndexerSearchRequest
from app.indexers.providers.mikan import MikanAdapter
from app.indexers.registry import IndexerRegistry
from app.indexers.result_store import IndexerResultStore
from app.indexers.service import IndexerService

_HTML = b'''<table><tbody><tr class="js-search-results-row">
<td><input data-magnet="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"></td>
<td><a href="/Home/Episode/fixture">Fixture Episode</a></td>
</tr></tbody></table>'''


class _Transport:
    def __init__(self, primary_error, mirror_error=None):
        self.primary_error = primary_error
        self.mirror_error = mirror_error
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        error = self.primary_error if len(self.calls) == 1 else self.mirror_error
        if error is not None:
            raise error
        return HttpResponse(
            url=url, status_code=200, headers={"content-type": "text/html; charset=utf-8"}, body=_HTML,
        )


class _ResponseBody(httpx.AsyncByteStream):
    def __init__(self, delay=None):
        self.delay = delay
        self.reading = asyncio.Event()
        self.cancelled = False
        self.closed = False

    async def __aiter__(self):
        yield _HTML[:8]
        self.reading.set()
        try:
            if self.delay is None:
                await asyncio.Event().wait()
            else:
                await asyncio.sleep(self.delay)
            yield _HTML[8:]
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aclose(self):
        self.closed = True


class MikanNetworkFallbackTests(unittest.IsolatedAsyncioTestCase):
    def _streaming_http(self, primary, mirror=None):
        bodies = {"mikanani.me": primary, "mikanime.tv": mirror}
        calls = []

        def handle(request):
            host = request.headers["host"]
            if calls:
                previous = bodies[calls[-1]]
                assert previous is not None
                self.assertTrue(previous.closed)
            calls.append(host)
            return httpx.Response(
                200, headers={"content-type": "text/html; charset=utf-8"}, stream=bodies[host],
            )

        http = FixedHostHttpClient(
            allowed_hosts=set(bodies),
            transport=httpx.MockTransport(handle),
            resolver=lambda _host, port: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            ],
            pin_resolved_address=True,
        )
        self.addAsyncCleanup(http.aclose)
        return http, calls

    async def test_slow_primary_body_does_not_consume_mirror_budget(self):
        primary, mirror = _ResponseBody(), _ResponseBody(delay=0.18)
        http, calls = self._streaming_http(primary, mirror)
        adapter = MikanAdapter(http=http, endpoint_timeout_seconds=0.2)
        service = IndexerService(
            registry=IndexerRegistry({"mikan": adapter}),
            result_store=IndexerResultStore(),
            site_timeout_seconds=0.2,
            total_timeout_seconds=0.2,
            max_concurrency=1,
        )
        self.addAsyncCleanup(service.aclose)

        result = await service.search("葬送的芙莉莲", site_ids=("mikan",))

        self.assertEqual(calls, ["mikanani.me", "mikanime.tv"])
        self.assertEqual(len(result.items), 1)
        self.assertFalse(result.errors)
        self.assertEqual(result.items[0].detail_url, "https://mikanime.tv/Home/Episode/fixture")
        self.assertTrue(primary.cancelled)
        self.assertTrue(primary.closed)
        self.assertTrue(mirror.closed)

    async def test_primary_can_use_almost_the_entire_endpoint_budget(self):
        primary = _ResponseBody(delay=0.18)
        http, calls = self._streaming_http(primary)

        page = await MikanAdapter(http=http, endpoint_timeout_seconds=0.2).search(
            IndexerSearchRequest.create("葬送的芙莉莲 01"),
        )

        self.assertEqual(len(page.items), 1)
        self.assertEqual(calls, ["mikanani.me"])
        self.assertTrue(primary.closed)
        self.assertFalse(primary.cancelled)

    async def test_stalled_single_or_all_endpoints_have_bounded_typed_timeouts(self):
        for mirrors in ((), ("https://mikanime.tv/",)):
            with self.subTest(mirrors=mirrors):
                primary, mirror = _ResponseBody(), _ResponseBody()
                http, calls = self._streaming_http(primary, mirror)
                adapter = MikanAdapter(
                    http=http, mirror_base_urls=mirrors, endpoint_timeout_seconds=0.1,
                )

                with self.assertRaises(IndexerTimeout) as caught:
                    await asyncio.wait_for(
                        adapter.search(IndexerSearchRequest.create("Fixture")), timeout=0.6,
                    )

                self.assertEqual(calls, ["mikanani.me", "mikanime.tv"] if mirrors else ["mikanani.me"])
                self.assertIn(calls[-1], str(caught.exception))
                self.assertTrue(primary.cancelled)
                self.assertTrue(primary.closed)
                self.assertEqual(mirror.cancelled, bool(mirrors))
                self.assertEqual(mirror.closed, bool(mirrors))

    def test_only_distinct_backup_endpoints_add_timeout_overhead(self):
        for mirrors, expected in (
            ((), 0),
            (("https://mikanani.me/",), 0),
            (("https://mikanime.tv/", "https://mikanime.tv/", "https://mikanani.me/"), 10),
        ):
            with self.subTest(mirrors=mirrors):
                adapter = MikanAdapter(http=_Transport(None), mirror_base_urls=mirrors)
                self.assertEqual(adapter.endpoint_timeout_seconds, 10)
                self.assertEqual(adapter.search_timeout_overhead_seconds(), expected)

    async def test_timeout_or_connection_failure_tries_registered_mirror(self):
        for error in (httpx.ReadTimeout("fixture timeout"), httpx.ConnectError("fixture connection failure")):
            with self.subTest(error=type(error).__name__):
                http = _Transport(error)
                page = await MikanAdapter(http=http).search(IndexerSearchRequest.create("Fixture"))
                self.assertEqual(len(page.items), 1)
                self.assertEqual(http.calls, [
                    "https://mikanani.me/Home/Search", "https://mikanime.tv/Home/Search",
                ])
                self.assertEqual(page.items[0].detail_url, "https://mikanime.tv/Home/Episode/fixture")

    async def test_exhausted_network_failures_use_typed_provider_errors(self):
        for error_type, expected in ((httpx.ReadTimeout, IndexerTimeout), (httpx.ConnectError, IndexerUnavailable)):
            with self.subTest(error=error_type.__name__):
                http = _Transport(error_type("fixture primary"), error_type("fixture mirror"))
                with self.assertRaises(expected):
                    await MikanAdapter(http=http).search(IndexerSearchRequest.create("Fixture"))
                self.assertEqual(len(http.calls), 2)

    async def test_caller_cancellation_does_not_start_another_network_request(self):
        primary, mirror = _ResponseBody(), _ResponseBody(delay=0)
        http, calls = self._streaming_http(primary, mirror)
        task = asyncio.create_task(MikanAdapter(http=http).search(IndexerSearchRequest.create("Fixture")))
        try:
            await asyncio.wait_for(primary.reading.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(calls, ["mikanani.me"])
        self.assertTrue(primary.cancelled)
        self.assertTrue(primary.closed)
        self.assertFalse(mirror.reading.is_set())

    async def test_security_failure_remains_fail_closed(self):
        for primary_fails in (True, False):
            with self.subTest(primary_fails=primary_fails):
                error = HttpSecurityError("upstream resolved to a non-public address")
                http = _Transport(error) if primary_fails else _Transport(httpx.ConnectError("offline"), error)
                with self.assertRaises(HttpSecurityError) as caught:
                    await MikanAdapter(http=http).search(IndexerSearchRequest.create("Fixture"))
                self.assertIs(caught.exception, error)
                self.assertEqual(len(http.calls), 1 if primary_fails else 2)
