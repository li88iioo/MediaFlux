"""整理并行规划与单 Writer 边界回归测试。"""
from __future__ import annotations

import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from app.modules.organize import (
    OrganizeContext,
    OrganizePlan,
    OrganizePlanningResult,
    Organizer,
    OrganizeRules,
    _PreparedOrganizeGroup,
    resolve_organize_workers,
)
from app.modules.organize_groups import (
    GROUP_STAGE_PENDING,
    GroupProgress,
    OrganizeGroupTask,
)
from app.modules.organize_tasks import OrganizeTaskManager
from app.modules.scraper import MatchResult


class _NoopLock:
    def acquire(self, *_args, **_kwargs):
        return True

    def release(self):
        return None


class _NoopLockContext:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class OrganizeWorkerBudgetTests(unittest.TestCase):
    def test_production_default_is_three_and_test_default_is_serial(self):
        with patch("app.modules.organize.get", return_value=""), patch.dict(
            os.environ, {"MEDIAFLUX_TEST_MODE": "0"}, clear=False
        ):
            self.assertEqual(resolve_organize_workers(), 3)
        with patch("app.modules.organize.get", return_value=""), patch.dict(
            os.environ, {"MEDIAFLUX_TEST_MODE": "1"}, clear=False
        ):
            self.assertEqual(resolve_organize_workers(), 1)

    def test_configured_budget_is_clamped_to_safe_range(self):
        with patch("app.modules.organize.get", return_value="99"):
            self.assertEqual(resolve_organize_workers(), 3)
        with patch("app.modules.organize.get", return_value="0"):
            self.assertEqual(resolve_organize_workers(), 1)


class OrganizeWholeSourceSingleWriterTests(unittest.TestCase):
    def test_scoped_whole_source_finalizes_and_executes_under_shared_gate(self):
        organizer = Organizer(client=SimpleNamespace(), scraper=object())
        gate = threading.Lock()
        context = OrganizeContext(
            source_dir_id="root",
            dry_run=False,
            post_actions=True,
            source_name="来源",
            group_pipeline=False,
            execution_lock=gate,
        )
        rules = OrganizeRules(
            target_dir_id="target",
            clean_empty=False,
            link_strm=False,
            notify_enabled=False,
            library_notify=False,
        )
        scan_result = SimpleNamespace()
        planning_result = OrganizePlanningResult(
            plans=[
                OrganizePlan(
                    file_id="video-1",
                    original_name="video-1.mkv",
                    original_path="/video-1.mkv",
                )
            ],
            subtitle_plans_by_video={},
        )
        events: list[str] = []

        def build(*_args, **kwargs):
            self.assertFalse(gate.locked())
            self.assertIs(kwargs.get("finalize"), False)
            events.append("plan")
            return planning_result

        def finalize(*_args, **_kwargs):
            self.assertTrue(gate.locked())
            events.append("finalize")
            return planning_result

        def execute(_scan, _planning, run_context, *_args, **_kwargs):
            self.assertTrue(gate.locked())
            self.assertFalse(run_context.post_actions)
            events.append("execute")

        def post_actions(*_args, **_kwargs):
            self.assertFalse(gate.locked())
            events.append("post")

        with patch.object(
            organizer, "_read_performance_snapshot", return_value={}
        ), patch.object(
            organizer, "_scan_source", return_value=scan_result
        ), patch.object(
            organizer, "_validate_scan_for_execution", return_value=None
        ), patch.object(
            organizer, "_build_plans", side_effect=build
        ), patch.object(
            organizer, "_finalize_planning_result", side_effect=finalize
        ), patch.object(
            organizer, "_run_execution_stage", side_effect=execute
        ), patch.object(
            organizer, "_run_post_actions", side_effect=post_actions
        ):
            plans, _stats = organizer._organize(context, rules)

        self.assertEqual([plan.file_id for plan in plans], ["video-1"])
        self.assertEqual(events, ["plan", "finalize", "execute", "post"])
        organizer.close()


class OrganizeGroupParallelPlanningTests(unittest.TestCase):
    def test_planning_overlaps_but_writer_commits_in_source_order(self):
        organizer = Organizer(client=SimpleNamespace(), scraper=object())
        tasks = [
            OrganizeGroupTask(
                source_dir_id="root",
                source_name="来源",
                group_id=f"group-{index}",
                group_path=f"作品 {index}",
                group_name=f"作品 {index}",
                index=index,
                total=3,
            )
            for index in range(1, 4)
        ]
        rows = {
            task.key: {
                "id": task.group_id,
                "path": task.group_path,
                "name": task.group_name,
                "status": "planned",
                "stage": GROUP_STAGE_PENDING,
                "index": task.index,
                "total": 0,
                "moved": 0,
                "metadata_moved": 0,
                "skipped": 0,
                "need_confirm": 0,
                "failed": 0,
            }
            for task in tasks
        }
        context = OrganizeContext(
            source_dir_id="root",
            dry_run=False,
            post_actions=False,
            source_name="来源",
        )
        rules = OrganizeRules(
            target_dir_id="target",
            clean_empty=False,
            link_strm=False,
            notify_enabled=False,
            library_notify=False,
        )
        planning_barrier = threading.Barrier(2)
        counter_lock = threading.Lock()
        planning_active = 0
        planning_peak = 0
        writer_active = 0
        writer_peak = 0
        commit_order: list[str] = []

        def prepare(_planner, task, _context, _rules, **_kwargs):
            nonlocal planning_active, planning_peak
            with counter_lock:
                planning_active += 1
                planning_peak = max(planning_peak, planning_active)
            try:
                if task.index <= 2:
                    planning_barrier.wait(timeout=2)
                # 让后组先完成规划，验证 coordinator 仍按原任务顺序提交。
                time.sleep(0.04 if task.index == 1 else 0.005)
                stats = organizer._initial_stats()
                stats["total"] = 1
                plan = OrganizePlan(
                    file_id=task.group_id,
                    original_name=f"{task.group_name}.mkv",
                    original_path=task.group_path,
                    action="move",
                )
                return _PreparedOrganizeGroup(
                    task=task,
                    stats=stats,
                    scan_result=SimpleNamespace(),
                    planning_result=OrganizePlanningResult(
                        plans=[plan], subtitle_plans_by_video={}
                    ),
                    planning_elapsed_seconds=0.01,
                )
            finally:
                with counter_lock:
                    planning_active -= 1

        def execute(_scan, planning, _context, _rules, stats, **_kwargs):
            nonlocal writer_active, writer_peak
            with counter_lock:
                writer_active += 1
                writer_peak = max(writer_peak, writer_active)
            try:
                time.sleep(0.005)
                commit_order.append(planning.plans[0].file_id)
                stats["moved"] = 1
            finally:
                with counter_lock:
                    writer_active -= 1

        with patch.object(
            organizer, "_prepare_group_plan", side_effect=prepare
        ), patch.object(
            organizer,
            "_finalize_planning_result",
            side_effect=lambda _scan, planning, *_args, **_kwargs: planning,
        ), patch.object(
            organizer, "_run_execution_stage", side_effect=execute
        ), patch.object(
            organizer, "_read_performance_snapshot", return_value={}
        ):
            plans, stats = organizer._organize_groups_parallel(
                context,
                rules,
                tasks=tasks,
                rows=rows,
                stats=organizer._initial_stats(),
                progress=GroupProgress(total=3, started_at=time.monotonic()),
                total_started=time.monotonic(),
                workers=2,
            )

        self.assertEqual(planning_peak, 2)
        self.assertEqual(writer_peak, 1)
        self.assertEqual(commit_order, ["group-1", "group-2", "group-3"])
        self.assertEqual([plan.file_id for plan in plans], commit_order)
        self.assertEqual(stats["moved"], 3)
        self.assertEqual(stats["planning_workers"], 2)
        self.assertEqual(stats["media_probe_worker_budget"], 4)
        self.assertEqual(stats["media_probe_workers_per_planner"], 4)
        organizer.close()

    def test_shared_executor_waits_for_running_planners_before_returning_on_cancel(self):
        organizer = Organizer(client=SimpleNamespace(), scraper=object())
        tasks = [
            OrganizeGroupTask(
                source_dir_id="root",
                source_name="来源",
                group_id=f"group-{index}",
                group_path=f"作品 {index}",
                group_name=f"作品 {index}",
                index=index,
                total=2,
            )
            for index in range(1, 3)
        ]
        rows = {
            task.key: {
                "id": task.group_id,
                "path": task.group_path,
                "name": task.group_name,
                "status": "planned",
                "stage": GROUP_STAGE_PENDING,
                "index": task.index,
                "total": 0,
                "moved": 0,
                "metadata_moved": 0,
                "skipped": 0,
                "need_confirm": 0,
                "failed": 0,
            }
            for task in tasks
        }
        cancel = threading.Event()
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        release_second = threading.Event()
        errors: list[BaseException] = []
        shared_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="shared-plan-test",
        )
        context = OrganizeContext(
            source_dir_id="root",
            dry_run=False,
            post_actions=False,
            cancel_event=cancel,
            planning_executor=shared_executor,
        )

        def prepare(_planner, task, *_args, **_kwargs):
            if task.index == 1:
                first_started.set()
                release_first.wait(timeout=2)
            else:
                second_started.set()
                release_second.wait(timeout=2)
            return _PreparedOrganizeGroup(
                task=task,
                stats=organizer._initial_stats(),
                planning_elapsed_seconds=0.01,
            )

        def run() -> None:
            try:
                organizer._organize_groups_parallel(
                    context,
                    OrganizeRules(
                        target_dir_id="target",
                        clean_empty=False,
                        link_strm=False,
                        notify_enabled=False,
                        library_notify=False,
                    ),
                    tasks=tasks,
                    rows=rows,
                    stats=organizer._initial_stats(),
                    progress=GroupProgress(total=2, started_at=time.monotonic()),
                    total_started=time.monotonic(),
                    workers=2,
                )
            except BaseException as exc:  # pragma: no cover - 主线程统一断言
                errors.append(exc)

        caller = threading.Thread(target=run)
        try:
            with patch.object(organizer, "_prepare_group_plan", side_effect=prepare):
                caller.start()
                self.assertTrue(first_started.wait(timeout=1))
                self.assertTrue(second_started.wait(timeout=1))
                cancel.set()
                release_first.set()
                time.sleep(0.08)
                # 第二个 Future 已在运行，cancel() 无法终止它；来源调用必须
                # 等它退出后才能关闭 Planner 并归还共享执行池。
                self.assertTrue(caller.is_alive())
                release_second.set()
                caller.join(timeout=2)
        finally:
            release_first.set()
            release_second.set()
            caller.join(timeout=2)
            shared_executor.shutdown(wait=True, cancel_futures=True)
            organizer.close()

        self.assertEqual(errors, [])
        self.assertFalse(caller.is_alive())

    def test_parallel_planners_force_refresh_same_tmdb_identity_once(self):
        class _SharedDetailScraper:
            def __init__(self):
                self.lock = threading.Lock()
                self.cached = {"name": "旧详情"}
                self.force_refreshes = 0

            def get_detail(self, _tmdb_id, _media_type, *, force_refresh=False):
                with self.lock:
                    if force_refresh:
                        self.force_refreshes += 1
                        time.sleep(0.02)
                        self.cached = {"name": "新详情"}
                    return dict(self.cached)

        scraper = _SharedDetailScraper()
        coordinator = Organizer(client=SimpleNamespace(), scraper=scraper)
        planners = [
            Organizer(client=SimpleNamespace(), scraper=scraper)
            for _ in range(2)
        ]
        key = ("42", "tv")
        for planner in planners:
            planner._detail_cache[key] = {"name": "旧详情"}
            coordinator._share_parallel_planning_state(planner)

        match = MatchResult(
            tmdb_id="42",
            title="测试剧",
            media_type="tv",
            provider="tmdb",
        )
        barrier = threading.Barrier(2)
        results: list[tuple[dict, bool]] = []
        result_lock = threading.Lock()

        def refresh(planner: Organizer) -> None:
            barrier.wait(timeout=2)
            result = planner._refresh_tmdb_detail_once(match)
            with result_lock:
                results.append(result)

        threads = [threading.Thread(target=refresh, args=(planner,)) for planner in planners]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(scraper.force_refreshes, 1)
        self.assertEqual([detail["name"] for detail, _refreshed in results], [
            "新详情",
            "新详情",
        ])
        self.assertEqual(sorted(refreshed for _detail, refreshed in results), [False, True])
        for planner in planners:
            planner.close()
        coordinator.close()


class OrganizeSourceParallelPlanningTests(unittest.TestCase):
    def _run_manager(
        self,
        rules: OrganizeRules,
        *,
        expected_source_workers: int = 2,
        probe_workers: int = 4,
        source_count: int = 2,
    ):
        state_lock = threading.Lock()
        barrier = (
            threading.Barrier(expected_source_workers)
            if expected_source_workers > 1 else None
        )
        planning_active = 0
        planning_peak = 0
        writer_active = 0
        writer_peak = 0
        organize_kwargs: list[dict] = []

        class _FakeOrganizer:
            def __init__(self, client=None):
                self.client = client if client is not None else object()

            @staticmethod
            def _append_reason(container, key, value, *, limit=20):
                bucket = container.setdefault(key, [])
                if value not in bucket and len(bucket) < limit:
                    bucket.append(value)

            @staticmethod
            def trigger_post_actions(*_args, **_kwargs):
                return None

            @staticmethod
            def notify_task_results(*_args, **_kwargs):
                return False

            @staticmethod
            def notify_task_confirmations(*_args, **_kwargs):
                return False

            def _validate_target_outside_source(self, *_args, **_kwargs):
                return None

            def organize(self, source_id, _rules, **kwargs):
                nonlocal planning_active, planning_peak, writer_active, writer_peak
                organize_kwargs.append(kwargs)
                with state_lock:
                    planning_active += 1
                    planning_peak = max(planning_peak, planning_active)
                try:
                    if barrier is not None and source_id in {
                        f"source-{chr(ord('a') + index)}"
                        for index in range(expected_source_workers)
                    }:
                        barrier.wait(timeout=2)
                    time.sleep(0.01)
                finally:
                    with state_lock:
                        planning_active -= 1
                gate = kwargs.get("execution_lock")
                if expected_source_workers > 1 and gate is None:
                    raise AssertionError("并行来源必须共享 execution_lock")
                if expected_source_workers <= 1 and gate is not None:
                    raise AssertionError("串行来源不应创建 execution_lock")
                gate_context = gate if gate is not None else _NoopLockContext()
                with gate_context:
                    with state_lock:
                        writer_active += 1
                        writer_peak = max(writer_peak, writer_active)
                    try:
                        time.sleep(0.01)
                    finally:
                        with state_lock:
                            writer_active -= 1
                return [], {
                    "total": 1,
                    "moved": 1,
                    "failed": 0,
                    "scan_errors": [],
                    "strm_changes": [
                        {"rel_dir": f"剧集/{source_id}", "name": "E01.mkv"}
                    ],
                }

            def close(self):
                return True

        manager = OrganizeTaskManager()
        manager._lock = _NoopLock()
        manager._task = {"id": "parallel-task", "status": "running", "stats": {}}
        manager._wake_download_tracker = lambda *_args, **_kwargs: None
        sources = [
            {
                "id": f"source-{chr(ord('a') + index)}",
                "name": f"来源 {chr(ord('A') + index)}",
            }
            for index in range(source_count)
        ]
        with patch(
            "app.modules.organize_tasks.Organizer", _FakeOrganizer
        ), patch(
            "app.modules.organize_tasks.resolve_organize_workers", return_value=2
        ), patch(
            "app.modules.organize_tasks.resolve_media_probe_workers",
            return_value=probe_workers,
        ), patch(
            "app.modules.organize_tasks._cleanup_manual_source_root",
            return_value=None,
        ):
            manager._run("parallel-task", 0, sources, rules)
        return manager, planning_peak, writer_peak, organize_kwargs

    def test_two_sources_share_budget_and_single_writer(self):
        manager, planning_peak, writer_peak, kwargs = self._run_manager(
            OrganizeRules(
                target_dir_id="target",
                clean_empty=False,
                link_strm=False,
                notify_enabled=False,
                library_notify=False,
            )
        )

        final = manager.task_status()
        self.assertEqual(final["status"], "completed")
        self.assertEqual(planning_peak, 2)
        self.assertEqual(writer_peak, 1)
        self.assertEqual(final["stats"]["source_workers"], 2)
        self.assertEqual(final["stats"]["organize_worker_budget"], 2)
        self.assertEqual(
            [item["id"] for item in final["source_results"]],
            ["source-a", "source-b"],
        )
        self.assertEqual([item["planning_workers"] for item in kwargs], [2, 2])
        self.assertEqual([item["media_probe_workers"] for item in kwargs], [4, 4])
        self.assertIs(
            kwargs[0]["planning_executor"], kwargs[1]["planning_executor"],
        )
        self.assertIsNotNone(kwargs[0]["planning_executor"])
        self.assertIs(kwargs[0]["execution_lock"], kwargs[1]["execution_lock"])

    def test_short_sources_release_the_shared_pool_to_the_remaining_source(self):
        manager, planning_peak, writer_peak, kwargs = self._run_manager(
            OrganizeRules(
                target_dir_id="target",
                clean_empty=False,
                link_strm=False,
                notify_enabled=False,
                library_notify=False,
            ),
            source_count=3,
        )

        final = manager.task_status()
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["stats"]["source_workers"], 2)
        self.assertEqual(planning_peak, 2)
        self.assertEqual(writer_peak, 1)
        self.assertEqual(len(kwargs), 3)
        shared_pool = kwargs[0]["planning_executor"]
        self.assertIsNotNone(shared_pool)
        self.assertTrue(all(item["planning_executor"] is shared_pool for item in kwargs))
        self.assertEqual([item["planning_workers"] for item in kwargs], [2, 2, 2])
        self.assertEqual(
            [item["id"] for item in final["source_results"]],
            ["source-a", "source-b", "source-c"],
        )

    def test_media_probe_budget_caps_parallel_sources(self):
        manager, planning_peak, writer_peak, kwargs = self._run_manager(
            OrganizeRules(
                target_dir_id="target",
                clean_empty=False,
                link_strm=False,
                notify_enabled=False,
                library_notify=False,
            ),
            expected_source_workers=1,
            probe_workers=1,
        )

        final = manager.task_status()
        self.assertEqual(final["status"], "completed")
        self.assertEqual(planning_peak, 1)
        self.assertEqual(writer_peak, 1)
        self.assertEqual(final["stats"]["source_workers"], 1)
        self.assertEqual(final["stats"]["media_probe_worker_budget"], 1)
        self.assertEqual([item["planning_workers"] for item in kwargs], [2, 2])
        self.assertEqual([item["media_probe_workers"] for item in kwargs], [1, 1])
        self.assertEqual([item["planning_executor"] for item in kwargs], [None, None])

    def test_nsfw_sources_fall_back_to_serial_source_dispatch(self):
        manager, planning_peak, writer_peak, kwargs = self._run_manager(
            OrganizeRules(
                target_dir_id="target",
                clean_empty=False,
                link_strm=False,
                notify_enabled=False,
                library_notify=False,
                nsfw_enabled=True,
                nsfw_source_ids='["source-a","source-b"]',
            ),
            expected_source_workers=1,
        )

        final = manager.task_status()
        self.assertEqual(final["status"], "completed")
        self.assertEqual(planning_peak, 1)
        self.assertEqual(writer_peak, 1)
        self.assertEqual(final["stats"]["source_workers"], 1)
        self.assertEqual([item["planning_workers"] for item in kwargs], [2, 2])
        self.assertEqual([item["media_probe_workers"] for item in kwargs], [4, 4])
        self.assertEqual([item["planning_executor"] for item in kwargs], [None, None])
        self.assertEqual([item["execution_lock"] for item in kwargs], [None, None])

    def test_parallel_source_failure_keeps_completed_peer_result(self):
        barrier = threading.Barrier(2)

        class _PartiallyFailingOrganizer:
            def __init__(self, client=None):
                self.client = client if client is not None else object()

            @staticmethod
            def _append_reason(container, key, value, *, limit=20):
                bucket = container.setdefault(key, [])
                if value not in bucket and len(bucket) < limit:
                    bucket.append(value)

            @staticmethod
            def trigger_post_actions(*_args, **_kwargs):
                return None

            @staticmethod
            def notify_task_results(*_args, **_kwargs):
                return False

            @staticmethod
            def notify_task_confirmations(*_args, **_kwargs):
                return False

            def _validate_target_outside_source(self, *_args, **_kwargs):
                return None

            def organize(self, source_id, _rules, **kwargs):
                barrier.wait(timeout=2)
                if source_id == "source-a":
                    raise RuntimeError("private provider failure")
                gate = kwargs["execution_lock"]
                with gate:
                    time.sleep(0.01)
                return [], {
                    "total": 1,
                    "moved": 1,
                    "failed": 0,
                    "scan_errors": [],
                    "strm_changes": [],
                }

            def close(self):
                return True

        manager = OrganizeTaskManager()
        manager._lock = _NoopLock()
        manager._task = {"id": "partial-task", "status": "running", "stats": {}}
        manager._wake_download_tracker = lambda *_args, **_kwargs: None
        with patch(
            "app.modules.organize_tasks.Organizer", _PartiallyFailingOrganizer
        ), patch(
            "app.modules.organize_tasks.resolve_organize_workers", return_value=2
        ), patch(
            "app.modules.organize_tasks.resolve_media_probe_workers", return_value=4
        ), patch(
            "app.modules.organize_tasks._cleanup_manual_source_root",
            return_value=None,
        ):
            manager._run(
                "partial-task",
                0,
                [
                    {"id": "source-a", "name": "来源 A"},
                    {"id": "source-b", "name": "来源 B"},
                ],
                OrganizeRules(
                    target_dir_id="target",
                    clean_empty=False,
                    link_strm=False,
                    notify_enabled=False,
                    library_notify=False,
                ),
            )

        final = manager.task_status()
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["stats"]["moved"], 1)
        self.assertEqual(
            [item["id"] for item in final["source_results"]], ["source-b"]
        )
        self.assertNotIn("private provider failure", final["error"])


if __name__ == "__main__":
    unittest.main()
