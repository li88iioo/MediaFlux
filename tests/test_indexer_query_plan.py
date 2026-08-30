from __future__ import annotations

import unittest

from app.indexers.models import IndexerMediaSearchRequest
from app.indexers.query_plan import build_site_queries


class IndexerQueryPlanTests(unittest.TestCase):
    def setUp(self):
        self.request = IndexerMediaSearchRequest.create(
            title="奇招百出的维多利亚",
            original_title="手札が多めのビクトリア",
            english_title="Victoria of Many Faces",
            aliases=["Tefuda ga Oome no Victoria", "Tefuda ga Oume no Victoria"],
            year=2026,
            media_type="tv",
        )

    def test_chinese_sites_start_with_localized_title_without_year(self):
        for site_id in ("mikan", "1lou", "btbtla"):
            with self.subTest(site_id=site_id):
                queries = build_site_queries(site_id, self.request)
                self.assertEqual(queries[0], "奇招百出的维多利亚")
                self.assertLessEqual(len(queries), 3)
                self.assertNotIn("2026", " ".join(queries))

    def test_anime_sites_prefer_latin_alias_then_original_title(self):
        queries = build_site_queries("nyaa", self.request)

        self.assertEqual(queries[0], "Tefuda ga Oome no Victoria")
        self.assertIn("手札が多めのビクトリア", queries)
        self.assertLessEqual(len(queries), 3)

    def test_tpb_uses_english_and_latin_only_when_available(self):
        queries = build_site_queries("tpb", self.request)

        self.assertEqual(queries[0], "Victoria of Many Faces")
        self.assertTrue(all(not any("\u3400" <= char <= "\u9fff" for char in query) for query in queries))
        self.assertLessEqual(len(queries), 3)

    def test_sukebei_prefers_original_title_and_deduplicates_casefolded_aliases(self):
        request = IndexerMediaSearchRequest.create(
            title="Demo",
            original_title="デモ作品",
            aliases=["DEMO", "demo", "Demo Alternative"],
        )

        queries = build_site_queries("sukebei", request)

        self.assertEqual(queries[0], "デモ作品")
        self.assertEqual(sum(query.casefold() == "demo" for query in queries), 1)

    def test_episode_intent_is_queried_before_broad_title_aliases(self):
        request = IndexerMediaSearchRequest.create(
            title="九门",
            english_title="Mystic Nine",
            aliases=["The Mystic Nine"],
            media_type="tv",
            season=2,
            episode=30,
        )

        self.assertEqual(
            build_site_queries("1lou", request),
            ("九门 S02E30", "九门 第2季 第30集", "九门"),
        )
        self.assertEqual(
            build_site_queries("nyaa", request),
            ("The Mystic Nine S02E30", "Mystic Nine S02E30", "九门 S02E30"),
        )

    def test_unknown_site_falls_back_to_stable_input_order(self):
        self.assertEqual(
            build_site_queries("custom", self.request),
            (
                "奇招百出的维多利亚",
                "手札が多めのビクトリア",
                "Victoria of Many Faces",
            ),
        )


if __name__ == "__main__":
    unittest.main()
