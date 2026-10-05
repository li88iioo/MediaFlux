from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.indexers.models import IndexerItem, IndexerMediaSearchRequest
from app.indexers.ranking import annotate_clusters, match_priority, rank_item


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

        self.assertIsNotNone(clustered[0].cluster_id)
        self.assertEqual(clustered[0].cluster_id, clustered[1].cluster_id)
        self.assertEqual([item.cluster_size for item in clustered], [2, 2])


if __name__ == "__main__":
    unittest.main()
