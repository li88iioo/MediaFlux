"""原生续行终态在新任务、部分入队和 ACK 中断后仍能交给唯一 outbox。"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from app import config
from app import database as db
from app.agent.kernel.bootstrap import (
    _capture_creation_binding,
    build_agent_kernel_runtime,
)
from app.agent.kernel.continuation import (
    _DeliveryAcknowledged,
    deliver_continuation_result,
)
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.state_types import StateUpdate
from app.agent.model_catalog import provider_channel_fingerprint
from app.agent.transports import QueryEnvelope
from app.bot.agent_adapter import (
    prepare_agent_continuation_delivery,
    publish_agent_continuation_delivery,
)
from app.modules import telegram_notification_center as notifications
from tests import test_agent_effect_continuation as effect_cases
from tests import test_agent_waiting_delivery as read_cases
from tests.agent_task_native_helpers import task_function
from tests.test_agent_read_wait_contract_repair import resumed
from tests.test_agent_read_wait_entry_exclusions import tg_config
from tests.test_agent_read_wait_lifecycle import WaitScript, isolated_database
from tests.test_agent_task_first_slice import declaration

__all__ = ["isolated_database", "tg_config"]

OWNER, SID, ROUTE = read_cases.OWNER, read_cases.SID, read_cases.ROUTE


def _telegram_model(script):
    script.adapter.settings = replace(
        script.adapter.settings,
        telegram_channel=provider_channel_fingerprint(config.get_many((
            "AGENT_LLM_API_URL", "AGENT_LLM_PROTOCOL", "AGENT_LLM_API_KEY",
        ))),
    )
    return script


def _read_task():
    task = declaration()
    task["obligations"] = [{
        "id": "read", "description": "取得本次查询回执", "required": True,
        "acceptance": {"kind": "observed_read", "criterion": "本次查询有真实回执"},
    }]
    return task


class CompleteReadScript(WaitScript):
    def __init__(self, prefix, answer):
        super().__init__(resume=True)
        self.prefix, self.answer = prefix, answer

    async def respond(self, request):
        if self.requests:
            response = await super().respond(request)
            body = response.json()
            body["choices"][0]["message"]["content"] = self.answer
            return httpx.Response(200, json=body)
        body = json.loads(request.content)
        self.requests.append(body)
        tasks = [json.loads(row["content"])["task"] for row in body["messages"]
                 if row.get("role") == "tool" and "task" in json.loads(row["content"])]
        fact = tasks[-1]["verified_facts"][0]
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [{
            "id": self.prefix + "-checkpoint", "type": "function",
            "function": task_function({
                "operation": "checkpoint", "evidence_ids": [], "blockers": [],
                "read_evidence": {"read": fact["evidence_id"]},
            }),
        }]}, "finish_reason": "tool_calls"}]})


async def _completed_read(clock, *, prefix="first", answer="第一项真实查询已完成。"):
    source = WaitScript()
    source.prefix = prefix + "-source"
    with patch.object(read_cases, "WaitScript", return_value=source), patch(
        "tests.test_agent_read_wait_lifecycle.declaration", side_effect=_read_task,
    ):
        candidate = await read_cases.park(clock, ROUTE)
    clock[0] += 10
    script = _telegram_model(CompleteReadScript(prefix + "-resume", answer))
    runtime, script, reads = resumed(clock, script)
    assert await runtime.session.resume_wait(candidate=candidate)
    state = await runtime.store.load(owner=OWNER, session_id=SID)
    assert state.metadata["task"]["status"] == "complete"
    assert len(reads) == 1 and len(script.requests) == 2
    return runtime, state, script, reads


async def _next_task(clock, *, prefix="next"):
    script = _telegram_model(WaitScript())
    script.prefix, script.steps = prefix, ["discover", "create", "finish"]
    runtime = build_agent_kernel_runtime(
        model=script.adapter, store=SQLiteKernelStore(clock=lambda: clock[0]),
    )
    runtime.session.continuation_binding = _capture_creation_binding()
    with patch("tests.test_agent_read_wait_lifecycle.declaration", side_effect=_read_task):
        events = [event async for event in runtime.session.run(QueryEnvelope(
            message=read_cases.GOAL, owner=OWNER, session_id=SID,
            request_id=prefix, channel="telegram", delivery_route=ROUTE,
        ).to_agent_input())]
    assert script.steps == [], [(event.type.value, dict(event.payload)) for event in events]
    return await runtime.store.load(owner=OWNER, session_id=SID)


def _outbox_rows():
    with db.get_conn() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM telegram_notification_outbox "
            "WHERE json_extract(event_json,'$._agent_continuation_delivery') IS NOT NULL ORDER BY id",
        )]


async def _handoff(store, candidate):
    return await deliver_continuation_result(
        store, candidate, prepare_delivery=prepare_agent_continuation_delivery,
        publish_delivery=publish_agent_continuation_delivery,
    )


@asynccontextmanager
async def _completed_effect(tmp_path, clock):
    values = {
        "AGENT_ENABLED": "1", "TG_AGENT_ENABLED": "1", "TG_CHAT_ID": "-100123",
        "TG_AGENT_ALLOWED_USER_IDS": "7",
    }
    with patch.object(effect_cases, "OWNER", OWNER), patch.object(effect_cases, "SID", SID), patch.object(
        effect_cases, "SQLiteKernelStore", side_effect=lambda: SQLiteKernelStore(clock=lambda: clock[0]),
    ), patch.object(effect_cases.effects, "time", SimpleNamespace(time=lambda: clock[0])):
        async with effect_cases._ready_case(tmp_path, channel="telegram", route=ROUTE, runtime_settings=values) as (
            store, candidate, client, reads, stack, _users,
        ):
            follow = effect_cases.FollowScript(resume=True)
            session = effect_cases._session(store, follow, client, reads, stack=stack)
            assert await session.resume_wait(candidate=candidate)
            yield store, follow, reads


def _save_legacy_delivery(store, state, *, source):
    payload = state.to_payload()
    delivery, = payload["metadata"].pop("continuation_deliveries").values()
    record = (payload["metadata"]["effect_waits"][delivery["delivery_id"].split(":")[0]]["continuation"]
              if source == "effect" else payload["metadata"]["read_wait"])
    record.update(delivery_pending=True, delivery_view=delivery["delivery_view"], delivery_state="retry_wait")
    owner_digest, session_digest = store._scope(OWNER, SID)
    domain = f"state:v1:{owner_digest}:{session_digest}:{state.generation}".encode()
    encoded, signature = store._encode(payload, domain=domain, maximum=store.max_state_bytes)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE agent_kernel_sessions SET state_json=?,state_hmac=? WHERE owner_digest=? AND session_digest=?",
            (encoded, signature, owner_digest, session_digest),
        )
    return delivery, encoded, signature, domain


def _stored_payload(store):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT state_json FROM agent_kernel_sessions WHERE owner_digest=? AND session_digest=?",
            store._scope(OWNER, SID),
        ).fetchone()
        return json.loads(row["state_json"])


def test_completed_read_answer_survives_next_task_seed(tg_config):
    async def run():
        clock = [100]
        runtime, before, script, reads = await _completed_read(clock)

        after = await _next_task(clock)

        assert after.metadata["task"]["task_id"] != before.metadata["task"]["task_id"]
        cold = SQLiteKernelStore(clock=lambda: clock[0])
        pending = await cold.due_continuations()
        assert len(pending) == 1, "合法的新任务不能覆盖尚未入 outbox 的旧终态"
        assert pending[0]["delivery_view"]["answer"] == "第一项真实查询已完成。"
        with patch.object(notifications, "wake_telegram_notification_dispatcher", return_value=False):
            assert await _handoff(cold, pending[0])
        assert await runtime.store.due_continuations() == []
        assert len(_outbox_rows()) == 1
        assert len(reads) == 1 and len(script.requests) == 2
    asyncio.run(run())


def test_two_completed_reads_keep_both_pending_answers(tg_config):
    async def run():
        clock = [100]
        _, first, first_script, first_reads = await _completed_read(clock)
        _, second, second_script, second_reads = await _completed_read(
            clock, prefix="second", answer="第二项真实查询已完成。",
        )

        assert first.metadata["task"]["task_id"] != second.metadata["task"]["task_id"]
        cold = SQLiteKernelStore(clock=lambda: clock[0])
        pending = await cold.due_continuations()
        assert {item["delivery_view"]["answer"] for item in pending} == {
            "第一项真实查询已完成。", "第二项真实查询已完成。",
        }, "两个已完成任务的待交接终态必须独立保留"
        with patch.object(notifications, "wake_telegram_notification_dispatcher", return_value=False):
            assert await _handoff(cold, pending[0])
            remaining, = await cold.due_continuations()
            assert remaining["delivery_view"] == pending[1]["delivery_view"]
            assert await _handoff(cold, remaining)
        assert await cold.due_continuations() == []
        assert len(_outbox_rows()) == 2
        assert len(first_reads) == len(second_reads) == 1
        assert len(first_script.requests) == len(second_script.requests) == 2
    asyncio.run(run())


@pytest.mark.parametrize("interruption", ["partial_enqueue", "before_ack"])
def test_interrupted_handoff_survives_new_task_and_cold_retry(tg_config, interruption):
    async def run():
        clock = [100]
        answer = "查询回执：" + "完整事实需要保留。" * 600
        runtime, before, script, reads = await _completed_read(clock, answer=answer)
        delivery, = await runtime.store.due_continuations()
        calls = []
        publish = notifications._publish

        def fail_second_part(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 2:
                return notifications.NotificationPublishResult(False, status="persist_failed")
            return publish(*args, **kwargs)

        with patch.object(notifications, "wake_telegram_notification_dispatcher", return_value=False):
            if interruption == "partial_enqueue":
                with patch.object(notifications, "_publish", side_effect=fail_second_part):
                    assert not await _handoff(runtime.store, delivery)
                assert len(calls) == 2 and len(_outbox_rows()) == 1
            else:
                with patch.object(
                    runtime.store, "update_effect_state", side_effect=RuntimeError("ACK interrupted"),
                ), pytest.raises(RuntimeError, match="ACK interrupted"):
                    await _handoff(runtime.store, delivery)
                assert len(_outbox_rows()) >= 2

            after = await _next_task(clock)
            assert after.metadata["task"]["task_id"] != before.metadata["task"]["task_id"]
            clock[0] += 31
            cold = SQLiteKernelStore(clock=lambda: clock[0])
            pending = await cold.due_continuations()
            assert len(pending) == 1, "新任务不能吞掉失败交接或尚未 ACK 的终态"
            assert pending[0]["delivery_view"]["answer"] == answer
            assert await _handoff(cold, pending[0])
            rows = _outbox_rows()
            assert len(rows) >= 2
            assert len({row["event_key"] for row in rows}) == len(rows)
            assert await cold.due_continuations() == []
            assert not await _handoff(cold, pending[0])
            assert _outbox_rows() == rows
        assert len(reads) == 1 and len(script.requests) == 2
    asyncio.run(run())


def test_effect_and_read_handoffs_survive_receipt_consumption_and_next_task_seed(tmp_path, tg_config):
    async def run():
        clock = [100]
        async with _completed_effect(tmp_path, clock) as (store, follow, effect_reads):
            original, = await store.due_continuations()
            assert "admitted.mp4" in original["delivery_view"]["answer"]

            _, read_state, read_script, read_calls = await _completed_read(clock)
            after = await _next_task(clock)

            assert after.metadata["task"]["task_id"] != read_state.metadata["task"]["task_id"]
            cold = SQLiteKernelStore(clock=lambda: clock[0])
            pending = await cold.due_continuations()
            assert len(pending) == 2, "已消费的 effect 回执与旧 READ 终态应各自保留待交接身份"
            assert {item["delivery_view"]["answer"] for item in pending} == {
                original["delivery_view"]["answer"], "第一项真实查询已完成。",
            }
            with patch.object(notifications, "wake_telegram_notification_dispatcher", return_value=False):
                for item in pending:
                    assert await _handoff(cold, item)
            assert await cold.due_continuations() == [] and len(_outbox_rows()) == 2
            assert effect_reads == ["admitted.mp4"] and len(follow.requests) == 2
            assert len(read_calls) == 1 and len(read_script.requests) == 2
            with db.get_conn() as conn:
                assert conn.execute("SELECT COUNT(*) FROM organize_operation_jobs").fetchone()[0] == 1
    asyncio.run(run())


async def _check_legacy_delivery(store, clock, *, source, tamper):
    state = await store.load(owner=OWNER, session_id=SID)
    expected, encoded, signature, domain = _save_legacy_delivery(store, state, source=source)
    cold = SQLiteKernelStore(clock=lambda: clock[0])
    if tamper:
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_kernel_sessions SET state_json=? WHERE owner_digest=? AND session_digest=?",
                (encoded.replace('"delivery_pending":true', '"delivery_pending":false'), *store._scope(OWNER, SID)),
            )
        assert await cold.due_continuations() == []
        with pytest.raises(ValueError, match="校验失败"):
            await cold.load(owner=OWNER, session_id=SID)
        assert not _stored_payload(cold)["metadata"].get("continuation_deliveries")
        with db.get_conn() as conn:
            assert conn.execute("SELECT state_hmac FROM agent_kernel_sessions").fetchone()[0] == signature
        return

    # SQL 初筛必须发现还未经过 load/正常写回的旧签名格式。
    pending, = await cold.due_continuations()
    assert pending == expected and pending["kind"] == "delivery"
    loaded = await cold.load(owner=OWNER, session_id=SID)
    assert loaded.metadata["continuation_deliveries"] == {expected["delivery_id"]: expected}
    source_record = (next(iter(loaded.metadata["effect_waits"].values()))["continuation"]
                     if source == "effect" else loaded.metadata["read_wait"])
    assert not {"delivery_pending", "delivery_view", "delivery_state"}.intersection(source_record)
    decoded = cold._decode(encoded, signature, domain=domain, expected_type=dict)
    normalized, normalized_signature = cold._encode(decoded, domain=domain, maximum=cold.max_state_bytes)
    assert cold._decode(normalized, normalized_signature, domain=domain, expected_type=dict) == decoded

    await cold.begin_turn(owner=OWNER, session_id=SID, request_id="consume-old-source")
    rewritten = _stored_payload(cold)
    assert rewritten["metadata"]["continuation_deliveries"] == {expected["delivery_id"]: expected}
    assert "delivery_pending" not in json.dumps(rewritten)
    if source == "effect":
        assert not rewritten["metadata"].get("effect_waits")
    with patch.object(notifications, "wake_telegram_notification_dispatcher", return_value=False):
        assert await _handoff(cold, pending)
    assert await cold.due_continuations() == [] and len(_outbox_rows()) == 1
    assert json.loads(_outbox_rows()[0]["event_json"])["_agent_continuation_delivery"]["delivery_route"] == ROUTE


@pytest.mark.parametrize("tamper", [False, True])
def test_old_signed_read_pending_is_normalized_before_due_scan(tg_config, tamper):
    async def run():
        clock = [100]
        runtime, _, script, reads = await _completed_read(clock)
        await _check_legacy_delivery(runtime.store, clock, source="read", tamper=tamper)
        assert len(reads) == 1 and len(script.requests) == 2
    asyncio.run(run())


@pytest.mark.parametrize("tamper", [False, True])
def test_old_signed_effect_pending_is_normalized_before_due_scan(tmp_path, tamper):
    async def run():
        clock = [100]
        async with _completed_effect(tmp_path, clock) as (store, script, reads):
            await _check_legacy_delivery(store, clock, source="effect", tamper=tamper)
            assert reads == ["admitted.mp4"] and len(script.requests) == 2
    asyncio.run(run())


def test_delivery_namespace_and_ack_cannot_clear_other_answers(tg_config):
    async def run():
        clock = [100]
        await _completed_read(clock)
        runtime, _, _, _ = await _completed_read(clock, prefix="second", answer="第二项真实查询已完成。")
        store = runtime.store
        pending = await store.due_continuations()
        lease, before = await store.begin_turn(owner=OWNER, session_id=SID, request_id="guarded-update")
        for key in ("metadata.continuation_deliveries", "metadata.continuation_deliveries.untrusted"):
            with pytest.raises(ValueError, match="typed transition"):
                await store.commit(lease, updates=(StateUpdate(key, {}),))

        def clear_deliveries(state):
            state.metadata.pop("continuation_deliveries")
        with pytest.raises(ValueError, match="publication binding"):
            await store.update_effect_state(owner=OWNER, session_id=SID, change=clear_deliveries)
        wrong = deepcopy(pending[0])
        wrong["delivery_view"]["answer"] = "其他内容不是此项答复"
        await store.update_effect_state(
            owner=OWNER, session_id=SID, change=_DeliveryAcknowledged(wrong, True, False, clock[0]),
        )
        assert (await store.load(owner=OWNER, session_id=SID)).metadata == before.metadata
        with patch.object(notifications, "wake_telegram_notification_dispatcher", return_value=False):
            assert await _handoff(store, pending[0])
        assert await store.due_continuations() == [pending[1]]
    asyncio.run(run())


def test_old_large_terminal_moves_without_duplicate_view_or_answer_truncation(tg_config):
    async def run():
        clock = [100]
        answer = "完整回执" + "a" * 40_000
        runtime, state, script, reads = await _completed_read(clock, answer=answer)
        size = len(json.dumps(state.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        _save_legacy_delivery(runtime.store, state, source="read")
        cold = SQLiteKernelStore(clock=lambda: clock[0], max_state_bytes=size + 64)
        pending, = await cold.due_continuations()
        assert pending["delivery_view"]["answer"] == answer
        await cold.begin_turn(owner=OWNER, session_id=SID, request_id="capacity")
        saved = _stored_payload(cold)
        assert "delivery_view" not in saved["metadata"]["read_wait"]
        assert saved["metadata"]["continuation_deliveries"][pending["delivery_id"]]["delivery_view"]["answer"] == answer
        assert len(json.dumps(saved, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()) <= cold.max_state_bytes
        assert len(reads) == 1 and len(script.requests) == 2
    asyncio.run(run())
