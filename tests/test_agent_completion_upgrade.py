"""升级前缺精确运行身份的持久跟踪必须收束未知，禁止认领后继运行。"""

from __future__ import annotations
from dataclasses import asdict

import json
import unittest
from typing import Any
from unittest.mock import patch

from app import database as db
from app.agent import effect_completion as effects
from app.agent.completion_observers import _CompletionTracker
from app.modules.library_patrol import build_persisted_patrol_projection
from app.agent.kernel.completion_receipts import EffectCompletionScope
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.models import ToolResult
from app.agent.public_view import public_result_state
from app.modules.agent_library_patrol_scheduler import AgentLibraryPatrolScheduler
from app.modules.organize_results import build_organize_result
from app.modules.organize_tasks import OrganizeTaskManager
from app.modules.scheduler import STRMScheduler
from tests.support import isolated_test_database


class CompletionUpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = isolated_test_database()
        self.database.__enter__()
        self.store = SQLiteKernelStore()
        self.scope = {"owner": "upgrade-owner", "session_id": "upgrade-session"}
        self.lease, _ = await self.store.begin_turn(**self.scope, request_id="old-confirm")

    async def asyncTearDown(self):
        self.database.__exit__(None, None, None)

    async def register(self, plan_id, kind, value):
        accepted = ToolResult(True, "accepted", "后台任务已提交")
        await effects._register_effect_wait(accepted, _CompletionTracker(kind, value),
            "test.background", EffectCompletionScope(self.store, self.lease, plan_id, "web"))
        return accepted

    async def make_due(self):
        def update(state):
            for wait in state.metadata["effect_waits"].values():
                wait["next_poll_at"] = 0
            state.metadata["effect_next_poll_at"] = 0
        await self.store.update_effect_state(**self.scope, change=update)

    async def test_cold_legacy_trackers_close_unknown_once_without_reading_or_repeating_business(self):
        trackers = (
            ("strm_run", {"after_run_id": 9, "trigger_type": "manual", "operation": "run"}),
            ("library_patrol", {"baseline_generation": 2, "baseline_status": "pending",
                "baseline_last_finished_at": "2026-10-19 10:00:00", "operation": "run"}),
            ("guangya_organize_task", {"task_id": "private-old-task", "operation": "run"}),
        )
        invalid_ids = (None, 0, -1, True, "bad", "7", 1.5, [])
        plans = []
        conversation: list[dict[str, Any]] = [{"role": "user", "content": "执行后台任务并回报实际结果。"}]
        for kind, original in trackers:
            for index, invalid in enumerate(invalid_ids):
                value: dict[str, Any] = dict(original)
                if invalid is not None:
                    value["run_id"] = invalid
                plan_id = f"legacy-{kind}-{index}"
                accepted = await self.register(plan_id, kind, value)
                plans.append(plan_id)
                conversation.extend([
                    {"role": "assistant", "content": "", "tool_calls": [{
                        "id": plan_id, "type": "function",
                        "function": {"name": "test__background", "arguments": "{}"}}]},
                    {"role": "tool", "name": "test__background", "tool_call_id": plan_id,
                        "effect_plan_id": plan_id, "content": json.dumps(accepted.to_dict())},
                ])
        await self.store.commit(self.lease, conversation=conversation)
        await self.make_due()
        cold = SQLiteKernelStore()
        with patch.object(db, "get_task_run") as read_run, patch.object(
            STRMScheduler, "trigger",
        ) as strm, patch.object(
            AgentLibraryPatrolScheduler, "trigger_now",
        ) as patrol, patch.object(OrganizeTaskManager, "start") as organize:
            for _ in plans:
                self.assertEqual(await effects.poll_effect_receipts(cold, limit=1), 1)
            self.assertEqual(await effects.poll_effect_receipts(SQLiteKernelStore(), limit=64), 0)
        for hook in (read_run, strm, patrol, organize):
            hook.assert_not_called()
        state = await cold.load(**self.scope)
        for plan_id in plans:
            wait = state.metadata["effect_waits"][plan_id]
            self.assertEqual(wait["state"], "terminal")
            self.assertTrue(wait["delivered"])
            self.assertEqual(wait["last_status"], "outcome_unknown")
            self.assertEqual(wait["read_errors"], 0)
            final = wait["final_result"]
            self.assertFalse(final["ok"])
            self.assertEqual(final["status"], "outcome_unknown")
            self.assertEqual(public_result_state(final), "warning")
            self.assertIn("自动跟踪已结束", final["summary"])
            self.assertIn("不要重复提交", "".join(final["suggestions"]))
            tool = next(row for row in state.conversation
                if row.get("role") == "tool" and row.get("effect_plan_id") == plan_id)
            self.assertEqual(json.loads(tool["content"]), final)
            self.assertNotIn("private-old-task", json.dumps(final))

    async def test_valid_run_read_failure_remains_retryable_and_recovers_original_result(self):
        patrol = build_persisted_patrol_projection(asdict(ToolResult(True, "up_to_date", "已核对",
            data={"checked_series_count": 0, "findings": []})))
        cases = (
            ("strm_run", "strm_sync", "success", {"stats": {"created": 1}}, "completed"),
            ("library_patrol", "agent_library_patrol", "up_to_date", patrol, "up_to_date"),
            ("guangya_organize_task", "guangya_organize", "success",
                build_organize_result({"moved": 1}, status="completed", task_id="exact-task"), "completed"),
        )
        for kind, task_name, status, payload, _ in cases:
            run_id = db.add_task_run(task_name, "manual", result=json.dumps(payload))
            db.finish_task_run(run_id, status, result=json.dumps(payload))
            await self.register(kind, kind, {"run_id": run_id, "operation": "run", "task_id": "exact-task"})
        await self.make_due()
        with patch.object(db, "get_task_run", side_effect=OSError("temporary database read failure")) as read:
            for _ in cases:
                self.assertEqual(await effects.poll_effect_receipts(SQLiteKernelStore(), limit=1), 0)
        self.assertEqual(read.call_count, 3)
        state = await self.store.load(**self.scope)
        for kind, *_ in cases:
            wait = state.metadata["effect_waits"][kind]
            self.assertEqual(wait["state"], "pending")
            self.assertFalse(wait["delivered"])
            self.assertEqual(wait["read_errors"], 1)
            self.assertNotIn("final_result", wait)
        await self.make_due()
        for _ in cases:
            self.assertEqual(await effects.poll_effect_receipts(SQLiteKernelStore(), limit=1), 1)
        state = await self.store.load(**self.scope)
        for kind, _, _, _, status in cases:
            wait = state.metadata["effect_waits"][kind]
            self.assertEqual(wait["final_result"]["status"], status)
            self.assertTrue(wait["final_result"]["ok"])
            self.assertTrue(wait["delivered"])
