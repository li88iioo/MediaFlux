"""完整模型批次的调用身份必须在任何工具或确认副作用前通过准入。"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.effects import InMemoryRateLimiter
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput
from tests.test_agent_kernel_core import ScriptedModel
from tests.test_agent_parallel_reads import query_tool
from tests.test_agent_tool_argument_ingress import Wire
from tests.test_agent_tool_argument_ingress import offline as offline
from tests.test_agent_tool_argument_ingress import store as store

TRANSPORTS = [(protocol, mode) for protocol in ("chat_completions", "responses")
              for mode in ("json", "sse")]
FINAL = [ModelEvent(ModelEventType.TEXT_DELTA, text="已核验实际查询结果。"),
         ModelEvent(ModelEventType.FINISH, finish_reason="stop")]


def batch(calls):
    return [*(ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=call) for call in calls),
            ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]


def payload(protocol, mode, calls):
    def encode(value):
        return json.dumps(value, separators=(",", ":"))

    items = [{"type": "function_call", "id": f"item-{index}", "call_id": call.call_id,
              "name": call.name, "arguments": encode(dict(call.arguments or {}))}
             for index, call in enumerate(calls)]
    if protocol == "chat_completions":
        native = [{"index": index, "type": "function", "id": item["call_id"],
                   "function": {"name": item["name"], "arguments": item["arguments"]}}
                  for index, item in enumerate(items)]
        body = {"choices": [{"message": {"tool_calls": native}, "finish_reason": "tool_calls"}]}
        events: list[dict[str, Any] | str] = [{"choices": [{"delta": {"tool_calls": [item]}}]} for item in native]
        events.extend([{"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}, "[DONE]"])
    else:
        body = {"status": "completed", "output": items}
        events = [event for index, item in enumerate(items) for event in (
            {"type": "response.output_item.added", "output_index": index, "item": item},
            {"type": "response.output_item.done", "output_index": index, "item": item},
        )]
        events.append({"type": "response.completed", "response": {"status": "completed"}})
    if mode == "json":
        return encode(body).encode(), "application/json"
    return "".join(f"data: {e if isinstance(e, str) else encode(e)}\n\n" for e in events).encode(), "text/event-stream"


def final_payload(protocol):
    body = ({"choices": [{"message": {"content": "已核验实际查询结果。"}, "finish_reason": "stop"}]}
            if protocol == "chat_completions" else {"status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "已核验实际查询结果。"}]}]})
    return json.dumps(body).encode(), "application/json"


async def run_session(model, store, *, parallel=True, with_write=False):
    observed = []

    def read(arguments, context):
        observed.append(("read", context.call_id, dict(arguments)))
        return {"ok": True, "data": dict(arguments)}

    def prepare(arguments, context):
        observed.append(("prepare", context.call_id, dict(arguments)))
        return PreparedEffect(preview={"summary": "等待确认"}, snapshot_fingerprint="identity-test")

    def write(arguments, expected, context):
        observed.append(("write", context.call_id, dict(arguments)))
        return {"ok": True, "status": "completed"}

    tools = [query_tool(read, metadata={"parallel_read": parallel})]
    if with_write:
        tools.append(KernelToolSpec(name="files.change", domain="files", description="修改媒体需确认",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect=ToolEffect.WRITE, prepare=prepare, execute_confirmed=write))
    catalog = ToolCatalog(tools)
    limiter = InMemoryRateLimiter()
    permits = AsyncMock(wraps=limiter.acquire)
    pipeline = ToolPipeline(catalog=catalog, state_store=store, rate_limiter=limiter)
    session = AgentSession(model=model, catalog=catalog, state_store=store,
         pipeline=pipeline)
    with patch.object(limiter, "acquire", permits), patch.object(
        pipeline.effect_store, "freeze", wraps=pipeline.effect_store.freeze,
    ) as freeze:
        events = [event async for event in session.run(AgentInput(
            owner="owner", session_id="identity", message="独立读取指定媒体，必要时修改媒体需确认"))]
    cold = await SQLiteKernelStore(secret_provider=lambda: "offline-tool-argument-test").load(
        owner="owner", session_id="identity")
    return events, cold, observed, permits.await_count, freeze.call_count


def assert_rejected(result):
    events, state, observed, permits, frozen = result
    assert not observed, "整个异常批次必须在第一个 handler 前拒绝"
    assert permits == frozen == 0
    assert not any(event.type.name in {"TOOL_STARTED", "EFFECT_PREVIEW_STARTED"} for event in events)
    assert events[-1].payload["code"] == "invalid_model_tool_calls"
    assert not state.pending_effect_plan_id
    assert not any(row.get("tool_calls") or row.get("role") == "tool" for row in state.conversation)


@pytest.mark.parametrize("protocol,mode", TRANSPORTS)
@pytest.mark.parametrize("parallel", [False, True], ids=["serial", "parallel"])
def test_http_duplicate_ids_reject_entire_batch_before_execution(protocol, mode, parallel, store):
    async def scenario():
        calls = [ModelToolCall("repeated", "library__lookup", {"query": value})
                 for value in ("first", "second")]
        wire = Wire(protocol, [payload(protocol, mode, calls), final_payload(protocol)])
        assert_rejected(await run_session(wire.adapter, store, parallel=parallel))
        wire.verify()
    asyncio.run(scenario())


@pytest.mark.parametrize("identity", ["", " \t\n", "\ud800", "first"],
                         ids=["empty", "whitespace", "invalid-utf8", "duplicate"])
def test_custom_adapter_cannot_bypass_batch_identity_admission(identity, store):
    async def scenario():
        model = ScriptedModel([batch([
            ModelToolCall("first", "library__lookup", {"query": "first"}),
            ModelToolCall(identity, "library__lookup", {"query": "second"}),
        ]), FINAL])
        assert_rejected(await run_session(model, store))
        assert len(model.requests) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("write_first", [False, True])
def test_mixed_write_batch_has_no_preview_ticket_or_read_on_duplicate_id(write_first, store):
    async def scenario():
        calls = [ModelToolCall("same", "library__lookup", {"query": "first"}),
                 ModelToolCall("same", "files__change", {})]
        if write_first:
            calls.reverse()
        protocol, mode = ("chat_completions", "json") if write_first else ("responses", "sse")
        wire = Wire(protocol, [payload(protocol, mode, calls), final_payload(protocol)])
        assert_rejected(await run_session(wire.adapter, store, with_write=True))
        wire.verify()
    asyncio.run(scenario())


@pytest.mark.parametrize("protocol,mode", TRANSPORTS)
def test_valid_http_batch_retains_native_identity_and_results(protocol, mode, store):
    async def scenario():
        ids = ["调用-é-😀", "second"]
        calls = [ModelToolCall(cid, "library__lookup", {"query": value})
                 for cid, value in zip(ids, ("first", "second"))]
        wire = Wire(protocol, [payload(protocol, mode, calls), final_payload(protocol)])
        events, state, observed, permits, frozen = await run_session(wire.adapter, store)
        assert events[-1].payload["status"] == "success"
        assert observed == [("read", cid, {"query": value}) for cid, value in zip(ids, ("first", "second"))]
        assert permits == 2 and frozen == 0
        native = next(row["tool_calls"] for row in state.conversation if row.get("tool_calls"))
        assert [call["call_id"] for call in native] == ids
        outputs = [row for row in state.conversation if row.get("role") == "tool"]
        assert [row["tool_call_id"] for row in outputs] == ids
        assert [json.loads(row["content"])["data"]["query"] for row in outputs] == ["first", "second"]
        wire.verify(2)
    asyncio.run(scenario())


def test_valid_identity_can_be_reused_in_a_later_model_round(store):
    async def scenario():
        cid = "  原始-id  "
        model = ScriptedModel([*(batch([ModelToolCall(cid, "library__lookup", {"query": value})])
                                for value in ("first", "second")), FINAL])
        events, state, observed, permits, frozen = await run_session(model, store)
        assert events[-1].payload["status"] == "success"
        assert observed == [("read", cid, {"query": value}) for value in ("first", "second")]
        assert permits == 2 and frozen == 0
        assert [row["tool_call_id"] for row in state.conversation if row.get("role") == "tool"] == [cid, cid]
        assert len(model.requests) == 3
    asyncio.run(scenario())
