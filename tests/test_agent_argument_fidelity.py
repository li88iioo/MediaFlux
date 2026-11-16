"""Request arguments are not result summaries; input admission precedes handlers."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.session import SessionLimits
from tests.support import isolated_test_database
from tests.test_agent_capacity_recovery import (
    PROTOCOLS, assert_native_wire, batch, collect, make_wire, reopened,
    result_rows, session, spec,
)
from tests.test_agent_provider_text_authority import native_reply
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414


def text_reply(protocol):
    return native_reply(protocol, "已核对", private=False)


def declaration(handler):
    return replace(spec("inspect_args", handler), input_schema={
        "type": "object", "additionalProperties": True,
    })


def arguments():
    return {
        "clauses": [{"label": "L" * 79 + str(i), "values": ["条件" * 40 + str(i)]} for i in range(18)],
        "zero": 0, "flag": False, "optional": None,
        "truncated": "business-value", "truncation": ["not-result-metadata"],
        "read_tool": "business-reader", "result_handle": "business-handle",
        # 真实回归：确认后历史把两个不同云端路径改成同一句占位符，核验连续失败。
        "paths": ["/验收/inbox/Work A", "/验收/inbox/Work B"],
        "operations": [{"op": "relocate", "target_path": "/验收/library/Work A", "new_name": "final-a.txt"}],
    }


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("pressure", ["storage", "model"])
def test_retained_arguments_are_exact_after_cold_read(agent_llm_enabled, protocol, pressure):
    async def exercise():
        handled = []
        args = arguments()
        def handler(value, context):
            handled.append(deepcopy(value))
            return {"data": {"description": "文" * 15000}} if "padding" in value else {"data": {"fact": "FIRST_FACT"}}
        catalog = ToolCatalog([declaration(handler)])
        calls = [("args", "inspect_args", args)] + [
            (f"pad-{i}", "inspect_args", {"padding": i})
            for i in range(4 if pressure == "storage" else 1)
        ]
        wire = make_wire(protocol, [batch(protocol, calls), text_reply(protocol)])
        events = await collect(session(catalog, wire))
        assert events[-1].payload["status"] == "success"
        state = await reopened()
        stored = [(c["call_id"], c["name"], c["arguments"]) for row in state.conversation for c in row.get("tool_calls", [])]
        # Check storage separately, not merely a live request containing original args.
        assert stored == calls
        native = [r["provider_state"] for r in state.conversation if r.get("tool_calls")]
        second = make_wire(protocol, [text_reply(protocol)])
        limits = SessionLimits(context_window_tokens=16384, max_output_tokens=128) if pressure == "model" else None
        await collect(session(catalog, second, limits=limits), "cold")
        assert_native_wire(protocol, second.requests[0], calls)
        assert [r["provider_state"] for r in (await reopened()).conversation if r.get("tool_calls")] == native
        assert handled == [c[2] for c in calls]
    with isolated_test_database():
        asyncio.run(exercise())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("prefix", [False, True])
def test_nonfitting_request_is_rejected_without_losing_prefix(agent_llm_enabled, protocol, prefix):
    async def exercise():
        handled, snapshots = [], []
        async def handler(value, context):
            # Cold read from inside the handler proves started came first.
            snapshots.append(await reopened())
            handled.append(deepcopy(value))
            return {"data": {"fact": "FIRST_FACT", "ordinal": value["ordinal"]}}
        catalog = ToolCatalog([declaration(handler)])
        previous = [(f"prior-{i}", "inspect_args", {"literal": chr(75+i)*30000, "ordinal": i}) for i in range(3)] if prefix else []
        rejected = [(f"wide-{i}", "inspect_args", {"literal": chr(65+i)*30000, "ordinal": i}) for i in range(3 if prefix else 6)]
        replies = ([batch(protocol, previous)] if prefix else []) + [batch(protocol, rejected), text_reply(protocol)]
        wire = make_wire(protocol, replies)
        events = await collect(session(catalog, wire))
        assert handled == [c[2] for c in previous]
        assert "history_capacity_exceeded" in repr(events)
        state = await reopened()
        assert [(c["call_id"], c["name"], c["arguments"]) for row in state.conversation for c in row.get("tool_calls", [])] == previous
        for index, snapshot in enumerate(snapshots):
            rows = result_rows(snapshot)
            assert [r["tool_call_id"] for r in rows] == [c[0] for c in previous]
            for position, row in enumerate(rows):
                content = json.loads(row["content"])
                if position < index:
                    assert content["data"]["fact"] == "FIRST_FACT"
                else:
                    assert content["code"] == ("result_unknown" if position == index else "not_executed")
            assert [(c["call_id"], c["name"], c["arguments"]) for row in snapshot.conversation for c in row.get("tool_calls", [])] == previous
        second = make_wire(protocol, [text_reply(protocol)])
        await collect(session(catalog, second), "cold")
        if previous:
            assert_native_wire(protocol, second.requests[0], previous)
            assert all("FIRST_FACT" in r["content"] for r in result_rows(state))
        assert handled == [c[2] for c in previous]
    with isolated_test_database():
        asyncio.run(exercise())


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_result_growth_preserves_started_checkpoint_and_completed_prefix(agent_llm_enabled, protocol):
    async def exercise():
        handled = []
        calls = [(f"grow-{i}", "inspect_args", {"literal": chr(65+i)*30000, "ordinal": i}) for i in range(5)]
        def handler(value, context):
            handled.append(deepcopy(value))
            return {"data": {"fact": "FIRST_FACT"}} if value["ordinal"] == 0 else {"data": {"description": "文"*15000}}
        catalog = ToolCatalog([declaration(handler)])
        wire = make_wire(protocol, [batch(protocol, calls), text_reply(protocol)])
        events = await collect(session(catalog, wire))
        state = await reopened()
        assert handled == [c[2] for c in calls[:2]]
        assert "history_capacity_exceeded" in repr(events)
        assert [(c["call_id"], c["name"], c["arguments"]) for row in state.conversation for c in row.get("tool_calls", [])] == calls
        rows = result_rows(state)
        assert json.loads(rows[0]["content"])["data"]["fact"] == "FIRST_FACT"
        assert [json.loads(r["content"])["code"] for r in rows[1:]] == ["result_unknown", "not_executed", "not_executed", "not_executed"]
        cold = make_wire(protocol, [text_reply(protocol)])
        await collect(session(catalog, cold), "cold")
        assert_native_wire(protocol, cold.requests[0], calls)
        assert handled == [c[2] for c in calls[:2]]
    with isolated_test_database():
        asyncio.run(exercise())
