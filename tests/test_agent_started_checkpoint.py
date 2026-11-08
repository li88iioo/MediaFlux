"""Ordinary batches durably enter unknown before business I/O, not in finally."""
from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
from copy import deepcopy
from unittest.mock import patch

import pytest

from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.projection import ToolOutcome
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.state_types import AgentInput
from tests.support import isolated_test_database
from tests.test_agent_provider_state import REDACTED, SIGNATURE, THINKING
from tests.test_agent_provider_text_authority import native_reply
from tests.test_agent_tool_argument_ingress import PROTOCOLS, Wire, encode

OWNER, SESSION = "checkpoint-owner", "checkpoint-session"
FIRST_FACT = "FIRST_FACT: inventory independently observed"
ASSISTANT = "逐项核对库存，不执行未经确认的写操作。"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("real sockets and DNS are forbidden")
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    for name in ("getaddrinfo", "create_connection", "gethostbyname"):
        monkeypatch.setattr(socket, name, deny)


def store():
    return SQLiteKernelStore(secret_provider=lambda: "started-checkpoint-synthetic")


async def reopened():
    return await store().load(owner=OWNER, session_id=SESSION)


def spec(name, handler):
    return KernelToolSpec(
        name=name, domain="library", description="inspect 库存 核对", effect=ToolEffect.READ,
        input_schema={"type": "object", "required": ["selector"], "properties": {
            "selector": {"type": "string"}, "exclude_played": {"type": "boolean"},
        }, "additionalProperties": False}, read=handler,
    )


def batch(protocol, calls):
    reply = native_reply(protocol, ASSISTANT)
    if protocol == "chat_completions":
        reply["choices"][0]["message"]["tool_calls"] = [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": encode(args)}}
            for cid, name, args in calls
        ]
        reply["choices"][0]["finish_reason"] = "tool_calls"
    elif protocol == "responses":
        reply["output"].extend(
            {"type": "function_call", "id": "fc_" + cid, "call_id": cid, "name": name, "arguments": encode(args)}
            for cid, name, args in calls
        )
    else:
        reply["content"].extend(
            {"type": "tool_use", "id": cid, "name": name, "input": args}
            for cid, name, args in calls
        )
        reply["stop_reason"] = "tool_use"
    return reply


def wire(protocol, replies):
    return Wire(protocol, [(encode(reply).encode(), "application/json") for reply in replies])


def session(catalog, transport, *, state_store=None, limits=None):
    state_store = state_store if state_store is not None else store()
    return AgentSession(
        model=transport.adapter, catalog=catalog,
        pipeline=ToolPipeline(catalog=catalog, state_store=state_store, reference_store=state_store),
        state_store=state_store, journal=state_store, limits=limits,
        system_prompt="按实际工具事实回答，未知不等于成功。",
    )


async def collect(active, request="initial"):
    return [event async for event in active.run(AgentInput(
        owner=OWNER, session_id=SESSION, request_id=request, message="inspect 核对库存，排除已看。",
    ))]


def result_rows(state):
    return [row for row in state.conversation if row.get("role") == "tool"]


def assert_pairs(state, calls):
    callers = [row for row in state.conversation if row.get("tool_calls")]
    assert len(callers) == 1
    assert callers[0]["content"] == ASSISTANT
    assert [(c["call_id"], c["name"], c["arguments"]) for c in callers[0]["tool_calls"]] == calls
    assert [(row["tool_call_id"], row["tool_name"]) for row in result_rows(state)] == [
        (cid, name) for cid, name, _ in calls
    ]
    return callers[0]["provider_state"]


def assert_unknown(row, code="result_unknown"):
    value = json.loads(row["content"])
    assert value["code"] == code
    assert value["ok"] is False and value["status"] == "error"


def assert_native_wire(protocol, payload, calls):
    if protocol == "chat_completions":
        messages = payload["messages"]
        actual = [(c["id"], c["function"]["name"], json.loads(c["function"]["arguments"]))
                  for m in messages for c in m.get("tool_calls", [])]
        results = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
        assert next(m["reasoning_content"] for m in messages if m.get("tool_calls")) == THINKING
    elif protocol == "responses":
        messages = payload["input"]
        actual = [(m["call_id"], m["name"], json.loads(m["arguments"]))
                  for m in messages if m.get("type") == "function_call"]
        results = [m["call_id"] for m in messages if m.get("type") == "function_call_output"]
        reasoning = next(m for m in messages if m.get("type") == "reasoning")
        assert reasoning == {"type": "reasoning", "id": "rs_1", "encrypted_content": REDACTED,
                             "summary": [{"type": "summary_text", "text": "Checking the actual result."}],
                             "content": [{"type": "reasoning_text", "text": THINKING}]}
    else:
        blocks = [b for m in payload["messages"] if isinstance(m.get("content"), list) for b in m["content"]]
        actual = [(b["id"], b["name"], b["input"]) for b in blocks if b.get("type") == "tool_use"]
        results = [b["tool_use_id"] for b in blocks if b.get("type") == "tool_result"]
        assert next(b for b in blocks if b.get("type") == "thinking") == {
            "type": "thinking", "thinking": THINKING, "signature": SIGNATURE,
        }
        assert next(b for b in blocks if b.get("type") == "redacted_thinking") == {
            "type": "redacted_thinking", "data": REDACTED,
        }
    assert actual == calls
    assert results == [cid for cid, _, _ in calls]


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("blocked", [False, True])
def test_batch_activation_characterization(protocol, blocked):
    async def exercise():
        handled = []
        calls = [(key, "inspect_batch", {"selector": key, "exclude_played": True}) for key in ("a", "b")]
        catalog = ToolCatalog([spec("inspect_batch", lambda a, c: handled.append(deepcopy(a)) or {"data": a})])
        transport = wire(protocol, [batch(protocol, calls), native_reply(protocol, "核对完成", private=False)])
        agent = session(catalog, transport, limits=SessionLimits(max_tool_calls=1 if blocked else 16))
        events = await collect(agent)
        state = await reopened()
        assert_pairs(state, calls)
        assert handled == ([] if blocked else [args for _, _, args in calls])
        for row, (_, _, args) in zip(result_rows(state), calls, strict=True):
            if blocked:
                assert_unknown(row, "tool_budget_exceeded")
            else:
                assert json.loads(row["content"])["data"] == args
        assert events[-1].payload["status"] == ("partial" if blocked else "success")
        assert events[-1].payload["tool_calls"] == (0 if blocked else 2)
        assert [e.payload["call_id"] for e in events if e.type is AgentEventType.TOOL_STARTED] == ([] if blocked else ["a", "b"])
        assert [e.payload["call_id"] for e in events if e.type is (AgentEventType.TOOL_FAILED if blocked else AgentEventType.TOOL_COMPLETED)] == ["a", "b"]
        if blocked:
            assert len(transport.requests) == 1  # 预算终结不偷偷追加汇总请求。
            await collect(agent, request="inspect-retained-native-history")
        assert_native_wire(protocol, transport.requests[-1], calls)
        transport.verify(2)
    with isolated_test_database():
        asyncio.run(exercise())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("kind", ["outcome", "error"])
def test_future_unbounded_result_cannot_erase_completed_prefix(protocol, kind):
    """Kernel-legal passthrough values, not a claim about any real media service."""
    async def exercise():
        handled, entered = [], []
        async def read(arguments, context):
            key = arguments["selector"]
            handled.append(key)
            entered.append(await reopened())
            if key == "good":
                return {"ok": True, "data": {"fact": FIRST_FACT}}
            if key == "large":
                if kind == "error":
                    raise ToolPipelineError("safe error", code="E" * 180000)
                return ToolOutcome(
                    model_content=encode({"ok": True, "status": "S" * 180000, "data": {"fact": "SECOND_FACT"}}),
                    public_content={"ok": True, "status": "success", "summary": "read finished"},
                )
            return {"data": {"fact": "THIRD_MUST_NOT_RUN"}}
        calls = [(key, "inspect_batch", {"selector": key, "exclude_played": True}) for key in ("good", "large", "later")]
        catalog = ToolCatalog([spec("inspect_batch", read)])
        transport = wire(protocol, [batch(protocol, calls)])
        events = await collect(session(catalog, transport))
        state = await reopened()
        native = assert_pairs(state, calls)
        rows = result_rows(state)
        assert_unknown(rows[1])
        assert json.loads(rows[0]["content"])["data"]["fact"] == FIRST_FACT
        assert_unknown(rows[2], "not_executed")
        assert handled == ["good", "large"]
        assert events[-1].payload.get("status") != "success"
        for index, snapshot in enumerate(entered):
            assert assert_pairs(snapshot, calls) == native
            assert_unknown(result_rows(snapshot)[index])
            for row in result_rows(snapshot)[index + 1:]:
                assert_unknown(row, "not_executed")
        assert json.loads(result_rows(entered[1])[0]["content"])["data"]["fact"] == FIRST_FACT
        # A new Session and store really serialize the persisted checkpoint.
        next_wire = wire(protocol, [native_reply(protocol, "结果仍未知，先核验", private=False)])
        await collect(session(catalog, next_wire, state_store=store()), "reopened")
        assert_native_wire(protocol, next_wire.requests[0], calls)
        assert handled == ["good", "large"]  # History is not replayed as work.
        assert THINKING not in repr(events) and SIGNATURE not in repr(events)
        transport.verify()
        next_wire.verify()
    with isolated_test_database():
        asyncio.run(exercise())


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_checkpoint_is_committed_at_each_handler_entry_and_replaced_on_success(protocol):
    async def exercise():
        handled, snapshots = [], []
        async def read(arguments, context):
            handled.append(deepcopy(arguments))
            snapshots.append(await reopened())
            return {"ok": True, "data": {"fact": arguments["selector"]}}
        calls = [(key, "inspect_batch", {"selector": key}) for key in ("first", "second", "third")]
        catalog = ToolCatalog([spec("inspect_batch", read)])
        transport = wire(protocol, [batch(protocol, calls), native_reply(protocol, "全部核对完成", private=False)])
        events = await collect(session(catalog, transport))
        final = await reopened()
        native = assert_pairs(final, calls)
        assert handled == [args for _, _, args in calls]
        assert len(snapshots) == len(calls)
        for index, snapshot in enumerate(snapshots):
            assert assert_pairs(snapshot, calls) == native
            rows = result_rows(snapshot)
            assert_unknown(rows[index])
            for prior in range(index):
                assert json.loads(rows[prior]["content"])["data"]["fact"] == calls[prior][0]
            for row in rows[index + 1:]:
                assert_unknown(row, "not_executed")
        assert [json.loads(row["content"])["data"]["fact"] for row in result_rows(final)] == [c[0] for c in calls]
        assert events[-1].payload["status"] == "success"
        assert_native_wire(protocol, transport.requests[-1], calls)
        transport.verify(2)
    with isolated_test_database():
        asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["io", "cancel", "stale"])
def test_preexecution_checkpoint_failure_never_runs_handler_or_erases_prior_fact(failure):
    async def exercise():
        handled, intercepted, cancelled_leases, prior_native = [], [], [], []
        calls = [(key, "inspect_batch", {"selector": key}) for key in ("good", "blocked", "later")]
        catalog = ToolCatalog([spec("inspect_batch", lambda a, c: handled.append(a["selector"]) or {"data": {"fact": FIRST_FACT}})])
        transport = wire("chat_completions", [batch("chat_completions", calls), native_reply("chat_completions", "完成", private=False)])
        active_store = store()
        active = session(catalog, transport, state_store=active_store)
        original_commit, original_write = active_store.commit, active_store._write_state
        def is_start(conversation):
            return any(row.get("tool_call_id") == "blocked" and json.loads(row["content"]).get("code") == "result_unknown"
                       for row in conversation or [])
        def failing_write(conn, state):
            if is_start(state.conversation):
                intercepted.append("io")
                raise sqlite3.OperationalError("synthetic disk I/O failure before state write")
            return original_write(conn, state)
        async def interrupt(lease, *, conversation=None, updates=()):
            if is_start(conversation) and not intercepted:
                intercepted.append(failure)
                if failure == "cancel":
                    prior = await reopened()
                    prior_native.append(assert_pairs(prior, calls))
                    assert_unknown(result_rows(prior)[1], "not_executed")
                    cancelled_leases.append(lease)
                    assert await active.cancel(owner=OWNER, session_id=SESSION)
                else:
                    await store().begin_turn(owner=OWNER, session_id=SESSION, request_id="new-owner")
            return await original_commit(lease, conversation=conversation, updates=updates)
        with patch.object(active_store, "_write_state", failing_write if failure == "io" else original_write), patch.object(
            active_store, "commit", original_commit if failure == "io" else interrupt,
        ):
            events = await collect(active)
        assert handled == ["good"], "failed pre-execution checkpoint must prevent this and later handlers"
        assert intercepted and set(intercepted) == {failure}
        final = await reopened()
        assert_pairs(final, calls)
        rows = result_rows(final)
        assert json.loads(rows[0]["content"])["data"]["fact"] == FIRST_FACT
        # Cancellation wins before the started checkpoint: neither handler ran.
        assert_unknown(rows[1], "not_executed")
        assert_unknown(rows[2], "not_executed")
        if failure == "cancel":
            from app.agent.kernel.state_types import StalePublicationError
            old_lease, = cancelled_leases
            assert assert_pairs(final, calls) == prior_native[0]
            assert final.generation == old_lease.generation + 1
            assert final.metadata["active_publication"] == {
                "generation": old_lease.generation,
                "turn_id": old_lease.turn_id,
                "request_id": old_lease.request_id, "status": "cancelled",
            }
            cold_store = store()
            assert not await cold_store.is_current(old_lease)
            with pytest.raises(StalePublicationError):
                await cold_store.commit(old_lease, conversation=final.conversation)
            assert (await reopened()).conversation == final.conversation
            next_wire = wire("chat_completions", [native_reply("chat_completions", "尚未执行", private=False)])
            await collect(session(catalog, next_wire, state_store=store()), "reopened")
            assert_native_wire("chat_completions", next_wire.requests[0], calls)
            assert handled == ["good"]  # Cold history is not business replay.
            next_wire.verify()
        if failure != "io":
            assert events[-1].type is AgentEventType.TURN_CANCELLED
        else:
            assert events[-1].payload["status"] == "partial"
        transport.verify()
    with isolated_test_database():
        asyncio.run(exercise())


@pytest.mark.parametrize("unknown", [False, True])
def test_read_preview_confirmation_and_duplicate_keep_receipt_without_replay(unknown):
    """Normal-capacity control, not a guarantee for oversized confirmed receipts."""
    async def exercise():
        reads, preparations, writes = [], [], []
        read = spec("inspect_batch", lambda a, c: reads.append(a) or {"data": {"fact": FIRST_FACT}})
        def prepare(arguments, context):
            preparations.append(deepcopy(arguments))
            return PreparedEffect(preview={"summary": "等待确认"}, snapshot_fingerprint="frozen-synthetic-snapshot")
        def execute(arguments, fingerprint, context):
            writes.append((deepcopy(arguments), fingerprint))
            if unknown:
                raise ToolPipelineError("执行结果未知，先核验，禁止重放", code="outcome_unknown")
            return {"ok": True, "status": "success", "summary": "WRITE_RECEIPT"}
        write = KernelToolSpec(
            name="inspect_change", domain="library", description="inspect 核对库存 变更",
            input_schema=read.input_schema, effect=ToolEffect.WRITE,
            prepare=prepare, execute_confirmed=execute,
        )
        calls = [("read", read.name, {"selector": "read"}), ("write", write.name, {"selector": "frozen"}),
                 ("deferred", read.name, {"selector": "later"})]
        catalog = ToolCatalog([read, write])
        transport = wire("chat_completions", [batch("chat_completions", calls), native_reply("chat_completions", "写入已完成", private=False)])
        active = session(catalog, transport)
        events = await collect(active)
        preview = await reopened()
        native = assert_pairs(preview, calls)
        assert events[-1].payload["status"] == "approval_required"
        assert reads == [{"selector": "read"}] and writes == []
        assert preparations == [{"selector": "frozen"}]
        assert json.loads(result_rows(preview)[0]["content"])["data"]["fact"] == FIRST_FACT
        assert_unknown(result_rows(preview)[2], "not_executed_after_approval")
        plan_id = preview.pending_effect_plan_id
        assert plan_id
        confirmed = [event async for event in active.confirm(owner=OWNER, session_id=SESSION, plan_id=plan_id)]
        assert writes == [({"selector": "frozen"}, "frozen-synthetic-snapshot")]
        saved = await reopened()
        assert assert_pairs(saved, calls) == native
        receipt = next(row for row in saved.conversation if row.get("completion_receipt_id") == plan_id)
        assert ("outcome_unknown" if unknown else "WRITE_RECEIPT") in receipt["content"]
        assert any(event.type is (AgentEventType.EFFECT_FAILED if unknown else AgentEventType.EFFECT_COMPLETED) for event in confirmed)
        duplicate = [event async for event in active.confirm(owner=OWNER, session_id=SESSION, plan_id=plan_id)]
        assert duplicate[-1].payload["code"] == "confirmation_invalid"
        assert writes == [({"selector": "frozen"}, "frozen-synthetic-snapshot")]
        next_wire = wire("chat_completions", [native_reply("chat_completions", "先核验状态", private=False)])
        await collect(session(catalog, next_wire, state_store=store()), "reopened")
        assert_native_wire("chat_completions", next_wire.requests[0], calls)
        assert writes == [({"selector": "frozen"}, "frozen-synthetic-snapshot")]
        assert reads == [{"selector": "read"}] and preparations == [{"selector": "frozen"}]
        if unknown:
            assert confirmed[-1].payload["status"] == "partial"
            assert "写入已完成" not in confirmed[-1].payload["answer"]
        transport.verify(2)
        next_wire.verify()
    with isolated_test_database():
        asyncio.run(exercise())


def test_cancellation_after_handler_entry_preserves_completed_and_unknown():
    async def exercise():
        started = asyncio.Event()
        handled = []
        async def read(arguments, context):
            key = arguments["selector"]
            handled.append(key)
            if key == "good":
                return {"data": {"fact": FIRST_FACT}}
            started.set()
            await context.cancellation.wait()
            context.cancellation.raise_if_cancelled()
        calls = [(key, "inspect_batch", {"selector": key}) for key in ("good", "waiting", "later")]
        catalog = ToolCatalog([spec("inspect_batch", read)])
        transport = wire("chat_completions", [batch("chat_completions", calls)])
        active = session(catalog, transport)
        task = asyncio.create_task(collect(active))
        try:
            await asyncio.wait_for(started.wait(), 5)
            assert await active.cancel(owner=OWNER, session_id=SESSION)
            events = await asyncio.wait_for(task, 5)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        state = await reopened()
        assert_pairs(state, calls)
        rows = result_rows(state)
        assert json.loads(rows[0]["content"])["data"]["fact"] == FIRST_FACT
        assert_unknown(rows[1])
        assert_unknown(rows[2], "not_executed")
        assert handled == ["good", "waiting"]
        assert events[-1].type is AgentEventType.TURN_CANCELLED
        transport.verify()
    with isolated_test_database():
        asyncio.run(exercise())
