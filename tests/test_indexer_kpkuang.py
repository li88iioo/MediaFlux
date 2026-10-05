from __future__ import annotations

import asyncio
import base64
import json
import unittest
from urllib.parse import urljoin

from app.indexers.errors import (
    IndexerChallengeRequired,
    IndexerInvalidResponse,
    IndexerRateLimited,
    IndexerResponseTooLarge,
    IndexerSecurityError,
    IndexerTimeout,
)
from app.indexers.http import IndexerHttpResponse
from app.indexers.models import IndexerMediaSearchRequest, IndexerSearchRequest
from app.indexers.providers.base import magnet_infohash, page_observer
from app.indexers.providers.kpkuang import KPKUANG_HOST_CONFIG, KPkuangAdapter, MAX_RESPONSE_BYTES
from app.indexers.ranking import rank_item

API = str(KPKUANG_HOST_CONFIG["api_search_url"])
BASE = str(KPKUANG_HOST_CONFIG["base_url"])
DETAIL = str(KPKUANG_HOST_CONFIG["detail_path_prefix"])
VODDOWN = str(KPKUANG_HOST_CONFIG["voddown_path_prefix"])


def response(url, body, status=200, content_type="text/html"):
    if isinstance(body, str):
        body = body.encode()
    return IndexerHttpResponse(url, status, {"content-type": content_type}, body)


def candidate(id, title, year, score=10):
    return {"id": id, "score": score, "data": {"vod_name": title, "vod_year": str(year or "")}}


def api(values):
    encoded = base64.b64encode(json.dumps(values, ensure_ascii=False).encode()).decode()
    return f'cb({json.dumps({"code": 1, "js": encoded})})'


def detail_url(id):
    return urljoin(BASE, f"{DETAIL}{id}/")


def down_url(id):
    return urljoin(BASE, f"{VODDOWN}{id}-1-1.html")


def magnet(hash, name="release"):
    return f"magnet:?xt=urn:btih:{hash}&dn={name}"


def encoded(value):
    return base64.b64encode(value.encode()).decode()


def detail_html(id, links="", *, add_default=True):
    default = f'<a href="{VODDOWN}{id}-1-1.html">资源</a>' if add_default else ""
    return f'''<div class="fed-main-info"><div class="fed-part-case">
      <div class="fed-tabs-info fed-play-data">{default}{links}</div>
      <div class="fed-part-layout"><a href="{VODDOWN}999999-1-1.html">侧栏链接</a></div>
    </div></div>'''


def download_html(rows, sidebar=()):
    main = "".join(
        f'<li>{title}<button data-clipboard-text="{encoded(value)}">复制</button></li>'
        for title, value in rows
    )
    side = "".join(
        f'<li>{title}<button data-clipboard-text="{encoded(value)}">复制</button></li>'
        for title, value in sidebar
    )
    return f'''<div class="fed-main-info"><div class="fed-part-case">
      <div class="fed-tabs-info fed-play-data"><ul>{main}</ul></div>
      <div class="fed-part-layout recommendations"><ul>{side}</ul></div>
    </div></div>'''


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        result = self.routes[url]
        if callable(result):
            result = result(url, kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, BaseException):
            raise result
        return result


class KPkuangTests(unittest.IsolatedAsyncioTestCase):
    def test_clipboard_non_magnets_do_not_abort_a_valid_resource_page(self):
        from app.indexers.providers.kpkuang import _decode_magnet
        for value in ("https://[invalid", "magnet://[invalid", "ed2k://file/[broken", "not a link"):
            self.assertIsNone(_decode_magnet(encoded(value)))
        self.assertEqual(_decode_magnet(encoded(magnet("a" * 40))), magnet("a" * 40))

    async def test_transient_empty_envelope_is_rechecked_but_never_called_no_results(self):
        from unittest.mock import AsyncMock, patch
        from app.indexers.errors import IndexerUnavailable
        for recover in (True, False):
            calls = []
            def respond(_url, kwargs):
                calls.append(kwargs)
                return response(API, api([]) if recover and len(calls) == 2 else 'cb({"code":0,"js":""})')
            http = FakeHttp({API: respond})
            with patch("app.indexers.providers.kpkuang.asyncio.sleep", new_callable=AsyncMock) as sleep:
                if recover:
                    page = await KPkuangAdapter(http=http).search(IndexerSearchRequest.create("未知电影"))
                    self.assertEqual(page.items, [])
                else:
                    with self.assertRaises(IndexerUnavailable):
                        await KPkuangAdapter(http=http).search(IndexerSearchRequest.create("未知电影"))
                self.assertEqual(sleep.await_count, 1)
                self.assertGreater(sleep.await_args.args[0], 5)
            self.assertEqual(len(calls), 2)
            self.assertGreater(calls[0]["params"]["ts"], 10**12)

    async def test_nested_movie_table_uses_nearest_row_title_not_entire_tab(self):
        raw = magnet("b" * 40)
        body = download_html([]).replace('<ul></ul>', '<li>外层影视合集<table><tr><td><a title="测试电影.2023.2160p[3GB]">下载</a><button data-clipboard-text="' + encoded(raw) + '">复制</button></td></tr></table></li>')
        routes = {API: response(API, api([candidate(9, "测试电影", 2023)])), detail_url(9): response(detail_url(9), detail_html(9)), down_url(9): response(down_url(9), body)}
        page = await KPkuangAdapter(http=FakeHttp(routes)).search(IndexerSearchRequest.create("测试电影", year=2023))
        self.assertEqual(page.items[0].title, "测试电影.2023.2160p[3GB]")
        self.assertEqual(page.items[0].size_bytes, 3 * 1000**3)

    async def test_exact_episode_found_does_not_fetch_more_catalogues(self):
        routes = {API: response(API, api([candidate(1, "仙逆", 2023), candidate(2, "仙逆完整版", 2023)])), detail_url(1): response(detail_url(1), detail_html(1)), down_url(1): response(down_url(1), download_html([("仙逆.S01E01.2160p", magnet("c" * 40))]))}
        http = FakeHttp(routes)
        page = await KPkuangAdapter(http=http).search(IndexerSearchRequest.create("仙逆", year=2023, season=1, episode=1))
        self.assertEqual(len(page.items), 1)
        self.assertEqual(http.calls, [API, detail_url(1), down_url(1)])

    async def test_numeric_title_does_not_override_catalogue_year(self):
        routes = {API: response(API, api([candidate(7, "1917", 2019)])), detail_url(7): response(detail_url(7), detail_html(7)), down_url(7): response(down_url(7), download_html([("1917.2019.1080p", magnet("d" * 40))]))}
        page = await KPkuangAdapter(http=FakeHttp(routes)).search(IndexerSearchRequest.create("1917", year=2019, media_type="movie"))
        self.assertEqual(len(page.items), 1)

    def test_encoded_api_matches_the_verified_source_configuration(self):
        import hashlib
        self.assertEqual(hashlib.sha256(API.encode()).hexdigest(), "5a5ae3c1824f2b9db2946af8b16cc0b9e1c59796b08a90258d2f5af3e3c2abdb")

    async def test_exports_decoded_fixed_host_config(self):
        self.assertEqual(len(KPKUANG_HOST_CONFIG["allowed_hosts"]), 2)
        self.assertTrue(BASE.startswith("https://"))
        self.assertTrue(API.startswith("https://"))

    async def test_year_candidate_and_main_container_only_with_valid_deduplicated_magnets(self):
        old, selected = 483192, 610999
        main_hash = "0123456789abcdef0123456789abcdef01234567"
        side_hash = "abcdef0123456789abcdef0123456789abcdef01"
        same_hash = magnet(main_hash, "another-name")
        rows = [
            ("三体 (2023) S01 第01-18集 [8.4GB]", magnet(main_hash)),
            ("重复条目", same_hash),
            ("坏哈希", "magnet:?xt=urn:btih:short"),
        ]
        routes = {
            API: response(API, api([candidate(old, "三体", 2021, 100), candidate(selected, "三体", 2023)]), content_type="application/javascript"),
            detail_url(selected): response(detail_url(selected), detail_html(selected, f'<a href="{VODDOWN}{old}-1-1.html">非目标</a>')),
            down_url(selected): response(down_url(selected), download_html(rows, [("侧栏推荐", magnet(side_hash))])),
        }
        http = FakeHttp(routes)
        page = await KPkuangAdapter(http=http).search(IndexerSearchRequest.create("三体", year=2023))

        self.assertEqual(len(page.items), 1)
        self.assertEqual(magnet_infohash(page.items[0].magnet), main_hash)
        self.assertEqual(page.items[0].detail_url, detail_url(selected))
        self.assertEqual(page.items[0].size_bytes, 8_400_000_000)
        self.assertEqual(http.calls, [API, detail_url(selected), down_url(selected)])
        self.assertTrue(page.complete)

    async def test_release_position_is_preserved_for_existing_shared_ranker(self):
        id = 77
        rows = [
            ("三体 (2023) S01 第06集 [4GB]", magnet("1111111111111111111111111111111111111111")),
            ("三体 (2023) S01 第07集 [4GB]", magnet("2222222222222222222222222222222222222222")),
        ]
        routes = {
            API: response(API, api([candidate(id, "三体", 2023)]), content_type="application/javascript"),
            detail_url(id): response(detail_url(id), detail_html(id, '<span>challenge-platform</span>')),
            down_url(id): response(down_url(id), download_html(rows)),
        }
        page = await KPkuangAdapter(http=FakeHttp(routes)).search(
            IndexerSearchRequest.create("三体", year=2023, season=1, episode=6)
        )
        media = IndexerMediaSearchRequest.create(title="三体", year=2023, season=1, episode=6)
        ranked = [rank_item(item, media=media, fallback_query="三体") for item in page.items]
        self.assertEqual(len(ranked), 2)
        self.assertIn("episode_exact", ranked[0].match_reasons)
        self.assertIn("episode_conflict", ranked[1].match_reasons)

    async def test_empty_results_are_success_and_malformed_jsonp_is_invalid(self):
        page = await KPkuangAdapter(http=FakeHttp({API: response(API, api([]), content_type="application/javascript")})).search(
            IndexerSearchRequest.create("nothing")
        )
        self.assertEqual(page.items, [])
        self.assertTrue(page.complete)
        with self.assertRaises(IndexerInvalidResponse):
            await KPkuangAdapter(http=FakeHttp({API: response(API, b"not jsonp")})).search(IndexerSearchRequest.create("三体"))

    async def test_partial_detail_timeout_keeps_collected_items(self):
        first, second = 91, 92
        routes = {
            API: response(API, api([candidate(first, "星际旅行", 2024), candidate(second, "星际旅行", 2023)]), content_type="application/javascript"),
            detail_url(first): response(detail_url(first), detail_html(first)),
            down_url(first): response(down_url(first), download_html([("星际旅行 (2024) [1GB]", magnet("7777777777777777777777777777777777777777"))])),
            detail_url(second): TimeoutError("test timeout"),
        }
        page = await KPkuangAdapter(http=FakeHttp(routes)).search(IndexerSearchRequest.create("星际旅行"))
        self.assertEqual(len(page.items), 1)
        self.assertFalse(page.complete)
        self.assertEqual(page.errors[0].code, "timeout")

    async def test_wrong_candidate_or_external_download_link_is_not_followed(self):
        id = 94
        body = detail_html(id, '<a href="/voddown/940-1-1.html">wrong id</a><a href="https://example.invalid/file">external</a>', add_default=False)
        routes = {
            API: response(API, api([candidate(id, "三体", 2023)]), content_type="application/javascript"),
            detail_url(id): response(detail_url(id), body),
        }
        http = FakeHttp(routes)
        page = await KPkuangAdapter(http=http).search(IndexerSearchRequest.create("三体", year=2023))
        self.assertEqual(page.items, [])
        self.assertEqual(http.calls, [API, detail_url(id)])

    async def test_body_limit_and_nonfirst_page_do_not_expand_requests(self):
        with self.assertRaises(IndexerResponseTooLarge):
            await KPkuangAdapter(http=FakeHttp({API: response(API, b"x" * (MAX_RESPONSE_BYTES + 1))})).search(
                IndexerSearchRequest.create("三体")
            )
        http = FakeHttp({})
        page = await KPkuangAdapter(http=http).search(IndexerSearchRequest.create("三体", page=2))
        self.assertEqual(page.items, [])
        self.assertEqual(http.calls, [])


    async def test_candidate_resolution_stops_after_rate_limit_challenge_or_security_error(self):
        first_hash = "1111111111111111111111111111111111111111"
        for stage in ("detail", "download"):
            for error_type in (IndexerRateLimited, IndexerChallengeRequired, IndexerSecurityError):
                with self.subTest(stage=stage, error=error_type.code):
                    values = [candidate(id, "边界测试剧集", 2024) for id in (801, 802, 803)]
                    routes = {
                        API: response(API, api(values), content_type="application/javascript"),
                        detail_url(801): response(detail_url(801), detail_html(801)),
                        down_url(801): response(down_url(801), download_html([("主资源", magnet(first_hash))])),
                        detail_url(802): response(detail_url(802), detail_html(802)),
                        down_url(802): error_type("upstream stop") if stage == "download" else response(
                            down_url(802), download_html([])
                        ),
                        detail_url(803): response(detail_url(803), detail_html(803)),
                        down_url(803): response(down_url(803), download_html([])),
                    }
                    if stage == "detail":
                        routes[detail_url(802)] = error_type("upstream stop")

                    http = FakeHttp(routes)
                    page = await KPkuangAdapter(http=http).search(
                        IndexerSearchRequest.create("边界测试剧集", year=2024)
                    )
                    self.assertEqual([magnet_infohash(item.magnet) for item in page.items], [first_hash])
                    self.assertIn(error_type.code, [error.code for error in page.errors])
                    self.assertNotIn(detail_url(803), http.calls)

    async def test_reported_partial_items_survive_cancellation_during_next_candidate(self):
        first_hash = "2222222222222222222222222222222222222222"
        second_detail_started = asyncio.Event()

        async def wait_on_second_detail(_url, _kwargs):
            second_detail_started.set()
            await asyncio.Event().wait()

        routes = {
            API: response(API, api([candidate(901, "部分结果测试", 2025), candidate(902, "部分结果测试", 2025)]), content_type="application/javascript"),
            detail_url(901): response(detail_url(901), detail_html(901)),
            down_url(901): response(down_url(901), download_html([("主资源", magnet(first_hash))])),
            detail_url(902): wait_on_second_detail,
        }
        reported = []
        token = page_observer.set(lambda site_id, page: reported.append((site_id, page)))
        task = asyncio.create_task(
            KPkuangAdapter(http=FakeHttp(routes)).search(
                IndexerSearchRequest.create("部分结果测试", year=2025)
            )
        )
        try:
            await asyncio.wait_for(second_detail_started.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            page_observer.reset(token)

        self.assertTrue(reported)
        site_id, partial = reported[-1]
        self.assertEqual(site_id, "kpkuang")
        self.assertFalse(partial.complete)
        self.assertEqual([magnet_infohash(item.magnet) for item in partial.items], [first_hash])

    async def test_all_detail_timeouts_raise_typed_timeout(self):
        id = 93
        routes = {
            API: response(API, api([candidate(id, "星际旅行", 2024)]), content_type="application/javascript"),
            detail_url(id): TimeoutError("test timeout"),
        }
        with self.assertRaises(IndexerTimeout):
            await KPkuangAdapter(http=FakeHttp(routes)).search(IndexerSearchRequest.create("星际旅行"))


if __name__ == "__main__":
    unittest.main()
