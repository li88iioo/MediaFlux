"""发布前命名检查：合法的宽季集编号在追加版本标签时仍须保留。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app import database as db
from app.modules.naming import append_variant_tags, build_context, render_media_template
from app.modules.organize import OrganizeRules
from tests import test_release_chain_organize as fixtures
from tests.support import isolated_test_database


class NumericPositionNamingTests(unittest.TestCase):
    def test_variant_truncation_keeps_the_entire_generated_position(self):
        for season, episode in ((0, 1), (100, 3), (1000, 10000)):
            with self.subTest(season=season, episode=episode):
                context = build_context(title="A" * 300, year="", season=season, episode=episode)
                name = render_media_template("${title}.${season_episode}.${ext}", context)
                result = append_variant_tags(name, ["Standard"])
                self.assertIn(f"S{season:02d}E{episode:02d}", result)
                self.assertTrue(result.endswith(".Standard.mkv"))
                self.assertLessEqual(len(result.encode("utf-8")), 240)

    def test_manual_season_100_correction_persists_a_name_with_full_position(self):
        fixture = fixtures.ReleaseChainBusinessSnapshotTests()
        with isolated_test_database():
            try:
                log_id, cloud, service = fixture._fixture()
                cloud.files["video"].name = "First.S01E07.HDR10.mkv"
                db.update_organize_log(log_id, current_name=cloud.files["video"].name)
                item = db.list_organize_log_items(log_id)[0]
                db.update_organize_log_item(item["id"], current_name=cloud.files["video"].name)
                service.scraper.get_detail = lambda tmdb_id, _media_type: {
                    "id": int(tmdb_id), "name": "A" * 240, "first_air_date": "",
                    "genres": [], "origin_country": ["US"],
                    "seasons": [{"season_number": 100, "episode_count": 12}],
                }
                with patch.object(OrganizeRules, "from_config", return_value=OrganizeRules(
                    target_dir_id="library", region_split=False, year_split=False,
                    link_strm=False, emby_refresh=False, keep_multi_versions=True,
                )):
                    result = service.reorganize(log_id, "wide-season", service.detail(log_id)["version"],
                                                "2", "tv", season=100, episode=3)
                row = db.get_organize_log(log_id)
                name = cloud.files["video"].name
                self.assertTrue(result["success"])
                self.assertEqual((row["season"], row["episode"]), (100, 3))
                self.assertIn("S100E03", name)
                self.assertTrue(name.endswith(".Standard.mkv"))
                self.assertEqual(row["current_name"], name)
                self.assertLessEqual(len(name.encode("utf-8")), 240)
            finally:
                fixture.doCleanups()
