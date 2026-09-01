from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app import database
from app.discovery.cache import DiscoveryCache
from app.discovery.models import DiscoveryPage, MediaCard, ProviderHealth


class DiscoveryModelTests(unittest.TestCase):
    def test_media_card_round_trip_preserves_provider_identity(self):
        card = MediaCard(
            provider="douban",
            external_id="1292052",
            media_type="movie",
            title="肖申克的救赎",
            original_title="The Shawshank Redemption",
            year="1994",
            overview="希望让人自由。",
            poster_key="douban/movie/1292052",
            rating=9.7,
            rating_source="douban",
            release_date="1994-09-10",
            tmdb_id="278",
            douban_id="1292052",
            state="watchlisted",
        )
        restored = MediaCard.from_dict(card.to_dict())
        self.assertEqual(restored, card)
        self.assertEqual(restored.stable_id, "douban:movie:1292052")

    def test_media_card_rejects_unknown_media_type(self):
        with self.assertRaises(ValueError):
            MediaCard(provider="tmdb", external_id="1", media_type="book", title="x")

    def test_discovery_page_serializes_provider_health(self):
        page = DiscoveryPage(
            items=[MediaCard(provider="tmdb", external_id="1", media_type="movie", title="A")],
            page=1,
            has_more=True,
            cached=True,
            stale=False,
            provider=ProviderHealth(name="tmdb", status="healthy"),
        )
        payload = page.to_dict()
        self.assertEqual(payload["provider"], {"name": "tmdb", "status": "healthy", "message": "", "retry_after": 0})
        self.assertEqual(payload["items"][0]["stable_id"], "tmdb:movie:1")
        with self.assertRaises(AttributeError):
            page.items.append(MediaCard(provider="tmdb", external_id="2", media_type="movie", title="B"))


class DiscoveryDatabaseTests(unittest.TestCase):
    def test_init_db_creates_discovery_tables_and_unique_constraints(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "discovery.db"
            with patch("app.database.DB_PATH", test_db):
                database.init_db()
                with database.get_conn() as conn:
                    tables = {
                        row["name"] for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    self.assertTrue({"discovery_cache", "media_external_ids", "media_watchlist"} <= tables)
                    database.add_media_watchlist("tmdb", "1", "movie", "A", "2026", "poster")
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(
                            "INSERT INTO media_watchlist(provider,external_id,media_type,title,year,poster_key,created_at) "
                            "VALUES(?,?,?,?,?,?,?)",
                            ("tmdb", "1", "movie", "A", "2026", "poster", database.now()),
                        )
                    conn.execute(
                        "INSERT INTO discovery_cache(cache_key,provider,payload,fetched_at,expires_at,stale_until) "
                        "VALUES('same','tmdb','{}','2026-01-01','2026-01-02','2026-01-03')"
                    )
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(
                            "INSERT INTO discovery_cache(cache_key,provider,payload,fetched_at,expires_at,stale_until) "
                            "VALUES('same','tmdb','{}','2026-01-01','2026-01-02','2026-01-03')"
                        )
                    conn.execute(
                        "INSERT INTO media_external_ids(provider,external_id,media_type,updated_at) "
                        "VALUES('douban','7','movie',?)", (database.now(),)
                    )
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(
                            "INSERT INTO media_external_ids(provider,external_id,media_type,updated_at) "
                            "VALUES('douban','7','movie',?)", (database.now(),)
                        )

    def test_watchlist_and_external_mapping_helpers_are_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            test_db = Path(root) / "discovery.db"
            with patch("app.database.DB_PATH", test_db):
                database.init_db()
                database.add_media_watchlist("bangumi", "42", "tv", "番组", "2026", "bgm/42")
                database.add_media_watchlist("bangumi", "42", "tv", "番组更新", "2026", "bgm/42")
                self.assertEqual(database.list_media_watchlist_keys([("bangumi", "42", "tv")]), {"bangumi:tv:42"})
                self.assertEqual(len(database.list_media_watchlist()), 1)
                self.assertTrue(database.delete_media_watchlist("bangumi", "42", "tv"))
                self.assertFalse(database.delete_media_watchlist("bangumi", "42", "tv"))

                database.upsert_media_external_id("douban", "7", "movie", "550", "Movie", "1999", 0.95, False)
                database.upsert_media_external_id("douban", "7", "movie", "551", "Wrong", "2000", 0.99, False)
                row = database.get_media_external_id("douban", "7", "movie")
                self.assertEqual(row["tmdb_id"], "551")
                database.upsert_media_external_id("douban", "7", "movie", "550", "Confirmed", "1999", 1.0, True)
                database.upsert_media_external_id("douban", "7", "movie", "999", "Auto", "2020", 1.0, False)
                row = database.get_media_external_id("douban", "7", "movie")
                self.assertEqual((row["tmdb_id"], row["confirmed"]), ("550", 1))


class DiscoveryCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "cache.db"
        self.db_patch = patch("app.database.DB_PATH", self.db_path)
        self.db_patch.start()
        database.init_db()
        self.now = datetime(2026, 7, 25, 12, 0, 0)
        self.cache = DiscoveryCache(clock=lambda: self.now)

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_cache_key_is_stable_for_filter_order(self):
        first = self.cache.make_key("tmdb", "discover", "movie", 1, {"genre": "16", "language": "ja"})
        second = self.cache.make_key("tmdb", "discover", "movie", 1, {"language": "ja", "genre": "16"})
        self.assertEqual(first, second)
        self.assertNotEqual(first, self.cache.make_key("tmdb", "discover", "movie", 2, {"genre": "16", "language": "ja"}))
        self.assertNotEqual(first, self.cache.make_key("tmdb", "discover", "movie", 1, {"genre": "35", "language": "ja"}))

    def test_cache_distinguishes_fresh_stale_and_expired(self):
        key = self.cache.make_key("tmdb", "popular", "movie", 1, {})
        self.cache.set_success(key, "tmdb", {"items": [{"title": "A"}]}, ttl_seconds=60, stale_seconds=180)
        fresh = self.cache.get(key)
        self.assertEqual((fresh.status, fresh.payload["items"][0]["title"]), ("fresh", "A"))

        self.now += timedelta(seconds=61)
        stale = self.cache.get(key)
        self.assertEqual(stale.status, "stale")

        self.now += timedelta(seconds=121)
        expired = self.cache.get(key)
        self.assertEqual(expired.status, "expired")
        self.assertIsNone(expired.payload)

    def test_error_write_preserves_usable_stale_payload(self):
        key = self.cache.make_key("douban", "movie_hot", "movie", 1, {})
        self.cache.set_success(key, "douban", {"items": [{"title": "old"}]}, ttl_seconds=1, stale_seconds=100)
        self.now += timedelta(seconds=2)
        self.cache.set_error(key, "douban", "timeout", ttl_seconds=10)
        lookup = self.cache.get(key)
        self.assertEqual((lookup.status, lookup.payload["items"][0]["title"], lookup.last_error), ("stale", "old", "timeout"))

    def test_error_write_replaces_payload_after_stale_window(self):
        key = self.cache.make_key("tmdb", "popular", "movie", 1, {})
        self.cache.set_success(
            key, "tmdb", {"items": [{"title": "expired"}]},
            ttl_seconds=1, stale_seconds=10,
        )
        self.now += timedelta(seconds=11)

        self.cache.set_error(
            key, "tmdb", "invalid upstream payload", ttl_seconds=30,
            code="invalid_response", status_code=502,
        )

        lookup = self.cache.get(key)
        self.assertEqual(lookup.status, "error")
        self.assertIsNone(lookup.payload)
        self.assertEqual((lookup.error_code, lookup.status_code), ("invalid_response", 502))

    def test_error_only_cache_is_not_reported_as_fresh_success(self):
        key = self.cache.make_key("tmdb", "popular", "movie", 1, {})
        self.cache.set_error(
            key, "tmdb", "api_key=secret-value", ttl_seconds=30,
            code="rate_limited", status_code=429, retry_after=42,
        )
        lookup = self.cache.get(key)
        self.assertEqual(lookup.status, "error")
        self.assertEqual((lookup.error_code, lookup.status_code, lookup.retry_after), ("rate_limited", 429, 42))
        self.assertIsNone(lookup.payload)
        self.assertNotIn("secret-value", lookup.last_error)
        self.now += timedelta(seconds=31)
        self.assertEqual(self.cache.get(key).status, "expired")

    def test_malformed_payload_is_a_cache_miss(self):
        key = "bad"
        database.upsert_discovery_cache(
            key, "tmdb", "{bad json", self.now.strftime("%Y-%m-%d %H:%M:%S"),
            (self.now + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S"),
            (self.now + timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S"), "",
        )
        self.assertEqual(self.cache.get(key).status, "miss")

    def test_purge_removes_expired_entries_and_caps_oldest_rows(self):
        with database.get_conn() as conn:
            rows = [
                (
                    "expired", "tmdb", "{}", "2026-01-01 00:00:00",
                    "2026-01-02 00:00:00", "2026-01-03 00:00:00",
                ),
                (
                    "old-live", "tmdb", "{}", "2026-06-01 00:00:00",
                    "2026-12-01 00:00:00", "2026-12-02 00:00:00",
                ),
                (
                    "new-live", "tmdb", "{}", "2026-07-01 00:00:00",
                    "2026-12-01 00:00:00", "2026-12-02 00:00:00",
                ),
            ]
            conn.executemany(
                "INSERT INTO discovery_cache("
                "cache_key,provider,payload,fetched_at,expires_at,stale_until"
                ") VALUES(?,?,?,?,?,?)",
                rows,
            )

        deleted = database.purge_discovery_cache(
            "2026-07-25 12:00:00", max_rows=1, batch_size=10
        )

        self.assertEqual(deleted, 2)
        with database.get_conn() as conn:
            remaining = [
                row["cache_key"]
                for row in conn.execute(
                    "SELECT cache_key FROM discovery_cache ORDER BY cache_key"
                )
            ]
        self.assertEqual(remaining, ["new-live"])

    def test_cache_maintenance_is_rate_limited(self):
        with patch.object(database, "purge_discovery_cache") as purge:
            self.cache.set_success(
                "one", "tmdb", {"items": []},
                ttl_seconds=60, stale_seconds=120,
            )
            self.cache.set_success(
                "two", "tmdb", {"items": []},
                ttl_seconds=60, stale_seconds=120,
            )
            self.assertEqual(purge.call_count, 1)

            self.now += timedelta(hours=1, seconds=1)
            self.cache.set_error("three", "tmdb", "timeout")

        self.assertEqual(purge.call_count, 2)

    def test_singleflight_serializes_same_key(self):
        entered: list[str] = []
        first_ready = threading.Event()
        release_first = threading.Event()

        def first():
            with self.cache.singleflight("same"):
                entered.append("first")
                first_ready.set()
                release_first.wait(1)

        def second():
            first_ready.wait(1)
            with self.cache.singleflight("same"):
                entered.append("second")

        a = threading.Thread(target=first)
        b = threading.Thread(target=second)
        a.start(); b.start()
        first_ready.wait(1)
        self.assertEqual(entered, ["first"])
        release_first.set()
        a.join(1); b.join(1)
        self.assertEqual(entered, ["first", "second"])
        self.assertEqual(self.cache._locks, {})
        self.assertEqual(self.cache._lock_users, {})

    def test_singleflight_cleans_up_after_exception(self):
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with self.cache.singleflight("error"):
                raise RuntimeError("boom")
        self.assertEqual(self.cache._locks, {})
        self.assertEqual(self.cache._lock_users, {})


class ProviderErrorSafetyTests(unittest.TestCase):
    def test_safe_message_redacts_credentials(self):
        from app.discovery.models import ProviderUnavailable
        error = ProviderUnavailable(
            "request failed https://api.example.test/path?api_key=secret-value&_sig=signed Authorization: Bearer token-value"
        )
        self.assertNotIn("secret-value", error.safe_message)
        self.assertNotIn("signed", error.safe_message)
        self.assertNotIn("token-value", error.safe_message)
        self.assertIn("********", error.safe_message)


if __name__ == "__main__":
    unittest.main()
