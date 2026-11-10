"""原生确认、SQLite 和独立运行时之间，停止只能触及已捕获的任务。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.repositories import agent_jobs as agent_jobs_repo
from app.agent.confirmation import SQLiteConfirmationStore
from app.modules.library_patrol import empty_patrol_projection
from app.agent.effect_completion import wait_for_effect_completion
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.session_guard import session_scope_guard
from app.agent.kernel.state_types import AgentInput, SessionBusyError
from app.agent.models import ToolContext, ToolResult
from app.agent.task_stop import _stop_captured_turn, stop_agent_session
from tests.support import isolated_test_database
from tests.test_agent_batch_confirmation import FINAL, call, capture
from tests.test_agent_kernel_core import ScriptedModel


@pytest.mark.parametrize("stopper,cutover", [
    (stopper, cutover)
    for stopper in ("warm", "cold")
    for cutover in ("same", "before-marker", "after-marker", "after-turn-stop")
    if (stopper, cutover) != ("cold", "after-turn-stop")
])
@pytest.mark.parametrize("tracker_registered", [False, True], ids=["late-tracker", "captured-tracker"])
def test_native_stop_preserves_later_generation_jobs(stopper, tracker_registered, cutover):
    asyncio.run(_native_stop_race(stopper=stopper, tracker_registered=tracker_registered, cutover=cutover))


@pytest.mark.parametrize("stopper", ["warm", "cold"])
@pytest.mark.parametrize("cutover", ["reset", "delete"])
def test_native_stop_preserves_recreated_session_jobs(stopper, cutover):
    # 生命周期清理会删除回执；请求已捕获的稳定任务 ID 仍可取消，不能重读扩大范围。
    asyncio.run(_native_stop_race(stopper=stopper, tracker_registered=True, cutover=cutover))


async def _native_stop_race(*, stopper: str, tracker_registered: bool, cutover: str) -> None:
    with isolated_test_database():
        owner, session_id = "stop-generation-owner", "stop-generation-session"
        entered_confirmation, release_confirmation = asyncio.Event(), asyncio.Event()
        entered_marker, release_marker = asyncio.Event(), asyncio.Event()
        finished_turn_stop, release_turn_stop = asyncio.Event(), asyncio.Event()
        jobs: dict[str, str] = {}

        def runtime(name: str, *, pause: bool = False) -> SimpleNamespace:
            def prepare(arguments, context):
                return PreparedEffect(
                    preview=ToolResult(True, "preview", "将创建离线巡检作业").to_dict(),
                    snapshot_fingerprint="stop-generation-snapshot",
                )

            async def execute(arguments, expected, context):
                assert expected == "stop-generation-snapshot"
                job, created = agent_jobs_repo.create_agent_job(
                    owner=owner, job_type="library_episode_audit", dedupe_key=name,
                    input_json="{}", checkpoint_json="{}",
                    projection_json=json.dumps(empty_patrol_projection(as_of="2026-10-19")),
                )
                assert created and job["status"] == "pending"
                jobs[name] = job["job_id"]
                return ToolResult(True, "accepted", "全库检查已受理", effect_metadata={
                    "completion": {"kind": "agent_job", "job_id": job["job_id"], "operation": "audit"},
                })

            async def verify(arguments, result, context):
                if pause and not tracker_registered:
                    entered_confirmation.set()
                    await release_confirmation.wait()
                pending = await wait_for_effect_completion(
                    result, tool=name, context=ToolContext(owner=owner, session_id=session_id),
                    scope=context.completion_scope, timeout_seconds=0,
                )
                if pause and tracker_registered:
                    entered_confirmation.set()
                    await release_confirmation.wait()
                return pending

            tool = KernelToolSpec(
                name=name, domain="stop_test", description="离线巡检作业", effect=ToolEffect.WRITE,
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                prepare=prepare, execute_confirmed=execute, verify=verify,
            )
            store = SQLiteKernelStore(secret_provider=lambda: "stop-generation-only")
            catalog = ToolCatalog([tool])
            session = AgentSession(
                model=ScriptedModel([call(name, {}), FINAL]), catalog=catalog,
                 state_store=store,
                pipeline=ToolPipeline(catalog=catalog, state_store=store,
                    effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore())),
            )
            return SimpleNamespace(session=session, store=store)

        async def preview(target, name):
            view, events = await capture(target.session.run(AgentInput(
                message=f"执行 {name} 离线巡检作业", owner=owner, session_id=session_id, channel="web",
            )))
            assert view.status == "approval_required", [event.to_dict() for event in events]
            assert view.approval is not None
            return view.approval.plan_id

        async def confirm(target, plan):
            return await capture(target.session.confirm(
                owner=owner, session_id=session_id, plan_id=plan, channel="web",
            ))

        old = runtime("stop_test.old", pause=True)
        old_plan = await preview(old, "stop_test.old")
        confirmation = asyncio.create_task(confirm(old, old_plan))
        await asyncio.wait_for(entered_confirmation.wait(), 3)
        old_state = await old.store.load(owner=owner, session_id=session_id)
        with pytest.raises(SessionBusyError), session_scope_guard(owner, session_id):
            pass
        active = await old.session.coordinator.describe(owner=owner, session_id=session_id)
        assert active is not None and active["protected"] is True
        assert active["generation"] == old_state.generation
        target = old if stopper == "warm" else runtime("stop_test.cold")
        marker = target.store.request_stop_generation

        async def delayed_marker(**kwargs):
            marked = await marker(**kwargs) if cutover == "after-marker" else False
            entered_marker.set()
            await release_marker.wait()
            return marked if cutover == "after-marker" else await marker(**kwargs)

        async def delayed_turn_stop(*args, **kwargs):
            result = await _stop_captured_turn(*args, **kwargs)
            if cutover == "after-turn-stop":
                finished_turn_stop.set()
                await release_turn_stop.wait()
            return result

        with patch.object(target.store, "request_stop_generation", new=delayed_marker), patch(
            "app.agent.task_stop._stop_captured_turn", new=delayed_turn_stop,
        ), patch("app.modules.agent_jobs_scheduler.get_agent_jobs_scheduler"):
            stopping = asyncio.create_task(stop_agent_session(target, owner, session_id))
            try:
                await asyncio.wait_for(entered_marker.wait(), 3)
                release_confirmation.set()
                old_final, _ = await asyncio.wait_for(confirmation, 3)
                assert old_final.effect_result["data"]["background_job"]["followup_pending"]
                with session_scope_guard(owner, session_id):
                    pass
                assert target.session._start_lock.locked()

                if cutover == "after-turn-stop":
                    release_marker.set()
                    await asyncio.wait_for(finished_turn_stop.wait(), 3)

                new_plan = None
                if cutover != "same":
                    new = runtime("stop_test.new")
                    if cutover == "reset":
                        reset = await new.store.reset_session(owner=owner, session_id=session_id)
                        assert reset.lifecycle_id != old_state.lifecycle_id
                        assert reset.generation > old_state.generation
                    elif cutover == "delete":
                        assert await new.store.delete_session(owner=owner, session_id=session_id)
                    new_plan = await preview(new, "stop_test.new")
                    new_final, _ = await confirm(new, new_plan)
                    assert new_final.effect_result["data"]["background_job"]["followup_pending"]
                    new_state = await new.store.load(owner=owner, session_id=session_id)
                    assert new_state.generation > old_state.generation
                    if cutover in {"reset", "delete"}:
                        assert new_state.lifecycle_id != old_state.lifecycle_id
                    else:
                        assert new_state.lifecycle_id == old_state.lifecycle_id
                    pending_job = agent_jobs_repo.get_agent_job(owner=owner, job_id=jobs["stop_test.new"])
                    assert pending_job is not None and pending_job["status"] == "pending"

                release_marker.set()
                release_turn_stop.set()
                result = await asyncio.wait_for(stopping, 3)
            finally:
                release_confirmation.set()
                release_marker.set()
                release_turn_stop.set()
                await asyncio.gather(confirmation, stopping, return_exceptions=True)

        stopped_job = agent_jobs_repo.get_agent_job(owner=owner, job_id=jobs["stop_test.old"])
        assert stopped_job is not None and stopped_job["status"] == "cancelled"
        assert result["background_tasks"] == [{"plan_id": old_plan, "kind": "agent_job", "status": "cancelled"}]
        if cutover != "same":
            pending_job = agent_jobs_repo.get_agent_job(owner=owner, job_id=jobs["stop_test.new"])
            assert pending_job is not None and pending_job["status"] == "pending"
            current = await SQLiteKernelStore(secret_provider=lambda: "stop-generation-only").load(
                owner=owner, session_id=session_id,
            )
            assert current.metadata.get("stop_requested_generation") != current.generation
            assert current.metadata["effect_waits"][new_plan]["state"] == "pending"
            if cutover != "after-turn-stop":
                assert result["model_turn"] == "superseded"
        elif stopper == "warm":
            assert result["model_turn"] == "not_running"
