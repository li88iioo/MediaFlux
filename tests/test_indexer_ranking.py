from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import datetime, timezone

from app.indexers.models import IndexerItem, IndexerMediaSearchRequest
from app.indexers.ranking import annotate_clusters, match_priority, rank_item


class TechnicalFilterTests(unittest.TestCase):
    def test_excluding_hdr_keeps_unknown_distinct_from_explicit_sdr(self):
        from app.indexers.ranking import filter_technical_candidates

        items = [{'title': f'Demo.1080p.{suffix}', 'result_id': f'resource-{number}', 'position': number}
                 for number, suffix in enumerate(('H264', '[SDR]', 'HDR10', '[DV]', '[HLG]', '[SDR][HDR]'), 1)]
        original = deepcopy(items)
        selected, stats = filter_technical_candidates(items, {'exclude_hdr': True})
        self.assertEqual(selected, items[:2])
        self.assertEqual(stats, {'requested': {'exclude_hdr': True}, 'basis': 'release_title_only',
                                'considered': 6, 'scope': 'input_candidate_titles',
                                'unknown_excluded': 0, 'mismatched_excluded': 4, 'stream_verified': False})
        selected, stats = filter_technical_candidates(items, {'exclude_hdr': True, 'dynamic_range': 'SDR'})
        self.assertEqual(selected, items[1:2])
        self.assertIs(selected[0], items[1])
        self.assertEqual(stats['unknown_excluded'], 1)
        self.assertEqual(filter_technical_candidates(items, {'exclude_hdr': False})[0], items)
        self.assertEqual(items, original)

    def test_technical_arguments_use_plain_value_error_and_keep_explicit_resets(self):
        from app.indexers.ranking import technical_filter_arguments

        for field, values in (
            ('resolution', (['1080p'], '1080', None)),
            ('dynamic_range', (True, 'guessed SDR')),
            ('exclude_hdr', ('false', 'true', 0, 1, None, [])),
        ):
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError) as caught:
                    technical_filter_arguments({field: value})
                self.assertIs(type(caught.exception), ValueError)
                self.assertEqual(str(caught.exception), f'{field} 不在允许范围内')
        self.assertEqual(technical_filter_arguments({}), {})
        reset = {'resolution': '', 'dynamic_range': '', 'exclude_hdr': False}
        self.assertEqual(technical_filter_arguments({'title': 'Demo', **reset}), reset)

    def test_combined_hdr_evidence_uses_complete_release_title(self):
        from app.indexers.ranking import filter_technical_candidates

        for title, desired, matched in [
            ('Demo.2160p.DoVi.HDR10', 'HDR10', True),
            ('Demo.2160p.DoVi.HDR10', 'HDR', True),
            ('Demo.2160p.HLG', 'HDR', True),
            ('Demo.1080p.SDR.HDR10', 'SDR', False),
            ('Demo.' + 'a' * 350 + '.1080p.SDR', 'SDR', True),
            ('Demo.1080p.SDR.' + 'a' * 350 + '.HDR10', 'SDR', False),
            ('Demo.' + 'a' * 350 + '.2160p.DoVi.HDR10', 'HDR10', True),
        ]:
            with self.subTest(title=title, desired=desired):
                items, _ = filter_technical_candidates([{'title': title}], {'dynamic_range': desired})
                self.assertEqual(bool(items), matched)


class IndexerRankingTests(unittest.TestCase):
    @staticmethod
    def _item(title: str, *, site_id: str = "btbtla") -> IndexerItem:
        return IndexerItem(
            site_id=site_id,
            site_name=site_id.upper(),
            title=title,
            download_state="resolvable",
            download_kinds=("torrent",),
            published_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
        )

    def test_named_special_is_not_recommended_as_regular_or_numbered_special_episode(self):
        from app.modules.episode_resource_ranking import rank_episode_search

        title = "黑镜 第一季 / Black.Mirror.S02.Special.White.Christmas.1080p.WEBRip.DD2.0.x264-CasStudio[rartv]"
        item = self._item(title)
        for season in (2, 0):
            with self.subTest(season=season):
                media = IndexerMediaSearchRequest.create(title="黑镜", original_title="Black Mirror", media_type="tv", season=season, episode=1)
                ranked = rank_item(item, media=media, fallback_query="黑镜")
                self.assertNotIn("episode_exact", ranked.match_reasons)
                result = rank_episode_search({"items": [item.to_public_dict()]}, season=season, episode=1, media=media)
                self.assertNotEqual(result["items"][0]["quality"]["match"], "exact_episode")
                self.assertIsNone(result["recommendation"]["selected"])
                self.assertEqual(result["download_plan"]["result_ids"], [])

    def test_subtitle_evidence_is_a_language_marker_not_size_or_word_fragment(self):
        from app.modules.episode_resource_ranking import rank_episode_search

        media = IndexerMediaSearchRequest.create(title="Demo", media_type="tv", season=1, episode=1)
        for suffix, expected in (
            ("[7.19GB]", []), ("[7.19 GB]", []), ("[7.19GiB]", []), ("Monarchs", []),
            ("[GB]", ["含简体中文标记"]), ("[GB2312]", ["含简体中文标记"]),
            ("[CHS]", ["含简体中文标记"]), ("[CHT]", ["含繁体中文标记"]),
        ):
            with self.subTest(suffix=suffix):
                item = self._item(f"Demo.S01E01.1080p {suffix}")
                result = rank_episode_search({"items": [item.to_public_dict()]}, season=1, episode=1, media=media)
                reasons = result["items"][0]["quality"]["reasons"]
                self.assertEqual([reason for reason in reasons if "中文标记" in reason], expected)

    def test_other_season_pack_is_conflicting_in_both_ranking_stages(self):
        from app.modules.episode_resource_ranking import rank_episode_search

        for title in ("Demo.S06.COMPLETE.2160p.WEB-DL", "Demo.S04.2160p.WEB-DL"):
            for episode in (None, 1):
                with self.subTest(title=title, episode=episode):
                    item = self._item(title)
                    media = IndexerMediaSearchRequest.create(title="Demo", media_type="tv", season=2, episode=episode)
                    ranked = rank_item(item, media=media, fallback_query=media.title)
                    self.assertIn("episode_conflict", ranked.match_reasons)
                    if episode is not None:
                        result = rank_episode_search({"items": [item.to_public_dict()]}, season=2, episode=episode, media=media)
                        self.assertEqual(result["items"][0]["quality"]["match"], "conflict")
                        self.assertFalse(result["items"][0]["quality"]["eligible"])
                        self.assertIsNone(result["recommendation"]["selected"])
                        self.assertEqual(result["download_plan"]["result_ids"], [])

    def test_captioned_episode_is_ranked_before_truncating_search_results(self):
        from app.indexers.ranking import candidate_sort_key
        media = IndexerMediaSearchRequest.create(title="理想禁区", media_type="tv", season=1, episode=7)
        items = [rank_item(self._item(f"【国产】理想禁区 {n:02d} 分集标题 1080P"), media=media, fallback_query=media.title) for n in range(1, 12)]
        ranked = sorted(enumerate(items), key=lambda entry: candidate_sort_key((0, entry[0], entry[1]), "relevance_desc"))
        self.assertEqual(ranked[0][0], 6)
        first_score = ranked[0][1].relevance_score
        second_score = ranked[1][1].relevance_score
        assert first_score is not None and second_score is not None
        self.assertGreater(first_score, second_score)

    def test_match_priority_separates_identity_from_popularity_and_year_conflicts(self):
        media = IndexerMediaSearchRequest.create(title="起义", original_title="The Uprising", year=2026)
        cases = [
            ("起义.2026.1080p", 0),
            ("The.Uprising.2026.1080p", 0),
            ("起义.1080p", 1),
            ("起义.2024.1080p", 3),
            ("二二八起义.2026.1080p", 3),
            ("Pacific.Rim.Uprising.2018.2160p", 3),
        ]
        for title, expected in cases:
            with self.subTest(title=title):
                candidate = rank_item(self._item(title), media=media, fallback_query="起义")
                self.assertEqual(match_priority(candidate), expected)

    def test_year_extraction_ignores_marked_technical_values_only(self):
        from app.modules.episode_resource_ranking import rank_episode_search

        media = IndexerMediaSearchRequest.create(title="起义", year=2026)
        cases = [
            ("起义.1920x1080.WEB-DL", False, False, 1),
            ("起义.1080x1920.WEB-DL", False, False, 1),
            ("起义.1920×1080.WEB-DL", False, False, 1),
            ("起义.1920kbps.WEB-DL", False, False, 1),
            ("起义.1920.WEB-DL", True, False, 3),
            ("起义.1920.1920x1080.WEB-DL", True, False, 3),
            ("起义.2026.1920x1080.WEB-DL", False, True, 0),
            ("起义.S01E01.2160p.WEB-DL", False, False, 1),
            ("起义.S01E01.2160P.WEB-DL", False, False, 1),
            ("起义.2026.2160p.WEB-DL", False, True, 0),
        ]

        for title, has_conflict, has_match, expected_priority in cases:
            with self.subTest(title=title):
                ranked = rank_item(self._item(title), media=media, fallback_query="起义")
                self.assertEqual("year_conflict" in ranked.match_reasons, has_conflict)
                self.assertEqual("year_match" in ranked.match_reasons, has_match)
                self.assertEqual(match_priority(ranked), expected_priority)
                public = {**ranked.to_public_dict(), "result_id": "year-evidence-resource-001"}
                recommendation = rank_episode_search({"items": [public]}, season=1, episode=1, media=media)
                self.assertEqual(recommendation["items"][0]["quality"]["eligible"], not has_conflict)

    def test_later_tv_release_year_is_not_a_proven_identity_conflict(self):
        from app.modules.episode_resource_ranking import rank_episode_search

        for media_type, season, release_year, eligible in (
            ("tv", 2, 2012, True), ("tv", 1, 2012, True), ("", 2, 2012, True),
            ("tv", 2, 2010, False), ("movie", None, 2012, False),
        ):
            with self.subTest(media_type=media_type, season=season, release_year=release_year):
                media = IndexerMediaSearchRequest.create(title="Show", year=2011, media_type=media_type, season=season)
                item = rank_item(self._item(f"Show {release_year} S{season or 1:02d}E01 1080p"),
                                 media=media, fallback_query=media.title)
                self.assertEqual("year_conflict" in item.match_reasons, not eligible)
                self.assertEqual("year_unverified" in item.match_reasons, eligible)
                for context in (None, media):
                    ranked = rank_episode_search({"items": [{**item.to_public_dict(), "result_id": "season-year-resource-001"}]},
                                                 season=season or 1, episode=1, media=context)
                    self.assertEqual(ranked["items"][0]["quality"]["eligible"], eligible)
                    self.assertEqual(bool(ranked["recommendation"]["selected"]), eligible)
                    self.assertTrue(any("年份" in warning for warning in ranked["items"][0]["quality"]["warnings"]))

    def test_short_cjk_title_requires_a_title_boundary(self):
        media = IndexerMediaSearchRequest.create(
            title="九门",
            year=2026,
            media_type="tv",
            season=2,
            episode=30,
        )

        correct = rank_item(
            self._item("九门[第30集].Mystic.Nine.S02.2026.1080p"),
            media=media,
            fallback_query="九门",
            now=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )
        prefixed = rank_item(
            self._item("老九门.S02E30.2026.1080p"),
            media=media,
            fallback_query="九门",
            now=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )
        suffixed = rank_item(
            self._item("九门之外.S02E30.2026.1080p"),
            media=media,
            fallback_query="九门",
            now=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )

        self.assertGreaterEqual(correct.relevance_score or 0, 90)
        self.assertNotIn("title_contains", prefixed.match_reasons)
        self.assertNotIn("title_contains", suffixed.match_reasons)
        self.assertGreater(correct.relevance_score or 0, (prefixed.relevance_score or 0) + 40)

    def test_publisher_season_is_not_discarded_before_tmdb_mapping(self):
        media = IndexerMediaSearchRequest.create(
            title="沧元图",
            media_type="tv",
            season=1,
            episode=95,
        )

        ranked = rank_item(
            self._item("[GM-Team][国漫][沧元图 第3季][2026][26][GB][4K HEVC 10Bit]"),
            media=media,
            fallback_query="沧元图",
            now=datetime(2026, 9, 20, tzinfo=timezone.utc),
        )

        self.assertNotIn("episode_conflict", ranked.match_reasons)
        self.assertGreater(ranked.relevance_score or 0, 0)

    def test_bracket_only_titles_keep_media_identity_for_clustering(self):
        items = [
            self._item(
                "[GM-Team][国漫][牧神记][Tales of Qin Mu][2024][97][AVC][GB][1080P]",
                site_id="nyaa",
            ),
            self._item(
                "[Other-Team][牧神记][Tales of Qin Mu][2024][97][HEVC][1080P]",
                site_id="mikan",
            ),
        ]

        clustered = annotate_clusters(items)

        assert (clustered[0].cluster_id) is not None
        self.assertEqual(clustered[0].cluster_id, clustered[1].cluster_id)
        self.assertEqual([item.cluster_size for item in clustered], [2, 2])


if __name__ == "__main__":
    unittest.main()
