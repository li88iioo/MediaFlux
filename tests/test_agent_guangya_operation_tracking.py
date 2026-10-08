from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app import database as db
from app.agent.domain_catalog.cloud_runtime import guangya_organize_status
from app.agent.effect_completion import (
    _CompletionTracker,
    _patrol_status,
    wait_for_effect_completion,
)
from app.agent.kernel.ports.existing_actions import adapt_tool_spec
from app.agent.kernel.state import CancellationToken, InMemorySessionStateStore
from app.agent.models import RiskLevel, ToolContext, ToolReference, ToolResult, ToolSpec

from app.modules.local_media_models import LOCAL_BUSY_TASK_STATUSES
from app.modules.local_media_scan_runs import (
    finish_local_media_scan_report,
    record_local_media_scan,
    resolve_local_media_scan,
)
from tests.support import isolated_test_database

_OPERATION_REF = "GY-0000-0000-0000-0000-0000-0000-0000-0001"


def _snapshot(status: str, *, stats: dict[str, int] | None = None) -> ToolResult:
    return ToolResult(
        ok=status in {"queued", "running", "completed"},
        status=status,
        summary=f"snapshot:{status}",
        data={
            "task": {
                "status": status,
                "running": status == "running",
                "stats": stats or {},
                "started_at": "2026-09-19T00:00:00+08:00",
                "finished_at": ""
                if status in {"queued", "running"}
                else "2026-09-19T00:00:02+08:00",
            },
            "queue": {"pending_count": 1 if status == "queued" else 0},
        },
    )


def _provider_accepted(operation: str, count: int) -> ToolResult:
    return ToolResult(
        True,
        "accepted",
        "Provider 已受理",
        data={"count": count, "verified": False, "verification_pending": True},
        model_data={"count": count, "verified": False, "verification_pending": True},
        references=[
            ToolReference(
                "guangya_task",
                {"task_id": "private-task", "operation": operation},
            )
        ],
    )


def _tracked_accepted(kind: str, **completion) -> ToolResult:
    return ToolResult(
        True,
        "accepted",
        "后台任务已提交",
        data={"accepted": True},
        effect_metadata={"completion": {"kind": kind, **completion}},
    )


class GuangYaOperationTrackingTests(unittest.IsolatedAsyncioTestCase):
    def test_status_binds_public_ref_to_owner_and_keeps_safe_stats(self) -> None:
        manager = Mock()
        manager.status.return_value = {
            "operation_queue": {"total": 0},
            "schedule": {},
        }
        manager.task_result.return_value = {
            "status": "completed",
            "stats": {
                "relocated": 1,
                "created": 2,
                "trashed": 3,
                "strm_scope_unknown": 1,
                "strm_trigger_skipped": 1,
                "secret": 99,
            },
            "stoppable": False,
        }
        with patch(
            "app.modules.organize_tasks.get_organize_manager", return_value=manager
        ):
            result = guangya_organize_status(
                {"operation_ref": _OPERATION_REF},
                ToolContext(owner="webk:v1:owner-safe"),
            )

        manager.task_result.assert_called_once_with(
            _OPERATION_REF, owner="webk:v1:owner-safe"
        )
        self.assertEqual(
            result.data["task"]["stats"],
            {
                "relocated": 1,
                "created": 2,
                "trashed": 3,
                "strm_scope_unknown": 1,
                "strm_trigger_skipped": 1,
            },
        )
        self.assertNotIn("secret", result.data["task"]["stats"])
        self.assertIn("未触发 STRM 联动", " ".join(result.suggestions))

    def test_copy_pending_queued_status_projects_safe_resume_summary(self) -> None:
        from app.repositories.organize_operation_jobs import CLOUD_COPY_PENDING_CODE

        manager = Mock()
        manager.status.return_value = {
            "operation_queue": {"total": 1},
            "schedule": {},
        }
        manager.task_result.return_value = {
            "status": "queued",
            "error_code": CLOUD_COPY_PENDING_CODE,
            "task_id": "private-copy-task-id",
            "running": False,
        }
        with patch(
            "app.modules.organize_tasks.get_organize_manager", return_value=manager
        ):
            result = guangya_organize_status(
                {"operation_ref": _OPERATION_REF},
                ToolContext(owner="webk:v1:owner-safe"),
            )

        self.assertEqual(result.status, "queued")
        self.assertEqual(result.data["task"]["status"], "queued")
        self.assertNotIn("private-copy-task-id", json.dumps(result.to_dict()))
        self.assertEqual(result.summary, "光鸭复制仍在处理中，完成后将继续原计划")

    async def test_waits_on_persistent_snapshots_and_reports_safe_progress(
        self,
    ) -> None:
        accepted = ToolResult(
            True,
            "accepted",
            "已提交",
            data={
                "operation_ref": _OPERATION_REF,
                "total": 4,
                "relocate_count": 2,
                "cloud_write": False,
            },
            model_data={"total": 4, "cloud_write": False},
        )
        progress: list[dict] = []
        snapshots = iter(
            (
                _snapshot("queued"),
                _snapshot("running"),
                _snapshot("completed", stats={"relocated": 2, "created": 1}),
            )
        )

        async def report(payload):
            progress.append(dict(payload))

        with (
            patch(
                "app.agent.domain_catalog.cloud_runtime.guangya_organize_status",
                side_effect=lambda *_args, **_kwargs: next(snapshots),
            ) as status,
            patch(
                "app.agent.effect_completion.asyncio.sleep",
                new=AsyncMock(),
            ) as sleep,
        ):
            result = await wait_for_effect_completion(
                accepted,
                tool="guangya.fs.change.execute",
                context=ToolContext(owner="tg:v1:owner-safe"),
                report_progress=report,
            )

        self.assertEqual(result.status, "completed")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["total"], 4)
        self.assertEqual(result.data["relocate_count"], 2)
        self.assertEqual(result.data["operation_ref"], _OPERATION_REF)
        self.assertEqual(result.data["background_job"]["stats"]["relocated"], 2)
        self.assertNotIn("cloud_write", result.data)
        self.assertNotIn("cloud_write", result.to_model_dict()["data"])
        self.assertEqual(
            result.to_model_dict()["data"]["background_job"]["status"], "completed"
        )
        self.assertEqual(status.call_count, 3)
        self.assertEqual(sleep.await_count, 2)
        self.assertEqual(
            [item["phase"] for item in progress],
            ["background_job", "background_job", "background_job"],
        )
        self.assertEqual(
            [item["status"] for item in progress], ["queued", "running", "completed"]
        )
        self.assertTrue(
            all(item["operation_ref"] == _OPERATION_REF for item in progress)
        )
        self.assertTrue(
            all(item["tool"] == "guangya.fs.change.execute" for item in progress)
        )
        self.assertNotIn("owner", progress[0])

    async def test_organize_completion_waits_across_worker_yield_and_reclaim(
        self,
    ) -> None:
        cases = (
            (
                "operation_ref",
                ToolResult(
                    True,
                    "accepted",
                    "已提交",
                    data={"operation_ref": _OPERATION_REF},
                ),
                "app.agent.domain_catalog.cloud_runtime.guangya_organize_status",
            ),
            (
                "private_task_id",
                _tracked_accepted(
                    "guangya_organize_task",
                    task_id="private-organize-task",
                    operation="run",
                ),
                "app.agent.domain_catalog.cloud_runtime.guangya_organize_task_status",
            ),
        )
        expected_statuses = ["running", "queued", "running", "completed"]

        for label, accepted, status_target in cases:
            with self.subTest(tracker=label):
                snapshots = iter(
                    _snapshot(status, stats={"copied": 1})
                    for status in expected_statuses
                )
                progress: list[dict] = []

                async def report(payload):
                    progress.append(dict(payload))

                with (
                    patch(
                        status_target,
                        side_effect=lambda *_args, _snapshots=snapshots, **_kwargs: (
                            next(_snapshots)
                        ),
                    ) as status,
                    patch(
                        "app.agent.effect_completion.asyncio.sleep",
                        new=AsyncMock(),
                    ) as sleep,
                ):
                    result = await wait_for_effect_completion(
                        accepted,
                        tool="guangya.fs.change.execute",
                        context=ToolContext(owner="tg:v1:owner-safe"),
                        report_progress=report,
                        timeout_seconds=60,
                    )

                self.assertTrue(result.ok)
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.data["background_job"]["status"], "completed")
                self.assertEqual(status.call_count, 4)
                self.assertEqual(sleep.await_count, 3)
                self.assertEqual(
                    [event["status"] for event in progress], expected_statuses
                )
                self.assertEqual(progress[-1]["status"], "completed")
                if label == "private_task_id":
                    self.assertNotIn("private-organize-task", str(result.to_dict()))
                    self.assertNotIn(
                        "private-organize-task", json.dumps(progress, ensure_ascii=False)
                    )

    async def test_waits_for_provider_task_until_recycle_clear_completes(self) -> None:
        snapshots = iter(
            (
                ToolResult(
                    True, "running", "光鸭任务仍在处理中", data={"progress": 0.5}
                ),
                ToolResult(True, "completed", "光鸭任务已完成", data={"progress": 1.0}),
            )
        )
        progress = AsyncMock()
        with (
            patch(
                "app.agent.guangya_recycle_actions.query_guangya_task_status",
                side_effect=lambda *_args, **_kwargs: next(snapshots),
            ) as status,
            patch(
                "app.agent.effect_completion.asyncio.sleep",
                new=AsyncMock(),
            ) as sleep,
        ):
            result = await wait_for_effect_completion(
                _provider_accepted("recycle_clear", 247),
                tool="guangya.recycle.clear",
                context=ToolContext(owner="tg:v1:owner-safe"),
                report_progress=progress,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.summary, "光鸭回收站已清空，共永久删除 247 个对象")
        self.assertTrue(result.data["verified"])
        self.assertFalse(result.data["verification_pending"])
        self.assertEqual(result.data["background_job"]["progress"], 1.0)
        self.assertTrue(result.model_data["verified"])
        self.assertEqual(status.call_count, 2)
        self.assertEqual(sleep.await_count, 1)
        self.assertEqual(
            [call.args[0]["status"] for call in progress.await_args_list],
            ["running", "completed"],
        )
        self.assertNotIn("private-task", str(result.to_dict()))

    async def test_provider_failure_and_timeout_never_claim_completion(self) -> None:
        failed = ToolResult(False, "failed", "光鸭任务执行失败", data={"progress": 0.4})
        with patch(
            "app.agent.guangya_recycle_actions.query_guangya_task_status",
            return_value=failed,
        ):
            result = await wait_for_effect_completion(
                _provider_accepted("recycle_restore", 2),
                tool="guangya.recycle.restore",
                context=ToolContext(owner="owner"),
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.summary, "光鸭回收站恢复任务执行失败")
        self.assertFalse(result.data["verification_pending"])

        running = ToolResult(
            True, "running", "光鸭任务仍在处理中", data={"progress": 0.2}
        )
        with patch(
            "app.agent.guangya_recycle_actions.query_guangya_task_status",
            return_value=running,
        ) as status:
            result = await wait_for_effect_completion(
                _provider_accepted("recycle_clear", 3),
                tool="guangya.recycle.clear",
                context=ToolContext(owner="owner"),
                timeout_seconds=0,
            )
        self.assertEqual(status.call_count, 1)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "outcome_unknown")
        self.assertTrue(result.data["verification_pending"])
        self.assertTrue(result.data["background_job"]["timed_out"])
        self.assertIn("请勿重复提交", " ".join(result.suggestions))

    async def test_all_private_completion_trackers_wait_for_terminal_state(
        self,
    ) -> None:
        cases = (
            (
                "guangya_organize_task",
                {"task_id": "private-organize-task", "operation": "run"},
                "app.agent.domain_catalog.cloud_runtime.guangya_organize_task_status",
                (_snapshot("queued"), _snapshot("completed")),
                "completed",
            ),
            (
                "local_media_task",
                {"task_id": 91, "task_number": 4, "operation": "retry"},
                "app.agent.local_media_task_actions.local_media_completion_status",
                (_snapshot("running"), _snapshot("completed")),
                "completed",
            ),
            (
                "agent_job",
                {"job_id": "private-agent-job", "operation": "audit"},
                "app.agent.durable_job_actions.get_agent_job_status",
                (
                    ToolResult(True, "pending", "全库检查已排队"),
                    ToolResult(True, "up_to_date", "全库检查已完成"),
                ),
                "up_to_date",
            ),
            (
                "strm_run",
                {"after_run_id": 8, "trigger_type": "manual", "operation": "run"},
                "app.agent.effect_completion._strm_status",
                (_snapshot("running"), _snapshot("completed")),
                "completed",
            ),
            (
                "library_patrol",
                {"lease_generation": 3, "task_status": "pending", "operation": "run"},
                "app.agent.effect_completion._patrol_status",
                (
                    _snapshot("queued"),
                    ToolResult(True, "up_to_date", "全库巡检已完成"),
                ),
                "up_to_date",
            ),
        )
        for kind, completion, target, raw_snapshots, expected in cases:
            with self.subTest(kind=kind):
                snapshots = iter(raw_snapshots)
                progress = AsyncMock()
                with (
                    patch(
                        target,
                        side_effect=lambda *_args, _snapshots=snapshots, **_kwargs: (
                            next(_snapshots)
                        ),
                    ) as status,
                    patch(
                        "app.agent.effect_completion.asyncio.sleep", new=AsyncMock()
                    ) as sleep,
                ):
                    result = await wait_for_effect_completion(
                        _tracked_accepted(kind, **completion),
                        tool=f"test.{kind}",
                        context=ToolContext(owner="tg:v1:owner-safe"),
                        report_progress=progress,
                    )

                self.assertTrue(result.ok)
                self.assertEqual(result.status, expected)
                self.assertEqual(result.data["background_job"]["status"], expected)
                self.assertEqual(status.call_count, 2)
                self.assertEqual(sleep.await_count, 1)
                self.assertEqual(
                    [call.args[0]["phase"] for call in progress.await_args_list],
                    ["background_job", "background_job"],
                )
                self.assertNotIn("private-", str(result.to_dict()))

    async def test_stop_and_cancel_trackers_project_success_without_losing_raw_terminal(
        self,
    ) -> None:
        cases = (
            (
                _tracked_accepted(
                    "guangya_organize_task",
                    task_id="private-organize-task",
                    operation="stop",
                ),
                "app.agent.domain_catalog.cloud_runtime.guangya_organize_task_status",
                _snapshot("stopped"),
                "光鸭整理任务已停止",
                "stopped",
            ),
            (
                _tracked_accepted(
                    "agent_job", job_id="private-agent-job", operation="cancel"
                ),
                "app.agent.durable_job_actions.get_agent_job_status",
                ToolResult(True, "cancelled", "全库检查已取消"),
                "全库检查已取消",
                "cancelled",
            ),
        )
        for accepted, target, snapshot, summary, raw_status in cases:
            with self.subTest(target=target), patch(target, return_value=snapshot):
                result = await wait_for_effect_completion(
                    accepted,
                    tool="test.cancel",
                    context=ToolContext(owner="tg:v1:owner-safe"),
                )
            self.assertTrue(result.ok)
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.summary, summary)
            self.assertEqual(result.data["background_job"]["status"], raw_status)

    async def test_strm_tracker_binds_to_first_manual_run_after_frozen_baseline(
        self,
    ) -> None:
        rows = [
            {
                "id": 12,
                "trigger_type": "manual",
                "status": "running",
                "started_at": "2026-09-20 01:00:02",
                "finished_at": "",
                "result": "{}",
            },
            {
                "id": 11,
                "trigger_type": "manual",
                "status": "success",
                "started_at": "2026-09-20 01:00:00",
                "finished_at": "2026-09-20 01:00:01",
                "result": '{"stats":{"created":3}}',
            },
        ]
        scheduler = Mock()
        scheduler.status.return_value = {"running": True, "progress": {"percent": 5}}
        with (
            patch("app.database.list_task_runs", return_value=rows),
            patch("app.modules.scheduler.get_scheduler", return_value=scheduler),
        ):
            result = await wait_for_effect_completion(
                _tracked_accepted(
                    "strm_run",
                    after_run_id=10,
                    trigger_type="manual",
                    operation="run",
                ),
                tool="strm.run_once",
                context=ToolContext(owner="tg:v1:owner-safe"),
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "completed")
        self.assertEqual(
            result.data["background_job"]["started_at"], rows[1]["started_at"]
        )
        self.assertEqual(result.data["background_job"]["stats"], {"created": 3})

    def test_patrol_tracker_does_not_treat_intermediate_batch_as_terminal(self) -> None:
        baseline_finished = "2026-09-19 23:00:00"
        row = {
            "lease_generation": 4,
            "status": "pending",
            "last_started_at": "2026-09-20 01:00:00",
            "last_finished_at": baseline_finished,
            "checked_series_count": 50,
            "updates_available_count": 0,
            "missing_episode_count": 0,
        }
        tracker = _CompletionTracker(
            "library_patrol",
            {
                "lease_generation": 3,
                "task_status": "running",
                "last_finished_at": baseline_finished,
                "operation": "run",
            },
        )
        with (
            patch("app.database.get_agent_library_patrol", return_value=row),
            patch(
                "app.agent.library_patrol_status.get_library_patrol_status"
            ) as terminal_status,
        ):
            result = _patrol_status(tracker)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "queued")
        terminal_status.assert_not_called()

    async def test_completed_cloud_write_keeps_optional_sync_warning(self):
        snapshot = _snapshot("completed", stats={"renamed": 1, "strm_scope_unknown": 1})
        snapshot.suggestions.append("同步范围未能确认，本次未触发 STRM 联动。")
        with patch(
            "app.agent.domain_catalog.cloud_runtime.guangya_organize_status",
            return_value=snapshot,
        ):
            result = await wait_for_effect_completion(
                ToolResult(
                    True, "accepted", "已提交", data={"operation_ref": _OPERATION_REF}
                ),
                tool="guangya.fs.change.execute",
                context=ToolContext(owner="owner"),
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "completed")
        self.assertIn("未触发 STRM 联动", " ".join(result.suggestions))
        self.assertEqual(result.data["stats"]["renamed"], 1)

    async def test_terminal_states_are_returned_without_collapsing_their_status(
        self,
    ) -> None:
        for terminal in ("partial", "failed", "cancelled", "manual_review", "stopped"):
            with self.subTest(terminal=terminal):
                accepted = ToolResult(
                    True,
                    "accepted",
                    "已提交",
                    data={
                        "operation_ref": _OPERATION_REF,
                        "total": 1,
                        "cloud_write": False,
                    },
                )
                with (
                    patch(
                        "app.agent.domain_catalog.cloud_runtime.guangya_organize_status",
                        return_value=_snapshot(terminal),
                    ) as status,
                    patch(
                        "app.agent.effect_completion.asyncio.sleep",
                        new=AsyncMock(),
                    ) as sleep,
                ):
                    result = await wait_for_effect_completion(
                        accepted,
                        tool="guangya.rename.execute",
                        context=ToolContext(owner="webk:v1:owner-safe"),
                        report_progress=AsyncMock(),
                    )
                self.assertEqual(result.status, terminal)
                self.assertEqual(result.ok, terminal == "stopped")
                self.assertEqual(result.data["operation_ref"], _OPERATION_REF)
                self.assertEqual(result.data["total"], 1)
                self.assertNotIn("cloud_write", result.data)
                status.assert_called_once()
                sleep.assert_not_awaited()

    async def test_missing_job_is_unknown_and_non_gy_submission_is_unchanged(
        self,
    ) -> None:
        accepted = ToolResult(
            True,
            "accepted",
            "已提交",
            data={"operation_ref": _OPERATION_REF, "total": 1},
        )
        with patch(
            "app.agent.domain_catalog.cloud_runtime.guangya_organize_status",
            return_value=ToolResult(
                False,
                "empty",
                "没有找到这个光鸭操作编号",
                data={"operation_ref": _OPERATION_REF, "found": False},
            ),
        ):
            unknown = await wait_for_effect_completion(
                accepted,
                tool="guangya.fs.change.execute",
                context=ToolContext(owner="webk:v1:owner-safe"),
            )

        self.assertEqual(unknown.status, "outcome_unknown")
        self.assertEqual(unknown.data["background_job"]["status"], "unknown")
        self.assertEqual(unknown.data["operation_ref"], _OPERATION_REF)

        normal_write = ToolResult(
            True, "accepted", "已提交", data={"accepted": True, "total": 1}
        )
        with patch(
            "app.agent.domain_catalog.cloud_runtime.guangya_organize_status"
        ) as status:
            unchanged = await wait_for_effect_completion(
                normal_write,
                tool="ingest.submit",
                context=ToolContext(owner="webk:v1:owner-safe"),
            )
        self.assertIs(unchanged, normal_write)
        status.assert_not_called()

    async def test_timeout_keeps_last_running_fact_as_unknown(self) -> None:
        accepted = ToolResult(
            True,
            "accepted",
            "已提交",
            data={
                "operation_ref": _OPERATION_REF,
                "total": 1,
                "cloud_write": False,
            },
        )
        with patch(
            "app.agent.domain_catalog.cloud_runtime.guangya_organize_status",
            return_value=_snapshot("running"),
        ):
            result = await wait_for_effect_completion(
                accepted,
                tool="guangya.fs.change.execute",
                context=ToolContext(owner="webk:v1:owner-safe"),
                report_progress=AsyncMock(),
                timeout_seconds=0,
            )

        self.assertEqual(result.status, "outcome_unknown")
        self.assertFalse(result.ok)
        self.assertEqual(result.data["operation_ref"], _OPERATION_REF)
        self.assertEqual(result.data["background_job"]["last_status"], "running")
        self.assertTrue(result.data["background_job"]["timed_out"])
        self.assertNotIn("cloud_write", result.data)
        self.assertNotIn("failed", result.summary)
        self.assertNotIn("completed", result.summary)

    async def test_sync_post_write_verifier_runs_off_event_loop(self) -> None:
        verifier_threads: list[int] = []
        main_thread = threading.get_ident()
        accepted = ToolResult(
            True,
            "accepted",
            "已提交",
            data={"operation_ref": _OPERATION_REF},
        )

        def verifier(_arguments, value):
            verifier_threads.append(threading.get_ident())
            return value

        spec = ToolSpec(
            name="guangya.fs.change.execute",
            description="测试同步写后验证",
            risk=RiskLevel.DANGER,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            validator=lambda value: dict(value),
            requires_confirmation=True,
            context_confirmation_preparer=lambda _args, _ctx: (
                ToolResult(True, "ready", "预检"),
                "fingerprint",
            ),
            context_confirmed_handler=lambda _args, _snapshot, _ctx: accepted,
            post_write_verifier=verifier,
        )
        tool = adapt_tool_spec(spec)
        state = InMemorySessionStateStore()
        lease, _ = await state.begin_turn(
            owner="webk:v1:owner-safe", session_id="session", request_id="request"
        )
        from app.agent.kernel.pipeline import ToolCallContext

        context = ToolCallContext(
            owner="webk:v1:owner-safe",
            session_id="session",
            request_id="request",
            turn_id=lease.turn_id,
            lease=lease,
            cancellation=CancellationToken(),
            report_progress=AsyncMock(),
            wait_for_completion=True,
        )
        with patch(
            "app.agent.kernel.ports.existing_actions.wait_for_effect_completion",
            new=AsyncMock(side_effect=lambda value, **_kwargs: value),
        ) as wait:
            result = await tool.verify({}, accepted, context)

        self.assertIs(result, accepted)
        self.assertEqual(len(verifier_threads), 1)
        self.assertNotEqual(verifier_threads[0], main_thread)
        wait.assert_awaited_once()

    async def test_cancel_drains_sync_verifier_before_verify_task_finishes(
        self,
    ) -> None:
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        accepted = ToolResult(True, "success", "已核验", data={"total": 1})

        def verifier(_arguments, value):
            entered.set()
            release.wait(timeout=5)
            finished.set()
            return value

        spec = ToolSpec(
            name="config.cancel_drain",
            description="测试取消时收稳同步核验",
            risk=RiskLevel.WRITE,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            validator=lambda value: dict(value),
            requires_confirmation=True,
            context_confirmation_preparer=lambda _args, _ctx: (
                ToolResult(True, "ready", "预检"),
                "fingerprint",
            ),
            context_confirmed_handler=lambda _args, _snapshot, _ctx: accepted,
            post_write_verifier=verifier,
        )
        tool = adapt_tool_spec(spec)
        state = InMemorySessionStateStore()
        lease, _ = await state.begin_turn(
            owner="webk:v1:owner-safe", session_id="session", request_id="request"
        )
        from app.agent.kernel.pipeline import ToolCallContext

        context = ToolCallContext(
            owner="webk:v1:owner-safe",
            session_id="session",
            request_id="request",
            turn_id=lease.turn_id,
            lease=lease,
            cancellation=CancellationToken(),
            report_progress=AsyncMock(),
        )
        verify_done: list[bool] = []

        async def run_verify():
            await tool.verify({}, accepted, context)
            verify_done.append(True)

        task = asyncio.create_task(run_verify())
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertFalse(finished.is_set())
        self.assertFalse(verify_done)

        release.set()
        await task
        self.assertTrue(finished.is_set())
        self.assertEqual(verify_done, [True])

    async def test_streaming_kernel_verify_uses_one_generic_completion_wait(
        self,
    ) -> None:
        accepted = ToolResult(
            True,
            "accepted",
            "已提交",
            data={"operation_ref": _OPERATION_REF},
        )
        spec = ToolSpec(
            name="guangya.fs.change.execute",
            description="测试 GY 后台写入",
            risk=RiskLevel.DANGER,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            validator=lambda value: dict(value),
            requires_confirmation=True,
            context_confirmation_preparer=lambda _args, _ctx: (
                ToolResult(True, "ready", "预检"),
                "fingerprint",
            ),
            context_confirmed_handler=lambda _args, _snapshot, _ctx: accepted,
        )
        tool = adapt_tool_spec(spec)
        state = InMemorySessionStateStore()
        lease, _ = await state.begin_turn(
            owner="webk:v1:owner-safe", session_id="session", request_id="request"
        )
        from app.agent.kernel.pipeline import ToolCallContext

        context = ToolCallContext(
            owner="webk:v1:owner-safe",
            session_id="session",
            request_id="request",
            turn_id=lease.turn_id,
            lease=lease,
            cancellation=CancellationToken(),
            report_progress=AsyncMock(),
            wait_for_completion=True,
        )
        terminal = ToolResult(
            True, "completed", "已完成", data={"operation_ref": _OPERATION_REF}
        )
        with patch(
            "app.agent.kernel.ports.existing_actions.wait_for_effect_completion",
            new=AsyncMock(return_value=terminal),
        ) as wait:
            result = await tool.verify({}, accepted, context)

        self.assertIs(result, terminal)
        wait.assert_awaited_once()
        self.assertEqual(wait.await_args.kwargs["tool"], "guangya.fs.change.execute")

    async def test_non_streaming_kernel_verify_returns_submission_without_waiting(
        self,
    ) -> None:
        accepted = ToolResult(
            True,
            "accepted",
            "已提交",
            data={"operation_ref": _OPERATION_REF},
        )
        spec = ToolSpec(
            name="guangya.fs.change.execute",
            description="测试非流式调用不占用请求等待后台终态",
            risk=RiskLevel.DANGER,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            validator=lambda value: dict(value),
            requires_confirmation=True,
            context_confirmation_preparer=lambda _args, _ctx: (
                ToolResult(True, "ready", "预检"),
                "fingerprint",
            ),
            context_confirmed_handler=lambda _args, _snapshot, _ctx: accepted,
        )
        tool = adapt_tool_spec(spec)
        state = InMemorySessionStateStore()
        lease, _ = await state.begin_turn(
            owner="service-owner", session_id="session", request_id="request"
        )
        from app.agent.kernel.pipeline import ToolCallContext

        context = ToolCallContext(
            owner="service-owner",
            session_id="session",
            request_id="request",
            turn_id=lease.turn_id,
            lease=lease,
            cancellation=CancellationToken(),
            report_progress=AsyncMock(),
        )
        with patch(
            "app.agent.kernel.ports.existing_actions.wait_for_effect_completion",
            new=AsyncMock(),
        ) as wait:
            result = await tool.verify({}, accepted, context)

        self.assertIs(result, accepted)
        wait.assert_not_awaited()




class GuangYaOperationResultMeaningTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_verification_failure_reaches_terminal_receipt_without_private_error(self):
        from app.agent.domain_catalog.cloud_runtime import _project_guangya_status
        from app.agent.public_view import format_public_result
        snapshot = _project_guangya_status(
            {'status': 'partial', 'stats': {'total': 2, 'failed': 2, 'verification_failed': 2},
             'error': 'private /folder token=do-not-expose'},
            overview={}, operation_ref=_OPERATION_REF,
        )
        accepted = ToolResult(True, 'accepted', '清理已提交', data={'operation_ref': _OPERATION_REF})
        with patch('app.agent.domain_catalog.cloud_runtime.guangya_organize_status', return_value=snapshot):
            result = await wait_for_effect_completion(accepted, tool='guangya.organize.cleanup.execute', context=ToolContext(owner='owner'))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 'partial')
        self.assertIn('2 项写后状态未核验通过', result.error)
        self.assertIn('请勿直接重复提交', format_public_result(result.to_dict()))
        self.assertNotIn('do-not-expose', str(result.to_dict()))
        self.assertNotIn('private', str(result.to_dict()))

    async def test_filesystem_success_receipt_does_not_claim_media_recognition_or_archival(self):
        from app.agent.public_view import format_public_result
        accepted = ToolResult(True, 'accepted', '文件变更已提交',
                              data={'operation_ref': _OPERATION_REF, 'operation': 'filesystem_change'},
                              model_data={'operation': 'filesystem_change'})
        snapshot = _snapshot('completed', stats={'renamed': 6, 'moved': 0})
        with patch('app.agent.domain_catalog.cloud_runtime.guangya_organize_status', return_value=snapshot):
            result = await wait_for_effect_completion(accepted, tool='guangya.fs.change.execute', context=ToolContext(owner='owner'))
        self.assertTrue(result.ok)
        self.assertEqual(result.data['scope_note'], result.model_data['scope_note'])
        text = format_public_result(result.to_dict())
        self.assertIn('改名 6 项', text)
        self.assertIn('不代表已完成元数据识别', text)
        self.assertNotIn('移动 6 项', text)

    def test_preflight_and_audit_failure_counts_have_distinct_explanations(self):
        from app.agent.domain_catalog.cloud_runtime import _project_guangya_status
        result = _project_guangya_status({'status': 'manual_review', 'stats': {'precondition_failed': 1, 'audit_failures': 2}}, overview={})
        self.assertIn('1 项写前核对未通过，后续动作未执行', result.error)
        self.assertIn('2 项执行审计未完整保存', result.error)
        success = _project_guangya_status({'status': 'completed', 'stats': {}}, overview={})
        self.assertEqual(success.error, '')

    def test_stale_recovered_plan_does_not_deny_prior_writes(self):
        from app.agent.domain_catalog.cloud_runtime import _project_guangya_status
        result = _project_guangya_status({"status": "failed", "error_code": "GuangYaFSChangeStale", "result": {
            "stats": {"total": 2}, "operation_items": [
                {"position": 1, "operation": "rename", "status": "unknown", "completed_actions": []},
                {"position": 2, "operation": "rename", "status": "not_started", "completed_actions": []},
            ]}}, overview={})
        self.assertIn("已发生的变更以逐项回执为准", result.error)
        self.assertNotIn("本次变更未执行", result.error)

    def test_task_projection_passes_sanitized_operation_items_and_keeps_legacy_stats(self):
        from app.agent.domain_catalog.cloud_runtime import _project_guangya_status

        projected = _project_guangya_status(
            {
                "status": "partial",
                "result": {
                    "stats": {"total": 2, "relocated": 1.8, "private_counter": 99},
                    "operation_items": [
                        {
                            "position": 1,
                            "operation": "relocate",
                            "status": "completed",
                            "label": "第 1 集 password=must-not-leak",
                            "completed_actions": ["move", "rename"],
                            "file_id": "private-file-id",
                            "path": "/private/library/episode.mkv",
                        },
                        {"position": 2, "operation": "unsafe", "status": "completed"},
                        {"position": 3, "op": "move", "status": "completed"},
                    ],
                },
            },
            overview={},
        )
        task = projected.data["task"]
        self.assertEqual(task["stats"], {"total": 2, "relocated": 1})
        self.assertEqual(
            task["operation_items"],
            [
                {
                    "position": 1,
                    "operation": "relocate",
                    "status": "completed",
                    "completed_actions": ["move", "rename"],
                }
            ],
        )
        self.assertNotIn("private-file-id", str(projected.to_dict()))
        self.assertNotIn("must-not-leak", str(projected.to_dict()))

        legacy = _project_guangya_status(
            {"status": "completed", "result": {"counters": {"moved": "3", "renamed": True, "failed": -1}}},
            overview={},
        )
        self.assertEqual(legacy.data["task"]["stats"], {"moved": 3, "renamed": 1, "failed": 0})
        self.assertNotIn("operation_items", legacy.data["task"])


class LocalMediaScanCompletionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        database = isolated_test_database("local-scan-completion.db")
        database.__enter__()
        self.addCleanup(database.__exit__, None, None, None)
        self.source_id = db.create_local_media_source(
            name="扫描测试来源", qb_profile="", qb_path_prefix="",
            local_root="/private/scan", owner="admin",
        )
        self.sequence = 0
        self.context = ToolContext(owner="webk:v1:scan-test")

    def _task(self, status="completed", action="move", *, warning=""):
        self.sequence += 1
        path = f"/private/scan/{self.sequence}.mkv"
        task_id = db.create_local_media_task(
            self.source_id, "", path, owner="admin", trigger="scan"
        )
        db.update_local_media_task(
            task_id, status=status, warning=warning, error="private-error-token"
        )
        if action is not None:
            db.add_local_media_task_item(
                task_id, path, f"/private/library/{self.sequence}.mkv",
                role="video", action=action,
            )
        return task_id

    def _accepted(self, task_ids):
        scan_ref = record_local_media_scan({
            "task_ids": task_ids, "queued_count": len(task_ids),
            "candidate_count": len(task_ids), "scanned_sources": 1,
        })
        result = _tracked_accepted("local_media_scan", scan_ref=scan_ref)
        result.data.update(scan_ref=scan_ref, queued_tasks=len(task_ids))
        return result

    async def _wait(self, accepted, **kwargs):
        return await wait_for_effect_completion(
            accepted, tool="local_media.scan_sources", context=self.context, **kwargs
        )

    async def test_real_batch_over_twenty_uses_frozen_members_and_live_file_facts(self):
        members = [self._task() for _ in range(24)]
        members.append(self._task("planned", "skip"))
        accepted = self._accepted(members)
        scan_ref = accepted.data["scan_ref"]
        # 已发送通知的旧快照不是本轮等待的文件事实。
        finish_local_media_scan_report(scan_ref, {"task_outcomes": [
            {"task_id": members[0], "status": "completed", "archived_video_count": 999}
        ]})
        unrelated = self._task("failed")
        self._accepted([unrelated])  # 最近扫描属于另一批，绝不能拿来替代。
        progress = AsyncMock()
        statements = []
        get_conn = db.get_conn

        @contextmanager
        def traced_conn():
            with get_conn() as conn:
                conn.set_trace_callback(statements.append)
                yield conn

        async def worker_finishes(_seconds):
            db.update_local_media_task(members[-1], status="completed")
            # 即使回执被后续改写，等待器也只追踪第一次解析冻结的成员。
            with db.get_conn() as conn:
                row = conn.execute("SELECT result FROM task_runs WHERE id=?", (int(scan_ref[2:]),)).fetchone()
                payload = json.loads(row["result"])
                payload["task_ids"].append(unrelated)
                conn.execute("UPDATE task_runs SET result=? WHERE id=?", (json.dumps(payload), int(scan_ref[2:])))

        with (
            patch("app.database.get_conn", side_effect=traced_conn),
            patch("app.modules.local_media_scan_runs.resolve_local_media_scan", wraps=resolve_local_media_scan) as resolve,
            patch("app.agent.effect_completion.asyncio.sleep", new=AsyncMock(side_effect=worker_finishes)) as sleep,
        ):
            result = await self._wait(accepted, report_progress=progress)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "completed")
        stats = result.data["stats"]
        self.assertEqual((stats["total"], stats["completed"]), (25, 25))
        self.assertEqual(stats["archived_video_count"], 24)
        self.assertEqual(stats["skipped_video_count"], 1)
        self.assertEqual(stats["unknown"], 0)
        self.assertIn("归档 24 个视频", result.summary)
        self.assertIn("冲突跳过 1 个视频", result.summary)
        self.assertNotIn("private-", str(result.to_dict()))
        self.assertNotIn("/private/", str(result.to_model_dict()))
        self.assertNotIn("task_ids", str(result.to_model_dict()))
        self.assertEqual([call.args[0]["status"] for call in progress.await_args_list], ["running", "completed"])
        resolve.assert_called_once_with(scan_ref, owner="admin")
        sleep.assert_awaited_once()
        for table in ("local_media_tasks", "local_media_task_items"):
            queries = [sql for sql in statements if sql.startswith(f"SELECT * FROM {table} WHERE owner=")]
            self.assertEqual(len(queries), 2, "每轮应批量读一次，不按任务逐个查")

    async def test_manual_and_failed_members_are_unsuccessful_terminal_after_busy_members_finish(self):
        for status in ("requires_manual", "failed"):
            with self.subTest(status=status):
                member = self._task(status)
                terminal = await self._wait(self._accepted([member]))
                self.assertFalse(terminal.ok)
                self.assertEqual(terminal.status, status)
                skipped = await self._wait(self._accepted([member, self._task(action="skip")]))
                self.assertEqual(skipped.status, status)
                self.assertEqual(skipped.data["stats"]["archived_video_count"], 0)
                busy = self._task("moving")
                accepted = self._accepted([member, self._task(), busy])

                async def finish(_seconds):
                    db.update_local_media_task(busy, status="completed")

                with patch("app.agent.effect_completion.asyncio.sleep", new=AsyncMock(side_effect=finish)) as sleep:
                    result = await self._wait(accepted)
                self.assertFalse(result.ok)
                self.assertEqual(result.status, "partial")
                self.assertEqual(result.data["stats"][status], 1)
                self.assertEqual(result.data["stats"]["completed"], 2)
                self.assertEqual(result.data["stats"]["archived_video_count"], 2)
                self.assertIn("部分完成", result.summary)
                self.assertFalse(result.data["background_job"]["timed_out"])
                self.assertTrue(result.error)
                sleep.assert_awaited_once()

    async def test_every_busy_status_waits_and_timeout_never_claims_completion(self):
        for status in sorted(LOCAL_BUSY_TASK_STATUSES):
            with self.subTest(status=status):
                accepted = self._accepted([self._task(status)])
                accepted.model_data = {"accepted": True}
                result = await self._wait(accepted, timeout_seconds=0)
                self.assertFalse(result.ok)
                self.assertEqual(result.status, "outcome_unknown")
                self.assertEqual(result.data["background_job"]["last_status"], "running")
                self.assertTrue(result.data["background_job"]["timed_out"])
                self.assertEqual(result.data["stats"]["running"], 1)
                self.assertEqual(result.data["stats"]["archived_video_count"], 0)
                self.assertEqual(result.to_model_dict()["data"]["stats"], result.data["stats"])

    async def test_missing_member_and_missing_file_evidence_cannot_be_completed(self):
        for kind in ("missing_member", "no_items", "preview_only"):
            with self.subTest(kind=kind):
                task_id = self._task(
                    action=None if kind == "no_items" else "move",
                    warning="仅预览模式：未移动文件" if kind == "preview_only" else "",
                )
                members = [task_id, task_id + 100000] if kind == "missing_member" else [task_id]
                result = await self._wait(self._accepted(members))
                self.assertFalse(result.ok)
                self.assertEqual(result.status, "outcome_unknown")
                self.assertEqual(result.data["stats"]["unknown"], 1)
                self.assertEqual(result.data["stats"]["missing_tasks"], int(kind == "missing_member"))

    async def test_all_conflicts_report_skips_not_archival(self):
        result = await self._wait(self._accepted([self._task(action="skip") for _ in range(3)]))
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.data["stats"]["archived_video_count"], 0)
        self.assertEqual(result.data["stats"]["skipped_video_count"], 3)
        self.assertIn("归档 0 个视频", result.summary)
        self.assertIn("冲突跳过 3 个视频", result.summary)

    async def test_unresolved_or_empty_receipts_never_fall_back_to_latest_scan(self):
        self._accepted([self._task()])
        empty_ref = self._accepted([]).data["scan_ref"]
        foreign_ref = record_local_media_scan({"task_ids": []}, owner="other")
        for scan_ref in ("", "LM-UNRECORDED", "invalid", "LM99999999", empty_ref, foreign_ref):
            with self.subTest(scan_ref=scan_ref):
                accepted = _tracked_accepted("local_media_scan", scan_ref=scan_ref)
                with patch("app.modules.local_media_scan_runs.resolve_local_media_scan", wraps=resolve_local_media_scan) as resolve:
                    result = await self._wait(accepted)
                self.assertFalse(result.ok)
                self.assertEqual(result.status, "outcome_unknown")
                if not scan_ref:
                    resolve.assert_not_called()
                else:
                    resolve.assert_called_once_with(scan_ref, owner="admin")

    async def test_scan_uses_existing_kernel_completion_wait(self):
        from app.agent.kernel.pipeline import ToolCallContext

        accepted = self._accepted([self._task()])
        tool = adapt_tool_spec(ToolSpec(
            name="local_media.scan_sources", description="本地扫描", risk=RiskLevel.WRITE,
            parameters={"type": "object", "properties": {}}, validator=lambda value: value,
            requires_confirmation=True,
            context_confirmation_preparer=lambda _args, _ctx: (ToolResult(True, "ready", "预检"), "snapshot"),
            context_confirmed_handler=lambda _args, _snapshot, _ctx: accepted,
        ))
        state = InMemorySessionStateStore()
        lease, _ = await state.begin_turn(owner="scan-owner", session_id="scan-session", request_id="scan-request")
        context = ToolCallContext(
            owner=lease.owner, session_id=lease.session_id, request_id=lease.request_id,
            turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(),
            report_progress=AsyncMock(), wait_for_completion=True,
        )
        result = await tool.verify({}, accepted, context)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.data["stats"]["archived_video_count"], 1)


class PersistentEffectReceiptTests(unittest.IsolatedAsyncioTestCase):
    """真实 SQLite/确认会话状态；仅 Provider 状态读取使用固定夹具。"""

    async def asyncSetUp(self):
        from app.agent.kernel.persistence import SQLiteKernelStore
        self.database = isolated_test_database()
        self.database.__enter__()
        self.store = SQLiteKernelStore()
        self.scope_args = {"owner": "tg:v1:123\x1f456", "session_id": "effect-session"}
        self.plan_id = "plan-effect-receipt"
        self.lease, _ = await self.store.begin_turn(**self.scope_args, request_id="confirm")

    async def asyncTearDown(self):
        self.database.__exit__(None, None, None)

    async def pending(self, channel="web"):
        from app.agent.effect_completion import EffectCompletionScope
        with patch("app.agent.effect_completion._poll", new=AsyncMock(
            return_value=(_snapshot("running"), "running", {"status": "running"}),
        )):
            result = await wait_for_effect_completion(
                _provider_accepted("empty_recycle_bin", 2), tool="recycle.empty",
                context=ToolContext(**self.scope_args), timeout_seconds=0,
                scope=EffectCompletionScope(self.store, self.lease, self.plan_id, channel),
            )
        self.assertEqual(result.status, "running")
        self.assertTrue(result.data["background_job"]["followup_pending"])
        await self.make_due()
        return result

    async def make_due(self):
        def change(state):
            for row in state.metadata.get("effect_waits", {}).values():
                row["next_poll_at"] = 0
            state.metadata["effect_next_poll_at"] = 0
        await self.store.update_effect_state(**self.scope_args, change=change)

    async def finish(self, *, store=None):
        from app.agent.effect_completion import poll_effect_receipts
        with patch("app.agent.effect_completion._poll", new=AsyncMock(
            return_value=(ToolResult(True, "completed", "云盘任务已完成"), "completed", {"status": "completed"}),
        )) as reader:
            count = await poll_effect_receipts(store or self.store)
        return count, reader

    def create_agent_job(self):
        from app.agent.library_patrol_progress import empty_patrol_projection

        row, created = db.create_agent_job(
            owner=self.scope_args["owner"], job_type="library_episode_audit",
            dedupe_key="2026-10-08:1", input_json='{"as_of":"2026-10-08","max_series":1}',
            checkpoint_json="{}",
            projection_json=json.dumps(empty_patrol_projection(as_of="2026-10-08")),
        )
        self.assertTrue(created)
        return row

    async def handoff_agent_job(self, job, *, operation="audit"):
        from app.agent.effect_completion import EffectCompletionScope

        with patch("app.agent.effect_completion.asyncio.sleep", new=AsyncMock(
            side_effect=AssertionError("持久任务应交回前台，不等待后台终态"),
        )):
            return await wait_for_effect_completion(
                _tracked_accepted("agent_job", job_id=job["job_id"], operation=operation),
                tool="agent.cancel_job" if operation == "cancel" else "library.start_episode_audit",
                context=ToolContext(**self.scope_args), timeout_seconds=60,
                scope=EffectCompletionScope(self.store, self.lease, self.plan_id),
            )

    async def test_durable_agent_job_handoff_survives_new_turn_and_reopened_store(self):
        from app.agent.effect_completion import poll_effect_receipts
        from app.agent.kernel.persistence import SQLiteKernelStore

        job = self.create_agent_job()
        result = await self.handoff_agent_job(job)
        self.assertEqual(result.status, "running")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["background_job"]["status"], "pending")
        self.assertTrue(result.data["background_job"]["followup_pending"])
        self.assertFalse(result.data["background_job"]["timed_out"])
        state = await self.store.load(**self.scope_args)
        self.assertEqual(state.metadata["effect_waits"][self.plan_id]["last_status"], "pending")
        next_lease, _ = await self.store.begin_turn(**self.scope_args, request_id="next-question")
        await self.store.commit(next_lease, conversation=[{"role": "user", "content": "后续问题"}])
        _, outcome = db.cancel_agent_job(owner=self.scope_args["owner"], job_id=job["job_id"])
        self.assertEqual(outcome, "cancelled")
        await self.make_due()
        reopened = SQLiteKernelStore()
        self.assertEqual(await poll_effect_receipts(reopened), 1)
        self.assertEqual(await poll_effect_receipts(reopened), 0)
        state = await reopened.load(**self.scope_args)
        self.assertEqual(state.generation, next_lease.generation)
        self.assertEqual(state.conversation[0]["content"], "后续问题")
        receipts = [m for m in state.conversation if m.get("completion_receipt_id") == self.plan_id]
        self.assertEqual(len(receipts), 1)
        self.assertIn("取消", receipts[0]["public_content"])

    async def test_running_job_cancel_request_hands_off_without_claiming_cancelled(self):
        from app.agent.effect_completion import poll_effect_receipts

        job = self.create_agent_job()
        claimed = db.claim_due_agent_job(job_type="library_episode_audit")
        self.assertIsNotNone(claimed)
        _, outcome = db.cancel_agent_job(owner=self.scope_args["owner"], job_id=job["job_id"])
        self.assertEqual(outcome, "requested")
        result = await self.handoff_agent_job(job, operation="cancel")
        self.assertEqual(result.status, "running")
        self.assertEqual(result.data["background_job"]["status"], "running")
        await self.make_due()
        self.assertEqual(await poll_effect_receipts(self.store), 0)
        self.assertTrue(db.finalize_cancelled_agent_job(
            job["job_id"], expected_lease_generation=claimed["lease_generation"],
        ))
        await self.make_due()
        self.assertEqual(await poll_effect_receipts(self.store), 1)
        self.assertEqual(await poll_effect_receipts(self.store), 0)

    async def test_already_terminal_agent_job_returns_final_receipt_without_handoff(self):
        job = self.create_agent_job()
        _, outcome = db.cancel_agent_job(owner=self.scope_args["owner"], job_id=job["job_id"])
        self.assertEqual(outcome, "cancelled")
        result = await self.handoff_agent_job(job, operation="cancel")
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.data["background_job"]["status"], "cancelled")
        self.assertNotIn("followup_pending", result.data["background_job"])
        state = await self.store.load(**self.scope_args)
        self.assertTrue(state.metadata["effect_waits"][self.plan_id]["foreground_final"])

    async def test_agent_job_handoff_requires_persisted_tracking(self):
        job = self.create_agent_job()
        with patch.object(self.store, "update_effect_state", side_effect=OSError("storage unavailable")), patch(
            "app.agent.effect_completion._poll", new=AsyncMock(),
        ) as poll:
            with self.assertRaises(OSError):
                await self.handoff_agent_job(job)
        poll.assert_not_awaited()
        self.assertEqual(db.get_agent_job(owner=self.scope_args["owner"], job_id=job["job_id"])["status"], "pending")
        self.assertEqual((await self.store.load(**self.scope_args)).metadata.get("effect_waits", {}), {})
        # 提交后跟踪登记失败不能靠重放制造第二份任务；重新确认复用原队列行。
        from app.agent.durable_job_actions import (
            prepare_start_episode_audit, start_episode_audit_confirmed,
        )
        arguments = {"as_of": "2026-10-08", "max_series": 1}
        context = ToolContext(**self.scope_args)
        _, fingerprint = prepare_start_episode_audit(arguments, context)
        with patch("app.agent.durable_job_actions.get_agent_jobs_scheduler"):
            recovered = start_episode_audit_confirmed(arguments, fingerprint, context)
        self.assertFalse(recovered.data["created"])
        self.assertEqual(recovered.data["job_id"], job["job_id"])
        self.assertEqual(len(db.list_agent_jobs(owner=self.scope_args["owner"])), 1)

    async def test_many_already_accepted_tasks_are_not_rejected_after_the_write(self):
        from app.agent.effect_completion import (
            EffectCompletionScope, _CompletionTracker, _register_effect_wait,
        )
        for index in range(17):
            await _register_effect_wait(
                _provider_accepted("empty_recycle_bin", 1),
                _CompletionTracker("guangya_task", {"task_id": f"private-{index}"}),
                "recycle.empty", EffectCompletionScope(self.store, self.lease, f"plan-{index}"),
            )
        state = await self.store.load(**self.scope_args)
        self.assertEqual(len(state.metadata["effect_waits"]), 17)
        await self.make_due()
        self.assertEqual(len(await self.store.due_effect_waits(limit=16)), 16)

    async def test_restart_completes_once_and_preserves_new_turn_and_plan(self):
        from app.agent.kernel.persistence import SQLiteKernelStore
        from app.agent.kernel.state import StateUpdate
        await self.pending()
        second, _ = await self.store.begin_turn(**self.scope_args, request_id="new-question")
        await self.store.commit(second, conversation=[{"role": "user", "content": "另一件事"}],
            updates=(StateUpdate("pending_effect_plan_id", "new-plan"),))
        count, reader = await self.finish(store=SQLiteKernelStore())
        self.assertEqual(count, 1)
        reader.assert_awaited_once()
        state = await self.store.load(**self.scope_args)
        self.assertEqual(state.generation, second.generation)
        self.assertEqual(state.pending_effect_plan_id, "new-plan")
        self.assertEqual(state.conversation[0]["content"], "另一件事")
        receipt = state.conversation[-1]
        self.assertEqual(receipt["completion_receipt_id"], self.plan_id)
        self.assertNotIn("private-task", json.dumps(receipt))
        self.assertNotIn("可信系统结果", receipt["public_content"])
        self.assertEqual((await self.finish())[0], 0)
        await self.store.commit(second, conversation=[{"role": "user", "content": "另一件事"}])
        self.assertEqual((await self.store.load(**self.scope_args)).conversation[-1], receipt)

    async def test_late_receipt_follows_new_user_even_without_model_continuation(self):
        from app.agent.public_view import public_conversation_messages
        await self.pending()
        await self.store.commit(self.lease, conversation=[
            {"role": "assistant", "content": "accepted", "public_content": "等待完成",
             "tool_name": "recycle.empty", "effect_plan_id": self.plan_id,
             "completion_receipt_id": self.plan_id},
            {"role": "user", "content": "还有别的事"},
        ])
        self.assertEqual((await self.finish())[0], 1)
        public = public_conversation_messages((await self.store.load(**self.scope_args)).conversation)
        self.assertEqual(public[-2]["content"], "还有别的事")
        self.assertIn("完成", public[-1]["content"])

    async def test_late_receipt_is_visible_after_foreground_model_answer(self):
        from app.agent.public_view import public_conversation_messages
        await self.pending()
        await self.store.commit(self.lease, conversation=[
            {"role": "assistant", "content": "accepted", "public_content": "等待完成",
             "tool_name": "recycle.empty", "effect_plan_id": self.plan_id,
             "completion_receipt_id": self.plan_id},
            {"role": "assistant", "content": "已受理，请等待", "effect_plan_id": self.plan_id},
            {"role": "user", "content": "还有别的事"},
        ])
        self.assertEqual((await self.finish())[0], 1)
        state = await self.store.load(**self.scope_args)
        public = public_conversation_messages(state.conversation)
        self.assertIn("完成", public[-1]["content"])
        self.assertEqual(public[-2]["content"], "还有别的事")
        self.assertNotIn("等待完成", json.dumps(public, ensure_ascii=False))

    async def test_receipt_fallback_without_public_text_never_exposes_internal_json(self):
        from app.agent.public_view import public_conversation_messages
        from app.agent.kernel.model import ModelMessage
        await self.pending()
        await self.finish()
        state = await self.store.load(**self.scope_args)
        restored = ModelMessage.from_dict(state.conversation[-1]).to_dict()
        self.assertEqual(restored["completion_receipt_id"], self.plan_id)
        public = public_conversation_messages([restored])
        self.assertIn("完成", public[-1]["content"])
        self.assertNotIn("可信系统结果", public[-1]["content"])
        self.assertNotIn('"evidence"', public[-1]["content"])

    async def test_foreground_terminal_saved_before_ack_does_not_duplicate(self):
        from app.agent.effect_completion import (
            EffectCompletionScope, _record_effect_observation, remember_effect_receipt,
        )
        await self.pending()
        scope = EffectCompletionScope(self.store, self.lease, self.plan_id)
        final = ToolResult(True, "completed", "完成")
        await _record_effect_observation(scope, status="completed", final=final)
        message = {"role": "assistant", "content": "已完成", "completion_receipt_id": self.plan_id}
        await remember_effect_receipt(self.store, **self.scope_args, plan_id=self.plan_id, message=message)
        await self.store.commit(self.lease, conversation=[message])
        await self.make_due()
        count, reader = await self.finish()
        self.assertEqual(count, 0)
        reader.assert_not_awaited()
        state = await self.store.load(**self.scope_args)
        self.assertEqual(state.conversation, [message])
        self.assertNotIn(self.plan_id, state.metadata.get("effect_waits", {}))

    async def test_reset_during_read_cannot_resurrect_receipt_or_send(self):
        from app.agent.effect_completion import poll_effect_receipts
        await self.pending(channel="telegram")
        async def reset(*args):
            await self.store.reset_session(**self.scope_args)
            return ToolResult(True, "completed", "完成"), "completed", {}
        with patch("app.agent.effect_completion._poll", side_effect=reset), patch(
            "app.agent.effect_completion._enqueue_late_receipt",
        ) as send:
            self.assertEqual(await poll_effect_receipts(self.store), 0)
        send.assert_not_called()
        state = await self.store.load(**self.scope_args)
        self.assertEqual(state.conversation, [])
        self.assertNotIn("effect_waits", state.metadata)

    async def test_transient_read_failure_remains_pending_without_fabricated_success(self):
        from app.agent.effect_completion import poll_effect_receipts
        await self.pending()
        with patch("app.agent.effect_completion._poll", side_effect=OSError("offline")):
            self.assertEqual(await poll_effect_receipts(self.store), 0)
        state = await self.store.load(**self.scope_args)
        self.assertEqual(state.conversation, [])
        self.assertEqual(state.metadata["effect_waits"][self.plan_id]["read_errors"], 1)
        await self.make_due()
        self.assertEqual((await self.finish())[0], 1)

    async def test_missing_reference_ends_tracking_without_replaying_write(self):
        from app.agent.effect_completion import poll_effect_receipts
        await self.pending()
        with patch("app.agent.effect_completion._poll", side_effect=LookupError("removed")) as read:
            self.assertEqual(await poll_effect_receipts(self.store), 1)
        read.assert_awaited_once()
        state = await self.store.load(**self.scope_args)
        self.assertIn("无法核验", state.conversation[-1]["public_content"])
        self.assertTrue(state.metadata["effect_waits"][self.plan_id]["delivered"])

    async def test_two_sweepers_claim_only_one_read(self):
        from app.agent.effect_completion import poll_effect_receipts
        await self.pending()
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args):
            entered.set()
            await release.wait()
            return ToolResult(True, "completed", "完成"), "completed", {}
        with patch("app.agent.effect_completion._poll", side_effect=delayed) as read:
            task = asyncio.create_task(poll_effect_receipts(self.store))
            await entered.wait()
            self.assertEqual(await poll_effect_receipts(self.store), 0)
            release.set()
            self.assertEqual(await task, 1)
        read.assert_awaited_once()

    async def test_expired_tracking_does_not_discard_failed_telegram_handoff(self):
        from app.agent.effect_completion import poll_effect_receipts
        await self.pending(channel="telegram")
        with patch("app.agent.effect_completion._poll", side_effect=LookupError("removed")), patch(
            "app.agent.effect_completion._enqueue_late_receipt", return_value=False,
        ):
            self.assertEqual(await poll_effect_receipts(self.store), 0)
        state = await self.store.load(**self.scope_args)
        self.assertFalse(state.metadata["effect_waits"][self.plan_id]["delivered"])
        self.assertIn("无法核验", state.conversation[-1]["public_content"])
        await self.make_due()
        with patch("app.agent.effect_completion._poll") as read, patch(
            "app.agent.effect_completion._enqueue_late_receipt", return_value=True,
        ):
            self.assertEqual(await poll_effect_receipts(self.store), 1)
        read.assert_not_called()
        self.assertEqual(len((await self.store.load(**self.scope_args)).conversation), 1)

    async def test_disabled_telegram_keeps_receipt_without_endless_handoff(self):
        await self.pending(channel="telegram")
        with patch("app.modules.telegram_notification_center._agent_interaction_enabled", return_value=False):
            self.assertEqual((await self.finish())[0], 1)
        state = await self.store.load(**self.scope_args)
        self.assertTrue(state.metadata["effect_waits"][self.plan_id]["delivered"])
        self.assertNotIn("effect_next_poll_at", state.metadata)

    async def test_telegram_outbox_handoff_is_idempotent_and_deleted_session_invalidates(self):
        from app.agent.effect_completion import (
            _effect_reply_key, agent_effect_reply_is_current,
        )
        from app.modules.telegram_notification_center import get_notification
        await self.pending(channel="telegram")
        with patch("app.modules.telegram_notification_center._agent_interaction_enabled", return_value=True):
            self.assertEqual((await self.finish())[0], 1)
        key = _effect_reply_key({**self.scope_args, "plan_id": self.plan_id})
        # 原会话 chat，绝不落到默认通知接收人。
        from app.modules.telegram_notification_center import _event_key
        event_key = _event_key("event", "agent", key, "123")
        row = get_notification(event_key)
        self.assertIsNotNone(row)
        self.assertEqual(row["chat_id"], "123")
        self.assertTrue(agent_effect_reply_is_current(event_key))
        self.assertEqual((await self.finish())[0], 0)
        await self.store.reset_session(**self.scope_args)
        self.assertFalse(agent_effect_reply_is_current(event_key))


if __name__ == "__main__":
    unittest.main()
