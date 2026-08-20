from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from datetime import datetime
from unittest.mock import Mock, patch

import requests

from app import config as app_config
from app.clients.douban_public import DoubanPublicClient, DoubanPublicPage
from app.clients.tmdb import TMDBClient
from app.discovery.models import (
    ProviderAuthenticationError,
    ProviderInvalidResponse,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.discovery.providers.bangumi import BangumiProvider
from app.discovery.providers.douban import DoubanProvider
from app.discovery.providers.tmdb import TMDBProvider
from app.discovery.registry import (
    list_filter_definitions,
    list_section_definitions,
    validate_filters,
    validate_request,
)
from app.modules.scraper import TMDBScraper


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.proxies = {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected GET {url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class CapturingDoubanPublicSession(requests.Session):
    def __init__(self, *payloads):
        super().__init__()
        self.payloads = list(payloads)
        self.prepared_requests = []

    def send(self, request, **kwargs):
        del kwargs
        self.prepared_requests.append(request)
        if not self.payloads:
            raise AssertionError(f"unexpected request {request.url}")
        response = requests.Response()
        response.status_code = 200
        response.headers = {"Content-Type": "application/json; charset=utf-8"}
        response.url = request.url
        response.request = request
        response._content = json.dumps(
            self.payloads.pop(0), ensure_ascii=False
        ).encode("utf-8")
        response._content_consumed = True
        return response


def tmdb_item(item_id=1, media_type="movie", **overrides):
    values = {
        "id": item_id,
        "media_type": media_type,
        "title": "测试电影" if media_type == "movie" else None,
        "name": "测试剧集" if media_type == "tv" else None,
        "original_title": "Test Movie" if media_type == "movie" else None,
        "original_name": "Test Series" if media_type == "tv" else None,
        "release_date": "2026-07-01" if media_type == "movie" else None,
        "first_air_date": "2026-06-01" if media_type == "tv" else None,
        "overview": "简介",
        "poster_path": "/poster.jpg",
        "backdrop_path": "/backdrop.jpg",
        "vote_average": 8.25,
    }
    values.update(overrides)
    return values


def douban_subject(subject_id="1292052", subject_type="movie", **overrides):
    values = {
        "id": subject_id,
        "type": subject_type,
        "title": "肖申克的救赎",
        "original_title": "The Shawshank Redemption",
        "card_subtitle": "1994 / 美国 / 剧情",
        "intro": "希望让人自由。",
        "rating": {"value": 9.7},
        "pic": {"large": "https://img1.doubanio.com/view/photo/l/public/p480747492.webp"},
        "release_date": "1994-09-10",
    }
    values.update(overrides)
    return values


def bangumi_subject(subject_id=1, **overrides):
    values = {
        "id": subject_id,
        "name": f"Subject {subject_id}",
        "name_cn": f"条目 {subject_id}",
        "summary": "简介",
        "date": "2026-07-01",
        "rating": {"score": 8.1},
        "images": {"large": f"https://lain.bgm.tv/pic/cover/l/{subject_id}.jpg"},
    }
    values.update(overrides)
    return values


class TMDBProviderTests(unittest.TestCase):
    def make_provider(self, payload):
        session = FakeSession(FakeResponse(payload))
        client = TMDBClient(
            api_key="test-key",
            base_url="https://tmdb.invalid/3",
            language="zh-CN",
            proxy_url="127.0.0.1:7890",
            timeout=(2, 6),
            session=session,
        )
        return TMDBProvider(client=client), session

    def test_client_logs_only_normalized_relative_path(self):
        session = FakeSession(FakeResponse({"results": [], "total_pages": 1}))
        client = TMDBClient(
            api_key="test-key",
            base_url="https://tmdb.invalid/3",
            proxy_url="127.0.0.1:7890",
            session=session,
        )

        with self.assertLogs("app.clients.tmdb", level="DEBUG") as captured:
            client.get(
                "/movie//popular/",
                {"query": "secret-query", "api_key": "attacker-key"},
            )

        message = "\n".join(captured.output)
        self.assertIn("path=/movie/popular", message)
        self.assertEqual(session.calls[0][0], "https://tmdb.invalid/3/movie/popular")
        for forbidden in (
            "https://tmdb.invalid", "secret-query", "test-key",
            "attacker-key", "127.0.0.1:7890",
        ):
            self.assertNotIn(forbidden, message)

    def test_client_applies_server_auth_language_proxy_and_finite_timeout(self):
        provider, session = self.make_provider({"results": [], "total_pages": 1})
        provider.list_items("popular", "movie", 1, {})

        url, kwargs = session.calls[0]
        self.assertEqual(url, "https://tmdb.invalid/3/movie/popular")
        self.assertEqual(kwargs["params"]["api_key"], "test-key")
        self.assertEqual(kwargs["params"]["language"], "zh-CN")
        self.assertEqual(kwargs["timeout"], (2, 6))
        self.assertEqual(session.proxies["https"], "http://127.0.0.1:7890")

    def test_weekly_trending_normalizes_mixed_media_and_skips_people(self):
        payload = {
            "page": 1,
            "total_pages": 2,
            "results": [
                tmdb_item(1, "movie"),
                tmdb_item(2, "tv"),
                {"id": 3, "media_type": "person", "name": "演员"},
            ],
        }
        provider, session = self.make_provider(payload)

        page = provider.list_items("trending", "all", 1, {})

        self.assertEqual(session.calls[0][0], "https://tmdb.invalid/3/trending/all/week")
        self.assertEqual([item.media_type for item in page.items], ["movie", "tv"])
        self.assertEqual(page.items[0].poster_key, "poster.jpg")
        self.assertEqual(page.items[1].tmdb_id, "2")
        self.assertTrue(page.has_more)

    def test_popular_movie_and_tv_use_distinct_paths_and_missing_dates_are_safe(self):
        session = FakeSession(
            FakeResponse({"results": [tmdb_item(1, "movie", release_date=None)], "total_pages": 1}),
            FakeResponse({"results": [tmdb_item(2, "tv", first_air_date="")], "total_pages": 1}),
        )
        client = TMDBClient(api_key="key", session=session)
        provider = TMDBProvider(client=client)

        movie_page = provider.list_items("popular", "movie", 1, {})
        tv_page = provider.list_items("popular", "tv", 1, {})

        self.assertTrue(session.calls[0][0].endswith("/movie/popular"))
        self.assertTrue(session.calls[1][0].endswith("/tv/popular"))
        self.assertEqual(movie_page.items[0].year, "")
        self.assertEqual(tv_page.items[0].release_date, "")

    def test_tv_title_falls_back_without_stringifying_none(self):
        provider, _ = self.make_provider({
            "results": [tmdb_item(7, "tv", name=None, title="Fallback TV")],
            "total_pages": 1,
        })

        page = provider.list_items("popular", "tv", 1, {})

        self.assertEqual(page.items[0].title, "Fallback TV")

    def test_discover_forwards_only_allowlisted_filters(self):
        provider, session = self.make_provider({"results": [], "total_pages": 3})

        page = provider.list_items(
            "discover",
            "movie",
            2,
            {
                "sort_by": "vote_average.desc",
                "with_genres": "16,35",
                "primary_release_year": "2026",
                "vote_average.gte": "7.5",
                "region": "CN",
                "language": "evil-language",
                "api_key": "attacker-key",
                "unknown": "drop-me",
            },
        )

        params = session.calls[0][1]["params"]
        self.assertEqual(params["page"], 2)
        self.assertEqual(params["sort_by"], "vote_average.desc")
        self.assertEqual(params["with_genres"], "16,35")
        self.assertEqual(params["primary_release_year"], "2026")
        self.assertEqual(params["vote_average.gte"], "7.5")
        self.assertEqual(params["region"], "CN")
        self.assertEqual(params["language"], "zh-CN")
        self.assertEqual(params["api_key"], "test-key")
        self.assertNotIn("unknown", params)
        self.assertTrue(page.has_more)

    def test_page_100_never_reports_more_even_when_tmdb_has_more_pages(self):
        provider, _ = self.make_provider({"results": [], "total_pages": 500})

        page = provider.list_items("popular", "movie", 100, {})

        self.assertFalse(page.has_more)

    def test_detail_returns_normalized_card_without_raw_poster_url(self):
        provider, _ = self.make_provider(tmdb_item(99, "movie"))

        card = provider.get_detail("99", "movie")

        self.assertEqual(card.external_id, "99")
        self.assertEqual(card.poster_key, "poster.jpg")
        self.assertNotIn("http", card.poster_key)

    def test_invalid_json_shape_is_structured(self):
        provider, _ = self.make_provider([])
        with self.assertRaises(ProviderInvalidResponse):
            provider.list_items("popular", "movie", 1, {})

    def test_http_401_maps_to_authentication_error(self):
        session = FakeSession(FakeResponse({}, status_code=401))
        provider = TMDBProvider(client=TMDBClient(api_key="key", session=session))

        with self.assertRaises(ProviderAuthenticationError):
            provider.list_items("popular", "movie", 1, {})

    def test_http_429_maps_to_rate_limit_with_retry_after(self):
        session = FakeSession(
            FakeResponse({}, status_code=429, headers={"Retry-After": "37"})
        )
        provider = TMDBProvider(client=TMDBClient(api_key="key", session=session))

        with self.assertRaises(ProviderRateLimited) as raised:
            provider.list_items("popular", "movie", 1, {})

        self.assertEqual(raised.exception.retry_after, 37)

    def test_http_5xx_maps_to_unavailable(self):
        session = FakeSession(FakeResponse({}, status_code=503))
        provider = TMDBProvider(client=TMDBClient(api_key="key", session=session))

        with self.assertRaises(ProviderUnavailable):
            provider.list_items("popular", "movie", 1, {})

    def test_timeout_is_structured(self):
        session = FakeSession(requests.Timeout("slow"), requests.Timeout("slow again"))
        provider = TMDBProvider(client=TMDBClient(api_key="key", session=session))
        with self.assertRaises(ProviderTimeout):
            provider.list_items("popular", "movie", 1, {})


class CredentialGuardConfig(Mapping):
    SENSITIVE_KEYS = {"DOUBAN_FRODO_API_KEY", "DOUBAN_FRODO_API_SECRET", "DOUBAN_DBCL2"}

    def __init__(self, values=None):
        self.values = dict(values or {})

    def __getitem__(self, key):
        if key in self.SENSITIVE_KEYS:
            raise AssertionError(f"credential key accessed: {key}")
        return self.values[key]

    def __iter__(self):
        return iter(self.values)

    def __len__(self):
        return len(self.values)


class CredentialTrackingConfig(Mapping):
    SENSITIVE_KEYS = {"DOUBAN_FRODO_API_KEY", "DOUBAN_FRODO_API_SECRET", "DOUBAN_DBCL2"}

    def __init__(self, values):
        self.values = dict(values)
        self.credential_accesses = []

    def __getitem__(self, key):
        if key in self.SENSITIVE_KEYS:
            self.credential_accesses.append(key)
        return self.values[key]

    def __iter__(self):
        return iter(self.values)

    def __len__(self):
        return len(self.values)


class DoubanProviderTests(unittest.TestCase):
    NOW = 1_700_000_000.0
    API_KEY = "configured-frodo-key"
    API_SECRET = "configured-frodo-secret"

    def setUp(self):
        get_patch = patch.object(
            app_config,
            "get",
            side_effect=lambda _key, default="": default,
        )
        bool_patch = patch.object(
            app_config,
            "get_bool",
            side_effect=lambda _key, default=False: default,
        )
        get_patch.start()
        bool_patch.start()
        self.addCleanup(get_patch.stop)
        self.addCleanup(bool_patch.stop)

    @staticmethod
    def normalized_item(subject_id="1292052", media_type="movie", **overrides):
        values = {
            "id": str(subject_id),
            "media_type": media_type,
            "title": "肖申克的救赎",
            "original_title": "The Shawshank Redemption",
            "year": "1994",
            "overview": "希望让人自由。",
            "poster_url": "https://img1.doubanio.com/view/photo/l/public/p480747492.webp",
            "rating": 9.7,
            "release_date": "1994-09-10",
            "is_new": False,
            "episodes_info": "",
        }
        values.update(overrides)
        return values

    def public_items(self, count=3, media_type="movie"):
        return tuple(
            self.normalized_item(str(1000 + index), media_type)
            for index in range(count)
        )

    def make_provider(
        self,
        public_client,
        *,
        frodo_factory=None,
        api_key=API_KEY,
        api_secret=API_SECRET,
        **kwargs,
    ):
        return DoubanProvider(
            enabled=True,
            api_key=api_key,
            api_secret=api_secret,
            public_client=public_client,
            frodo_client_factory=frodo_factory,
            clock=lambda: self.NOW,
            page_size=2,
            **kwargs,
        )

    def test_provider_defaults_enabled_and_explicit_zero_disables(self):
        self.assertTrue(DoubanProvider(config={}, public_client=Mock()).enabled)
        self.assertFalse(
            DoubanProvider(
                config={"DISCOVERY_DOUBAN_ENABLED": "0"},
                public_client=Mock(),
            ).enabled
        )
        self.assertFalse(DoubanProvider(enabled=0, public_client=Mock()).enabled)
        self.assertFalse(DoubanProvider(enabled="0", public_client=Mock()).enabled)


    def test_public_list_success_never_accesses_configured_frodo_keys(self):
        public = Mock()
        public.list_items.return_value = DoubanPublicPage(
            items=self.public_items(),
            source="public",
        )
        provider = DoubanProvider(
            config=CredentialGuardConfig({"DISCOVERY_DOUBAN_ENABLED": "1"}),
            public_client=public,
        )

        page = provider.list_items("movie_hot", "movie", 1, {})

        self.assertEqual(page.provider.message, "public")
        public.list_items.assert_called_once_with("movie_hot", "movie", 1, {})

    def test_public_detail_success_never_accesses_configured_frodo_keys(self):
        public = Mock()
        public.get_detail.return_value = self.normalized_item("42", "tv")
        provider = DoubanProvider(
            config=CredentialGuardConfig({"DISCOVERY_DOUBAN_ENABLED": "1"}),
            public_client=public,
        )

        card = provider.get_detail("42", "tv")

        self.assertEqual((card.external_id, card.media_type), ("42", "tv"))
        public.get_detail.assert_called_once_with("42", "tv")

    def test_zero_credential_config_still_allows_public_list_and_detail_success(self):
        public = Mock()
        public.list_items.return_value = DoubanPublicPage(items=self.public_items(), source="public")
        public.get_detail.return_value = self.normalized_item("7", "movie")
        provider = DoubanProvider(config={}, public_client=public)

        page = provider.list_items("movie_hot", "movie", 1, {})
        card = provider.get_detail("7", "movie")

        self.assertEqual(page.provider.message, "public")
        self.assertEqual(card.external_id, "7")

    def test_public_list_and_detail_are_first_and_map_unified_schema(self):
        public = Mock()
        public.list_items.return_value = DoubanPublicPage(
            items=(self.normalized_item(),),
            has_more=True,
            source="public",
        )
        public.get_detail.return_value = self.normalized_item(
            "35781362",
            "tv",
            title="漫长的季节",
            year="2023",
            release_date="2023-04-22",
        )
        frodo_factory = Mock(side_effect=AssertionError("Frodo must stay lazy"))
        provider = self.make_provider(public, frodo_factory=frodo_factory)

        page = provider.list_items("recommend", "movie", 2, {"sort": "recommend"})
        detail = provider.get_detail("35781362", "tv")

        public.list_items.assert_called_once_with(
            "recommend", "movie", 2, {"sort": "recommend"}
        )
        public.get_detail.assert_called_once_with("35781362", "tv")
        frodo_factory.assert_not_called()
        self.assertEqual((page.page, page.has_more), (2, True))
        self.assertEqual((page.provider.status, page.provider.message), ("healthy", "public"))
        card = page.items[0]
        self.assertEqual(card.external_id, "1292052")
        self.assertEqual(card.douban_id, "1292052")
        self.assertEqual(card.media_type, "movie")
        self.assertEqual(card.title, "肖申克的救赎")
        self.assertEqual(card.original_title, "The Shawshank Redemption")
        self.assertEqual(card.year, "1994")
        self.assertEqual(card.overview, "希望让人自由。")
        self.assertEqual(card.rating, 9.7)
        self.assertEqual(card.release_date, "1994-09-10")
        self.assertEqual(
            card.poster_key,
            "img1.doubanio.com/view/photo/l/public/p480747492.webp",
        )
        self.assertEqual((detail.media_type, detail.title, detail.year), ("tv", "漫长的季节", "2023"))
        self.assertTrue(detail.poster_key.startswith("img1.doubanio.com/"))

    def test_server_only_frodo_fallback_never_reads_config_credentials(self):
        config = CredentialTrackingConfig({
            "DISCOVERY_DOUBAN_ENABLED": "1",
            "DOUBAN_FRODO_API_KEY": "stale-web-key",
            "DOUBAN_FRODO_API_SECRET": "stale-web-secret",
        })
        public = Mock()
        public.list_items.side_effect = ProviderUnavailable("public unavailable")
        frodo = Mock()
        frodo.configured = True
        frodo.list_items.return_value = DoubanPublicPage(items=(), source="frodo")
        factory = Mock(return_value=frodo)

        provider = DoubanProvider(
            config=config,
            public_client=public,
            frodo_client_factory=factory,
        )
        page = provider.list_items("movie_hot", "movie", 1, {})

        self.assertEqual(config.credential_accesses, [])
        factory.assert_called_once_with()
        self.assertEqual(page.provider.message, "frodo-fallback")

    def test_page_one_public_results_below_three_fall_back_to_frodo(self):
        public = Mock()
        public.list_items.return_value = DoubanPublicPage(
            items=self.public_items(2),
            source="public-json",
        )
        frodo = Mock()
        frodo.configured = True
        frodo.list_items.return_value = DoubanPublicPage(
            items=(self.normalized_item("9001"),),
            source="frodo",
        )
        provider = self.make_provider(public, frodo_factory=Mock(return_value=frodo))

        page = provider.list_items("movie_hot", "movie", 1, {})

        self.assertEqual([item.external_id for item in page.items], ["9001"])
        self.assertEqual(page.provider.message, "frodo-fallback")

    def test_later_public_page_may_legitimately_return_fewer_than_three(self):
        public = Mock()
        public.list_items.return_value = DoubanPublicPage(
            items=(self.normalized_item("9002"),),
            source="public",
        )
        factory = Mock(side_effect=AssertionError("later page must not fall back"))
        provider = self.make_provider(public, frodo_factory=factory)

        page = provider.list_items("movie_hot", "movie", 2, {})

        self.assertEqual([item.external_id for item in page.items], ["9002"])
        self.assertEqual(page.provider.message, "public")
        factory.assert_not_called()

    def test_fallback_order_is_public_then_frodo_then_dbcl2(self):
        events = []
        public = Mock()
        public.list_items.side_effect = lambda *args: (
            events.append("public"),
            (_ for _ in ()).throw(ProviderUnavailable("public unavailable")),
        )[-1]
        frodo = Mock()
        frodo.configured = True
        frodo.list_items.side_effect = lambda *args: (
            events.append("frodo"),
            (_ for _ in ()).throw(ProviderUnavailable("frodo unavailable")),
        )[-1]
        authenticated = Mock()
        authenticated.configured = True
        authenticated.list_items.side_effect = lambda *args: (
            events.append("dbcl2"),
            DoubanPublicPage(items=(self.normalized_item("9003"),), source="authenticated"),
        )[-1]
        provider = self.make_provider(
            public,
            frodo_factory=Mock(side_effect=lambda: (events.append("construct-frodo"), frodo)[-1]),
            config={"DOUBAN_DBCL2": "123456789:test-dbcl2-value"},
            authenticated_client_factory=Mock(
                side_effect=lambda: (events.append("construct-dbcl2"), authenticated)[-1]
            ),
        )

        page = provider.list_items("movie_hot", "movie", 1, {})

        self.assertEqual(
            events,
            ["public", "construct-frodo", "frodo", "construct-dbcl2", "dbcl2"],
        )
        self.assertEqual(page.provider.message, "dbcl2-fallback")
        self.assertEqual(page.items[0].external_id, "9003")

    def test_frodo_success_does_not_read_or_construct_dbcl2_fallback(self):
        config = CredentialTrackingConfig({
            "DISCOVERY_DOUBAN_ENABLED": "1",
            "DOUBAN_DBCL2": "123456789:test-dbcl2-value",
        })
        public = Mock()
        public.list_items.side_effect = ProviderUnavailable("public unavailable")
        frodo = Mock()
        frodo.configured = True
        frodo.list_items.return_value = DoubanPublicPage(
            items=(self.normalized_item("9004"),), source="frodo"
        )
        authenticated_factory = Mock(side_effect=AssertionError("dbcl2 must stay lazy"))
        provider = DoubanProvider(
            config=config,
            public_client=public,
            frodo_client_factory=Mock(return_value=frodo),
            authenticated_client_factory=authenticated_factory,
        )

        page = provider.list_items("movie_hot", "movie", 1, {})

        self.assertEqual(page.provider.message, "frodo-fallback")
        self.assertEqual(config.credential_accesses, [])
        authenticated_factory.assert_not_called()

    def test_all_public_structured_failures_lazily_fallback_in_order(self):
        errors = (
            ProviderTimeout("public timeout"),
            ProviderRateLimited("public limited", retry_after=17),
            ProviderUnavailable("public unavailable"),
            ProviderInvalidResponse("public invalid response"),
        )
        for public_error in errors:
            with self.subTest(error=public_error.code):
                events = []
                public = Mock()
                public.list_items.side_effect = lambda *args, error=public_error: (
                    events.append("public"),
                    (_ for _ in ()).throw(error),
                )[-1]
                frodo = Mock()
                frodo.configured = True
                frodo.list_items.side_effect = lambda *args: (
                    events.append("frodo"),
                    DoubanPublicPage(
                        items=(self.normalized_item("2"),),
                        has_more=False,
                        source="frodo",
                    ),
                )[-1]
                factory = Mock(side_effect=lambda: (events.append("construct-frodo"), frodo)[-1])
                provider = self.make_provider(public, frodo_factory=factory)

                page = provider.list_items("movie_hot", "movie", 1, {})

                self.assertEqual(events, ["public", "construct-frodo", "frodo"])
                factory.assert_called_once_with()
                frodo.list_items.assert_called_once_with("movie_hot", "movie", 1, {})
                self.assertEqual(page.items[0].external_id, "2")
                self.assertEqual(
                    (page.provider.status, page.provider.message),
                    ("degraded", "frodo-fallback"),
                )

    def test_detail_falls_back_only_after_public_failure(self):
        events = []
        public = Mock()
        public.get_detail.side_effect = lambda *args: (
            events.append("public"),
            (_ for _ in ()).throw(ProviderUnavailable("public unavailable")),
        )[-1]
        frodo = Mock()
        frodo.configured = True
        frodo.get_detail.side_effect = lambda *args: (
            events.append("frodo"),
            self.normalized_item("42", "tv", title="回退剧集"),
        )[-1]
        factory = Mock(side_effect=lambda: (events.append("construct-frodo"), frodo)[-1])
        provider = self.make_provider(public, frodo_factory=factory)

        card = provider.get_detail("42", "tv")

        self.assertEqual(events, ["public", "construct-frodo", "frodo"])
        public.get_detail.assert_called_once_with("42", "tv")
        frodo.get_detail.assert_called_once_with("42", "tv")
        self.assertEqual((card.external_id, card.media_type, card.title), ("42", "tv", "回退剧集"))

    def test_missing_or_partial_credentials_reraise_original_public_error(self):
        credentials = (("", ""), (self.API_KEY, ""), ("", self.API_SECRET))
        for api_key, api_secret in credentials:
            for error_type in (
                ProviderTimeout,
                ProviderRateLimited,
                ProviderUnavailable,
                ProviderInvalidResponse,
            ):
                with self.subTest(
                    api_key=bool(api_key),
                    api_secret=bool(api_secret),
                    error=error_type.code,
                ):
                    public_error = error_type("public failed", retry_after=9)
                    public = Mock()
                    public.list_items.side_effect = public_error
                    factory = Mock(side_effect=AssertionError("incomplete fallback must stay lazy"))
                    provider = self.make_provider(
                        public,
                        frodo_factory=factory,
                        api_key=api_key,
                        api_secret=api_secret,
                    )

                    with self.assertRaises(error_type) as raised:
                        provider.list_items("movie_hot", "movie", 1, {})

                    self.assertIs(raised.exception, public_error)
                    factory.assert_not_called()

    def test_disabled_and_local_validation_never_touch_either_client(self):
        public = Mock()
        factory = Mock(side_effect=AssertionError("Frodo must stay lazy"))
        disabled = DoubanProvider(
            enabled=False,
            api_key=self.API_KEY,
            api_secret=self.API_SECRET,
            public_client=public,
            frodo_client_factory=factory,
        )
        with self.assertRaises(ProviderNotConfigured):
            disabled.list_items("movie_hot", "movie", 1, {})

        provider = self.make_provider(public, frodo_factory=factory)
        invalid_calls = (
            lambda: provider.list_items("unknown", "movie", 1, {}),
            lambda: provider.list_items("movie_hot", "tv", 1, {}),
            lambda: provider.list_items("movie_hot", "movie", 0, {}),
            lambda: provider.get_detail("not-an-id", "movie"),
            lambda: provider.get_detail("1", "all"),
        )
        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(ProviderInvalidResponse):
                    call()

        public.list_items.assert_not_called()
        public.get_detail.assert_not_called()
        factory.assert_not_called()

    def test_dual_failure_raises_detached_safe_error_without_secrets(self):
        public = Mock()
        public.list_items.side_effect = ProviderUnavailable("public unavailable")
        frodo = Mock()
        frodo.configured = True
        frodo.list_items.side_effect = ProviderUnavailable(
            f"fallback failed apiKey={self.API_KEY} secret={self.API_SECRET}",
            detail=f"url?_sig=signature&apiKey={self.API_KEY}&secret={self.API_SECRET}",
        )
        provider = self.make_provider(public, frodo_factory=Mock(return_value=frodo))

        with self.assertRaises(ProviderUnavailable) as raised:
            provider.list_items("movie_hot", "movie", 1, {})

        error = raised.exception
        serialized = " ".join((str(error), error.safe_message, error.detail, repr(error)))
        for secret in (self.API_KEY, self.API_SECRET, "signature"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(error.safe_message, "豆瓣可用数据源均不可用")
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_frodo_client_object_is_not_inspected_until_public_failure(self):
        public = Mock()
        public.list_items.return_value = DoubanPublicPage(items=self.public_items(), source="public")

        class LazyFrodo:
            @property
            def configured(self):
                raise AssertionError("configured read too early")

            def list_items(self, *args):
                raise AssertionError("Frodo called on public success")

        provider = DoubanProvider(
            enabled=True,
            public_client=public,
            frodo_client=LazyFrodo(),
        )

        page = provider.list_items("movie_hot", "movie", 1, {})

        self.assertEqual(page.provider.message, "public")

    def test_public_circuit_opens_after_three_failures_and_success_resets_it(self):
        now = [self.NOW]
        public = Mock()
        public.list_items.side_effect = [
            ProviderUnavailable("failure-1"),
            DoubanPublicPage(items=self.public_items(), source="public"),
            ProviderUnavailable("failure-2"),
            ProviderUnavailable("failure-3"),
            ProviderUnavailable("failure-4"),
        ]
        provider = DoubanProvider(
            enabled=True,
            api_key="",
            api_secret="",
            public_client=public,
            clock=lambda: now[0],
        )

        with self.assertRaises(ProviderUnavailable):
            provider.list_items("movie_hot", "movie", 1, {})
        provider.list_items("movie_hot", "movie", 1, {})
        for _ in range(3):
            with self.assertRaises(ProviderUnavailable):
                provider.list_items("movie_hot", "movie", 1, {})
        with self.assertRaises(ProviderUnavailable) as opened:
            provider.list_items("movie_hot", "movie", 1, {})

        self.assertEqual(public.list_items.call_count, 5)
        self.assertEqual(opened.exception.retry_after, 300)
        now[0] += 301
        public.list_items.side_effect = None
        public.list_items.return_value = DoubanPublicPage(items=self.public_items(), source="public")
        provider.list_items("movie_hot", "movie", 1, {})
        self.assertEqual(public.list_items.call_count, 6)

    def test_pre_network_invalid_pages_do_not_open_public_circuit_for_other_categories(self):
        session = CapturingDoubanPublicSession({
            "subjects": [
                {"id": "1", "title": "Healthy movie 1"},
                {"id": "2", "title": "Healthy movie 2"},
                {"id": "3", "title": "Healthy movie 3"},
            ],
        })
        public = DoubanPublicClient(session=session, min_interval=0)
        provider = DoubanProvider(
            enabled=True,
            api_key="",
            api_secret="",
            public_client=public,
            clock=lambda: self.NOW,
        )

        for _ in range(3):
            with self.assertRaises(ProviderInvalidResponse):
                provider.list_items("movie_showing", "movie", 2, {})

        page = provider.list_items("movie_hot", "movie", 1, {})

        self.assertEqual([item.external_id for item in page.items], ["1", "2", "3"])
        self.assertEqual(len(session.prepared_requests), 1)

    def test_upstream_invalid_payloads_still_open_public_circuit(self):
        session = CapturingDoubanPublicSession(
            {"subjects": "invalid"},
            {"subjects": "invalid"},
            {"subjects": "invalid"},
            {"subjects": [{"id": "1", "title": "must stay unrequested"}]},
        )
        public = DoubanPublicClient(session=session, min_interval=0)
        provider = DoubanProvider(
            enabled=True,
            api_key="",
            api_secret="",
            public_client=public,
            clock=lambda: self.NOW,
        )

        for _ in range(3):
            with self.assertRaises(ProviderInvalidResponse):
                provider.list_items("movie_hot", "movie", 1, {})
        with self.assertRaises(ProviderUnavailable) as opened:
            provider.list_items("movie_hot", "movie", 1, {})

        self.assertEqual(opened.exception.retry_after, 300)
        self.assertEqual(len(session.prepared_requests), 3)


class DoubanRegistryTests(unittest.TestCase):
    def test_public_weekly_titles_change_without_changing_category_keys(self):
        sections = {
            section["key"]: section
            for section in list_section_definitions(douban_enabled=True)
        }

        self.assertEqual(sections["douban-chinese-weekly"]["title"], "华语高分剧集")
        self.assertEqual(sections["douban-global-weekly"]["title"], "全球高分剧集")
        self.assertEqual(
            sections["douban-chinese-weekly"]["category"],
            "tv_chinese_weekly",
        )
        self.assertEqual(
            sections["douban-global-weekly"]["category"],
            "tv_global_weekly",
        )
        self.assertEqual(
            validate_request("douban", "tv_chinese_weekly", "tv"),
            ("douban", "tv_chinese_weekly", "tv"),
        )
        self.assertEqual(
            validate_request("douban", "tv_global_weekly", "tv"),
            ("douban", "tv_global_weekly", "tv"),
        )

    def test_registry_defaults_enabled_and_explicit_zero_disables_only_douban(self):
        enabled = list_section_definitions(douban_enabled=True)
        disabled = list_section_definitions(douban_enabled=0)

        self.assertTrue(all(section["enabled"] for section in enabled))
        self.assertTrue(
            all(
                not section["enabled"]
                for section in disabled
                if section["provider"] == "douban"
            )
        )
        self.assertTrue(
            all(
                section["enabled"]
                for section in disabled
                if section["provider"] != "douban"
            )
        )

    def test_douban_sort_values_use_public_endpoint_contract(self):
        for media_type in ("movie", "tv"):
            definitions = list_filter_definitions("douban", media_type)
            by_key = {item["key"]: item for item in definitions["filters"]}
            self.assertEqual(
                by_key["sort"]["options"],
                [
                    {"value": "recommend", "label": "热门推荐"},
                    {"value": "rank", "label": "评分优先"},
                    {"value": "time", "label": "时间优先"},
                ],
            )
            self.assertEqual(
                definitions["defaults"], {"sort": "recommend", "tags": ""}
            )

    def test_douban_tv_tags_use_search_subject_endpoint_tokens(self):
        definitions = list_filter_definitions("douban", "tv")
        by_key = {item["key"]: item for item in definitions["filters"]}

        self.assertEqual(
            by_key["tags"]["options"],
            [
                {"value": "国产剧", "label": "国产剧"},
                {"value": "美剧", "label": "美剧"},
                {"value": "英剧", "label": "英剧"},
                {"value": "日剧", "label": "日剧"},
                {"value": "韩剧", "label": "韩剧"},
            ],
        )
        self.assertEqual(
            validate_filters(
                "douban", "recommend", "tv", {"sort": "recommend", "tags": "韩剧"}
            ),
            {"sort": "recommend", "tags": "韩剧"},
        )
        with self.assertRaisesRegex(ValueError, "豆瓣标签无效"):
            validate_filters(
                "douban", "recommend", "tv", {"sort": "recommend", "tags": "中国大陆"}
            )

    def test_bangumi_weekdays_have_chinese_labels(self):
        definitions = list_filter_definitions("bangumi", "tv")
        weekday = definitions["filters"][0]
        self.assertEqual(weekday["label"], "放送星期")
        self.assertEqual(weekday["options"][0], {"value": "1", "label": "星期一"})
        self.assertEqual(weekday["options"][-1], {"value": "7", "label": "星期日"})


class BangumiProviderTests(unittest.TestCase):
    def calendar(self):
        return [
            {
                "weekday": {"id": weekday if weekday < 7 else 0, "en": "Day", "cn": f"星期{weekday}", "ja": ""},
                "items": [bangumi_subject(weekday * 10 + offset) for offset in range(1, 4)],
            }
            for weekday in range(1, 8)
        ]

    def make_provider(self, *responses, page_size=2, **provider_kwargs):
        session = FakeSession(*responses)
        provider = BangumiProvider(
            session=session,
            user_agent="MediaFlux-Test/1.0 (test@example.invalid)",
            timeout=(2, 6),
            page_size=page_size,
            clock=lambda: datetime(2026, 7, 25, 12, 0, 0),
            **provider_kwargs,
        )
        return provider, session

    def test_reads_bangumi_user_agent_from_runtime_config(self):
        provider = BangumiProvider(
            session=FakeSession(),
            config={"BANGUMI_USER_AGENT": "Configured-Agent/2.0"},
        )
        self.assertEqual(provider.user_agent, "Configured-Agent/2.0")

    def test_weekly_calendar_flattens_all_seven_groups_before_paging(self):
        provider, session = self.make_provider(FakeResponse(self.calendar()), page_size=5)

        page = provider.list_items("weekly", "tv", 2, {})

        self.assertTrue(session.calls[0][0].endswith("/calendar"))
        request_kwargs = session.calls[0][1]
        self.assertEqual(
            request_kwargs["headers"]["User-Agent"],
            "MediaFlux-Test/1.0 (test@example.invalid)",
        )
        self.assertEqual(request_kwargs["timeout"], (2, 6))
        self.assertEqual(len(page.items), 5)
        self.assertEqual([item.external_id for item in page.items], ["23", "31", "32", "33", "41"])
        self.assertTrue(page.has_more)
        self.assertTrue(all(item.poster_key.startswith("lain.bgm.tv/") for item in page.items))

    def test_weekday_filter_happens_before_exact_pagination(self):
        provider, _ = self.make_provider(FakeResponse(self.calendar()), page_size=2)

        page = provider.list_items("weekly", "tv", 2, {"weekday": 7})

        self.assertEqual([item.external_id for item in page.items], ["73"])
        self.assertFalse(page.has_more)
        self.assertEqual(page.items[0].weekday, 7)

    def test_weekday_switches_share_one_raw_calendar_request(self):
        provider, session = self.make_provider(FakeResponse(self.calendar()), page_size=20)

        monday = provider.list_items("calendar", "tv", 1, {"weekday": 1})
        tuesday = provider.list_items("calendar", "tv", 1, {"weekday": 2})

        self.assertEqual(len(session.calls), 1)
        self.assertTrue(all(item.weekday == 1 for item in monday.items))
        self.assertTrue(all(item.weekday == 2 for item in tuesday.items))

    def test_expired_calendar_retries_then_falls_back_to_stale_snapshot(self):
        monotonic = [0.0]
        provider, session = self.make_provider(
            FakeResponse(self.calendar()),
            requests.Timeout("first timeout"),
            requests.Timeout("second timeout"),
            page_size=20,
            calendar_ttl_seconds=300,
            calendar_clock=lambda: monotonic[0],
        )
        initial = provider.list_items("calendar", "tv", 1, {"weekday": 1})
        monotonic[0] = 301.0

        stale = provider.list_items("calendar", "tv", 1, {"weekday": 2})

        self.assertEqual(len(session.calls), 3)
        self.assertEqual(initial.provider.status, "healthy")
        self.assertEqual(stale.provider.status, "degraded")
        self.assertEqual(stale.provider.message, "使用缓存周历")
        self.assertTrue(all(item.weekday == 2 for item in stale.items))

        cached_stale = provider.list_items("calendar", "tv", 1, {"weekday": 3})
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(cached_stale.provider.status, "degraded")
        self.assertTrue(all(item.weekday == 3 for item in cached_stale.items))

    def test_cold_calendar_timeout_has_one_bounded_retry(self):
        provider, session = self.make_provider(
            requests.Timeout("first timeout"),
            requests.Timeout("second timeout"),
        )

        with self.assertRaises(ProviderTimeout):
            provider.list_items("calendar", "tv", 1, {"weekday": 1})

        self.assertEqual(len(session.calls), 2)

    def test_cross_week_page_has_no_extra_records(self):
        provider, _ = self.make_provider(FakeResponse(self.calendar()), page_size=20)

        page = provider.list_items("weekly", "tv", 2, {})

        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0].external_id, "73")
        self.assertFalse(page.has_more)

    def test_title_fallback_empty_images_and_today_filter(self):
        calendar = self.calendar()
        calendar[5]["items"] = [bangumi_subject(61, name_cn="", name="Fallback", images=None)]
        provider, _ = self.make_provider(FakeResponse(calendar), page_size=20)

        page = provider.list_items("today", "tv", 1, {})

        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0].title, "Fallback")
        self.assertEqual(page.items[0].poster_key, "")
        self.assertEqual(page.items[0].weekday, 6)

    def test_subject_detail_is_normalized(self):
        provider, session = self.make_provider(FakeResponse(bangumi_subject(42, name_cn="")))

        card = provider.get_detail("42", "tv")

        self.assertTrue(session.calls[0][0].endswith("/v0/subjects/42"))
        self.assertEqual(card.title, "Subject 42")
        self.assertEqual(card.bangumi_id, "42")
        self.assertNotIn("http", card.poster_key)

    def test_invalid_calendar_shape_is_structured(self):
        provider, _ = self.make_provider(FakeResponse({"items": []}))
        with self.assertRaises(ProviderInvalidResponse):
            provider.list_items("weekly", "tv", 1, {})


class TMDBScraperCompatibilityTests(unittest.TestCase):
    def test_search_and_get_detail_delegate_to_shared_client(self):
        client = Mock()
        client.api_key = "key"
        client.search.return_value = [{"id": 1}]
        client.detail.return_value = {"id": 1, "title": "Movie"}
        scraper = TMDBScraper(client=client)

        self.assertEqual(scraper.search("Movie", "2026", "movie"), [{"id": 1}])
        self.assertEqual(scraper.get_detail("1", "movie"), {"id": 1, "title": "Movie"})
        client.search.assert_called_once_with("Movie", "2026", "movie")
        client.detail.assert_called_once_with("1", "movie")

    def test_missing_key_and_client_errors_preserve_empty_fallbacks(self):
        no_key = Mock()
        no_key.api_key = ""
        scraper = TMDBScraper(client=no_key)
        self.assertEqual(scraper.search("Movie", "2026", "movie"), [])
        no_key.search.assert_not_called()

        broken = Mock()
        broken.api_key = "key"
        broken.search.side_effect = ProviderUnavailable("upstream unavailable")
        broken.detail.side_effect = ProviderUnavailable("upstream unavailable")
        scraper = TMDBScraper(client=broken)
        self.assertEqual(scraper.search("Movie", "", "movie"), [])
        self.assertEqual(scraper.get_detail("1", "movie"), {})


if __name__ == "__main__":
    unittest.main()
