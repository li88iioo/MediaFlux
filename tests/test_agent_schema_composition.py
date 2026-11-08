"""Actual catalog composition and mutual-exclusion contracts at pipeline ingress."""
from __future__ import annotations

import asyncio
import json
from collections import UserDict
from types import MappingProxyType
from unittest.mock import patch

import pytest

from app import database as db
from app.agent.kernel.bootstrap import build_tool_catalog
from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import AgentInput, CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.modules import rss_delivery
from tests.support import isolated_test_database
from tests.test_agent_sdk_effects import context_for, executor
from tests.test_agent_tool_argument_ingress import Wire, encode, wire


def catalog_schema(name):
    from app.agent.kernel.ports.sdk_effects import (
        rss_delivery_tools, cloud_change_tool,
        schedule_policy_tools,
    )
    if name == "cloud.change":
        return cloud_change_tool().input_schema
    if name == "rss.refresh":
        return next(spec.input_schema for spec in rss_delivery_tools() if spec.name == name)
    if name == "strm.set_schedule":
        return next(spec.input_schema for spec in schedule_policy_tools() if spec.name == name)
    return build_tool_catalog().get(name).input_schema


@pytest.mark.parametrize("protocol", ["responses", "chat_completions", "anthropic_messages"])
def test_cloud_operation_schema_survives_native_protocol_projection(protocol):
    from app.agent.kernel.ports.sdk_effects import cloud_change_tool
    from app.clients.openai_compatible import native_tool_definitions

    spec = cloud_change_tool()
    native, = native_tool_definitions(protocol, [spec.model_definition()])
    native = json.loads(json.dumps(native))
    if protocol == "chat_completions":
        native = native["function"]
    assert native["input_schema" if protocol == "anthropic_messages" else "parameters"] == spec.input_schema


async def execute(schema, arguments, trace, *, validator=None):
    def validate(value):
        trace.append(("validate", value))
        return validator(value) if validator is not None else value

    tool = KernelToolSpec(name="lookup", domain="library", description="offline schema contract",
                          input_schema=schema, effect=ToolEffect.READ, validator=validate,
                          read=lambda args, ctx: trace.append(("read", args)) or {"ok": True})
    store = InMemorySessionStateStore()
    lease, _ = await store.begin_turn(owner="owner", session_id="composition", request_id="r")

    async def progress(payload):
        pass

    context = ToolCallContext(owner="owner", session_id="composition", request_id="r",
                              turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(),
                              report_progress=progress)
    return await ToolPipeline(catalog=ToolCatalog([tool]), state_store=store).execute(
        "lookup", arguments, context=context)


@pytest.mark.parametrize("name,arguments", [
    ("rss.refresh", {"subscription_ids": [True]}),
    ("strm.set_schedule", {}),
    ("cloud.change", {"operations": [{"op": "batch_relocate", "items": [], "target_path": "test"}]}),
])
def test_actual_catalog_rejects_before_domain_or_execution(name, arguments):
    trace = []
    with pytest.raises(ToolPipelineError) as error:
        asyncio.run(execute(catalog_schema(name), arguments, trace))
    assert error.value.code == "invalid_arguments"
    assert trace == []


def test_rss_mutually_exclusive_scopes_are_rejected_by_actual_sdk_prepare():
    async def run():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        sid = db.add_rss_subscription("scope fixture", "https://feed.invalid/rss")
        with patch.object(rss_delivery, "execute", side_effect=AssertionError("未确认不能执行")) as submit:
            with patch.object(rss_delivery, "prepare", side_effect=AssertionError("无效范围不能读取业务快照")) as prepare:
                for arguments in ({}, {"subscription_ids": None, "scope": None},
                                  {"subscription_ids": []},
                                  {"subscription_ids": [sid], "scope": "all_configured"}):
                    with pytest.raises(ToolPipelineError) as error:
                        await effects.prepare("rss.refresh", arguments, context=context)
                    assert error.value.code == "invalid_arguments"
                    assert await effects.get_pending_plan(owner=context.owner, session_id=context.session_id) is None
                prepare.assert_not_called()
            for arguments, expected in (
                ({"subscription_ids": [sid], "scope": None}, {"subscription_ids": [sid]}),
                ({"subscription_ids": None, "scope": "all_configured"}, {"scope": "all_configured"}),
            ):
                result = await effects.prepare("rss.refresh", arguments, context=context)
                assert result.effect_plan is not None
                assert dict(result.effect_plan.arguments) == expected
                assert await effects.cancel_effect(result.effect_plan.plan_id, lease=context.lease)
            submit.assert_not_called()
    with isolated_test_database():
        asyncio.run(run())


@pytest.mark.parametrize("name,arguments", [
    ("rss.refresh", {"subscription_ids": [1]}),
    ("rss.refresh", {"scope": "all_configured"}),
    ("strm.set_schedule", {"enabled": False}),
])
def test_actual_catalog_valid_shapes_preserve_arguments(name, arguments):
    trace = []
    assert asyncio.run(execute(catalog_schema(name), arguments, trace)).arguments == arguments
    assert trace == [("validate", arguments), ("read", arguments)]


@pytest.mark.parametrize("schema,value,valid", [
    ({"type": ["integer", "null"]}, True, False),
    ({"type": ["number", "null"]}, False, False),
    ({"type": ["integer", "null"]}, None, True),
    ({"type": ["integer", "null"]}, 1, True),
    ({"type": ["integer", "null"]}, "1", False),
    ({"enum": [1]}, True, False),
    ({"enum": [True]}, 1, False),
    ({"enum": [{"a": [True]}]}, {"a": [1]}, False),
    ({"enum": [{"a": [1], "b": None}]}, {"b": None, "a": [1.0]}, True),
    ({"oneOf": [{"type": "number"}, {"type": "integer"}]}, 1, False),
    ({"oneOf": [{"type": "number"}, {"type": "integer"}]}, 1.5, True),
    ({"anyOf": [{"type": "number"}, {"type": "integer"}]}, 1, True),
    ({"anyOf": [{"type": "number"}, {"type": "integer"}]}, "x", False),
    ({"not": {"enum": [True]}}, 1, True),
    ({"not": {"enum": [True]}}, True, False),
])
def test_composition_semantics(schema, value, valid):
    arguments = {"value": value}
    root = {"type": "object", "properties": {"value": schema}}
    trace = []
    if valid:
        assert asyncio.run(execute(root, arguments, trace)).arguments == arguments
    else:
        with pytest.raises(ToolPipelineError, match="arguments.value") as error:
            asyncio.run(execute(root, arguments, trace))
        assert error.value.code == "invalid_arguments"
        assert trace == []


@pytest.mark.parametrize("mapping", [dict, MappingProxyType, UserDict])
def test_composition_keeps_root_mapping(mapping):
    value = {"scope": "all_configured"}
    assert asyncio.run(execute(catalog_schema("rss.refresh"), mapping(value), [])).arguments == value


@pytest.mark.parametrize("name,invalid,valid", [
    ("rss.refresh",
     {"subscription_ids": [True]}, {"subscription_ids": [1]}),
    ("cloud.change",
     {"operations": [{"op": "batch_relocate", "items": [], "target_path": "test"}]},
     {"operations": [{"op": "copy", "file_id": "Case-sensitive-file-ID", "target_path": "/test"}]}),
])
def test_native_composed_schema_invalid_arguments_corrected_call(name, invalid, valid):
    async def run():
        executed = []
        schema = catalog_schema(name)
        tool = KernelToolSpec(name="lookup", domain="library", description="offline schema contract",
                              input_schema=schema, effect=ToolEffect.READ, validator=dict,
                              read=lambda args, ctx: executed.append(args) or {"ok": True, "summary": "read"})
        final = {"choices": [{"message": {"content": "已核验"}, "finish_reason": "stop"}]}
        transport = Wire("chat_completions", [wire("chat_completions", "json", encode(invalid)),
                         wire("chat_completions", "json", encode(valid)), (encode(final).encode(), "application/json")])
        catalog, store = ToolCatalog([tool]), InMemorySessionStateStore()
        session = AgentSession(model=transport.adapter, catalog=catalog, state_store=store,

                               pipeline=ToolPipeline(catalog=catalog, state_store=store))
        events = [event async for event in session.run(AgentInput(message="schema contract", owner="owner", session_id="native"))]
        assert executed == [valid], [event.payload for event in events]
        feedback = [message for message in transport.requests[1]["messages"] if message["role"] == "tool"]
        assert json.loads(feedback[-1]["content"])["code"] == "invalid_arguments"
        assert events[-1].payload.get("status") == "success"
        transport.verify(3)
    asyncio.run(run())


def test_copy_preview_uses_native_cloud_change_and_actual_file_ids():
    schema = catalog_schema("cloud.change")
    operation = schema["properties"]["operations"]["items"]
    assert set(operation["properties"]["op"]["enum"]) == {
        "rename", "move", "copy", "relocate", "batch_files", "trash", "create_directory"}
    assert "file_id" in operation["properties"]
    assert "object_ref" not in operation["properties"]
    assert operation["additionalProperties"] is False
    assert asyncio.run(execute(schema, {"operations": [{"op": "copy", "file_id": "Case-sensitive-ID",
        "target_path": "/destination", "new_name": "副本.mkv"}]}, [])).arguments["operations"][0]["file_id"] == "Case-sensitive-ID"
