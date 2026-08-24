from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.support import InitializedWebTestCase

from app import config as app_config
from app.discovery.models import MediaCard, ProviderUnavailable
from app.main import create_app


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self.headers = {"Content-Type": "application/json; charset=utf-8"}
        self._content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.text = self._content.decode("utf-8")

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)


class FakeSearchProvider:
    def __init__(self, name, cards=None, error=None, has_more=False):
        self.name = name
        self.cards = list(cards or [])
        self.error = error
        self.has_more = has_more
        self.calls = []

    def search(self, query, page):
        self.calls.append((query, page))
        if self.error:
            raise self.error
        return self.cards, self.has_more


class DiscoverySearchUnitTests(unittest.TestCase):
    def test_shutdown_defers_provider_close_until_inflight_search_finishes(self):
        from app.discovery.search import DiscoverySearchService

        started = threading.Event()
        release = threading.Event()

        class BlockingProvider(FakeSearchProvider):
            def __init__(self):
                super().__init__("tmdb")
                self.closed = False

            def search(self, query, page):
                started.set()
                release.wait(timeout=2)
                return [], False

            def close(self):
                self.closed = True

        provider = BlockingProvider()
        service = DiscoverySearchService(providers={"tmdb": provider})
        result_holder = []
        thread = threading.Thread(
            target=lambda: result_holder.append(
                service.search("测试", 1, ["tmdb"], timeout_seconds=0.01)
            )
        )
        thread.start()
        self.assertTrue(started.wait(timeout=1))
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_holder[0].errors[0]["code"], "timeout")
        service.shutdown()
        self.assertFalse(provider.closed)

        release.set()
        deadline = time.monotonic() + 1
        while not provider.closed and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(provider.closed)

    def test_partial_submit_failure_keeps_started_future_in_lifecycle_ledger(self):
        from app.discovery.search import DiscoverySearchService

        started = threading.Event()
        release = threading.Event()

        class BlockingProvider(FakeSearchProvider):
            def __init__(self, name):
                super().__init__(name)
                self.closed = False

            def search(self, query, page):
                started.set()
                release.wait(timeout=2)
                return [], False

            def close(self):
                self.closed = True

        class PartialExecutor:
            def __init__(self):
                self.delegate = ThreadPoolExecutor(max_workers=1)
                self.calls = 0

            def submit(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("submit failed")
                return self.delegate.submit(*args, **kwargs)

        first = BlockingProvider("tmdb")
        second = BlockingProvider("douban")
        executor = PartialExecutor()
        service = DiscoverySearchService(
            providers={"tmdb": first, "douban": second}, executor=executor,
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "submit failed"):
                service.search("测试", 1, ["tmdb", "douban"])
            self.assertTrue(started.wait(timeout=1))
            service.shutdown()
            self.assertFalse(first.closed)
            self.assertFalse(second.closed)
            release.set()
            deadline = time.monotonic() + 1
            while not first.closed and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(first.closed)
            self.assertTrue(second.closed)
        finally:
            release.set()
            executor.delegate.shutdown(wait=True)

    def test_provider_close_failure_does_not_block_remaining_providers(self):
        from app.discovery.search import DiscoverySearchService

        class Provider(FakeSearchProvider):
            def __init__(self, name, fail=False):
                super().__init__(name)
                self.fail = fail
                self.closed = False

            def close(self):
                self.closed = True
                if self.fail:
                    raise RuntimeError("close failed")

        first = Provider("tmdb", fail=True)
        second = Provider("douban")
        service = DiscoverySearchService(providers={"tmdb": first, "douban": second})
        service.shutdown()
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)

    def test_tmdb_search_maps_movie_and_tv_and_ignores_people(self):
        from app.discovery.search import TMDBSearchProvider

        class Client:
            def get(self, path, params):
                self.call = (path, params)
                return {
                    "page": 2,
                    "total_pages": 4,
                    "results": [
                        {"id": 1, "media_type": "movie", "title": "电影", "release_date": "2024-01-01", "poster_path": "/m.jpg"},
                        {"id": 2, "media_type": "tv", "name": "剧集", "first_air_date": "2023-02-03", "poster_path": "/t.jpg"},
                        {"id": 3, "media_type": "person", "name": "演员"},
                    ],
                }

        client = Client()
        cards, has_more = TMDBSearchProvider(client=client).search("测试", 2)
        self.assertEqual(client.call, ("/search/multi", {"query": "测试", "page": 2, "include_adult": "false"}))
        self.assertEqual([(card.provider, card.media_type, card.title) for card in cards], [
            ("tmdb", "movie", "电影"), ("tmdb", "tv", "剧集")
        ])
        self.assertTrue(has_more)

    def test_douban_search_uses_fixed_suggestion_endpoint(self):
        from app.discovery.search import DoubanSearchProvider

        session = FakeSession([FakeResponse([
            {"id": "1292052", "title": "肖申克的救赎", "type": "movie", "year": "1994", "img": "https://img1.doubanio.com/view/photo/s_ratio_poster/public/p1.jpg"},
        ])])
        cards, has_more = DoubanSearchProvider(session=session).search("肖申克", 1)
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ("GET", "https://movie.douban.com/j/subject_suggest"))
        self.assertEqual(kwargs["params"], {"q": "肖申克"})
        self.assertEqual(cards[0].stable_id, "douban:movie:1292052")
        self.assertEqual(cards[0].poster_key, "img1.doubanio.com/view/photo/s_ratio_poster/public/p1.jpg")
        self.assertFalse(has_more)

    def test_douban_search_uses_configured_dbcl2_cookie(self):
        from app.discovery.search import DoubanSearchProvider

        session = FakeSession([FakeResponse([])])
        with patch(
            "app.discovery.search.app_config.get",
            side_effect=lambda key, default="": (
                'bid=ignored; dbcl2="123456789:test-value"; ck=ignored'
                if key == "DOUBAN_DBCL2" else default
            ),
        ):
            DoubanSearchProvider(session=session).search("测试", 1)

        _method, _url, kwargs = session.calls[0]
        self.assertEqual(kwargs["headers"]["Cookie"], "dbcl2=123456789:test-value")

    def test_bangumi_search_posts_bounded_query(self):
        from app.discovery.search import BangumiSearchProvider

        session = FakeSession([FakeResponse({
            "total": 40,
            "data": [{"id": 12, "name": "Test", "name_cn": "测试动画", "date": "2025-01-01", "images": {"large": "https://lain.bgm.tv/pic/cover/l/test.jpg"}}],
        })])
        cards, has_more = BangumiSearchProvider(session=session, user_agent="MediaFlux/Test").search("测试", 2)
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ("POST", "https://api.bgm.tv/v0/search/subjects"))
        self.assertEqual(kwargs["params"], {"limit": 20, "offset": 20})
        self.assertEqual(kwargs["json"], {"keyword": "测试", "sort": "match", "filter": {"type": [2]}})
        self.assertEqual(cards[0].stable_id, "bangumi:tv:12")
        self.assertTrue(has_more)

    def test_service_returns_partial_success_and_deduplicates(self):
        from app.discovery.search import DiscoverySearchService

        card = MediaCard(provider="tmdb", external_id="1", media_type="movie", title="电影")
        providers = {
            "tmdb": FakeSearchProvider("tmdb", [card, card], has_more=True),
            "douban": FakeSearchProvider("douban", error=ProviderUnavailable("internal detail must not leak")),
        }
        result = DiscoverySearchService(providers=providers).search("电影", 1, ["tmdb", "douban"])
        self.assertEqual(result.items, (card,))
        self.assertEqual(result.providers_attempted, ("tmdb", "douban"))
        self.assertEqual(result.providers_succeeded, ("tmdb",))
        self.assertEqual(result.errors[0]["provider"], "douban")
        self.assertNotIn("internal detail", result.errors[0]["message"])
        self.assertTrue(result.has_more)

    def test_service_validates_query_page_and_provider_names(self):
        from app.discovery.search import DiscoverySearchService

        service = DiscoverySearchService(providers={"tmdb": FakeSearchProvider("tmdb")})
        for query in ("", " ", "x" * 121, "bad\nquery"):
            with self.subTest(query=query):
                with self.assertRaises(ValueError):
                    service.search(query, 1, None)
        with self.assertRaises(ValueError):
            service.search("movie", 101, None)
        with self.assertRaises(ValueError):
            service.search("movie", 1, ["unknown"])

    def test_service_returns_after_total_timeout_and_marks_slow_provider(self):
        import time
        from app.discovery.search import DiscoverySearchService

        class SlowProvider(FakeSearchProvider):
            def search(self, query, page):
                time.sleep(0.2)
                return super().search(query, page)

        service = DiscoverySearchService(
            providers={"tmdb": SlowProvider("tmdb")},
        )
        started = time.monotonic()
        result = service.search("测试", 1, ["tmdb"], timeout_seconds=0.05)
        elapsed = time.monotonic() - started
        service.shutdown()

        self.assertLess(elapsed, 0.15)
        self.assertEqual(result.providers_succeeded, ())
        self.assertEqual(result.errors[0]["code"], "timeout")

    def test_disabled_douban_is_never_called_and_cannot_be_selected(self):
        from app.discovery.search import DiscoverySearchService

        douban = FakeSearchProvider("douban", [
            MediaCard(provider="douban", external_id="1", media_type="movie", title="不应调用")
        ])
        tmdb = FakeSearchProvider("tmdb", [])
        service = DiscoverySearchService(providers={"tmdb": tmdb, "douban": douban})
        try:
            with patch("app.discovery.search.app_config.get_bool", return_value=False):
                result = service.search("电影", 1, None)
                self.assertEqual(result.providers_attempted, ("tmdb",))
                self.assertEqual(douban.calls, [])
                with self.assertRaisesRegex(ValueError, "已关闭"):
                    service.search("电影", 1, ["douban"])
        finally:
            service.shutdown()

    def test_global_service_is_reused_and_shutdown_closes_provider_sessions(self):
        from app.discovery.search import get_discovery_search_service, shutdown_discovery_search_service

        shutdown_discovery_search_service()
        first = get_discovery_search_service()
        second = get_discovery_search_service()
        self.assertIs(first, second)
        sessions = [
            getattr(provider, "session", None)
            or getattr(getattr(provider, "client", None), "session", None)
            for provider in first.providers.values()
        ]
        with patch.object(first._executor, "shutdown", wraps=first._executor.shutdown) as shutdown:
            shutdown_discovery_search_service()
            shutdown.assert_called_once()
        self.assertTrue(all(getattr(session, "close", None) for session in sessions if session is not None))
        replacement = get_discovery_search_service()
        self.assertIsNot(replacement, first)
        shutdown_discovery_search_service()

    def test_query_rejects_unicode_format_controls(self):
        from app.discovery.search import DiscoverySearchService

        service = DiscoverySearchService(providers={"tmdb": FakeSearchProvider("tmdb")})
        try:
            for query in ("电影\u200b名称", "电影\u202e名称", "电影\u0085名称"):
                with self.subTest(query=query):
                    with self.assertRaises(ValueError):
                        service.search(query, 1, None)
        finally:
            service.shutdown()

    def test_tmdb_client_does_not_follow_redirects(self):
        from app.clients.tmdb import TMDBClient
        from app.discovery.models import ProviderUnavailable

        session = FakeSession([FakeResponse({}, status_code=302)])
        client = TMDBClient(api_key="key", session=session, retries=0)
        with self.assertRaises(ProviderUnavailable):
            client.get("/search/movie", {"query": "电影"})
        self.assertIs(session.calls[0][2]["allow_redirects"], False)


class FakeSearchService:
    def search(self, query, page, providers):
        from app.discovery.search import DiscoverySearchResult
        return DiscoverySearchResult(
            query=query,
            page=page,
            items=(MediaCard(provider="tmdb", external_id="550", media_type="movie", title="Fight Club", poster_key="poster.jpg"),),
            has_more=True,
            providers_attempted=tuple(providers or ("tmdb", "douban", "bangumi")),
            providers_succeeded=("tmdb",),
            errors=({"provider": "douban", "code": "unavailable", "message": "数据源暂不可用", "retry_after": 0},),
        )


class DiscoverySearchAPITests(InitializedWebTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env_patch = patch.object(app_config, "ENV_FILE", Path(self.temp.name) / "user.env")
        self.env_patch.start()
        self.cache_patch = patch.object(app_config, "_cache", {})
        self.cache_patch.start()
        self.os_patch = patch.dict(
            os.environ,
            {
                "MEDIAFLUX_INITIALIZED": "1",
                "DISCOVERY_ENABLED": "1",
                "WEB_SECRET_KEY": "search-test-secret",
                "ENV_WEB_PASSPORT": "admin",
                "ENV_WEB_PASSWORD": "123456",
            },
            clear=False,
        )
        self.os_patch.start()
        self.client = TestClient(create_app(), raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        self.os_patch.stop()
        self.cache_patch.stop()
        self.env_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def _csrf(response):
        import re
        match = re.search(r'name="csrf-token" content="([^"]+)"', response.text)
        if not match:
            match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        if not match:
            raise AssertionError("missing csrf token")
        return match.group(1)

    def authenticate(self):
        login = self.client.get("/login")
        token = self._csrf(login)
        response = self.client.post(
            "/login",
            data={"csrf_token": token, "username": "admin", "password": "123456"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

    def test_search_api_requires_login_and_returns_signed_poster(self):
        self.assertEqual(self.client.get("/api/discovery/search?q=Fight").status_code, 401)
        self.authenticate()
        with patch("app.routes.discovery_api.get_discovery_search_service", return_value=FakeSearchService()):
            response = self.client.get("/api/discovery/search?q=Fight&page=2&providers=tmdb,douban")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["query"], "Fight")
        self.assertEqual(payload["page"], 2)
        self.assertTrue(payload["has_more"])
        self.assertEqual(payload["items"][0]["stable_id"], "tmdb:movie:550")
        self.assertTrue(payload["items"][0]["poster_url"].startswith("/discovery-poster/tmdb/"))
        self.assertNotIn("poster_key", payload["items"][0])
        self.assertEqual(payload["errors"][0]["code"], "unavailable")

    def test_search_api_rejects_oversized_provider_parameter(self):
        self.authenticate()
        response = self.client.get("/api/discovery/search", params={"q": "Fight", "providers": "tmdb," * 40})
        self.assertEqual(response.status_code, 400)

    def test_search_api_rejects_unknown_query_parameters(self):
        self.authenticate()
        response = self.client.get("/api/discovery/search?q=Fight&debug=1")
        self.assertEqual(response.status_code, 400)
        self.assertIn("未知查询参数", response.json()["error"])


if __name__ == "__main__":
    unittest.main()
