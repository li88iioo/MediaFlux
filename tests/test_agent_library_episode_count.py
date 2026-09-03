"""Media Agent 本地剧集数量工具测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.agent.errors import AgentToolError
from app.agent.library_episode_count import (
    count_series_episodes,
    count_series_episodes_arguments,
)


class LibraryEpisodeCountArgumentsTests(unittest.TestCase):
    def test_normalizes_supported_arguments(self):
        self.assertEqual(
            count_series_episodes_arguments(
                {"query": "  师兄啊师兄  ", "tmdb_id": " 12345 "}
            ),
            {"query": "师兄啊师兄", "tmdb_id": "12345"},
        )

    def test_accepts_named_library_scope(self):
        self.assertEqual(
            count_series_episodes_arguments(
                {
                    "query": "师兄啊师兄",
                    "library_name": "  美女库  ",
                }
            ),
            {"query": "师兄啊师兄", "tmdb_id": "", "library_name": "美女库"},
        )

    def test_rejects_invalid_or_extra_arguments(self):
        invalid = (
            {},
            {"query": 1},
            {"query": "剧名\n注入"},
            {"query": "剧名", "tmdb_id": "abc"},
            {"query": "剧名", "extra": True},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                with self.assertRaises(AgentToolError):
                    count_series_episodes_arguments(arguments)


class LibraryEpisodeCountToolTests(unittest.TestCase):
    @patch("app.agent.library_episode_count.inspect_series_episode_sources")
    def test_counts_unique_local_episode_coordinates_and_seasons(self, inspect_sources):
        inspect_sources.return_value = [
            {
                "server_type": "jellyfin",
                "server_name": "Jellyfin",
                "status": "unmapped",
                "selected": {"name": "师兄啊师兄", "year": "2023"},
                "episodes": [(1, 1), (1, 2), (2, 1)],
                "ignored_specials": 2,
                "ignored_unknown": 0,
                "truncated": False,
            },
            {
                "server_type": "emby",
                "server_name": "Emby",
                "status": "ready",
                "selected": {"name": "师兄啊师兄", "year": "2023"},
                "episodes": [(1, 1), (2, 1), (2, 2)],
                "ignored_specials": 0,
                "ignored_unknown": 0,
                "truncated": False,
            },
        ]

        result = count_series_episodes({"query": "师兄啊师兄", "tmdb_id": ""})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.data["local_episode_count"], 4)
        self.assertEqual(result.data["season_count"], 2)
        self.assertEqual(
            result.data["seasons"],
            [
                {"season": 1, "count": 2, "first_episode": 1, "last_episode": 2},
                {"season": 2, "count": 2, "first_episode": 1, "last_episode": 2},
            ],
        )
        self.assertEqual(result.data["matched_source_count"], 2)
        self.assertIn("本地收录 4 集", result.summary)
        inspect_sources.assert_called_once_with(
            "师兄啊师兄",
            tmdb_id="",
            max_episodes=2000,
            include_specials=False,
        )

    @patch("app.agent.library_episode_count.inspect_series_episode_sources")
    def test_limits_count_to_named_library_without_exposing_internal_id(
        self, inspect_sources
    ):
        inspect_sources.return_value = [
            {
                "server_type": "jellyfin",
                "server_name": "Jellyfin",
                "library_name": "美女库",
                "status": "ready",
                "selected": {"name": "师兄啊师兄", "year": "2023"},
                "episodes": [(1, 1), (1, 2)],
                "truncated": False,
            }
        ]

        result = count_series_episodes(
            {
                "query": "师兄啊师兄",
                "tmdb_id": "",
                "library_name": "美女库",
            }
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data["library_name"], "美女库")
        inspect_sources.assert_called_once_with(
            "师兄啊师兄",
            tmdb_id="",
            max_episodes=2000,
            include_specials=False,
            library_name="美女库",
        )
        self.assertNotIn("library-id", repr(result.to_dict()))

    @patch("app.agent.library_episode_count.inspect_series_episode_sources")
    def test_does_not_report_zero_when_media_server_is_not_configured(
        self, inspect_sources
    ):
        inspect_sources.return_value = []

        result = count_series_episodes({"query": "师兄啊师兄", "tmdb_id": ""})

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "not_configured")
        self.assertIsNone(result.data["local_episode_count"])
        self.assertNotIn("0 集", result.summary)

    @patch("app.agent.library_episode_count.inspect_series_episode_sources")
    def test_not_found_returns_candidates_without_false_count(self, inspect_sources):
        inspect_sources.return_value = [
            {
                "server_type": "jellyfin",
                "server_name": "Jellyfin",
                "status": "not_found",
                "candidates": [
                    {"name": "师兄啊师兄 年番", "year": "2024", "id": "private"}
                ],
                "episodes": [],
            }
        ]

        result = count_series_episodes({"query": "师兄啊师兄", "tmdb_id": ""})

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "not_found")
        self.assertIsNone(result.data["local_episode_count"])
        self.assertEqual(
            result.data["candidates"], [{"name": "师兄啊师兄 年番", "year": "2024"}]
        )
        self.assertNotIn("id", result.data["candidates"][0])

    @patch("app.agent.library_episode_count.inspect_series_episode_sources")
    def test_marks_unnumbered_or_truncated_inventory_as_partial(self, inspect_sources):
        inspect_sources.return_value = [
            {
                "server_type": "jellyfin",
                "server_name": "Jellyfin",
                "status": "ready",
                "selected": {"name": "测试剧", "year": "2026"},
                "episodes": [(1, 1), (1, 2)],
                "ignored_unknown": 1,
                "truncated": True,
            }
        ]

        result = count_series_episodes({"query": "测试剧", "tmdb_id": ""})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.data["local_episode_count"], 2)
        self.assertIn("下限", result.suggestions[0])


if __name__ == "__main__":
    unittest.main()
