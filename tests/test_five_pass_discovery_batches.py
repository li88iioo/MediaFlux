"""大页/重复候选的收藏标记保持完整且不突破 SQLite 表达式深度。"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from app import database as db
from app.discovery.cache import DiscoveryCache
from app.discovery.models import DiscoveryPage, MediaCard
from app.discovery.registry import ProviderRegistry
from app.discovery.service import DiscoveryService
from tests.support import IsolatedDatabaseTestCase, isolated_test_database


class DiscoveryIdentityBatchTests(IsolatedDatabaseTestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.service = DiscoveryService(
            registry=ProviderRegistry({}), cache=DiscoveryCache(),
        )
        self.addCleanup(self.service.shutdown)
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO media_watchlist(provider,external_id,media_type,title,created_at) VALUES(?,?,?,?,?)",
                [("tmdb", str(index), "tv", f"Show {index}", db.now()) for index in range(0, 1200, 2)],
            )

    def test_large_page_decorates_all_cards_without_mutating_cached_page(self):
        page = DiscoveryPage(items=tuple(
            MediaCard("tmdb", str(index), "tv", f"Show {index}") for index in range(1200)
        ))
        decorated = self.service._decorate(page, cached=True, stale=False)
        self.assertEqual(len(decorated.items), 1200)
        self.assertEqual(sum(item.state == "watchlisted" for item in decorated.items), 600)
        self.assertEqual([item.external_id for item in decorated.items], [str(i) for i in range(1200)])
        self.assertTrue(all(item.state == "none" for item in page.items))
        db.delete_media_watchlist("tmdb", "1198", "tv")
        again = self.service._decorate(page, cached=True, stale=True)
        self.assertEqual(again.items[1198].state, "none")
        self.assertTrue(again.stale)

    def test_duplicate_identity_list_uses_one_bounded_lookup(self):
        queries = []
        real_conn = db.get_conn

        @contextmanager
        def traced():
            with real_conn() as conn:
                conn.set_trace_callback(queries.append)
                yield conn

        with patch.object(db, "get_conn", traced):
            keys = db.list_media_watchlist_keys([("tmdb", "2", "tv")] * 3000)
        self.assertEqual(keys, {"tmdb:tv:2"})
        selects = [sql for sql in queries if sql.startswith("SELECT ")]
        self.assertEqual(len(selects), 1)
        self.assertEqual(selects[0].count("provider="), 1)

    def test_empty_and_mixed_provider_type_identity_do_not_collide(self):
        db.add_media_watchlist("douban", "2", "movie", "Movie")
        self.assertEqual(db.list_media_watchlist_keys([]), set())
        self.assertEqual(db.list_media_watchlist_keys([
            ("tmdb", "2", "tv"), ("tmdb", "2", "movie"),
            ("douban", "2", "movie"), ("douban", "2", "tv"),
        ]), {"tmdb:tv:2", "douban:movie:2"})
