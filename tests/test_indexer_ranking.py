from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.indexers.models import IndexerItem, IndexerMediaSearchRequest
from app.indexers.ranking import annotate_clusters, rank_item


class IndexerRankingTests(unittest.TestCase):
    @staticmethod
    def _item(title: str, *, site_id: str = "1lou") -> IndexerItem:
        return IndexerItem(
            site_id=site_id,
            site_name=site_id.upper(),
            title=title,
            download_state="resolvable",
            download_kinds=("torrent",),
            published_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
        )

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
