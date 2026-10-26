"""媒体组流水线（Sprint 1）需求驱动测试。

覆盖场景来自实施计划的验收条件：
- Task 1.1 媒体组任务/结果模型与组级统计合并
- Task 1.2 快速枚举媒体目录任务与失败关闭
- Task 1.3 逐媒体组完整计划与串行执行
- Task 1.4 组级失败隔离与来源级安全门
- Task 1.5 实时目录级进度投影
"""
from __future__ import annotations

from app.modules.process_lock import CrossProcessLock

from typing import cast
from app.clients.guangya import GuangYaClient
from app.clients.tmdb import TMDBClient
from app.modules.scraper import TMDBScraper
from app.modules.media_variant import MediaVariant

import json
import socket
import threading
import unittest
from unittest.mock import patch

from tests.support import isolated_test_database

from app.clients.guangya import GuangYaFile
from app.modules.organize import (
    OrganizeContext,
    OrganizePlan,
    OrganizePlanningResult,
    OrganizeRules,
    OrganizeScanUnsafeError,
    Organizer,
)
from app.modules.organize_groups import (
    GROUP_ROOT_PATH,
    GROUP_STAGE_EXECUTE,
    GROUP_STAGE_PLAN,
    GROUP_STAGE_SCAN,
    GROUP_STATUS_COMPLETED,
    GROUP_STATUS_FAILED,
    GROUP_STATUS_STOPPED,
    OrganizeGroupTask,
    build_group_result,
    changed_strm_paths,
    changed_target_dirs,
    enumerate_group_tasks,
    merge_group_stats,
)

MIB = 1024 * 1024


def _dir(file_id: str, name: str, parent_id: str) -> GuangYaFile:
    return GuangYaFile(file_id, name, True, parent_id=parent_id)


def _video(file_id: str, name: str, parent_id: str, size_mib: int = 100) -> GuangYaFile:
    return GuangYaFile(file_id, name, False, size_mib * MIB, parent_id=parent_id)


class _TreeClient:
    """记录调用顺序的只读目录树桩，便于断言逐组流水线时序。"""

    def __init__(self, tree: dict[str, list[GuangYaFile]], *, root_name: str = "待整理"):
        self.tree = tree
        self.calls: list[str] = []
        self.root_name = root_name
        self.list_dir_errors: dict[str, Exception] = {}

    def file_info(self, file_id: str):
        return GuangYaFile(file_id, self.root_name, True, parent_id="0")

    def list_dir(self, file_id: str):
        key = file_id
        self.calls.append(key)
        error = self.list_dir_errors.get(key)
        if error is not None:
            raise error
        return self.tree.get(key, [])


def _sample_tree() -> _TreeClient:
    return _TreeClient({
        "root": [
            _dir("group-a", "作品 A", "root"),
            _dir("group-b", "作品 B", "root"),
            _video("root-video", "Root.S01E01.mkv", "root"),
        ],
        "group-a": [_dir("nested-a", "Season 01", "group-a")],
        "nested-a": [_video("a-video", "A.S01E01.mkv", "nested-a")],
        "group-b": [_video("b-video", "B.S01E01.mkv", "group-b")],
    })


class GroupEnumerationTests(unittest.TestCase):
    """Task 1.2：快速枚举媒体目录任务。"""

    def _enumerate(self, client, **kwargs):
        params = {
            "source_dir_id": "root",
            "source_name": "待整理",
            "video_exts": {"mkv", "mp4"},
        }
        params.update(kwargs)
        return enumerate_group_tasks(client, **params)

    def test_root_media_and_top_level_directories_become_tasks(self):
        client = _sample_tree()

        enumeration = self._enumerate(client)

        self.assertTrue(enumeration.complete)
        self.assertEqual(
            [(task.group_id, task.group_path) for task in enumeration.tasks],
            [("root", GROUP_ROOT_PATH), ("group-a", "作品 A"), ("group-b", "作品 B")],
        )
        self.assertEqual([task.index for task in enumeration.tasks], [1, 2, 3])
        self.assertEqual({task.total for task in enumeration.tasks}, {3})

    def test_independent_root_media_are_split_into_file_scoped_tasks(self):
        client = _TreeClient({
            "root": [
                _video("movie-a", "Movie.A.2025.1080p.mkv", "root"),
                _video("movie-b", "Movie.B.2026.2160p.mkv", "root"),
            ],
        })

        enumeration = self._enumerate(client)

        self.assertEqual([task.group_id for task in enumeration.tasks], ["movie-a", "movie-b"])
        self.assertEqual([task.file_ids for task in enumeration.tasks], [("movie-a",), ("movie-b",)])
        self.assertTrue(all(task.is_root for task in enumeration.tasks))
        self.assertEqual({task.group_path for task in enumeration.tasks}, {GROUP_ROOT_PATH})

    def test_root_season_pack_remains_one_media_unit(self):
        client = _TreeClient({
            "root": [
                _video("episode-1", "Show.S01E01.1080p.mkv", "root"),
                _video("episode-2", "Show.S01E02.1080p.mkv", "root"),
            ],
        })

        enumeration = self._enumerate(client)

        self.assertEqual(len(enumeration.tasks), 1)
        self.assertEqual(enumeration.tasks[0].group_id, "root")
        self.assertEqual(enumeration.tasks[0].file_ids, ("episode-1", "episode-2"))

    def test_root_plain_anime_episode_and_ova_remain_one_media_unit(self):
        client = _TreeClient({
            "root": [
                _video("episode-1", "Example Anime - 01.mkv", "root"),
                _video("episode-2", "Example Anime - 02.mkv", "root"),
                _video("ova-1", "Example Anime - OVA.mkv", "root"),
            ],
        })

        enumeration = self._enumerate(client)

        self.assertEqual(len(enumeration.tasks), 1)
        self.assertEqual(
            enumeration.tasks[0].file_ids,
            ("episode-1", "episode-2", "ova-1"),
        )

    def test_year_after_hyphen_number_does_not_merge_movie_sequels(self):
        client = _TreeClient({
            "root": [
                _video("movie-1", "Example - 1.2024.1080p.mkv", "root"),
                _video("movie-2", "Example - 2.2026.1080p.mkv", "root"),
            ],
        })

        enumeration = self._enumerate(client)

        self.assertEqual(len(enumeration.tasks), 2)
        self.assertEqual(
            [task.file_ids for task in enumeration.tasks],
            [("movie-1",), ("movie-2",)],
        )

    def test_root_explicit_multipart_remains_one_media_unit(self):
        client = _TreeClient({
            "root": [
                _video("disc-1", "Concert.CD1.1080p.mkv", "root"),
                _video("disc-2", "Concert.CD2.1080p.mkv", "root"),
            ],
        })

        enumeration = self._enumerate(client)

        self.assertEqual(len(enumeration.tasks), 1)
        self.assertEqual(enumeration.tasks[0].file_ids, ("disc-1", "disc-2"))

    def test_root_movie_variants_remain_one_arbitration_unit(self):
        client = _TreeClient({
            "root": [
                _video(
                    "small", "Variant.Movie.2026.2160p.SDR.x265-SMALL.mkv",
                    "root",
                ),
                _video(
                    "large", "Variant.Movie.2026.2160p.SDR.x265-LARGE.mkv",
                    "root",
                ),
            ],
        })

        enumeration = self._enumerate(client)

        self.assertEqual(len(enumeration.tasks), 1)
        self.assertEqual(enumeration.tasks[0].file_ids, ("small", "large"))

    def test_root_nsfw_same_identifier_remains_one_media_unit(self):
        client = _TreeClient({
            "root": [
                _video("part-1", "hhd800.com@FJIN-140-CD1.mp4", "root"),
                _video("part-2", "hhd800.com@FJIN-140-CD2.mp4", "root"),
            ],
        })

        enumeration = self._enumerate(client, nsfw_enabled=True)

        self.assertEqual(len(enumeration.tasks), 1)
        self.assertEqual(enumeration.tasks[0].file_ids, ("part-1", "part-2"))

    def test_root_media_without_stable_ids_falls_back_to_legacy_group(self):
        client = _TreeClient({
            "root": [
                _video("", "Movie.A.2025.mkv", "root"),
                _video("movie-b", "Movie.B.2026.mkv", "root"),
            ],
        })

        enumeration = self._enumerate(client)

        self.assertEqual(len(enumeration.tasks), 1)
        self.assertEqual(enumeration.tasks[0].group_id, "root")
        self.assertEqual(enumeration.tasks[0].file_ids, ())

    def test_enumeration_does_not_descend_into_nested_directories(self):
        client = _sample_tree()

        self._enumerate(client)

        # 快速枚举只读取来源根一层，深层 Season 目录不产生额外请求。
        self.assertEqual(client.calls, ["root"])

    def test_root_group_is_skipped_without_root_level_media(self):
        client = _TreeClient({
            "root": [_dir("group-a", "作品 A", "root")],
            "group-a": [_video("a-video", "A.S01E01.mkv", "group-a")],
        })

        enumeration = self._enumerate(client)

        self.assertEqual(
            [task.group_path for task in enumeration.tasks], ["作品 A"]
        )

    def test_protected_source_roots_are_excluded(self):
        client = _sample_tree()

        enumeration = self._enumerate(client, protected_source_ids={"group-b"})

        self.assertEqual(
            [task.group_id for task in enumeration.tasks], ["root", "group-a"]
        )

    def test_protected_staging_is_reported_without_entering_the_directory(self):
        client = _TreeClient({
            "root": [_dir("stage", "MF-101-FixtureRelease", "root")],
            "stage": [_video("blocked", "123.mp4", "stage")],
        }, root_name="NewNsfw")

        enumeration = self._enumerate(client, protected_staging_ids={"stage"})

        self.assertTrue(enumeration.complete)
        self.assertEqual(client.calls, ["root"])
        blocked = enumeration.protected_staging_dirs
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["file_id"], "stage")
        self.assertEqual(blocked[0]["source_dir_id"], "root")
        self.assertEqual(blocked[0]["path"], "NewNsfw/MF-101-FixtureRelease")
        self.assertIn("未扫描", blocked[0]["reason"])
        self.assertIn("核验", blocked[0]["reason"])

    def test_configured_source_protection_is_not_reported_as_a_download(self):
        enumeration = self._enumerate(_sample_tree(), protected_source_ids={"group-b"})

        self.assertEqual(enumeration.protected_staging_dirs, [])

    def test_unreadable_source_fails_closed_without_partial_tasks(self):
        client = _sample_tree()
        client.list_dir_errors["root"] = RuntimeError("provider secret")

        enumeration = self._enumerate(client)

        self.assertFalse(enumeration.complete)
        self.assertEqual(enumeration.tasks, [])
        self.assertEqual(enumeration.errors, ["root: 目录读取失败"])
        self.assertNotIn("provider secret", json.dumps(enumeration.errors))

    def test_empty_source_still_yields_one_root_group(self):
        client = _TreeClient({"root": []})

        enumeration = self._enumerate(client)

        self.assertTrue(enumeration.complete)
        self.assertEqual(len(enumeration.tasks), 1)
        self.assertTrue(enumeration.tasks[0].is_root)
        self.assertEqual(enumeration.tasks[0].group_path, GROUP_ROOT_PATH)

    def test_cancelled_enumeration_reports_incomplete(self):
        client = _sample_tree()

        enumeration = self._enumerate(client, cancelled=lambda: True)

        self.assertFalse(enumeration.complete)
        self.assertEqual(enumeration.tasks, [])

    def test_directories_without_identity_are_ignored(self):
        client = _TreeClient({
            "root": [
                _dir("", "无 ID", "root"),
                _dir("group-a", "   ", "root"),
                _dir("group-b", "作品 B", "root"),
            ],
        })

        enumeration = self._enumerate(client)

        self.assertEqual([task.group_id for task in enumeration.tasks], ["group-b"])


class GroupModelTests(unittest.TestCase):
    """Task 1.1：媒体组任务与结果模型。"""

    @staticmethod
    def _task(**kwargs) -> OrganizeGroupTask:
        params = {
            "source_dir_id": "root",
            "source_name": "待整理",
            "group_id": "group-a",
            "group_path": "作品 A",
            "group_name": "作品 A",
            "index": 2,
            "total": 3,
        }
        params.update(kwargs)
        return OrganizeGroupTask(**params)

    def test_group_result_is_json_serializable(self):
        result = build_group_result(
            self._task(),
            {
                "total": 4,
                "moved": 3,
                "failed": 0,
                "strm_changes": [
                    {"rel_dir": "剧集/作品 A/Season 01", "name": "A - S01E01.mkv"},
                ],
                "media_items": [{"title": "作品 A"}],
                "empty_dirs_cleaned": 1,
            },
            elapsed_seconds=1.25,
        )

        payload = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))
        self.assertEqual(payload["status"], GROUP_STATUS_COMPLETED)
        self.assertEqual(payload["scanned"], 4)
        self.assertEqual(payload["moved"], 3)
        self.assertEqual(payload["identities"], ["作品 A"])
        self.assertEqual(payload["changed_target_dirs"], ["剧集/作品 A/Season 01"])
        self.assertEqual(
            payload["changed_strm_paths"], ["剧集/作品 A/Season 01/A - S01E01.mkv"]
        )
        self.assertEqual(payload["cleanup"]["empty_dirs_cleaned"], 1)
        self.assertEqual(payload["elapsed_seconds"], 1.25)

    def test_root_group_keeps_explicit_root_identity(self):
        task = self._task(group_id="root", group_path=GROUP_ROOT_PATH, is_root=True)

        self.assertEqual(task.key, f"root\x1f{GROUP_ROOT_PATH}")
        self.assertTrue(task.to_dict()["is_root"])

    def test_progress_row_keeps_legacy_frontend_fields(self):
        result = build_group_result(
            self._task(),
            {"total": 2, "moved": 1, "skipped": 1, "need_confirm": 0, "failed": 0},
        )

        row = result.progress_row()
        for key in ("id", "path", "name", "status", "total", "moved",
                    "metadata_moved", "skipped", "need_confirm", "failed"):
            self.assertIn(key, row)
        self.assertEqual(row["total"], 2)

    def test_failed_and_stopped_statuses_are_derived_from_stats(self):
        failed = build_group_result(self._task(), {}, error="整理失败")
        stopped = build_group_result(self._task(), {"stopped": 1})
        partial = build_group_result(self._task(), {"failed": 2})

        self.assertEqual(failed.status, GROUP_STATUS_FAILED)
        self.assertEqual(stopped.status, GROUP_STATUS_STOPPED)
        self.assertEqual(partial.status, "partial")


class GroupStatsMergeTests(unittest.TestCase):
    """Task 1.1：组级统计合并策略。"""

    def test_counters_sum_and_scan_complete_uses_and(self):
        aggregate: dict = {}

        merge_group_stats(aggregate, {"moved": 2, "failed": 0, "scan_complete": True})
        merge_group_stats(aggregate, {"moved": 3, "failed": 1, "scan_complete": False})

        self.assertEqual(aggregate["moved"], 5)
        self.assertEqual(aggregate["failed"], 1)
        self.assertFalse(aggregate["scan_complete"])

    def test_flag_style_counters_use_max_not_sum(self):
        aggregate: dict = {}

        merge_group_stats(aggregate, {"stopped": 1, "scan_limited": 1})
        merge_group_stats(aggregate, {"stopped": 1, "scan_limited": 1})

        self.assertEqual(aggregate["stopped"], 1)
        self.assertEqual(aggregate["scan_limited"], 1)

    def test_reason_lists_dedupe_within_limit(self):
        aggregate: dict = {}

        merge_group_stats(aggregate, {"confirmations": ["A", "B"]})
        merge_group_stats(aggregate, {"confirmations": ["B", "C", "D"]})

        self.assertEqual(aggregate["confirmations"], ["A", "B", "C"])

    def test_structured_lists_are_appended_without_dedupe(self):
        aggregate: dict = {}
        change = {"rel_dir": "a", "name": "x.mkv"}

        merge_group_stats(aggregate, {"strm_changes": [change]})
        merge_group_stats(aggregate, {"strm_changes": [change]})

        self.assertEqual(len(aggregate["strm_changes"]), 2)

    def test_pipeline_owned_keys_are_never_merged(self):
        aggregate = {"source_groups": ["kept"], "source_groups_total": 7}

        merge_group_stats(aggregate, {
            "source_groups": ["group"],
            "source_groups_total": 1,
            "source_groups_completed": 1,
            "current_source_group": "作品 A",
        })

        self.assertEqual(aggregate["source_groups"], ["kept"])
        self.assertEqual(aggregate["source_groups_total"], 7)
        self.assertNotIn("source_groups_completed", aggregate)

    def test_float_timings_accumulate(self):
        aggregate: dict = {}

        merge_group_stats(aggregate, {"scan_elapsed_seconds": 0.5})
        merge_group_stats(aggregate, {"scan_elapsed_seconds": 0.25})

        self.assertEqual(aggregate["scan_elapsed_seconds"], 0.75)

    def test_changed_paths_dedupe_and_ignore_incomplete_rows(self):
        changes: list[dict] = cast(list[dict], [
            {"rel_dir": "剧集/A/Season 01", "name": "E01.mkv"},
            {"rel_dir": "剧集/A/Season 01/", "name": "E02.mkv"},
            {"rel_dir": "剧集/A/Season 01", "name": "E01.mkv"},
            {"action": "remove", "file_id": "x"},
            "not-a-dict",
        ])

        self.assertEqual(changed_target_dirs(changes), ["剧集/A/Season 01"])
        self.assertEqual(
            changed_strm_paths(changes),
            ["剧集/A/Season 01/E01.mkv", "剧集/A/Season 01/E02.mkv"],
        )


class _PipelineHarness:
    """用桩化识别驱动真实的组级流水线时序。"""

    def __init__(self, client: _TreeClient, **rule_overrides):
        self.client = client
        rules = {
            "target_dir_id": "target",
            "clean_empty": False,
            "link_strm": False,
            "notify_enabled": False,
            "library_notify": False,
        }
        rules.update(rule_overrides)
        self.rules = OrganizeRules(**rules)
        self.organizer = Organizer(client=cast(GuangYaClient, client), scraper=cast(TMDBScraper, object()))
        self.events: list[str] = []
        self.execute_hook = None
        self.plan_hook = None

    def _build_plans(self, scan_result, context, rules, stats, performance_before, **_kwargs):
        group = ""
        plans = []
        for item in scan_result.scanned_videos:
            group = item.source_group_path
            plan = OrganizePlan(
                file_id=item.file.file_id,
                original_name=item.file.name,
                original_path=item.relative_dir,
                original_parent_id=item.file.parent_id or "0",
                size=item.file.size,
                action="move",
            )
            plan.source_group_id = item.source_group_id
            plan.source_group_path = item.source_group_path
            plans.append(plan)
        stats["total"] = len(plans)
        self.events.append(f"plan:{group or GROUP_ROOT_PATH}")
        if self.plan_hook is not None:
            self.plan_hook(group or GROUP_ROOT_PATH, stats)
        return OrganizePlanningResult(plans=plans, subtitle_plans_by_video={})

    def _execute(self, _organizer, plans, _rules, stats, *_args, **kwargs):
        group = plans[0].source_group_path if plans else GROUP_ROOT_PATH
        self.events.append(f"execute:{group}")
        stats["moved"] = len(plans)
        on_progress = kwargs.get("on_progress")
        if on_progress is not None:
            for index, _plan in enumerate(plans, start=1):
                on_progress(index, len(plans))
        if self.execute_hook is not None:
            self.execute_hook(group, stats)

    def run(self, **kwargs):
        with patch.object(Organizer, "_build_plans", self._build_plans), patch(
            "app.modules.organize.execute_organize_plans", side_effect=self._execute
        ):
            return self.organizer.organize(
                "root", self.rules, dry_run=False, post_actions=False, **kwargs
            )


class GroupPipelineExecutionTests(unittest.TestCase):
    """Task 1.3：逐媒体组完整计划与串行执行。"""

    def test_first_group_executes_before_next_group_is_scanned(self):
        harness = _PipelineHarness(_sample_tree())

        _plans, stats = harness.run()

        self.assertEqual(harness.events, [
            f"plan:{GROUP_ROOT_PATH}", f"execute:{GROUP_ROOT_PATH}",
            "plan:作品 A", "execute:作品 A",
            "plan:作品 B", "execute:作品 B",
        ])
        self.assertEqual(stats["source_groups_total"], 3)
        self.assertEqual(stats["source_groups_completed"], 3)
        self.assertEqual(stats["moved"], 3)
        self.assertTrue(stats["scan_complete"])
        self.assertEqual(stats["protected_staging_dirs"], [])

    def test_protected_download_makes_the_scan_partial_and_keeps_safe_media(self):
        for pipeline in (False, True):
            with self.subTest(group_pipeline=pipeline):
                client = _TreeClient({
                    "root": [
                        _dir("stage", "MF-101-FixtureRelease", "root"),
                        _dir("safe", "Normal fixture", "root"),
                    ],
                    "stage": [_dir("inner", "123", "stage")],
                    "inner": [_video("blocked", "123.mp4", "inner")],
                    "safe": [_video("normal", "Normal.mkv", "safe")],
                }, root_name="NewNsfw")
                harness = _PipelineHarness(client)

                plans, stats = harness.run(
                    group_pipeline=pipeline, require_complete_scan=True,
                    protected_staging_ids={"stage"},
                )

                self.assertEqual([plan.file_id for plan in plans], ["normal"])
                self.assertEqual(stats["moved"], 1)
                self.assertFalse(stats["scan_complete"])
                self.assertEqual(stats["scan_errors"], [])
                self.assertEqual(len(stats["protected_staging_dirs"]), 1)
                self.assertIn("MF-101-FixtureRelease", stats["skip_reasons"][0])
                self.assertNotIn("stage", client.calls)
                self.assertNotIn("inner", client.calls)

    def test_nested_protected_staging_is_reported_in_both_scan_modes(self):
        for pipeline in (False, True):
            with self.subTest(group_pipeline=pipeline):
                client = _TreeClient({
                    "root": [_dir("bundle", "Bundle", "root")],
                    "bundle": [_dir("stage", "MF-102-FixtureRelease", "bundle")],
                    "stage": [_video("blocked", "123.mp4", "stage")],
                }, root_name="NewNsfw")
                harness = _PipelineHarness(client)

                plans, stats = harness.run(
                    group_pipeline=pipeline, require_complete_scan=True,
                    protected_staging_ids={"stage"},
                )

                self.assertEqual(plans, [])
                self.assertEqual(stats["moved"], 0)
                self.assertFalse(stats["scan_complete"])
                self.assertEqual(stats["protected_staging_dirs"][0]["path"],
                                 "NewNsfw/Bundle/MF-102-FixtureRelease")
                self.assertNotIn("stage", client.calls)

    def test_empty_frozen_members_do_not_report_directories_outside_scope(self):
        client = _TreeClient({
            "root": [_dir("stage", "MF-103-FixtureRelease", "root")],
        })

        plans, stats = _PipelineHarness(client).run(
            source_member_ids=frozenset(), protected_staging_ids={"stage"},
        )

        self.assertEqual(plans, [])
        self.assertEqual(stats["protected_staging_dirs"], [])
        self.assertTrue(stats["scan_complete"])

    def test_first_group_latency_does_not_grow_with_remaining_groups(self):
        tree = {"root": [_dir(f"group-{i}", f"作品 {i}", "root") for i in range(100)]}
        for i in range(100):
            tree[f"group-{i}"] = [_video(f"video-{i}", f"S{i}.S01E01.mkv", f"group-{i}")]
        client = _TreeClient(tree)
        harness = _PipelineHarness(client)
        first_execute_calls: list[int] = []

        def record(_group, _stats):
            if not first_execute_calls:
                first_execute_calls.append(len(client.calls))

        harness.execute_hook = record
        harness.run()

        # 枚举 1 次 + 首组扫描 1 次；其余 99 组的识别耗时不进入首组关键路径。
        self.assertEqual(first_execute_calls, [2])
        self.assertEqual(len(harness.events), 200)

    def test_independent_root_files_are_scanned_in_separate_batches(self):
        client = _TreeClient({
            "root": [
                _video("movie-a", "Movie.A.2025.1080p.mkv", "root"),
                _video("movie-b", "Movie.B.2026.2160p.mkv", "root"),
            ],
        })
        harness = _PipelineHarness(client)
        batches: list[int] = []
        harness.execute_hook = lambda _group, stats: batches.append(stats["moved"])

        _plans, stats = harness.run()

        self.assertEqual(batches, [1, 1])
        self.assertEqual(stats["source_groups_total"], 2)
        self.assertEqual(stats["moved"], 2)

    def test_group_keeps_full_directory_context_in_one_plan_batch(self):
        client = _TreeClient({
            "root": [_dir("group-a", "作品 A", "root")],
            "group-a": [_dir("nested-a", "Season 01", "group-a")],
            "nested-a": [
                _video("a1", "A.S01E01.mkv", "nested-a"),
                _video("a2", "A.S01E02.mkv", "nested-a"),
            ],
        })
        harness = _PipelineHarness(client)
        batches: list[int] = []
        harness.execute_hook = lambda _group, stats: batches.append(stats["moved"])

        harness.run()

        # 同一作品的两集必须在同一批执行，禁止退化为逐文件盲目移动。
        self.assertEqual(batches, [2])

    def test_target_listing_cache_is_cleared_between_groups(self):
        harness = _PipelineHarness(_sample_tree())
        observed: list[int] = []

        def poison(_group, _stats):
            observed.append(len(harness.organizer._existing_variant_cache))
            harness.organizer._existing_variant_cache[cast(tuple[str, str, int, str], "stale")] = cast(MediaVariant, [])

        harness.plan_hook = poison
        harness.run()

        self.assertEqual(observed, [0, 0, 0])

    def test_source_level_probe_wall_clock_cap_degrades_to_cache_only(self):
        """探测累计耗时到达来源级上限后，剩余组只读缓存，不再在线探测。"""
        from app.modules import organize as organize_module

        harness = _PipelineHarness(_sample_tree())
        observed_cache_only: list[bool] = []

        original_build = harness._build_plans

        def build_with_context(*args, **_kwargs):
            # patch.object 会把纯函数当描述符绑定 organizer，参数取尾部五个。
            scan_result, context, rules, stats, performance_before = args[-5:]
            observed_cache_only.append(bool(context.probe_cache_only))
            result = original_build(scan_result, context, rules, stats, performance_before)
            stats["media_probe_elapsed_seconds"] = 1.0
            return result

        harness._build_plans = build_with_context
        with patch.object(organize_module, "_GROUP_PIPELINE_PROBE_CAP_SECONDS", 1.5):
            _plans, stats = harness.run()

        # 第 1 组累计 1s 未达上限，第 2 组累计 2s 达上限 → 第 3 组降级。
        self.assertEqual(observed_cache_only, [False, False, True])
        self.assertEqual(stats.get("media_probe_wall_clock_capped"), 1)

    def test_group_results_are_recorded_as_structured_data(self):
        harness = _PipelineHarness(_sample_tree())

        _plans, stats = harness.run()

        results = stats["group_results"]
        self.assertEqual(
            [row["group_path"] for row in results],
            [GROUP_ROOT_PATH, "作品 A", "作品 B"],
        )
        self.assertTrue(all(row["status"] == GROUP_STATUS_COMPLETED for row in results))
        json.dumps(results, ensure_ascii=False)


class GroupFailureIsolationTests(unittest.TestCase):
    """Task 1.4：组级失败隔离与来源级安全门。"""

    def test_group_failure_does_not_block_following_groups(self):
        harness = _PipelineHarness(_sample_tree())

        def fail_first_group(group, _stats):
            if group == "作品 A":
                raise RuntimeError("云盘写入失败")

        harness.execute_hook = fail_first_group
        _plans, stats = harness.run()

        statuses = {row["path"]: row["status"] for row in stats["source_groups"]}
        self.assertEqual(statuses["作品 A"], GROUP_STATUS_FAILED)
        self.assertEqual(statuses["作品 B"], GROUP_STATUS_COMPLETED)
        self.assertIn("execute:作品 B", harness.events)
        self.assertEqual(stats["failed"], 1)

    def test_failed_group_reports_public_error_without_provider_text(self):
        harness = _PipelineHarness(_sample_tree())

        def fail(group, _stats):
            if group == "作品 A":
                raise RuntimeError("provider-secret-token")

        harness.execute_hook = fail
        _plans, stats = harness.run()

        payload = json.dumps(stats["group_results"], ensure_ascii=False)
        self.assertNotIn("provider-secret-token", payload)
        failed = [row for row in stats["group_results"] if row["status"] == GROUP_STATUS_FAILED]
        self.assertTrue(failed[0]["error"])

    def test_unreadable_source_root_fails_the_whole_task(self):
        client = _sample_tree()
        client.list_dir_errors["root"] = RuntimeError("provider secret")
        harness = _PipelineHarness(client)

        with self.assertRaises(OrganizeScanUnsafeError) as raised:
            harness.run()

        self.assertNotIn("provider secret", str(raised.exception))
        self.assertEqual(harness.events, [])

    def test_incomplete_group_scan_fails_closed_for_the_whole_source(self):
        client = _sample_tree()
        client.list_dir_errors["nested-a"] = RuntimeError("provider secret")
        harness = _PipelineHarness(client)

        with self.assertRaises(OrganizeScanUnsafeError):
            harness.run(require_complete_scan=True)

        # 根组已完成，但扫描不完整的组必须阻断后续组的危险写入。
        self.assertNotIn("execute:作品 B", harness.events)

    def test_cancel_marks_remaining_groups_stopped(self):
        harness = _PipelineHarness(_sample_tree())
        cancel = threading.Event()

        def stop_after_first(_group, _stats):
            cancel.set()

        harness.execute_hook = stop_after_first
        _plans, stats = harness.run(cancel_event=cancel)

        statuses = [row["status"] for row in stats["source_groups"]]
        self.assertEqual(statuses[0], GROUP_STATUS_COMPLETED)
        self.assertEqual(statuses[1:], [GROUP_STATUS_STOPPED, GROUP_STATUS_STOPPED])
        self.assertEqual(stats["stopped"], 1)


class GroupProgressProjectionTests(unittest.TestCase):
    """Task 1.5：实时目录级进度投影。"""

    def test_progress_reports_index_group_and_stage(self):
        harness = _PipelineHarness(_sample_tree())
        snapshots: list[dict] = []

        harness.run(group_progress=lambda payload: snapshots.append(payload["progress"]))

        stages = [row["current_stage"] for row in snapshots]
        self.assertIn(GROUP_STAGE_SCAN, stages)
        self.assertIn(GROUP_STAGE_PLAN, stages)
        self.assertIn(GROUP_STAGE_EXECUTE, stages)
        indexes = [row["current_index"] for row in snapshots if row["current_index"]]
        self.assertEqual(sorted(set(indexes)), [1, 2, 3])
        self.assertEqual({row["total"] for row in snapshots}, {3})
        self.assertEqual(snapshots[-1]["completed"], 3)

    def test_progress_reports_file_level_position_within_group(self):
        client = _TreeClient({
            "root": [_dir("group-a", "作品 A", "root")],
            "group-a": [
                _video("a1", "A.S01E01.mkv", "group-a"),
                _video("a2", "A.S01E02.mkv", "group-a"),
            ],
        })
        harness = _PipelineHarness(client)
        snapshots: list[dict] = []

        harness.run(group_progress=lambda payload: snapshots.append(payload["progress"]))

        positions = [
            (row["current_file_index"], row["current_file_total"])
            for row in snapshots
            if row["current_file_total"]
        ]
        self.assertEqual(positions[:2], [(1, 2), (2, 2)])

    def test_progress_payload_carries_group_rows_for_partial_ui_update(self):
        harness = _PipelineHarness(_sample_tree())
        payloads: list[dict] = []

        harness.run(group_progress=payloads.append)

        self.assertTrue(payloads)
        rows = payloads[-1]["groups"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            [row["path"] for row in rows],
            [GROUP_ROOT_PATH, "作品 A", "作品 B"],
        )

    def test_progress_callback_failure_never_breaks_organize(self):
        harness = _PipelineHarness(_sample_tree())

        def broken(_payload):
            raise RuntimeError("ui down")

        _plans, stats = harness.run(group_progress=broken)

        self.assertEqual(stats["source_groups_completed"], 3)

    def test_telegram_line_aggregates_by_media_directory(self):
        from app.bot.handlers import _organize_group_progress_line

        line = _organize_group_progress_line({
            "group_progress": {
                "total": 100,
                "current_index": 17,
                "current_group": "作品 A",
                "current_stage_label": "执行整理",
            }
        })

        self.assertEqual(line, "媒体目录：17/100 · 作品 A · 执行整理")
        self.assertEqual(_organize_group_progress_line({}), "")
        self.assertEqual(_organize_group_progress_line({"group_progress": {}}), "")


class TaskStateProjectionTests(unittest.TestCase):
    """Task 1.5：任务状态对外暴露组级进度，供 Web 轮询局部更新。"""

    def setUp(self):
        self.enterContext(isolated_test_database())

    def _run_manager(self, snapshots: list[dict]):
        from app.modules import organize_tasks as tasks_module
        from app import database as db

        manager = tasks_module.OrganizeTaskManager()
        run_id = db.add_task_run("guangya_organize", "manual", result=json.dumps({"task_id": "task-a"}))
        manager._task = {"id": "task-a", "run_id": run_id, "status": "running", "stats": {}}

        class _NoopLock:
            def acquire(self, *_args, **_kwargs):
                return True

            def release(self):
                return None

        manager._lock = cast(CrossProcessLock, _NoopLock())
        manager._wake_download_tracker = lambda *_args, **_kwargs: None

        class _FakeOrganizer:
            @staticmethod
            def trigger_post_actions(*_args, **_kwargs):
                return None

            @staticmethod
            def notify_task_results(*_args, **_kwargs):
                return False

            @staticmethod
            def _append_reason(container, key, value, *, limit=20):
                bucket = container.setdefault(key, [])
                if value not in bucket and len(bucket) < limit:
                    bucket.append(value)

            def _validate_target_outside_source(self, *_args, **_kwargs):
                return None

            def organize(self, *_args, **kwargs):
                callback = kwargs.get("group_progress")
                assert callback is not None
                callback({
                    "progress": {
                        "total": 100,
                        "completed": 16,
                        "current_index": 17,
                        "current_group": "作品 Q",
                        "current_stage": "execute",
                        "current_stage_label": "执行整理",
                        "current_file_index": 2,
                        "current_file_total": 5,
                    },
                    "groups": [{"path": "作品 Q", "status": "running"}],
                })
                snapshots.append(manager.task_status())
                return [], {
                    "moved": 1,
                    "group_results": [{
                        "group_path": "作品 Q", "status": "completed", "moved": 1,
                    }],
                    "strm_changes": [
                        {"rel_dir": "剧集/作品 Q/Season 01", "name": "E01.mkv"},
                    ],
                }

        with patch.object(tasks_module, "Organizer", _FakeOrganizer), patch.object(
            tasks_module.OrganizeTaskManager, "_cleanup_download_staging",
            lambda *_args, **_kwargs: None,
        ), patch.object(
            tasks_module.OrganizeTaskManager, "_finalize_download_requests",
            lambda *_args, **_kwargs: None, create=True,
        ):
            manager._run(
                "task-a", run_id, [{"id": "root", "name": "待整理"}],
                OrganizeRules(target_dir_id="target"),
            )
        return manager

    def test_running_status_exposes_current_group_and_stage(self):
        snapshots: list[dict] = []

        self._run_manager(snapshots)

        self.assertTrue(snapshots)
        live = snapshots[-1]
        self.assertEqual(live["group_progress"]["current_index"], 17)
        self.assertEqual(live["group_progress"]["total"], 100)
        self.assertEqual(live["source_groups"], [{"path": "作品 Q", "status": "running"}])
        self.assertEqual(live["message"], "正在整理：待整理 · 17/100 · 作品 Q · 执行整理")

    def test_finished_task_clears_live_group_progress(self):
        snapshots: list[dict] = []

        manager = self._run_manager(snapshots)

        final = manager.task_status()
        self.assertNotIn(final["status"], {"running", "stopping"})
        self.assertEqual(final["group_progress"], {})

    def test_versioned_result_keeps_group_results_and_changed_dirs(self):
        """任务聚合不得丢弃组级结构化结果（曾经只合并 media_items）。"""
        snapshots: list[dict] = []

        manager = self._run_manager(snapshots)

        result = manager.task_status().get("result") or {}
        self.assertEqual(
            [row["group_path"] for row in result.get("groups", [])], ["作品 Q"]
        )
        self.assertEqual(result.get("changed_target_dirs"), ["剧集/作品 Q/Season 01"])


class ProtectedStagingTaskTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.enterContext(patch.object(
            socket.socket, "connect", side_effect=AssertionError("unexpected network"),
        ))

    def _run(self, *, gy_status="completed", safe_media=False):
        from app import database as db
        from app.modules import organize_tasks as tasks_module

        request_id, _ = db.create_download_request("staging-scan-fixture", "magnet")
        db.update_download_request(
            request_id, status="completed" if gy_status == "completed" else "downloading",
            gy_status=gy_status, gy_target_dir="stage", gy_isolated=1,
            gy_staging_name="MF-101-FixtureRelease", gy_staging_parent_dir="old-parent",
            gy_staging_cleanup_status="retained", gy_settle_stable_count=2,
            gy_settle_snapshot="old-stable-snapshot", organize_status="requires_manual",
            organize_started=1, organize_task_id="old-confirmation-task",
        )
        tree = {
            "root": [_dir("stage", "MF-101-FixtureRelease", "root")],
            "stage": [_dir("inner", "123", "stage")],
            "inner": [_video("blocked", "123.mp4", "inner")],
        }
        if safe_media:
            tree["root"].append(_video("safe", "Normal.mkv", "root"))
        client = _TreeClient(tree, root_name="NewNsfw")
        harness = _PipelineHarness(client)
        manager = tasks_module.OrganizeTaskManager()
        run_id = db.add_task_run("guangya_organize", "telegram", result=json.dumps({"task_id": "protected-task"}))
        manager._task = {"id": "protected-task", "run_id": run_id, "status": "running", "stats": {}}
        self.assertTrue(manager._lock.acquire(blocking=False))
        with patch.object(Organizer, "_build_plans", harness._build_plans), patch(
            "app.modules.organize.execute_organize_plans", side_effect=harness._execute,
        ), patch.object(Organizer, "trigger_post_actions"), patch.object(
            Organizer, "_validate_target_outside_source",
        ), patch.object(manager, "_wake_download_tracker"), patch.object(
            tasks_module, "resolve_organize_workers", return_value=1,
        ):
            manager._run(
                "protected-task", run_id, [{"id": "root", "name": "NewNsfw"}],
                harness.rules, trigger_type="telegram", client=client,
            )
        return manager.task_status(), client, db.get_download_request(request_id)

    def test_completed_moved_staging_is_explicitly_partial_without_releasing_protection(self):
        from app import database as db

        task, client, row = self._run()

        self.assertEqual(task["status"], "partial")
        self.assertEqual(task["stats"]["total"], 0)
        self.assertFalse(task["stats"]["scan_complete"])
        self.assertIn("受保护下载目录", task["message"])
        self.assertIn("MF-101-FixtureRelease", task["message"])
        self.assertIn("MF-101-FixtureRelease", task["error"])
        self.assertEqual(task["result"]["status"], "partial")
        self.assertEqual(task["result"]["protected_staging_dirs"][0]["file_id"], "stage")
        self.assertIn("MF-101-FixtureRelease", task["result"]["skip_reasons"][0])
        self.assertEqual(task["result"]["sources"][0]["status"], "partial")
        self.assertNotIn("stage", client.calls)
        self.assertIn("stage", db.list_protected_guangya_staging_ids())
        assert row is not None
        self.assertEqual(row["gy_staging_parent_dir"], "old-parent")
        self.assertEqual(row["gy_staging_cleanup_status"], "retained")
        self.assertEqual(row["organize_status"], "requires_manual")
        self.assertEqual(row["organize_task_id"], "old-confirmation-task")
        from app.bot.handlers import _organize_terminal_progress

        self.assertIn("MF-101-FixtureRelease", _organize_terminal_progress(task))

    def test_active_download_stays_blocked_while_ordinary_media_can_be_organized(self):
        task, client, _row = self._run(gy_status="downloading", safe_media=True)

        self.assertEqual(task["status"], "partial")
        self.assertEqual(task["stats"]["moved"], 1)
        self.assertNotIn("stage", client.calls)
        self.assertNotIn("inner", client.calls)

    def test_unknown_download_is_not_admitted_even_with_old_stable_snapshot(self):
        task, client, _row = self._run(gy_status="")

        self.assertEqual(task["status"], "partial")
        self.assertEqual(task["stats"]["moved"], 0)
        self.assertNotIn("stage", client.calls)


class GroupPipelineRollbackTests(unittest.TestCase):
    """回退开关：保留旧整源执行入口。"""

    def test_pipeline_can_be_disabled_per_call(self):
        harness = _PipelineHarness(_sample_tree())
        batches: list[int] = []
        harness.execute_hook = lambda _group, stats: batches.append(stats["moved"])

        _plans, stats = harness.run(group_pipeline=False)

        # 旧路径一次性规划并执行全部来源，不产生分组流水线结果。
        self.assertEqual(len([item for item in harness.events if item.startswith("execute:")]), 1)
        self.assertEqual(batches, [3])
        self.assertNotIn("group_results", stats)
        self.assertTrue(stats["scan_complete"])
        self.assertEqual(stats["protected_staging_dirs"], [])

    def test_preview_keeps_whole_source_arbitration(self):
        harness = _PipelineHarness(_sample_tree())
        context = OrganizeContext(source_dir_id="root", dry_run=True)

        self.assertFalse(harness.organizer._group_pipeline_enabled(context))

    def test_max_files_preview_limit_keeps_legacy_path(self):
        harness = _PipelineHarness(_sample_tree())
        context = OrganizeContext(source_dir_id="root", dry_run=False, max_files=10)

        self.assertFalse(harness.organizer._group_pipeline_enabled(context))

    def test_frozen_members_keep_group_pipeline(self):
        harness = _PipelineHarness(_sample_tree())
        frozen = frozenset({"root-video", "group-a", "nested-a", "a-video", "group-b", "b-video"})

        plans, stats = harness.run(source_member_ids=frozen)

        self.assertEqual({plan.file_id for plan in plans}, {"root-video", "a-video", "b-video"})
        self.assertEqual(len(stats["group_results"]), 3)
        self.assertEqual(stats["group_progress"]["total"], 3)
        self.assertEqual(harness.events.count("plan:__root__"), 1)
        self.assertEqual(len([event for event in harness.events if event.startswith("plan:")]), 3)

    def test_frozen_members_reach_parallel_planners_without_disabling_workers(self):
        from app.modules.scraper import TMDBScraper

        client = _sample_tree()
        client.tree["nested-a"].append(_video("late", "A.S01E02.mkv", "nested-a"))
        harness = _PipelineHarness(client)
        scraper = TMDBScraper(cast(TMDBClient, "offline-fixture"))
        self.addCleanup(scraper.close)
        harness.organizer.scraper = scraper
        frozen = frozenset({"group-a", "nested-a", "a-video", "group-b", "b-video"})
        barrier = threading.Barrier(2)
        harness.plan_hook = lambda *_args: barrier.wait(timeout=5)
        source_scan = Organizer._scan_source
        contexts = []

        def scan(instance, context, *args, **kwargs):
            contexts.append((context.source_member_ids, threading.get_ident()))
            return source_scan(instance, context, *args, **kwargs)

        with patch.object(Organizer, "_scan_source", scan), patch.object(
            Organizer, "_finalize_planning_result",
            lambda _self, _scan, planning, *_args, **_kwargs: planning,
        ):
            plans, stats = harness.run(
                source_member_ids=frozen, planning_workers=2, media_probe_cache_only=True,
            )

        self.assertEqual({plan.file_id for plan in plans}, {"a-video", "b-video"})
        self.assertEqual([scope for scope, _thread in contexts], [frozen, frozen])
        self.assertEqual(len({thread for _scope, thread in contexts}), 2)
        self.assertEqual(stats["planning_workers"], 2)
        self.assertEqual(len(stats["group_results"]), 2)
        self.assertEqual(stats["group_progress"]["total"], 2)
        self.assertEqual(stats["moved"], 2)
        self.assertIs(harness.organizer.client, client)
        self.assertEqual(len(client.list_dir("nested-a")), 2)

    def test_empty_frozen_members_are_not_unrestricted(self):
        harness = _PipelineHarness(_sample_tree())

        plans, stats = harness.run(source_member_ids=frozenset())

        self.assertEqual(plans, [])
        self.assertEqual(stats["moved"], 0)
        self.assertNotIn("group-a", harness.client.calls)
        self.assertNotIn("group-b", harness.client.calls)

    def test_group_rescan_excludes_members_added_after_enumeration(self):
        client = _sample_tree()
        harness = _PipelineHarness(client)
        frozen = frozenset({"root-video", "group-a", "nested-a", "a-video"})

        def after_root_plan(group, _stats):
            if group == GROUP_ROOT_PATH:
                client.tree["nested-a"].append(_video("late-video", "A.S01E02.mkv", "nested-a"))
                client.tree["group-a"].append(_dir("late-dir", "Season 2", "group-a"))
                client.tree["late-dir"] = [_video("late-child", "A.S02E01.mkv", "late-dir")]

        harness.plan_hook = after_root_plan
        # 任意其它读取均保持 live；成员约束与调用次数无关。
        self.assertEqual(len(client.list_dir("root")), 3)
        self.assertEqual(len(client.list_dir("root")), 3)
        plans, stats = harness.run(source_member_ids=frozen)
        self.assertEqual({plan.file_id for plan in plans}, {"root-video", "a-video"})
        self.assertEqual(len(stats["group_results"]), 2)
        self.assertEqual(stats["moved"], 2)
        self.assertNotIn("group-b", client.calls)
        self.assertNotIn("late-dir", client.calls)
        self.assertEqual(len(client.list_dir("nested-a")), 2)


if __name__ == "__main__":
    unittest.main()
