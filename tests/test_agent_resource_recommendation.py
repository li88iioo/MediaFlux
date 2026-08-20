"""缺集资源确定性排序与只读推荐测试。"""
from __future__ import annotations

import copy
import unittest

from app.agent.resource_recommendation import rank_episode_search


def _item(
    result_id: str,
    title: str,
    *,
    site_id: str = "nyaa",
    state: str = "ready",
    seeders=None,
    downloads=None,
):
    return {
        "result_id": result_id,
        "site_id": site_id,
        "site_name": site_id.upper(),
        "title": title,
        "download_state": state,
        "download_kinds": ["magnet"] if state in {"ready", "resolvable"} else [],
        "seeders": seeders,
        "downloads": downloads,
    }


class ResourceRecommendationTests(unittest.TestCase):
    def test_exact_episode_dominates_quality_and_conflicts_are_ineligible(self):
        data = {
            "query": "示例剧 S02E03",
            "items": [
                _item("wrong-result-id-0001", "示例剧 S02E04 2160p BluRay DoVi", seeders=900),
                _item("unknown-result-id-01", "示例剧 2160p Remux 简中", site_id="mikan", seeders=200),
                _item("exact-result-id-0001", "示例剧 S02E03 1080p WEB-DL 简中", seeders=8),
            ],
        }

        ranked = rank_episode_search(data, season=2, episode=3)

        self.assertEqual([item["result_id"] for item in ranked["items"]], [
            "exact-result-id-0001", "unknown-result-id-01", "wrong-result-id-0001"
        ])
        self.assertEqual(ranked["items"][0]["quality"]["match"], "exact_episode")
        self.assertEqual(ranked["items"][0]["quality"]["confidence"], "high")
        self.assertTrue(ranked["items"][0]["quality"]["eligible"])
        self.assertFalse(ranked["items"][-1]["quality"]["eligible"])
        self.assertTrue(any("冲突" in warning for warning in ranked["items"][-1]["quality"]["warnings"]))
        self.assertEqual(ranked["recommendation"]["status"], "recommended")
        self.assertEqual(ranked["recommendation"]["selected"]["result_id"], "exact-result-id-0001")

    def test_missing_activity_is_unknown_instead_of_zero_quality(self):
        data = {"items": [
            _item("zero-seeders-id-0001", "示例剧 S02E03 1080p WEB-DL", seeders=0),
            _item("unknown-result-id-01", "示例剧 S02E03 1080p WEB-DL", seeders=None),
        ]}

        ranked = rank_episode_search(data, season=2, episode=3)

        self.assertEqual(ranked["items"][0]["result_id"], "unknown-result-id-01")
        unknown_warnings = ranked["items"][0]["quality"]["warnings"]
        zero_warnings = ranked["items"][1]["quality"]["warnings"]
        self.assertIn("站点未提供做种数", unknown_warnings)
        self.assertIn("当前做种数为 0", zero_warnings)

    def test_order_is_stable_when_input_order_changes(self):
        first = _item("beta-result-id-0001", "Beta S02E03 1080p WEB-DL", site_id="mikan")
        second = _item("alpha-result-id-001", "Alpha S02E03 1080p WEB-DL", site_id="nyaa")

        order_one = [item["result_id"] for item in rank_episode_search(
            {"items": [first, second]}, season=2, episode=3
        )["items"]]
        order_two = [item["result_id"] for item in rank_episode_search(
            {"items": [second, first]}, season=2, episode=3
        )["items"]]

        self.assertEqual(order_one, ["alpha-result-id-001", "beta-result-id-0001"])
        self.assertEqual(order_one, order_two)

    def test_season_pack_requires_review_and_never_auto_submits(self):
        source = {"items": [
            _item("season-pack-id-0001", "示例剧 S02 Complete 1080p BluRay", state="resolvable", seeders=10)
        ]}
        original = copy.deepcopy(source)

        ranked = rank_episode_search(source, season=2, episode=3)

        self.assertEqual(source, original)
        self.assertEqual(ranked["recommendation"]["status"], "review_required")
        self.assertEqual(ranked["recommendation"]["selected"]["result_id"], "season-pack-id-0001")
        self.assertEqual(ranked["items"][0]["quality"]["match"], "season_pack")
        self.assertEqual(ranked["download_plan"]["mode"], "read_only")
        self.assertFalse(ranked["download_plan"]["auto_submit"])
        self.assertTrue(ranked["download_plan"]["requires_confirmation"])
        self.assertEqual(ranked["download_plan"]["prepare_tool"], "indexer.submit_resource")
        self.assertEqual(ranked["download_plan"]["result_id"], "season-pack-id-0001")

    def test_multi_episode_range_is_review_only_instead_of_exact_match(self):
        ranked = rank_episode_search({"items": [
            _item(
                "episode-pack-id-001",
                "示例剧 S02E01-E06 1080p WEB-DL",
                seeders=24,
            )
        ]}, season=2, episode=3)

        self.assertEqual(ranked["items"][0]["quality"]["match"], "episode_pack")
        self.assertEqual(ranked["recommendation"]["status"], "review_required")
        self.assertTrue(any(
            "多集资源" in warning
            for warning in ranked["items"][0]["quality"]["warnings"]
        ))
        self.assertFalse(ranked["download_plan"]["auto_submit"])

    def test_unavailable_and_empty_results_have_no_selected_plan(self):
        unavailable = rank_episode_search({"items": [
            _item("blocked-result-id-01", "示例剧 S02E03 2160p", state="unavailable", seeders=30)
        ]}, season=2, episode=3)
        invalid_handle_item = _item("short", "示例剧 S02E03 1080p", seeders=30)
        invalid_handle_item["download_kinds"] = None
        invalid_handle = rank_episode_search(
            {"items": [invalid_handle_item]}, season=2, episode=3
        )
        empty = rank_episode_search({"items": []}, season=2, episode=3)

        self.assertEqual(unavailable["recommendation"]["status"], "no_downloadable_candidate")
        self.assertIsNone(unavailable["recommendation"]["selected"])
        self.assertEqual(unavailable["download_plan"]["result_id"], "")
        self.assertEqual(invalid_handle["recommendation"]["status"], "no_downloadable_candidate")
        self.assertFalse(invalid_handle["items"][0]["quality"]["eligible"])
        self.assertTrue(any(
            "资源句柄" in warning
            for warning in invalid_handle["items"][0]["quality"]["warnings"]
        ))
        self.assertEqual(empty["recommendation"]["status"], "empty")
        self.assertEqual(empty["recommendation"]["candidate_count"], 0)

    def test_quality_explanation_is_bounded_and_uses_verified_release_tags(self):
        ranked = rank_episode_search({"items": [
            _item(
                "rich-result-id-00001",
                "示例剧 S02E03 2160p WEB-DL DoVi Atmos H265 简中-Group",
                seeders=64,
                downloads=1024,
            )
        ]}, season=2, episode=3)
        quality = ranked["items"][0]["quality"]

        self.assertLessEqual(len(quality["reasons"]), 6)
        self.assertLessEqual(len(quality["warnings"]), 4)
        self.assertEqual(quality["tags"]["resolution"], "2160p")
        self.assertEqual(quality["tags"]["media"], "WEB-DL")
        self.assertEqual(quality["tags"]["video_codec"], "H.265")
        self.assertIn("DoVi", quality["tags"]["effect"])
        self.assertIn("Atmos", quality["tags"]["audio"])


if __name__ == "__main__":
    unittest.main()
