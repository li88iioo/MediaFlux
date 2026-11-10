"""原生后台巡检沿真实 SQLite 单例、逐次运行记录和持久确认执行。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app import config, database as db, sdk
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog, ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.ports.sdk_effects import library_patrol_tool
from app.agent.kernel.state_types import _PendingEffectTransition
from app.agent.kernel.tool_calls import PreparedEffect
from app.modules import library_patrol
from app.modules.agent_library_patrol_scheduler import AgentLibraryPatrolScheduler
from app.modules.episode_audit import EpisodeAuditResult
from app.repositories import agent_library_patrol as repository
from tests.support import initialized_web_runtime
from tests.test_agent_kernel_existing_ports import context_for

OWNER = "webk:v1:" + "a" * 64


@pytest.fixture
def scheduler(monkeypatch):
    with initialized_web_runtime():
        config.set_and_save({"AGENT_LIBRARY_PATROL_ENABLED": "1", "AGENT_LIBRARY_PATROL_NOTIFY_ENABLED": "0"})
        result = EpisodeAuditResult(True, "up_to_date", "巡检完成", data={
            "as_of": "2026-08-03", "checked_series_count": 3, "findings": [], "findings_truncated": False})
        instance = AgentLibraryPatrolScheduler(clock=lambda: datetime(2026, 8, 3, 12),
            audit_executor=Mock(return_value=(result, 10)), notification_sender=Mock(side_effect=AssertionError("no notification")))
        monkeypatch.setattr("app.modules.agent_library_patrol_scheduler.get_agent_library_patrol_scheduler", lambda: instance)
        yield instance
        instance.stop()


def executor(store):
    return EffectExecutor(catalog=ToolCatalog([library_patrol_tool()]), state_store=store, reference_store=store,
        effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()), authorization=MediaFluxAuthorizationPolicy(),
        effect_lifecycle=MediaFluxEffectLifecycle())


def runs():
    with db.get_conn() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM task_runs WHERE task_name='agent_library_patrol' ORDER BY id")]


async def prepare(effects, context):
    result = await effects.prepare("library.run_patrol", {}, context=context)
    assert result.effect_plan is not None
    return result.effect_plan


def test_cold_confirmation_is_owner_session_bound_accepted_and_once_only(scheduler):
    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER, session="patrol-session")
        effects = executor(store)
        assert sdk.library.patrol_status()["status"] == "not_run"
        with pytest.raises(ValueError, match="确认"):
            sdk.library.run_patrol()
        plan = await prepare(effects, context)
        assert plan.snapshot_fingerprint.startswith("mfkr2:")
        assert runs() == [] and repository.get_agent_library_patrol() is None
        cold = executor(SQLiteKernelStore())
        for owner, session in [(OWNER, "foreign-session"), ("webk:v1:" + "b" * 64, "patrol-session")]:
            foreign = await context_for(store, owner=owner, session=session)
            with pytest.raises(ToolPipelineError):
                await cold.execute_confirmed(plan.plan_id, context=foreign)
        accepted = (await cold.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
        assert accepted["status"] == "accepted" and accepted["data"]["queued"]
        run_id = accepted["data"]["run_id"]
        assert run_id > 0 and len(runs()) == 1
        assert sdk.library.patrol_status(run_id)["status"] == "queued"
        before = runs()
        with pytest.raises(ToolPipelineError):
            await cold.execute_confirmed(plan.plan_id, context=context)
        assert runs() == before
        assert scheduler.run_once() == 1
        completed = sdk.library.patrol_status(run_id)
        assert completed["status"] == "up_to_date" and completed["data"]["checked_series_count"] == 3
        assert completed["data"]["run_id"] == run_id
        scheduler._notification_sender.assert_not_called()
    asyncio.run(exercise())


@pytest.mark.parametrize("ending", ["cancel_ticket", "cancel_before_confirm", "cancel_in_transaction", "cancel_prepare", "config_aba", "disabled"])
def test_cancelled_or_changed_policy_cannot_create_a_patrol_run(scheduler, ending):
    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        effects = executor(store)
        if ending == "disabled":
            config.set_and_save({"AGENT_LIBRARY_PATROL_ENABLED": "0"})
            with pytest.raises(ToolPipelineError) as failure:
                await effects.prepare("library.run_patrol", {}, context=context)
            assert failure.value.code == "disabled"
        elif ending == "cancel_prepare":
            original_prepare = library_patrol.prepare_patrol
            def cancelled_prepare():
                snapshot = original_prepare()
                context.cancellation.cancel()
                return snapshot
            with patch.object(library_patrol, "prepare_patrol", side_effect=cancelled_prepare), pytest.raises(asyncio.CancelledError):
                await effects.prepare("library.run_patrol", {}, context=context)
        else:
            plan = await prepare(effects, context)
            if ending == "config_aba":
                config.set_and_save({"AGENT_LIBRARY_PATROL_MAX_SERIES": "60"})
                config.set_and_save({"AGENT_LIBRARY_PATROL_MAX_SERIES": "50"})
            elif ending == "cancel_ticket":
                assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
            elif ending == "cancel_before_confirm":
                context.cancellation.cancel()
            original_state = repository.patrol_task_state
            def cancelled_in_transaction(row):
                context.cancellation.cancel()
                return original_state(row)
            with patch.object(repository, "patrol_task_state", side_effect=cancelled_in_transaction) if ending == "cancel_in_transaction" else patch.object(repository, "patrol_task_state", wraps=original_state):
                with pytest.raises(asyncio.CancelledError if ending.startswith("cancel_") and ending != "cancel_ticket" else ToolPipelineError):
                    await effects.execute_confirmed(plan.plan_id, context=context)
        assert runs() == [] and repository.get_agent_library_patrol() is None
        scheduler._audit_executor.assert_not_called()
    asyncio.run(exercise())


def test_atomic_trigger_rejects_task_that_arrived_after_service_recheck(scheduler):
    original = scheduler.trigger_now
    concurrent = {}

    def arrive_before_transaction(**options):
        concurrent.update(repository.trigger_agent_library_patrol(next_run_at=scheduler._now()))
        return original(**options)

    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        effects = executor(store)
        plan = await prepare(effects, context)
        with patch.object(scheduler, "trigger_now", side_effect=arrive_before_transaction):
            with pytest.raises(ToolPipelineError) as failure:
                await effects.execute_confirmed(plan.plan_id, context=context)
        assert failure.value.code == "confirmation_stale" and failure.value.execution_started is False
        assert len(runs()) == 1 and runs()[0]["id"] == concurrent["run_id"]
        current = repository.get_agent_library_patrol()
        assert current is not None and current["cycle_run_id"] == concurrent["run_id"]
        assert not scheduler._wake_event.is_set()
    asyncio.run(exercise())


def test_running_patrol_reuses_id_and_next_cycle_cannot_overwrite_original_query(scheduler):
    async def exercise():
        first = library_patrol.run_patrol(library_patrol.prepare_patrol())
        run_id = first["data"]["run_id"]
        assert repository.claim_due_agent_library_patrol(current_time=scheduler._now()) is not None
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        effects = executor(store)
        plan = await prepare(effects, context)
        reused = (await effects.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
        assert reused["status"] == "accepted" and reused["data"]["reused"]
        assert reused["data"]["run_id"] == run_id and len(runs()) == 1
        repository.cancel_agent_library_patrol_lease(next_run_at=scheduler._now())
        assert sdk.library.patrol_status(run_id)["status"] == "cancelled"
        later = library_patrol.run_patrol(library_patrol.prepare_patrol())
        assert later["data"]["run_id"] != run_id
        assert scheduler.run_once() == 1
        assert sdk.library.patrol_status()["status"] == "up_to_date"
        assert sdk.library.patrol_status(run_id)["status"] == "cancelled"
    asyncio.run(exercise())


def test_task_lease_changes_invalidate_frozen_patrol(scheduler):
    async def exercise():
        first = library_patrol.run_patrol(library_patrol.prepare_patrol())
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        effects = executor(store)
        plan = await prepare(effects, context)
        assert repository.claim_due_agent_library_patrol(current_time=scheduler._now()) is not None
        before = runs()
        with pytest.raises(ToolPipelineError) as failure:
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert failure.value.code == "confirmation_stale" and failure.value.execution_started is False
        assert runs() == before and len(before) == 1 and before[0]["id"] == first["data"]["run_id"]
    asyncio.run(exercise())


def test_unknown_admission_reply_never_replays_the_patrol(scheduler):
    original = library_patrol.run_patrol

    def lost_reply(*args, **kwargs):
        assert original(*args, **kwargs)["ok"]
        raise RuntimeError("lost reply")

    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        effects = executor(store)
        plan = await prepare(effects, context)
        with patch.object(library_patrol, "run_patrol", side_effect=lost_reply) as admission:
            with pytest.raises(ToolPipelineError) as failure:
                await effects.execute_confirmed(plan.plan_id, context=context)
            assert failure.value.execution_started is True
            before = runs()
            with pytest.raises(ToolPipelineError):
                await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
            admission.assert_called_once()
            assert runs() == before and len(before) == 1
            assert sdk.library.patrol_status(before[0]["id"])["status"] == "queued"
    asyncio.run(exercise())


def test_retired_patrol_confirmation_never_executes_as_native(scheduler):
    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        effects = executor(store)
        old = effects.effect_store.freeze(owner=OWNER, session_id=context.session_id, generation=context.lease.generation,
            tool_name="library.trigger_patrol_now", effect=ToolEffect.WRITE, arguments={},
            prepared=PreparedEffect(preview={"ok": True, "summary": "旧巡检确认"}, snapshot_fingerprint="legacy-patrol"))
        await store.commit(context.lease, updates=(_PendingEffectTransition("pending_effect_plan_id", old.plan_id,
            plan_generation=context.lease.generation, goal={"role": "user", "content": "旧巡检"}),))
        with pytest.raises(ToolPipelineError) as failure:
            await executor(SQLiteKernelStore()).execute_confirmed(old.plan_id, context=context)
        assert failure.value.code == "tool_not_found" and failure.value.execution_started is False
        assert runs() == [] and repository.get_agent_library_patrol() is None
    asyncio.run(exercise())


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="真实 Linux root 工作区验收")
def test_workspace_patrol_cold_confirmation_does_not_replay_script(scheduler, monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))

    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER, session="workspace-patrol")
        catalog = ToolCatalog([library_patrol_tool()])
        pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store,
            effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()), authorization=MediaFluxAuthorizationPolicy())
        catalog.register(sandbox.run_code_tool(pipeline))
        result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": '''
from pathlib import Path
import json
from mf import library
with Path("runs.txt").open("a") as output:
    output.write("run\\n")
Path("status.json").write_text(json.dumps(library.patrol_status()))
library.run_patrol()
Path("tail.txt").write_text("must not resume")
'''}, context=context)
        assert result.effect_plan is not None and result.effect_plan.tool_name == "library.run_patrol"
        assert runs() == [] and repository.get_agent_library_patrol() is None
        cold = executor(SQLiteKernelStore())
        accepted = (await cold.execute_confirmed(result.effect_plan.plan_id, context=context)).outcome.public_content
        assert accepted["status"] == "accepted" and sdk.library.patrol_status(accepted["data"]["run_id"])["status"] == "queued"
        before = runs()
        with pytest.raises(ToolPipelineError):
            await cold.execute_confirmed(result.effect_plan.plan_id, context=context)
        assert runs() == before
        root, _ = sandbox._workspace_location(context.owner, context.session_id)
        assert (root / "work/runs.txt").read_text() == "run\n"
        assert not (root / "work/tail.txt").exists()
        assert scheduler.run_once() == 1
        assert sdk.library.patrol_status(accepted["data"]["run_id"])["status"] == "up_to_date"
    asyncio.run(exercise())
