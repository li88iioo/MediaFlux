from __future__ import annotations

import asyncio
from pathlib import Path
import unittest

import httpx

from app.indexers.errors import (
    IndexerInvalidResponse,
    IndexerRateLimited,
    IndexerResultExpired,
    IndexerSecurityError,
    IndexerUnavailable,
)
from app.indexers.http import BrowserImpersonatingHttpClient, FixedHostHttpClient, IndexerHttpResponse
from app.indexers.models import IndexerItem, IndexerSearchRequest
from app.indexers.providers.btbtla import BTBtlaAdapter
from app.indexers.providers.google_site import GoogleSiteSearch
from app.indexers.providers.mikan import MikanAdapter
from app.indexers.providers.onelou import OneLouAdapter
from app.indexers.providers.nyaa import NyaaAdapter
from app.indexers.providers.piratebay import PirateBayAdapter
from app.indexers.registry import IndexerRegistry, build_default_registry


_INDEXER_FIXTURES = Path(__file__).with_name("fixtures") / "indexers"
NYAA_HTML = (_INDEXER_FIXTURES / "nyaa-search.html").read_bytes()

MIKAN_HTML = b"""
<table><tbody>
<tr class="js-search-results-row">
  <td><input class="js-episode-select" data-magnet="magnet:?xt=urn:btih:89abcdef0123456789abcdef0123456789abcdef" /></td>
  <td><a href="/Home/Episode/abc">[LoliHouse] Frieren 01 [1080p]</a></td>
</tr>
</tbody></table>
"""

BTBTLA_SEARCH_HTML = (_INDEXER_FIXTURES / "btbtla-search.html").read_bytes()

BTBTLA_DETAIL_HTML = (_INDEXER_FIXTURES / "btbtla-detail.html").read_bytes()
BTBTLA_DOWNLOAD_HTML = (_INDEXER_FIXTURES / "btbtla-download.html").read_bytes()

ONELOU_GOOGLE_HTML = """
<html><body>
  <a href="https://www.1lou.me/thread-101.htm"><h3>Frieren Complete 1080p</h3></a>
  <a href="/url?q=https%3A%2F%2Fwww.1lou.pro%2Fthread-102.htm"><h3>Frieren 夸克网盘</h3></a>
  <a href="https://evil.example/thread-103.htm"><h3>Unsafe</h3></a>
</body></html>
""".encode()

ONELOU_GOOGLE_CONFLICTING_META_HTML = """
<html><head><meta charset="windows-1252"></head><body>
  <a href="https://www.1lou.me/thread-101.htm"><h3>Frieren Complete 1080p</h3></a>
  <a href="https://www.1lou.me/thread-102.htm"><h3>Frieren 夸克网盘</h3></a>
</body></html>
""".encode("utf-8")

ONELOU_GOOGLE_INTERSTITIAL = """
<html><body>Google Search 如果您在几秒钟内没有被重定向，请点击此处。</body></html>
""".encode()

ONELOU_GOOGLE_EMPTY_HTML = """
<html><body>找不到和您查询的内容相符的任何文件。</body></html>
""".encode()

ONELOU_HTML = """
<ul><li class="media thread"><div class="subject">
  <a href="thread-201.htm">Frieren Legacy 1080p</a>
</div></li></ul>
""".encode()

ONELOU_API_JSON = """
{"ok": 1, "data": {"hits": [
  {"subject": "<em>Frieren</em> Complete 1080p", "thread_url": "thread-101.htm",
   "create_date": 1720000000, "fid": 4, "username": "u", "posts": 3},
  {"subject": "Frieren 夸克网盘", "thread_url": "thread-102.htm",
   "create_date": 1720000000, "fid": 4, "username": "u", "posts": 1}
]}}
""".encode()
ONELOU_API_EMPTY = b'{"ok": 1, "data": {"hits": []}}'

ONELOU_DETAIL_HTML = b'<a href="/attach-download-frieren.torrent">Frieren.torrent</a>'
ONELOU_DETAIL_MAGNET_HTML = (
    b'<a href="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&amp;dn=Frieren">magnet</a>'
)

TPB_JSON = (_INDEXER_FIXTURES / "tpb-search.json").read_bytes()




class FakeHttpClient:
    def __init__(
        self,
        body: bytes,
        content_type: str = "text/html; charset=utf-8",
        status_code: int = 200,
    ):
        self.body = body
        self.content_type = content_type
        self.status_code = status_code
        self.responses = []
        self.status_codes = []
        self.content_types = []
        self.calls = []

    async def get(self, url: str, *, params=None, headers=None, max_redirects=3):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        body = self.responses.pop(0) if self.responses else self.body
        status_code = self.status_codes.pop(0) if self.status_codes else self.status_code
        content_type = self.content_types.pop(0) if self.content_types else self.content_type
        return IndexerHttpResponse(
            url=url,
            status_code=status_code,
            headers={"content-type": content_type},
            body=body,
        )


class ClosableHttpClient(FakeHttpClient):
    def __init__(self, body: bytes, content_type: str = "text/html; charset=utf-8"):
        super().__init__(body, content_type=content_type)
        self.close_calls = 0

    async def aclose(self):
        self.close_calls += 1


async def _record_sleep(calls: list[float], delay: float) -> None:
    calls.append(delay)


class IndexerProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_tpb_builds_magnet_and_filters_unrelated_hot_results(self):
        adapter = PirateBayAdapter(
            http=FakeHttpClient(TPB_JSON, content_type="application/json"),
        )

        page = await adapter.search(IndexerSearchRequest.create("Frieren"))

        self.assertEqual([item.title for item in page.items], ["Frieren S01 1080p"])
        item = page.items[0]
        self.assertEqual(item.category, "Video")
        self.assertEqual(item.size_bytes, 2147483648)
        self.assertEqual(item.seeders, 50)
        self.assertEqual(item.leechers, 3)
        self.assertIn("urn:btih:0123456789abcdef0123456789abcdef01234567", item.magnet)
        self.assertIn("dn=Frieren%20S01%201080p", item.magnet)
        self.assertIn("tr=udp%3A%2F%2Ftracker.coppersurfer.tk%3A6969%2Fannounce", item.magnet)
        self.assertIn("tr=udp%3A%2F%2Ftracker.leechers-paradise.org%3A6969", item.magnet)
        self.assertIn("tr=udp%3A%2F%2Fopen.demonii.com%3A1337%2Fannounce", item.magnet)

    async def test_tpb_normalizes_query_punctuation_before_local_filtering(self):
        payload = b'''[{"id":"1","name":"Dune Part Two 2024 2160p",\n          "info_hash":"0123456789ABCDEF0123456789ABCDEF01234567",\n          "size":"100","seeders":"1","leechers":"0","added":"1720000000","category":"201"}]'''
        adapter = PirateBayAdapter(
            http=FakeHttpClient(payload, content_type="application/json"),
        )

        page = await adapter.search(IndexerSearchRequest.create("Dune: Part Two"))

        self.assertEqual([item.title for item in page.items], ["Dune Part Two 2024 2160p"])

    async def test_tpb_filters_non_video_categories_for_media_searches(self):
        payload = b'''[
          {"id":"1","name":"Demo Movie Video","info_hash":"0123456789ABCDEF0123456789ABCDEF01234567",
           "size":"100","seeders":"1","leechers":"0","added":"1720000000","category":"201"},
          {"id":"2","name":"Demo Movie Game","info_hash":"89ABCDEF0123456789ABCDEF0123456789ABCDEF",
           "size":"100","seeders":"1","leechers":"0","added":"1720000000","category":"401"}
        ]'''
        adapter = PirateBayAdapter(
            http=FakeHttpClient(payload, content_type="application/json"),
        )

        page = await adapter.search(IndexerSearchRequest.create("Demo Movie", media_type="movie"))

        self.assertEqual([item.title for item in page.items], ["Demo Movie Video"])

    async def test_tpb_treats_api_no_result_sentinel_as_empty_page(self):
        http = FakeHttpClient(b'[{"id":"0"}]', content_type="application/json")

        page = await PirateBayAdapter(http=http).search(IndexerSearchRequest.create("Frieren"))

        self.assertEqual(page.items, [])
        self.assertFalse(page.has_more)
        self.assertFalse(page.pagination_supported)

    async def test_tpb_tracker_policy_deduplicates_and_rejects_invalid_urls(self):
        adapter = PirateBayAdapter(
            http=FakeHttpClient(TPB_JSON, content_type="application/json"),
            trackers=(
                "udp://tracker.example:6969/announce",
                "udp://tracker.example:6969/announce",
                "javascript:alert(1)",
                "https://user:secret@tracker.example/announce",
            ),
        )

        page = await adapter.search(IndexerSearchRequest.create("Frieren"))
        magnet = page.items[0].magnet or ""

        self.assertEqual(magnet.count("tracker.example"), 1)
        self.assertNotIn("javascript", magnet)
        self.assertNotIn("secret", magnet)

    async def test_tpb_rejects_malformed_json(self):
        adapter = PirateBayAdapter(
            http=FakeHttpClient(b"{", content_type="application/json"),
        )

        with self.assertRaises(IndexerInvalidResponse):
            await adapter.search(IndexerSearchRequest.create("Frieren"))

    async def test_tpb_skips_invalid_info_hash(self):
        payload = TPB_JSON.replace(
            b"0123456789ABCDEF0123456789ABCDEF01234567",
            b"not-a-valid-info-hash",
        )
        adapter = PirateBayAdapter(
            http=FakeHttpClient(payload, content_type="application/json"),
        )

        page = await adapter.search(IndexerSearchRequest.create("Frieren"))

        self.assertEqual(page.items, [])

    async def test_tpb_skips_non_string_titles_and_info_hashes(self):
        payload = b"""[
          {"id":"1","name":"Frieren Valid","info_hash":"0123456789ABCDEF0123456789ABCDEF01234567",
           "size":"100","seeders":"1","leechers":"0","added":"1720000000","category":"201"},
          {"id":"2","name":["Frieren"],"info_hash":"89ABCDEF0123456789ABCDEF0123456789ABCDEF",
           "size":"100","seeders":"1","leechers":"0","added":"1720000000","category":"201"},
          {"id":"3","name":"Frieren Numeric Hash","info_hash":1111111111111111111111111111111111111111,
           "size":"100","seeders":"1","leechers":"0","added":"1720000000","category":"201"}
        ]"""
        adapter = PirateBayAdapter(
            http=FakeHttpClient(payload, content_type="application/json"),
        )

        page = await adapter.search(IndexerSearchRequest.create("Frieren"))

        self.assertEqual([item.title for item in page.items], ["Frieren Valid"])

    async def test_tpb_skips_negative_numeric_records_without_aborting_search(self):
        payload = b"""[
          {"id":"1","name":"Frieren Valid","info_hash":"0123456789ABCDEF0123456789ABCDEF01234567",
           "size":"100","seeders":"1","leechers":"0","added":"1720000000","category":"201"},
          {"id":"2","name":"Frieren Negative Size","info_hash":"89ABCDEF0123456789ABCDEF0123456789ABCDEF",
           "size":"-1","seeders":"1","leechers":"0","added":"1720000000","category":"201"},
          {"id":"3","name":"Frieren Negative Seeders","info_hash":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
           "size":"100","seeders":"-1","leechers":"0","added":"1720000000","category":"201"},
          {"id":"4","name":"Frieren Negative Leechers","info_hash":"BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
           "size":"100","seeders":"1","leechers":"-1","added":"1720000000","category":"201"},
          {"id":"5","name":"Frieren Negative Added","info_hash":"CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
           "size":"100","seeders":"1","leechers":"0","added":"-1","category":"201"}
        ]"""
        adapter = PirateBayAdapter(
            http=FakeHttpClient(payload, content_type="application/json"),
        )

        page = await adapter.search(IndexerSearchRequest.create("Frieren"))

        self.assertEqual([item.title for item in page.items], ["Frieren Valid"])

    async def test_tpb_page_two_is_empty_without_upstream_request(self):
        http = FakeHttpClient(TPB_JSON, content_type="application/json")

        page = await PirateBayAdapter(http=http).search(IndexerSearchRequest.create("Frieren", page=2))

        self.assertEqual(page.items, [])
        self.assertFalse(page.has_more)
        self.assertFalse(page.pagination_supported)
        self.assertEqual(http.calls, [])

    async def test_tpb_pacer_delays_repeated_search_slots(self):
        sleeps: list[float] = []
        times = iter([20.0, 20.0, 20.5, 21.0])
        monotonic = lambda: next(times)
        sleeper = lambda delay: _record_sleep(sleeps, delay)
        tpb = PirateBayAdapter(
            http=FakeHttpClient(TPB_JSON, content_type="application/json"),
            min_interval_seconds=1,
            monotonic=monotonic,
            sleeper=sleeper,
        )
        request = IndexerSearchRequest.create("Frieren")

        await tpb.wait_for_search_slot(request)
        await tpb.wait_for_search_slot(request)

        self.assertEqual(sleeps, [0.5])

    async def test_search_pacer_handles_zero_monotonic_origin(self):
        sleeps: list[float] = []
        times = iter([0.0, 0.0, 0.25, 1.0])
        adapter = PirateBayAdapter(
            http=FakeHttpClient(TPB_JSON, content_type="application/json"),
            min_interval_seconds=1,
            monotonic=lambda: next(times),
            sleeper=lambda delay: _record_sleep(sleeps, delay),
        )
        request = IndexerSearchRequest.create("Frieren")

        await adapter.wait_for_search_slot(request)
        await adapter.wait_for_search_slot(request)

        self.assertEqual(sleeps, [0.75])
    async def test_nyaa_parses_direct_downloads_counts_and_native_pagination(self):
        http = FakeHttpClient(NYAA_HTML)
        adapter = NyaaAdapter(
            site_id="nyaa",
            site_name="Nyaa",
            base_url="https://nyaa.si/",
            http=http,
            default_enabled=True,
        )

        page = await adapter.search(IndexerSearchRequest.create("Frieren", page=1))

        self.assertTrue(adapter.capabilities.pagination_supported)
        self.assertTrue(page.pagination_supported)
        self.assertTrue(page.has_more)
        self.assertEqual(len(page.items), 1)
        item = page.items[0]
        self.assertEqual(item.site_id, "nyaa")
        self.assertEqual(item.title, "[Group] Frieren 01")
        self.assertEqual(item.seeders, 88)
        self.assertEqual(item.leechers, 4)
        self.assertEqual(item.downloads, 500)
        self.assertEqual(item.size_bytes, 1288490188)
        self.assertEqual(item.download_state, "ready")
        self.assertEqual(item.download_kinds, ("magnet", "torrent"))
        self.assertTrue(item.detail_url.endswith("/view/123"))
        self.assertTrue(item.torrent_url.endswith("/download/123.torrent"))
        self.assertEqual(http.calls[0]["params"]["p"], "1")
        self.assertEqual(http.calls[0]["params"]["q"], "Frieren")

    async def test_nyaa_detects_mirror_div_pagination(self):
        mirror_html = NYAA_HTML.replace(
            b'<ul class="pagination">',
            b'<div class="pagination">',
        ).replace(b"</ul>", b"</div>")
        adapter = NyaaAdapter(
            site_id="nyaa",
            site_name="Nyaa",
            base_url="https://nyaa.net/",
            http=FakeHttpClient(mirror_html),
            default_enabled=True,
        )

        page = await adapter.search(IndexerSearchRequest.create("Frieren", page=1))

        self.assertTrue(page.has_more)
        self.assertTrue(page.pagination_supported)

    async def test_nyaa_ignores_unrelated_pagination_component(self):
        unrelated_pagination = NYAA_HTML.replace(
            b'<ul class="pagination"><li><a href="/?p=2">Next</a></li></ul>',
            b'<footer><div class="pagination"><a href="/help?p=2">Next</a></div></footer>',
        )
        adapter = NyaaAdapter(
            site_id="nyaa",
            site_name="Nyaa",
            base_url="https://nyaa.net/",
            http=FakeHttpClient(unrelated_pagination),
            default_enabled=True,
        )

        page = await adapter.search(IndexerSearchRequest.create("Frieren", page=1))

        self.assertFalse(page.has_more)

    async def test_nyaa_maps_requested_sort_to_upstream_subset(self):
        http = FakeHttpClient(NYAA_HTML)
        adapter = NyaaAdapter(
            site_id="nyaa",
            site_name="Nyaa",
            base_url="https://nyaa.si/",
            http=http,
            default_enabled=True,
        )

        await adapter.search(IndexerSearchRequest.create("Frieren", sort_mode="published_desc"))
        await adapter.search(IndexerSearchRequest.create("Frieren", sort_mode="size_asc"))

        self.assertEqual((http.calls[0]["params"]["s"], http.calls[0]["params"]["o"]), ("id", "desc"))
        self.assertEqual((http.calls[1]["params"]["s"], http.calls[1]["params"]["o"]), ("size", "asc"))

    async def test_nyaa_ignores_comment_links_and_bad_rows(self):
        body = b"""
        <table class="torrent-list"><tbody>
        <tr>
          <td><a title="Anime">Anime</a></td>
          <td>
            <a href="/view/123#comments" title="Comments">2</a>
            <a href="/view/123" title="Tefuda ga Oome no Victoria 04">Real title</a>
          </td>
          <td><a href="/download/123.torrent">torrent</a><a href="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567">magnet</a></td>
          <td>1 GiB</td><td data-timestamp="1720000000">date</td><td>10</td><td>2</td><td>30</td>
        </tr>
        <tr>
          <td><a title="Anime">Anime</a></td>
          <td><a href="/view/999#comments" title="Comments">1</a></td>
          <td><a href="https://evil.example/file.torrent">torrent</a></td>
          <td>1 GiB</td><td>date</td><td>1</td><td>0</td><td>1</td>
        </tr>
        </tbody></table>
        """
        adapter = NyaaAdapter(
            site_id="nyaa",
            site_name="Nyaa",
            base_url="https://nyaa.si/",
            http=FakeHttpClient(body),
            default_enabled=True,
        )

        page = await adapter.search(IndexerSearchRequest.create("Tefuda"))

        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0].title, "Tefuda ga Oome no Victoria 04")
        self.assertEqual(page.items[0].detail_url, "https://nyaa.si/view/123")

    async def test_nyaa_falls_back_to_mirror_when_primary_is_rate_limited(self):
        class ScriptedHttpClient:
            def __init__(self, outcomes):
                self.outcomes = list(outcomes)
                self.calls = []

            async def get(self, url, *, params=None, headers=None, max_redirects=3):
                self.calls.append({"url": url, "params": dict(params or {})})
                status_code, body = self.outcomes.pop(0)
                return IndexerHttpResponse(
                    url=url,
                    status_code=status_code,
                    headers={"content-type": "text/html; charset=utf-8"},
                    body=body,
                )

        http = ScriptedHttpClient([(429, b""), (200, NYAA_HTML), (200, NYAA_HTML)])
        adapter = NyaaAdapter(
            site_id="nyaa",
            site_name="Nyaa",
            base_url="https://nyaa.si/",
            http=http,
            default_enabled=True,
            mirror_base_urls=("https://nyaa.net/",),
        )

        page = await adapter.search(IndexerSearchRequest.create("Frieren", page=1))

        self.assertEqual(len(http.calls), 2)
        self.assertTrue(http.calls[0]["url"].startswith("https://nyaa.si/"))
        self.assertTrue(http.calls[1]["url"].startswith("https://nyaa.net/"))
        self.assertEqual(len(page.items), 1)
        # 镜像返回的相对链接必须落在镜像域名上。
        self.assertEqual(page.items[0].detail_url, "https://nyaa.net/view/123")

        await adapter.search(IndexerSearchRequest.create("Frieren 2", page=1))
        self.assertEqual(len(http.calls), 3)
        self.assertTrue(
            http.calls[2]["url"].startswith("https://nyaa.net/"),
            "最近成功的镜像应成为下一次搜索首选，避免重复请求已限流主站",
        )

        resolved = await adapter.resolve(page.items[0])
        self.assertEqual(resolved.kind, "magnet")

    async def test_nyaa_endpoint_timeout_still_leaves_budget_for_mirror(self):
        class SlowPrimaryHttpClient:
            def __init__(self):
                self.calls = []

            async def get(self, url, *, params=None, headers=None, max_redirects=3):
                self.calls.append({"url": url, "params": dict(params or {})})
                if url.startswith("https://nyaa.si/"):
                    await asyncio.sleep(0.2)
                return IndexerHttpResponse(
                    url=url,
                    status_code=200,
                    headers={"content-type": "text/html; charset=utf-8"},
                    body=NYAA_HTML,
                )

        http = SlowPrimaryHttpClient()
        adapter = NyaaAdapter(
            site_id="nyaa",
            site_name="Nyaa",
            base_url="https://nyaa.si/",
            http=http,
            default_enabled=True,
            mirror_base_urls=("https://nyaa.net/",),
            endpoint_timeout_seconds=0.01,
        )

        page = await adapter.search(IndexerSearchRequest.create("Frieren"))

        self.assertEqual(len(page.items), 1)
        self.assertEqual(len(http.calls), 2)
        self.assertTrue(http.calls[0]["url"].startswith("https://nyaa.si/"))
        self.assertTrue(http.calls[1]["url"].startswith("https://nyaa.net/"))

    async def test_nyaa_mirror_torrent_url_resolves_against_mirror_host(self):
        adapter = NyaaAdapter(
            site_id="nyaa",
            site_name="Nyaa",
            base_url="https://nyaa.si/",
            http=FakeHttpClient(NYAA_HTML),
            default_enabled=True,
            mirror_base_urls=("https://nyaa.net/",),
        )
        stored = IndexerItem(
            site_id="nyaa",
            site_name="Nyaa",
            title="Frieren",
            detail_url="https://nyaa.net/view/123",
            torrent_url="https://nyaa.net/download/123.torrent",
            download_state="ready",
            download_kinds=("torrent",),
        )

        resolved = await adapter.resolve(stored)

        self.assertEqual(resolved.kind, "torrent")
        self.assertEqual(resolved.value, "https://nyaa.net/download/123.torrent")
        with self.assertRaises(IndexerSecurityError):
            await adapter.resolve(IndexerItem(
                site_id="nyaa",
                site_name="Nyaa",
                title="Frieren",
                detail_url="https://evil.example/view/1",
                torrent_url="https://evil.example/download/1.torrent",
                download_state="ready",
                download_kinds=("torrent",),
            ))

    async def test_nyaa_parses_mirror_layout_with_class_based_counters(self):
        body = b"""
        <table class="table"><tbody>
        <tr class="torrent-info">
          <td><a class="cat-icon" title="Anime">Anime</a></td>
          <td><a href="/view/456" title="[Mirror] Frieren 02">[Mirror] Frieren 02</a></td>
          <td><a href="/download/456.torrent">torrent</a></td>
          <td class="col-size">700 MiB</td>
          <td class="col-date" title="2026-07-26T00:00:00Z">2026-07-26</td>
          <td class="num-s">42</td><td class="num-l">3</td><td class="num-c">120</td>
        </tr>
        </tbody></table>
        """
        adapter = NyaaAdapter(
            site_id="nyaa",
            site_name="Nyaa",
            base_url="https://nyaa.net/",
            http=FakeHttpClient(body),
            default_enabled=True,
        )

        page = await adapter.search(IndexerSearchRequest.create("Frieren"))

        self.assertEqual(len(page.items), 1)
        item = page.items[0]
        self.assertEqual(item.title, "[Mirror] Frieren 02")
        self.assertEqual(item.seeders, 42)
        self.assertEqual(item.leechers, 3)
        self.assertEqual(item.downloads, 120)
        self.assertEqual(item.size_text, "700 MiB")
        self.assertIsNotNone(item.published_at)
        self.assertEqual(item.detail_url, "https://nyaa.net/view/456")

    async def test_nyaa_prefers_media_category_and_falls_back_to_all(self):
        empty_html = b'<table class="torrent-list"><tbody></tbody></table><p>No results found</p>'
        http = FakeHttpClient(NYAA_HTML)
        http.responses = [empty_html, NYAA_HTML]
        adapter = NyaaAdapter(
            site_id="nyaa",
            site_name="Nyaa",
            base_url="https://nyaa.si/",
            http=http,
            default_enabled=True,
        )

        page = await adapter.search(
            IndexerSearchRequest.create("Frieren", media_type="tv")
        )

        self.assertEqual(len(page.items), 1)
        self.assertEqual([call["params"]["c"] for call in http.calls], ["1_0", "0_0"])

    async def test_sukebei_prefers_adult_category_before_all(self):
        http = FakeHttpClient(NYAA_HTML)
        adapter = NyaaAdapter(
            site_id="sukebei",
            site_name="Sukebei",
            base_url="https://sukebei.nyaa.si/",
            http=http,
            default_enabled=False,
        )

        page = await adapter.search(IndexerSearchRequest.create("Example"))

        self.assertEqual(len(page.items), 1)
        self.assertEqual(http.calls[0]["params"]["c"], "2_2")

    async def test_mikan_returns_page_one_without_inventing_pagination(self):
        http = FakeHttpClient(MIKAN_HTML)
        adapter = MikanAdapter(http=http)

        first = await adapter.search(IndexerSearchRequest.create("Frieren", page=1))
        second = await adapter.search(IndexerSearchRequest.create("Frieren", page=2))

        self.assertFalse(adapter.capabilities.pagination_supported)
        self.assertFalse(first.pagination_supported)
        self.assertFalse(first.has_more)
        self.assertEqual(first.items[0].download_kinds, ("magnet",))
        self.assertEqual(first.items[0].detail_url, "https://mikanani.me/Home/Episode/abc")
        self.assertEqual(second.items, [])
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(http.calls[0]["params"], {"searchstr": "Frieren"})
        self.assertEqual(http.calls[0]["headers"]["Referer"], "https://mikanani.me/")

    async def test_mikan_fixture_extracts_torrent_size_and_published_time(self):
        fixture = (_INDEXER_FIXTURES / "mikan-search.html").read_bytes()
        adapter = MikanAdapter(http=FakeHttpClient(fixture))

        page = await adapter.search(IndexerSearchRequest.create("凡人修仙传"))

        self.assertEqual(len(page.items), 2)
        first = page.items[0]
        self.assertEqual(first.download_kinds, ("magnet", "torrent"))
        self.assertEqual(
            first.torrent_url,
            "https://mikanani.me/Download/20260822/"
            "a9091799ffb7c2cb0d3bfdc66697f1e308a9689e.torrent",
        )
        self.assertEqual(first.size_text, "1.15 GB")
        self.assertEqual(first.size_bytes, 1_150_000_000)
        self.assertIsNotNone(first.published_at)
        self.assertEqual(page.items[1].size_bytes, 848_300_000)

    async def test_mikan_keeps_torrent_only_rows_downloadable(self):
        body = (
            b'<table><tr class="js-search-results-row"><td></td><td>'
            b'<a href="/Home/Episode/only">Torrent only</a></td><td>700 MiB</td>'
            b'<td><a href="/Download/only.torrent">DL</a></td></tr></table>'
        )
        adapter = MikanAdapter(http=FakeHttpClient(body))

        page = await adapter.search(IndexerSearchRequest.create("Torrent only"))

        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0].download_kinds, ("torrent",))
        resolved = await adapter.resolve(page.items[0])
        self.assertEqual(resolved.kind, "torrent")
        self.assertEqual(resolved.value, "https://mikanani.me/Download/only.torrent")

    async def test_mikan_falls_back_to_mirror_and_preserves_mirror_download_host(self):
        mirror_html = (
            b'<table><tr class="js-search-results-row"><td></td><td>'
            b'<a href="/Home/Episode/mirror">Mirror result</a></td><td>700 MiB</td>'
            b'<td><a href="/Download/mirror.torrent">DL</a></td></tr></table>'
        )
        http = FakeHttpClient(b"<html>domain parking</html>")
        http.responses = [b"<html>domain parking</html>", mirror_html]
        adapter = MikanAdapter(http=http)

        page = await adapter.search(IndexerSearchRequest.create("Mirror result"))

        self.assertEqual(len(http.calls), 2)
        self.assertEqual(http.calls[0]["url"], "https://mikanani.me/Home/Search")
        self.assertEqual(http.calls[1]["url"], "https://mikanime.tv/Home/Search")
        self.assertEqual(http.calls[1]["headers"]["Referer"], "https://mikanime.tv/")
        item = page.items[0]
        self.assertEqual(item.detail_url, "https://mikanime.tv/Home/Episode/mirror")
        self.assertEqual(item.torrent_url, "https://mikanime.tv/Download/mirror.torrent")
        resolved = await adapter.resolve(item)
        self.assertEqual(resolved.value, "https://mikanime.tv/Download/mirror.torrent")

    async def test_mikan_resolve_rejects_unregistered_mirror_host(self):
        stored = IndexerItem(
            site_id="mikan",
            site_name="Mikan",
            title="Unsafe",
            torrent_url="https://evil.example/file.torrent",
            download_state="ready",
            download_kinds=("torrent",),
        )

        with self.assertRaises(IndexerSecurityError):
            await MikanAdapter(http=FakeHttpClient(MIKAN_HTML)).resolve(stored)

    async def test_btbtla_parses_redesigned_search_and_detail_layout(self):
        """改版布局：标题锚点换成 a.module-item-title，资源行只剩 module-row-info。"""
        search_html = """
        <div class="module-item">
          <a href="/detail/9001.html" class="module-item-title" title="Frieren Redesigned">Frieren Redesigned</a>
          <div class="module-item-caption"><span>2026</span><span>动漫</span><span>日本</span></div>
        </div>
        """.encode()
        detail_html = """
        <div class="module-list">
          <div class="module-row-info">
            <a class="module-row-text copy" href="/tdown/900101.html" title="Frieren.S02E01.torrent">
              <h4>Frieren S02E01 1080p [900MiB]</h4>
            </a>
          </div>
        </div>
        """.encode()
        http = FakeHttpClient(search_html)
        http.responses = [search_html, detail_html]
        adapter = BTBtlaAdapter(http=http)

        page = await adapter.search(IndexerSearchRequest.create("Frieren"))

        self.assertEqual(len(page.items), 1)
        item = page.items[0]
        self.assertIn("Frieren S02E01", item.title)
        self.assertEqual(item.category, "动漫")
        self.assertTrue(item.detail_url.endswith("/tdown/900101.html"))
        self.assertEqual(item.download_kinds, ("magnet", "torrent"))

    async def test_btbtla_search_expands_detail_into_concrete_torrent_resources(self):
        http = FakeHttpClient(BTBTLA_SEARCH_HTML)
        http.responses = [BTBTLA_SEARCH_HTML, BTBTLA_DETAIL_HTML]
        adapter = BTBtlaAdapter(http=http)

        page = await adapter.search(IndexerSearchRequest.create("Frieren", page=1))

        self.assertTrue(page.pagination_supported)
        self.assertFalse(page.has_more)
        self.assertEqual(len(http.calls), 2, "search should load only the best matching show detail")
        self.assertEqual(http.calls[1]["url"], "https://www.btbtlb.com/detail/frieren")
        self.assertEqual(len(page.items), 2, "cloud-drive /pdown entries must stay excluded")
        item = page.items[0]
        self.assertEqual(item.title, "Frieren S01E28 2160p")
        self.assertEqual(item.category, "Animation")
        self.assertEqual(item.size_text, "1.68GiB")
        self.assertEqual(item.size_bytes, int(1.68 * 1024**3))
        self.assertEqual(item.downloads, 5)
        self.assertEqual(item.download_state, "resolvable")
        self.assertEqual(item.download_kinds, ("magnet", "torrent"))
        self.assertEqual(item.detail_url, "https://www.btbtlb.com/tdown/848617892.html")

        http.responses = [BTBTLA_DOWNLOAD_HTML]
        resolved = await adapter.resolve(item)
        self.assertEqual(len(http.calls), 3)
        self.assertEqual(http.calls[2]["url"], "https://www.btbtlb.com/tdown/848617892.html")
        self.assertEqual(resolved.kind, "magnet")
        self.assertIn("urn:btih:0123456789abcdef0123456789abcdef01234567", resolved.value)

    async def test_btbtla_prefers_complete_modern_resource_rows_when_layouts_coexist(self):
        search_html = b"""
        <div class="module-item">
          <a class="module-item-title" href="/detail/demo.html">Demo</a>
        </div>
        """
        detail_html = b"""
        <div id="download-list">
          <div class="module-row-one active">
            <div class="module-row-info">
              <a class="module-row-text" href="/tdown/1.html">Demo One [1GiB]</a>
            </div>
            <a class="btn-down" href="/tdown/1.html">11</a>
          </div>
          <div class="module-row-info">
            <a class="module-row-text" href="/tdown/2.html">Demo Two [2GiB]</a>
            <a class="btn-down" href="/tdown/2.html">22</a>
          </div>
          <div class="module-row-info">
            <a class="module-row-text" href="/tdown/3.html">Demo Three [3GiB]</a>
            <a class="btn-down" href="/tdown/3.html">33</a>
          </div>
          <div class="module-row-info">
            <a class="module-row-text" href="/tdown/2.html">Duplicate Two [2GiB]</a>
          </div>
          <div class="module-row-info">
            <a class="module-row-text" href="/pdown/cloud.html">Cloud Only [9GiB]</a>
          </div>
        </div>
        """
        http = FakeHttpClient(search_html)
        http.responses = [search_html, detail_html]

        page = await BTBtlaAdapter(http=http).search(IndexerSearchRequest.create("Demo"))

        self.assertEqual(
            [item.detail_url for item in page.items],
            [
                "https://www.btbtlb.com/tdown/1.html",
                "https://www.btbtlb.com/tdown/2.html",
                "https://www.btbtlb.com/tdown/3.html",
            ],
        )
        self.assertEqual([item.downloads for item in page.items], [11, 22, 33])

    async def test_btbtla_keeps_legacy_row_with_metadata_only_info_block(self):
        search_html = b"""
        <div class="module-item">
          <a class="module-item-title" href="/detail/legacy.html">Legacy</a>
        </div>
        """
        detail_html = b"""
        <div id="download-list">
          <div class="module-row-one active">
            <div class="module-row-info"><span>metadata only</span></div>
            <a class="module-row-text" href="/tdown/legacy.html">Legacy [1GiB]</a>
            <a class="btn-down" href="/tdown/legacy.html">8</a>
          </div>
        </div>
        """
        http = FakeHttpClient(search_html)
        http.responses = [search_html, detail_html]

        page = await BTBtlaAdapter(http=http).search(IndexerSearchRequest.create("Legacy"))

        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0].detail_url, "https://www.btbtlb.com/tdown/legacy.html")
        self.assertEqual(page.items[0].downloads, 8)

    async def test_btbtla_resolve_falls_back_to_trusted_torrent_url(self):
        download_html = b'<a href="/dlt/token-123">Torrent file</a>'
        adapter = BTBtlaAdapter(http=FakeHttpClient(download_html))
        stored = IndexerItem(
            site_id="btbtla",
            site_name="BTBtla",
            title="Frieren",
            detail_url="https://www.btbtlb.com/tdown/123.html",
            download_state="resolvable",
            download_kinds=("magnet", "torrent"),
        )

        resolved = await adapter.resolve(stored)

        self.assertEqual(resolved.kind, "torrent")
        self.assertEqual(resolved.value, "https://www.btbtlb.com/dlt/token-123")

    async def test_btbtla_uses_second_ranked_detail_when_first_has_no_resources(self):
        search_html = b"""
        <div class="module-item"><a class="module-item-title" href="/detail/first.html">Frieren</a></div>
        <div class="module-item"><a class="module-item-title" href="/detail/second.html">Frieren Season 2</a></div>
        """
        empty_detail = b'<div id="download-list"></div>'
        second_detail = b"""
        <div class="module-row-info">
          <a class="module-row-text" href="/tdown/second.html"><h4>Frieren S02E01 [900MiB]</h4></a>
        </div>
        """
        http = FakeHttpClient(search_html)
        http.responses = [search_html, empty_detail, second_detail]
        adapter = BTBtlaAdapter(http=http, max_detail_candidates=2)

        page = await adapter.search(IndexerSearchRequest.create("Frieren"))

        self.assertEqual(len(http.calls), 3)
        self.assertEqual([item.title for item in page.items], ["Frieren S02E01"])

    async def test_btbtla_falls_back_from_www_to_apex_and_preserves_source_host(self):
        http = FakeHttpClient(b"<html>domain parking</html>")
        http.responses = [b"<html>domain parking</html>", BTBTLA_SEARCH_HTML, BTBTLA_DETAIL_HTML]
        adapter = BTBtlaAdapter(http=http)

        page = await adapter.search(IndexerSearchRequest.create("Frieren"))

        self.assertEqual(len(http.calls), 3)
        self.assertTrue(http.calls[0]["url"].startswith("https://www.btbtlb.com/search/"))
        self.assertTrue(http.calls[1]["url"].startswith("https://btbtlb.com/search/"))
        self.assertEqual(http.calls[2]["url"], "https://btbtlb.com/detail/frieren")
        self.assertEqual(page.items[0].detail_url, "https://btbtlb.com/tdown/848617892.html")

    async def test_btbtla_resolve_preserves_apex_torrent_host(self):
        stored = IndexerItem(
            site_id="btbtla",
            site_name="BTBtla",
            title="Demo",
            detail_url="https://btbtlb.com/tdown/123.html",
            download_state="resolvable",
            download_kinds=("magnet", "torrent"),
        )
        adapter = BTBtlaAdapter(
            http=FakeHttpClient(b'<a href="/dlt/token-123">seed</a>')
        )

        resolved = await adapter.resolve(stored)

        self.assertEqual(resolved.kind, "torrent")
        self.assertEqual(resolved.value, "https://btbtlb.com/dlt/token-123")

    async def test_btbtla_rejects_non_download_candidate_and_classifies_404(self):
        stored = IndexerItem(
            site_id="btbtla", site_name="BTBtla", title="Demo",
            detail_url="https://www.btbtlb.com/tdown/demo",
            download_state="resolvable", download_kinds=("magnet",),
        )
        http = FakeHttpClient('<a href="https://example.com/file">普通链接</a>'.encode())
        with self.assertRaises(IndexerInvalidResponse):
            await BTBtlaAdapter(http=http).resolve(stored)

        with self.assertRaises(IndexerUnavailable):
            await BTBtlaAdapter(http=FakeHttpClient(b"not found", status_code=404)).search(
                IndexerSearchRequest.create("Demo")
            )
        expired_http = FakeHttpClient(b"not found", status_code=404)
        with self.assertRaises(IndexerResultExpired):
            await BTBtlaAdapter(http=expired_http).resolve(stored)

    async def test_btbtla_enforces_configured_minimum_interval(self):
        current = [100.0]
        sleeps = []

        async def sleeper(seconds):
            sleeps.append(seconds)
            current[0] += seconds

        adapter = BTBtlaAdapter(
            http=FakeHttpClient("<p>暂无</p>".encode()), min_interval_seconds=5,
            monotonic=lambda: current[0], sleeper=sleeper,
        )
        self.assertEqual(adapter.search_timeout_overhead_seconds(), 20.0)
        await adapter.search(IndexerSearchRequest.create("one"))
        await adapter.search(IndexerSearchRequest.create("two"))
        self.assertEqual(sleeps, [5.0])

    async def test_btbtla_detects_and_requests_native_search_pages(self):
        first_search = b"""
        <div class="module-item">
          <a class="module-item-title" href="/detail/first.html">Star Wars</a>
        </div>
        <a class="page-next" href="/search/Star%20Wars/2">Next</a>
        """
        second_search = b"""
        <div class="module-item">
          <a class="module-item-title" href="/detail/second.html">Star Wars</a>
        </div>
        <a class="page-number" href="/search/Star%20Wars/1">1</a>
        """
        first_detail = b"""
        <div id="download-list"><div class="module-row-info">
          <a class="module-row-text" href="/tdown/first.html">Star Wars First [1GiB]</a>
        </div></div>
        """
        second_detail = b"""
        <div id="download-list"><div class="module-row-info">
          <a class="module-row-text" href="/tdown/second.html">Star Wars Second [2GiB]</a>
        </div></div>
        """
        http = FakeHttpClient(first_search)
        http.responses = [first_search, first_detail, second_search, second_detail]
        adapter = BTBtlaAdapter(http=http)

        first = await adapter.search(IndexerSearchRequest.create("Star Wars", page=1))
        second = await adapter.search(IndexerSearchRequest.create("Star Wars", page=2))

        self.assertTrue(first.pagination_supported)
        self.assertTrue(first.has_more)
        self.assertEqual(first.page, 1)
        self.assertEqual([item.title for item in first.items], ["Star Wars First"])
        self.assertTrue(second.pagination_supported)
        self.assertFalse(second.has_more)
        self.assertEqual(second.page, 2)
        self.assertEqual([item.title for item in second.items], ["Star Wars Second"])
        self.assertEqual(
            [call["url"] for call in http.calls],
            [
                "https://www.btbtlb.com/search/Star%20Wars",
                "https://www.btbtlb.com/detail/first.html",
                "https://www.btbtlb.com/search/Star%20Wars/2",
                "https://www.btbtlb.com/detail/second.html",
            ],
        )

    async def test_btbtla_ignores_off_host_and_wrong_query_page_links(self):
        search_html = b"""
        <div class="module-item">
          <a class="module-item-title" href="/detail/demo.html">Demo</a>
        </div>
        <a class="page-next" href="https://evil.example/search/Demo/2">Unsafe</a>
        <a class="page-number" href="/search/Other/2">Other query</a>
        """
        detail_html = b"""
        <div id="download-list"><div class="module-row-info">
          <a class="module-row-text" href="/tdown/demo.html">Demo [1GiB]</a>
        </div></div>
        """
        http = FakeHttpClient(search_html)
        http.responses = [search_html, detail_html]

        page = await BTBtlaAdapter(http=http).search(IndexerSearchRequest.create("Demo"))

        self.assertFalse(page.has_more)

    async def test_btbtla_resolve_rejects_off_host_redirect_with_fake_transport(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(302, headers={"Location": "https://evil.example/download"})

        client = FixedHostHttpClient(
            allowed_hosts={"www.btbtlb.com"},
            transport=httpx.MockTransport(handler),
            resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
        )
        self.addAsyncCleanup(client.aclose)
        adapter = BTBtlaAdapter(http=client)
        stored = IndexerItem(
            site_id="btbtla",
            site_name="BTBtla",
            title="Frieren",
            detail_url="https://www.btbtlb.com/detail/frieren",
            download_state="resolvable",
            download_kinds=("magnet",),
        )

        with self.assertRaises(IndexerSecurityError):
            await adapter.resolve(stored)
        self.assertEqual(seen, ["https://www.btbtlb.com/detail/frieren"])

    async def test_onelou_search_is_list_only_and_resolves_torrent_attachment_lazily(self):
        http = FakeHttpClient(ONELOU_API_JSON, content_type="application/json")
        adapter = OneLouAdapter(http=http)

        page = await adapter.search(IndexerSearchRequest.create("Frieren", page=1))

        self.assertFalse(page.pagination_supported)
        self.assertFalse(page.has_more)
        self.assertEqual(len(http.calls), 1, "search must not visit thread pages")
        self.assertIn("/search/api/search.php", http.calls[0]["url"])
        self.assertEqual(http.calls[0]["params"]["fid"], "0")
        self.assertEqual(http.calls[0]["params"]["sort"], "newest")
        self.assertEqual(http.calls[0]["params"]["track"], "0")
        self.assertEqual(len(page.items), 1, "cloud-drive-only results stay filtered")
        item = page.items[0]
        self.assertEqual(item.title, "Frieren Complete 1080p")
        self.assertIsNotNone(item.published_at)
        self.assertEqual(item.download_state, "resolvable")
        self.assertEqual(item.download_kinds, ("torrent",))
        self.assertEqual(item.detail_url, "https://www.1lou.me/thread-101.htm")

        http.responses = [ONELOU_DETAIL_HTML]
        http.content_type = "text/html; charset=utf-8"
        resolved = await adapter.resolve(item)
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(resolved.kind, "torrent")
        self.assertEqual(resolved.value, "https://www.1lou.me/attach-download-frieren.torrent")
        self.assertEqual(resolved.filename, "Frieren.torrent")

    async def test_onelou_enforces_configured_minimum_search_interval(self):
        current = [100.0]
        sleeps = []

        async def sleeper(seconds):
            sleeps.append(seconds)
            current[0] += seconds

        adapter = OneLouAdapter(
            http=FakeHttpClient(ONELOU_API_JSON, content_type="application/json"),
            min_interval_seconds=5,
            monotonic=lambda: current[0],
            sleeper=sleeper,
        )
        request = IndexerSearchRequest.create("Frieren")

        await adapter.wait_for_search_slot(request)
        await adapter.wait_for_search_slot(request)

        self.assertEqual(sleeps, [5.0])

    async def test_onelou_prefers_native_newest_results_without_touching_google(self):
        native_http = FakeHttpClient(ONELOU_API_JSON, content_type="application/json")
        google_http = FakeHttpClient(ONELOU_GOOGLE_HTML)
        adapter = OneLouAdapter(
            http=native_http,
            google_search=GoogleSiteSearch(http=google_http),
        )

        page = await adapter.search(IndexerSearchRequest.create("Frieren"))

        self.assertEqual(len(native_http.calls), 1)
        self.assertEqual(native_http.calls[0]["params"]["sort"], "newest")
        self.assertEqual(google_http.calls, [])
        self.assertEqual([item.title for item in page.items], ["Frieren Complete 1080p"])
        self.assertEqual(page.items[0].detail_url, "https://www.1lou.me/thread-101.htm")

    async def test_onelou_google_respects_http_charset_over_conflicting_meta(self):
        native_http = FakeHttpClient(ONELOU_API_EMPTY, content_type="application/json")
        google_http = FakeHttpClient(
            ONELOU_GOOGLE_CONFLICTING_META_HTML,
            content_type="text/html; charset=utf-8",
        )
        adapter = OneLouAdapter(
            http=native_http,
            google_search=GoogleSiteSearch(http=google_http),
        )

        page = await adapter.search(IndexerSearchRequest.create("Frieren"))

        self.assertEqual(len(native_http.calls), 1)
        self.assertEqual([item.title for item in page.items], ["Frieren Complete 1080p"])

    async def test_onelou_native_rate_limit_falls_back_to_google(self):
        native_http = FakeHttpClient(b"slow down", status_code=429)
        google_http = FakeHttpClient(ONELOU_GOOGLE_HTML)
        adapter = OneLouAdapter(
            http=native_http,
            google_search=GoogleSiteSearch(http=google_http),
        )

        page = await adapter.search(IndexerSearchRequest.create("Frieren"))

        self.assertEqual(len(page.items), 1)
        self.assertEqual(len(google_http.calls), 1)
        self.assertEqual(len(native_http.calls), 2)

    async def test_onelou_endpoint_timeouts_still_reach_google_fallback(self):
        class SlowNativeHttp(FakeHttpClient):
            async def get(self, url: str, *, params=None, headers=None, max_redirects=3):
                self.calls.append({
                    "url": url,
                    "params": dict(params or {}),
                    "headers": dict(headers or {}),
                })
                await asyncio.sleep(0.2)
                raise AssertionError("timed-out native request must be cancelled")

        native_http = SlowNativeHttp(ONELOU_API_EMPTY, content_type="application/json")
        google_http = FakeHttpClient(ONELOU_GOOGLE_HTML)
        adapter = OneLouAdapter(
            http=native_http,
            google_search=GoogleSiteSearch(http=google_http),
            endpoint_timeout_seconds=0.01,
        )

        page = await adapter.search(IndexerSearchRequest.create("Frieren"))

        self.assertEqual([item.title for item in page.items], ["Frieren Complete 1080p"])
        self.assertEqual(len(native_http.calls), 2)
        self.assertEqual(len(google_http.calls), 1)

    async def test_onelou_google_interstitial_cools_down_after_native_empty(self):
        native_http = FakeHttpClient(ONELOU_API_EMPTY, content_type="application/json")
        google_http = FakeHttpClient(ONELOU_GOOGLE_INTERSTITIAL)
        google = GoogleSiteSearch(http=google_http, cooldown_seconds=300)
        adapter = OneLouAdapter(http=native_http, google_search=google)

        first = await adapter.search(IndexerSearchRequest.create("Frieren"))
        second = await adapter.search(IndexerSearchRequest.create("Frieren 2"))

        self.assertEqual(first.items, [])
        self.assertEqual(second.items, [])
        self.assertEqual(len(google_http.calls), 1, "Google failure should open a local cooldown")
        self.assertEqual(len(native_http.calls), 2)

    async def test_onelou_google_timeout_returns_native_empty_with_independent_budget(self):
        class SlowGoogleHttp(FakeHttpClient):
            async def get(self, *args, **kwargs):
                await asyncio.sleep(0.2)
                return await super().get(*args, **kwargs)

        native_http = FakeHttpClient(ONELOU_API_EMPTY, content_type="application/json")
        google_http = SlowGoogleHttp(ONELOU_GOOGLE_HTML)
        adapter = OneLouAdapter(
            http=native_http,
            google_search=GoogleSiteSearch(http=google_http, timeout_seconds=0.01),
        )

        page = await adapter.search(IndexerSearchRequest.create("Frieren"))

        self.assertEqual(page.items, [])
        self.assertEqual(len(native_http.calls), 1)

    async def test_onelou_google_explicit_empty_result_does_not_open_cooldown(self):
        native_http = FakeHttpClient(ONELOU_API_EMPTY, content_type="application/json")
        google_http = FakeHttpClient(ONELOU_GOOGLE_EMPTY_HTML)
        adapter = OneLouAdapter(
            http=native_http,
            google_search=GoogleSiteSearch(http=google_http, cooldown_seconds=300),
        )

        first = await adapter.search(IndexerSearchRequest.create("Frieren"))
        second = await adapter.search(IndexerSearchRequest.create("Frieren 2"))

        self.assertEqual(first.items, [])
        self.assertEqual(second.items, [])
        self.assertEqual(len(google_http.calls), 2, "valid empty pages must not disable Google")
        self.assertEqual(len(native_http.calls), 2)

    async def test_onelou_native_api_path_accepts_legacy_html_without_extra_request(self):
        http = FakeHttpClient(ONELOU_HTML)
        adapter = OneLouAdapter(http=http)

        page = await adapter.search(IndexerSearchRequest.create("Frieren"))

        self.assertEqual(len(http.calls), 1)
        self.assertEqual([item.title for item in page.items], ["Frieren Legacy 1080p"])
        self.assertEqual(page.items[0].detail_url, "https://www.1lou.me/thread-201.htm")

    async def test_onelou_invalid_apis_fall_back_to_legacy_html_on_https_mirror(self):
        http = FakeHttpClient(b"invalid", content_type="application/json")
        http.responses = [b"invalid", b"invalid", ONELOU_HTML]
        http.content_types = ["application/json", "application/json", "text/html"]
        adapter = OneLouAdapter(http=http)

        page = await adapter.search(IndexerSearchRequest.create("Frieren"))

        self.assertEqual(len(http.calls), 3)
        self.assertIn("/search-Frieren.htm", http.calls[2]["url"])
        self.assertEqual(page.items[0].detail_url, "https://www.1lou.me/thread-201.htm")

    async def test_onelou_native_search_falls_back_to_pro_mirror(self):
        http = FakeHttpClient(ONELOU_API_JSON, content_type="application/json")
        http.responses = [b"slow down", ONELOU_API_JSON]
        http.status_codes = [429, 200]
        adapter = OneLouAdapter(http=http)

        page = await adapter.search(IndexerSearchRequest.create("Frieren"))

        self.assertEqual(len(http.calls), 2)
        self.assertTrue(http.calls[0]["url"].startswith("https://www.1lou.me/"))
        self.assertTrue(http.calls[1]["url"].startswith("https://www.1lou.pro/"))
        self.assertEqual(page.items[0].detail_url, "https://www.1lou.pro/thread-101.htm")

    async def test_onelou_classifies_http_200_verification_page_as_unavailable(self):
        challenge = (
            b"<html><title>Just a moment...</title>"
            b"<div id='challenge-platform'>Verify you are human</div></html>"
        )
        http = FakeHttpClient(challenge)
        adapter = OneLouAdapter(http=http)

        with self.assertRaises(IndexerUnavailable):
            await adapter.search(IndexerSearchRequest.create("Frieren"))

        self.assertEqual(len(http.calls), 2, "verification pages should try the trusted mirror only")

    async def test_onelou_resolve_rejects_http_200_verification_page(self):
        challenge = b"<html><title>Just a moment...</title><div class='turnstile'>Verify you are human</div></html>"
        adapter = OneLouAdapter(http=FakeHttpClient(challenge))
        stored = IndexerItem(
            site_id="1lou",
            site_name="1lou",
            title="Frieren",
            detail_url="https://www.1lou.me/thread-101.htm",
            download_state="resolvable",
            download_kinds=("torrent", "magnet"),
        )

        with self.assertRaises(IndexerUnavailable):
            await adapter.resolve(stored)

    async def test_onelou_classifies_http_429_as_rate_limited_after_mirrors_fail(self):
        http = FakeHttpClient(b"slow down", status_code=429)
        adapter = OneLouAdapter(http=http)

        with self.assertRaises(IndexerRateLimited):
            await adapter.search(IndexerSearchRequest.create("Frieren"))
        self.assertEqual(len(http.calls), 2)

    async def test_onelou_preserves_rate_limit_when_mirror_fallback_is_structurally_invalid(self):
        http = FakeHttpClient(b"slow down", status_code=429)
        http.responses = [b"slow down", b"invalid", b"slow down"]
        http.status_codes = [429, 200, 429]
        http.content_types = ["text/html", "application/json", "text/html"]
        adapter = OneLouAdapter(http=http)

        with self.assertRaises(IndexerRateLimited):
            await adapter.search(IndexerSearchRequest.create("Frieren"))
        self.assertEqual(len(http.calls), 3)
        self.assertTrue(http.calls[2]["url"].startswith("https://www.1lou.pro/"))

    async def test_onelou_resolve_falls_back_to_magnet_when_no_attachment(self):
        http = FakeHttpClient(ONELOU_DETAIL_MAGNET_HTML)
        adapter = OneLouAdapter(http=http)
        stored = IndexerItem(
            site_id="1lou",
            site_name="1lou",
            title="Frieren",
            detail_url="https://www.1lou.me/thread-101.htm",
            download_state="resolvable",
            download_kinds=("torrent",),
        )

        resolved = await adapter.resolve(stored)

        self.assertEqual(resolved.kind, "magnet")
        self.assertIn("btih:0123456789abcdef", resolved.value)

    async def test_onelou_resolves_legacy_apex_domain_results(self):
        http = FakeHttpClient(ONELOU_DETAIL_HTML)
        adapter = OneLouAdapter(http=http)
        stored = IndexerItem(
            site_id="1lou",
            site_name="1lou",
            title="Frieren",
            detail_url="https://1lou.me/thread-frieren.htm",
            download_state="resolvable",
            download_kinds=("torrent",),
        )

        resolved = await adapter.resolve(stored)

        self.assertEqual(resolved.kind, "torrent")

    async def test_onelou_pro_result_keeps_relative_attachment_on_pro_host(self):
        http = FakeHttpClient(ONELOU_DETAIL_HTML)
        adapter = OneLouAdapter(http=http)
        stored = IndexerItem(
            site_id="1lou",
            site_name="1lou",
            title="Frieren",
            detail_url="https://www.1lou.pro/thread-101.htm",
            download_state="resolvable",
            download_kinds=("torrent",),
        )

        resolved = await adapter.resolve(stored)

        self.assertEqual(resolved.value, "https://www.1lou.pro/attach-download-frieren.torrent")

    async def test_onelou_rejects_off_host_attachment_and_page_two_does_not_search(self):
        http = FakeHttpClient(b'<a href="https://evil.example/attach-download.torrent">evil</a>')
        adapter = OneLouAdapter(http=http)
        stored = IndexerItem(
            site_id="1lou",
            site_name="1lou",
            title="Frieren",
            detail_url="https://www.1lou.me/thread-frieren.htm",
            download_state="resolvable",
            download_kinds=("torrent",),
        )

        with self.assertRaises(IndexerSecurityError):
            await adapter.resolve(stored)
        second = await adapter.search(IndexerSearchRequest.create("Frieren", page=2))
        self.assertEqual(second.items, [])
        self.assertFalse(second.has_more)
        self.assertFalse(second.pagination_supported)
        self.assertEqual(len(http.calls), 1)

    async def test_registry_closes_onelou_native_and_google_clients(self):
        native_http = ClosableHttpClient(ONELOU_API_JSON, content_type="application/json")
        google_http = ClosableHttpClient(ONELOU_GOOGLE_HTML)
        registry = IndexerRegistry({
            "1lou": OneLouAdapter(
                http=native_http,
                google_search=GoogleSiteSearch(http=google_http),
            )
        })

        await registry.aclose()

        self.assertEqual(native_http.close_calls, 1)
        self.assertEqual(google_http.close_calls, 1)

    def test_default_registry_uses_browser_transport_for_challenged_sites(self):
        registry = build_default_registry()

        self.assertIsInstance(registry.get("btbtla").http, BrowserImpersonatingHttpClient)
        self.assertEqual(registry.get("btbtla").base_url, "https://www.btbtlb.com/")
        self.assertEqual(registry.get("btbtla").mirror_base_urls, ("https://btbtlb.com/",))
        self.assertEqual(registry.get("btbtla").http.sni_host, "btbtlb.com")
        self.assertIn("www.btbtlb.com", registry.get("btbtla").http.allowed_hosts)
        self.assertIn("btbtlb.com", registry.get("btbtla").http.allowed_hosts)
        self.assertEqual(registry.get("mikan").mirror_base_urls, ("https://mikanime.tv/",))
        self.assertIn("mikanime.tv", registry.get("mikan").http.allowed_hosts)
        self.assertTrue(registry.get("nyaa").http.pin_resolved_address)
        self.assertTrue(registry.get("mikan").http.pin_resolved_address)
        self.assertTrue(registry.get("1lou").http.pin_resolved_address)
        self.assertTrue(registry.get("tpb").http.pin_resolved_address)
        self.assertEqual(registry.get("1lou").min_interval_seconds, 5)
        self.assertIsNotNone(registry.get("1lou").google_search)
        self.assertIn("www.1lou.pro", registry.get("1lou").http.allowed_hosts)

    def test_default_registry_contains_supported_sites_and_disabled_sukebei(self):
        clients = {
            "nyaa": FakeHttpClient(NYAA_HTML),
            "sukebei": FakeHttpClient(NYAA_HTML),
            "mikan": FakeHttpClient(MIKAN_HTML),
            "btbtla": FakeHttpClient(BTBTLA_SEARCH_HTML),
            "1lou": FakeHttpClient(ONELOU_API_JSON, content_type="application/json"),
            "google": FakeHttpClient(ONELOU_GOOGLE_HTML),
            "tpb": FakeHttpClient(TPB_JSON, content_type="application/json"),
        }
        registry = build_default_registry(http_clients=clients)

        self.assertEqual(registry.ids(), ("nyaa", "sukebei", "mikan", "btbtla", "1lou", "tpb"))
        self.assertTrue(registry.get("nyaa").default_enabled)
        self.assertFalse(registry.get("sukebei").default_enabled)
        self.assertTrue(registry.get("sukebei").capabilities.pagination_supported)
        self.assertEqual(registry.get("sukebei").base_url, "https://sukebei.nyaa.si/")
        self.assertTrue(registry.get("mikan").default_enabled)
        self.assertTrue(registry.get("btbtla").default_enabled)
        self.assertTrue(registry.get("btbtla").capabilities.pagination_supported)
        self.assertTrue(registry.get("1lou").default_enabled)
        self.assertEqual(registry.get("1lou").capabilities.download_kinds, ("torrent", "magnet"))
        self.assertTrue(registry.get("tpb").default_enabled)
        self.assertEqual(registry.get("tpb").capabilities.download_kinds, ("magnet",))


if __name__ == "__main__":
    unittest.main()
