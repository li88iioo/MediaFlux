"""媒体组流水线（Sprint 1）需求驱动测试。

覆盖场景来自实施计划的验收条件：
- Task 1.1 媒体组任务/结果模型与组级统计合并
- Task 1.2 快速枚举媒体目录任务与失败关闭
- Task 1.3 逐媒体组完整计划与串行执行
- Task 1.4 组级失败隔离与来源级安全门
- Task 1.5 实时目录级进度投影
"""
from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import patch

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
        return GuangYaFile(str(file_id), self.root_name, True, parent_id="0")

    def list_dir(self, file_id: str):
        key = str(file_id)
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
        changes = [
            {"rel_dir": "剧集/A/Season 01", "name": "E01.mkv"},
            {"rel_dir": "剧集/A/Season 01/", "name": "E02.mkv"},
            {"rel_dir": "剧集/A/Season 01", "name": "E01.mkv"},
            {"action": "remove", "file_id": "x"},
            "not-a-dict",
        ]

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
        self.organizer = Organizer(client=client, scraper=object())
        self.events: list[str] = []
        self.execute_hook = None
        self.plan_hook = None

    def _build_plans(self, scan_result, context, rules, stats, performance_before):
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
            harness.organizer._existing_variant_cache["stale"] = []

        harness.plan_hook = poison
        harness.run()

        self.assertEqual(observed, [0, 0, 0])

    def test_source_level_probe_wall_clock_cap_degrades_to_cache_only(self):
        """探测累计耗时到达来源级上限后，剩余组只读缓存，不再在线探测。"""
        from app.modules import organize as organize_module

        harness = _PipelineHarness(_sample_tree())
        observed_cache_only: list[bool] = []

        original_build = harness._build_plans

        def build_with_context(*args):
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

    def _run_manager(self, snapshots: list[dict]):
        from app.modules import organize_tasks as tasks_module

        manager = tasks_module.OrganizeTaskManager()
        manager._task = {"id": "task-a", "status": "running", "stats": {}}

        class _NoopLock:
            def acquire(self, *_args, **_kwargs):
                return True

            def release(self):
                return None

        manager._lock = _NoopLock()
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
                "task-a", 0, [{"id": "root", "name": "待整理"}],
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

    def test_preview_keeps_whole_source_arbitration(self):
        harness = _PipelineHarness(_sample_tree())
        context = OrganizeContext(source_dir_id="root", dry_run=True)

        self.assertFalse(harness.organizer._group_pipeline_enabled(context))

    def test_max_files_preview_limit_keeps_legacy_path(self):
        harness = _PipelineHarness(_sample_tree())
        context = OrganizeContext(source_dir_id="root", dry_run=False, max_files=10)

        self.assertFalse(harness.organizer._group_pipeline_enabled(context))

    def test_scoped_client_disables_group_pipeline(self):
        client = _sample_tree()
        client.supports_group_pipeline = False
        harness = _PipelineHarness(client)
        batches: list[int] = []
        harness.execute_hook = lambda _group, stats: batches.append(stats["moved"])

        _plans, stats = harness.run()

        # 一次性作用域扫描窗口不得被逐组枚举提前消耗。
        self.assertEqual(batches, [3])
        self.assertNotIn("group_results", stats)


if __name__ == "__main__":
    unittest.main()
