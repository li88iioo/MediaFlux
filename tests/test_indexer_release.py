from __future__ import annotations

import unittest

from app.indexers.release import parse_indexer_release_position
from app.modules.episode_mapping import classify_episode_position


class IndexerReleasePositionTests(unittest.TestCase):
    def test_captioned_bare_episode_requires_an_exact_known_media_title(self):
        title = "【国产】理想禁区 07 黑历史 1080P"
        self.assertIsNone(parse_indexer_release_position(title)["episode"])
        self.assertEqual(parse_indexer_release_position(title, media_title="理想禁区"), {"season": None, "episode": 7, "episode_end": None})
        for text, hint in ((title, "其他作品"), ("【国产】新理想禁区 07 黑历史 1080P", "理想禁区"), ("【国产】理想禁区 2026 中文字幕 1080P", "理想禁区"), ("理想禁区 1080P", "理想禁区"), ("【国产】第七禁区 07 中文字幕", "第七禁区 07")):
            with self.subTest(text=text, hint=hint):
                self.assertIsNone(parse_indexer_release_position(text, media_title=hint)["episode"])

    def test_binds_chinese_episode_range_to_season_marker_elsewhere_in_title(self):
        position = parse_indexer_release_position(
            "九门[第29-30集][国语配音/中文字幕].Mystic.Nine.S02.1080p.WEB-DL"
        )

        self.assertEqual(position, {"season": 2, "episode": 29, "episode_end": 30})
        match = classify_episode_position(
            source_season=position["season"],
            source_episode=position["episode"],
            source_episode_end=position["episode_end"],
            target_season=2,
            target_episode=30,
        )
        self.assertEqual(match.relation, "range")

    def test_chinese_single_episode_and_complete_pack_are_exposed(self):
        self.assertEqual(
            parse_indexer_release_position("九门[第30集].Mystic.Nine.S01.2026.2160p"),
            {"season": 1, "episode": 30, "episode_end": None},
        )
        self.assertEqual(
            parse_indexer_release_position("九门[全30集].Mystic.Nine.S02.2026.2160p"),
            {"season": 2, "episode": 1, "episode_end": 30},
        )

    def test_gm_team_positions_are_parsed_without_tmdb_assumptions(self):
        self.assertEqual(
            parse_indexer_release_position(
                "[GM-Team][国漫][师兄啊师兄][2026][159][GB][4K HEVC 10Bit]"
            ),
            {"season": None, "episode": 159, "episode_end": None},
        )
        self.assertEqual(
            parse_indexer_release_position(
                "[GM-Team][国漫][沧元图 第3季][2026][26][GB][4K HEVC 10Bit]"
            ),
            {"season": 3, "episode": 26, "episode_end": None},
        )

    def test_position_conflicts_are_distinguished_from_unknown_titles(self):
        conflict_position = parse_indexer_release_position(
            "九门[第30集].Mystic.Nine.S01.2026.2160p"
        )
        unknown_position = parse_indexer_release_position("九门 2026 2160p")
        conflict = classify_episode_position(
            source_season=conflict_position["season"],
            source_episode=conflict_position["episode"],
            source_episode_end=conflict_position["episode_end"],
            target_season=2,
            target_episode=30,
        )
        unknown = classify_episode_position(
            source_season=unknown_position["season"],
            source_episode=unknown_position["episode"],
            source_episode_end=unknown_position["episode_end"],
            target_season=2,
            target_episode=30,
        )

        self.assertEqual(conflict.relation, "conflict")
        self.assertEqual(unknown.relation, "unknown")


if __name__ == "__main__":
    unittest.main()
