from __future__ import annotations

import os
import re
import tempfile
import unittest
from collections.abc import Mapping

import requests
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from tests.support import InitializedWebTestCase

from app import config as app_config
from app import database
from app.clients.douban_public import DoubanPublicPage
from app.discovery.cache import DiscoveryCache
from app.discovery.models import DiscoveryPage, MediaCard, ProviderHealth, ProviderUnavailable
from app.discovery.providers.douban import DoubanProvider
from app.discovery.registry import ProviderRegistry
from app.discovery.service import DiscoveryService
from app.main import create_app


class FakeDiscoveryService:
    def list_sections(self):
        return [{"key": "tmdb-popular", "title": "热门电影", "provider": "tmdb", "category": "popular", "media_type": "movie", "enabled": True}]

    def list_filters(self, provider, media_type):
        if provider == "bad":
            raise ValueError("不支持的数据源")
        return {
            "filters": [{
                "key": "with_genres",
                "label": "类型",
                "all_label": "全部类型",
                "options": [{"value": "16", "label": "动画"}],
            }],
            "defaults": {"with_genres": ""},
        }

    def list_items(self, provider, category, media_type, page, filters):
        if provider == "bad":
            raise ValueError("不支持的数据源")
        if category == "down":
            raise ProviderUnavailable("数据源暂不可用")
        return DiscoveryPage(
            items=[MediaCard(provider="tmdb", external_id="550", media_type="movie", title="Fight Club", poster_key="abc.jpg")],
            page=page,
            has_more=False,
            cached=True,
            provider=ProviderHealth(name=provider),
        )

    def get_detail(self, provider, media_type, external_id):
        return MediaCard(provider=provider, external_id=external_id, media_type=media_type, title="Detail", poster_key="poster.jpg")

    def map_to_tmdb(self, *args, **kwargs):
        return {"tmdb_id": "550", "confirmed": False, "candidates": []}

    async def map_to_tmdb_async(self, *args, **kwargs):
        return self.map_to_tmdb(*args, **kwargs)

    def list_watchlist(self):
        return [{
            "provider": "douban", "external_id": "7", "media_type": "movie",
            "title": "Movie", "year": "1999",
            "poster_key": "img3.doubanio.com/view/photo/s_ratio_poster/public/p7.jpg",
        }]

    def add_watchlist(self, card):
        return None

    def remove_watchlist(self, provider, media_type, external_id):
        return True


class CredentialReadForbiddenConfig(Mapping):
    """允许读取普通开关，但任何 Frodo 凭据读取都会使回归测试失败。"""

    _FORBIDDEN_KEYS = {"DOUBAN_FRODO_API_KEY", "DOUBAN_FRODO_API_SECRET"}

    def __init__(self, values=None):
        self._values = dict(values or {})

    def __getitem__(self, key):
        if key in self._FORBIDDEN_KEYS:
            raise AssertionError(f"credential key accessed: {key}")
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


class PublicDoubanClientFixture:
    POSTER_URL = "https://img1.doubanio.com/view/photo/l/public/p480747492.webp"

    @staticmethod
    def _item(external_id="1292052", media_type="movie"):
        return {
            "id": external_id,
            "media_type": media_type,
            "title": "肖申克的救赎" if media_type == "movie" else "测试剧集",
            "original_title": "The Shawshank Redemption",
            "year": "1994",
            "overview": "希望让人自由。",
            "poster_url": PublicDoubanClientFixture.POSTER_URL,
            "rating": 9.7,
            "release_date": "1994-09-10",
        }

    def __init__(self):
        self.list_calls = []
        self.detail_calls = []

    def list_items(self, category, media_type, page, filters):
        self.list_calls.append((category, media_type, page, filters))
        return DoubanPublicPage(items=(self._item(),), source="public")

    def get_detail(self, external_id, media_type):
        self.detail_calls.append((external_id, media_type))
        return self._item(external_id, media_type)


class _BaseClientTests(InitializedWebTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch("app.database.DB_PATH", Path(self.temp.name) / "api.db")
        self.db_patch.start()
        self.env_patch = patch.dict(os.environ, {
            "MEDIAFLUX_INITIALIZED": "1",
            "WEB_SECRET_KEY": "test-secret",
            "ENV_WEB_PASSPORT": "admin",
            "ENV_WEB_PASSWORD": "123456",
            "DISCOVERY_ENABLED": "1",
        })
        self.env_patch.start()
        self.client = TestClient(create_app(), raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        self.env_patch.stop()
        self.db_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def _csrf(response) -> str:
        match = re.search(r'name="csrf-token" content="([^"]+)"', response.text)
        if not match:
            match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        if not match:
            raise AssertionError("missing csrf token")
        return match.group(1)

    def authenticate(self, client=None):
        client = client or self.client
        login = client.get("/login")
        token = self._csrf(login)
        response = client.post(
            "/login",
            data={"csrf_token": token, "username": "admin", "password": "123456"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        page = client.get("/settings")
        return {"X-CSRF-Token": self._csrf(page)}


class DiscoveryAPITests(_BaseClientTests):
    def test_api_requires_login(self):
        self.assertEqual(self.client.get("/api/discovery/sections").status_code, 401)

    def test_sections_items_filters_and_detail_use_safe_contract(self):
        self.authenticate()
        service = FakeDiscoveryService()
        with patch("app.routes.discovery_api.get_discovery_service", return_value=service):
            sections = self.client.get("/api/discovery/sections")
            items = self.client.get("/api/discovery/items?provider=tmdb&category=popular&media_type=movie&page=1")
            filters = self.client.get("/api/discovery/filters/tmdb/movie")
            detail = self.client.get("/api/discovery/detail/tmdb/movie/550")
        self.assertEqual(sections.status_code, 200)
        self.assertEqual(items.status_code, 200)
        payload = items.json()
        self.assertEqual(payload["items"][0]["stable_id"], "tmdb:movie:550")
        poster_url = payload["items"][0]["poster_url"]
        self.assertTrue(poster_url.startswith("/discovery-poster/tmdb/"))
        self.assertNotIn("abc.jpg", poster_url)
        self.assertNotIn("poster_key", payload["items"][0])
        self.assertNotIn("api_key", items.text.lower())
        self.assertEqual(filters.json()["filters"][0]["label"], "类型")
        self.assertEqual(
            filters.json()["filters"][0]["options"],
            [{"value": "16", "label": "动画"}],
        )
        self.assertEqual(detail.json()["title"], "Detail")

    def test_authenticated_douban_api_uses_public_client_without_reading_frodo_credentials(self):
        empty_user_env = Path(self.temp.name) / "empty-user.env"
        empty_user_env.write_text("", encoding="utf-8")

        with patch.object(app_config, "ENV_FILE", empty_user_env), patch.object(
            app_config, "_cache", None
        ), patch.dict(os.environ, {
            "MEDIAFLUX_INITIALIZED": "1",
            "DISCOVERY_ENABLED": "1",
            "WEB_SECRET_KEY": "task5-api-signing-secret",
            "ENV_WEB_PASSPORT": "admin",
            "ENV_WEB_PASSWORD": "123456",
        }, clear=True):
            self.assertEqual(app_config.all_items(), {})
            self.assertEqual(
                app_config.get("WEB_SECRET_KEY"),
                "task5-api-signing-secret",
            )
            self.assertEqual(
                set(os.environ),
                {
                    "MEDIAFLUX_INITIALIZED", "DISCOVERY_ENABLED", "WEB_SECRET_KEY",
                    "ENV_WEB_PASSPORT", "ENV_WEB_PASSWORD",
                },
            )
            self.assertNotIn("DOUBAN_FRODO_API_KEY", os.environ)
            self.assertNotIn("DOUBAN_FRODO_API_SECRET", os.environ)
            client = TestClient(create_app(), raise_server_exceptions=False)
            self.authenticate(client)
            database.init_db()
            public_client = PublicDoubanClientFixture()
            provider = DoubanProvider(
                config=CredentialReadForbiddenConfig(),
                public_client=public_client,
            )
            service = DiscoveryService(
                registry=ProviderRegistry({"douban": provider}),
                cache=DiscoveryCache(),
                refresh_submit=lambda callback: None,
            )

            try:
                with patch(
                    "app.routes.discovery_api.get_discovery_service",
                    return_value=service,
                ):
                    items = client.get(
                        "/api/discovery/items?provider=douban&category=movie_hot&media_type=movie&page=1"
                    )
                    detail = client.get("/api/discovery/detail/douban/movie/1292052")
            finally:
                service.shutdown()
                client.close()

            self.assertEqual(empty_user_env.read_text(encoding="utf-8"), "")
            self.assertEqual(
                set(os.environ),
                {
                    "MEDIAFLUX_INITIALIZED", "DISCOVERY_ENABLED", "WEB_SECRET_KEY",
                    "ENV_WEB_PASSPORT", "ENV_WEB_PASSWORD",
                },
            )

        self.assertEqual(items.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(public_client.list_calls, [("movie_hot", "movie", 1, {})])
        self.assertEqual(public_client.detail_calls, [("1292052", "movie")])

        items_payload = items.json()
        detail_payload = detail.json()
        self.assertEqual(items_payload["provider"]["message"], "public")
        self.assertEqual(items_payload["items"][0]["stable_id"], "douban:movie:1292052")
        self.assertEqual(detail_payload["stable_id"], "douban:movie:1292052")

        for poster_url in (
            items_payload["items"][0]["poster_url"],
            detail_payload["poster_url"],
        ):
            self.assertTrue(poster_url.startswith("/discovery-poster/douban/"))
            self.assertNotIn(PublicDoubanClientFixture.POSTER_URL, poster_url)
            self.assertNotIn("doubanio.com", poster_url)
            self.assertNotIn("frodo", poster_url.lower())
            self.assertNotIn("apiKey", poster_url)
            self.assertNotIn("_sig", poster_url)

        combined_response = items.text + detail.text
        for forbidden in (
            PublicDoubanClientFixture.POSTER_URL,
            "movie.douban.com",
            "frodo.douban.com",
            "DOUBAN_FRODO_API_KEY",
            "DOUBAN_FRODO_API_SECRET",
            "apiKey",
            "_sig",
        ):
            self.assertNotIn(forbidden, combined_response)

    def test_disabled_discovery_hides_navigation_and_blocks_all_entry_points(self):
        self.authenticate()
        from app.routes.discovery_image import encode_poster_token

        token = encode_poster_token("tmdb", "abc.jpg")
        with patch.dict(os.environ, {"DISCOVERY_ENABLED": "0"}), patch(
            "app.routes.discovery_api.get_discovery_service"
        ) as get_service, patch("app.routes.discovery_image._get_poster_session") as request_get:
            settings = self.client.get("/settings")
            page = self.client.get("/discovery")
            api = self.client.get("/api/discovery/sections")
            poster = self.client.get(f"/discovery-poster/tmdb/{token}")

        self.assertNotIn('href="/discovery"', settings.text)
        self.assertEqual(page.status_code, 404)
        self.assertEqual(api.status_code, 404)
        self.assertEqual(poster.status_code, 404)
        get_service.assert_not_called()
        request_get.assert_not_called()

    def test_identity_rejects_invalid_provider_media_pairs_and_nonnumeric_ids(self):
        self.authenticate()
        service = Mock()
        service.get_detail.side_effect = lambda provider, media_type, external_id: MediaCard(
            provider=provider,
            external_id=external_id,
            media_type=media_type,
            title="Detail",
        )
        invalid_paths = (
            "/api/discovery/detail/bangumi/movie/1",
            "/api/discovery/detail/tmdb/movie/not-numeric",
            "/api/discovery/detail/bangumi/tv/not-numeric",
        )
        with patch("app.routes.discovery_api.get_discovery_service", return_value=service):
            for path in invalid_paths:
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path).status_code, 400)
            douban = self.client.get("/api/discovery/detail/douban/tv/subject:abc-1")
            tmdb = self.client.get("/api/discovery/detail/tmdb/movie/550")
            bangumi = self.client.get("/api/discovery/detail/bangumi/tv/42")

        self.assertEqual(douban.status_code, 200)
        self.assertEqual(tmdb.status_code, 200)
        self.assertEqual(bangumi.status_code, 200)
        self.assertEqual(service.get_detail.call_count, 3)

    def test_invalid_params_and_provider_errors_are_structured(self):
        self.authenticate()
        service = FakeDiscoveryService()
        with patch("app.routes.discovery_api.get_discovery_service", return_value=service):
            invalid_page = self.client.get("/api/discovery/items?provider=tmdb&category=popular&media_type=movie&page=0")
            invalid_page_type = self.client.get("/api/discovery/items?provider=tmdb&category=popular&media_type=movie&page=abc")
            invalid_filter = self.client.get("/api/discovery/items?provider=tmdb&category=popular&media_type=movie&page=1&api_key=leak")
            invalid_provider = self.client.get("/api/discovery/items?provider=bad&category=popular&media_type=movie&page=1")
            unavailable = self.client.get("/api/discovery/items?provider=tmdb&category=down&media_type=movie&page=1")
        self.assertEqual(invalid_page.status_code, 400)
        self.assertEqual(invalid_page_type.status_code, 400)
        self.assertEqual(invalid_filter.status_code, 400)
        self.assertEqual(invalid_provider.status_code, 400)
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.json()["code"], "unavailable")

    def test_anonymous_mutations_return_401_before_csrf(self):
        self.assertEqual(self.client.post("/api/discovery/map", json={}).status_code, 401)
        self.assertEqual(self.client.post("/api/discovery/watchlist", json={}).status_code, 401)
        self.assertEqual(self.client.delete("/api/discovery/watchlist/tmdb/movie/1").status_code, 401)

    def test_mutations_reject_untrusted_identity_and_raw_poster_key(self):
        headers = self.authenticate()
        service = FakeDiscoveryService()
        with patch("app.routes.discovery_api.get_discovery_service", return_value=service):
            invalid_map = self.client.post(
                "/api/discovery/map",
                json={"provider": "evil", "external_id": "../x", "media_type": "movie", "title": "X"},
                headers=headers,
            )
            invalid_watch = self.client.post(
                "/api/discovery/watchlist",
                json={
                    "provider": "tmdb", "external_id": "1", "media_type": "movie",
                    "title": "X", "poster_key": "https://user:pass@example/x.jpg",
                },
                headers=headers,
            )
            invalid_delete = self.client.delete(
                "/api/discovery/watchlist/evil/movie/1", headers=headers
            )
        self.assertEqual(invalid_map.status_code, 400)
        self.assertEqual(invalid_watch.status_code, 400)
        self.assertEqual(invalid_delete.status_code, 400)


    def test_mapping_prefers_async_service_boundary(self):
        headers = self.authenticate()
        service = FakeDiscoveryService()
        service.map_to_tmdb_async = AsyncMock(return_value={
            "tmdb_id": "550", "confirmed": False, "candidates": []
        })
        data = {
            "provider": "douban",
            "external_id": "7",
            "media_type": "movie",
            "title": "Movie",
            "year": "1999",
        }
        with patch("app.routes.discovery_api.get_discovery_service", return_value=service):
            response = self.client.post("/api/discovery/map", json=data, headers=headers)

        self.assertEqual(response.status_code, 200)
        service.map_to_tmdb_async.assert_awaited_once()

    def test_mapping_and_watchlist_writes_require_csrf(self):
        headers = self.authenticate()
        service = FakeDiscoveryService()
        data = {"provider": "douban", "external_id": "7", "media_type": "movie", "title": "Movie", "year": "1999"}
        with patch("app.routes.discovery_api.get_discovery_service", return_value=service):
            denied = self.client.post("/api/discovery/map", json=data)
            allowed = self.client.post("/api/discovery/map", json=data, headers=headers)
            from app.routes.discovery_image import encode_poster_token
            watch_data = dict(data)
            watch_data["poster_token"] = encode_poster_token(
                "douban", "img3.doubanio.com/view/photo/s_ratio_poster/public/p7.jpg"
            )
            added = self.client.post("/api/discovery/watchlist", json=watch_data, headers=headers)
            listed = self.client.get("/api/discovery/watchlist")
            removed = self.client.delete("/api/discovery/watchlist/douban/movie/7", headers=headers)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(added.status_code, 200)
        self.assertEqual(listed.status_code, 200)
        self.assertNotIn("poster_key", listed.text)
        self.assertNotIn("img3.doubanio.com", listed.text)
        self.assertTrue(listed.json()[0]["poster_url"].startswith("/discovery-poster/douban/"))
        self.assertEqual(removed.status_code, 200)


class DiscoveryPosterTests(_BaseClientTests):
    def _image_response(self, *, content_type="image/jpeg", content=b"\xff\xd8\xff\xe0", status=200, length=None):
        response = Mock(status_code=status)
        response.headers = {"Content-Type": content_type}
        if length is not None:
            response.headers["Content-Length"] = str(length)
        response.iter_content.return_value = [content]
        response.close = Mock()
        return response

    def _mock_session(self, response):
        """创建 mock Session，其 get 方法返回指定的上游响应。"""
        session = Mock()
        session.get.return_value = response
        return session

    def test_poster_requires_login_and_rejects_unknown_provider_key(self):
        self.assertEqual(self.client.get("/discovery-poster/tmdb/abc.jpg").status_code, 401)
        self.authenticate()
        self.assertEqual(self.client.get("/discovery-poster/douban/evil.example/poster.jpg").status_code, 404)
        self.assertEqual(self.client.get("/discovery-poster/unknown/x").status_code, 400)

    def test_poster_proxy_uses_allowlisted_upstream_without_redirects(self):
        self.authenticate()
        from app.routes.discovery_image import encode_poster_token
        response = self._image_response()
        session = self._mock_session(response)
        token = encode_poster_token("tmdb", "abc.jpg")
        with patch("app.routes.discovery_image._get_poster_session", return_value=session):
            result = self.client.get(f"/discovery-poster/tmdb/{token}")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["cache-control"], "private, max-age=86400")
        self.assertEqual(session.get.call_args.args[0], "https://image.tmdb.org/t/p/w500/abc.jpg")
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        response.close.assert_called_once()

    def test_poster_accepts_only_allowlisted_raster_mime_with_matching_magic(self):
        self.authenticate()
        from app.routes.discovery_image import encode_poster_token

        token = encode_poster_token("tmdb", "abc.jpg")
        cases = (
            ("image/jpeg", b"\xff\xd8\xff\xe0"),
            ("image/png", b"\x89PNG\r\n\x1a\n"),
            ("image/webp", b"RIFF\x04\x00\x00\x00WEBP"),
            ("image/avif", b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00avifmif1"),
            ("image/gif", b"GIF89a"),
        )
        for content_type, content in cases:
            upstream = self._image_response(content_type=content_type, content=content)
            session = self._mock_session(upstream)
            with self.subTest(content_type=content_type), patch(
                "app.routes.discovery_image._get_poster_session", return_value=session
            ):
                result = self.client.get(f"/discovery-poster/tmdb/{token}")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.headers["content-type"], content_type)
            upstream.close.assert_called_once()

    def test_poster_rejects_svg_unsafe_raster_mime_and_magic_mismatch(self):
        self.authenticate()
        from app.routes.discovery_image import encode_poster_token

        token = encode_poster_token("tmdb", "abc.jpg")
        cases = (
            ("image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"),
            ("image/bmp", b"BMnot-allowed"),
            ("image/jpeg", b"\x89PNG\r\n\x1a\n"),
        )
        for content_type, content in cases:
            upstream = self._image_response(content_type=content_type, content=content)
            session = self._mock_session(upstream)
            with self.subTest(content_type=content_type), patch(
                "app.routes.discovery_image._get_poster_session", return_value=session
            ):
                result = self.client.get(f"/discovery-poster/tmdb/{token}")
            self.assertEqual(result.status_code, 502)
            upstream.close.assert_called_once()

    def test_poster_rejects_non_image_oversized_or_redirect_response(self):
        self.authenticate()
        from app.routes.discovery_image import encode_poster_token
        token = encode_poster_token("tmdb", "abc.jpg")
        cases = [
            self._image_response(content_type="text/html"),
            self._image_response(length=6 * 1024 * 1024),
            self._image_response(status=302),
        ]
        for upstream in cases:
            session = self._mock_session(upstream)
            with self.subTest(upstream=upstream), patch("app.routes.discovery_image._get_poster_session", return_value=session):
                result = self.client.get(f"/discovery-poster/tmdb/{token}")
                self.assertEqual(result.status_code, 502)
                upstream.close.assert_called_once()

    def test_valid_douban_and_bangumi_tokens_map_to_fixed_hosts(self):
        from app.routes.discovery_image import encode_poster_token
        self.authenticate()
        cases = [
            ("douban", "img3.doubanio.com/view/photo/p7.jpg", "https://img3.doubanio.com/view/photo/p7.jpg"),
            ("bangumi", "lain.bgm.tv/pic/cover/l/42.jpg", "https://lain.bgm.tv/pic/cover/l/42.jpg"),
        ]
        for provider, key, expected in cases:
            upstream = self._image_response()
            session = self._mock_session(upstream)
            token = encode_poster_token(provider, key)
            with self.subTest(provider=provider), patch("app.routes.discovery_image._get_poster_session", return_value=session) as get:
                result = self.client.get(f"/discovery-poster/{provider}/{token}")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(session.get.call_args.args[0], expected)
            upstream.close.assert_called_once()

    def test_poster_token_rejects_encoded_traversal_unknown_host_and_tampering(self):
        from fastapi import HTTPException
        from app.routes.discovery_image import encode_poster_token
        with self.assertRaises(HTTPException):
            encode_poster_token("tmdb", "%2e%2e/%2e%2e/x.jpg")
        with self.assertRaises(HTTPException):
            encode_poster_token("douban", "img999.doubanio.com/x.jpg")
        self.authenticate()
        self.assertEqual(self.client.get("/discovery-poster/tmdb/not-a-valid-token").status_code, 400)

    def test_stream_read_failure_returns_502_and_closes_response(self):
        from app.routes.discovery_image import encode_poster_token
        self.authenticate()
        upstream = self._image_response()
        upstream.iter_content.side_effect = requests.ConnectionError("stream broke")
        session = self._mock_session(upstream)
        token = encode_poster_token("tmdb", "abc.jpg")
        with patch("app.routes.discovery_image._get_poster_session", return_value=session):
            result = self.client.get(f"/discovery-poster/tmdb/{token}")
        self.assertEqual(result.status_code, 502)
        upstream.close.assert_called_once()



def __getattr__(name: str):
    """兼容实施计划中的显式配置测试入口，常规发现时不重复收集。"""
    if name == "DiscoveryConfigTests":
        from tests.test_discovery_config import DiscoveryConfigTests
        return DiscoveryConfigTests
    raise AttributeError(name)


if __name__ == "__main__":
    unittest.main()
