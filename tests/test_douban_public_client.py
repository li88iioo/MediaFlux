from __future__ import annotations

import json
import threading
import time
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

import requests

from app.clients.douban_public import DoubanPublicClient, DoubanPublicPage
from app.discovery.models import (
    ProviderInvalidResponse,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)


class FakeCookies:
    def __init__(self):
        self.clear_calls = 0

    def clear(self):
        self.clear_calls += 1


class FakeResponse:
    def __init__(
        self,
        payload=b"",
        *,
        status_code=200,
        content_type=None,
        headers=None,
        url="https://movie.douban.com/fixed",
        chunks=None,
    ):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            content_type = content_type or "application/json; charset=utf-8"
        elif isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.body = bytes(payload)
        self.status_code = status_code
        self.headers = dict(headers or {})
        if content_type:
            self.headers.setdefault("Content-Type", content_type)
        self.url = url
        self._chunks = list(chunks) if chunks is not None else [self.body]
        self.closed = False

    def iter_content(self, chunk_size=65536):
        del chunk_size
        yield from self._chunks

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.cookies = FakeCookies()

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected GET {url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class CapturingSession(requests.Session):
    def __init__(self, payload):
        super().__init__()
        self.payload = payload
        self.prepared_requests = []

    def send(self, request, **kwargs):
        del kwargs
        self.prepared_requests.append(request)
        response = requests.Response()
        response.status_code = 200
        response.headers = {"Content-Type": "application/json; charset=utf-8"}
        response.url = request.url
        response.request = request
        response._content = json.dumps(self.payload, ensure_ascii=False).encode("utf-8")
        response._content_consumed = True
        return response


class SharedGateProbe:
    def __init__(self):
        self.guard = threading.Lock()
        self.first_entered = threading.Event()
        self.second_entered = threading.Event()
        self.release_first = threading.Event()
        self.entries = 0
        self.active = 0
        self.max_active = 0


class ProbeSession(FakeSession):
    def __init__(self, probe, item_id):
        super().__init__(FakeResponse({"subjects": [{"id": item_id, "title": item_id}]}))
        self.probe = probe

    def get(self, url, **kwargs):
        with self.probe.guard:
            index = self.probe.entries
            self.probe.entries += 1
            self.probe.active += 1
            self.probe.max_active = max(self.probe.max_active, self.probe.active)
        try:
            if index == 0:
                self.probe.first_entered.set()
                if not self.probe.release_first.wait(timeout=2):
                    raise AssertionError("first process-wide request was not released")
            else:
                self.probe.second_entered.set()
            return super().get(url, **kwargs)
        finally:
            with self.probe.guard:
                self.probe.active -= 1


class BlockingSession(FakeSession):
    def __init__(self):
        super().__init__(
            FakeResponse({"subjects": [{"id": "1", "title": "First"}]}),
            FakeResponse({"subjects": [{"id": "2", "title": "Second"}]}),
        )
        self.first_entered = threading.Event()
        self.release_first = threading.Event()
        self._guard = threading.Lock()
        self.active = 0
        self.max_active = 0

    def get(self, url, **kwargs):
        with self._guard:
            index = len(self.calls)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if index == 0:
                self.first_entered.set()
                if not self.release_first.wait(timeout=2):
                    raise AssertionError("first request was not released")
            return super().get(url, **kwargs)
        finally:
            with self._guard:
                self.active -= 1


SHOWING_HTML = """
<!doctype html><html><body>
<ul class="lists">
  <li class="list-item" data-title="流浪地球 2" data-score="8.3"
      data-subject="35267208" data-release="2023">
    <div class="poster"><a href="/subject/35267208/">
      <img alt="流浪地球 2" src="https://img1.doubanio.com/view/photo/s_ratio_poster/public/p2884280708.webp">
    </a></div>
    <span class="abstract">中国大陆 / 科幻</span>
    <script>window.evil = '<b>do not return</b>';</script>
  </li>
</ul>
<a class="next" href="?page=2">后页</a>
</body></html>
"""

COMING_HTML = """
<html><body><table class="coming_list"><tbody>
<tr class="item">
  <td class="release_date">08月01日</td>
  <td class="title"><a href="https://movie.douban.com/subject/36612345/">明日之片</a></td>
  <td class="rating">8.0</td>
  <td><img src="https://img2.doubanio.com/view/photo/s_ratio_poster/public/p123.webp"></td>
</tr>
</tbody></table></body></html>
"""

TOP250_HTML = """
<html><body><ol class="grid_view">
<li><div class="item">
  <div class="pic"><a href="https://movie.douban.com/subject/1292052/">
    <img alt="肖申克的救赎" src="https://img3.doubanio.com/view/photo/s_ratio_poster/public/p480747492.webp">
  </a></div>
  <div class="info"><div class="hd"><a href="/subject/1292052/">
    <span class="title">肖申克的救赎</span><span class="other">/ The Shawshank Redemption</span>
  </a></div><div class="bd"><p>导演: 弗兰克·德拉邦特<br>1994 / 美国 / 剧情</p>
    <span class="rating_num">9.7</span>
  </div></div>
</div></li>
</ol><span class="next"><a href="?start=20&amp;filter=">后页&gt;</a></span>
</body></html>
"""

TWO_ITEM_HTML = """
<html><body><ol class="grid_view">
<li><div class="item"><a href="/subject/101/"><img alt="第一部" src="https://img1.doubanio.com/101.jpg"></a><p>第一简介<br>2001</p><span class="rating_num">8.1</span></div></li>
<li><div class="item"><a href="/subject/102/"><img alt="第二部" src="https://img2.doubanio.com/102.jpg"></a><p>第二简介<br>2002</p><span class="rating_num">8.2</span></div></li>
</ol></body></html>
"""

VOID_DETAIL_HTML = """
<html><head>
<meta property="og:title" content="Void 元素电影 (豆瓣)">
<meta property="og:image" content="https://img3.doubanio.com/view/photo/l/public/p300.webp">
<meta name="description" content="来自 meta 的简介">
</head><body>
<span property="v:itemreviewed"><img src="https://img1.doubanio.com/decorative.jpg">Void 元素电影</span>
<strong class="rating_num" property="v:average">8.8</strong>
<div>不得污染标题</div>
</body></html>
"""


DETAIL_HTML = """
<html><head>
<meta property="og:title" content="不应覆盖 JSON-LD">
<meta property="og:image" content="https://img9.doubanio.com/view/photo/l/public/p480747492.webp">
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Movie",
  "name": "<b>肖申克的救赎</b>",
  "alternateName": "The Shawshank Redemption",
  "image": "https://img1.doubanio.com/view/photo/l/public/p480747492.webp",
  "datePublished": "1994-09-10",
  "description": "<p>希望让人自由。</p>",
  "aggregateRating": {"ratingValue": "9.7"}
}
</script>
</head><body>
<span property="v:itemreviewed">错误的语义标题</span>
<strong class="rating_num" property="v:average">1.0</strong>
<script>evil(); steal()</script>
</body></html>
"""

SEMANTIC_DETAIL_HTML = """
<html><head>
<meta property="og:title" content="语义电影 (豆瓣)">
<meta property="og:image" content="https://img2.doubanio.com/view/photo/l/public/p222.webp">
<meta name="description" content="干净简介 &amp; 更多">
</head><body>
<span property="v:itemreviewed">语义电影</span><span class="year">(2024)</span>
<strong class="rating_num" property="v:average">8.6</strong>
<span property="v:initialReleaseDate" content="2024-05-20">2024-05-20</span>
</body></html>
"""


class DoubanPublicClientTests(unittest.TestCase):
    def make_client(self, *responses, **kwargs):
        session = FakeSession(*responses)
        client = DoubanPublicClient(
            session=session,
            min_interval=0,
            timeout=(2.0, 6.0),
            **kwargs,
        )
        return client, session

    @staticmethod
    def json_response(subjects):
        return FakeResponse({"subjects": subjects})

    def test_json_lists_use_fixed_host_exact_endpoint_and_no_cookie_or_redirects(self):
        client, session = self.make_client(
            self.json_response([{
                "id": "1292052",
                "title": "肖申克的救赎",
                "rate": "9.7",
                "cover": "https://img1.doubanio.com/view/photo/s_ratio_poster/public/p480747492.webp",
                "url": "https://attacker.invalid/raw-upstream-must-not-be-returned",
                "is_new": False,
                "episodes_info": "",
            }])
        )

        page = client.list_items(
            "recommend", "movie", 1, {"sort": "recommend", "tags": "剧情"}
        )

        self.assertEqual(session.calls[0][0], "https://movie.douban.com/j/search_subjects")
        options = session.calls[0][1]
        self.assertEqual(
            options["params"],
            {"type": "movie", "tag": "剧情", "sort": "recommend", "page_limit": 20, "page_start": 0},
        )
        self.assertEqual(options["timeout"], (2.0, 6.0))
        self.assertFalse(options["allow_redirects"])
        self.assertTrue(options["stream"])
        self.assertNotIn("Cookie", options["headers"])
        self.assertIn("application/json", options["headers"]["Accept"])
        self.assertEqual(options["headers"]["Referer"], "https://movie.douban.com/")
        self.assertGreaterEqual(session.cookies.clear_calls, 1)
        self.assertEqual(page.source, "public-json")
        self.assertEqual(page.items[0]["id"], "1292052")
        self.assertNotIn("url", page.items[0])

    def test_page_size_is_capped_and_page_two_maps_to_page_start(self):
        client, session = self.make_client(self.json_response([{"id": "1", "title": "剧集"}]), page_size=99)

        client.list_items("recommend", "tv", 2, {"sort": "recommend"})

        params = session.calls[0][1]["params"]
        self.assertEqual(params["type"], "tv")
        self.assertEqual(params["page_limit"], 20)
        self.assertEqual(params["page_start"], 20)

    def test_public_sort_values_are_forwarded_without_aliases(self):
        responses = [self.json_response([{"id": str(i), "title": f"Item {i}"}]) for i in range(3)]
        client, session = self.make_client(*responses)

        for sort_value in ("recommend", "rank", "time"):
            client.list_items("recommend", "movie", 1, {"sort": sort_value})

        self.assertEqual(
            [call[1]["params"]["sort"] for call in session.calls],
            ["recommend", "rank", "time"],
        )

    def test_legacy_sort_aliases_are_rejected_before_network_access(self):
        client, session = self.make_client()

        for sort_value in ("T", "U", "S", "R"):
            with self.subTest(sort=sort_value), self.assertRaises(ProviderInvalidResponse):
                client.list_items("recommend", "movie", 1, {"sort": sort_value})

        self.assertEqual(session.calls, [])

    def test_json_data_shape_is_normalized_and_page_result_is_immutable(self):
        payload = {
            "data": [{
                "id": 42,
                "title": "<b>干净标题</b><script>bad()</script>",
                "rate": "8.5",
                "cover": "https://img2.doubanio.com/view/photo/l/public/p42.webp",
                "year": 2025,
                "description": "<p>简介</p>",
                "is_new": 1,
                "episodes_info": "全12集",
            }]
        }
        client, _ = self.make_client(FakeResponse(payload))

        page = client.list_items("tv_hot", "tv", 1, {"sort": "rank"})

        self.assertIsInstance(page, DoubanPublicPage)
        self.assertIsInstance(page.items, tuple)
        self.assertEqual(page.items, ({
            "id": "42",
            "media_type": "tv",
            "title": "干净标题",
            "original_title": "",
            "year": "2025",
            "overview": "简介",
            "poster_url": "https://img2.doubanio.com/view/photo/l/public/p42.webp",
            "rating": 8.5,
            "release_date": "",
            "is_new": True,
            "episodes_info": "全12集",
        },))
        self.assertFalse(page.has_more)
        with self.assertRaises(FrozenInstanceError):
            page.source = "changed"
        with self.assertRaises(FrozenInstanceError):
            page.items += ({},)

    def test_full_json_page_reports_has_more(self):
        client, _ = self.make_client(self.json_response([
            {"id": str(i), "title": f"Item {i}"} for i in range(20)
        ]))
        self.assertTrue(client.list_items("movie_hot", "movie", 1, {}).has_more)

    def test_json_output_is_capped_and_has_more_uses_pre_truncation_evidence(self):
        client, _ = self.make_client(self.json_response([
            {"id": "1", "title": "One"},
            {"id": "2", "title": "Two"},
            {"id": "3", "title": "Three"},
        ]), page_size=2)

        page = client.list_items("recommend", "movie", 1, {})

        self.assertEqual([item["id"] for item in page.items], ["1", "2"])
        self.assertTrue(page.has_more)

    def test_partial_corrupt_json_items_are_skipped_and_raw_count_preserves_has_more(self):
        client, _ = self.make_client(self.json_response([
            {"id": "1", "title": "One"},
            {"id": "2", "title": {"html": "not scalar"}},
            {"id": "3", "title": "Three"},
        ]), page_size=2)

        page = client.list_items("recommend", "movie", 1, {})

        self.assertEqual([item["id"] for item in page.items], ["1", "3"])
        self.assertTrue(page.has_more)

    def test_structured_values_in_known_scalar_fields_reject_only_that_item(self):
        scalar_fields = (
            "id", "title", "rate", "cover", "url", "is_new",
            "episodes_info", "description", "year",
        )
        responses = []
        for index, field in enumerate(scalar_fields, 1):
            corrupt = {"id": str(index + 100), "title": f"Corrupt {field}"}
            corrupt[field] = {"nested": ["not", "scalar"]}
            responses.append(self.json_response([
                {"id": str(index), "title": f"Valid {field}"},
                corrupt,
            ]))
        client, _ = self.make_client(*responses)

        for index, field in enumerate(scalar_fields, 1):
            with self.subTest(field=field):
                page = client.list_items("recommend", "movie", 1, {})
                self.assertEqual([item["id"] for item in page.items], [str(index)])

    def test_nonempty_json_payload_with_only_corrupt_items_is_invalid(self):
        client, _ = self.make_client(self.json_response([
            {"id": "1", "title": ["not", "scalar"]},
            {"id": {"nested": "id"}, "title": "Bad ID"},
        ]))

        with self.assertRaises(ProviderInvalidResponse):
            client.list_items("recommend", "movie", 1, {})

    def test_category_defaults_use_current_tv_tags_and_unknown_input_is_rejected(self):
        responses = [self.json_response([{"id": str(i), "title": "TV"}]) for i in range(5)]
        client, session = self.make_client(*responses)

        client.list_items("tv_chinese_weekly", "tv", 1, {})
        client.list_items("tv_global_weekly", "tv", 1, {})
        self.assertEqual(
            [call[1]["params"]["tag"] for call in session.calls],
            ["国产剧", "美剧", "英剧", "日剧", "韩剧"],
        )

        with self.assertRaises(ProviderInvalidResponse):
            client.list_items("unsupported", "movie", 1, {})
        for category in ("tv_chinese_best_weekly", "tv_global_best_weekly"):
            with self.subTest(category=category), self.assertRaises(ProviderInvalidResponse):
                client.list_items(category, "tv", 1, {})
        with self.assertRaises(ProviderInvalidResponse):
            client.list_items("recommend", "animation", 1, {})
        with self.assertRaises(ProviderInvalidResponse):
            client.list_items("recommend", "movie", 0, {})
        self.assertEqual(len(session.calls), 5)

    def test_global_weekly_aggregates_regions_deduplicates_and_sorts_by_rating(self):
        client, session = self.make_client(
            self.json_response([
                {"id": "1", "title": "US One", "rate": "9.2"},
                {"id": "9", "title": "Duplicate First", "rate": "8.0"},
            ]),
            self.json_response([
                {"id": "2", "title": "UK Two", "rate": "9.8"},
            ]),
            self.json_response([
                {"id": "3", "title": "JP Three", "rate": "9.5"},
                {"id": "9", "title": "Duplicate Later", "rate": "9.9"},
            ]),
            self.json_response([
                {"id": "4", "title": "KR Four", "rate": ""},
            ]),
            page_size=20,
        )

        page = client.list_items("tv_global_weekly", "tv", 1, {})

        self.assertEqual([item["id"] for item in page.items], ["2", "3", "1", "9", "4"])
        self.assertEqual([call[1]["params"]["page_limit"] for call in session.calls], [5] * 4)
        self.assertEqual(page.source, "public-json-aggregate")
        self.assertFalse(page.has_more)

    def test_global_weekly_keeps_successful_regions_when_one_request_fails(self):
        client, _ = self.make_client(
            requests.ConnectionError("us unavailable"),
            self.json_response([{"id": "2", "title": "UK", "rate": "9.4"}]),
            self.json_response([{"id": "3", "title": "JP", "rate": "9.1"}]),
            self.json_response([{"id": "4", "title": "KR", "rate": "8.9"}]),
        )

        page = client.list_items("tv_global_weekly", "tv", 1, {})

        self.assertEqual([item["id"] for item in page.items], ["2", "3", "4"])
        self.assertEqual(page.source, "public-json-aggregate-partial")

    def test_high_score_weekly_categories_force_rank_sort(self):
        responses = [
            self.json_response([{"id": str(index), "title": "Weekly"}])
            for index in range(5)
        ]
        client, session = self.make_client(*responses)

        client.list_items("tv_chinese_weekly", "tv", 1, {"sort": "time"})
        client.list_items("tv_global_weekly", "tv", 1, {})

        self.assertEqual(
            [call[1]["params"]["sort"] for call in session.calls],
            ["rank"] * 5,
        )

    def test_void_img_does_not_merge_two_consecutive_list_items(self):
        client, _ = self.make_client(FakeResponse(TWO_ITEM_HTML, content_type="text/html"))

        page = client.list_items("movie_top250", "movie", 1, {})

        self.assertEqual(
            [(item["id"], item["title"], item["rating"]) for item in page.items],
            [("101", "第一部", 8.1), ("102", "第二部", 8.2)],
        )

    def test_void_meta_and_img_keep_detail_stack_matched(self):
        client, _ = self.make_client(FakeResponse(VOID_DETAIL_HTML, content_type="text/html"))

        detail = client.get_detail("300", "movie")

        self.assertEqual(detail["title"], "Void 元素电影")
        self.assertEqual(detail["poster_url"], "https://img3.doubanio.com/view/photo/l/public/p300.webp")
        self.assertEqual(detail["overview"], "来自 meta 的简介")
        self.assertEqual(detail["rating"], 8.8)
        self.assertNotIn("不得污染标题", detail["title"])

    def test_showing_html_is_conservatively_normalized(self):
        client, session = self.make_client(FakeResponse(SHOWING_HTML, content_type="text/html; charset=utf-8"))

        page = client.list_items("movie_showing", "movie", 1, {})

        self.assertEqual(session.calls[0][0], "https://movie.douban.com/cinema/nowplaying/")
        self.assertEqual(page.source, "public-html")
        self.assertTrue(page.has_more)
        self.assertEqual(page.items[0]["id"], "35267208")
        self.assertEqual(page.items[0]["title"], "流浪地球 2")
        self.assertEqual(page.items[0]["year"], "2023")
        self.assertEqual(page.items[0]["rating"], 8.3)
        self.assertEqual(page.items[0]["overview"], "中国大陆 / 科幻")
        self.assertNotIn("evil", repr(page.items))
        self.assertNotIn("<", repr(page.items))

    def test_coming_and_top250_html_use_fixed_paths_and_extract_fields(self):
        client, session = self.make_client(
            FakeResponse(COMING_HTML, content_type="text/html"),
            FakeResponse(TOP250_HTML, content_type="text/html"),
        )

        coming = client.list_items("movie_soon", "movie", 1, {})
        top = client.list_items("movie_top250", "movie", 2, {})

        self.assertEqual(session.calls[0][0], "https://movie.douban.com/cinema/later/")
        self.assertEqual(session.calls[1][0], "https://movie.douban.com/top250")
        self.assertEqual(session.calls[1][1]["params"], {"start": 20, "filter": ""})
        self.assertEqual(
            (coming.items[0]["id"], coming.items[0]["title"], coming.items[0]["release_date"]),
            ("36612345", "明日之片", "08月01日"),
        )
        self.assertEqual(
            (top.items[0]["id"], top.items[0]["title"], top.items[0]["original_title"]),
            ("1292052", "肖申克的救赎", "The Shawshank Redemption"),
        )
        self.assertEqual((top.items[0]["year"], top.items[0]["rating"]), ("1994", 9.7))
        self.assertTrue(top.has_more)

    def test_detail_prefers_json_ld_and_strips_markup_and_script_content(self):
        client, session = self.make_client(FakeResponse(DETAIL_HTML, content_type="text/html; charset=utf-8"))

        detail = client.get_detail("1292052", "movie")

        self.assertEqual(session.calls[0][0], "https://movie.douban.com/subject/1292052/")
        self.assertEqual(detail, {
            "id": "1292052",
            "media_type": "movie",
            "title": "肖申克的救赎",
            "original_title": "The Shawshank Redemption",
            "year": "1994",
            "overview": "希望让人自由。",
            "poster_url": "https://img1.doubanio.com/view/photo/l/public/p480747492.webp",
            "rating": 9.7,
            "release_date": "1994-09-10",
        })
        self.assertNotIn("evil", repr(detail))
        self.assertNotIn("steal", repr(detail))
        self.assertNotIn("<", repr(detail))

    def test_detail_falls_back_to_open_graph_and_semantic_nodes(self):
        client, _ = self.make_client(FakeResponse(SEMANTIC_DETAIL_HTML, content_type="text/html"))

        detail = client.get_detail("36600000", "movie")

        self.assertEqual(detail["title"], "语义电影")
        self.assertEqual(detail["year"], "2024")
        self.assertEqual(detail["release_date"], "2024-05-20")
        self.assertEqual(detail["rating"], 8.6)
        self.assertEqual(detail["overview"], "干净简介 & 更多")

    def test_empty_or_malformed_html_is_invalid_response(self):
        for body in ("<html><body>layout changed</body></html>", "<script>alert(1)</script>"):
            client, _ = self.make_client(FakeResponse(body, content_type="text/html"))
            with self.assertRaises(ProviderInvalidResponse):
                client.list_items("movie_top250", "movie", 1, {})

    def test_detail_id_and_media_type_are_validated_before_network(self):
        client, session = self.make_client()
        with self.assertRaises(ProviderInvalidResponse):
            client.get_detail("../admin", "movie")
        with self.assertRaises(ProviderInvalidResponse):
            client.get_detail("1292052", "animation")
        self.assertEqual(session.calls, [])

    def test_actual_requests_session_strips_credentials_from_prepared_request(self):
        session = CapturingSession({"subjects": [{"id": "1", "title": "Safe"}]})
        session.headers["Authorization"] = "Bearer session-secret"
        session.headers["Cookie"] = "manual=session-secret"
        session.headers["Proxy-Authorization"] = "Basic proxy-secret"
        session.auth = ("session-user", "session-password")
        session.cookies.set("sid", "cookie-secret", domain="movie.douban.com", path="/")

        client = DoubanPublicClient(session=session, min_interval=0)
        client.list_items("recommend", "movie", 1, {})

        prepared = session.prepared_requests[0]
        lowered = {key.lower(): value for key, value in prepared.headers.items()}
        self.assertNotIn("authorization", lowered)
        self.assertNotIn("proxy-authorization", lowered)
        self.assertNotIn("cookie", lowered)
        self.assertIsNone(session.auth)
        self.assertFalse(session.trust_env)

    def test_actual_requests_session_disables_netrc_before_preparing_request(self):
        session = CapturingSession({"subjects": [{"id": "1", "title": "Safe"}]})
        session.trust_env = True
        with patch("requests.sessions.get_netrc_auth", return_value=("netrc-user", "netrc-secret")) as netrc:
            client = DoubanPublicClient(session=session, min_interval=0)
            client.list_items("recommend", "movie", 1, {})

        prepared = session.prepared_requests[0]
        self.assertNotIn("Authorization", prepared.headers)
        self.assertFalse(session.trust_env)
        netrc.assert_not_called()

    def test_status_codes_map_to_structured_provider_errors(self):
        cases = (
            (429, ProviderRateLimited),
            (403, ProviderUnavailable),
            (418, ProviderUnavailable),
            (500, ProviderUnavailable),
        )
        for status, error_type in cases:
            with self.subTest(status=status):
                headers = {"Retry-After": "17"} if status == 429 else {}
                client, _ = self.make_client(FakeResponse(status_code=status, headers=headers))
                with self.assertRaises(error_type) as raised:
                    client.list_items("recommend", "movie", 1, {})
                self.assertNotIn("movie.douban.com", str(raised.exception))
                if status == 429:
                    self.assertEqual(raised.exception.retry_after, 17)

    def test_timeout_and_connection_errors_map_without_leaking_urls(self):
        errors = (
            (requests.Timeout("https://movie.douban.com/?secret=x"), ProviderTimeout),
            (requests.ConnectionError("https://movie.douban.com/?secret=x"), ProviderUnavailable),
        )
        for error, error_type in errors:
            with self.subTest(error_type=error_type):
                client, _ = self.make_client(error)
                with self.assertRaises(error_type) as raised:
                    client.list_items("recommend", "movie", 1, {})
                self.assertNotIn("secret", str(raised.exception))
                self.assertNotIn("douban.com", raised.exception.safe_message)

    def test_converted_request_errors_drop_sensitive_recursive_exception_chains(self):
        for request_error, error_type in (
            (requests.Timeout("https://movie.douban.com/path?token=timeout-secret"), ProviderTimeout),
            (requests.ConnectionError("https://movie.douban.com/path?token=connection-secret"), ProviderUnavailable),
        ):
            with self.subTest(error_type=error_type):
                client, _ = self.make_client(request_error)
                with self.assertRaises(error_type) as raised:
                    client.list_items("recommend", "movie", 1, {})

                current = raised.exception
                visited = set()
                while current is not None and id(current) not in visited:
                    visited.add(id(current))
                    rendered = f"{current!s} {current!r}"
                    self.assertNotIn("movie.douban.com", rendered)
                    self.assertNotIn("token=", rendered)
                    self.assertNotIn("secret", rendered)
                    current = current.__cause__ or current.__context__
                self.assertEqual(len(visited), 1)

    def test_json_content_type_top_level_and_syntax_are_checked(self):
        cases = (
            FakeResponse("{}", content_type="text/html"),
            FakeResponse("not-json", content_type="application/json"),
            FakeResponse([], content_type="application/json"),
            FakeResponse({"subjects": "not-a-list"}),
        )
        for response in cases:
            with self.subTest(response=response.body[:20]):
                client, _ = self.make_client(response)
                with self.assertRaises(ProviderInvalidResponse):
                    client.list_items("recommend", "movie", 1, {})

    def test_html_content_type_is_checked(self):
        client, _ = self.make_client(FakeResponse(SHOWING_HTML, content_type="application/json"))
        with self.assertRaises(ProviderInvalidResponse):
            client.list_items("movie_showing", "movie", 1, {})

    def test_content_length_and_streamed_size_ceiling_are_checked(self):
        responses = (
            FakeResponse(
                b"{}", content_type="application/json", headers={"Content-Length": "100"}
            ),
            FakeResponse(
                b"", content_type="application/json", chunks=[b"12345678", b"123456789"]
            ),
        )
        for response in responses:
            client, _ = self.make_client(response, max_response_bytes=16)
            with self.assertRaises(ProviderInvalidResponse):
                client.list_items("recommend", "movie", 1, {})
            self.assertTrue(response.closed)

    def test_malformed_upstream_url_ports_remain_structured(self):
        client, _ = self.make_client(self.json_response([{
            "id": "1",
            "title": "Malformed poster",
            "cover": "https://img1.doubanio.com:not-a-port/poster.jpg",
        }]))
        page = client.list_items("recommend", "movie", 1, {})
        self.assertEqual(page.items[0]["poster_url"], "")

        redirect = FakeResponse(
            status_code=302,
            headers={"Location": "https://movie.douban.com:not-a-port/subject/1/"},
        )
        client, _ = self.make_client(redirect)
        with self.assertRaises(ProviderUnavailable):
            client.list_items("recommend", "movie", 1, {})

    def test_off_host_redirect_is_rejected_and_same_host_redirect_is_followed_safely(self):
        off_host = FakeResponse(
            status_code=302,
            headers={"Location": "https://evil.invalid/steal?token=secret"},
        )
        client, session = self.make_client(off_host)
        with self.assertRaises(ProviderUnavailable) as raised:
            client.list_items("recommend", "movie", 1, {})
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn("evil.invalid", str(raised.exception))
        self.assertNotIn("secret", raised.exception.safe_message)

        redirected = FakeResponse(status_code=302, headers={"Location": "/j/search_subjects"})
        final = self.json_response([{"id": "1", "title": "Safe"}])
        client, session = self.make_client(redirected, final)
        page = client.list_items("recommend", "movie", 1, {})
        self.assertEqual(len(page.items), 1)
        self.assertEqual([call[0] for call in session.calls], [
            "https://movie.douban.com/j/search_subjects",
            "https://movie.douban.com/j/search_subjects",
        ])

    def test_rate_gate_uses_injected_clock_and_sleeper_for_one_second_spacing(self):
        now = [100.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        session = FakeSession(
            self.json_response([{"id": "1", "title": "One"}]),
            self.json_response([{"id": "2", "title": "Two"}]),
        )
        client = DoubanPublicClient(
            session=session,
            clock=lambda: now[0],
            sleeper=sleep,
            min_interval=1.0,
        )

        client.list_items("recommend", "movie", 1, {})
        client.list_items("recommend", "movie", 1, {})

        self.assertEqual(sleeps, [1.0])

    def test_rate_spacing_is_process_wide_across_client_instances(self):
        now = [500.0]
        sleeps = []

        def clock():
            return now[0]

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        first = DoubanPublicClient(
            session=FakeSession(self.json_response([{"id": "1", "title": "One"}])),
            clock=clock, sleeper=sleep, min_interval=1.0,
        )
        second = DoubanPublicClient(
            session=FakeSession(self.json_response([{"id": "2", "title": "Two"}])),
            clock=clock, sleeper=sleep, min_interval=1.0,
        )

        first.list_items("recommend", "movie", 1, {})
        second.list_items("recommend", "movie", 1, {})

        self.assertEqual(sleeps, [1.0])

    def test_rate_lock_serializes_requests_across_client_instances(self):
        probe = SharedGateProbe()
        first_client = DoubanPublicClient(session=ProbeSession(probe, "1"), min_interval=0)
        second_client = DoubanPublicClient(session=ProbeSession(probe, "2"), min_interval=0)
        errors = []

        def run(client):
            try:
                client.list_items("recommend", "movie", 1, {})
            except Exception as exc:  # pragma: no cover - assertion reports captured error
                errors.append(exc)

        first = threading.Thread(target=run, args=(first_client,))
        second = threading.Thread(target=run, args=(second_client,))
        first.start()
        self.assertTrue(probe.first_entered.wait(timeout=1))
        second.start()
        self.assertFalse(probe.second_entered.wait(timeout=0.1))
        self.assertEqual(probe.max_active, 1)
        probe.release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(probe.second_entered.is_set())
        self.assertEqual(probe.max_active, 1)

    def test_rate_gate_serializes_concurrent_requests(self):
        session = BlockingSession()
        client = DoubanPublicClient(session=session, min_interval=0)
        errors = []

        def run():
            try:
                client.list_items("recommend", "movie", 1, {})
            except Exception as exc:  # pragma: no cover - assertion reports captured error
                errors.append(exc)

        first = threading.Thread(target=run)
        second = threading.Thread(target=run)
        first.start()
        self.assertTrue(session.first_entered.wait(timeout=1))
        second.start()
        time.sleep(0.05)
        self.assertEqual(len(session.calls), 0)
        session.release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(session.max_active, 1)
        self.assertEqual(len(session.calls), 2)


if __name__ == "__main__":
    unittest.main()
