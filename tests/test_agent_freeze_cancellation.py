"""预览取消必须等签发和精确撤销线程结束后才释放会话。"""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import patch

import pytest

from app.agent.kernel.events import AgentEventType as E
from app.agent.kernel.lifecycle import AgentSessionLifecycle
from app.agent.kernel.state_types import AgentInput, SessionBusyError, StalePublicationError
from tests.test_agent_confirmation_edit import OWNER, SESSION
from tests.test_agent_confirmation_liveness import native_call, native_session
from tests.test_agent_confirmation_liveness import rig as rig  # noqa: PLC0414
from tests.test_agent_kernel_core import collect
from tests.test_agent_recovered_progress import reply
from tests.test_agent_tool_argument_ingress import PROTOCOLS, Wire
from tests.test_agent_tool_argument_ingress import offline as offline  # noqa: PLC0414
from tests.test_agent_tool_argument_ingress import store as store  # noqa: PLC0414


class TrackedWire(Wire):
    async def handler(self, request):
        self.producer = asyncio.current_task()
        return await super().handler(request)


def start_preview(rig, protocol):
    wire = TrackedWire(protocol, [native_call(protocol, rig, "A"), reply(protocol, text="已记录预检失败。")])
    source = native_session(rig, wire, journal=rig.store)
    task = asyncio.create_task(collect(source.run(AgentInput(owner=OWNER, session_id=SESSION,
        message="修改名称A。", request_id="old-freeze", channel="telegram"))))
    return source, wire, task


async def cancellation_delivered():
    delivered = asyncio.Event()
    asyncio.get_running_loop().call_soon(delivered.set)
    await delivered.wait()


async def assert_peer_busy(peer):
    with pytest.raises(SessionBusyError):
        await peer.state_store.begin_turn(owner=OWNER, session_id=SESSION, request_id="blocked-peer")
    lifecycle = AgentSessionLifecycle(session=peer, store=peer.state_store,
        effect_store=peer.pipeline.effect_store,
        clear_legacy_plans=lambda **_: None)
    with pytest.raises(SessionBusyError):
        await lifecycle.reset(owner=OWNER, session_id=SESSION)
    queried = await collect(peer.run(AgentInput(owner=OWNER, session_id=SESSION,
        message="修改名称B。", request_id="blocked-query")))
    assert queried[-1].payload["code"] == "effect_in_progress"
    confirmed = await collect(peer.confirm(owner=OWNER, session_id=SESSION,
        plan_id="not-issued-yet", request_id="blocked-confirm"))
    assert confirmed[-1].payload["code"] == "effect_in_progress"


async def finish(task):
    return (await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5))[0]


async def confirm_new_b(rig, peer, protocol):
    peer.model = Wire(protocol, [native_call(protocol, rig, "B")]).adapter
    preview = await collect(peer.run(AgentInput(owner=OWNER, session_id=SESSION,
        message="修改名称B。", request_id="new-preview")))
    assert preview[-1].payload["status"] == "approval_required"
    plan_id = preview[-1].payload["plan_id"]
    pending = await peer.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION)
    assert pending is not None and pending.plan_id == plan_id and pending.arguments["label"] == "B"
    peer.model = Wire(protocol, [reply(protocol, text="名称B已修改。")]).adapter
    confirmed = await collect(peer.confirm(owner=OWNER, session_id=SESSION,
        plan_id=plan_id, request_id="new-confirm"))
    assert confirmed[-1].type is E.TURN_COMPLETED
    assert any(event.type is E.EFFECT_COMPLETED for event in confirmed)
    assert [row["label"] for row in rig.writes] == ["B"]


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("mode", ["complete", "consumer", "producer", "repeated"])
def test_cancelled_freeze_is_drained_and_cannot_revoke_the_next_card(rig, protocol, mode):
    async def run():
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        freeze, cancel = rig.pipeline.effect_store.freeze, rig.pipeline.effect_store.cancel
        plans, cancellations = [], []

        def gated_freeze(**kwargs):
            entered.set()
            try:
                assert release.wait(5), "签发线程未释放"
                plan = freeze(**kwargs)
                plans.append(plan)
                return plan
            finally:
                done.set()

        def observed_cancel(**kwargs):
            cancellations.append(kwargs["plan_id"])
            return cancel(**kwargs)

        source, wire, task = start_preview(rig, protocol)
        peer = native_session(rig, Wire(protocol, []), cold=True)
        with patch.object(rig.pipeline.effect_store, "freeze", side_effect=gated_freeze), patch.object(
            rig.pipeline.effect_store, "cancel", side_effect=observed_cancel), patch.object(
            source.pipeline.effect_lifecycle, "cancelled") as cancelled_hook, patch.object(
            source.pipeline.effect_lifecycle, "prepare_failed") as failed_hook:
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                producer = wire.producer
                assert producer is not None
                if mode in {"consumer", "repeated"}:
                    task.cancel()
                elif mode == "producer":
                    producer.cancel()
                await cancellation_delivered()
                if mode == "repeated":
                    for _ in range(2):
                        producer.cancel()
                        await cancellation_delivered()
                assert not producer.done() and not task.done()
                await assert_peer_busy(peer)
            finally:
                release.set()
                result = await finish(task)
                assert await asyncio.to_thread(done.wait, 5)
            assert len(plans) == 1
            assert cancellations == ([] if mode == "complete" else [plans[0].plan_id])
            assert failed_hook.call_count == 0
            assert cancelled_hook.call_count == int(mode != "complete")
        if mode == "complete":
            assert not isinstance(result, BaseException)
            assert result[-1].payload["plan_id"] == plans[0].plan_id
        else:
            if mode in {"consumer", "repeated"}:
                assert isinstance(result, asyncio.CancelledError)
            else:
                assert not isinstance(result, BaseException)
                assert result[-1].type is E.TURN_CANCELLED
            assert await peer.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION) is None
            assert rig.pipeline.effect_store.get_active_plan(owner=OWNER, session_id=SESSION,
                generation=plans[0].generation, plan_id=plans[0].plan_id) is None
        assert rig.writes == []
        await confirm_new_b(rig, peer, protocol)
        stale = await collect(peer.confirm(owner=OWNER, session_id=SESSION,
            plan_id=plans[0].plan_id, request_id="stale-confirm"))
        assert stale[-1].payload["code"] == "confirmation_invalid"
        assert [row["label"] for row in rig.writes] == ["B"]

    asyncio.run(run())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("failure", ["runtime-error", "worker-cancel"])
@pytest.mark.parametrize("cancel_producer", [False, True], ids=["worker-only", "also-task-cancel"])
def test_failed_freeze_worker_is_not_treated_as_a_created_plan(rig, protocol, failure, cancel_producer):
    async def run():
        entered, release = threading.Event(), threading.Event()

        def failed_freeze(**_kwargs):
            entered.set()
            assert release.wait(5), "故障线程未释放"
            if failure == "worker-cancel":
                raise asyncio.CancelledError("freeze worker cancelled itself")
            raise RuntimeError("freeze worker failed before issuing a ticket")

        source, wire, task = start_preview(rig, protocol)
        peer = native_session(rig, Wire(protocol, []), cold=True)
        with patch.object(rig.pipeline.effect_store, "freeze", side_effect=failed_freeze), patch.object(
            rig.pipeline.effect_store, "cancel", wraps=rig.pipeline.effect_store.cancel) as cancel, patch.object(
            source.pipeline.effect_lifecycle, "cancelled") as cancelled_hook, patch.object(
            source.pipeline.effect_lifecycle, "prepare_failed") as failed_hook:
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                if cancel_producer:
                    assert wire.producer is not None
                    wire.producer.cancel()
                    await cancellation_delivered()
                    wire.producer.cancel()
                    await cancellation_delivered()
                assert not task.done()
                await assert_peer_busy(peer)
            finally:
                release.set()
                result = await finish(task)
            assert cancel.call_count == cancelled_hook.call_count == 0
            assert failed_hook.call_count == 1
        assert not isinstance(result, BaseException)
        assert not any(event.type in {E.EFFECT_APPROVAL_REQUIRED, E.EFFECT_COMPLETED} for event in result)
        if failure == "worker-cancel":
            assert result[-1].type is E.TURN_CANCELLED
        assert await peer.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION) is None
        assert rig.writes == []
        await confirm_new_b(rig, peer, protocol)

    asyncio.run(run())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("origin", ["freeze-cancel", "pending-commit-failure"])
def test_exact_ticket_discard_is_drained_before_unlocking_even_on_repeated_cancel(rig, protocol, origin):
    async def run():
        freeze_entered, freeze_release = threading.Event(), threading.Event()
        cancel_entered, cancel_release, cancel_done = threading.Event(), threading.Event(), threading.Event()
        freeze, cancel = rig.pipeline.effect_store.freeze, rig.pipeline.effect_store.cancel
        commit = rig.store.commit
        plans, cancellations = [], []

        def gated_freeze(**kwargs):
            freeze_entered.set()
            assert freeze_release.wait(5), "清理用例签发线程未释放"
            plan = freeze(**kwargs)
            plans.append(plan)
            return plan

        def gated_cancel(**kwargs):
            cancellations.append(kwargs["plan_id"])
            cancel_entered.set()
            try:
                assert cancel_release.wait(5), "精确撤销线程未释放"
                return cancel(**kwargs)
            finally:
                cancel_done.set()

        async def failing_commit(lease, *, updates=(), **kwargs):
            if origin == "pending-commit-failure" and any(update.key == "pending_effect_plan_id" for update in updates):
                raise StalePublicationError("pending publication failed before commit")
            return await commit(lease, updates=updates, **kwargs)

        source, wire, task = start_preview(rig, protocol)
        peer = native_session(rig, Wire(protocol, []), cold=True)
        with patch.object(rig.pipeline.effect_store, "freeze", side_effect=gated_freeze), patch.object(
            rig.pipeline.effect_store, "cancel", side_effect=gated_cancel), patch.object(
            rig.store, "commit", side_effect=failing_commit), patch.object(
            source.pipeline.effect_lifecycle, "cancelled") as cancelled_hook, patch.object(
            source.pipeline.effect_lifecycle, "prepare_failed") as failed_hook:
            try:
                assert await asyncio.to_thread(freeze_entered.wait, 5)
                producer = wire.producer
                assert producer is not None
                if origin == "freeze-cancel":
                    producer.cancel()
                    await cancellation_delivered()
                freeze_release.set()
                assert await asyncio.to_thread(cancel_entered.wait, 5), "未撤销实际冻结结果"
                for _ in range(3):
                    producer.cancel()
                    await cancellation_delivered()
                assert not producer.done() and not task.done()
                await assert_peer_busy(peer)
            finally:
                freeze_release.set()
                cancel_release.set()
                result = await finish(task)
            assert await asyncio.to_thread(cancel_done.wait, 5)
            assert len(plans) == 1 and cancellations == [plans[0].plan_id]
            assert cancelled_hook.call_count == 1 and failed_hook.call_count == 0
        assert not isinstance(result, BaseException)
        assert result[-1].type is E.TURN_CANCELLED
        assert rig.pipeline.effect_store.get_active_plan(owner=OWNER, session_id=SESSION,
            generation=plans[0].generation, plan_id=plans[0].plan_id) is None
        assert await peer.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION) is None
        assert rig.writes == []
        await confirm_new_b(rig, peer, protocol)

    asyncio.run(run())
