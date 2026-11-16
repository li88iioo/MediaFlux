"""真实确认任务回执的生命周期、历史保留和发送互斥。"""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import threading
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app import database as db
from app.agent.kernel.bootstrap import build_agent_kernel_runtime
from app.agent.kernel.model import ModelEvent, ModelEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.state_types import AgentInput, SessionBusyError
from app.modules import telegram_notification_center as center
from app.notifier import TelegramSendResult
from tests.test_agent_continuation_delivery_handoff import OWNER, ROUTE, SID, _completed_effect
from tests.test_agent_continuation_delivery_identity import _process_drain
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414


def _rows():
    with db.get_conn() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM telegram_notification_outbox "
            "WHERE event_key LIKE 'tg:event:agent:agent-effect:%' ORDER BY id"
        )]


@contextmanager
def _transport(send=None):
    sent = []
    stopped = center._dispatch_stop.is_set()
    center._dispatch_stop.clear()

    def deliver(event, **kwargs):
        sent.append((event, kwargs))
        return send(event, **kwargs) if send else TelegramSendResult(ok=True, message_id=101)

    try:
        with patch.object(center, "wake_telegram_notification_dispatcher", return_value=False), patch.object(
            center, "_agent_interaction_enabled", return_value=True,
        ), patch.object(center, "send_event_result", side_effect=deliver):
            yield sent
    finally:
        if stopped:
            center._dispatch_stop.set()


class _AnswerModel:
    async def stream(self, request, *, cancellation):
        yield ModelEvent(ModelEventType.TEXT_DELTA, text="收到")
        yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")


def test_completed_effect_delivery_uses_stable_identity_and_frozen_route(agent_llm_enabled, tmp_path):
    async def run():
        with _transport() as sent:
            async with _completed_effect(tmp_path, [100]) as (store, follow, reads):
                state = await store.load(owner=OWNER, session_id=SID)
                row, = _rows()
                payload = json.loads(row["event_json"])
                identity = payload["_agent_effect_delivery"]
                assert identity == {
                    "version": 1, "owner": OWNER, "session_id": SID,
                    "delivery_id": next(iter(state.metadata["effect_waits"])),
                    "lifecycle_id": state.lifecycle_id, "delivery_route": ROUTE,
                }
                assert await asyncio.to_thread(center.drain_telegram_notifications, limit=1)
                assert len(sent) == 1
                event, options = sent[0]
                assert event.title == "已确认任务的后续结果"
                assert options == ROUTE
                assert _rows()[0]["status"] == "sent"
                assert reads == ["admitted.mp4"] and len(follow.requests) == 2
    asyncio.run(run())


def test_normal_history_retention_does_not_revoke_queued_effect_receipt(agent_llm_enabled, tmp_path):
    async def run():
        with _transport() as sent:
            async with _completed_effect(tmp_path, [100]) as (store, _, _):
                before = await store.load(owner=OWNER, session_id=SID)
                row, = _rows()
                receipt = json.loads(row["event_json"])["_agent_effect_delivery"]["delivery_id"]
                assert any(item.get("completion_receipt_id") == receipt for item in before.conversation)
                for index in range(28):
                    lease, state = await store.begin_turn(owner=OWNER, session_id=SID, request_id=f"retention-{index}")
                    await store.commit(lease, conversation=[*state.conversation,
                        {"role": "user", "content": f"普通问题{index}"},
                        {"role": "assistant", "content": "x" * 7900},
                    ])
                after = await store.load(owner=OWNER, session_id=SID)
                assert after.lifecycle_id == before.lifecycle_id
                assert not any(item.get("completion_receipt_id") == receipt for item in after.conversation)
                assert await asyncio.to_thread(center.drain_telegram_notifications, limit=1)
                assert len(sent) == 1 and _rows()[0]["status"] == "sent"
                assert _rows()[0]["event_key"] == row["event_key"]
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["reset", "delete"])
def test_successful_lifecycle_change_prevents_old_receipt_send(agent_llm_enabled, tmp_path, operation):
    async def run():
        with _transport() as sent:
            async with _completed_effect(tmp_path, [100]) as (store, _, _):
                before = await store.load(owner=OWNER, session_id=SID)
                runtime = build_agent_kernel_runtime(model=_AnswerModel(), store=store)
                await getattr(runtime.lifecycle, operation)(owner=OWNER, session_id=SID)
                # 同名会话真实重新创建也不能把旧交付绑定到新 lifecycle。
                events = [event async for event in runtime.session.run(AgentInput(
                    owner=OWNER, session_id=SID, request_id="after-lifecycle-change",
                    message="你好，请只回复收到", channel="telegram", delivery_route=ROUTE,
                ))]
                assert events[-1].payload.get("status") == "success"
                after = await store.load(owner=OWNER, session_id=SID)
                assert after.lifecycle_id != before.lifecycle_id
                await asyncio.to_thread(center.drain_telegram_notifications, limit=1)
                assert not sent
                row, = _rows()
                assert row["status"] == "suppressed" and row["last_error"]
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["reset", "delete"])
def test_session_mutation_cannot_complete_between_receipt_check_and_send(agent_llm_enabled, tmp_path, operation):
    async def run():
        entered, release = threading.Event(), threading.Event()

        def send(_event, **_kwargs):
            entered.set()
            assert release.wait(10), "test failed to release the simulated transport"
            return TelegramSendResult(ok=True, message_id=202)

        with _transport(send) as sent:
            async with _completed_effect(tmp_path, [100]) as (store, _, _):
                before = await store.load(owner=OWNER, session_id=SID)
                runtime = build_agent_kernel_runtime(model=_AnswerModel(), store=store)
                delivery = asyncio.create_task(asyncio.to_thread(center.drain_telegram_notifications, limit=1))
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    with pytest.raises(SessionBusyError):
                        await getattr(runtime.lifecycle, operation)(owner=OWNER, session_id=SID)
                    assert (await store.load(owner=OWNER, session_id=SID)).lifecycle_id == before.lifecycle_id
                finally:
                    release.set()
                    await delivery
                assert len(sent) == 1 and _rows()[0]["status"] == "sent"
                await getattr(runtime.lifecycle, operation)(owner=OWNER, session_id=SID)
                await asyncio.to_thread(center.drain_telegram_notifications, limit=1)
                assert len(sent) == 1
    asyncio.run(run())


def test_ordinary_new_turn_preserves_queued_completed_effect_receipt(agent_llm_enabled, tmp_path):
    async def run():
        with _transport() as sent:
            async with _completed_effect(tmp_path, [100]) as (store, _, _):
                before = await store.load(owner=OWNER, session_id=SID)
                runtime = build_agent_kernel_runtime(model=_AnswerModel(), store=store)
                events = [event async for event in runtime.session.run(AgentInput(
                    owner=OWNER, session_id=SID, request_id="new-ordinary-question",
                    message="你好，请只回复收到", channel="telegram", delivery_route=ROUTE,
                ))]
                assert events[-1].payload.get("status") == "success"
                after = await store.load(owner=OWNER, session_id=SID)
                assert after.generation > before.generation and after.lifecycle_id == before.lifecycle_id
                assert await asyncio.to_thread(center.drain_telegram_notifications, limit=1)
                assert len(sent) == 1 and _rows()[0]["status"] == "sent"
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["reset", "delete"])
@pytest.mark.parametrize("first", ["send", "lifecycle"])
def test_effect_delivery_and_lifecycle_share_lock_across_processes(agent_llm_enabled, tmp_path, operation, first):
    async def run():
        with _transport() as sent:
            async with _completed_effect(tmp_path, [100]) as (store, _, _):
                runtime = build_agent_kernel_runtime(model=_AnswerModel(), store=store)
                if first == "lifecycle":
                    await getattr(runtime.lifecycle, operation)(owner=OWNER, session_id=SID)
                context = multiprocessing.get_context("spawn")
                parent, child = context.Pipe()
                process = context.Process(target=_process_drain, args=(
                    str(db.resolve_db_path()), SQLiteKernelStore()._secret_provider(), child,
                ))
                process.start()
                child.close()
                try:
                    assert await asyncio.to_thread(parent.poll, 15)
                    stage = parent.recv()
                    if first == "send":
                        assert stage == ("sending", True)
                        with pytest.raises(SessionBusyError):
                            await getattr(runtime.lifecycle, operation)(owner=OWNER, session_id=SID)
                        await runtime.lifecycle.reset(owner=OWNER, session_id="independent-effect-session")
                        parent.send("finish")
                        assert await asyncio.to_thread(parent.poll, 15)
                        assert parent.recv() == ("drained", True)
                        await getattr(runtime.lifecycle, operation)(owner=OWNER, session_id=SID)
                        assert _rows()[0]["status"] == "sent"
                    else:
                        assert stage == ("drained", True)  # 普通通知的抑制是已完成处理。
                        assert _rows()[0]["status"] == "suppressed"
                    await asyncio.to_thread(process.join, 15)
                    assert not process.is_alive() and process.exitcode == 0
                finally:
                    if process.is_alive():
                        process.terminate()
                        await asyncio.to_thread(process.join, 10)
                    parent.close()
                assert not sent, "发送必须由持有真实会话锁的子进程完成"
    asyncio.run(run())
