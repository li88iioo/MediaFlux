"""全局搜索聚合服务。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.clients.base import MediaItem
from app.discovery.models import MediaCard
from app.modules.global_search import _local_section, build_global_search, normalize_search_query


class GlobalSearchTests(unittest.TestCase):
    def test_query_validation_rejects_control_and_overlong_input(self):
        self.assertEqual(normalize_search_query("  沙丘  "), "沙丘")
        for value in ("", "x" * 121, "bad\nquery"):
            with self.assertRaisesRegex(ValueError, "1 到 120"):
                normalize_search_query(value)

    @patch("app.modules.global_search.search_media_servers")
    def test_local_media_distinguishes_series_from_individual_episode(self, media_search):
        media_search.return_value = [{
            "server_type": "jellyfin",
            "server_name": "家庭 Jellyfin",
            "web_url": "http://media.example",
            "error": "",
            "items": [
                MediaItem(id="1", name="测试剧集", type="Series", year="2026"),
                MediaItem(
                    id="2", name="序章", type="Episode", year="2026",
                    series_name="测试剧集", season_number=1, episode_number=3,
                ),
            ],
        }]

        section = _local_section("测试")

        media_search.assert_called_once_with("测试", limit=16)
        series, episode = section["items"]
        self.assertEqual((series["title"], series["type_label"]), ("测试剧集", "剧集"))
        self.assertEqual((episode["title"], episode["type_label"]), ("序章", "单集"))
        self.assertEqual(episode["episode_context"], "测试剧集 · 第 1 季 · 第 3 集")
        self.assertIn("q=%E6%B5%8B%E8%AF%95%E5%89%A7%E9%9B%86", episode["resource_url"])

    @patch("app.modules.global_search.db.list_organize_logs")
    @patch("app.modules.global_search.db.list_download_logs")
    @patch("app.modules.global_search.db.list_rss_subscriptions")
    @patch("app.modules.global_search.get_discovery_search_service")
    @patch("app.modules.global_search.app_config.get_bool", return_value=True)
    @patch("app.modules.global_search.search_media_servers")
    def test_search_groups_local_discovery_rss_downloads_and_logs(
        self, media_search, _enabled, discovery_service, rss_rows, download_rows, log_rows,
    ):
        media_search.return_value = [{
            "server_type": "jellyfin",
            "server_name": "家庭 Jellyfin",
            "web_url": "http://media.example",
            "error": "",
            "items": [MediaItem(
                id="a" * 32,
                name="沙丘 2",
                type="Movie",
                year="2024",
                primary_image="/media-image/jellyfin/" + "a" * 32,
                web_url="http://media.example/details",
            )],
        }]
        discovery_service.return_value.search.return_value = SimpleNamespace(
            items=(MediaCard(
                provider="tmdb", external_id="693134", media_type="movie",
                title="沙丘2", year="2024", poster_key="poster.jpg",
            ),),
            errors=(),
        )
        rss_rows.return_value = [{
            "id": 1, "name": "沙丘订阅", "urls": "https://example/rss",
            "parser": "mikan", "exclude_keywords": "", "enabled": 1,
        }]
        download_rows.return_value = [{
            "id": 2, "title": "沙丘2 2160p", "source": "qb",
            "status": "downloading", "created_at": "2026-07-29 10:00:00",
            "path": "/downloads/dune", "error": "",
        }]
        log_rows.return_value = [{
            "id": 3, "original_name": "沙丘2.mkv", "current_name": "沙丘2.mkv",
            "original_path": "/source", "new_path": "/media/沙丘2.mkv",
            "status": "success", "created_at": "2026-07-29 10:00:00",
        }]

        result = build_global_search("沙丘")

        self.assertEqual(result["query"], "沙丘")
        sections = {section["key"]: section for section in result["sections"]}
        self.assertEqual(set(sections), {"local", "discovery", "rss", "downloads", "logs"})
        self.assertEqual(sections["local"]["items"][0]["title"], "沙丘 2")
        self.assertTrue(sections["local"]["items"][0]["external"])
        discovery_item = sections["discovery"]["items"][0]
        self.assertEqual(discovery_item["title"], "沙丘2")
        self.assertEqual(discovery_item["external_id"], "693134")
        self.assertEqual(discovery_item["media_type"], "movie")
        self.assertEqual(
            discovery_item["url"],
            "/discovery?detail_provider=tmdb&detail_type=movie&detail_id=693134"
            "&return_query=%E6%B2%99%E4%B8%98",
        )
        self.assertEqual(discovery_item["detail_url"], discovery_item["url"])
        self.assertIn("/discovery?q=", discovery_item["resource_url"])
        self.assertEqual(sections["rss"]["items"][0]["title"], "沙丘订阅")
        self.assertEqual(sections["downloads"]["items"][0]["title"], "沙丘2 2160p")
        self.assertEqual(sections["logs"]["items"][0]["title"], "沙丘2.mkv")

    @patch("app.modules.global_search.db.list_organize_logs", side_effect=RuntimeError("db down"))
    @patch("app.modules.global_search.db.list_download_logs", return_value=[])
    @patch("app.modules.global_search.db.list_rss_subscriptions", return_value=[])
    @patch("app.modules.global_search.app_config.get_bool", return_value=False)
    @patch("app.modules.global_search.search_media_servers", return_value=[])
    def test_source_failure_is_isolated_and_discovery_can_be_hidden(
        self, _media, _enabled, _rss, _downloads, _logs,
    ):
        result = build_global_search("测试")
        sections = {section["key"]: section for section in result["sections"]}

        self.assertNotIn("discovery", sections)
        self.assertEqual(sections["logs"]["items"], [])
        self.assertEqual(sections["logs"]["error"], "该来源暂时不可用")
        self.assertIn("local", sections)


if __name__ == "__main__":
    unittest.main()
