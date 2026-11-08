"""缺集资源确定性排序与只读推荐测试。"""
from __future__ import annotations

import ast
import copy
import subprocess
import sys
import textwrap
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.modules.episode_resource_ranking import rank_episode_search


def _item(
    result_id: str,
    title: str,
    *,
    site_id: str = "nyaa",
    state: str = "ready",
    seeders=None,
    downloads=None,
) -> dict[str, Any]:
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
    def test_episode_quality_does_not_override_indexer_media_identity(self):
        from app.indexers.models import IndexerItem, IndexerMediaSearchRequest
        from app.indexers.ranking import rank_item

        request = IndexerMediaSearchRequest.create(
            title="目标剧", original_title="Target Show", media_type="tv", season=1, episode=2,
        )
        items = [
            IndexerItem("nyaa", "Nyaa", "Target Show S01E02 720p", result_id="right-show-000001",
                        download_state="ready", download_kinds=("magnet",), seeders=1),
            IndexerItem("nyaa", "Nyaa", "其他作品 S01E02 2160p", result_id="wrong-show-000001",
                        download_state="ready", download_kinds=("magnet",), seeders=1000),
        ]
        public = [rank_item(item, media=request, fallback_query=request.title).to_public_dict() for item in items]
        ranked = rank_episode_search({"items": public}, season=1, episode=2)
        self.assertEqual(ranked["recommendation"]["selected"]["result_id"], "right-show-000001")
        self.assertEqual(ranked["recommendation"]["candidate_count"], 1)
        self.assertFalse(ranked["items"][1]["quality"]["eligible"])

    def test_checksum_cannot_make_a_different_episode_recommended(self):
        item = _item("episode-12-with-checksum", "Show S04E12 [E5DA5D0E]")
        for episode, expected in ((5, None), (12, item["result_id"])):
            with self.subTest(episode=episode):
                result = rank_episode_search({"items": [item]}, season=4, episode=episode)
                selected = result["recommendation"]["selected"] or {}
                self.assertEqual(selected.get("result_id"), expected)

    def test_ranking_checks_every_supplied_candidate_before_selecting(self):
        items = [_item(f"wrong-result-{index:04d}", "示例剧 S02E02 2160p") for index in range(50)]
        items.append(_item("exact-result-last", "示例剧 S02E03 1080p"))
        ranked = rank_episode_search({"items": items}, season=2, episode=3)
        self.assertEqual(len(ranked["items"]), len(items))
        self.assertEqual(ranked["recommendation"]["selected"]["result_id"], "exact-result-last")
        self.assertEqual(ranked["download_plan"]["result_ids"], ["exact-result-last"])
        self.assertNotIn("prepare_tool", ranked["download_plan"])
        self.assertNotIn("候选序号", str(ranked["download_plan"]))
        self.assertEqual(ranked["recommendation"]["candidate_count"], 1)
        self.assertNotIn("quality", items[-1])

    def test_subscriptions_and_agent_use_one_business_ranking(self):
        from app.modules import media_subscriptions, missing_resources

        self.assertIs(media_subscriptions.rank_episode_search, rank_episode_search)
        self.assertIs(missing_resources.rank_episode_search, rank_episode_search)
        self.assertEqual(rank_episode_search.__module__, "app.modules.episode_resource_ranking")
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / "app/agent/domains/resources/recommendation.py").exists())
        self.assertFalse((root / "app/agent/domains/subscriptions/preference_policy.py").exists())

    def test_shared_ranking_and_preferences_load_without_agent_domains(self):
        root = Path(__file__).resolve().parents[1]
        for filename in ("episode_resource_ranking.py", "media_preferences.py"):
            tree = ast.parse((root / "app/modules" / filename).read_text())
            imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
            imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
            self.assertFalse(any(name and name.startswith("app.agent") for name in imports))
        script = textwrap.dedent('''
            import tests
            import importlib.abc
            import sys
            from unittest.mock import patch

            blocked = ("app.agent.domains", "app.agent.kernel")

            class NoAgentDomainImports(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
                        raise AssertionError(f"业务排序反向依赖 Agent 领域/内核: {fullname}")

            sys.meta_path.insert(0, NoAgentDomainImports())
            with patch("socket.socket.connect", side_effect=AssertionError("禁止真实外联")):
                from app.indexers.ranking import filter_technical_candidates, technical_filter_arguments
                from app.modules.episode_resource_ranking import rank_episode_search
                from app.modules.media_preferences import effective_preferences
                assert rank_episode_search({"items": []}, season=1, episode=1)["items"] == []
                assert effective_preferences({"minimum_resolution": "bad"})["minimum_resolution"] == "any"
                arguments = technical_filter_arguments({"resolution": "1080p", "dynamic_range": "SDR"})
                items = [{"title": "Demo 1080p"}, {"title": "Demo 1080p SDR"}]
                assert filter_technical_candidates(items, arguments)[0] == items[1:]
            assert not any(name == prefix or name.startswith(prefix + ".")
                           for name in sys.modules for prefix in blocked)
        ''')
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_claimed_finished_pack_cannot_cover_episodes_not_yet_aired(self):
        as_of = date(2026, 10, 16)
        context = {
            "as_of": as_of.isoformat(),
            "detail": {"name": "诛仙", "seasons": [{"season_number": 4, "episode_count": 26}]},
            "season_detail": {"season_number": 4, "episodes": [
                {"episode_number": number, "air_date": (as_of + timedelta(days=number - 11)).isoformat()}
                for number in range(1, 27)
            ]},
        }
        for title in (
            "[GM-Team][国漫][诛仙 第4季][Jade Dynasty IV][2026][01-26 Fin][HEVC][GB][4K]",
            "诛仙 S04 Complete 2160p WEB-DL",
        ):
            with self.subTest(title=title):
                ranked = rank_episode_search({"items": [_item("future-pack-result-001", title)]},
                                             season=4, episode=11, mapping_context=context)
                self.assertFalse(ranked["items"][0]["quality"]["eligible"])
                self.assertIsNone(ranked["recommendation"]["selected"])
                self.assertTrue(any("未播" in warning for warning in ranked["items"][0]["quality"]["warnings"]))
        valid = rank_episode_search({"items": [_item("aired-pack-result-0001", "诛仙 S04E01-E11 2160p")]},
                                    season=4, episode=11, mapping_context=context)
        self.assertEqual(valid["items"][0]["quality"]["match"], "episode_pack")
        self.assertTrue(valid["items"][0]["quality"]["eligible"])
        context["as_of"] = (as_of + timedelta(days=15)).isoformat()
        finished = rank_episode_search(
            {"items": [_item("finished-pack-result-001", "诛仙 第4季 [01-26 Fin] 2160p")]},
            season=4, episode=11, mapping_context=context,
        )
        self.assertEqual(finished["items"][0]["quality"]["match"], "episode_pack")
        self.assertTrue(finished["items"][0]["quality"]["eligible"])
        assert (finished["recommendation"]["selected"]) is not None

    def test_pack_with_unknown_dates_is_not_confirmed_coverage(self):
        context = {"as_of": "2026-10-16", "detail": {"seasons": [{"season_number": 4, "episode_count": 26}]},
                   "season_detail": {"season_number": 4, "episodes": [
                       {"episode_number": number, "air_date": "2026-10-01" if number <= 11 else None}
                       for number in range(1, 27)
                   ]}}
        ranked = rank_episode_search({"items": [_item("unknown-date-pack-001", "诛仙 S04E01-E26 2160p")]},
                                     season=4, episode=11, mapping_context=context)
        quality = ranked["items"][0]["quality"]
        self.assertEqual(quality["match"], "unknown")
        self.assertTrue(quality["eligible"], "日期未知不是资源不存在，保留用户主动查看和手动选择能力")

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
        self.assertEqual(
            [item.get("position") for item in ranked["items"]],
            [None, None, None],
        )
        self.assertTrue(any("冲突" in warning for warning in ranked["items"][-1]["quality"]["warnings"]))
        self.assertEqual(ranked["recommendation"]["status"], "recommended")
        selected = ranked["recommendation"]["selected"]
        self.assertEqual(selected["result_id"], "exact-result-id-0001")
        self.assertIn("精确匹配 S02E03", selected["reasons"])
        self.assertEqual(selected["tags"]["resolution"], "1080p")
        self.assertEqual(selected["warnings"], [])

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

    def test_matching_summary_counts_singles_and_packs_beyond_top_alternatives(self):
        source = {"items": [
            *[_item(f"single-candidate-{n}", "Show S01E01 1080p") for n in range(5)],
            *[_item(f"range-candidate-{n}", "Show S01E01-E03 1080p") for n in range(3)],
            _item("unavailable-single", "Show S01E01 1080p", state="unavailable"),
            _item("unknown-candidate", "Show 1080p"),
            _item("season-candidate", "Show S01 Complete 1080p"),
            _item("conflict-candidate", "Show S01E02 1080p"),
            _item("excluded-single", "Show S01E01 480p"),
            _item("excluded-range", "Show S01E01-E03 480p"),
        ]}
        original = copy.deepcopy(source)
        ranked = rank_episode_search(source, season=1, episode=1,
                                     preferences={"minimum_resolution": "1080p"})
        summary = ranked["recommendation"]
        self.assertEqual(source, original)
        self.assertEqual(summary["matched_counts"], {"exact_episode": 5, "episode_pack": 3})
        self.assertEqual(summary["matched_count"], sum(summary["matched_counts"].values()))
        self.assertTrue(all(item["match"] == "exact_episode"
                            for item in [summary["selected"], *summary["alternatives"]]))

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

    def test_captioned_episode_uses_verified_title_without_inventing_a_season(self):
        source = {"items": [_item(f"captioned-result-{episode:03d}", f"【国产】理想禁区 {episode:02d} 分集标题 1080P") for episode in range(1, 12)]}
        unknown = rank_episode_search(source, season=1, episode=7)
        self.assertEqual(unknown["recommendation"]["status"], "review_required")
        proved = rank_episode_search(source, season=1, episode=7, mapping_context={"detail": {"name": "理想禁区"}, "season_detail": {}})
        self.assertEqual(proved["recommendation"]["status"], "recommended")
        self.assertEqual(proved["recommendation"]["selected"]["result_id"], "captioned-result-007")
        self.assertEqual(sum(item["quality"]["match"] == "exact_episode" for item in proved["items"]), 1)
        later = rank_episode_search(source, season=2, episode=7, mapping_context={"detail": {"name": "理想禁区"}, "season_detail": {}})
        self.assertNotEqual(later["recommendation"]["status"], "recommended")

    def test_known_title_range_still_requires_airing_and_file_evidence(self):
        source = {"items": [_item("bare-range-result-001", "光阴之外46-47.2160p")]}
        context = {
            "as_of": "2026-11-08",
            "detail": {"name": "光阴之外"},
            "season_detail": {"season_number": 1, "episodes": [
                {"episode_number": 46, "air_date": "2026-10-25"},
                {"episode_number": 47, "air_date": "2026-11-01"},
            ]},
        }
        unknown = rank_episode_search(source, season=1, episode=47)
        self.assertEqual(unknown["recommendation"]["status"], "review_required")
        proved = rank_episode_search(source, season=1, episode=47, mapping_context=context)
        quality = proved["items"][0]["quality"]
        self.assertEqual(quality["match"], "episode_pack")
        self.assertTrue(quality["eligible"])
        self.assertIn("未核验实际文件清单", " ".join(quality["warnings"]))
        later = rank_episode_search(source, season=2, episode=47, mapping_context=context)
        self.assertEqual(later["items"][0]["quality"]["match"], "unknown")
        self.assertEqual(later["recommendation"]["status"], "review_required")
        context["as_of"] = "2026-10-26"
        future = rank_episode_search(source, season=1, episode=47, mapping_context=context)
        self.assertFalse(future["items"][0]["quality"]["eligible"])

    def test_season_pack_requires_review_and_never_auto_submits(self):
        source = {"items": [
            _item("season-pack-id-0001", "示例剧 S02 Complete 1080p BluRay", state="resolvable", seeders=10)
        ]}
        original = copy.deepcopy(source)

        ranked = rank_episode_search(source, season=2, episode=3)

        self.assertEqual(source, original)
        self.assertEqual(ranked["recommendation"]["status"], "review_required")
        self.assertEqual(ranked["recommendation"]["matched_count"], 0)
        self.assertEqual(ranked["recommendation"]["basis"], "release_title_only")
        self.assertFalse(ranked["recommendation"]["stream_verified"])
        self.assertIsNone(ranked["recommendation"]["selected"])
        self.assertEqual(ranked["recommendation"]["alternatives"], [])
        self.assertTrue(ranked["items"][0]["quality"]["eligible"], "仍允许用户明确选择合集，但不冒充已匹配推荐")
        self.assertEqual(ranked["items"][0]["quality"]["match"], "season_pack")
        self.assertEqual(ranked["download_plan"]["mode"], "read_only")
        self.assertFalse(ranked["download_plan"]["auto_submit"])
        self.assertTrue(ranked["download_plan"]["requires_confirmation"])
        self.assertEqual(ranked["download_plan"]["result_ids"], [])
        self.assertNotIn("candidate_position", ranked["download_plan"])

    def test_reranking_replaces_target_and_preference_evidence_without_mutating_search(self):
        search = {"request": {"season": 1, "episode": 1}, "items": [
            _item("first-episode-resource", "Show S01E01 2160p"),
            _item("second-episode-resource", "Show S01E02 1080p"),
            _item("unknown-episode-resource", "Show WEB-DL 1080p"),
        ]}
        original = copy.deepcopy(search)
        first = rank_episode_search(search, season=1, episode=1,
                                    preferences={"preferred_resolution": "2160p"})
        before = copy.deepcopy(first)
        second = rank_episode_search(first, season=1, episode=2)
        self.assertEqual(first, before)
        self.assertEqual(search, original)
        self.assertTrue(any("preference_match" in item for item in first["items"]))
        self.assertTrue(all("preference_match" not in item for item in second["items"]))
        self.assertEqual(second["request"], original["request"])
        self.assertEqual(second["recommendation"]["target"], {"season": 1, "episode": 2})
        self.assertEqual(second["recommendation"]["candidate_count"], 2)
        self.assertEqual(second["recommendation"]["matched_count"], 1)
        self.assertEqual(second["recommendation"]["matched_counts"], {"exact_episode": 1, "episode_pack": 0})
        self.assertEqual(second["recommendation"]["selected"]["result_id"], "second-episode-resource")

    def test_exact_episode_marker_wins_over_ambiguous_complete_tag(self):
        ranked = rank_episode_search({"items": [
            _item(
                "exact-complete-id-01",
                "示例剧 S02E03 Complete 1080p WEB-DL",
                seeders=16,
            )
        ]}, season=2, episode=3)

        self.assertEqual(ranked["items"][0]["quality"]["match"], "exact_episode")
        self.assertEqual(ranked["recommendation"]["status"], "recommended")

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

    def test_chinese_episode_markers_bind_to_season_marker_elsewhere(self):
        ranked = rank_episode_search({"items": [
            _item(
                "episode-pack-id-zh-01",
                "九门[第29-30集][国语配音/中文字幕].Mystic.Nine.S02.1080p.WEB-DL",
                seeders=24,
            ),
            _item(
                "episode-conflict-zh",
                "九门[第30集][国语音轨].Mystic.Nine.S01.2026.2160p",
                seeders=30,
            ),
        ]}, season=2, episode=30)

        self.assertEqual(ranked["items"][0]["quality"]["match"], "episode_pack")
        conflict = next(
            item for item in ranked["items"] if item["result_id"] == "episode-conflict-zh"
        )
        self.assertEqual(conflict["quality"]["match"], "conflict")
        self.assertFalse(conflict["quality"]["eligible"])

    def test_gm_team_bare_absolute_episode_is_recommended(self):
        ranked = rank_episode_search(
            {
                "items": [
                    _item(
                        "gm-team-absolute-159",
                        "[GM-Team][国漫][师兄啊师兄][2026][159][GB][4K HEVC 10Bit]",
                        seeders=36,
                    )
                ]
            },
            season=1,
            episode=159,
        )

        quality = ranked["items"][0]["quality"]
        self.assertEqual(quality["match"], "exact_episode")
        self.assertEqual(quality["confidence"], "high")
        self.assertEqual(ranked["recommendation"]["status"], "recommended")

    def test_release_season_maps_to_merged_tmdb_season_with_airdate_evidence(self):
        episodes = []
        for number in range(1, 96):
            if number <= 40:
                year, day = 2024, number
            elif number <= 69:
                year, day = 2025, number - 40
            else:
                year, day = 2026, number - 69
            episodes.append(
                {
                    "episode_number": number,
                    "air_date": f"{year}-01-{min(day, 28):02d}",
                }
            )
        mapping_context = {
            "detail": {"seasons": [{"season_number": 1, "episode_count": 95}]},
            "season_detail": {"season_number": 1, "episodes": episodes},
        }
        ranked = rank_episode_search(
            {
                "items": [
                    _item(
                        "gm-team-season-3-26",
                        "[GM-Team][国漫][沧元图 第3季][2026][26][GB][4K HEVC 10Bit]",
                        seeders=54,
                    )
                ]
            },
            season=1,
            episode=95,
            mapping_context=mapping_context,
        )

        quality = ranked["items"][0]["quality"]
        self.assertEqual(quality["match"], "exact_episode")
        self.assertEqual(quality["confidence"], "high")
        self.assertTrue(
            any(
                "S03E26" in reason and "S01E95" in reason
                for reason in quality["reasons"]
            )
        )

    def test_unproven_arc_episode_remains_review_only(self):
        ranked = rank_episode_search(
            {
                "items": [
                    _item(
                        "gm-team-arc-episode-16",
                        "[GM-Team][国漫][凡人修仙传 慕兰之战][2026][16][GB][4K HEVC 10Bit]",
                        seeders=40,
                    )
                ]
            },
            season=1,
            episode=192,
        )

        quality = ranked["items"][0]["quality"]
        self.assertEqual(quality["match"], "unknown")
        self.assertTrue(quality["eligible"])
        self.assertEqual(ranked["recommendation"]["status"], "review_required")
        self.assertIsNone(ranked["recommendation"]["selected"])
        self.assertEqual(ranked["recommendation"]["alternatives"], [])

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
        self.assertNotIn("candidate_position", unavailable["download_plan"])
        self.assertEqual(invalid_handle["recommendation"]["status"], "no_downloadable_candidate")
        self.assertFalse(invalid_handle["items"][0]["quality"]["eligible"])
        self.assertTrue(any(
            "资源句柄" in warning
            for warning in invalid_handle["items"][0]["quality"]["warnings"]
        ))
        self.assertEqual(empty["recommendation"]["status"], "empty")
        self.assertEqual(empty["recommendation"]["candidate_count"], 0)
        for ranked in (unavailable, invalid_handle, empty):
            self.assertEqual(ranked["download_plan"]["result_ids"], [])
            self.assertEqual(ranked["recommendation"]["matched_counts"], {"exact_episode": 0, "episode_pack": 0})

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
