from __future__ import annotations

import asyncio
import unittest
from urllib.parse import unquote_to_bytes

from app.indexers.errors import IndexerInvalidResponse, IndexerUnavailable
from app.indexers.http import IndexerHttpResponse
from app.indexers.models import IndexerSearchRequest
from app.indexers.providers.empire import EmpireAdapter

_DYGANG_RESULT = '''
<html><body>
<a class="classlinkclass" href="/ys/2025/123.htm">测试电影 2025</a>
</body></html>
'''
_YS_RESULT = '''
<html><body><h3><a href="/movie/456.html">测试剧集</a></h3></body></html>
'''
_VALID_HASH = "0123456789abcdef0123456789abcdef01234567"


class _EmpireHttp:
    def __init__(self, search_response, detail_responses=None, *, block_details=False):
        self.search_response = search_response
        self.detail_responses = dict(detail_responses or {})
        self.form_calls = []
        self.get_calls = []
        self.active_details = 0
        self.max_active_details = 0
        self.block_details = block_details
        self.all_details_started = asyncio.Event()
        self.cancelled_details = 0

    async def post_form(self, url, *, content: bytes, headers=None):
        self.form_calls.append((url, content, dict(headers or {})))
        if isinstance(self.search_response, Exception):
            raise self.search_response
        return self.search_response

    async def get(self, url, *, headers=None):
        self.get_calls.append((url, dict(headers or {})))
        self.active_details += 1
        self.max_active_details = max(self.max_active_details, self.active_details)
        try:
            if self.block_details:
                if len(self.get_calls) == 3:
                    self.all_details_started.set()
                await asyncio.Event().wait()
            await asyncio.sleep(0)
            response = self.detail_responses[url]
            if isinstance(response, Exception):
                raise response
            return response
        except asyncio.CancelledError:
            self.cancelled_details += 1
            raise
        finally:
            self.active_details -= 1


def _response(url: str, body: str, *, status=200, content_type="text/html; charset=gbk"):
    return IndexerHttpResponse(
        url=url,
        status_code=status,
        headers={"content-type": content_type},
        body=body.encode("gbk"),
    )


def _adapter(site_id: str, http: _EmpireHttp) -> EmpireAdapter:
    if site_id == "dygang":
        return EmpireAdapter(
            site_id="dygang", site_name="电影港", base_url="https://www.dygang.tv/", http=http,
        )
    return EmpireAdapter(
        site_id="ys5266", site_name="5266影视", base_url="https://www.5266ys.net/", http=http,
    )


class EmpireAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_live_tv_and_animation_search_routes_are_not_discarded(self):
        # 真实“三体”搜索页：电影港并非只有 /ys/ 电影路由，剧版还有字母文件名。
        paths = ("/yx/20240322/54243.htm", "/dsj/20230116/st.htm", "/dmq/20221210/50941.htm")
        body = "".join(f'<a class="classlinkclass" href="{path}">三体</a>' for path in paths)
        adapter = _adapter("dygang", None)
        result = adapter._parse_results(body, "https://www.dygang.tv/e/search/result/?searchid=1")
        self.assertEqual([item.detail_url for item in result], [adapter.base_url.rstrip("/") + path for path in paths])

    async def test_form_uses_gbk_and_site_specific_empire_fields(self):
        for site_id, host, body in (
            ("dygang", "www.dygang.tv", _DYGANG_RESULT),
            ("ys5266", "www.5266ys.net", _YS_RESULT),
        ):
            with self.subTest(site_id=site_id):
                result_url = f"https://{host}/e/search/result.html"
                detail_url = (
                    "https://www.dygang.tv/ys/2025/123.htm"
                    if site_id == "dygang"
                    else "https://www.5266ys.net/movie/456.html"
                )
                http = _EmpireHttp(
                    _response(result_url, body),
                    {detail_url: _response(detail_url, f'<a href="magnet:?xt=urn:btih:{_VALID_HASH}&amp;dn=测试片 S01E02 1080p">下载 1080p</a>')},
                )
                await _adapter(site_id, http).search(IndexerSearchRequest.create("测试片"))

                url, content, headers = http.form_calls[0]
                self.assertEqual(url, f"https://{host}/e/search/index.php")
                decoded = unquote_to_bytes(content.decode("ascii")).decode("gbk")
                self.assertIn("测试片", decoded)
                self.assertIn("application/x-www-form-urlencoded", headers["Content-Type"])
                self.assertEqual(headers["Referer"], f"https://{host}/")
                if site_id == "dygang":
                    self.assertIn(b"Submit=", content)
                    self.assertNotIn(b"submit=", content)
                else:
                    self.assertIn(b"submit=", content)

    async def test_relative_detail_url_uses_final_post_redirect_url_and_keeps_release_identity(self):
        final_search_url = "https://www.dygang.tv/e/search/2025/result.html"
        detail_url = "https://www.dygang.tv/ys/2025/123.htm"
        http = _EmpireHttp(
            _response(final_search_url, '<a class="classlinkclass" href="../../../ys/2025/123.htm">测试电影</a>'),
            {
                detail_url: _response(
                    detail_url,
                    f'<a href="magnet:?xt=urn:btih:{_VALID_HASH}&amp;dn=Movie.S01E02.1080p.WEB-DL">下载链接（S01E02 1080P）</a>',
                ),
            },
        )

        page = await _adapter("dygang", http).search(IndexerSearchRequest.create("测试"))

        self.assertEqual(http.get_calls[0][0], detail_url)
        self.assertEqual(page.items[0].detail_url, detail_url)
        self.assertIn("Movie.S01E02.1080p.WEB-DL", page.items[0].title)
        self.assertIn("测试电影", page.items[0].title)
        self.assertEqual(page.items[0].site_name, "电影港")

    async def test_explicit_empty_result_is_a_successful_empty_page(self):
        http = _EmpireHttp(_response(
            "https://www.5266ys.net/e/search/result.html",
            "<html><body><div>没有找到相关结果</div></body></html>",
        ))

        page = await _adapter("ys5266", http).search(IndexerSearchRequest.create("不存在"))

        self.assertEqual(page.items, [])
        self.assertEqual(page.errors, ())
        self.assertFalse(page.has_more)

    async def test_unrecognized_search_response_is_not_silently_reported_as_empty(self):
        http = _EmpireHttp(_response(
            "https://www.dygang.tv/e/search/result.html",
            "<html><body>登录后查看</body></html>",
        ))

        with self.assertRaises(IndexerInvalidResponse):
            await _adapter("dygang", http).search(IndexerSearchRequest.create("测试"))

    async def test_search_http_error_is_raised(self):
        http = _EmpireHttp(_response(
            "https://www.dygang.tv/e/search/index.php", "upstream failure", status=503,
        ))

        with self.assertRaises(IndexerUnavailable):
            await _adapter("dygang", http).search(IndexerSearchRequest.create("测试"))

    async def test_invalid_magnets_are_skipped_without_false_site_error(self):
        detail_url = "https://www.dygang.tv/ys/2025/123.htm"
        http = _EmpireHttp(
            _response("https://www.dygang.tv/e/search/result.html", _DYGANG_RESULT),
            {detail_url: _response(detail_url, '<a href="magnet:?xt=urn:btih:not-a-hash">坏磁力</a>')},
        )

        page = await _adapter("dygang", http).search(IndexerSearchRequest.create("测试"))

        self.assertEqual(page.items, [])
        self.assertEqual(page.errors, ())

    async def test_normal_detail_without_supported_magnets_is_not_a_parse_error(self):
        detail_url = "https://www.dygang.tv/ys/2025/123.htm"
        http = _EmpireHttp(
            _response("https://www.dygang.tv/e/search/result.html", _DYGANG_RESULT),
            {
                detail_url: _response(
                    detail_url,
                    "<html><body><h1>测试电影</h1><p>ed2k://|file|资源</p><a href='https://pan.example/share'>网盘下载</a></body></html>",
                ),
            },
        )

        page = await _adapter("dygang", http).search(IndexerSearchRequest.create("测试"))

        self.assertEqual(page.items, [])
        self.assertEqual(page.errors, ())

    async def test_detail_challenge_is_reported_as_partial_site_error(self):
        detail_url = "https://www.dygang.tv/ys/2025/123.htm"
        http = _EmpireHttp(
            _response("https://www.dygang.tv/e/search/result.html", _DYGANG_RESULT),
            {
                detail_url: _response(
                    detail_url,
                    "<html><body>Just a moment... verify you are human</body></html>",
                ),
            },
        )

        page = await _adapter("dygang", http).search(IndexerSearchRequest.create("测试"))

        self.assertEqual(page.items, [])
        self.assertEqual(len(page.errors), 1)
        self.assertEqual(page.errors[0].site_id, "btbtla")
        self.assertIn("电影港", page.errors[0].message)

    async def test_partial_detail_failure_keeps_other_items_and_identifies_subsite(self):
        search_url = "https://www.5266ys.net/e/search/result.html"
        first = "https://www.5266ys.net/movie/1.html"
        second = "https://www.5266ys.net/movie/2.html"
        search = '''<h3><a href="/movie/1.html">电影一</a></h3>
        <h3><a href="/movie/2.html">电影二</a></h3>'''
        http = _EmpireHttp(
            _response(search_url, search),
            {
                first: _response(first, f'<a href="magnet:?xt=urn:btih:{_VALID_HASH}&amp;dn=发行版 1080p">下载一</a>'),
                second: IndexerUnavailable("fixture failure"),
            },
        )

        page = await _adapter("ys5266", http).search(IndexerSearchRequest.create("电影"))

        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0].detail_url, first)
        self.assertEqual(len(page.errors), 1)
        self.assertEqual(page.errors[0].site_id, "btbtla")
        self.assertIn("5266影视", page.errors[0].message)

    async def test_detail_requests_are_bounded_and_duplicate_infohashes_are_removed(self):
        search_url = "https://www.dygang.tv/e/search/result.html"
        paths = [f"/ys/2025/{index}.htm" for index in range(1, 4)]
        search = "".join(
            f'<a class="classlinkclass" href="{path}">电影 {index}</a>'
            for index, path in enumerate(paths, start=1)
        )
        details = {
            f"https://www.dygang.tv{path}": _response(
                f"https://www.dygang.tv{path}",
                f'<a href="magnet:?xt=urn:btih:{_VALID_HASH}&amp;dn=同一资源 S01E01">资源</a>',
            )
            for path in paths
        }
        http = _EmpireHttp(_response(search_url, search), details)

        page = await _adapter("dygang", http).search(IndexerSearchRequest.create("电影"))

        self.assertEqual(len(http.get_calls), 3)
        self.assertLessEqual(http.max_active_details, 3)
        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0].magnet and page.items[0].magnet.split("&", 1)[0], f"magnet:?xt=urn:btih:{_VALID_HASH}")

    async def test_later_pages_return_empty_without_network(self):
        http = _EmpireHttp(None)

        page = await _adapter("dygang", http).search(IndexerSearchRequest.create("电影", page=2))

        self.assertEqual(page.items, [])
        self.assertEqual(http.form_calls, [])
        self.assertEqual(http.get_calls, [])

    async def test_external_cancellation_cancels_and_reaps_all_detail_tasks(self):
        search_url = "https://www.dygang.tv/e/search/result.html"
        paths = [f"/ys/2025/{index}.htm" for index in range(1, 4)]
        search = "".join(
            f'<a class="classlinkclass" href="{path}">电影 {index}</a>'
            for index, path in enumerate(paths, start=1)
        )
        details = {
            f"https://www.dygang.tv{path}": _response(
                f"https://www.dygang.tv{path}", "<html><body>unused</body></html>",
            )
            for path in paths
        }
        http = _EmpireHttp(_response(search_url, search), details, block_details=True)
        search_task = asyncio.create_task(
            _adapter("dygang", http).search(IndexerSearchRequest.create("电影")),
        )
        await asyncio.wait_for(http.all_details_started.wait(), timeout=1)

        search_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await search_task

        self.assertEqual(http.cancelled_details, 3)
        self.assertEqual(http.active_details, 0)

    async def test_search_error_is_not_converted_to_empty_page(self):
        http = _EmpireHttp(IndexerUnavailable("network error"))

        with self.assertRaises(IndexerUnavailable):
            await _adapter("ys5266", http).search(IndexerSearchRequest.create("电影"))


if __name__ == "__main__":
    unittest.main()
