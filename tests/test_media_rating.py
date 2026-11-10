"""公共媒体评分查询与原生 SDK 身份条件回归测试。"""

from __future__ import annotations
from typing import Any
from typing import cast

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.discovery.ratings import (
    _rating_from_douban_html,
    _select_card,
)
from app import sdk
from app.modules.web_evidence import _result as make_result
from app.discovery.models import MediaCard
from app.discovery.search import DiscoverySearchResult


def _card(
    *,
    title: str = "九门",
    media_type: str = "tv",
    year: str = "2026",
    rating: float | None = 7.6,
    external_id: str = "12345678",
) -> MediaCard:
    return MediaCard(
        provider="douban",
        external_id=external_id,
        media_type=media_type,
        title=title,
        original_title="Jiu Men" if title == "九门" else "",
        year=year,
        rating=rating,
        rating_source="douban",
    )


def _search_result(
    *items: MediaCard,
    errors: tuple[dict, ...] = (),
    succeeded: tuple[str, ...] = ("douban",),
) -> DiscoverySearchResult:
    return DiscoverySearchResult(
        query="九门",
        page=1,
        items=tuple(items),
        has_more=False,
        providers_attempted=("douban",),
        providers_succeeded=succeeded,
        errors=errors,
    )


class MediaRatingArgumentTests(unittest.TestCase):
    @patch("app.discovery.ratings.config.get_bool", return_value=True)
    @patch("app.discovery.ratings.get_discovery_search_service")
    def test_arguments_are_strict_and_normalized(self, source, _enabled):
        source.return_value.search.return_value = _search_result(_card(title="九門"))
        result = sdk.media.rating("  九門  ", media_type="TV", year=2026, allow_web_fallback=False)
        source.return_value.search.assert_called_once_with("九門", 1, ["douban"])
        self.assertEqual(result["data"]["media_type"], "tv")
        self.assertEqual(result["data"]["year"], "2026")
        source.reset_mock()
        invalid = (
            {}, {"query": None}, {"query": ""}, {"query": "x", "unknown": True},
            {"query": "x", "media_type": "anime"}, {"query": "x", "year": True},
            {"query": "x", "year": "26"}, {"query": "x", "allow_web_fallback": 1},
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                sdk.media.rating(**cast(dict[str, Any], value))
        source.assert_not_called()

    def test_arguments_reject_credentials_before_network_access(self):
        with self.assertRaises(ValueError) as raised:
            sdk.media.rating("九门 api_key=abcdefgh123456")
        self.assertIn("凭据", str(raised.exception))

    def test_ambiguous_exact_titles_are_not_guessed(self):
        self.assertIsNone(
            _select_card(
                (
                    _card(media_type="tv", year="2026"),
                    _card(media_type="movie", year="2025"),
                ),
                query="九门",
                media_type="",
                year="",
            )
        )
        selected = _select_card(
            (
                _card(media_type="tv", year="2026"),
                _card(media_type="movie", year="2025"),
            ),
            query="九门",
            media_type="tv",
            year="2026",
        )
        assert (selected) is not None
        self.assertEqual(selected.media_type, "tv")


class MediaRatingExecutionTests(unittest.TestCase):
    @patch("app.discovery.ratings.config.get_bool", return_value=True)
    @patch("app.discovery.ratings.get_discovery_search_service")
    @patch("app.discovery.ratings.get_discovery_service")
    def test_requested_year_is_verified_even_when_search_already_has_a_rating(
        self, detail_factory, search_factory, _enabled
    ):
        search_factory.return_value.search.return_value = _search_result(_card(year="", rating=7.4))
        for detail_year, expected in (("2025", False), ("", False), ("2026", True)):
            with self.subTest(detail_year=detail_year):
                detail_factory.return_value.get_detail.return_value = _card(year=detail_year, rating=7.4)
                result = sdk.media.rating("九门", media_type="tv", year="2026", allow_web_fallback=False)
                self.assertIs(result["ok"], expected)
                self.assertEqual(result["data"]["rating"], 7.4 if expected else None)
                if expected:
                    self.assertEqual(result["data"]["year"], "2026")
                    self.assertEqual(result["data"]["source_method"], "douban_detail")

    @patch("app.discovery.ratings.config.get_bool", return_value=True)
    @patch("app.discovery.ratings.get_discovery_search_service")
    @patch("app.discovery.ratings.search_web")
    def test_same_type_remakes_need_a_year_and_never_fall_back_to_search_order(
        self, web_search, search_factory, _enabled
    ):
        search_factory.return_value.search.return_value = _search_result(
            _card(year="2000", external_id="100", rating=7.4),
            _card(year="2026", external_id="200", rating=8.4),
        )
        ambiguous = sdk.media.rating("九门", media_type="tv")
        self.assertEqual(ambiguous["status"], "ambiguous")
        self.assertIsNone(ambiguous["data"]["rating"])
        web_search.assert_not_called()
        resolved = sdk.media.rating("九门", media_type="tv", year="2026", allow_web_fallback=False)
        self.assertEqual(resolved["data"]["rating"], 8.4)

    @patch("app.discovery.ratings.config.get_bool", return_value=True)
    @patch("app.discovery.ratings.get_discovery_search_service")
    @patch("app.discovery.ratings.get_discovery_service")
    def test_detail_does_not_replace_the_requested_identity_or_year(
        self, detail_factory, search_factory, _enabled
    ):
        search_factory.return_value.search.return_value = _search_result(_card(rating=None))
        for detail in (_card(year="2025"), _card(external_id="other"), _card(media_type="movie")):
            with self.subTest(detail=detail):
                detail_factory.return_value.get_detail.return_value = detail
                result = sdk.media.rating("九门", media_type="tv", year="2026", allow_web_fallback=False)
                self.assertFalse(result["ok"])
                self.assertIsNone(result["data"]["rating"])
                self.assertEqual(result["evidence"][-1]["status"], "identity_mismatch")

    def test_failed_web_search_keeps_source_status_instead_of_reporting_not_found(self):
        for structured in (False, True):
            for failure in ("timeout", "authentication", "budget_exhausted"):
                with self.subTest(structured=structured, failure=failure), patch(
                    "app.discovery.ratings.config.get_bool", return_value=structured
                ), patch("app.discovery.ratings.get_discovery_search_service") as source, patch(
                    "app.discovery.ratings.search_web", return_value=make_result(False, failure, "搜索未完成")
                ):
                    source.return_value.search.return_value = _search_result()
                    result = sdk.media.rating("九门", media_type="tv", year="2026")
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["error"], "rating_unavailable")
                self.assertEqual(result["evidence"][-1]["source"], "web_search")
                self.assertEqual(result["evidence"][-1]["status"], failure)
                self.assertFalse(result["evidence"][-1]["ok"])

    @patch("app.discovery.ratings.config.get_bool", return_value=False)
    @patch("app.discovery.ratings.search_web")
    @patch("app.discovery.ratings.FixedHostHttpClient")
    def test_page_fetch_failure_and_empty_page_remain_distinct(self, client_factory, web_search, _disabled):
        web_search.return_value = make_result(True, "ok", "网页结果", data={"results": [{
            "title": "九门 (豆瓣)", "url": "https://movie.douban.com/subject/12345678/",
            "snippet": "九门 电视剧 2026 剧情简介",
        }]})
        for code, expected, status in ((403, "unavailable", "authentication"), (429, "unavailable", "rate_limited"),
                                       (503, "unavailable", "unavailable"), (404, "not_found", "not_found")):
            with self.subTest(code=code):
                client = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(status_code=code)), aclose=AsyncMock())
                client_factory.return_value = client
                result = sdk.media.rating("九门", media_type="tv", year="2026")
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["evidence"][-1]["source"], "web_fetch")
                self.assertEqual(result["evidence"][-1]["status"], status)
                client.aclose.assert_awaited_once()
        web_search.return_value["data"]["results"].append({
            "title": "九门 (豆瓣)", "url": "https://movie.douban.com/subject/87654321/",
            "snippet": "九门 电视剧 剧情简介",
        })
        client_factory.return_value = SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(status_code=403)), aclose=AsyncMock()
        )
        result = sdk.media.rating("九门", media_type="tv")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(sum(item["source"] == "web_fetch" and item["ok"] is False for item in result["evidence"]), 2)

    @patch("app.discovery.ratings.config.get_bool", return_value=True)
    @patch("app.discovery.ratings.get_discovery_search_service")
    @patch("app.discovery.ratings.search_web")
    def test_successful_web_fallback_keeps_failed_structured_source_evidence(self, web_search, source, _enabled):
        source.return_value.search.return_value = _search_result(errors=({"code": "timeout"},), succeeded=())
        web_search.return_value = make_result(True, "ok", "网页结果", data={"results": [{
            "title": "九门 (豆瓣)", "url": "https://movie.douban.com/subject/12345678/",
            "snippet": "九门 电视剧 2026 豆瓣评分 7.7",
        }, {
            "title": "九门 (豆瓣)", "url": "https://movie.douban.com/subject/87654321/",
            "snippet": "九门 电视剧 2026 剧情简介",
        }]})
        with patch("app.discovery.ratings._fetch_douban_rating_async", new_callable=AsyncMock, side_effect=TimeoutError):
            result = sdk.media.rating("九门", media_type="tv", year="2026")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["rating"], 7.7)
        self.assertEqual(result["evidence"][0]["status"], "timeout")
        self.assertFalse(result["evidence"][0]["ok"])
        self.assertEqual(result["evidence"][1]["source"], "web_search")
        self.assertEqual(result["evidence"][1]["url"], "https://movie.douban.com/subject/12345678/")
        self.assertEqual(result["evidence"][2]["status"], "timeout")
        self.assertFalse(result["evidence"][2]["ok"])

    @patch("app.discovery.ratings.config.get_bool", return_value=False)
    @patch("app.discovery.ratings.search_web")
    def test_web_fallback_does_not_guess_between_same_title_subjects(self, web_search, _disabled):
        web_search.return_value = make_result(True, "ok", "网页结果", data={"results": [
            {"title": "九门 (豆瓣)", "url": f"https://movie.douban.com/subject/{identity}/",
             "snippet": f"九门 电视剧 {year} 豆瓣评分 {score}"}
            for identity, year, score in ((100, "2000", 7.4), (200, "2026", 8.4))
        ]})
        result = sdk.media.rating("九门", media_type="tv")
        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["data"]["rating"])

    def test_failure_evidence_only_describes_attempted_sources(self):
        for structured in (False, True):
            for fallback in (False, True):
                with self.subTest(structured=structured, fallback=fallback), patch(
                    "app.discovery.ratings.config.get_bool", return_value=structured
                ), patch("app.discovery.ratings.get_discovery_search_service") as source, patch(
                    "app.discovery.ratings.search_web", return_value=make_result(True, "empty", "网页结果为空", data={"results": []})
                ) as web:
                    source.return_value.search.return_value = _search_result(
                        errors=({"provider": "douban", "code": "unavailable"},), succeeded=()
                    )
                    result = sdk.media.rating(**{"query": "九门", "media_type": "tv", "allow_web_fallback": fallback})
                self.assertFalse(result["ok"])
                self.assertEqual(source.called, structured)
                self.assertEqual(web.called, fallback)
                self.assertEqual(result["data"]["web_fallback_used"], fallback)
                evidence = " ".join(item["description"] for item in result["evidence"])
                self.assertIn("网页补查未取得" if fallback else "未进行网页补查", result["summary"])
                self.assertIn("已尝试豆瓣结构化查询" if structured else "结构化查询未启用", evidence)
                self.assertIn("已尝试网页补查" if fallback else "未进行网页补查", evidence)

    @patch("app.discovery.ratings.config.get_bool", return_value=True)
    @patch("app.discovery.ratings.get_discovery_search_service")
    @patch("app.discovery.ratings.get_discovery_service")
    @patch("app.discovery.ratings.search_web")
    def test_structured_search_rating_wins_without_detail_or_web(
        self, web_search, detail_factory, search_factory, _enabled
    ):
        search_factory.return_value.search.return_value = _search_result(
            _card(rating=8.1)
        )
        result = sdk.media.rating(**
            {"query": "九门", "media_type": "tv", "year": "2026"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["rating"], 8.1)
        self.assertEqual(result["data"]["source_method"], "douban_search")
        self.assertIn("电视剧", result["summary"])
        detail_factory.assert_not_called()
        web_search.assert_not_called()

    @patch("app.discovery.ratings.config.get_bool", return_value=True)
    @patch("app.discovery.ratings.get_discovery_search_service")
    @patch("app.discovery.ratings.get_discovery_service")
    @patch("app.discovery.ratings.search_web")
    def test_detail_supplies_rating_when_search_card_has_none(
        self, web_search, detail_factory, search_factory, _enabled
    ):
        search_factory.return_value.search.return_value = _search_result(
            _card(rating=None)
        )
        detail_factory.return_value.get_detail.return_value = _card(rating=7.4)
        result = sdk.media.rating(**
            {"query": "九门", "media_type": "tv", "year": "2026"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["source_method"], "douban_detail")
        detail_factory.return_value.get_detail.assert_called_once_with(
            "douban", "tv", "12345678"
        )
        web_search.assert_not_called()

    @patch("app.discovery.ratings.config.get_bool", return_value=False)
    @patch("app.discovery.ratings.search_web")
    def test_controlled_web_fallback_accepts_only_verified_douban_subject(
        self, web_search, _disabled
    ):
        web_search.return_value = make_result(
            True,
            "success",
            "找到网页结果",
            data={
                "results": [
                    {
                        "title": "九门 (豆瓣)",
                        "url": "https://movie.douban.com/subject/12345678/",
                        "source": "movie.douban.com",
                        "snippet": "九门 电视剧 2026 豆瓣评分 7.7",
                    }
                ]
            },
        )
        result = sdk.media.rating(**
            {"query": "九门", "media_type": "tv", "year": "2026"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["rating"], 7.7)
        self.assertEqual(result["data"]["source_method"], "web_search")
        self.assertTrue(result["data"]["web_fallback_used"])
        self.assertEqual(result["evidence"][-1]["url"], "https://movie.douban.com/subject/12345678/")

    def test_controlled_fetch_extracts_rating_only_from_matching_douban_page(self):
        html = '\n        <html><head>\n          <meta property="og:title" content="九门 (豆瓣)">\n          <meta property="og:description" content="九门 电视剧 2026">\n        </head><body><strong class="ll rating_num" property="v:average">7.8</strong></body></html>\n        '
        self.assertEqual(
            _rating_from_douban_html(html, query="九门", year="2026", media_type="tv"),
            7.8,
        )
        self.assertIsNone(_rating_from_douban_html(html, query="老九门", year="2026"))
        self.assertIsNone(_rating_from_douban_html(html, query="九门", year="2025"))
        self.assertIsNone(
            _rating_from_douban_html(
                html.replace("电视剧", "电影"),
                query="九门",
                year="2026",
                media_type="tv",
            )
        )

    @patch("app.discovery.ratings.config.get_bool", return_value=False)
    @patch("app.discovery.ratings._fetch_douban_rating_async", new_callable=AsyncMock, return_value=7.9)
    @patch("app.discovery.ratings.search_web")
    def test_web_fallback_fetches_verified_subject_when_snippet_has_no_score(
        self, web_search, fetch_rating, _disabled
    ):
        web_search.return_value = make_result(
            True,
            "success",
            "网页结果",
            data={
                "results": [
                    {
                        "title": "九门 (豆瓣)",
                        "url": "https://movie.douban.com/subject/12345678/",
                        "source": "movie.douban.com",
                        "snippet": "九门 电视剧 2026 剧情简介",
                    }
                ]
            },
        )
        result = sdk.media.rating(query="九门", media_type="tv", year="2026")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["rating"], 7.9)
        self.assertEqual(result["data"]["source_method"], "web_fetch")
        fetch_rating.assert_awaited_once_with(
            "https://movie.douban.com/subject/12345678/",
            query="九门",
            year="2026",
            media_type="tv",
        )

    @patch("app.discovery.ratings.config.get_bool", return_value=False)
    @patch("app.discovery.ratings._fetch_douban_rating_async", new_callable=AsyncMock, return_value=None)
    @patch("app.discovery.ratings.search_web")
    def test_web_fallback_rejects_wrong_host_path_year_and_media_type(self, web_search, _fetch, _disabled):
        web_search.return_value = make_result(
            True,
            "success",
            "网页结果",
            data={
                "results": [
                    {
                        "title": "九门",
                        "url": "https://evil.example/subject/123/",
                        "snippet": "九门 电视剧 2026 豆瓣评分 9.9",
                    },
                    {
                        "title": "九门",
                        "url": "https://movie.douban.com/search?q=九门",
                        "snippet": "九门 电视剧 2026 豆瓣评分 9.8",
                    },
                    {
                        "title": "九门 (豆瓣)",
                        "url": "https://movie.douban.com/subject/321/",
                        "snippet": "九门 电影 2025 豆瓣评分 8.8",
                    },
                    {
                        "title": "老九门 (豆瓣)",
                        "url": "https://movie.douban.com/subject/654/",
                        "snippet": "老九门 电视剧 2026 豆瓣评分 9.9",
                    },
                ]
            },
        )
        self.assertFalse(sdk.media.rating(query="九门", media_type="tv", year="2026")["ok"])
        self.assertFalse(sdk.media.rating(query="九门", media_type="", year="2026")["ok"])

    @patch("app.discovery.ratings.config.get_bool", return_value=True)
    @patch("app.discovery.ratings.get_discovery_search_service")
    @patch("app.discovery.ratings.search_web")
    def test_successful_empty_provider_is_not_reported_as_unavailable(
        self, web_search, search_factory, _enabled
    ):
        search_factory.return_value.search.return_value = _search_result()
        result = sdk.media.rating(**
            {
                "query": "九门",
                "media_type": "tv",
                "year": "2026",
                "allow_web_fallback": False,
            }
        )
        self.assertEqual(result["status"], "not_found")
        self.assertFalse(result["data"]["web_fallback_used"])
        web_search.assert_not_called()

    @patch("app.discovery.ratings.config.get_bool", return_value=True)
    @patch("app.discovery.ratings.get_discovery_search_service")
    @patch("app.discovery.ratings.search_web")
    def test_total_provider_failure_is_unavailable_without_optional_fallback(
        self, web_search, search_factory, _enabled
    ):
        search_factory.return_value.search.return_value = _search_result(
            errors=({"provider": "douban", "code": "timeout"},), succeeded=()
        )
        result = sdk.media.rating(**
            {
                "query": "九门",
                "media_type": "tv",
                "year": "2026",
                "allow_web_fallback": False,
            }
        )
        self.assertEqual(result["status"], "unavailable")
        web_search.assert_not_called()

    @patch("app.discovery.ratings.config.get_bool", return_value=False)
    @patch("app.discovery.ratings.search_web")
    def test_unknown_media_type_requires_clarification_and_skips_web(
        self, web_search, _disabled
    ):
        result = sdk.media.rating(**{"query": "九门"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "not_found")
        self.assertIn("电影还是电视剧", result["summary"])
        self.assertNotIn("存在同名作品", result["summary"])
        web_search.assert_not_called()
