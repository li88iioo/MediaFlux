from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch
from urllib.parse import unquote_to_bytes

from bs4 import BeautifulSoup

from app.indexers.errors import IndexerChallengeRequired, IndexerInvalidResponse, IndexerQueryRejected, IndexerRateLimited, IndexerUnavailable
from app.indexers.http import IndexerHttpResponse
from app.indexers.models import IndexerSearchRequest
from app.indexers.providers.empire import EmpireAdapter, _DETAIL_TIMEOUT_SECONDS

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
        result = adapter._parse_results(BeautifulSoup(body, "lxml"), "https://www.dygang.tv/e/search/result/?searchid=1")
        self.assertEqual([item.detail_url for item in result], [adapter.base_url.rstrip("/") + path for path in paths])

    async def test_search_interval_starts_after_response_and_skips_invalid_queries(self):
        clock = [100.0]
        sleeps = []
        http = _EmpireHttp(_response("https://www.dygang.tv/e/search/", "<title>信息提示</title>没有搜索到相关的内容"))
        original_post = http.post_form
        async def delayed_response(*args, **kwargs):
            clock[0] += 3  # 首次握手/服务器响应不能占用后续安全间隔。
            return await original_post(*args, **kwargs)
        async def sleep(delay):
            sleeps.append(delay)
            clock[0] += delay
        adapter = _adapter("dygang", http)
        with (
            patch.object(http, "post_form", side_effect=delayed_response),
            patch("app.indexers.providers.empire.monotonic", side_effect=lambda: clock[0]),
            patch("app.indexers.providers.empire.asyncio.sleep", side_effect=sleep),
        ):
            await adapter.search(IndexerSearchRequest.create("电影"))
            self.assertEqual(sleeps, [])
            with self.assertRaises(IndexerQueryRejected):
                await adapter.search(IndexerSearchRequest.create("x" * 21))
            await adapter.search(IndexerSearchRequest.create("电影", page=2))
            self.assertEqual(sleeps, [])
            await adapter.search(IndexerSearchRequest.create("剧集"))
        self.assertEqual(sleeps, [5.5])
        self.assertEqual(adapter._search_finished, 111.5)
        self.assertEqual(len(http.form_calls), 2)

    async def test_cancelled_search_releases_form_lock_for_next_request(self):
        started = asyncio.Event()
        http = _EmpireHttp(_response("https://www.dygang.tv/e/search/", "<title>信息提示</title>没有搜索到相关的内容"))
        adapter = _adapter("dygang", http)
        async def blocked(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        with patch.object(http, "post_form", side_effect=blocked):
            task = asyncio.create_task(adapter.search(IndexerSearchRequest.create("电影")))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with patch("app.indexers.providers.empire._SEARCH_INTERVAL_SECONDS", 0):
            result = await asyncio.wait_for(adapter.search(IndexerSearchRequest.create("剧集")), 1)
        self.assertEqual(result.items, [])
        self.assertIsNotNone(adapter._search_finished)

    async def test_unacceptable_keywords_are_rejected_without_http(self):
        for site_id in ("dygang", "ys5266"):
            for query in ("a", "x" * 21, "天地玄黄宇宙洪荒日月盈", "电影😀"):
                with self.subTest(site=site_id, query=query):
                    http = _EmpireHttp(None)
                    with self.assertRaises(IndexerQueryRejected):
                        await _adapter(site_id, http).search(IndexerSearchRequest.create(query))
                    self.assertEqual(http.form_calls, [])

    async def test_gbk_byte_boundaries_accept_complete_terms(self):
        for query in ("ab", "x" * 20, "仙", "天地玄黄宇宙洪荒日月"):
            with self.subTest(query=query):
                http = _EmpireHttp(_response("https://www.dygang.tv/e/search/", "<title>信息提示</title>没有搜索到相关的内容"))
                result = await _adapter("dygang", http).search(IndexerSearchRequest.create(query))
                self.assertFalse(result.items)
                self.assertEqual(len(http.form_calls), 1)
                body = unquote_to_bytes(http.form_calls[0][1].decode("ascii")).decode("gbk")
                self.assertIn("keyboard=" + query + "&", body)

    async def test_cms_notice_pages_have_distinct_safe_error_types(self):
        cases = (
            ("系统限制的搜索关键字只能在 2~20 个字符之间", IndexerQueryRejected),
            ("两次搜索间隔不能小于10秒，请不要频繁搜索", IndexerRateLimited),
            ("系统限制的搜索时间间隔为 5 秒,请稍后再搜索", IndexerRateLimited),
            ("请输入验证码完成安全验证", IndexerChallengeRequired),
            ("搜索功能暂时关闭", IndexerUnavailable),
        )
        for site in ("dygang", "ys5266"):
            for notice, error in cases:
                with self.subTest(site=site, notice=notice):
                    host = "www.dygang.tv" if site == "dygang" else "www.5266ys.net"
                    http = _EmpireHttp(_response(f"https://{host}/e/search/", f"<title>信息提示</title><p>{notice}</p><script>const secret='private-fixture';</script>"))
                    with self.assertRaises(error) as caught:
                        await _adapter(site, http).search(IndexerSearchRequest.create("示例"))
                    self.assertNotIn("private-fixture", caught.exception.public_message)
                    self.assertFalse(http.get_calls)

    async def test_challenge_pages_are_classified_for_success_and_block_statuses(self):
        for status in (200, 403, 503):
            with self.subTest(status=status):
                http = _EmpireHttp(_response("https://www.dygang.tv/e/search/", "<html>Just a moment... verify you are human</html>",status=status))
                with self.assertRaises(IndexerChallengeRequired):
                    await _adapter("dygang",http).search(IndexerSearchRequest.create("示例"))

    async def test_notice_words_inside_a_real_detail_do_not_become_site_errors(self):
        url = "https://www.dygang.tv/ys/2025/123.htm"
        detail = f'<title>电影信息</title><p>片中角色没有搜索到答案，频繁搜索、人机验证只是剧情描述。</p><a href="magnet:?xt=urn:btih:{_VALID_HASH}">片源</a>'
        http = _EmpireHttp(_response("https://www.dygang.tv/e/search/",_DYGANG_RESULT),{url:_response(url,detail)})
        result = await _adapter("dygang",http).search(IndexerSearchRequest.create("示例"))
        self.assertEqual(len(result.items),1)
        self.assertEqual(result.errors,())

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
        self.assertEqual(page.errors[0].site_id, "dygang")
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
        self.assertEqual(page.errors[0].site_id, "ys5266")
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

    async def test_fourth_candidate_fills_when_first_three_have_no_magnet(self):
        search_url = "https://www.dygang.tv/e/search/result.html"
        paths = [f"/ys/2025/{index}.htm" for index in range(1, 5)]
        search = "".join(
            f'<a class="classlinkclass" href="{path}">电影 {index}</a>'
            for index, path in enumerate(paths, start=1)
        )
        details = {
            f"https://www.dygang.tv{path}": _response(
                f"https://www.dygang.tv{path}",
                (
                    f'<a href="magnet:?xt=urn:btih:{_VALID_HASH}&amp;dn=第四候选">资源</a>'
                    if index == 4
                    else f"<html><body>第 {index} 个详情只有介绍</body></html>"
                ),
            )
            for index, path in enumerate(paths, start=1)
        }
        details[f"https://www.dygang.tv{paths[1]}"] = IndexerUnavailable("fixture failure")
        http = _EmpireHttp(_response(search_url, search), details)

        page = await _adapter("dygang", http).search(IndexerSearchRequest.create("电影"))

        self.assertEqual(len(http.get_calls), 4)
        self.assertEqual([item.detail_url for item in page.items], [f"https://www.dygang.tv{paths[3]}"])
        self.assertEqual(len(page.errors), 1)
        self.assertEqual(page.errors[0].code, "unavailable")

    async def test_successful_first_batch_does_not_fetch_later_candidates(self):
        search_url = "https://www.dygang.tv/e/search/result.html"
        paths = [f"/ys/2025/{index}.htm" for index in range(1, 5)]
        search = "".join(
            f'<a class="classlinkclass" href="{path}">电影 {index}</a>'
            for index, path in enumerate(paths, start=1)
        )
        details = {
            f"https://www.dygang.tv{path}": _response(
                f"https://www.dygang.tv{path}",
                (
                    f'<a href="magnet:?xt=urn:btih:{_VALID_HASH}&amp;dn=首批资源">资源</a>'
                    if index == 1
                    else f"<html><body>第 {index} 个详情</body></html>"
                ),
            )
            for index, path in enumerate(paths, start=1)
        }
        http = _EmpireHttp(_response(search_url, search), details)

        page = await _adapter("dygang", http).search(IndexerSearchRequest.create("电影"))

        self.assertEqual(len(http.get_calls), 3)
        self.assertEqual(len(page.items), 1)

    async def test_expired_shared_deadline_prevents_creating_next_batch(self):
        search_url = "https://www.dygang.tv/e/search/result.html"
        paths = [f"/ys/2025/{index}.htm" for index in range(1, 7)]
        search = "".join(
            f'<a class="classlinkclass" href="{path}">电影 {index}</a>'
            for index, path in enumerate(paths, start=1)
        )
        details = {
            f"https://www.dygang.tv{path}": _response(
                f"https://www.dygang.tv{path}", f"<html><body>第 {index} 个详情</body></html>",
            )
            for index, path in enumerate(paths, start=1)
        }
        http = _EmpireHttp(_response(search_url, search), details)
        loop = asyncio.get_running_loop()
        real_loop_time = loop.time
        deadline_offset = [0.0]
        real_wait = asyncio.wait
        wait_calls = 0

        def controlled_time():
            return real_loop_time() + deadline_offset[0]

        async def wait_then_expire_deadline(
            tasks, *, timeout=None, return_when=asyncio.ALL_COMPLETED,
        ):
            nonlocal wait_calls
            done, pending = await real_wait(
                tasks, timeout=timeout, return_when=return_when,
            )
            wait_calls += 1
            if wait_calls == 1:
                # 首批正常完成但没有磁力时，共享详情预算已经耗尽。
                deadline_offset[0] = _DETAIL_TIMEOUT_SECONDS + 1
            return done, pending

        with patch.object(loop, "time", side_effect=controlled_time):
            with patch(
                "app.indexers.providers.empire.asyncio.wait",
                side_effect=wait_then_expire_deadline,
            ):
                page = await _adapter("dygang", http).search(
                    IndexerSearchRequest.create("电影"),
                )

        self.assertEqual([error.code for error in page.errors], ["timeout"])
        self.assertEqual(wait_calls, 1)
        self.assertEqual(len(http.get_calls), 3)
        self.assertEqual(http.active_details, 0)
        self.assertEqual(page.items, [])

    async def test_total_detail_requests_are_capped_at_six(self):
        search_url = "https://www.dygang.tv/e/search/result.html"
        paths = [f"/ys/2025/{index}.htm" for index in range(1, 10)]
        search = "".join(
            f'<a class="classlinkclass" href="{path}">电影 {index}</a>'
            for index, path in enumerate(paths, start=1)
        )
        details = {
            f"https://www.dygang.tv{path}": _response(
                f"https://www.dygang.tv{path}", f"<html><body>第 {index} 个详情</body></html>",
            )
            for index, path in enumerate(paths, start=1)
        }
        http = _EmpireHttp(_response(search_url, search), details)

        await _adapter("dygang", http).search(IndexerSearchRequest.create("电影"))

        self.assertEqual(len(http.get_calls), 6)
        self.assertLessEqual(http.max_active_details, 3)

    async def test_rate_limit_or_challenge_stops_follow_up_candidates(self):
        search_url = "https://www.dygang.tv/e/search/result.html"
        paths = [f"/ys/2025/{index}.htm" for index in range(1, 5)]
        search = "".join(
            f'<a class="classlinkclass" href="{path}">电影 {index}</a>'
            for index, path in enumerate(paths, start=1)
        )

        for stop_response, expected_code in (
            (_response("unused", "限流", status=429), "rate_limited"),
            (_response("unused", "<html><body>Just a moment... verify you are human</body></html>"), "challenge_required"),
        ):
            with self.subTest(expected_code=expected_code):
                details = {
                    f"https://www.dygang.tv{path}": _response(
                        f"https://www.dygang.tv{path}", "<html><body>无磁力详情</body></html>",
                    )
                    for path in paths
                }
                details[f"https://www.dygang.tv{paths[0]}"] = stop_response
                http = _EmpireHttp(_response(search_url, search), details)

                page = await _adapter("dygang", http).search(IndexerSearchRequest.create("电影"))

                self.assertEqual(len(http.get_calls), 3)
                self.assertEqual(page.errors[0].code, expected_code)

    async def test_detail_timeout_cancels_batch_and_does_not_start_follow_up(self):
        search_url = "https://www.dygang.tv/e/search/result.html"
        paths = [f"/ys/2025/{index}.htm" for index in range(1, 5)]
        search = "".join(
            f'<a class="classlinkclass" href="{path}">电影 {index}</a>'
            for index, path in enumerate(paths, start=1)
        )
        details = {
            f"https://www.dygang.tv{path}": _response(
                f"https://www.dygang.tv{path}", "<html><body>无磁力详情</body></html>",
            )
            for path in paths
        }
        http = _EmpireHttp(_response(search_url, search), details, block_details=True)

        with patch("app.indexers.providers.empire._DETAIL_TIMEOUT_SECONDS", 0.01):
            page = await _adapter("dygang", http).search(IndexerSearchRequest.create("电影"))

        self.assertEqual(len(http.get_calls), 3)
        self.assertEqual(http.cancelled_details, 3)
        self.assertEqual(http.active_details, 0)
        self.assertEqual(len(page.errors), 3)

    async def test_later_pages_return_empty_without_network(self):
        http = _EmpireHttp(None)

        page = await _adapter("dygang", http).search(IndexerSearchRequest.create("电影", page=2))

        self.assertEqual(page.items, [])
        self.assertEqual(http.form_calls, [])
        self.assertEqual(http.get_calls, [])

    async def test_external_cancellation_cancels_and_reaps_all_detail_tasks(self):
        search_url = "https://www.dygang.tv/e/search/result.html"
        paths = [f"/ys/2025/{index}.htm" for index in range(1, 5)]
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
