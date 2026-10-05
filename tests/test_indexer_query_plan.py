from __future__ import annotations

import unittest

from app.indexers.models import IndexerMediaSearchRequest
from app.indexers.query_plan import (
    build_site_queries,
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

    def test_chinese_titles_without_explicit_aliases_are_not_expanded(self):
        for title in ("凡人修仙传", "仙逆", "凡人", "未知国漫标题"):
            for media_type in ("tv", "movie", None):
                with self.subTest(title=title, media_type=media_type):
                    request = IndexerMediaSearchRequest.create(title=title, media_type=media_type)
                    for site in ("nyaa", "mikan", "tpb", "btbtla"):
                        self.assertEqual(build_site_queries(site, request), (title,))
                    self.assertEqual(request.aliases, ())

    def test_explicit_aliases_preserve_position_and_request_identity(self):
        request = IndexerMediaSearchRequest.create(
            title="凡人修仙传",
            english_title="Caller English Title",
            aliases=["Caller Latin Alias"],
            media_type="tv",
            season=2,
            episode=4,
        )
        identity = request.cache_identity()
        self.assertEqual(
            build_site_queries("nyaa", request),
            ("Caller Latin Alias S02E04", "凡人修仙传", "Caller English Title"),
        )
        self.assertEqual(request.cache_identity(), identity)

    def test_empire_queries_use_supported_complete_names_without_episode_suffixes(self):
        for site in ("dygang", "ys5266"):
            with self.subTest(site=site):
                request = IndexerMediaSearchRequest.create(
                    title="天地玄黄宇宙洪荒日月盈", original_title="An Unsupported Original Name",
                    english_title="A Second Very Long English Title",
                    aliases=["仙逆", "Renegade Immortal"], season=1, episode=1,
                )
                self.assertEqual(build_site_queries(site,request),("仙逆","Renegade Immortal"))
                self.assertEqual(request.title,"天地玄黄宇宙洪荒日月盈")
                self.assertEqual((request.season,request.episode),(1,1))

    def test_empire_without_supported_alias_retains_whole_query_for_explicit_rejection(self):
        request=IndexerMediaSearchRequest.create(title="天地玄黄宇宙洪荒日月盈")
        for site in ("dygang","ys5266"):
            self.assertEqual(build_site_queries(site,request),(request.title,))
        self.assertEqual(build_site_queries("btbtla",request),(request.title,))

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
