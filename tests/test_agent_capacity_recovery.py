"""Lossless checkpoint capacity and recovery contracts at real storage/wire boundaries.

Fixtures are module-local; HTTP uses the existing MockTransport provider harness.
Ordinary-domain cases also run unchanged against revisions before phase support.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import random
import socket
from copy import deepcopy

import pytest

from app import database as db
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.projection import ToolOutcome
from app.agent.kernel.session import AgentSession
from app.agent.kernel.session_state import SessionState
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import AgentInput, StalePublicationError, StateUpdate
from tests.support import isolated_test_database
from tests.test_agent_provider_state import REDACTED, SIGNATURE, THINKING
from tests.test_agent_provider_text_authority import native_reply
from tests.test_agent_tool_argument_ingress import PROTOCOLS, Wire, encode

OWNER, SESSION = "checkpoint-owner", "checkpoint-session"
ASSISTANT = "逐项核对库存，不执行未经确认的写操作。"
B = 262144
T = 426007
STATUS_LENGTH = 162551


@pytest.fixture(autouse=True)
def offline(monkeypatch):

    def deny(*args, **kwargs):
        raise AssertionError("real sockets and DNS are forbidden")

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    for name in ("getaddrinfo", "create_connection", "gethostbyname"):
        monkeypatch.setattr(socket, name, deny)


def make_store():
    return SQLiteKernelStore(secret_provider=lambda: "started-checkpoint-synthetic")


async def reopened():
    return await make_store().load(owner=OWNER, session_id=SESSION)


def spec(name, handler):
    return KernelToolSpec(
        name=name,
        domain="library",
        description="inspect 库存 核对",
        effect=ToolEffect.READ,
        input_schema={
            "type": "object",
            "required": ["selector"],
            "properties": {
                "selector": {"type": "string"},
                "exclude_played": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        read=handler,
    )


def batch(protocol, calls):
    reply = native_reply(protocol, ASSISTANT)
    if protocol == "chat_completions":
        reply["choices"][0]["message"]["tool_calls"] = [
            {
                "id": cid,
                "type": "function",
                "function": {"name": name, "arguments": encode(args)},
            }
            for cid, name, args in calls
        ]
        reply["choices"][0]["finish_reason"] = "tool_calls"
    elif protocol == "responses":
        reply["output"].extend(
            (
                {
                    "type": "function_call",
                    "id": "fc_" + cid,
                    "call_id": cid,
                    "name": name,
                    "arguments": encode(args),
                }
                for cid, name, args in calls
            )
        )
    else:
        reply["content"].extend(
            (
                {"type": "tool_use", "id": cid, "name": name, "input": args}
                for cid, name, args in calls
            )
        )
        reply["stop_reason"] = "tool_use"
    return reply


def make_wire(protocol, replies):
    return Wire(
        protocol, [(encode(reply).encode(), "application/json") for reply in replies]
    )


def session(catalog, transport, *, state_store=None, limits=None):
    state_store = state_store if state_store is not None else make_store()
    return AgentSession(
        model=transport.adapter,
        catalog=catalog,

        pipeline=ToolPipeline(
            catalog=catalog, state_store=state_store, reference_store=state_store
        ),
        state_store=state_store,
        journal=state_store,
        limits=limits,
        system_prompt="按实际工具事实回答，未知不等于成功。",
    )


async def collect(active, request="initial"):
    return [
        event
        async for event in active.run(
            AgentInput(
                owner=OWNER,
                session_id=SESSION,
                request_id=request,
                message="inspect 核对库存，排除已看。",
            )
        )
    ]


def result_rows(state):
    return [row for row in state.conversation if row.get("role") == "tool"]


def assert_unknown(row, code="result_unknown"):
    value = json.loads(row["content"])
    assert value["code"] == code
    assert value["ok"] is False and value["status"] == "error"


def assert_native_wire(protocol, payload, calls):
    if protocol == "chat_completions":
        messages = payload["messages"]
        actual = [
            (c["id"], c["function"]["name"], json.loads(c["function"]["arguments"]))
            for m in messages
            for c in m.get("tool_calls", [])
        ]
        results = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
        assert (
            next((m["reasoning_content"] for m in messages if m.get("tool_calls")))
            == THINKING
        )
    elif protocol == "responses":
        messages = payload["input"]
        actual = [
            (m["call_id"], m["name"], json.loads(m["arguments"]))
            for m in messages
            if m.get("type") == "function_call"
        ]
        results = [
            m["call_id"] for m in messages if m.get("type") == "function_call_output"
        ]
        reasoning = next((m for m in messages if m.get("type") == "reasoning"))
        assert reasoning == {
            "type": "reasoning",
            "id": "rs_1",
            "encrypted_content": REDACTED,
            "summary": [{"type": "summary_text", "text": "Checking the actual result."}],
            "content": [{"type": "reasoning_text", "text": THINKING}],
        }
    else:
        blocks = [
            b
            for m in payload["messages"]
            if isinstance(m.get("content"), list)
            for b in m["content"]
        ]
        actual = [
            (b["id"], b["name"], b["input"])
            for b in blocks
            if b.get("type") == "tool_use"
        ]
        results = [b["tool_use_id"] for b in blocks if b.get("type") == "tool_result"]
        assert next((b for b in blocks if b.get("type") == "thinking")) == {
            "type": "thinking",
            "thinking": THINKING,
            "signature": SIGNATURE,
        }
        assert next((b for b in blocks if b.get("type") == "redacted_thinking")) == {
            "type": "redacted_thinking",
            "data": REDACTED,
        }
    assert actual == calls
    assert results == [cid for cid, _, _ in calls]


def test_two_real_turns_preserve_supported_small_read_after_large_storable_result():

    async def exercise():
        handled = []

        def read(a, c):
            handled.append(a["selector"])
            if a["selector"] == "old":
                return ToolOutcome(
                    model_content=encode({"ok": True, "status": "S" * STATUS_LENGTH}),
                    public_content={
                        "ok": True,
                        "status": "success",
                        "summary": "old observed",
                    },
                )
            return {"ok": True, "status": "success", "summary": "ok", "data": {}}

        tool = spec("inspect_batch", read)
        catalog = ToolCatalog([tool])
        old_calls = [("old", tool.name, {"selector": "old"})]
        calls = [("new", tool.name, {"selector": "new"})]
        old_transport = make_wire(
            "chat_completions",
            [
                batch("chat_completions", old_calls),
                native_reply("chat_completions", "ok", private=False),
            ],
        )
        first_events = await collect(session(catalog, old_transport), "old-request")
        initial = await reopened()
        old_rows = [r for r in initial.conversation if r.get("tool_call_id") == "old"]
        assert first_events[-1].payload.get("status") == "success"
        assert len(old_rows) == 1 and json.loads(old_rows[0]["content"]) == {
            "ok": True,
            "status": "S" * STATUS_LENGTH,
        }
        old_transport.verify(2)
        transport = make_wire(
            "chat_completions",
            [
                batch("chat_completions", calls),
                native_reply("chat_completions", "ok", private=False),
            ],
        )
        events = await collect(
            session(catalog, transport, state_store=make_store()), "new-request"
        )
        final = await reopened()
        assert handled == ["old", "new"], (
            "an already-supported tiny follow-up must still run"
        )
        assert events[-1].payload.get("status") == "success"
        assert [
            r for r in final.conversation if r.get("tool_call_id") == "old"
        ] == old_rows
        new_rows = [r for r in final.conversation if r.get("tool_call_id") == "new"]
        assert len(new_rows) == 1 and json.loads(new_rows[0]["content"])["ok"] is True
        assert_native_wire(
            "chat_completions", transport.requests[-1], old_calls + calls
        )
        transport.verify(2)

    with isolated_test_database():
        asyncio.run(exercise())


@pytest.mark.parametrize(
    "field,value",
    [
        ("summary", "s" * 8001),
        ("recent_refs", [str(i) for i in range(101)]),
        ("generation", -1),
    ],
)
def test_clone_is_a_copy_not_normalization(field, value):
    state = SessionState(owner="o", session_id="s")
    setattr(state, field, value)
    cloned = state.clone()
    assert cloned == state


@pytest.mark.parametrize("budget,pad", [(32768, 20000), (400000, 270000)])
def test_explicit_store_budget_no_exception_after_successful_commit(budget, pad):

    async def exercise():
        store = SQLiteKernelStore(
            secret_provider=lambda: "started-checkpoint-synthetic",
            max_state_bytes=budget,
        )
        lease, _ = await store.begin_turn(
            owner=OWNER, session_id=SESSION, request_id="custom"
        )
        error = None
        try:
            await store.commit(lease, updates=(StateUpdate("metadata.pad", "p" * pad),))
        except Exception as exc:
            error = repr(exc)
        cold = await store.load(owner=OWNER, session_id=SESSION)
        with db.get_conn() as conn:
            raw = conn.execute(
                "SELECT state_json FROM agent_kernel_sessions"
            ).fetchone()[0]
        assert error is None, (
            "legal explicit budget persisted successfully, then clone raised"
        )
        assert len(raw.encode()) <= budget
        assert cold.metadata["pad"] == "p" * pad

    with isolated_test_database():
        asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["sqlite", "memory"])
def test_normal_updates_clone_and_aliasing_control(kind):

    async def exercise():
        store = make_store() if kind == "sqlite" else InMemorySessionStateStore()
        lease, _ = await store.begin_turn(
            owner=OWNER, session_id=SESSION, request_id="normal"
        )
        state = await store.commit(
            lease,
            conversation=[{"role": "user", "content": "ok"}],
            updates=(
                StateUpdate("summary", "s" * 8001),
                StateUpdate("recent_refs", [str(i) for i in range(101)]),
                StateUpdate("metadata.peer", {"a": [1]}),
            ),
        )
        assert len(state.summary) == 8000 and len(state.recent_refs) == 100
        assert state.generation == lease.generation
        state.metadata["peer"]["a"].append(2)
        assert (await store.load(owner=OWNER, session_id=SESSION)).metadata["peer"] == {
            "a": [1]
        }

    with isolated_test_database():
        asyncio.run(exercise())


def active(catalog, wire, store):
    return AgentSession(
        model=wire.adapter,
        catalog=catalog,

        pipeline=ToolPipeline(
            catalog=catalog, state_store=store, reference_store=make_store()
        ),
        state_store=store,
        # Guarded publication must share the authoritative session store.
        # Memory uses the supported no-journal mode, not an unrelated SQLite row.
        journal=store if isinstance(store, SQLiteKernelStore) else None,
        system_prompt="未知不等于完成",
    )


@pytest.mark.parametrize("kind", ["sqlite", "memory"])
@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("large_metadata", [True])
def test_cold_explanation_preserves_unknown_across_generations_and_new_read(
    kind, protocol, large_metadata
):
    from app.agent.kernel.conversation_codec import PendingText

    async def exercise():
        store = make_store() if kind == "sqlite" else InMemorySessionStateStore()

        def reopen():
            return make_store() if kind == "sqlite" else store

        handled = []

        async def read(args, context):
            handled.append(args["selector"])
            before = await reopen().load(owner=OWNER, session_id=SESSION)
            assert before.execution_capacity
            if args["selector"] == "old":
                if large_metadata:
                    return ToolOutcome(
                        model_content="{}",
                        public_content={"ok": True, "status": "success"},
                        state_updates=(StateUpdate("metadata.pad", "p" * 270000),),
                    )
                return ToolOutcome(
                    model_content=encode({"ok": True, "status": "S" * 180000}),
                    public_content={"ok": True, "status": "success"},
                )
            return ToolOutcome(
                model_content="{}",
                public_content={"ok": True, "status": "success"},
                state_updates=(StateUpdate("metadata.pad", None, mode="delete"),),
            )

        tool = spec("inspect_batch", read)
        catalog = ToolCatalog([tool])
        calls = [("old", tool.name, {"selector": "old"})]
        wire = make_wire(protocol, [batch(protocol, calls)])
        events = await collect(active(catalog, wire, store))
        assert events[-1].payload.get("status") != "success"
        saved = await reopen().load(owner=OWNER, session_id=SESSION)
        assert saved.execution_capacity
        prefix = saved.conversation
        assert isinstance(result_rows(saved)[0]["content"], PendingText)
        assert_unknown(result_rows(saved)[0])
        generation = saved.generation
        for turn in range(3):
            cold = reopen()
            explain = make_wire(
                protocol,
                [native_reply(protocol, "我宣称旧任务已经恢复并完成", private=False)],
            )
            result = await collect(
                active(catalog, explain, cold), "explain-" + str(turn)
            )
            explain.verify(1)
            assert_native_wire(protocol, explain.requests[0], calls)
            current = await reopen().load(owner=OWNER, session_id=SESSION)
            assert current.generation == generation + turn + 1
            assert current.conversation[: len(prefix)] == prefix
            assert isinstance(result_rows(current)[0]["content"], PendingText)
            assert_unknown(result_rows(current)[0])
            assert current.execution_capacity is large_metadata
            assert handled == ["old"]
            if large_metadata:
                assert current.metadata["pad"] == "p" * 270000
                assert result[-1].payload.get("status") != "success"
            else:
                assert result[-1].payload["status"] == "success"
                assert (
                    len(
                        json.dumps(
                            current.to_payload(),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode()
                    )
                    <= 262144
                )
        new_calls = [("new", tool.name, {"selector": "new"})]
        fresh = make_wire(
            protocol,
            [
                batch(protocol, new_calls),
                native_reply(protocol, "新检查完成，旧结果未知", private=False),
            ],
        )
        result = await collect(active(catalog, fresh, reopen()), "explicit-new-read")
        fresh.verify(2)
        current = await reopen().load(owner=OWNER, session_id=SESSION)
        assert result[-1].payload["status"] == "success"
        assert not current.execution_capacity and "pad" not in current.metadata
        assert current.conversation[: len(prefix)] == prefix
        assert_unknown(result_rows(current)[0])
        assert handled == ["old", "new"]
        assert (
            len(
                json.dumps(
                    current.to_payload(), ensure_ascii=False, separators=(",", ":")
                ).encode()
            )
            <= 262144
        )
        wire.verify(1)

    with isolated_test_database():
        asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["sqlite", "memory"])
def test_unstorable_new_user_request_cannot_evict_original_checkpoint(kind):
    from app.agent.kernel.conversation_codec import pending_content
    from app.agent.kernel.state_types import _ExecutionCapacity

    async def exercise():
        store = make_store() if kind == "sqlite" else InMemorySessionStateStore()
        lease, _ = await store.begin_turn(
            owner=OWNER, session_id=SESSION, request_id="seed"
        )
        rows = [
            {"role": "user", "content": "original"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "call_id": "old",
                        "name": "inspect_batch",
                        "arguments": {"selector": "old"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "old",
                "tool_name": "inspect_batch",
                "content": pending_content(True),
            },
        ]
        saved = await store.commit(lease, conversation=rows)
        with pytest.raises(ValueError, match="持久化上限"):
            await store.commit(
                lease, conversation=rows + [{"role": "user", "content": "x" * (B + 1)}],
                updates=(_ExecutionCapacity("", False),),
            )
        cold = make_store() if kind == "sqlite" else store
        assert await cold.load(owner=OWNER, session_id=SESSION) == saved

    with isolated_test_database():
        asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["sqlite", "memory"])
def test_crash_cold_read_bound_plus_one_exit_and_no_replay(kind):
    from app.agent.kernel.conversation_codec import pending_content
    from app.agent.kernel.state_types import _ExecutionCapacity

    ROWS = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "call_id": "c",
                    "name": "inspect_batch",
                    "arguments": {"selector": "x"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "c",
            "tool_name": "inspect_batch",
            "content": pending_content(True),
        },
    ]

    async def exercise():
        store = make_store() if kind == "sqlite" else InMemorySessionStateStore()
        # Publication request identity is now persisted inside the byte cap.
        # Equal-width IDs isolate generation recovery from payload growth.
        lease, _ = await store.begin_turn(
            owner=OWNER, session_id=SESSION, request_id="cold-old"
        )
        state = await store.commit(
            lease, conversation=ROWS, updates=(_ExecutionCapacity("", True),)
        )
        payload = state.to_payload()
        payload["metadata"]["pad"] = ""
        empty = len(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        )
        pad = "x" * (T - empty)
        await store.commit(lease, updates=(StateUpdate("metadata.pad", pad),))
        cold_store = make_store() if kind == "sqlite" else store
        cold = await cold_store.load(owner=OWNER, session_id=SESSION)
        assert cold.execution_capacity and cold.metadata["pad"] == pad
        assert (
            len(
                json.dumps(
                    cold.to_payload(), ensure_ascii=False, separators=(",", ":")
                ).encode()
            )
            == T
        )
        with pytest.raises(ValueError):
            await store.commit(lease, updates=(StateUpdate("metadata.pad", pad + "x"),))
        with pytest.raises(ValueError):
            await store.commit(lease, updates=(_ExecutionCapacity("", False),))
        # A longer publication identity cannot evict facts or exceed T.
        with pytest.raises(ValueError):
            await cold_store.begin_turn(
                owner=OWNER, session_id=SESSION, request_id="cold-new-longer"
            )
        assert await cold_store.load(owner=OWNER, session_id=SESSION) == cold
        assert await cold_store.is_current(lease)
        new_lease, admitted = await cold_store.begin_turn(
            owner=OWNER, session_id=SESSION, request_id="cold-new"
        )
        assert admitted.execution_capacity and admitted.conversation == ROWS
        assert await cold_store.is_current(new_lease)
        assert not await cold_store.is_current(lease)
        with pytest.raises(StalePublicationError):
            await store.commit(lease, conversation=[])
        cold = await cold_store.load(owner=OWNER, session_id=SESSION)
        assert cold.generation == lease.generation + 1 and cold.metadata["pad"] == pad
        assert cold.conversation[-1]["content"] == pending_content(True)
        with pytest.raises(ValueError):
            await cold_store.commit(new_lease, updates=(_ExecutionCapacity("", False),))
        assert await cold_store.load(owner=OWNER, session_id=SESSION) == cold
        resolved = await cold_store.commit(
            new_lease,
            updates=(
                StateUpdate("metadata.pad", None, mode="delete"),
                _ExecutionCapacity("", False),
            ),
        )
        assert not resolved.execution_capacity
        assert resolved.conversation == ROWS
        assert resolved.conversation[-1]["content"] == pending_content(True)
        assert len(json.dumps(resolved.to_payload()).encode()) <= B

    with isolated_test_database():
        asyncio.run(exercise())


def test_business_collision_cannot_authorize_expansion():

    async def exercise():
        store = make_store()
        lease, _ = await store.begin_turn(
            owner=OWNER, session_id=SESSION, request_id="collision"
        )
        state = await store.commit(
            lease,
            updates=(
                StateUpdate("execution_capacity", True),
                StateUpdate("metadata.execution_capacity", 1),
            ),
        )
        assert not state.execution_capacity
        with pytest.raises(ValueError):
            await store.commit(lease, updates=(StateUpdate("metadata.pad", "x" * B),))
        cold = await reopened()
        assert cold.metadata == {
            "execution_capacity": 1,
            "active_publication": {
                "generation": lease.generation,
                "turn_id": lease.turn_id,
                "request_id": "collision",
                "status": "running",
            },
        }
        assert not cold.execution_capacity

    with isolated_test_database():
        asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["sqlite", "memory"])
def test_callback_exception_is_atomic_and_clone_alias_graph(kind):

    async def exercise():
        store = make_store() if kind == "sqlite" else InMemorySessionStateStore()
        lease, _ = await store.begin_turn(
            owner=OWNER, session_id=SESSION, request_id="callback"
        )
        await store.commit(lease, updates=(StateUpdate("metadata.peer", {"x": [1]}),))
        error = RuntimeError("callback sentinel")

        def change(state):
            state.metadata["peer"]["x"].append(2)
            raise error

        with pytest.raises(RuntimeError) as caught:
            await store.update_effect_state(
                owner=OWNER, session_id=SESSION, change=change
            )
        assert caught.value is error
        assert (await store.load(owner=OWNER, session_id=SESSION)).metadata["peer"] == {
            "x": [1]
        }

    with isolated_test_database():
        asyncio.run(exercise())
    shared = []
    state = SessionState(
        owner="o", session_id="s", conversation=shared, metadata={"shared": shared}
    )
    copy = state.clone()
    assert (
        copy.conversation is copy.metadata["shared"] and copy.conversation is not shared
    )


def paired(content):
    return [
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"call_id": "x", "name": "inspect_batch", "arguments": {}}],
        },
        {
            "role": "tool",
            "content": content,
            "tool_call_id": "x",
            "tool_name": "inspect_batch",
        },
    ]


@pytest.mark.parametrize("content", ["{}", '{"v":1,"r":[],"pending":1}'])
def test_literal_content_never_becomes_pending(content):
    from app.agent.kernel.conversation_codec import (
        conversation_cost,
        conversation_payload,
        conversation_rows,
        json_text,
    )

    rows = paired(content)
    actual = conversation_rows(json.loads(json_text(conversation_payload(rows))))
    assert actual == rows
    assert type(actual[-1]["content"]) is str
    assert conversation_cost(rows) <= len(json_text(rows).encode())


@pytest.mark.parametrize("kind", ["sqlite", "memory"])
def test_authoritative_roundtrip_aliasing_cas_atomic_updates_receipt_merge(kind):
    from app.agent.kernel.conversation_codec import PendingText, pending_content

    async def exercise():
        store = make_store() if kind == "sqlite" else InMemorySessionStateStore()
        lease, _ = await store.begin_turn(
            owner=OWNER, session_id=SESSION, request_id="x"
        )
        rows = paired(pending_content(True))
        receipt = {
            "role": "assistant",
            "content": "receipt",
            "completion_receipt_id": "plan",
        }
        updates = (
            StateUpdate("metadata.peer", {"one": [1, True, None, "😀"]}),
            StateUpdate(
                "metadata.effect_waits",
                {"plan": {"receipt_message": receipt, "delivered": True}},
            ),
        )
        state = await store.commit(lease, conversation=rows, updates=updates)
        history_id = state.conversation[0]["history_id"]
        assert isinstance(history_id, str) and history_id
        assert "history_id" not in rows[0], "commit must not mutate caller-owned rows"
        committed_rows = deepcopy(rows)
        committed_rows[0]["history_id"] = history_id
        assert state.conversation == committed_rows
        state.conversation[0]["content"] = "mutation"
        state.conversation[0]["history_id"] = "mutated-returned-id"
        cold = make_store() if kind == "sqlite" else store
        loaded = await cold.load(owner=OWNER, session_id=SESSION)
        assert loaded.conversation == committed_rows
        assert isinstance(loaded.conversation[-1]["content"], PendingText)
        assert loaded.metadata["peer"] == {"one": [1, True, None, "😀"]}
        before = loaded.clone()
        if kind == "sqlite":
            with pytest.raises(ValueError):
                await store.commit(
                    lease, updates=(StateUpdate("metadata.too_large", "X" * 270000),)
                )
        else:
            await store.commit(
                lease, updates=(StateUpdate("metadata.too_large", "X" * 270000),)
            )
            expanded = await cold.load(owner=OWNER, session_id=SESSION)
            assert expanded.metadata["too_large"] == "X" * 270000
            expected = before.clone()
            expected.metadata["too_large"] = "X" * 270000
            assert expanded == expected
            await store.commit(
                lease, updates=(StateUpdate("metadata.too_large", None, mode="delete"),)
            )
        assert await cold.load(owner=OWNER, session_id=SESSION) == before
        lease2, after = await cold.begin_turn(
            owner=OWNER, session_id=SESSION, request_id="y"
        )
        assert after.conversation == committed_rows + [receipt]
        assert "effect_waits" not in after.metadata
        with pytest.raises(StalePublicationError):
            await store.commit(lease, conversation=[])
        assert await cold.is_current(lease2)
        assert not (await cold.load(owner="other", session_id=SESSION)).conversation

    with isolated_test_database():
        asyncio.run(exercise())


def test_legacy_sqlite_hmac_cold_reopen_and_lazy_rewrite():
    from app.agent.kernel.conversation_codec import pending_content

    async def exercise():
        store = make_store()
        lease, _ = await store.begin_turn(
            owner=OWNER, session_id=SESSION, request_id="legacy"
        )
        rows = paired(str(pending_content(True)))
        od, sd = store._scope(OWNER, SESSION)
        payload = SessionState(owner=OWNER, session_id=SESSION).to_payload()
        payload["conversation"] = rows
        domain = f"state:v1:{od}:{sd}:{lease.generation}".encode()
        encoded, signature = store._encode(
            payload, domain=domain, maximum=store.max_state_bytes
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_kernel_sessions SET state_json=?,state_hmac=? WHERE owner_digest=? AND session_digest=?",
                (encoded, signature, od, sd),
            )
        loaded = await reopened()
        assert loaded.conversation == rows
        assert type(loaded.conversation[-1]["content"]) is str
        with db.get_conn() as conn:
            persisted = conn.execute(
                "SELECT state_json,state_hmac FROM agent_kernel_sessions"
            ).fetchone()
        assert tuple(persisted) == (encoded, signature), "legacy load must not rewrite signed data"
        committed = await make_store().commit(lease, conversation=rows)
        history_id = committed.conversation[0]["history_id"]
        assert isinstance(history_id, str) and history_id
        assert "history_id" not in rows[0], "lazy rewrite must not mutate legacy input rows"
        committed_rows = deepcopy(rows)
        committed_rows[0]["history_id"] = history_id
        assert committed.conversation == committed_rows
        with db.get_conn() as conn:
            raw = conn.execute(
                "SELECT state_json FROM agent_kernel_sessions"
            ).fetchone()[0]
        assert json.loads(raw)["conversation"][0] == 1
        rewritten = await reopened()
        assert rewritten.conversation == committed_rows
        assert type(rewritten.conversation[-1]["content"]) is str
        with db.get_conn() as conn:
            conn.execute("UPDATE agent_kernel_sessions SET state_hmac='bad'")
        with pytest.raises(ValueError):
            await reopened()

    with isolated_test_database():
        asyncio.run(exercise())


@pytest.mark.parametrize("phase", [True, "1", 2])
def test_signed_invalid_phase_rejected_without_rewrite(phase):

    async def exercise():
        store = make_store()
        lease, state = await store.begin_turn(
            owner=OWNER, session_id=SESSION, request_id="phase"
        )
        payload = store._state_payload(state)
        payload["execution_capacity"] = phase
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        o, s = store._scope(OWNER, SESSION)
        domain = f"state:v1:{o}:{s}:{lease.generation}".encode()
        signature = hmac.new(
            store._secret(), domain + b"\x00" + raw.encode(), hashlib.sha256
        ).hexdigest()
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_kernel_sessions SET state_json=?,state_hmac=?",
                (raw, signature),
            )
        with pytest.raises(ValueError, match="invalid execution capacity phase"):
            await reopened()
        with db.get_conn() as conn:
            assert (
                conn.execute("SELECT state_json FROM agent_kernel_sessions").fetchone()[
                    0
                ]
                == raw
            )

    with isolated_test_database():
        asyncio.run(exercise())


@pytest.mark.parametrize("result_status,diagnostic_length", [("running", 0), ("partial", 180_000)])
def test_unfinished_confirmation_preserves_capacity_and_receipt_without_replay(
    result_status, diagnostic_length,
):
    """Real Session read -> preview -> explicit confirm, inspect terminal paths."""

    async def exercise():
        writes, reads = [], []
        read = spec(
            "inspect_batch",
            lambda a, c: reads.append(a["selector"]) or ToolOutcome(
                model_content=encode({"ok": True, "status": "S" * diagnostic_length})
                    if a["selector"] == "diagnose" and diagnostic_length else "{}",
                public_content={"ok": True, "status": "success"},
                state_updates=(StateUpdate("metadata.pad", "p" * 270000),),
            ),
        )
        write = KernelToolSpec(
            name="inspect_change",
            domain="library",
            description="inspect 核对库存 变更",
            input_schema=read.input_schema,
            effect=ToolEffect.WRITE,
            prepare=lambda a, c: PreparedEffect(
                preview={"summary": "等待确认"}, snapshot_fingerprint="frozen"
            ),
            execute_confirmed=lambda a, fp, c: (
                writes.append(a)
                or {"ok": True, "status": result_status, "summary": "receipt"}
            ),
        )
        calls = [
            ("r", read.name, {"selector": "r"}),
            ("w", write.name, {"selector": "w"}),
        ]
        wire = make_wire(
            "chat_completions",
            [
                batch("chat_completions", calls),
                batch("chat_completions", [("diagnose", read.name, {"selector": "diagnose"})]),
                native_reply("chat_completions", "完成", private=False),
            ],
        )
        active = session(ToolCatalog([read, write]), wire)
        preview = await collect(active)
        assert preview[-1].payload["status"] == "approval_required"
        assert not any(e.payload.get("code") == "internal_error" for e in preview)
        saved = await reopened()
        assert (
            saved.pending_effect_plan_id and saved.execution_capacity and (writes == [])
        )
        confirmed = [
            e
            async for e in active.confirm(
                owner=OWNER, session_id=SESSION, plan_id=saved.pending_effect_plan_id
            )
        ]
        current = await reopened()
        assert isinstance(active.state_store, SQLiteKernelStore)
        size = len(
            json.dumps(
                active.state_store._state_payload(current),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        )
        duplicate = [
            e
            async for e in active.confirm(
                owner=OWNER, session_id=SESSION, plan_id=saved.pending_effect_plan_id
            )
        ]
        assert writes == [{"selector": "w"}]
        assert duplicate[-1].payload.get("code") == "confirmation_invalid"
        assert current.execution_capacity and B < size <= T
        assert current.metadata["pad"] == "p" * 270000
        # N01 按未完成的真实回执收尾，不能用模型 stop 覆盖 running/partial。
        assert confirmed[-1].payload["finish_reason"] == result_status
        assert confirmed[-1].payload["status"] == "partial"
        assert reads == ["r", "diagnose"], "续接诊断不得提前撤掉同一执行组的有界容量"
        assert "internal_error" not in str(confirmed[-1].payload)
        receipts = [
            row
            for row in current.conversation
            if row.get("completion_receipt_id") == saved.pending_effect_plan_id
        ]
        assert len(receipts) == 1
        assert "receipt" in receipts[0]["content"]
        if diagnostic_length:
            assert not any(row.get("tool_call_id") == "diagnose" for row in current.conversation)
            assert not any(call["call_id"] == "diagnose" for row in current.conversation
                           for call in row.get("tool_calls", ()))
        assert await reopened() == current
        wire.verify(3)

    with isolated_test_database():
        asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["sqlite", "memory"])
def test_diagnostic_retention_preserves_full_receipt_and_pending_calls(kind):
    from app.agent.kernel.conversation_codec import pending_content
    from app.agent.kernel.state_types import HistoryCapacityError, _ExecutionCapacity

    async def exercise():
        store = make_store() if kind == "sqlite" else InMemorySessionStateStore()
        lease, _ = await store.begin_turn(owner=OWNER, session_id=SESSION, request_id="receipt")
        receipt = {"role": "assistant", "content": "可信回执：" + "R" * 12_000 + "END_PARTIAL",
                   "tool_name": "inspect_change", "effect_plan_id": "plan", "completion_receipt_id": "plan"}
        initial = await store.commit(lease, conversation=[
            {"role": "user", "content": "原始目标，不重放写入"},
            {"role": "assistant", "tool_calls": [{"call_id": "write", "name": "inspect_change", "arguments": {}}]},
            {"role": "tool", "tool_call_id": "write", "tool_name": "inspect_change",
             "effect_plan_id": "plan", "content": '{"ok":true,"status":"partial"}'},
            receipt,
        ], updates=(_ExecutionCapacity("", True, current_start=0), StateUpdate("metadata.pad", "p" * 270_000)))
        diagnostic = [
            {"role": "assistant", "tool_calls": [{"call_id": "diagnose", "name": "inspect_batch", "arguments": {}}]},
            {"role": "tool", "tool_call_id": "diagnose", "tool_name": "inspect_batch",
             "content": encode({"ok": True, "status": "S" * 180_000})},
        ]
        update = _ExecutionCapacity("", True, current_start=0, receipt_plan_id="plan")
        saved = await store.commit(lease, conversation=initial.conversation + diagnostic, updates=(update,))
        retained = deepcopy(initial.conversation)
        retained[0]["history_before_truncated"] = True
        assert saved.conversation == retained
        assert saved.conversation[-1] == receipt
        assert saved.metadata["pad"] == initial.metadata["pad"]
        assert saved.execution_capacity and B < len(encode(saved.to_payload()).encode()) <= T
        cold = make_store() if kind == "sqlite" else store
        assert await cold.load(owner=OWNER, session_id=SESSION) == saved
        # 活跃调用不能按已完成诊断淘汰；过大的可信回执也不能被截成 8k 或丢弃。
        pending = deepcopy(diagnostic)
        pending_call = pending[0]["tool_calls"][0]
        assert isinstance(pending_call, dict)
        pending_call["arguments"] = {"selector": "x" * 180_000}
        pending[1]["content"] = pending_content(True)
        oversized_receipt = {**receipt, "content": "R" * 180_000}
        for rows in (saved.conversation + pending, saved.conversation[:-1] + [oversized_receipt]):
            with pytest.raises(HistoryCapacityError):
                await store.commit(lease, conversation=rows, updates=(update,))
            assert await cold.load(owner=OWNER, session_id=SESSION) == saved

    with isolated_test_database():
        asyncio.run(exercise())


def test_joint_high_entropy_old_metadata_is_deleted_only_by_handler():
    def text(seed, length):
        rng = random.Random(seed)
        return "".join(
            rng.choice(
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
            )
            for _ in range(length)
        )

    async def exercise():
        pad = text(8129, 261300)
        opaque = text(9147, 100000)
        rng = random.Random(7193)
        selector = "".join(rng.choice("abcdef0123456789") for _ in range(12000))
        state_store = make_store()
        lease, _ = await state_store.begin_turn(
            owner=OWNER, session_id=SESSION, request_id="seed"
        )
        await state_store.commit(lease, updates=(StateUpdate("metadata.pad", pad),))
        assert (await reopened()).metadata["pad"] == pad
        handled = []

        async def read(arguments, context):
            handled.append(deepcopy(arguments))
            # The old metadata is not discarded speculatively to admit the call.
            assert (await reopened()).metadata["pad"] == pad
            return ToolOutcome(
                model_content="{}",
                public_content={"ok": True, "status": "success"},
                state_updates=(StateUpdate("metadata.pad", None, mode="delete"),),
            )

        tool = spec("inspect_batch", read)
        calls = [("delete-pad", tool.name, {"selector": selector})]
        reply = batch("responses", calls)
        for block in reply["output"]:
            if block.get("type") == "reasoning":
                block["encrypted_content"] = opaque
        transport = make_wire(
            "responses", [reply, native_reply("responses", "ok", private=False)]
        )
        events = await collect(
            session(ToolCatalog([tool]), transport, state_store=state_store)
        )
        cold = await reopened()
        assert handled == [{"selector": selector}]
        assert events[-1].payload["status"] == "success"
        assert "pad" not in cold.metadata
        caller = next(row for row in cold.conversation if row.get("tool_calls"))
        assert caller["tool_calls"] == [
            {
                "call_id": "delete-pad",
                "name": tool.name,
                "arguments": {"selector": selector},
            }
        ]
        assert opaque in caller["provider_state"]["layout_json"]
        assert len(encode(cold.conversation).encode()) <= 163840
        wire_calls = [
            row
            for row in transport.requests[-1]["input"]
            if row.get("type") == "function_call"
        ]
        assert json.loads(wire_calls[0]["arguments"]) == {"selector": selector}
        assert (
            next(
                row["encrypted_content"]
                for row in transport.requests[-1]["input"]
                if row.get("type") == "reasoning"
            )
            == opaque
        )
        with db.get_conn() as conn:
            raw = conn.execute(
                "SELECT state_json FROM agent_kernel_sessions"
            ).fetchone()[0]
        assert len(raw.encode()) <= B
        transport.verify(2)

    with isolated_test_database():
        asyncio.run(exercise())


def test_ordinary_memory_api_accepts_large_metadata_without_new_policy():
    async def exercise():
        state_store = InMemorySessionStateStore()
        lease, before = await state_store.begin_turn(
            owner=OWNER, session_id=SESSION, request_id="legacy"
        )
        expected = before.clone()
        expected.metadata["pad"] = "x" * 270000
        actual = await state_store.commit(
            lease, updates=(StateUpdate("metadata.pad", "x" * 270000),)
        )
        assert actual == expected
        assert await state_store.load(owner=OWNER, session_id=SESSION) == expected
        assert (
            await state_store.commit(
                lease, updates=(StateUpdate("metadata.pad", None, mode="delete"),)
            )
            == before
        )

    asyncio.run(exercise())
