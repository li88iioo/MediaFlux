"""真实进程活性与确认管理：只用本地领域数据和脚本模型，不调用云端。"""
from __future__ import annotations

import asyncio
import multiprocessing
import os
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from app import database as db

from app.agent.kernel.events import AgentEventType as E
from app.agent.kernel.model import ModelEvent, ModelEventType as M, ModelToolCall
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.session import AgentSession
from app.agent.kernel.session_guard import foreground_execution_active
from app.agent.kernel.state_types import AgentInput, SessionBusyError
from tests import test_agent_scoped_trust as trust_cases
from tests.test_agent_confirmation_edit import EditRig
from tests.test_agent_kernel_core import ScriptedModel, collect

trust_rig = trust_cases.rig
task_store = trust_cases.task_store
OWNER, SESSION = trust_cases.OWNER, trust_cases.SESSION
_STORE_SECRET = "task-test-only"
_WEB_SECRET = "scoped-trust-test-only"


def _session(rig, model, *, journal=None):
    return AgentSession(model=model, catalog=rig.pipeline.catalog,
                        pipeline=rig.pipeline, state_store=rig.store, journal=journal)


def _edit_round(rig, label):
    return [ModelEvent(M.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(label, rig.tool.name,
                {"label": label, "object_id": "opaque-object-7", "signature": "credential-signed-original"})),
            ModelEvent(M.FINISH, finish_reason="tool_calls")]


async def _preview(rig):
    calls = trust_cases.call(trust_cases.arguments()) if isinstance(rig, trust_cases.TrustRig) else _edit_round(rig, "original")
    session = _session(rig, ScriptedModel([calls]))
    events = await collect(session.run(AgentInput(owner=OWNER, session_id=SESSION,
        request_id="preview", message="按已核实对象准备修改确认卡。")))
    assert events[-1].payload["status"] == "approval_required"
    plan = next(event.payload["plan"] for event in events if event.type is E.EFFECT_APPROVAL_REQUIRED)
    return session, plan["plan_id"]


def _ordinary_worker(database, termination, ready, release):
    db.configure_database(database, test_mode=True)

    class HeldModel:
        async def stream(self, request, *, cancellation):
            ready.set()
            if termination == "kill":
                await asyncio.Event().wait()
            if not await asyncio.to_thread(release.wait, 30):
                raise TimeoutError("ordinary process release barrier")
            if termination == "exit":
                os._exit(23)
            yield ModelEvent(M.TEXT_DELTA, text="保留原确认卡。")
            yield ModelEvent(M.FINISH, finish_reason="stop")

    async def run():
        rig = EditRig(SQLiteKernelStore(secret_provider=lambda: _STORE_SECRET))
        session = _session(rig, HeldModel())
        events = await collect(session.run(AgentInput(owner=OWNER, session_id=SESSION,
            request_id="held-ordinary-process", message="只解释当前计划，保留待确认卡。")))
        assert events[-1].payload["status"] == "success", events
        assert rig.writes == []

    with patch("app.modules.web_secret.get_web_secret", lambda: _WEB_SECRET):
        asyncio.run(run())


@asynccontextmanager
async def _held_ordinary_process(termination):
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    worker = context.Process(target=_ordinary_worker,
                             args=(str(db.resolve_db_path()), termination, ready, release))
    worker.start()
    try:
        assert await asyncio.to_thread(ready.wait, 15), f"ordinary process did not start: exit={worker.exitcode}"
        yield worker, release
    finally:
        if worker.is_alive():
            worker.kill()
        await asyncio.to_thread(worker.join, 10)
        assert not worker.is_alive()


async def _manage(session, operation, plan_id):
    if operation == "edit":
        result = await session.edit_confirmation(owner=OWNER, session_id=SESSION,
            plan_id=plan_id, arguments={"label": "after-process"})
        assert result.effect_plan is not None
        return result.effect_plan.plan_id
    grant = await session.set_scoped_trust(owner=OWNER, session_id=SESSION, operation="grant",
        plan_id=plan_id, tool_name=trust_cases.TOOL, scope=trust_cases.SCOPE, ttl_seconds=300, max_actions=5)
    assert grant["active"] and grant["remaining"] == 5 and grant["scope"] == trust_cases.SCOPE
    return plan_id


@pytest.mark.parametrize("operation", ["edit", "grant"])
@pytest.mark.parametrize("termination", ["kill", "exit"])
def test_dead_foreground_allows_cold_management_and_original_write_exactly_once(trust_rig, operation, termination):
    async def run():
        original = EditRig(trust_rig.store) if operation == "edit" else trust_rig
        _source, plan_id = await _preview(original)
        async with _held_ordinary_process(termination) as (worker, release):
            before = await original.store.load(owner=OWNER, session_id=SESSION)
            assert before.pending_effect_plan_id == plan_id
            assert before.metadata["active_publication"]["status"] == "running"
            assert foreground_execution_active(OWNER, SESSION, before.lifecycle_id, before.generation)
            if termination == "kill":
                worker.kill()
            else:
                release.set()
            await asyncio.to_thread(worker.join, 10)
            assert not worker.is_alive()
            assert worker.exitcode == 23 if termination == "exit" else worker.exitcode != 0
            cold_store = SQLiteKernelStore(secret_provider=lambda: _STORE_SECRET)
            cold_rig = type(original)(cold_store)
            cold = _session(cold_rig, ScriptedModel([trust_cases.FINAL]))
            assert await cold.coordinator.describe(owner=OWNER, session_id=SESSION) is None
            crashed = await cold_store.load(owner=OWNER, session_id=SESSION)
            assert crashed.metadata["active_publication"]["status"] == "running"
            assert not foreground_execution_active(OWNER, SESSION, crashed.lifecycle_id, crashed.generation)
            pending = await cold.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION)
            assert pending is not None and pending.plan_id == plan_id
            managed_plan = await _manage(cold, operation, plan_id)
            managed = await cold_store.load(owner=OWNER, session_id=SESSION)
            assert (managed.lifecycle_id, managed.generation) == (before.lifecycle_id, before.generation)
            assert managed.metadata["active_publication"]["status"] == "finished"
            assert managed.pending_effect_plan_id == managed_plan
            confirmed = await collect(cold.confirm(owner=OWNER, session_id=SESSION, plan_id=managed_plan))
            assert sum(event.type is E.EFFECT_COMPLETED for event in confirmed) == 1
            assert confirmed[-1].payload["status"] == "success"
            if operation == "edit":
                assert [item["label"] for item in cold_rig.writes] == ["after-process"]
                assert original.writes == []
            else:
                assert isinstance(cold_rig, trust_cases.TrustRig)
                assert isinstance(original, trust_cases.TrustRig)
                assert len(cold_rig.attempts) == 1 and original.attempts == []
                assert db.get_media_watchlist("tmdb", "100", "tv") is not None
            replay_rig = type(original)(SQLiteKernelStore(secret_provider=lambda: _STORE_SECRET))
            replay = _session(replay_rig, ScriptedModel([]))
            duplicate = await collect(replay.confirm(owner=OWNER, session_id=SESSION, plan_id=managed_plan))
            assert duplicate[-1].type is E.TURN_FAILED
            assert duplicate[-1].payload["code"] == "confirmation_invalid"
            if isinstance(replay_rig, EditRig):
                assert replay_rig.writes == []
            else:
                assert replay_rig.attempts == []

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["edit", "grant"])
def test_live_foreground_rejects_cold_management_but_allows_revocation(trust_rig, operation):
    async def run():
        original = EditRig(trust_rig.store) if operation == "edit" else trust_rig
        source, plan_id = await _preview(original)
        if operation == "grant":
            await _manage(source, operation, plan_id)
        async with _held_ordinary_process("finish") as (worker, release):
            cold_rig = type(original)(SQLiteKernelStore(secret_provider=lambda: _STORE_SECRET))
            cold = _session(cold_rig, ScriptedModel([]))
            before = await cold.state_store.load(owner=OWNER, session_id=SESSION)
            assert before.pending_effect_plan_id == plan_id
            assert await cold.coordinator.describe(owner=OWNER, session_id=SESSION) is None
            with pytest.raises(SessionBusyError):
                await _manage(cold, operation, plan_id)
            assert await cold.state_store.load(owner=OWNER, session_id=SESSION) == before
            assert await cold.set_scoped_trust(owner=OWNER, session_id=SESSION, operation="revoke") == {"active": False}
            revoked = await cold.state_store.load(owner=OWNER, session_id=SESSION)
            assert "scoped_trust" not in revoked.metadata
            assert revoked.pending_effect_plan_id == plan_id and revoked.generation == before.generation
            release.set()
            await asyncio.to_thread(worker.join, 10)
            assert worker.exitcode == 0
            await _manage(cold, operation, plan_id)
            if isinstance(cold_rig, EditRig):
                assert cold_rig.writes == []
            else:
                assert cold_rig.attempts == []

    asyncio.run(run())


def test_finished_cold_confirmation_ignores_fenced_old_ordinary_foreground(trust_rig):
    async def run():
        held, release = asyncio.Event(), asyncio.Event()
        original = EditRig(trust_rig.store)

        class HeldJournal:
            async def append(self, event, *, owner):
                await original.store.append(event, owner=owner)

            async def append_for_turn(self, event, lease):
                await original.store.append_for_turn(event, lease)
                if event.type is E.TURN_COMPLETED and event.payload.get("status") == "approval_required":
                    held.set()
                    await release.wait()

        source = _session(original, ScriptedModel([_edit_round(original, "A")]), journal=HeldJournal())
        prior = asyncio.create_task(collect(source.run(AgentInput(owner=OWNER, session_id=SESSION,
            request_id="held-original-preview", message="修改名称A后继续预检B。"))))
        try:
            await asyncio.wait_for(held.wait(), 5)
            active = await source.coordinator.describe(owner=OWNER, session_id=SESSION)
            assert active is not None
            first = (await original.store.load(owner=OWNER, session_id=SESSION)).pending_effect_plan_id
            cold_rig = EditRig(SQLiteKernelStore(secret_provider=lambda: _STORE_SECRET))
            cold = _session(cold_rig, ScriptedModel([_edit_round(cold_rig, "B")]))
            confirmed = await collect(cold.confirm(owner=OWNER, session_id=SESSION, plan_id=first))
            assert any(event.type is E.EFFECT_COMPLETED for event in confirmed)
            assert [item["label"] for item in cold_rig.writes] == ["A"]
            state = await cold.state_store.load(owner=OWNER, session_id=SESSION)
            assert state.pending_effect_plan_id and state.pending_effect_plan_id != first
            assert state.metadata["active_publication"]["status"] == "finished"
            assert state.metadata["confirmed_publication"]["turn_id"] != active["turn_id"]
            assert await cold.coordinator.describe(owner=OWNER, session_id=SESSION) is None
            assert foreground_execution_active(OWNER, SESSION, state.lifecycle_id, state.generation)
            assert not prior.done()
            edited = await cold.edit_confirmation(owner=OWNER, session_id=SESSION,
                plan_id=state.pending_effect_plan_id, arguments={"label": "C"})
            assert edited.effect_plan is not None
            assert len(cold_rig.writes) == 1
        finally:
            release.set()
            await asyncio.wait_for(prior, 5)

    asyncio.run(run())
