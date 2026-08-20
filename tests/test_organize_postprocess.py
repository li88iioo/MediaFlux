"""整理纯后处理规则测试。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.clients.guangya import GuangYaFile
from app.modules.organize_postprocess import (
    companion_target_name,
    media_notification_item,
    media_role,
    normalize_media_number,
    normalized_stem,
    replacement_delete_block_reason,
    resolved_plan_position,
)


class OrganizePostprocessTests(unittest.TestCase):
    def test_companion_identity_helpers_preserve_semantic_suffixes(self):
        self.assertEqual(normalized_stem("Movie.zh-Hans.srt"), "moviezhhans")
        self.assertEqual(
            companion_target_name("Movie.mkv", "Movie (2026).mkv", "Movie.zh-Hans.srt"),
            "Movie (2026).zh-Hans.srt",
        )
        self.assertEqual(media_role("Movie.zh-Hans.srt"), "subtitle")
        self.assertEqual(media_role("poster.webp"), "image")

    def test_media_numbers_and_planned_position_are_stable(self):
        self.assertEqual(normalize_media_number([None, "3.0", 4]), 3)
        self.assertIsNone(normalize_media_number([True, "bad", -1]))
        plan = SimpleNamespace(season=2, episode=None)
        self.assertEqual(
            resolved_plan_position(plan, {"season": 1, "episode": 7}),
            (2, 7),
        )

    def test_notification_projection_has_original_shape(self):
        match = SimpleNamespace(title="Show", year="2026", media_type="tv", tmdb_id="9")
        plan = SimpleNamespace(
            match=match,
            original_path="Shows/Incoming",
            year="",
            season=1,
            episode=2,
            season_total=12,
            target_path="电视剧/Show (2026)/Season 01",
            size=100,
            backdrop_path="/backdrop.jpg",
            poster_path="/poster.jpg",
        )

        item = media_notification_item(
            plan,
            "Show.S01E02.mkv",
            {"season": 9, "episode": 9},
            season_present_episodes=[1, 2],
        )

        self.assertEqual((item["season"], item["episode"]), (1, 2))
        self.assertEqual(item["source"], "光鸭云盘")
        self.assertEqual(item["season_present_episodes"], [1, 2])

    def test_replacement_delete_verification_remains_fail_closed(self):
        old = GuangYaFile("old", "Movie.old.mkv", False, 100, "etag-old", "target")
        new = GuangYaFile("new", "Movie.mkv", False, 200, "etag-new", "target")

        self.assertEqual(
            replacement_delete_block_reason(
                expected_old=old,
                expected_new=new,
                old_detail=old,
                new_detail=new,
                target_files=[old, new],
                scan_errors=[],
                move_succeeded=True,
            ),
            "",
        )
        self.assertEqual(
            replacement_delete_block_reason(
                expected_old=old,
                expected_new=new,
                old_detail=old,
                new_detail=new,
                target_files=[old],
                scan_errors=[],
                move_succeeded=True,
            ),
            "替换文件缺失，禁止将旧文件移入回收站",
        )
