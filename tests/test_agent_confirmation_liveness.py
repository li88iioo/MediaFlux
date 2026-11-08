"""确认尾段沿真实执行者判活；管理检查与变更处于同一互斥窗口。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import partial
from unittest.mock import patch

import pytest

from app import database as db
from app.agent import effect_completion as effects
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.events import AgentEventType as E
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.session_guard import continuation_execution_guard
from app.agent.kernel.state_types import AgentInput, SessionBusyError
from tests.test_agent_confirmation_edit import OWNER, SESSION, EditRig
from tests.test_agent_kernel_core import collect
from tests.test_agent_recovered_progress import reply
from tests.test_agent_tool_argument_ingress import PROTOCOLS, Wire
from tests.test_agent_tool_argument_ingress import offline as offline  # noqa: PLC0414
from tests.test_agent_tool_argument_ingress import store as store  # noqa: PLC0414


@pytest.fixture
def rig(store, monkeypatch):
    monkeypatch.setattr("app.modules.web_secret.get_web_secret", lambda: "confirmation-edit-test-only")
    current = EditRig(store)
    current.tool = replace(current.tool, metadata={"risk": "low_write"},
        approval_scope=lambda _name, arguments, _metadata: {"label": arguments["label"]})
    current.pipeline.catalog = ToolCatalog([current.tool])
    return current


def native_call(protocol, rig, label, *, index=0):
    return reply(protocol, name=rig.tool.model_name, index=index, arguments={"label": label,
        "object_id": "opaque-object-7", "signature": "credential-signed-original"})


def native_session(rig, wire, *, cold=False, journal=None):
    current = SQLiteKernelStore(secret_provider=lambda: "offline-tool-argument-test") if cold else rig.store
    return AgentSession(model=wire.adapter, catalog=rig.pipeline.catalog,
         state_store=current, journal=journal,
        pipeline=ToolPipeline(catalog=rig.pipeline.catalog, state_store=current,
            effect_store=rig.pipeline.effect_store))


class PausedJournal:
    def __init__(self, store, event_type):
        self.store, self.event_type = store, event_type
        self.ready, self.release = asyncio.Event(), asyncio.Event()
        self.event = None
        self.armed = False

    async def append(self, event, *, owner):
        await self.store.append(event, owner=owner)

    async def append_for_turn(self, event, lease):
        await self.store.append_for_turn(event, lease)
        if (self.armed and self.event is None and event.type is self.event_type
                and (event.type is not E.TURN_COMPLETED or event.payload.get("status") == "approval_required")):
            self.event = event
            self.ready.set()
            await self.release.wait()


async def manage(session, rig, plan_id, operation):
    if operation == "edit":
        return await session.edit_confirmation(owner=OWNER, session_id=SESSION,
            plan_id=plan_id, arguments={"label": "C"})
    return await session.set_scoped_trust(owner=OWNER, session_id=SESSION, operation="grant",
        plan_id=plan_id, tool_name=rig.tool.name, scope={"label": "B"}, ttl_seconds=300, max_actions=5)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("operation", ["edit", "grant"])
def test_cold_management_rejects_live_direct_confirmation_tail_without_losing_card(rig, protocol, operation):
    async def run():
        wire = Wire(protocol, [native_call(protocol, rig, "A"), native_call(protocol, rig, "B", index=1)])
        journal = PausedJournal(rig.store, E.EFFECT_APPROVAL_REQUIRED)
        source = native_session(rig, wire, journal=journal)
        first = await collect(source.run(AgentInput(message="修改名称A，然后修改B。", owner=OWNER, session_id=SESSION)))
        first_id = first[-1].payload["plan_id"]
        journal.armed = True
        prior = asyncio.create_task(collect(source.confirm(owner=OWNER, session_id=SESSION, plan_id=first_id)))
        try:
            await asyncio.wait_for(journal.ready.wait(), 5)
            state = await rig.store.load(owner=OWNER, session_id=SESSION)
            second_id = state.pending_effect_plan_id
            assert second_id != first_id and state.metadata["active_publication"]["status"] == "running"
            assert not prior.done() and [row["label"] for row in rig.writes] == ["A"]
            cold = native_session(rig, Wire(protocol, []), cold=True)
            assert await cold.coordinator.describe(owner=OWNER, session_id=SESSION) is None
            with pytest.raises(SessionBusyError):
                await manage(cold, rig, second_id, operation)
            after = await cold.state_store.load(owner=OWNER, session_id=SESSION)
            assert after == state and len(rig.prepares) == 2
            pending = await cold.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION)
            assert pending is not None and pending.plan_id == second_id
        finally:
            journal.release.set()
            await asyncio.wait_for(prior, 5)
        result = await manage(cold, rig, second_id, operation)
        if operation == "edit":
            assert result.effect_plan is not None and result.effect_plan.arguments["label"] == "C"
        else:
            assert result["active"] is True and result["remaining"] == 5
        assert [row["label"] for row in rig.writes] == ["A"]
        wire.verify(2)

    asyncio.run(run())


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_scoped_confirmation_keeps_ordinary_foreground_live_and_revoke_available(rig, protocol):
    async def run():
        initial_wire = Wire(protocol, [native_call(protocol, rig, "A")])
        source = native_session(rig, initial_wire)
        initial = await collect(source.run(AgentInput(message="修改名称A。", owner=OWNER, session_id=SESSION)))
        plan_id = initial[-1].payload["plan_id"]
        grant = await source.set_scoped_trust(owner=OWNER, session_id=SESSION, operation="grant", plan_id=plan_id,
            tool_name=rig.tool.name, scope={"label": "A"}, ttl_seconds=300, max_actions=5)
        assert grant["active"] is True
        assert await source.cancel_effect(owner=OWNER, session_id=SESSION, plan_id=plan_id)
        wire = Wire(protocol, [native_call(protocol, rig, "A", index=1), native_call(protocol, rig, "B", index=2)])
        journal = PausedJournal(rig.store, E.EFFECT_APPROVAL_REQUIRED)
        journal.armed = True
        source.model, source.journal = wire.adapter, journal
        prior = asyncio.create_task(collect(source.run(AgentInput(
            message="执行预授权A，随后生成B的确认卡。", owner=OWNER, session_id=SESSION))))
        try:
            await asyncio.wait_for(journal.ready.wait(), 5)
            state = await rig.store.load(owner=OWNER, session_id=SESSION)
            assert state.metadata["active_publication"]["status"] == "running"
            current = await source.coordinator.describe(owner=OWNER, session_id=SESSION)
            assert current is not None and state.metadata["confirmed_publication"]["turn_id"] == current["turn_id"]
            assert state.metadata["scoped_trust"]["remaining"] == 4
            cold = native_session(rig, Wire(protocol, []), cold=True)
            for operation in ("edit", "grant"):
                with pytest.raises(SessionBusyError):
                    await manage(cold, rig, state.pending_effect_plan_id, operation)
            assert await cold.set_scoped_trust(owner=OWNER, session_id=SESSION, operation="revoke") == {"active": False}
            pending = await cold.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION)
            assert pending is not None and pending.plan_id == state.pending_effect_plan_id
            assert len(rig.prepares) == 3 and [row["label"] for row in rig.writes] == ["A"]
        finally:
            journal.release.set()
            await asyncio.wait_for(prior, 5)
        initial_wire.verify()
        wire.verify(2)

    asyncio.run(run())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("cold", [False, True], ids=["same-runtime", "cold-runtime"])
@pytest.mark.parametrize("origin", ["ordinary", "confirmation"])
def test_published_next_card_can_be_confirmed_before_previous_executor_finally(rig, protocol, cold, origin):
    async def run():
        replies = [native_call(protocol, rig, "A")]
        if origin == "confirmation":
            replies.append(native_call(protocol, rig, "B", index=1))
        wire = Wire(protocol, replies)
        journal = PausedJournal(rig.store, E.TURN_COMPLETED)
        source = native_session(rig, wire, journal=journal)
        first_input = AgentInput(message="依次修改名称A、B。", owner=OWNER, session_id=SESSION)
        if origin == "confirmation":
            first = await collect(source.run(first_input))
            journal.armed = True
            prior = asyncio.create_task(collect(source.confirm(owner=OWNER, session_id=SESSION,
                plan_id=first[-1].payload["plan_id"])))
        else:
            journal.armed = True
            prior = asyncio.create_task(collect(source.run(first_input)))
        try:
            await asyncio.wait_for(journal.ready.wait(), 5)
            plan_id = (await rig.store.load(owner=OWNER, session_id=SESSION)).pending_effect_plan_id
            approvals = [event for event in await rig.store.list_events(owner=OWNER, session_id=SESSION)
                         if event["type"] == E.EFFECT_APPROVAL_REQUIRED.value]
            assert approvals[-1]["payload"]["plan"]["plan_id"] == plan_id
            accepted_wire = Wire(protocol, [reply(protocol, text="按实际回执核对名称。")])
            receiver = native_session(rig, accepted_wire, cold=True) if cold else source
            if not cold:
                receiver.model = accepted_wire.adapter
            accepted = await collect(receiver.confirm(owner=OWNER, session_id=SESSION, plan_id=plan_id))
            assert accepted[-1].type is E.TURN_COMPLETED and accepted[-1].payload["status"] == "success"
            assert [row["label"] for row in rig.writes] == (["A"] if origin == "ordinary" else ["A", "B"])
            assert not (await rig.store.load(owner=OWNER, session_id=SESSION)).pending_effect_plan_id
            assert not prior.done(), "已发布确认卡可立即消费，不必等待上一执行者的 finally"
            accepted_wire.verify()
        finally:
            journal.release.set()
            await asyncio.wait_for(prior, 5)
        wire.verify(1 if origin == "ordinary" else 2)

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["edit", "grant"])
def test_management_holds_idle_check_and_actual_mutation_against_new_turn_and_continuation(rig, operation):
    async def run():
        first_wire = Wire("chat_completions", [native_call("chat_completions", rig, "B")])
        source = native_session(rig, first_wire)
        first = await collect(source.run(AgentInput(message="修改名称B。", owner=OWNER, session_id=SESSION)))
        plan_id = first[-1].payload["plan_id"]
        before = await rig.store.load(owner=OWNER, session_id=SESSION)
        checked, release = asyncio.Event(), asyncio.Event()
        original = source._confirmation_context

        async def pause_after_check(**kwargs):
            context = await original(**kwargs)
            checked.set()
            await release.wait()
            return context

        with patch.object(source, "_confirmation_context", pause_after_check):
            managing = asyncio.create_task(manage(source, rig, plan_id, operation))
            try:
                await asyncio.wait_for(checked.wait(), 5)
                peer_wire = Wire("chat_completions", [reply("chat_completions", text="后来的普通问答。")])
                peer = native_session(rig, peer_wire, cold=True)
                events = await collect(peer.run(AgentInput(message="另一个问题。", owner=OWNER, session_id=SESSION)))
                assert events[-1].type is E.TURN_FAILED and events[-1].payload["code"] == "effect_in_progress"
                assert not peer_wire.requests and (await rig.store.load(owner=OWNER, session_id=SESSION)) == before
                with continuation_execution_guard(OWNER, SESSION) as acquired:
                    assert not acquired, "检查后到变更完成期间不能让定时续行取得执行权"
            finally:
                release.set()
                outcome, = await asyncio.gather(managing, return_exceptions=True)
        assert not isinstance(outcome, BaseException), repr(outcome)
        after = await rig.store.load(owner=OWNER, session_id=SESSION)
        assert after.generation == before.generation and rig.writes == []
        assert (await source.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION)) is not None
        first_wire.verify()

    asyncio.run(run())


@pytest.mark.parametrize("cold", [False, True], ids=["same-runtime", "cold-runtime"])
def test_native_resume_next_card_can_be_confirmed_before_previous_continuation_finally(tmp_path, cold):
    from tests.test_agent_effect_continuation import (
        OWNER,
        SID,
        FollowScript,
        NextWriteScript,
        _ready_case,
        _session,
    )

    async def run():
        async with _ready_case(tmp_path) as (store, candidate, client, reads, stack, _users):
            source = _session(store, NextWriteScript(), client, reads, stack=stack)
            source.pipeline.effect_store = ConfirmationEffectPlanStore(SQLiteConfirmationStore())
            journal = PausedJournal(store, E.TURN_COMPLETED)
            journal.armed = True
            source.journal = journal
            prior = asyncio.create_task(source.resume_wait(candidate=candidate))
            try:
                await asyncio.wait_for(journal.ready.wait(), 8)
                saved = await store.list_events(owner=OWNER, session_id=SID)
                approvals = [event for event in saved if event["type"] == E.EFFECT_APPROVAL_REQUIRED.value]
                plan_id = (await store.load(owner=OWNER, session_id=SID)).pending_effect_plan_id
                assert plan_id and approvals[-1]["payload"]["plan"]["plan_id"] == plan_id
                with continuation_execution_guard(OWNER, SID) as acquired:
                    assert not acquired
                receiver = _session(SQLiteKernelStore(), FollowScript(resume=True), client, reads, stack=stack) if cold else source
                if cold:
                    receiver.pipeline.effect_store = ConfirmationEffectPlanStore(SQLiteConfirmationStore())
                with patch.object(effects, "wait_for_effect_completion",
                        partial(effects.wait_for_effect_completion, timeout_seconds=0)):
                    accepted = await collect(receiver.confirm(owner=OWNER, session_id=SID, plan_id=plan_id, channel="web"))
                assert accepted[-1].type is E.TURN_COMPLETED and accepted[-1].payload["status"] == "waiting"
                with db.get_conn() as conn:
                    jobs = conn.execute("SELECT status FROM organize_operation_jobs").fetchall()
                assert sorted(row["status"] for row in jobs) == ["completed", "pending"]
                assert not (await store.load(owner=OWNER, session_id=SID)).pending_effect_plan_id
                assert not prior.done()
            finally:
                journal.release.set()
                await asyncio.wait_for(prior, 8)

    asyncio.run(run())
