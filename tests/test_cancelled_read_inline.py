"""Private returned READ settlement: full facts, exact CAS and no authority revival."""
import asyncio
import json
from typing import Any, cast
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

import pytest

from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.conversation_codec import pending_content
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.references import InMemoryReferenceStore
from app.agent.kernel.projection import DefaultProjector
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import AgentInput, StalePublicationError
from tests.support import isolated_test_database
from tests.test_agent_kernel_core import ScriptedModel

READ_TOOL = KernelToolSpec(name="library.inspect", domain="library", description="inspect",
    effect=ToolEffect.READ, input_schema={"type": "object"}, read=lambda arguments: arguments)


def sqlite_store():
    return SQLiteKernelStore(secret_provider=lambda: "cancel-inline-test-secret")


def model(call=True):
    return ScriptedModel([[
        ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("same", "library.inspect", {}))
        if call else ModelEvent(ModelEventType.TEXT_DELTA, text="完整结果已保留"),
        ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls" if call else "stop"),
    ]])


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("mode", ["returned", "no_return", "new_generation", "oversize", "wire_budget", "late_write", "compact_outcome", "fake_handle"])
def test_real_returned_read(backend, mode):
    async def run():
        store = sqlite_store() if backend == "sqlite" else InMemorySessionStateStore()
        payload = {"ok": True, "status": "partial", "summary": "observed", "data": {
            "rows": [{"index": i, "value": "fact"} for i in range(20)],
            "long": "x" * (300000 if mode == "oversize" else 100000 if mode in {"wire_budget", "fake_handle"} else 12000) + "EXACT_TAIL",
            "private_path": "/private/cancel-canary", "password": "SECRET_CANARY",
        }}
        if mode == "fake_handle":
            payload.update(result_handle="absent-source", read_tool="agent.read_result")
        before = {}; calls = []; holder = {}
        async def read(arguments, context):
            calls.append(arguments)
            assert not hasattr(context, "_returned_read")
            before["lease"] = context.lease
            before["started"] = await store.load(owner="o", session_id="s")
            assert await holder["session"].cancel(owner="o", session_id="s", request_id="same")
            before["cancelled"] = await store.load(owner="o", session_id="s")
            if mode == "new_generation":
                await store.begin_turn(owner="o", session_id="s", request_id="same")
                before["cancelled"] = await store.load(owner="o", session_id="s")
            if mode == "no_return":
                raise asyncio.CancelledError("never returned")
            if mode == "late_write":
                from app.agent.kernel.tool_calls import PreparedEffect
                return PreparedEffect(preview={"effect": "read", "receipt": payload}, snapshot_fingerprint="fake-read-authority")
            if mode == "compact_outcome":
                from app.agent.kernel.projection import ToolOutcome
                return ToolOutcome(model_content='{"ok":true,"summary":"not a complete source"}', public_content={"ok": True})
            return payload
        def forbidden_write(*args):
            raise AssertionError("confirmed write must never execute")
        catalog = ToolCatalog([KernelToolSpec(name="library.inspect", domain="library", description="inspect",
            effect=ToolEffect.WRITE if mode == "late_write" else ToolEffect.READ,
            input_schema={"type": "object", "properties": {}},
            read=read if mode != "late_write" else None,
            prepare=read if mode == "late_write" else None,
            execute_confirmed=forbidden_write if mode == "late_write" else None)])
        active = AgentSession(model=model(), catalog=catalog,
            pipeline=ToolPipeline(catalog=catalog, state_store=store,
                                  reference_store=store if isinstance(store, SQLiteKernelStore) else None), state_store=store)
        holder["session"] = active
        events = [event async for event in active.run(AgentInput(owner="o", session_id="s", request_id="same", message="inspect"))]
        assert events[-1].type == AgentEventType.TURN_CANCELLED
        assert not any(e.type == AgentEventType.TOOL_COMPLETED for e in events)
        assert "SECRET_CANARY" not in repr([e.payload for e in events])
        assert "/private/cancel-canary" not in repr([e.payload for e in events])
        if backend == "sqlite":
            from app import database as db
            with db.get_conn() as conn:
                assert conn.execute("SELECT COUNT(*) FROM agent_kernel_refs").fetchone()[0] == 0
        else:
            assert isinstance(active.pipeline.reference_store, InMemoryReferenceStore)
            assert not active.pipeline.reference_store._records
        cold = sqlite_store() if backend == "sqlite" else store
        after = await cold.load(owner="o", session_id="s")
        expected = before["cancelled"].clone()
        if mode in {"returned", "wire_budget", "fake_handle"}:
            projection = DefaultProjector().project(payload)
            expected.conversation[-1]["content"] = projection.full_model_content or projection.model_content
            assert json.loads(after.conversation[-1]["content"]) == json.loads(expected.conversation[-1]["content"])
            expected.conversation[-1]["content"] = after.conversation[-1]["content"]
            assert events[-1].payload["read_settlement"] == "settled"
            assert "SECRET_CANARY" not in json.dumps(after.to_payload())
            assert "/private/cancel-canary" not in json.dumps(after.to_payload())
        else:
            assert after.conversation[-1]["content"] == pending_content(True)
            if mode not in {"no_return", "late_write"}:
                assert events[-1].payload["read_settlement"] == "rejected"
            else:
                assert "read_settlement" not in events[-1].payload
        assert after == expected
        assert not await cold.is_current(before["lease"])
        with pytest.raises(StalePublicationError):
            await cold.commit(before["lease"], conversation=[])
        if mode in {"returned", "wire_budget", "fake_handle"}:
            next_model = model(False)
            from app.agent.kernel.session import SessionLimits
            following = AgentSession(model=next_model, catalog=catalog,
                pipeline=ToolPipeline(catalog=catalog, state_store=cold), state_store=cold,
                limits=SessionLimits(context_window_tokens=16384) if mode in {"wire_budget", "fake_handle"} else None)
            following_events = [e async for e in following.run(AgentInput(owner="o", session_id="s", message="describe existing facts"))]
            if mode in {"wire_budget", "fake_handle"}:
                assert following_events[-1].type == AgentEventType.TURN_COMPLETED
                assert next_model.requests
                assert any(json.loads(m.content).get("truncated") for m in next_model.requests[0].messages if m.role == "tool")
            else:
                assert following_events[-1].type == AgentEventType.TURN_COMPLETED
                actual = next(m.content for m in next_model.requests[0].messages if m.role == "tool")
                assert json.loads(actual) == json.loads(after.conversation[-1]["content"])
            later = await cold.load(owner="o", session_id="s")
            assert next(r for r in later.conversation if r["role"] == "tool") == after.conversation[-1]
        assert len(calls) == 1
    with isolated_test_database():
        asyncio.run(run())


def test_small_cancelled_fact_real_consumer():
    async def run():
        store = sqlite_store(); holder = {}; calls = []
        payload = {"ok": True, "data": {"unique": "SMALL_SOLE_FACT"}}
        async def read(arguments, context):
            calls.append(arguments)
            assert await holder["session"].cancel(owner="o", session_id="small")
            return payload
        def session(m, limits=None):
            from app.agent.kernel.sandbox import run_code_tool
            cold = sqlite_store()
            catalog = ToolCatalog([replace(READ_TOOL, read=read)])
            pipeline = ToolPipeline(catalog=catalog, state_store=cold, reference_store=cold)
            catalog.register(run_code_tool(pipeline))
            return AgentSession(model=m, catalog=catalog,
                pipeline=pipeline,
                state_store=cold, limits=limits)
        holder["session"] = session(model())
        events = [e async for e in holder["session"].run(AgentInput(owner="o", session_id="small", message="Q" * 12000))]
        assert events[-1].type == AgentEventType.TURN_CANCELLED
        assert events[-1].payload["read_settlement"] == "settled"
        assert not any(e.type == AgentEventType.TOOL_COMPLETED for e in events)
        saved = await store.load(owner="o", session_id="small")
        fact = next(r for r in saved.conversation if r["role"] == "tool")
        assert len(fact["content"]) <= 400
        assert json.loads(fact["content"]) == json.loads(DefaultProjector().project(payload).model_content)
        for i in range(4):
            mid_model = model(False)
            mid = session(mid_model)
            events = [e async for e in mid.run(AgentInput(owner="o", session_id="small", message=str(i) + "R" * 11999))]
            assert events[-1].type == AgentEventType.TURN_COMPLETED
            assert fact["content"] in [m.content for m in mid_model.requests[0].messages if m.role == "tool"]
        from app.agent.kernel.session import SessionLimits
        next_model = model(False)
        following = session(next_model, SessionLimits(context_window_tokens=16384))
        events = [e async for e in following.run(AgentInput(owner="o", session_id="small", message="describe previous fact"))]
        assert events[-1].type == AgentEventType.TURN_COMPLETED
        assert next_model.requests
        assert "observations" in next_model.requests[0].system_prompt
        cold = await sqlite_store().load(owner="o", session_id="small")
        assert next(r for r in cold.conversation if r["role"] == "tool") == fact
        assert len(calls) == 1
    with isolated_test_database():
        asyncio.run(run())


def test_native_cold_followup():
    from tests.test_agent_started_checkpoint import (
        OWNER,
        SESSION,
        assert_native_wire,
        batch,
        collect,
        native_reply,
        reopened,
        session,
        spec,
        wire,
    )
    async def run():
        calls = [("native-call", "inspect_batch", {"selector": "one", "exclude_played": True})]
        payload = {"ok": True, "status": "partial", "data": {"value": "fact" * 2000 + "NATIVE_TAIL"}}
        holder: dict[str, Any] = {}; observed = []
        async def read(arguments, context):
            observed.append(deepcopy(arguments))
            holder["started"] = await reopened()
            assert await holder["active"].cancel(owner=OWNER, session_id=SESSION)
            return payload
        catalog = ToolCatalog([spec("inspect_batch", read)])
        first = wire("chat_completions", [batch("chat_completions", calls)])
        holder["active"] = session(catalog, first)
        events = await collect(holder["active"])
        assert events[-1].type == AgentEventType.TURN_CANCELLED
        after = await reopened()
        assert after.conversation[:-1] == holder["started"].conversation[:-1]
        assert json.loads(after.conversation[-1]["content"]) == json.loads(DefaultProjector().project(payload).model_content)
        second = wire("chat_completions", [native_reply("chat_completions", "核对已保留", private=False)])
        follow = await collect(session(catalog, second), "followup")
        assert follow[-1].type == AgentEventType.TURN_COMPLETED
        assert_native_wire("chat_completions", second.requests[0], calls)
        actual = next(m["content"] for m in second.requests[0]["messages"] if m["role"] == "tool")
        assert json.loads(actual) == json.loads(after.conversation[-1]["content"])
        cold = await reopened()
        assert next(row for row in cold.conversation if row["role"] == "tool") == after.conversation[-1]
        assert len(observed) == 1
        first.verify(1); second.verify(1)
    with isolated_test_database():
        asyncio.run(run())


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("excess", [0, 1])
def test_exact_whole_state_capacity(backend, excess):
    async def run():
        from app.agent.kernel.conversation_codec import json_text
        from app.agent.kernel.state_types import (
            StateUpdate,
            _CancelledReadInline,
            _ExecutionCapacity,
        )
        store = sqlite_store() if backend == "sqlite" else InMemorySessionStateStore()
        lease, _ = await store.begin_turn(owner="o", session_id="s", request_id="same")
        rows: list[dict[str, Any]] = [{"role": "assistant", "content": "", "tool_calls": [
            {"call_id": "same", "name": "library.inspect", "arguments": {}}]},
            {"role": "tool", "tool_call_id": "same", "tool_name": "library.inspect", "content": pending_content(True)}]
        state = await store.commit(lease, conversation=rows, updates=(
            _ExecutionCapacity("", True), StateUpdate("metadata.filler", "")))
        content = json_text({"ok": True, "data": "x" * 50000 + "TAIL"})
        projected = state.clone()
        projected.metadata["active_publication"]["status"] = "cancelled"
        projected.conversation[-1]["content"] = content
        filler = 256 * 1024 - len(json_text(projected.to_payload()).encode()) + excess
        state = await store.commit(lease, updates=(StateUpdate("metadata.filler", "z" * filler),))
        receipt = _CancelledReadInline.capture(lease, state.conversation, 0, tool=READ_TOOL)
        receipt.put_once(content)
        await store.cancel_publication(owner="o", session_id="s", request_id="same")
        cold = sqlite_store() if backend == "sqlite" else store
        before = await cold.load(owner="o", session_id="s")
        if excess:
            with pytest.raises(ValueError): await store.settle_cancelled_reads((receipt,))
            assert await cold.load(owner="o", session_id="s") == before
        else:
            assert await store.settle_cancelled_reads((receipt,))
            after = await cold.load(owner="o", session_id="s")
            assert len(json_text(after.to_payload()).encode()) == 256 * 1024
            before.conversation[-1]["content"] = content
            assert after == before
    with isolated_test_database():
        asyncio.run(run())


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("mode", ["success", "prefix", "native_type", "wrong_row", "wrong_same_id", "pending", "scope", "forged", "write_failure", "capacity"])
def test_checkpoint_cas(backend, mode):
    async def run():
        from app.agent.kernel.state_types import (
            _CancelledReadInline,
            _ExecutionCapacity,
        )
        store = sqlite_store() if backend == "sqlite" else InMemorySessionStateStore()
        lease, _ = await store.begin_turn(owner="o", session_id="s", request_id="same")
        rows: list[dict[str, Any]] = [{"role": "user", "content": "original prefix"},
                {"role": "assistant", "content": "", "provider_state": {"native": [True, 1, "signature"]},
                 "tool_calls": [{"call_id": "same", "name": "library.inspect", "arguments": {}}]},
                {"role": "tool", "tool_call_id": "same", "tool_name": "library.inspect", "content": pending_content(mode != "pending")}]
        if mode == "wrong_same_id":
            rows[1]["tool_calls"].append(deepcopy(rows[1]["tool_calls"][0]))
            rows.append(deepcopy(rows[-1]))
        state = await store.commit(lease, conversation=rows, updates=(_ExecutionCapacity("", True),))
        receipt = _CancelledReadInline.capture(lease, state.conversation, 0, tool=READ_TOOL)
        if mode == "wrong_same_id":
            receipt = replace(receipt, tool_index=3)
        receipt.put_once(json.dumps({"ok": True, "data": "x" * 50000 + "FULL_TAIL"}))
        with pytest.raises(ValueError):
            receipt.put_once('{"replacement":true}')
        if mode in {"prefix", "native_type"}:
            changed = deepcopy(rows)
            if mode == "prefix": changed[0]["content"] = "different"
            else: changed[1]["provider_state"]["native"][0] = 1
            await store.commit(lease, conversation=changed)
        if mode == "wrong_row":
            receipt = replace(receipt, tool_index=1)
            receipt.put_once('{"ok":true}')
        if mode == "scope":
            receipt = replace(receipt, lease=replace(lease, owner="other"))
            receipt.put_once('{"ok":true}')
        if mode == "forged": receipt = cast(_CancelledReadInline, {"effect": "read", "content": "fake"})
        if mode == "capacity":
            from app.agent.kernel.state_types import StateUpdate
            await store.commit(lease, updates=(StateUpdate("metadata.filler", "z" * 220000),))
        assert await store.cancel_publication(owner="o", session_id="s", request_id="same")
        cold = sqlite_store() if backend == "sqlite" else store
        before = await cold.load(owner="o", session_id="s")
        if mode == "write_failure":
            if backend == "sqlite":
                assert isinstance(store, SQLiteKernelStore)
                original = store._write_state
                def fail(conn, state):
                    original(conn, state)
                    raise OSError("after SQL before commit")
                target, field, injected = store, "_write_state", fail
            else:
                import app.agent.kernel.state as module
                target, field, injected = module, "validate_execution_state", OSError("before assignment")
            with patch.object(target, field, side_effect=injected), pytest.raises(OSError):
                await store.settle_cancelled_reads((receipt,))
        elif mode == "capacity":
            with pytest.raises(ValueError):
                await store.settle_cancelled_reads((receipt,))
        else:
            settled = await store.settle_cancelled_reads((receipt,))
            assert settled == (mode == "success")
        after = await cold.load(owner="o", session_id="s")
        if mode == "success":
            before.conversation[2]["content"] = receipt._content
            assert not await store.settle_cancelled_reads((receipt,))
        assert after == before
        assert not await store.is_current(lease)
    with isolated_test_database():
        asyncio.run(run())


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("mode", ["both", "duplicate", "foreign", "unknown", "new_turn", "combined_capacity"])
def test_cancelled_read_batch_settlement_is_atomic(backend, mode):
    async def run():
        from dataclasses import replace

        from app.agent.kernel.conversation_codec import json_text
        from app.agent.kernel.state_types import (
            _CancelledReadInline,
            _ExecutionCapacity,
        )
        store = sqlite_store() if backend == "sqlite" else InMemorySessionStateStore()
        lease, _ = await store.begin_turn(owner="o", session_id="s", request_id="batch")
        calls = [{"call_id": f"read-{i}", "name": "library.inspect", "arguments": {}} for i in range(2)]
        conversation = [{"role": "assistant", "content": "", "tool_calls": calls}, *[
            {"role": "tool", "tool_call_id": call["call_id"], "tool_name": call["name"], "content": pending_content(True)}
            for call in calls]]
        state = await store.commit(lease, conversation=conversation, updates=(_ExecutionCapacity("", True),))
        receipts = [_CancelledReadInline.capture(lease, state.conversation, i, tool=READ_TOOL) for i in range(2)]
        payloads = [{"ok": True, "data": {"index": i, "value": "x" * (140000 if mode == "combined_capacity" else 10)}} for i in range(2)]
        for receipt, payload in zip(receipts, payloads, strict=True):
            receipt.put_once(json_text(payload))
        if mode == "duplicate":
            receipts[1] = receipts[0]
        elif mode == "foreign":
            foreign = _CancelledReadInline.capture(replace(lease, session_id="elsewhere"), state.conversation, 1, tool=READ_TOOL)
            foreign.put_once(json_text(payloads[1]))
            receipts[1] = foreign
        elif mode == "unknown":
            receipts[1] = _CancelledReadInline.capture(lease, state.conversation, 1, tool=READ_TOOL)
        assert await store.cancel_publication(owner="o", session_id="s", request_id="batch")
        if mode == "new_turn":
            await store.begin_turn(owner="o", session_id="s", request_id="new")
        cold = sqlite_store() if backend == "sqlite" else store
        before = await cold.load(owner="o", session_id="s")
        if mode == "combined_capacity":
            with pytest.raises(ValueError):
                await store.settle_cancelled_reads(tuple(receipts))
            assert await cold.load(owner="o", session_id="s") == before
        elif mode == "both":
            assert await store.settle_cancelled_reads(tuple(receipts))
            after = await cold.load(owner="o", session_id="s")
            assert [json.loads(row["content"]) for row in after.conversation[1:]] == payloads
            before.conversation = after.conversation
            assert after == before
            assert not await store.settle_cancelled_reads(tuple(receipts))
        else:
            assert not await store.settle_cancelled_reads(tuple(receipts))
            assert await cold.load(owner="o", session_id="s") == before
    with isolated_test_database():
        asyncio.run(run())
