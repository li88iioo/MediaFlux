from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.agent.domain_catalog.cloud_runtime import guangya_organize_status
from app.agent.effect_completion import (
    _CompletionTracker,
    _patrol_status,
    wait_for_effect_completion,
)
from app.agent.kernel.ports.existing_actions import adapt_tool_spec
from app.agent.kernel.state import CancellationToken, InMemorySessionStateStore
from app.agent.models import RiskLevel, ToolContext, ToolReference, ToolResult, ToolSpec

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


if __name__ == "__main__":
    unittest.main()
