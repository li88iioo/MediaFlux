from __future__ import annotations

import unittest

from app.indexers.release import parse_indexer_release_position, release_covers_target


class IndexerReleasePositionTests(unittest.TestCase):
    def test_binds_chinese_episode_range_to_season_marker_elsewhere_in_title(self):
        position = parse_indexer_release_position(
            "九门[第29-30集][国语配音/中文字幕].Mystic.Nine.S02.1080p.WEB-DL"
        )

        self.assertEqual(position, {"season": 2, "episode": 29, "episode_end": 30})
        match, _position = release_covers_target(
            "九门[第29-30集].Mystic.Nine.S02.1080p",
            season=2,
            episode=30,
        )
        self.assertEqual(match, "range")

    def test_chinese_single_episode_and_complete_pack_are_exposed(self):
        self.assertEqual(
            parse_indexer_release_position("九门[第30集].Mystic.Nine.S01.2026.2160p"),
            {"season": 1, "episode": 30, "episode_end": None},
        )
        self.assertEqual(
            parse_indexer_release_position("九门[全30集].Mystic.Nine.S02.2026.2160p"),
            {"season": 2, "episode": 1, "episode_end": 30},
        )

    def test_position_conflicts_are_distinguished_from_unknown_titles(self):
        conflict, _ = release_covers_target(
            "九门[第30集].Mystic.Nine.S01.2026.2160p",
            season=2,
            episode=30,
        )
        unknown, _ = release_covers_target("九门 2026 2160p", season=2, episode=30)

        self.assertEqual(conflict, "conflict")
        self.assertEqual(unknown, "unknown")


if __name__ == "__main__":
    unittest.main()
