"""本地整理识别摘要聚合与历史回退契约。"""
from __future__ import annotations

import unittest

from app.modules.local_media_recognition_summary import (
    build_recognition_summary,
    infer_recognition_summary,
    parse_recognition_summary,
    serialize_recognition_summary,
)
from tests.support import release_parse_result


class _HistoryScraper:
    def parse_media(self, filename: str, parent_path: str = ""):
        return release_parse_result(
            {"title": "骸骨骑士大人异世界冒险中", "year": "2022", "type": "tv",
             "season": 2, "episode": 8},
            filename=filename, parent_path=parent_path,
        )


class LocalMediaRecognitionSummaryTests(unittest.TestCase):
    def test_same_show_episodes_are_aggregated_without_losing_positions(self):
        summary = build_recognition_summary([
            {"tmdb_id": "1235283", "title": "骸骨骑士大人异世界冒险中", "year": "2022",
             "media_type": "tv", "category": "anime", "season": 2, "episode": 7},
            {"tmdb_id": "1235283", "title": "骸骨骑士大人异世界冒险中", "year": "2022",
             "media_type": "tv", "category": "anime", "season": 2, "episode": 8},
        ])
        self.assertEqual(summary["status"], "resolved")
        self.assertEqual(summary["file_count"], 2)
        self.assertEqual(summary["media"][0]["categories"], ["anime"])
        self.assertEqual(summary["media"][0]["seasons"][0]["episodes"], [7, 8])
        self.assertEqual(
            parse_recognition_summary(serialize_recognition_summary(summary)), summary,
        )

    def test_different_tmdb_identities_are_not_collapsed(self):
        summary = build_recognition_summary([
            {"tmdb_id": "1", "title": "电影甲", "media_type": "movie"},
            {"tmdb_id": "2", "title": "电影乙", "media_type": "movie"},
        ])
        self.assertEqual(summary["status"], "multiple")
        self.assertEqual(summary["media_count"], 2)

    def test_history_inference_requires_unambiguous_explicit_tmdb_marker(self):
        rows = [{
            "role": "video",
            "target_path": "/media/动漫/骸骨骑士大人异世界冒险中 (2022) {tmdb-1235283}/Season 2/"
                           "骸骨骑士大人异世界冒险中.2022.S02E08-WEB-DL.1080p.mp4",
        }]
        summary = infer_recognition_summary(rows, scraper=_HistoryScraper())
        self.assertEqual(summary["source"], "history_inferred")
        self.assertEqual(summary["media"][0]["tmdb_id"], "1235283")
        self.assertEqual(summary["media"][0]["seasons"][0]["episodes"], [8])

        conflicted = [{
            "role": "video",
            "target_path": "/media/电影/错误 {tmdb-1}/影片.{tmdb-2}.mkv",
        }]
        self.assertEqual(
            infer_recognition_summary(conflicted, scraper=_HistoryScraper()), {},
        )


if __name__ == "__main__":
    unittest.main()
