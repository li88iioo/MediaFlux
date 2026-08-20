from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import database as db
from app.config import web_credentials
from app.main import create_app
from app.modules import media_proxy
from tests.support import IsolatedDatabaseTestCase


async def _value(value):
    return value


class SignedUrlCacheIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_releasing_old_runtime_cache_keeps_replacement_registered(self):
        old_cache = media_proxy.SignedUrlCache()
        new_cache = media_proxy.SignedUrlCache()
        await old_cache.get_or_fetch(
            "old", lambda: _value("https://signed.invalid/old")
        )
        await new_cache.get_or_fetch(
            "new", lambda: _value("https://signed.invalid/new")
        )
        with media_proxy._signed_url_caches_lock:
            previous = media_proxy._signed_url_caches.get(7)
            media_proxy._signed_url_caches[7] = new_cache
        try:
            media_proxy._release_signed_url_cache(7, old_cache)
            self.assertEqual(old_cache.entry_count, 0)
            self.assertEqual(new_cache.entry_count, 1)
            with media_proxy._signed_url_caches_lock:
                self.assertIs(media_proxy._signed_url_caches[7], new_cache)
        finally:
            with media_proxy._signed_url_caches_lock:
                if previous is None:
                    media_proxy._signed_url_caches.pop(7, None)
                else:
                    media_proxy._signed_url_caches[7] = previous

    async def test_cache_is_scoped_by_instance_account_and_optional_user_agent(self):
        now = [100.0]
        cache = media_proxy.SignedUrlCache(
            ttl_seconds=60,
            clock=lambda: now[0],
            wall_clock=lambda: 1000.0 + now[0] - 100.0,
        )
        calls = []

        async def fetch():
            calls.append(len(calls) + 1)
            return f"https://signed.invalid/{len(calls)}?Expires=2000"

        first = await cache.get_or_fetch("file", fetch, scope="instance-1:account-a")
        second = await cache.get_or_fetch("file", fetch, scope="instance-1:account-a")
        third = await cache.get_or_fetch("file", fetch, scope="instance-2:account-a")
        ua_a = await cache.get_or_fetch(
            "ua-file", fetch, scope="instance-1:account-a", user_agent="UA-A", ua_bound=True
        )
        ua_b = await cache.get_or_fetch(
            "ua-file", fetch, scope="instance-1:account-a", user_agent="UA-B", ua_bound=True
        )
        no_ua_a = await cache.get_or_fetch(
            "plain-file", fetch, scope="instance-1:account-a", user_agent="UA-A"
        )
        no_ua_b = await cache.get_or_fetch(
            "plain-file", fetch, scope="instance-1:account-a", user_agent="UA-B"
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, third)
        self.assertNotEqual(ua_a, ua_b)
        self.assertEqual(no_ua_a, no_ua_b)
        metrics = cache.metrics()
        self.assertEqual(metrics["hits"], 2)
        self.assertEqual(metrics["misses"], 5)
        self.assertEqual(metrics["entries"], 5)
        self.assertNotIn("account-a", json.dumps(metrics))

    async def test_provider_expiry_margin_shortens_cache_lifetime(self):
        mono = [50.0]
        wall = [1000.0]
        cache = media_proxy.SignedUrlCache(
            ttl_seconds=60,
            expiry_margin_seconds=10,
            clock=lambda: mono[0],
            wall_clock=lambda: wall[0],
        )
        calls = []

        async def fetch():
            calls.append(1)
            return "https://signed.invalid/file?Expires=1015&token=must-not-leak"

        await cache.get_or_fetch("file", fetch, scope="one")
        mono[0] += 4
        wall[0] += 4
        await cache.get_or_fetch("file", fetch, scope="one")
        mono[0] += 2
        wall[0] += 2
        await cache.get_or_fetch("file", fetch, scope="one")

        self.assertEqual(len(calls), 2)
        self.assertEqual(cache.metrics()["expired"], 1)

    async def test_clear_scope_does_not_evict_other_instances(self):
        cache = media_proxy.SignedUrlCache()
        await cache.get_or_fetch("file", lambda: _value("https://signed.invalid/1"), scope="1:a")
        await cache.get_or_fetch("file", lambda: _value("https://signed.invalid/2"), scope="2:a")

        self.assertEqual(cache.clear_scope("1:"), 1)
        self.assertEqual(cache.entry_count, 1)

    async def test_global_cache_metrics_include_safe_aggregate_and_instances(self):
        first = media_proxy.SignedUrlCache()
        second = media_proxy.SignedUrlCache()
        await first.get_or_fetch("a", lambda: _value("https://signed.invalid/a"))
        await first.get_or_fetch("a", lambda: _value("https://signed.invalid/a"))
        await second.get_or_fetch("b", lambda: _value("https://signed.invalid/b"))
        with media_proxy._signed_url_caches_lock:
            previous = dict(media_proxy._signed_url_caches)
            media_proxy._signed_url_caches.clear()
            media_proxy._signed_url_caches.update({1: first, 2: second})
        try:
            metrics = media_proxy.signed_url_cache_metrics()
        finally:
            with media_proxy._signed_url_caches_lock:
                media_proxy._signed_url_caches.clear()
                media_proxy._signed_url_caches.update(previous)

        self.assertEqual(metrics["hits"], 1)
        self.assertEqual(metrics["misses"], 2)
        self.assertEqual(metrics["entries"], 2)
        self.assertEqual(set(metrics["instances"]), {"1", "2"})


class ProxyRouteClassificationTests(unittest.TestCase):
    def test_routes_keep_guangya_direct_and_upstream_boundaries(self):
        cases = {
            "/playgy/file/e/1/Movie.mkv": "guangya_direct",
            "/Videos/item/stream": "stream",
            "/emby/Items/item/PlaybackInfo": "playback_info",
            "/Videos/item/master.m3u8": "upstream_hls",
            "/Videos/item/hls1/main/0.ts": "upstream_hls",
            "/socket": "upstream",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(media_proxy.classify_proxy_route(path, "GET"), expected)


class PlaybackSessionRegistryTests(unittest.TestCase):
    def test_new_playback_info_context_wins_over_previous_same_media_session(self):
        registry = media_proxy.PlaybackSessionRegistry()
        first = registry.begin("scope", 7, item_id="item-1")
        first_stream = registry.resolve(
            "scope", 7, item_id="item-1", source_id="source-1", create=True
        )
        second = registry.begin("scope", 7, item_id="item-1")
        second_stream = registry.resolve(
            "scope", 7, item_id="item-1", source_id="source-1", create=True
        )

        self.assertEqual(first_stream.token, first.token)
        self.assertEqual(second_stream.token, second.token)
        self.assertNotEqual(first.token, second.token)

    def test_ambiguous_pending_sessions_reuse_the_created_exact_fallback(self):
        registry = media_proxy.PlaybackSessionRegistry()
        registry.begin("scope", 7, item_id="item-1")
        registry.begin("scope", 7, item_id="item-1")

        first = registry.resolve(
            "scope", 7, item_id="item-1", source_id="source-1", create=True
        )
        second = registry.resolve(
            "scope", 7, item_id="item-1", source_id="source-1", create=True
        )

        self.assertEqual(second.token, first.token)

    def test_persistent_key_does_not_expose_session_or_auth_scope(self):
        registry = media_proxy.PlaybackSessionRegistry()
        entry = registry.begin(
            "auth-scope-secret", 7, token="play-session-secret", item_id="item-1"
        )

        key = registry.persistent_key(entry)

        self.assertEqual(len(key), 48)
        self.assertNotIn("auth-scope-secret", key)
        self.assertNotIn("play-session-secret", key)


class PlaybackRecordDatabaseTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_proxy_playback_records")
            conn.execute("DELETE FROM media_proxy_playback_sessions")
            conn.execute("DELETE FROM media_proxy_instances")
        self.instance_id = db.add_media_proxy_instance(
            name="测试反代",
            server_type="jellyfin",
            config_source="custom",
            upstream_url="http://127.0.0.1:8096",
            api_key="secret-api-key",
            listen_host="127.0.0.1",
            listen_port=18999,
            local_root="",
            enabled=1,
        )

    def test_record_is_redacted_and_list_never_returns_urls_or_credentials(self):
        record_id = db.record_media_proxy_playback_attempt(
            instance_id=self.instance_id,
            route_class="guangya_direct",
            method="GET",
            status_code=502,
            source="guangya",
            cache_hit=False,
            upstream_latency_ms=12,
            total_latency_ms=18,
            failure_stage="signed_url",
            error=(
                "failed https://signed.invalid/file?token=secret-token "
                "Authorization: Bearer access-secret api_key=secret-api-key"
            ),
        )

        result = db.list_media_proxy_playback_records(instance_id=self.instance_id)

        self.assertEqual(result["total"], 1)
        row = result["items"][0]
        self.assertEqual(row["id"], record_id)
        serialized = json.dumps(dict(row), ensure_ascii=False)
        for secret in ("signed.invalid", "secret-token", "access-secret", "secret-api-key"):
            self.assertNotIn(secret, serialized)
        self.assertNotIn("url", {key.lower() for key in row.keys()})

    def test_filters_pagination_and_confirmed_clear(self):
        for status, source in ((302, "guangya"), (206, "upstream"), (502, "upstream")):
            db.record_media_proxy_playback_attempt(
                instance_id=self.instance_id,
                route_class="stream",
                method="GET",
                status_code=status,
                source=source,
            )

        page = db.list_media_proxy_playback_records(
            instance_id=self.instance_id,
            status="error",
            source="upstream",
            page=1,
            page_size=1,
        )
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["status_code"], 502)
        legacy = db.list_media_proxy_playback_sessions(instance_id=self.instance_id)
        self.assertEqual(legacy["total"], 0)
        self.assertEqual(legacy["unlinked_total"], 3)
        self.assertEqual(db.clear_media_proxy_playback_records(instance_id=self.instance_id), 3)

    def test_session_status_filter_uses_final_outcome_after_a_retry(self):
        for status in (502, 206):
            db.record_media_proxy_playback_attempt(
                instance_id=self.instance_id,
                playback_session_key="recovered-session",
                media_item_id="item-recovered",
                route_class="stream",
                method="GET",
                status_code=status,
                source="upstream",
            )

        success = db.list_media_proxy_playback_sessions(
            instance_id=self.instance_id, status="success"
        )
        failure = db.list_media_proxy_playback_sessions(
            instance_id=self.instance_id, status="error"
        )

        self.assertEqual(success["total"], 1)
        self.assertEqual(success["items"][0]["error_count"], 1)
        self.assertEqual(failure["total"], 0)

    def test_same_session_is_aggregated_while_request_details_remain_available(self):
        first_id = db.record_media_proxy_playback_attempt(
            instance_id=self.instance_id,
            playback_session_key="session-digest-1",
            media_item_id="item-1",
            media_source_id="source-1",
            route_class="playback_info",
            method="POST",
            status_code=200,
            source="playback_info",
            total_latency_ms=30,
        )
        second_id = db.record_media_proxy_playback_attempt(
            instance_id=self.instance_id,
            playback_session_key="session-digest-1",
            media_item_id="item-1",
            media_source_id="source-1",
            guangya_file_id="file-1",
            route_class="guangya_direct",
            method="GET",
            status_code=302,
            source="guangya",
            cache_hit=True,
            total_latency_ms=10,
        )

        sessions = db.list_media_proxy_playback_sessions(instance_id=self.instance_id)

        self.assertEqual(sessions["total"], 1)
        summary = sessions["items"][0]
        self.assertEqual(summary["media_item_id"], "item-1")
        self.assertEqual(summary["media_source_id"], "source-1")
        self.assertEqual(summary["guangya_file_id"], "file-1")
        self.assertEqual(summary["request_count"], 2)
        self.assertEqual(summary["success_count"], 2)
        self.assertEqual(summary["error_count"], 0)
        self.assertEqual(summary["cache_hit_count"], 1)
        self.assertEqual(summary["average_total_latency_ms"], 20)
        details = db.list_media_proxy_playback_records(session_id=summary["id"])
        self.assertEqual(details["total"], 2)
        self.assertEqual({row["id"] for row in details["items"]}, {first_id, second_id})


class ProxyRouteRecordIntegrationTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_proxy_playback_records")
            conn.execute("DELETE FROM media_proxy_playback_sessions")
        media_proxy._playback_sessions.clear()

    def test_logged_out_request_from_old_runtime_does_not_clear_replacement_cache(self):
        class LoggedOutClient:
            logged_in = False

        old_cache = media_proxy.SignedUrlCache()
        old_app = media_proxy.create_proxy_app(7, old_cache)
        replacement_cache = media_proxy.SignedUrlCache()
        asyncio.run(replacement_cache.get_or_fetch(
            "replacement",
            lambda: _value("https://signed.invalid/replacement"),
        ))
        instance = {
            "id": 7,
            "enabled": 1,
            "upstream_url": "http://127.0.0.1:8096",
        }
        with media_proxy._signed_url_caches_lock:
            previous = media_proxy._signed_url_caches.get(7)
            media_proxy._signed_url_caches[7] = replacement_cache
        try:
            with patch(
                "app.modules.media_proxy._resolved_instance", return_value=instance
            ), patch(
                "app.modules.media_proxy._client_is_authorized",
                new=AsyncMock(return_value=True),
            ), patch("app.modules.media_proxy.GuangYaClient", LoggedOutClient):
                with TestClient(old_app) as client:
                    response = client.get(
                        "/playgy/file-1/e/1/Movie.mkv",
                        follow_redirects=False,
                    )

            self.assertEqual(response.status_code, 503)
            self.assertEqual(replacement_cache.entry_count, 1)
        finally:
            with media_proxy._signed_url_caches_lock:
                if previous is None:
                    media_proxy._signed_url_caches.pop(7, None)
                else:
                    media_proxy._signed_url_caches[7] = previous

    def test_guangya_redirect_records_cache_miss_then_hit_without_signed_url(self):
        class Raw:
            token = "provider-account-secret"

        class Client:
            calls = 0
            logged_in = True
            _raw = Raw()

            def get_download_url(self, _file_id, **_kwargs):
                self.__class__.calls += 1
                return "https://signed.invalid/file?Expires=4102444800&token=secret"

        instance = {
            "id": 7,
            "enabled": 1,
            "upstream_url": "http://127.0.0.1:8096",
        }
        Client.calls = 0
        with patch("app.modules.media_proxy._resolved_instance", return_value=instance), patch(
            "app.modules.media_proxy._client_is_authorized", new=AsyncMock(return_value=True)
        ), patch("app.modules.media_proxy.GuangYaClient", Client):
            with TestClient(media_proxy.create_proxy_app(7)) as client:
                first = client.get("/playgy/file-1/e/1/Movie.mkv", follow_redirects=False)
                second = client.get("/playgy/file-1/e/1/Movie.mkv", follow_redirects=False)

        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertEqual(Client.calls, 1)
        rows = db.list_media_proxy_playback_records(instance_id=7)["items"]
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["cache_hit"] for row in reversed(rows)], [0, 1])
        self.assertTrue(all(row["route_class"] == "guangya_direct" for row in rows))
        sessions = db.list_media_proxy_playback_sessions(instance_id=7)
        self.assertEqual(sessions["total"], 2)
        self.assertEqual(
            [session["request_count"] for session in sessions["items"]],
            [1, 1],
        )
        self.assertTrue(all(session["guangya_file_id"] == "file-1" for session in sessions["items"]))
        serialized = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn("signed.invalid", serialized)
        self.assertNotIn("provider-account-secret", serialized)


@contextmanager
def _logged_in_client():
    with patch.dict(os.environ, {
        "APP_ENV": "development",
        "MEDIAFLUX_INITIALIZED": "1",
        "WEB_SECRET_KEY": "proxy-record-test-secret",
        "ENV_WEB_PASSPORT": "admin",
        "ENV_WEB_PASSWORD": "password",
    }, clear=False), TestClient(create_app()) as client:
        page = client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        response = client.post(
            "/login",
            data={"csrf_token": token, "username": "admin", "password": "password"},
            follow_redirects=False,
        )
        if response.status_code != 302:
            raise AssertionError(response.text)
        settings = client.get("/settings")
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', settings.text).group(1)
        yield client, csrf


class PlaybackRecordApiTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_proxy_playback_records")
            conn.execute("DELETE FROM media_proxy_playback_sessions")
            conn.execute("DELETE FROM media_proxy_instances")
        self.instance_id = db.add_media_proxy_instance(
            name="API 反代", server_type="jellyfin", config_source="custom",
            upstream_url="http://127.0.0.1:8096", api_key="secret",
            listen_host="127.0.0.1", listen_port=19001, local_root="", enabled=1,
        )
        db.record_media_proxy_playback_attempt(
            instance_id=self.instance_id, playback_session_key="api-session-1",
            media_item_id="api-item", media_source_id="api-source",
            route_class="guangya_direct", method="GET", status_code=302,
            source="guangya", cache_hit=True,
        )

    def test_records_api_requires_login_and_returns_safe_page(self):
        with TestClient(create_app()) as anonymous:
            denied = anonymous.get("/api/media-proxy/records")
        self.assertEqual(denied.status_code, 401)

        with _logged_in_client() as (client, _csrf):
            response = client.get(
                f"/api/media-proxy/records?instance_id={self.instance_id}&source=guangya"
            )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["total"], 1)
        self.assertNotIn("url", json.dumps(payload).lower())
        self.assertNotIn("secret", json.dumps(payload).lower())

    def test_sessions_api_returns_one_media_playback_summary(self):
        with _logged_in_client() as (client, _csrf):
            response = client.get(
                f"/api/media-proxy/sessions?instance_id={self.instance_id}&source=guangya"
            )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["media_item_id"], "api-item")
        self.assertEqual(payload["items"][0]["request_count"], 1)
        self.assertNotIn("session_key", payload["items"][0])

    def test_clear_api_requires_exact_confirmation_and_csrf(self):
        with _logged_in_client() as (client, csrf):
            denied = client.request(
                "DELETE",
                "/api/media-proxy/records",
                headers={"X-CSRF-Token": csrf},
                json={"confirm": "clear"},
            )
            cleared = client.request(
                "DELETE",
                "/api/media-proxy/records",
                headers={"X-CSRF-Token": csrf},
                json={"confirm": "CLEAR PLAYBACK RECORDS", "instance_id": self.instance_id},
            )
        self.assertEqual(denied.status_code, 400)
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json()["deleted"], 1)
        self.assertEqual(db.list_media_proxy_playback_sessions(instance_id=self.instance_id)["total"], 0)


if __name__ == "__main__":
    unittest.main()
