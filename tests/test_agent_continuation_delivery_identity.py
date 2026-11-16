"""签名生命周期、旧 outbox 归一及发送边界；仅隔离外部 Bot I/O。"""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import sqlite3
from copy import deepcopy
from unittest.mock import patch

import pytest

from app import config, database as db
from app.agent.kernel.events import AgentEventType, EventFactory
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.session_guard import session_scope_guard
from app.agent.kernel.session_state import SessionState
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import SessionBusyError, StateUpdate
from app.bot import agent_adapter as adapter
from app.modules import telegram_notification_center as center
from app.repositories import telegram_notifications as notifications
from tests.test_agent_continuation_delivery_lifecycle import (
    _enqueue,
    _rows,
    bot as bot,
    isolated_database as isolated_database,
    tg_config as tg_config,
)
from tests.test_agent_continuation_telegram import NativeBotFixture
from tests.test_agent_waiting_delivery import OWNER, ROUTE, SID
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414

pytestmark = pytest.mark.usefixtures("tg_config")


def _signed_payload(store, state, payload):
    owner_digest, session_digest = store._scope(state.owner, state.session_id)
    encoded, signature = store._encode(
        payload, domain=f"state:v1:{owner_digest}:{session_digest}:{state.generation}".encode(),
        maximum=store.max_state_bytes,
    )
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE agent_kernel_sessions SET state_json=?,state_hmac=? WHERE owner_digest=? AND session_digest=?",
            (encoded, signature, owner_digest, session_digest),
        )


def _change_parts(change):
    with db.get_conn() as conn:
        for row in _rows():
            stored = json.loads(row["event_json"])
            change(stored["_agent_continuation_delivery"])
            conn.execute("UPDATE telegram_notification_outbox SET event_json=? WHERE id=?", (json.dumps(stored), row["id"]))


def _legacy_parts(part):
    for key in ("version", "delivery_id", "lifecycle_id"):
        part.pop(key)


def _make_due():
    with db.get_conn() as conn:
        conn.execute("UPDATE telegram_notification_outbox SET next_attempt_at='2000-01-01 00:00:00' WHERE status='retry_wait'")


@pytest.mark.parametrize("storage", ["memory", "sqlite"])
def test_absent_session_reads_are_stable_and_actual_creation_gets_a_new_identity(storage):
    async def exercise():
        store = InMemorySessionStateStore() if storage == "memory" else SQLiteKernelStore()
        first = await store.load(owner=OWNER, session_id=SID)
        cold = SQLiteKernelStore() if storage == "sqlite" else store
        assert await cold.load(owner=OWNER, session_id=SID) == first
        assert first.lifecycle_id == "", "未创建的空读取不应分配活跃生命周期"
        if isinstance(store, SQLiteKernelStore):
            assert store.load_existing_session(owner=OWNER, session_id=SID) is None
        _, created = await store.begin_turn(owner=OWNER, session_id=SID, request_id="create")
        assert created.lifecycle_id and created.lifecycle_id != first.lifecycle_id
        assert (await cold.load(owner=OWNER, session_id=SID)).lifecycle_id == created.lifecycle_id
        if isinstance(store, SQLiteKernelStore):
            assert await store.delete_session(owner=OWNER, session_id=SID)
            assert await cold.load(owner=OWNER, session_id=SID) == first
            _, recreated = await cold.begin_turn(owner=OWNER, session_id=SID, request_id="recreate")
            assert recreated.lifecycle_id and recreated.lifecycle_id != created.lifecycle_id

    asyncio.run(exercise())


@pytest.mark.parametrize("storage", ["memory", "sqlite"])
def test_lifecycle_identity_survives_turns_and_cannot_be_reassigned_by_tool_updates(storage):
    async def exercise():
        store = InMemorySessionStateStore() if storage == "memory" else SQLiteKernelStore()
        lease, first = await store.begin_turn(owner=OWNER, session_id=SID, request_id="one")
        assert first.lifecycle_id and first.clone().lifecycle_id == first.lifecycle_id
        with pytest.raises(ValueError, match="lifecycle"):
            await store.commit(lease, updates=[StateUpdate("lifecycle_id", "forged")])

        def change(state):
            state.lifecycle_id = "forged"
            state.metadata["fixture_receipt"] = True

        await store.update_effect_state(owner=OWNER, session_id=SID, change=change)
        _, second = await store.begin_turn(owner=OWNER, session_id=SID, request_id="two")
        assert second.generation > first.generation and second.lifecycle_id == first.lifecycle_id
        assert second.metadata["fixture_receipt"] is True
        assert second.to_payload()["lifecycle_id"] == first.lifecycle_id

    asyncio.run(exercise())


@pytest.mark.parametrize("write", ["commit", "display"])
def test_legacy_signed_session_has_stable_identity_and_first_write_persists_it(write):
    async def exercise():
        store = SQLiteKernelStore()
        _, initial = await store.begin_turn(owner=OWNER, session_id=SID, request_id="legacy")
        payload = initial.to_payload()
        payload.pop("lifecycle_id")
        _signed_payload(store, initial, payload)
        first = await SQLiteKernelStore().load(owner=OWNER, session_id=SID)
        second = await SQLiteKernelStore().load(owner=OWNER, session_id=SID)
        assert first.lifecycle_id.startswith("legacy:") and first.lifecycle_id == second.lifecycle_id
        assert SessionState.from_payload(owner=OWNER, session_id=SID, generation=999, payload=payload).lifecycle_id == first.lifecycle_id
        assert SessionState.from_payload(owner=OWNER, session_id="another", generation=first.generation, payload=payload).lifecycle_id != first.lifecycle_id
        if write == "display":
            await store.patch_session_display(owner=OWNER, session_id=SID, patch={"title": "保留身份"})
        else:
            lease, _ = await store.begin_turn(owner=OWNER, session_id=SID, request_id="persist")
            await store.commit(lease, updates=[StateUpdate("summary", "已保存")])
        with db.get_conn() as conn:
            saved = json.loads(conn.execute("SELECT state_json FROM agent_kernel_sessions").fetchone()[0])
        assert saved["lifecycle_id"] == first.lifecycle_id
        reset = await store.reset_session(owner=OWNER, session_id=SID)
        assert reset.lifecycle_id != first.lifecycle_id and not reset.lifecycle_id.startswith("legacy:")
        assert await store.delete_session(owner=OWNER, session_id=SID)
        assert store.load_existing_session(owner=OWNER, session_id=SID) is None
        _, recreated = await store.begin_turn(owner=OWNER, session_id=SID, request_id="recreated")
        assert recreated.lifecycle_id not in {first.lifecycle_id, reset.lifecycle_id}

    asyncio.run(exercise())


@pytest.mark.parametrize("identity", [None, "", "   ", 5])
def test_present_invalid_signed_lifecycle_is_rejected_instead_of_legacy_fallback(identity):
    async def exercise():
        store = SQLiteKernelStore()
        _, state = await store.begin_turn(owner=OWNER, session_id=SID, request_id="invalid")
        payload = state.to_payload()
        payload["lifecycle_id"] = identity
        _signed_payload(store, state, payload)
        with pytest.raises(ValueError, match="lifecycle"):
            store.load_existing_session(owner=OWNER, session_id=SID)

    asyncio.run(exercise())


@pytest.mark.parametrize("field", ["delivery_id", "lifecycle_id", "version"])
def test_new_unbound_identity_suppresses_entire_group_without_legacy_fallback(agent_llm_enabled, bot, field):
    async def exercise():
        await _enqueue([100], "分段核验事实。" * 1800)
        before = _rows()
        assert len(before) >= 3
        stored = json.loads(before[0]["event_json"])
        stored["_agent_continuation_delivery"].pop(field)
        with db.get_conn() as conn:
            conn.execute("UPDATE telegram_notification_outbox SET event_json=? WHERE id=?", (json.dumps(stored), before[0]["id"]))
        assert not await asyncio.to_thread(center.drain_telegram_notifications, limit=16)
        assert not bot.attempts
        assert all(row["status"] == "suppressed" and row["last_error"] for row in _rows())

    asyncio.run(exercise())


def test_tampered_signed_lifecycle_suppresses_every_unsent_part(agent_llm_enabled, bot):
    async def exercise():
        await _enqueue([100], "签名边界核验事实。" * 1400)
        with db.get_conn() as conn:
            conn.execute("UPDATE agent_kernel_sessions SET state_json=json_set(state_json,'$.lifecycle_id','forged')")
        await asyncio.to_thread(center.drain_telegram_notifications, limit=16)
        assert not bot.attempts
        assert all(row["status"] == "suppressed" and row["last_error"] == "AgentContinuationSessionInvalid" for row in _rows())

    asyncio.run(exercise())


def test_temporary_signed_read_failure_retries_and_does_not_starve_other_group(agent_llm_enabled, bot):
    async def exercise():
        await _enqueue([100], "第一份事实。")
        with patch("tests.test_agent_waiting_delivery.SID", "independent-delivery"):
            await _enqueue([100], "第二份事实。")
        original = SQLiteKernelStore.load_existing_session
        calls = []

        def read(store, **scope):
            calls.append(scope)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is temporarily busy")
            return original(store, **scope)

        with patch.object(SQLiteKernelStore, "load_existing_session", read):
            assert not await asyncio.to_thread(center.drain_telegram_notifications, limit=16)
        assert [row["status"] for row in _rows()] == ["retry_wait", "sent"]
        assert _rows()[0]["attempts"] == 1 and "OperationalError" in _rows()[0]["last_error"]
        assert len(bot.sent) == 1 and "第二份事实" in bot.sent[0][1]
        _make_due()
        assert await asyncio.to_thread(center.drain_telegram_notifications, limit=16)
        assert [row["status"] for row in _rows()] == ["sent", "sent"]
        assert len(bot.sent) == len(bot.attempts) == 2

    asyncio.run(exercise())


def test_legacy_group_is_bound_from_retained_signed_source_and_preserves_sent_prefix(agent_llm_enabled, bot):
    async def exercise():
        runtime, _, _ = await _enqueue([100], "旧格式中的完整媒体事实。" * 1400)
        assert await asyncio.to_thread(center.drain_telegram_notifications, limit=1)
        first = _rows()[0]
        state = await runtime.store.load(owner=OWNER, session_id=SID)
        assert state.metadata["read_wait"]["state"] == "terminal"
        assert not state.metadata.get("continuation_deliveries")
        expected_id = state.metadata["read_wait"]["wait_id"] + ":" + state.metadata["read_wait"]["resume_turn_id"]
        payload = state.to_payload()
        payload.pop("lifecycle_id")
        _signed_payload(runtime.store, state, payload)
        state = await SQLiteKernelStore().load(owner=OWNER, session_id=SID)
        assert state.lifecycle_id.startswith("legacy:")
        _change_parts(_legacy_parts)
        keys = [row["event_key"] for row in _rows()]
        preserved = []
        original = center.bind_agent_continuation_group

        def bind(*args, **kwargs):
            before = _rows()
            result = original(*args, **kwargs)
            after = _rows()
            preserved.append([{k: v for k, v in row.items() if k != "event_json"} for row in before]
                             == [{k: v for k, v in row.items() if k != "event_json"} for row in after])
            return result

        with patch.object(center, "bind_agent_continuation_group", side_effect=bind):
            assert await asyncio.to_thread(center.drain_telegram_notifications, limit=16)
        rows = _rows()
        assert preserved == [True] and [row["event_key"] for row in rows] == keys
        assert all(row["status"] == "sent" for row in rows)
        assert rows[0]["message_id"] == first["message_id"] and rows[0]["attempts"] == first["attempts"]
        assert len(bot.sent) == len(rows)
        for row in rows:
            part = json.loads(row["event_json"])["_agent_continuation_delivery"]
            assert (part["version"], part["delivery_id"], part["lifecycle_id"]) == (1, expected_id, state.lifecycle_id)
        before = deepcopy(rows)
        assert not await asyncio.to_thread(center.drain_telegram_notifications, limit=16)
        assert _rows() == before

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["temporary_db", "snapshot_changed"])
def test_legacy_normalization_not_completed_retries_without_blocking_other_groups(agent_llm_enabled, bot, failure):
    async def exercise():
        await _enqueue([100], "待归一的真实事实。")
        _change_parts(_legacy_parts)
        with patch("tests.test_agent_waiting_delivery.SID", "independent-delivery"):
            await _enqueue([100], "无需迁移的新事实。")
        original = center.bind_agent_continuation_group
        calls = []

        def bind(*args, **kwargs):
            calls.append(True)
            if failure == "temporary_db":
                raise sqlite3.OperationalError("temporary write failure")
            # 模拟原 payload 的后续 part 在 snapshot 读取后才入队；当前 lease 仍合法。
            old = _rows()[0]
            group, _, chat = old["event_key"].rsplit(":", 2)
            notifications.upsert_notification(
                group + ":1:" + chat, topic=old["topic"], importance=old["importance"],
                chat_id=old["chat_id"], event_json=old["event_json"],
            )
            return original(*args, **kwargs)

        with patch.object(center, "bind_agent_continuation_group", side_effect=bind):
            assert not await asyncio.to_thread(center.drain_telegram_notifications, limit=16)
        assert calls == [True]
        assert _rows()[0]["status"] == "retry_wait" and _rows()[0]["attempts"] == 1
        assert "version" not in json.loads(_rows()[0]["event_json"])["_agent_continuation_delivery"]
        assert len(bot.sent) == 1 and "无需迁移的新事实" in bot.sent[0][1]
        _make_due()
        assert await asyncio.to_thread(center.drain_telegram_notifications, limit=16)
        assert all(row["status"] == "sent" for row in _rows())
        assert len(bot.sent) == (2 if failure == "temporary_db" else 3)

    asyncio.run(exercise())


def test_actual_session_lock_busy_is_retryable_before_any_bot_call(agent_llm_enabled, bot):
    async def exercise():
        await _enqueue([100], "被同会话操作暂时阻挡的事实。")
        with patch("tests.test_agent_waiting_delivery.SID", "independent-delivery"):
            await _enqueue([100], "其它会话仍可发送。")
        with session_scope_guard(OWNER, SID):
            # 不传播当前 scope ContextVar，真实走工作线程的 OS 锁互斥。
            assert not await asyncio.get_running_loop().run_in_executor(None, center.drain_telegram_notifications)
        assert [row["status"] for row in _rows()] == ["retry_wait", "sent"]
        assert _rows()[0]["last_error"] == "SessionBusyError"
        assert len(bot.sent) == 1 and "其它会话仍可发送" in bot.sent[0][1]
        _make_due()
        assert await asyncio.to_thread(center.drain_telegram_notifications, limit=16)
        assert [row["status"] for row in _rows()] == ["sent", "sent"]
        assert len(bot.sent) == 2

    asyncio.run(exercise())


def test_legacy_group_can_bind_from_canonical_delivery_after_original_task_is_replaced(agent_llm_enabled, bot):
    from tests.test_agent_continuation_delivery_handoff import _completed_read, _handoff, _next_task

    async def exercise():
        clock = [100]
        runtime, _, _, _ = await _completed_read(clock)
        delivery, = await runtime.store.due_continuations()
        with patch.object(runtime.store, "update_effect_state", side_effect=RuntimeError("ACK interrupted")):
            with pytest.raises(RuntimeError, match="ACK interrupted"):
                await _handoff(runtime.store, delivery)
        _change_parts(_legacy_parts)
        newer = await _next_task(clock)
        assert delivery["delivery_id"] in newer.metadata["continuation_deliveries"]
        assert newer.metadata["read_wait"]["wait_id"] != delivery["delivery_id"].split(":")[0]
        assert await asyncio.to_thread(center.drain_telegram_notifications, limit=16)
        assert len(bot.sent) == 1 and _rows()[0]["status"] == "sent"
        saved = json.loads(_rows()[0]["event_json"])["_agent_continuation_delivery"]
        assert saved["delivery_id"] == delivery["delivery_id"] and saved["lifecycle_id"] == newer.lifecycle_id

    asyncio.run(exercise())


def test_real_effect_legacy_group_binds_from_signed_sealed_origin(agent_llm_enabled, tmp_path, bot):
    from tests.test_agent_continuation_delivery_handoff import _completed_effect, _handoff

    async def exercise():
        async with _completed_effect(tmp_path, [100]) as (store, script, reads):
            pending, = await store.due_continuations()
            assert await _handoff(store, pending)
            state = await SQLiteKernelStore().load(owner=OWNER, session_id=SID)
            assert not state.metadata.get("continuation_deliveries")
            effect, = state.metadata["effect_waits"].values()
            record = effect["continuation"]
            assert record["state"] == "terminal" and effect["sealed"] and not record.get("delivery_route")
            delivery_id = record["wait_id"] + ":" + record["resume_turn_id"]
            assert delivery_id == pending["delivery_id"]
            _change_parts(_legacy_parts)
            before = _rows()
            assert len(before) == 1 and before[0]["status"] == "pending"
            assert await asyncio.to_thread(center.drain_telegram_notifications, limit=16, event_key=before[0]["event_key"])
            after = _rows()
            assert [row["event_key"] for row in after] == [row["event_key"] for row in before]
            assert after[0]["status"] == "sent" and len(bot.sent) == len(bot.attempts) == 1
            part = json.loads(after[0]["event_json"])["_agent_continuation_delivery"]
            assert (part["version"], part["delivery_id"], part["lifecycle_id"]) == (1, delivery_id, state.lifecycle_id)
            assert reads == ["admitted.mp4"] and len(script.requests) == 2

    asyncio.run(exercise())


def test_legacy_group_without_retained_source_is_isolated_without_rebinding(agent_llm_enabled, bot):
    from tests.test_agent_continuation_delivery_handoff import _completed_read, _handoff, _next_task

    async def exercise():
        clock = [100]
        runtime, state, _, _ = await _completed_read(clock, answer="旧身份来源可能被消费。" * 1400)
        delivery, = await runtime.store.due_continuations()
        assert await _handoff(runtime.store, delivery)
        _change_parts(_legacy_parts)
        newer = await _next_task(clock)
        assert newer.lifecycle_id == state.lifecycle_id
        assert newer.metadata["read_wait"]["wait_id"] != state.metadata["read_wait"]["wait_id"]
        await asyncio.to_thread(center.drain_telegram_notifications, limit=16)
        assert not bot.attempts
        assert all(row["status"] == "suppressed" and row["last_error"] == "AgentContinuationLegacyIdentityUnavailable" for row in _rows())

    asyncio.run(exercise())


@pytest.mark.parametrize("changed", ["lease", "revision"])
def test_stale_claim_cannot_suppress_or_rebind_group(agent_llm_enabled, changed, bot):
    async def exercise():
        await _enqueue([100], "并发分段事实。" * 1800)
        claim, = notifications.claim_due_notifications(limit=1)
        before = _rows()
        with db.get_conn() as conn:
            column = "lease_generation" if changed == "lease" else "revision"
            conn.execute(f"UPDATE telegram_notification_outbox SET {column}={column}+1 WHERE id=?", (claim["id"],))
        snapshot = _rows()
        kwargs = {"lease_generation": claim["lease_generation"], "claimed_revision": claim["revision"]}
        assert not notifications.suppress_agent_continuation_group(claim["id"], **kwargs, reason="stale")
        parts = {row["id"]: (row["revision"], row["event_json"], "{}") for row in before}
        assert notifications.bind_agent_continuation_group(claim["id"], **kwargs, parts=parts) is None
        assert _rows() == snapshot and not bot.attempts

    asyncio.run(exercise())


def test_group_suppression_preserves_unknown_sent_and_other_sender(agent_llm_enabled, bot):
    async def exercise():
        await _enqueue([100], "独立回执和结果未知都要保留。" * 1800)
        rows = _rows()
        assert len(rows) >= 5
        with db.get_conn() as conn:
            conn.execute("UPDATE telegram_notification_outbox SET status='sent',message_id=321 WHERE id=?", (rows[0]["id"],))
        claim, = notifications.claim_due_notifications(limit=1)
        with db.get_conn() as conn:
            conn.execute("UPDATE telegram_notification_outbox SET status='outcome_unknown',last_error='unknown' WHERE id=?", (rows[2]["id"],))
            conn.execute("UPDATE telegram_notification_outbox SET status='sending',lease_generation=8 WHERE id=?", (rows[3]["id"],))
        assert notifications.suppress_agent_continuation_group(
            claim["id"], lease_generation=claim["lease_generation"], claimed_revision=claim["revision"], reason="reset",
        )
        after = _rows()
        assert after[0]["status"] == "sent" and after[0]["message_id"] == 321
        assert after[1]["status"] == "suppressed"
        assert after[2]["status"] == "outcome_unknown" and after[2]["last_error"] == "unknown"
        assert after[3]["status"] == "sending" and after[3]["lease_generation"] == 8
        assert all(row["status"] == "suppressed" for row in after[4:]) and not bot.attempts

    asyncio.run(exercise())


def _process_drain(db_path, secret, pipe):
    """真实子进程执行 outbox；只替换 Telegram I/O 与测试配置来源。"""
    db.configure_database(db_path, test_mode=True)
    values = {"WEB_SECRET_KEY": secret, "TG_AGENT_ENABLED": "1", "TG_CHAT_ID": ROUTE["chat_id"],
              "TG_AGENT_ALLOWED_USER_IDS": "7", "AGENT_ENABLED": "1"}

    class BlockingBot(NativeBotFixture):
        def send_message(self, *args, **kwargs):
            pipe.send(("sending", True))
            assert pipe.recv() == "finish"
            return super().send_message(*args, **kwargs)

    try:
        with patch.object(config, "get", side_effect=lambda key, default="": values.get(key, default)), patch.object(
            adapter, "is_agent_enabled", return_value=True,
        ), patch.object(center, "is_agent_enabled", return_value=True), patch("app.notifier.get_bot", return_value=BlockingBot()):
            center._dispatch_stop.clear()
            pipe.send(("drained", center.drain_telegram_notifications(limit=16)))
    finally:
        pipe.close()


@pytest.mark.parametrize("operation", ["reset", "delete"])
@pytest.mark.parametrize("first", ["send", "lifecycle"])
def test_cross_process_actual_send_and_lifecycle_have_one_lock_boundary(agent_llm_enabled, bot, operation, first):
    async def exercise():
        runtime, _, _ = await _enqueue([100])
        if first == "lifecycle":
            await getattr(runtime.lifecycle, operation)(owner=OWNER, session_id=SID)
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=_process_drain, args=(str(db.DB_PATH), SQLiteKernelStore()._secret_provider(), child))
        process.start()
        child.close()
        try:
            assert await asyncio.to_thread(parent.poll, 15), "子进程未执行真实 drain"
            stage = parent.recv()
            if first == "send":
                assert stage == ("sending", True)
                with pytest.raises(SessionBusyError):
                    await getattr(runtime.lifecycle, operation)(owner=OWNER, session_id=SID)
                # 同 owner 的另一个 session 不受发送锁影响。
                await runtime.lifecycle.reset(owner=OWNER, session_id="unrelated")
                parent.send("finish")
                assert await asyncio.to_thread(parent.poll, 15)
                assert parent.recv() == ("drained", True)
                await getattr(runtime.lifecycle, operation)(owner=OWNER, session_id=SID)
                assert [row["status"] for row in _rows()] == ["sent"]
            else:
                assert stage == ("drained", False), "reset/delete 已成功后不得再进入 Bot"
                assert [row["status"] for row in _rows()] == ["suppressed"]
            await asyncio.to_thread(process.join, 15)
            assert process.exitcode == 0 and not process.is_alive()
        finally:
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 10)
            parent.close()
        assert not bot.attempts

    asyncio.run(exercise())


def test_event_journal_eviction_does_not_revoke_queued_fact(agent_llm_enabled, bot):
    async def exercise():
        runtime, _, _ = await _enqueue([100])
        before = await runtime.store.load(owner=OWNER, session_id=SID)
        clock = [200]
        store = SQLiteKernelStore(clock=lambda: clock[0])
        factory = EventFactory(session_id=SID, turn_id="journal-fill", request_id="journal-fill")
        for _ in range(2005):
            await store.append(factory.create(AgentEventType.TURN_STARTED), owner=OWNER)
        with db.get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM agent_kernel_events").fetchone()[0] == 2000
        clock[0] += 31 * 24 * 3600
        await store.append(factory.create(AgentEventType.TURN_STARTED), owner=OWNER)
        with db.get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM agent_kernel_events").fetchone()[0] == 1
        assert (await store.load(owner=OWNER, session_id=SID)).lifecycle_id == before.lifecycle_id
        assert await asyncio.to_thread(center.drain_telegram_notifications, limit=16)
        assert len(bot.sent) == 1 and [row["status"] for row in _rows()] == ["sent"]

    asyncio.run(exercise())
