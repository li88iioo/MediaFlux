"""Mikan 的端点读取预算、回落行为和验证页上报。"""
from __future__ import annotations

import asyncio
import socket
import unittest

import httpx

from app.clients.http import FixedHostHttpClient, HttpInvalidResponse, HttpResponse, HttpSecurityError
from app.indexers.errors import IndexerRateLimited, IndexerTimeout, IndexerUnavailable
from app.indexers.models import IndexerSearchRequest
from app.indexers.providers.mikan import MikanAdapter
from app.indexers.registry import IndexerRegistry
from app.indexers.result_store import IndexerResultStore
from app.indexers.service import IndexerService

_HTML = b'''<table><tbody><tr class="js-search-results-row">
<td><input data-magnet="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"></td>
<td><a href="/Home/Episode/fixture">Fixture Episode</a></td>
</tr></tbody></table>'''
_SEARCH_FORM = '<form action="/Home/Search"><input name="searchstr"></form>'


def _search_response(body: str, *, status: int = 200, headers: dict[str, str] | None = None) -> HttpResponse:
    return HttpResponse(
        url="https://mikanani.me/Home/Search", status_code=status,
        headers={"content-type": "text/html; charset=utf-8", **(headers or {})},
        body=body.encode("utf-8"),
    )


class _Transport:
    def __init__(self, primary, mirror=None):
        self.primary = primary
        self.mirror = mirror
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        response = self.primary if len(self.calls) == 1 else self.mirror
        if isinstance(response, Exception):
            raise response
        return response or HttpResponse(
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

    async def test_confirmed_interstitial_is_reported_without_mirror_fallback(self):
        for status, headers, body in (
            (200, {}, "<title>Just a moment...</title><table><tr><td>Verify you are human</td></tr></table>"),
            (200, {}, "<title>Attention Required! | Cloudflare</title><table></table>"),
            (403, {"cf-mitigated": "challenge"}, "<table></table>"),
            (403, {}, "<title>Attention Required! | Cloudflare</title><h1>Sorry, you have been blocked</h1>"),
            (503, {}, "<title>Just a moment...</title><h1>Performing security verification</h1>"),
        ):
            with self.subTest(status=status, body=body):
                response = _search_response(body, status=status, headers=headers)
                http = _Transport(response)
                service = IndexerService(
                    registry=IndexerRegistry({"mikan": MikanAdapter(http=http)}),
                    result_store=IndexerResultStore(),
                )
                self.addAsyncCleanup(service.aclose)

                result = await service.search("Fixture", site_ids=("mikan",))

                self.assertEqual(result.items, [])
                self.assertEqual(result.sites_succeeded, ())
                self.assertEqual([(error.site_id, error.code) for error in result.errors], [
                    ("mikan", "challenge_required"),
                ])
                self.assertEqual(http.calls, [response.url])

    async def test_ordinary_http_failures_keep_mirror_fallback_and_error_types(self):
        for status, body, headers, error in (
            (429, "<title>Too Many Requests</title>", {}, IndexerRateLimited),
            (503, "<title>Service Unavailable</title>", {}, IndexerUnavailable),
            (503, '{"error": "Service unavailable"}', {"content-type": "application/json"}, IndexerUnavailable),
        ):
            with self.subTest(status=status, headers=headers):
                response = _search_response(body, status=status, headers=headers)
                http = _Transport(response, response)

                with self.assertRaises(error):
                    await MikanAdapter(http=http).search(IndexerSearchRequest.create("Fixture"))

                self.assertEqual(http.calls, [response.url, "https://mikanime.tv/Home/Search"])

    async def test_ordinary_empty_search_pages_succeed_without_mirror_fallback(self):
        title = "<title>Mikan Project - 搜索结果</title>"
        for body in (
            title + _SEARCH_FORM * 3
            + '<p>"MFAuditNoMatch83051" 找不到对应结果 &gt;_&lt; 试试别的关键词吧。</p>'
            + "<p>找不到对应结果 &gt;_&lt; 这个结果是禁止说明的事项</p>",
            title + _SEARCH_FORM + "<table><thead><tr><th>标题</th></tr></thead><tbody></tbody></table>",
            "<p>没有找到相关结果</p>",
            "<p>No results found</p>",
        ):
            with self.subTest(body=body):
                response = _search_response(body)
                http = _Transport(response)

                page = await MikanAdapter(http=http).search(IndexerSearchRequest.create("Fixture"))

                self.assertEqual(page.items, [])
                self.assertEqual(http.calls, [response.url])

    async def test_unrelated_table_and_login_page_are_invalid_search_responses(self):
        for body in (
            "<title>Welcome</title><table><tbody></tbody></table>",
            "<title>Mikan Project - 登录</title>" + _SEARCH_FORM + "<a>还没有账号？赶紧来注册一个吧~</a>",
        ):
            with self.subTest(body=body):
                response = _search_response(body)
                http = _Transport(response, response)

                with self.assertRaises(HttpInvalidResponse):
                    await MikanAdapter(http=http).search(IndexerSearchRequest.create("Fixture"))

                self.assertEqual(http.calls, [response.url, "https://mikanime.tv/Home/Search"])

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
