from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import unittest
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import database as db
from app.config import web_credentials
from app.main import create_app
from app.modules import media_proxy
from app.modules.media_proxy_safety import safe_media_name
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

    async def test_guangya_ts_expiry_and_exact_invalidation(self):
        mono = [10.0]
        wall = [1000.0]
        cache = media_proxy.SignedUrlCache(
            ttl_seconds=60,
            expiry_margin_seconds=5,
            clock=lambda: mono[0],
            wall_clock=lambda: wall[0],
        )
        calls = []

        async def fetch():
            calls.append(1)
            return f"https://signed.invalid/file?ts=1010&token={len(calls)}"

        first = await cache.get_or_fetch(
            "file", fetch, scope="1:account", user_agent="UA", ua_bound=True,
        )
        mono[0] += 4
        wall[0] += 4
        second = await cache.get_or_fetch(
            "file", fetch, scope="1:account", user_agent="UA", ua_bound=True,
        )
        self.assertEqual(first, second)
        self.assertTrue(cache.invalidate(
            "file", scope="1:account", user_agent="UA", ua_bound=True,
        ))
        third = await cache.get_or_fetch(
            "file", fetch, scope="1:account", user_agent="UA", ua_bound=True,
        )

        self.assertNotEqual(second, third)
        self.assertEqual(len(calls), 2)

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
    @staticmethod
    def _source_signature(
        token: str,
        *,
        instance_id: int = 7,
        item_id: str = "item-1",
        source_id: str = "source-1",
    ) -> str:
        return media_proxy._playback_source_signature(
            instance_id, item_id, source_id, token
        )

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

    def test_server_capability_recovers_authenticated_scope_without_raw_token(self):
        registry = media_proxy.PlaybackSessionRegistry()
        entry = registry.begin(
            "scope", 7, item_id="item-1", server_capability=True
        )

        signature = self._source_signature(entry.token)
        recovered = registry.resolve_capability(
            7,
            entry.token,
            item_id="item-1",
            source_id="source-1",
            source_signature=signature,
        )

        self.assertIs(recovered, entry)
        self.assertEqual(recovered.auth_scope, "scope")
        self.assertIsNone(registry.resolve_capability(
            8,
            entry.token,
            item_id="item-1",
            source_id="source-1",
            source_signature=signature,
        ))
        self.assertIsNone(registry.resolve_capability(
            7,
            entry.token,
            item_id="item-2",
            source_id="source-1",
            source_signature=signature,
        ))

    def test_upstream_play_session_alias_resolves_to_server_capability_session(self):
        registry = media_proxy.PlaybackSessionRegistry()
        entry = registry.finalize_capability(
            "scope",
            7,
            "opaque-capability",
            item_id="item-1",
            upstream_session_token="emby-session-1",
            media_name="Movie.mkv",
        )

        resolved = registry.resolve(
            "scope",
            7,
            token="emby-session-1",
            item_id="item-1",
            source_id="source-1",
        )

        self.assertIs(resolved, entry)
        self.assertEqual(resolved.media_name, "Movie.mkv")
        self.assertEqual(
            registry.persistent_key(resolved), registry.persistent_key(entry)
        )

    def test_new_play_session_inherits_recent_media_name_for_exact_source(self):
        registry = media_proxy.PlaybackSessionRegistry()
        registry.remember_media_names(
            "scope-a",
            7,
            "item-1",
            {"source-a": "Movie.mkv"},
        )

        first = registry.begin(
            "scope-a",
            7,
            token="play-session-a",
            item_id="item-1",
            source_id="source-a",
        )
        second = registry.begin(
            "scope-a",
            7,
            token="play-session-b",
            item_id="item-1",
            source_id="source-a",
        )

        self.assertEqual(first.media_name, "Movie.mkv")
        self.assertEqual(second.media_name, "Movie.mkv")
        self.assertNotEqual(
            registry.persistent_key(first), registry.persistent_key(second)
        )

    def test_same_session_switching_source_does_not_pollute_new_source_title(self):
        registry = media_proxy.PlaybackSessionRegistry()
        registry.remember_media_names(
            "scope-a",
            7,
            "item-1",
            {
                "source-a": "Source A.mkv",
                "source-b": "Source B.mkv",
            },
        )
        entry = registry.begin(
            "scope-a",
            7,
            token="shared-session",
            item_id="item-1",
            source_id="source-a",
        )

        switched = registry.resolve(
            "scope-a",
            7,
            token="shared-session",
            item_id="item-1",
            source_id="source-b",
        )
        fresh = registry.begin(
            "scope-a",
            7,
            token="fresh-session",
            item_id="item-1",
            source_id="source-b",
        )

        self.assertIs(switched, entry)
        self.assertEqual(switched.media_name, "Source B.mkv")
        self.assertEqual(fresh.media_name, "Source B.mkv")

    def test_media_name_context_isolated_by_scope_instance_item_and_source(self):
        registry = media_proxy.PlaybackSessionRegistry()
        registry.remember_media_names(
            "scope-a",
            7,
            "item-1",
            {"source-a": "Movie.mkv"},
        )

        cases = (
            ("scope-b", 7, "item-1", "source-a"),
            ("scope-a", 8, "item-1", "source-a"),
            ("scope-a", 7, "item-2", "source-a"),
            ("scope-a", 7, "item-1", "source-b"),
            ("", 7, "item-1", "source-a"),
        )
        for index, (scope, instance_id, item_id, source_id) in enumerate(cases):
            with self.subTest(
                scope=scope,
                instance_id=instance_id,
                item_id=item_id,
                source_id=source_id,
            ):
                entry = registry.begin(
                    scope,
                    instance_id,
                    token=f"isolated-{index}",
                    item_id=item_id,
                    source_id=source_id,
                )
                self.assertEqual(entry.media_name, "")

    def test_media_name_context_expires_with_playback_session_ttl(self):
        now = [100.0]
        registry = media_proxy.PlaybackSessionRegistry(
            ttl_seconds=10,
            clock=lambda: now[0],
        )
        registry.remember_media_names(
            "scope-a",
            7,
            "item-1",
            {"source-a": "Movie.mkv"},
        )

        now[0] = 111.0
        entry = registry.begin(
            "scope-a",
            7,
            token="expired-session",
            item_id="item-1",
            source_id="source-a",
        )

        self.assertEqual(entry.media_name, "")

    def test_unsafe_media_name_is_not_added_to_inheritance_context(self):
        registry = media_proxy.PlaybackSessionRegistry()
        registry.remember_media_names(
            "scope-a",
            7,
            "item-1",
            {"source-a": "web+foo:user:password@example.invalid"},
        )

        entry = registry.begin(
            "scope-a",
            7,
            token="safe-session",
            item_id="item-1",
            source_id="source-a",
        )

        self.assertEqual(entry.media_name, "")

    def test_repeated_capabilities_share_upstream_session_and_expire_independently(self):
        now = [100.0]
        registry = media_proxy.PlaybackSessionRegistry(
            capability_ttl_seconds=900,
            capability_max_ttl_seconds=950,
            clock=lambda: now[0],
        )
        first = registry.finalize_capability(
            "scope",
            7,
            "capability-a",
            item_id="item-1",
            upstream_session_token="emby-session-1",
        )
        now[0] = 200.0
        second = registry.finalize_capability(
            "scope",
            7,
            "capability-b",
            item_id="item-1",
            upstream_session_token="emby-session-1",
        )

        self.assertIs(first, second)
        for token in ("capability-a", "capability-b"):
            self.assertIs(
                registry.resolve_capability(
                    7,
                    token,
                    item_id="item-1",
                    source_id="source-1",
                    source_signature=self._source_signature(token),
                ),
                first,
            )
        self.assertIs(
            registry.resolve(
                "scope", 7, token="emby-session-1", item_id="item-1"
            ),
            first,
        )

        now[0] = 1051.0
        self.assertIsNone(
            registry.resolve_capability(
                7,
                "capability-a",
                item_id="item-1",
                source_id="source-1",
                source_signature=self._source_signature("capability-a"),
            )
        )
        self.assertIs(
            registry.resolve_capability(
                7,
                "capability-b",
                item_id="item-1",
                source_id="source-1",
                source_signature=self._source_signature("capability-b"),
            ),
            first,
        )

    def test_upstream_session_alias_never_merges_different_items(self):
        registry = media_proxy.PlaybackSessionRegistry()
        first = registry.finalize_capability(
            "scope",
            7,
            "capability-a",
            item_id="item-a",
            media_name="A.mkv",
            upstream_session_token="shared-session",
        )

        second = registry.resolve(
            "scope",
            7,
            token="shared-session",
            item_id="item-b",
            source_id="source-b",
            media_name="B.mkv",
            create=True,
        )

        self.assertIsNot(second, first)
        self.assertEqual(first.item_id, "item-a")
        self.assertEqual(first.media_name, "A.mkv")
        self.assertEqual(second.item_id, "item-b")
        self.assertNotEqual(
            registry.persistent_key(first), registry.persistent_key(second)
        )
        self.assertIs(
            registry.resolve(
                "scope", 7, token="shared-session", item_id="item-a"
            ),
            first,
        )

    def test_server_capability_never_upgrades_anonymous_session(self):
        registry = media_proxy.PlaybackSessionRegistry()
        entry = registry.begin(
            "", 7, item_id="item-1", server_capability=True
        )

        self.assertIsNone(
            registry.resolve_capability(
                7,
                entry.token,
                item_id="item-1",
                source_id="source-1",
                source_signature=self._source_signature(entry.token),
            )
        )

    def test_client_selected_session_token_is_not_a_server_capability(self):
        registry = media_proxy.PlaybackSessionRegistry()
        entry = registry.begin(
            "scope", 7, token="client-selected", item_id="item-1"
        )

        self.assertIsNone(
            registry.resolve_capability(
                7,
                entry.token,
                item_id="item-1",
                source_id="source-1",
                source_signature=self._source_signature(entry.token),
            )
        )

    def test_server_capability_expires_after_idle_timeout(self):
        now = [100.0]
        registry = media_proxy.PlaybackSessionRegistry(
            ttl_seconds=1800,
            capability_ttl_seconds=900,
            capability_max_ttl_seconds=3600,
            clock=lambda: now[0],
        )
        entry = registry.begin(
            "scope", 7, item_id="item-1", server_capability=True
        )

        now[0] = 1001.0
        self.assertIsNone(
            registry.resolve_capability(
                7,
                entry.token,
                item_id="item-1",
                source_id="source-1",
                source_signature=self._source_signature(entry.token),
            ),
        )

    def test_server_capability_renews_while_playback_remains_active(self):
        now = [100.0]
        registry = media_proxy.PlaybackSessionRegistry(
            ttl_seconds=3600,
            capability_ttl_seconds=900,
            capability_max_ttl_seconds=3600,
            clock=lambda: now[0],
        )
        entry = registry.begin(
            "scope", 7, item_id="item-1", server_capability=True
        )

        for timestamp in (999.0, 1800.0, 2600.0):
            now[0] = timestamp
            self.assertIs(
                registry.resolve_capability(
                    7,
                    entry.token,
                    item_id="item-1",
                    source_id="source-1",
                    source_signature=self._source_signature(entry.token),
                ),
                entry,
            )

    def test_server_capability_never_exceeds_absolute_authorization_ttl(self):
        now = [100.0]
        registry = media_proxy.PlaybackSessionRegistry(
            ttl_seconds=5000,
            capability_ttl_seconds=900,
            capability_max_ttl_seconds=1800,
            clock=lambda: now[0],
        )
        entry = registry.begin(
            "scope", 7, item_id="item-1", server_capability=True
        )

        for timestamp in (999.0, 1799.0, 1899.0):
            now[0] = timestamp
            self.assertIs(
                registry.resolve_capability(
                    7,
                    entry.token,
                    item_id="item-1",
                    source_id="source-1",
                    source_signature=self._source_signature(entry.token),
                ),
                entry,
            )
        now[0] = 1901.0
        self.assertIsNone(
            registry.resolve_capability(
                7,
                entry.token,
                item_id="item-1",
                source_id="source-1",
                source_signature=self._source_signature(entry.token),
            )
        )

    def test_finalize_capability_recreates_evicted_entry_and_restarts_ttl(self):
        now = [100.0]
        registry = media_proxy.PlaybackSessionRegistry(
            ttl_seconds=1800,
            capability_ttl_seconds=900,
            max_entries=1,
            clock=lambda: now[0],
        )
        issued = registry.begin(
            "scope", 7, item_id="item-1", server_capability=True
        )
        registry.begin("other-scope", 8, item_id="other-item")

        now[0] = 1001.0
        finalized = registry.finalize_capability(
            "scope", 7, issued.token, item_id="item-1"
        )

        self.assertIs(
            registry.resolve_capability(
                7,
                issued.token,
                item_id="item-1",
                source_id="source-1",
                source_signature=self._source_signature(issued.token),
            ),
            finalized,
        )
        now[0] = 1902.0
        self.assertIsNone(
            registry.resolve_capability(
                7,
                issued.token,
                item_id="item-1",
                source_id="source-1",
                source_signature=self._source_signature(issued.token),
            )
        )

    def test_invalid_capability_lookup_does_not_scan_all_sessions(self):
        class NoScanOrderedDict(OrderedDict):
            def items(self):
                raise AssertionError("capability lookup must use the direct index")

        registry = media_proxy.PlaybackSessionRegistry()
        entry = registry.begin(
            "scope", 7, item_id="item-1", server_capability=True
        )
        registry._entries = NoScanOrderedDict(registry._entries)

        self.assertIsNone(registry.resolve_capability(
            7,
            "missing",
            item_id="item-1",
            source_id="source-1",
            source_signature=self._source_signature("missing"),
        ))
        self.assertIs(
            registry.resolve_capability(
                7,
                entry.token,
                item_id="item-1",
                source_id="source-1",
                source_signature=self._source_signature(entry.token),
            ),
            entry,
        )

    def test_persistent_key_does_not_expose_session_or_auth_scope(self):
        registry = media_proxy.PlaybackSessionRegistry()
        entry = registry.begin(
            "auth-scope-secret", 7, token="play-session-secret", item_id="item-1"
        )

        key = registry.persistent_key(entry)

        self.assertEqual(len(key), 48)
        self.assertNotIn("auth-scope-secret", key)
        self.assertNotIn("play-session-secret", key)


class PlaybackAuthorizationLeaseTests(unittest.TestCase):
    def test_dynamic_mapping_renews_until_absolute_limit(self):
        now = [0.0]
        mappings = media_proxy.DynamicGuangYaMappings(
            ttl_seconds=10,
            max_ttl_seconds=25,
            clock=lambda: now[0],
        )
        mappings.register(7, "item", "source", "file")

        for timestamp in (9.0, 18.0, 24.0):
            now[0] = timestamp
            self.assertEqual(mappings.get(7, "item", "source"), "file")
        now[0] = 26.0
        self.assertIsNone(mappings.get(7, "item", "source"))

    def test_item_binding_scope_renews_until_absolute_limit(self):
        now = [0.0]
        scopes = media_proxy.ItemLevelBindingScopes(
            ttl_seconds=10,
            max_ttl_seconds=25,
            clock=lambda: now[0],
        )
        scopes.register(7, "item", "source", "binding", "scope")

        for timestamp in (9.0, 18.0, 24.0):
            now[0] = timestamp
            self.assertTrue(
                scopes.matches(7, "item", "source", "binding", "scope")
            )
        now[0] = 26.0
        self.assertFalse(
            scopes.matches(7, "item", "source", "binding", "scope")
        )

    def test_playback_grant_renews_until_absolute_limit(self):
        now = [0.0]
        grants = media_proxy.PlaybackGrantRegistry(
            ttl_seconds=10,
            max_ttl_seconds=25,
            clock=lambda: now[0],
        )
        grants.register(
            "scope",
            7,
            "item",
            "source",
            source_type="guangya",
            file_id="file",
        )

        for timestamp in (9.0, 18.0, 24.0):
            now[0] = timestamp
            self.assertTrue(grants.allows_file("scope", 7, "file"))
        now[0] = 26.0
        self.assertFalse(grants.allows_file("scope", 7, "file"))


class SafeMediaNameTests(unittest.TestCase):
    def test_human_titles_with_colons_or_slashes_are_not_treated_as_uris(self):
        for title in (
            "Re:Zero",
            "Cyberpunk:Edgerunners",
            "Face/Off",
            "Fate/stay night",
        ):
            with self.subTest(title=title):
                self.assertEqual(safe_media_name(title), title)

    def test_explicit_paths_and_unsafe_opaque_uris_remain_redacted(self):
        self.assertEqual(
            safe_media_name(
                "https://user:password@example.invalid/private/Movie.mkv"
                "?api_key=secret"
            ),
            "Movie.mkv",
        )
        self.assertEqual(
            safe_media_name("custom:/private/library/Movie.mkv"),
            "Movie.mkv",
        )
        for value in (
            "web+foo:user:password@example.invalid",
            "javascript:alert(document.cookie)",
            "data:text/plain,secret",
        ):
            with self.subTest(value=value):
                self.assertEqual(safe_media_name(value), "")


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

    def test_authority_only_media_url_never_leaks_credentials(self):
        db.record_media_proxy_playback_attempt(
            instance_id=self.instance_id,
            playback_session_key="authority-only",
            media_item_id="item-authority",
            media_name=(
                "https://user:password@media.invalid"
                "?api_key=secret#private"
            ),
            route_class="playback_info",
            method="GET",
            status_code=200,
            source="playback_info",
        )

        summary = db.list_media_proxy_playback_sessions(
            instance_id=self.instance_id
        )["items"][0]
        serialized = json.dumps(dict(summary), ensure_ascii=False)
        self.assertEqual(summary["media_name"], "")
        for secret in ("user", "password", "media.invalid", "api_key", "secret"):
            self.assertNotIn(secret, serialized)

    def test_encoded_media_url_is_reduced_to_safe_filename(self):
        db.record_media_proxy_playback_attempt(
            instance_id=self.instance_id,
            playback_session_key="encoded-url",
            media_item_id="item-encoded",
            media_name=(
                "https%3A%2F%2Fuser%3Apassword%40media.invalid%2Fprivate%2F"
                "Encoded%20Movie.mkv%3Fapi_key%3Dsecret%23frag"
            ),
            route_class="playback_info",
            method="GET",
            status_code=200,
            source="playback_info",
        )

        summary = db.list_media_proxy_playback_sessions(
            instance_id=self.instance_id
        )["items"][0]
        serialized = json.dumps(dict(summary), ensure_ascii=False)
        self.assertEqual(summary["media_name"], "Encoded Movie.mkv")
        for secret in (
            "user", "password", "media.invalid", "api_key", "secret", "%40", "%3F"
        ):
            self.assertNotIn(secret, serialized)

    def test_opaque_uri_media_name_is_not_persisted(self):
        db.record_media_proxy_playback_attempt(
            instance_id=self.instance_id,
            playback_session_key="opaque-uri",
            media_item_id="item-opaque",
            media_name="web+foo:user:password@example.invalid",
            route_class="playback_info",
            method="GET",
            status_code=200,
            source="playback_info",
        )

        summary = db.list_media_proxy_playback_sessions(
            instance_id=self.instance_id
        )["items"][0]
        serialized = json.dumps(dict(summary), ensure_ascii=False)
        self.assertEqual(summary["media_name"], "")
        for secret in ("user", "password", "example.invalid", "web+foo"):
            self.assertNotIn(secret, serialized)

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
            media_name=(
                "https://user:password@media.invalid/private/"
                "Movie%20Name.mkv?api_key=secret"
            ),
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
        self.assertEqual(summary["media_name"], "Movie Name.mkv")
        self.assertEqual(summary["guangya_file_id"], "file-1")
        self.assertNotIn("secret", json.dumps(dict(summary), ensure_ascii=False))
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


class PlaybackFailureSummaryTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_proxy_playback_records")
            conn.execute("DELETE FROM media_proxy_playback_sessions")
            conn.execute("DELETE FROM media_proxy_instances")
        self.first = db.add_media_proxy_instance(
            name="第一反代",
            server_type="jellyfin",
            config_source="custom",
            upstream_url="http://127.0.0.1:8096",
            api_key="secret-api-key",
            listen_host="127.0.0.1",
            listen_port=19101,
            enabled=1,
        )
        self.second = db.add_media_proxy_instance(
            name="第二反代",
            server_type="emby",
            config_source="custom",
            upstream_url="http://127.0.0.1:8097",
            api_key="secret-emby-key",
            listen_host="127.0.0.1",
            listen_port=19102,
            enabled=1,
        )

    def test_failure_summary_aggregates_only_safe_diagnostics(self):
        db.record_media_proxy_playback_attempt(
            instance_id=self.first,
            route_class="guangya_direct",
            method="GET",
            status_code=206,
            source="guangya",
            cache_hit=True,
            upstream_latency_ms=5,
            total_latency_ms=10,
            media_name="PRIVATE MOVIE",
        )
        db.record_media_proxy_playback_attempt(
            instance_id=self.first,
            route_class="guangya_direct",
            method="GET",
            status_code=502,
            source="guangya",
            cache_hit=False,
            upstream_latency_ms=15,
            total_latency_ms=20,
            failure_stage="signed_url",
            error="PRIVATE https://secret.invalid/file?token=SECRET",
            media_name="PRIVATE MOVIE",
        )
        db.record_media_proxy_playback_attempt(
            instance_id=self.first,
            route_class="upstream_transcode",
            method="GET",
            status_code=0,
            source="upstream",
            cache_hit=False,
            upstream_latency_ms=25,
            total_latency_ms=30,
            failure_stage="upstream",
            error="PRIVATE /private/path",
        )
        db.record_media_proxy_playback_attempt(
            instance_id=self.second,
            route_class="upstream_direct",
            method="GET",
            status_code=200,
            source="upstream",
            total_latency_ms=40,
        )

        summary = db.get_media_proxy_playback_failure_summary(
            hours=24, instance_id=self.first
        )
        self.assertEqual(summary["total_recorded"], 3)
        self.assertEqual(summary["failed"], 2)
        self.assertEqual(summary["success"], 1)
        self.assertEqual(summary["cache_hits"], 1)
        self.assertEqual(summary["average_latency_ms"], 20.0)
        self.assertEqual(
            {item["stage"] for item in summary["failure_stages"]},
            {"signed_url", "upstream"},
        )
        serialized = json.dumps(summary, ensure_ascii=False)
        for secret in ("PRIVATE", "secret.invalid", "SECRET", "/private"):
            self.assertNotIn(secret, serialized)

    def test_failure_summary_rejects_unbounded_windows(self):
        with self.assertRaises(ValueError):
            db.get_media_proxy_playback_failure_summary(hours=48)
