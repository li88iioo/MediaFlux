"""取消审计只属于当前回合或它精确的用户取消检查点。"""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.requests import Request

from app.agent.kernel.events import AgentEventType as E, EventFactory
from app.agent.kernel.lifecycle import AgentSessionLifecycle
from app.agent.kernel.state_types import AgentInput, StalePublicationError
from app.agent.transports import WebKernelTransport
from app.routes import agent_api
from tests.test_agent_confirmation_edit import OWNER, SESSION
from tests.test_agent_confirmation_liveness import native_call, native_session
from tests.test_agent_confirmation_liveness import rig as rig  # noqa: PLC0414
from tests.test_agent_kernel_core import collect
from tests.test_agent_recovered_progress import reply
from tests.test_agent_tool_argument_ingress import PROTOCOLS, Wire
from tests.test_agent_tool_argument_ingress import offline as offline  # noqa: PLC0414
from tests.test_agent_tool_argument_ingress import store as store  # noqa: PLC0414


class HeldWire(Wire):
    def __init__(self, protocol, replies, *, kind, defer_cancel=False):
        super().__init__(protocol, replies)
        self.hold_index = int(kind == "confirmation")
        self.defer_cancel = defer_cancel
        self.ready, self.release = asyncio.Event(), asyncio.Event()
        self.producer = None

    async def handler(self, request):
        if len(self.requests) == self.hold_index:
            self.producer = asyncio.current_task()
            self.ready.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    if not self.defer_cancel:
                        raise
        return await super().handler(request)


async def held_turn(rig, protocol, kind, *, defer_cancel=False):
    replies = [reply(protocol, text="原回合的模型尾段。")]
    if kind == "confirmation":
        replies.insert(0, native_call(protocol, rig, "A"))
    wire = HeldWire(protocol, replies, kind=kind, defer_cancel=defer_cancel)
    source = native_session(rig, wire, journal=rig.store)
    request_id = "old-" + kind
    if kind == "confirmation":
        preview = await collect(source.run(AgentInput(owner=OWNER, session_id=SESSION,
            message="修改名称A并报告结果。", request_id="old-preview")))
        events = source.confirm(owner=OWNER, session_id=SESSION,
            plan_id=preview[-1].payload["plan_id"], request_id=request_id)
    else:
        events = source.run(AgentInput(owner=OWNER, session_id=SESSION,
            message="报告当前状态。", request_id=request_id))
    return source, wire, asyncio.create_task(collect(events)), request_id


def lifecycle(session):
    return AgentSessionLifecycle(session=session, store=session.state_store,
        effect_store=session.pipeline.effect_store,
        clear_legacy_plans=lambda **_: None)


async def fresh_turn(session, protocol):
    session.model = Wire(protocol, [reply(protocol, text="新请求已完成。")]).adapter
    session.journal = session.state_store
    events = await collect(session.run(AgentInput(owner=OWNER, session_id=SESSION,
        message="这是新的独立请求。", request_id="fresh-request")))
    assert events[-1].type is E.TURN_COMPLETED
    persisted = await session.state_store.list_events(owner=OWNER, session_id=SESSION)
    assert persisted[-1]["event_id"] == events[-1].event_id
    return events[-1], persisted


async def last_turn(session):
    runtime = SimpleNamespace(store=session.state_store, session=session, web=WebKernelTransport(session))
    request = Request({"type": "http", "method": "GET", "path": "/api/agent/sessions/" + SESSION, "headers": []})
    with patch.object(agent_api, "get_agent_kernel_runtime", return_value=runtime), patch.object(
        agent_api, "require_api_login", return_value=None), patch.object(agent_api, "_owner", return_value=OWNER), patch.object(
        agent_api, "_draft_scope", return_value="draft"):
        response = await agent_api.get_session(request, SESSION)
    return json.loads(bytes(response.body))["last_turn"]


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("cold", [False, True], ids=["warm", "cold"])
@pytest.mark.parametrize("operation", ["reset", "delete", "new-user"])
@pytest.mark.parametrize("kind", ["ordinary", "confirmation"])
def test_old_cancellation_cannot_replace_new_public_terminal(rig, protocol, cold, operation, kind):
    async def run():
        source, wire, task, _ = await held_turn(rig, protocol, kind, defer_cancel=True)
        try:
            await asyncio.wait_for(wire.ready.wait(), 5)
            assert len(rig.writes) == int(kind == "confirmation")
            receiver = native_session(rig, Wire(protocol, []), cold=True) if cold else source
            if operation != "new-user":
                await getattr(lifecycle(receiver), operation)(owner=OWNER, session_id=SESSION)
                assert await rig.store.list_events(owner=OWNER, session_id=SESSION) == []
            fresh, snapshot = await fresh_turn(receiver, protocol)
            assert not task.done(), "新终态必须先于旧执行器的取消收尾真实落盘"
        finally:
            wire.release.set()
        old = await asyncio.wait_for(task, 5)
        assert old[-1].type is E.TURN_CANCELLED
        assert await rig.store.list_events(owner=OWNER, session_id=SESSION) == snapshot
        assert not any(row["event_id"] == old[-1].event_id for row in snapshot)
        view = await last_turn(receiver)
        assert view["turn_id"] == fresh.turn_id and view["status"] == "completed"
        assert len(rig.writes) == int(kind == "confirmation")

    asyncio.run(run())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("kind", ["ordinary", "confirmation"])
@pytest.mark.parametrize("mode", ["complete", "user-cancel", "task-cancel", "other-reset"])
def test_current_completion_and_real_cancellation_keep_their_journal(rig, protocol, kind, mode):
    async def run():
        source, wire, task, request_id = await held_turn(rig, protocol, kind)
        try:
            await asyncio.wait_for(wire.ready.wait(), 5)
            before = await rig.store.load(owner=OWNER, session_id=SESSION)
            cold = native_session(rig, Wire(protocol, []), cold=True)
            if mode == "user-cancel":
                assert await cold.cancel(owner=OWNER, session_id=SESSION, request_id=request_id)
            elif mode == "task-cancel":
                assert wire.producer is not None
                wire.producer.cancel()
            elif mode == "other-reset":
                await lifecycle(cold).reset(owner=OWNER, session_id="independent-session")
        finally:
            wire.release.set()
        events = await asyncio.wait_for(task, 5)
        cancelled = mode in {"user-cancel", "task-cancel"}
        assert events[-1].type is (E.TURN_CANCELLED if cancelled else E.TURN_COMPLETED)
        persisted = await rig.store.list_events(owner=OWNER, session_id=SESSION)
        assert persisted[-1]["event_id"] == events[-1].event_id
        after = await rig.store.load(owner=OWNER, session_id=SESSION)
        assert after.generation == before.generation + int(mode == "user-cancel")
        assert len(rig.writes) == int(kind == "confirmation")

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["before-transaction", "after-transaction"])
@pytest.mark.parametrize("operation", ["reset", "delete", "new-user"])
@pytest.mark.parametrize("kind", ["ordinary", "confirmation"])
def test_cancel_journal_rechecks_authority_at_the_sqlite_transaction(rig, phase, operation, kind):
    async def run():
        source, wire, task, request_id = await held_turn(rig, "chat_completions", kind)
        entered, release = threading.Event(), threading.Event()
        append = rig.store._append_event_sync
        leases = []

        def gated_append(event, owner, lease=None):
            if event.type is not E.TURN_CANCELLED:
                return append(event, owner, lease)
            leases.append(lease)
            if phase == "after-transaction":
                append(event, owner, lease)
            entered.set()
            assert release.wait(5), "取消日志事务屏障未释放"
            if phase == "before-transaction":
                append(event, owner, lease)

        with patch.object(rig.store, "_append_event_sync", side_effect=gated_append):
            try:
                await asyncio.wait_for(wire.ready.wait(), 5)
                receiver = native_session(rig, Wire("chat_completions", []), cold=True)
                assert await receiver.cancel(owner=OWNER, session_id=SESSION, request_id=request_id)
                wire.release.set()
                assert await asyncio.to_thread(entered.wait, 5), "旧回合未进入取消日志边界"
                if operation != "new-user":
                    await getattr(lifecycle(receiver), operation)(owner=OWNER, session_id=SESSION)
                fresh, snapshot = await fresh_turn(receiver, "chat_completions")
            finally:
                wire.release.set()
                release.set()
            old = await asyncio.wait_for(task, 5)
        assert len(leases) == 1 and leases[0] is not None
        assert old[-1].type is E.TURN_CANCELLED
        assert await rig.store.list_events(owner=OWNER, session_id=SESSION) == snapshot
        assert (await last_turn(receiver))["turn_id"] == fresh.turn_id

    asyncio.run(run())


@pytest.mark.parametrize("event_type", [E.TURN_CANCELLED, E.TURN_COMPLETED, E.TURN_FAILED])
@pytest.mark.parametrize("mutation", ["none", "wrong-request", "wrong-turn", "new-turn", "reset", "delete"])
def test_cancel_checkpoint_grants_only_its_exact_cancel_event(store, event_type, mutation):
    async def run():
        lease, _ = await store.begin_turn(owner=OWNER, session_id=SESSION, request_id="original-request")
        assert await store.cancel_publication(owner=OWNER, session_id=SESSION, request_id=lease.request_id)
        attempt = lease
        if mutation == "wrong-request":
            attempt = replace(lease, request_id="wrong-request")
        elif mutation == "wrong-turn":
            attempt = replace(lease, turn_id="wrong-turn")
        elif mutation == "new-turn":
            await store.begin_turn(owner=OWNER, session_id=SESSION, request_id=lease.request_id)
        elif mutation == "reset":
            await store.reset_session(owner=OWNER, session_id=SESSION)
        elif mutation == "delete":
            await store.delete_session(owner=OWNER, session_id=SESSION)
        event = EventFactory(session_id=SESSION, turn_id=attempt.turn_id, request_id=attempt.request_id).create(event_type)
        before = await store.list_events(owner=OWNER, session_id=SESSION)
        if mutation == "none" and event_type is E.TURN_CANCELLED:
            await store.append_for_turn(event, attempt)
            assert (await store.list_events(owner=OWNER, session_id=SESSION))[-1]["event_id"] == event.event_id
        else:
            with pytest.raises(StalePublicationError):
                await store.append_for_turn(event, attempt)
            assert await store.list_events(owner=OWNER, session_id=SESSION) == before

    asyncio.run(run())


@pytest.mark.parametrize("cancelled", [False, True], ids=["current", "cancel-checkpoint"])
@pytest.mark.parametrize("field", ["session_id", "turn_id", "request_id"])
def test_fenced_journal_rejects_event_identity_different_from_lease(store, cancelled, field):
    async def run():
        lease, _ = await store.begin_turn(owner=OWNER, session_id=SESSION, request_id="original-request")
        if cancelled:
            assert await store.cancel_publication(owner=OWNER, session_id=SESSION, request_id=lease.request_id)
        event = EventFactory(session_id=SESSION, turn_id=lease.turn_id, request_id=lease.request_id).create(E.TURN_CANCELLED)
        event = replace(event,
            session_id="wrong-identity" if field == "session_id" else event.session_id,
            turn_id="wrong-identity" if field == "turn_id" else event.turn_id,
            request_id="wrong-identity" if field == "request_id" else event.request_id)
        with pytest.raises(StalePublicationError):
            await store.append_for_turn(event, lease)
        assert await store.list_events(owner=OWNER, session_id=SESSION) == []
        assert await store.list_events(owner=OWNER, session_id="wrong-identity") == []

    asyncio.run(run())
