from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.indexers.errors import IndexerResultExpired, IndexerResultNotFound, IndexerValidationError
from app.indexers.models import AggregatedIndexerResult, IndexerItem, IndexerMediaSearchRequest, IndexerSearchRequest
from app.indexers.result_store import IndexerResultStore


class IndexerModelTests(unittest.TestCase):
    def test_search_request_normalizes_query_and_bounds_page(self):
        request = IndexerSearchRequest.create(
            "  葬送   的芙莉莲  ",
            page=3,
            media_type="TV",
        )
        self.assertEqual(request.query, "葬送 的芙莉莲")
        self.assertEqual(request.page, 3)
        self.assertEqual(request.media_type, "tv")

        for query in ("", "   ", "x" * 121):
            with self.subTest(query_length=len(query)):
                with self.assertRaises(IndexerValidationError):
                    IndexerSearchRequest.create(query, page=1)
        for page in (0, 101):
            with self.subTest(page=page):
                with self.assertRaises(IndexerValidationError):
                    IndexerSearchRequest.create("valid", page=page)
        with self.assertRaises(IndexerValidationError):
            IndexerSearchRequest.create("valid", media_type="person")

    def test_media_search_request_normalizes_titles_aliases_and_metadata(self):
        request = IndexerMediaSearchRequest.create(
            title="  奇招百出的维多利亚  ",
            original_title="手札が多めのビクトリア",
            english_title=" Victoria   of Many Faces ",
            aliases=[
                " Tefuda ga Oome no Victoria ",
                "Tefuda ga Oome no Victoria",
                "奇招百出的维多利亚",
            ],
            year=2026,
            media_type="TV",
            page=2,
        )

        self.assertEqual(request.title, "奇招百出的维多利亚")
        self.assertEqual(request.original_title, "手札が多めのビクトリア")
        self.assertEqual(request.english_title, "Victoria of Many Faces")
        self.assertEqual(request.aliases, ("Tefuda ga Oome no Victoria",))
        self.assertEqual(request.year, 2026)
        self.assertEqual(request.media_type, "tv")
        self.assertEqual(request.page, 2)
        self.assertEqual(
            request.cache_identity(),
            (
                "奇招百出的维多利亚",
                "手札が多めのビクトリア",
                "Victoria of Many Faces",
                ("Tefuda ga Oome no Victoria",),
                2026,
                "tv",
            ),
        )

    def test_media_search_request_rejects_unbounded_or_invalid_metadata(self):
        invalid_cases = (
            {"title": ""},
            {"title": "x" * 121},
            {"title": "Demo", "aliases": [f"alias-{index}" for index in range(9)]},
            {"title": "Demo", "aliases": ["x" * 121]},
            {"title": "Demo", "year": 1799},
            {"title": "Demo", "year": True},
            {"title": "Demo", "media_type": "person"},
            {"title": "Demo", "page": 0},
        )
        for values in invalid_cases:
            with self.subTest(values=values):
                with self.assertRaises(IndexerValidationError):
                    IndexerMediaSearchRequest.create(**values)

    def test_aggregated_result_clone_preserves_site_item_counts(self):
        result = AggregatedIndexerResult(
            query="Demo",
            page=1,
            items=[],
            sites_attempted=("nyaa", "mikan"),
            sites_succeeded=("nyaa", "mikan"),
            site_item_counts={"nyaa": 1, "mikan": 1},
        )

        cloned = result.clone(cached=True)

        self.assertEqual(cloned.site_item_counts, {"nyaa": 1, "mikan": 1})
        self.assertIsNot(cloned.site_item_counts, result.site_item_counts)

    def test_public_item_hides_upstream_download_locations(self):
        item = IndexerItem(
            site_id="nyaa",
            site_name="Nyaa",
            title="Example",
            detail_url="https://nyaa.si/view/1",
            magnet="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
            torrent_url="https://nyaa.si/download/1.torrent",
            result_id="opaque-token",
            download_state="ready",
            download_kinds=("magnet", "torrent"),
        )

        payload = item.to_public_dict()

        self.assertEqual(payload["result_id"], "opaque-token")
        self.assertEqual(payload["download_kinds"], ["magnet", "torrent"])
        self.assertNotIn("magnet", payload)
        self.assertNotIn("torrent_url", payload)
        self.assertNotIn("detail_url", payload)
        self.assertIsNone(payload["relevance_score"])
        self.assertEqual(payload["match_reasons"], [])
        self.assertIsNone(payload["cluster_id"])
        self.assertEqual(payload["cluster_size"], 1)


class IndexerResultStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
        self.store = IndexerResultStore(ttl_seconds=60, max_entries=2, clock=lambda: self.now)

    @staticmethod
    def item(title: str) -> IndexerItem:
        return IndexerItem(site_id="nyaa", site_name="Nyaa", title=title, download_state="ready")

    def test_put_returns_opaque_unique_ids_and_round_trips_copy(self):
        first_id = self.store.put(self.item("one"))
        second_id = self.store.put(self.item("two"))

        self.assertNotEqual(first_id, second_id)
        self.assertNotIn("one", first_id)
        self.assertGreaterEqual(len(first_id), 24)
        loaded = self.store.get(first_id)
        self.assertEqual(loaded.title, "one")
        loaded.title = "mutated"
        self.assertEqual(self.store.get(first_id).title, "one")

    def test_expired_and_unknown_ids_have_distinct_safe_errors(self):
        result_id = self.store.put(self.item("one"))
        self.now += timedelta(seconds=61)

        with self.assertRaises(IndexerResultExpired):
            self.store.get(result_id)
        with self.assertRaises(IndexerResultNotFound):
            self.store.get("missing-result-id")

    def test_capacity_evicts_oldest_entry(self):
        first_id = self.store.put(self.item("one"))
        self.now += timedelta(seconds=1)
        second_id = self.store.put(self.item("two"))
        self.now += timedelta(seconds=1)
        third_id = self.store.put(self.item("three"))

        with self.assertRaises(IndexerResultNotFound):
            self.store.get(first_id)
        self.assertEqual(self.store.get(second_id).title, "two")
        self.assertEqual(self.store.get(third_id).title, "three")


if __name__ == "__main__":
    unittest.main()
