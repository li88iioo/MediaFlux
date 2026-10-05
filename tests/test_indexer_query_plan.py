from __future__ import annotations

import unittest

from app.indexers.models import IndexerMediaSearchRequest
from app.indexers.query_plan import (
    build_site_queries,
    enrich_media_aliases,
    needs_bilingual_search,
)


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
        for site_id in ("mikan", "btbtla"):
            with self.subTest(site_id=site_id):
                queries = build_site_queries(site_id, self.request)
                self.assertEqual(queries[0], "奇招百出的维多利亚")
                self.assertLessEqual(len(queries), 3)
                self.assertNotIn("2026", " ".join(queries))

    def test_nyaa_preserves_japanese_original_title_in_existing_plan(self):
        queries = build_site_queries("nyaa", self.request)

        self.assertEqual(
            queries[:2],
            ("Tefuda ga Oome no Victoria", "手札が多めのビクトリア"),
        )
        self.assertIn("手札が多めのビクトリア", queries)
        self.assertFalse(needs_bilingual_search(queries))
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

    def test_nyaa_episode_search_keeps_chinese_broad_query_second(self):
        request = IndexerMediaSearchRequest.create(
            title="九门",
            english_title="Mystic Nine",
            aliases=["The Mystic Nine"],
            media_type="tv",
            season=2,
            episode=30,
        )

        self.assertEqual(
            build_site_queries("btbtla", request),
            ("九门 S02E30", "九门 第2季 第30集", "九门"),
        )
        self.assertEqual(
            build_site_queries("nyaa", request),
            ("The Mystic Nine S02E30", "九门", "Mystic Nine"),
        )
        self.assertTrue(needs_bilingual_search(build_site_queries("nyaa", request)))

    def test_bilingual_search_predicate_requires_two_queries_in_different_scripts(self):
        self.assertTrue(needs_bilingual_search(("The Great Ruler", "大主宰")))
        self.assertFalse(needs_bilingual_search(("Renegade Immortal", "仙逆の物語")))
        self.assertFalse(needs_bilingual_search(("The Great Ruler", "Perfect World")))
        self.assertFalse(needs_bilingual_search(("The Great Ruler", "大主宰 Renegade Immortal")))
        self.assertFalse(needs_bilingual_search(("大主宰",)))

    def test_nyaa_prefers_original_cjk_title_for_the_second_broad_query(self):
        request = IndexerMediaSearchRequest.create(
            title="本地标题",
            original_title="原題",
            aliases=["Romanized Alias", "English Alias"],
            media_type="tv",
        )

        queries = build_site_queries("nyaa", request)

        self.assertEqual(queries, ("Romanized Alias", "原題", "English Alias"))
        self.assertTrue(needs_bilingual_search(queries))

    def test_known_chinese_title_gets_exact_builtin_aliases_without_mutation(self):
        request = IndexerMediaSearchRequest.create(
            title="凡人修仙传",
            year=2024,
            media_type="tv",
            page=3,
            sort_mode="seeders_desc",
            season=1,
            episode=192,
        )

        enriched = enrich_media_aliases(request)

        self.assertIsNot(enriched, request)
        self.assertEqual(
            enriched.aliases,
            ("A Record of a Mortal's Journey to Immortality", "Fanren Xiu Xian Chuan"),
        )
        self.assertEqual(request.aliases, ())
        self.assertEqual(
            enriched.cache_identity(),
            (
                request.title,
                request.original_title,
                request.english_title,
                enriched.aliases,
                request.year,
                request.media_type,
                request.sort_mode,
                request.season,
                request.episode,
            ),
        )
        self.assertEqual((enriched.page, enriched.year, enriched.season, enriched.episode), (3, 2024, 1, 192))
        self.assertEqual(
            build_site_queries("nyaa", request),
            ("A Record of a Mortal's Journey to Immortality S01E192", "凡人修仙传", "Fanren Xiu Xian Chuan"),
        )
        self.assertLessEqual(len(build_site_queries("nyaa", request)), 3)

    def test_chinese_short_word_does_not_match_longer_builtin_title(self):
        request = IndexerMediaSearchRequest.create(title="凡人")

        enriched = enrich_media_aliases(request)

        self.assertEqual(enriched.aliases, ())
        self.assertEqual(build_site_queries("nyaa", request), ("凡人",))

    def test_explicit_latin_aliases_take_precedence_over_builtin_aliases(self):
        request = IndexerMediaSearchRequest.create(
            title="凡人修仙传",
            english_title="Caller English Title",
            aliases=["Caller Latin Alias"],
            media_type="tv",
            season=2,
            episode=4,
        )

        enriched = enrich_media_aliases(request)

        self.assertEqual(enriched.aliases, request.aliases)
        self.assertEqual(
            build_site_queries("nyaa", request),
            ("Caller Latin Alias S02E04", "凡人修仙传", "Caller English Title"),
        )

    def test_movie_and_unknown_chinese_titles_are_not_guessed(self):
        movie = IndexerMediaSearchRequest.create(title="凡人修仙传", media_type="movie")
        unknown = IndexerMediaSearchRequest.create(title="未知国漫标题", media_type="tv")

        self.assertEqual(enrich_media_aliases(movie).aliases, ())
        self.assertEqual(build_site_queries("nyaa", movie), ("凡人修仙传",))
        self.assertEqual(enrich_media_aliases(unknown).aliases, ())
        self.assertEqual(build_site_queries("nyaa", unknown), ("未知国漫标题",))

    def test_anime_aliases_are_not_added_to_other_sites(self):
        request = IndexerMediaSearchRequest.create(title="仙逆", media_type="tv")

        self.assertEqual(build_site_queries("mikan", request), ("仙逆",))
        self.assertEqual(build_site_queries("tpb", request), ("仙逆",))

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
