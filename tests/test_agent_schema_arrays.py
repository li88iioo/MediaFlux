"""Declared array contracts at the real pipeline and native repair boundaries."""
from __future__ import annotations

import asyncio
import json
from collections import UserDict
from types import MappingProxyType

import pytest

from app.agent.kernel.bootstrap import build_tool_catalog
from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import AgentInput, CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from tests.test_agent_tool_argument_ingress import Wire, encode, wire


def array_schema():
    specs = build_tool_catalog().visible()
    for spec in specs:
        child = spec.input_schema.get("properties", {}).get("subscription_ids", {})
        if child.get("uniqueItems"):
            return child
    raise AssertionError("real RSS native executor contract missing")


async def execute(schema, arguments, trace):
    def validate(value):
        trace.append(("validate", value))
        return value

    def read(value, context):
        trace.append(("read", value))
        return {"ok": True, "summary": "read"}

    tool = KernelToolSpec(name="lookup", domain="library", description="offline schema contract",
                          input_schema=schema, effect=ToolEffect.READ, validator=validate, read=read)
    state = InMemorySessionStateStore()
    lease, _ = await state.begin_turn(owner="owner", session_id="arrays", request_id="r")

    async def progress(payload):
        pass

    context = ToolCallContext(owner="owner", session_id="arrays", request_id="r", turn_id=lease.turn_id,
                              lease=lease, cancellation=CancellationToken(), report_progress=progress)
    return await ToolPipeline(catalog=ToolCatalog([tool]), state_store=state).execute(
        "lookup", arguments, context=context)





def test_real_rss_unique_array_rejected_before_domain_validator():
    trace = []
    schema = {"type": "object", "properties": {"subscription_ids": array_schema()}}
    with pytest.raises(ToolPipelineError, match=r"arguments\.subscription_ids\[1\]") as error:
        asyncio.run(execute(schema, {"subscription_ids": [1, 1]}, trace))
    assert error.value.code == "invalid_arguments"
    assert trace == []


@pytest.mark.parametrize("values,duplicate", [
    ([1, 1.0], True), ([0, -0.0], True), ([True, 1], False), ([False, 0], False),
    ([{"a": [1, {"x": 2}], "b": None}, {"b": None, "a": [1.0, {"x": 2.0}]}], True),
    ([{"a": [True]}, {"a": [1]}], False), ([[1, 2], [2, 1]], False),
    (["1", 1, None, False, {}, []], False),
])
def test_json_unique_equality(values, duplicate):
    schema = {"type": "object", "properties": {"values": {"type": "array", "uniqueItems": True}}}
    trace = []
    if duplicate:
        with pytest.raises(ToolPipelineError, match=r"arguments\.values\[1\]"):
            asyncio.run(execute(schema, {"values": values}, trace))
        assert trace == []
    else:
        result = asyncio.run(execute(schema, {"values": values}, trace))
        assert result.arguments == {"values": values}
        assert [step for step, _ in trace] == ["validate", "read"]


@pytest.mark.parametrize("mapping", [dict, MappingProxyType, UserDict])
def test_admitted_mapping_and_nested_arguments_are_preserved(mapping):
    nested: dict[str, object] = {"leaf": True}
    for _ in range(55):
        nested = {"child": nested}
    arguments = {"values": [nested, {"other": 1}]}
    schema = {"type": "object", "properties": {"values": {
        "type": "array", "minItems": 2, "maxItems": 2, "uniqueItems": True}}}
    trace = []
    result = asyncio.run(execute(schema, mapping(arguments), trace))
    assert result.arguments == arguments
    assert trace == [("validate", arguments), ("read", arguments)]


def test_array_without_items_and_zero_maximum():
    schema = {"type": "object", "properties": {"nested": {"type": "array", "items": {
        "type": "array", "maxItems": 0}}}}
    with pytest.raises(ToolPipelineError, match=r"arguments\.nested\[0\]"):
        asyncio.run(execute(schema, {"nested": [[1]]}, []))
    assert asyncio.run(execute(schema, {"nested": [[]]}, [])).arguments == {"nested": [[]]}


def test_existing_item_error_order_and_fitting_capacity_are_unchanged():
    schema = {"type": "object", "properties": {"values": {
        "type": "array", "maxItems": 1, "uniqueItems": True, "items": {"type": "integer"}}}}
    with pytest.raises(ToolPipelineError, match=r"arguments\.values\[0\] 类型无效"):
        asyncio.run(execute(schema, {"values": [True, True]}, []))
    values = list(range(5000))
    schema["properties"]["values"] = {"type": "array", "uniqueItems": True, "maxItems": 5000}
    assert asyncio.run(execute(schema, {"values": values}, [])).arguments == {"values": values}


def test_native_invalid_array_direct_retry_keeps_valid_parameters():
    async def run():
        executed = []
        tool = KernelToolSpec(name="lookup", domain="library", description="offline schema contract",
                              input_schema={"type":"object","properties":{"subscription_ids":array_schema()},"additionalProperties":False}, effect=ToolEffect.READ,
                              validator=dict,
                              read=lambda args, ctx: executed.append(args) or {"ok": True, "summary": "read"})
        invalid = {"subscription_ids": [1, 1]}
        valid = {"subscription_ids": [1]}
        final = {"choices": [{"message": {"content": "已核验"}, "finish_reason": "stop"}]}
        transport = Wire("chat_completions", [wire("chat_completions", "json", encode(invalid)),
                         wire("chat_completions", "json", encode(valid)), (encode(final).encode(), "application/json")])
        catalog, state = ToolCatalog([tool]), InMemorySessionStateStore()
        session = AgentSession(model=transport.adapter, catalog=catalog, state_store=state,

                               pipeline=ToolPipeline(catalog=catalog, state_store=state))
        events = [event async for event in session.run(AgentInput(message="schema contract", owner="owner", session_id="native"))]
        assert executed == [valid]
        feedback = [m for m in transport.requests[1]["messages"] if m["role"] == "tool"]
        error = json.loads(feedback[-1]["content"])
        assert error["code"] == "invalid_arguments"
        assert "arguments.subscription_ids" in error["error"]
        assert events[-1].payload.get("status") == "success"
        transport.verify(3)
    asyncio.run(run())
