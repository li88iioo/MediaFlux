"""准入拒绝须保留未完成事实；只有原调用真实恢复才能清算错误。

已注册、当前可见且授权的只读工具被直接点名时即执行；未注册、未授权或写工具仍被拒绝。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.events import AgentEventType as E
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput
from app.agent.public_view import public_conversation_messages

from tests.test_agent_recovered_progress import reply
from tests.test_agent_tool_argument_ingress import PROTOCOLS, Wire, encode
from tests.test_agent_tool_argument_ingress import offline as offline  # noqa: PLC0414
from tests.test_agent_tool_argument_ingress import store as store  # noqa: PLC0414

OWNER, SID = "admission-owner", "admission-errors"
LOOKUP, HEALTH = "library.lookup", "library.health"
FIND = (HEALTH, {})
X, Y = {"ref": "object-x"}, {"ref": "object-y"}
CLAIM = "所有目录均已核验完成，没有任何错误。"





def reader(name, calls):
    def read(arguments, context):
        assert (context.owner, context.session_id) == (OWNER, SID)
        calls.append((name, dict(arguments)))
        return {"ok": True, "status": "success", "summary": "已核验指定对象",
                "data": {"title": "可信查询结果"}}

    return KernelToolSpec(
        name=name, domain="library", description="核验指定对象", effect=ToolEffect.READ,
        input_schema={"type": "object", "properties": {
            "ref": {"type": "string"}, "limit": {"type": "integer"}}, "additionalProperties": False},
        read=read,
    )


def make_session(store, adapter, tools):
    catalog = ToolCatalog([ *tools])
    return AgentSession(model=adapter, catalog=catalog, state_store=store, journal=store,
        pipeline=ToolPipeline(catalog=catalog, state_store=store))


async def cold_history(store, events):
    cold = SQLiteKernelStore(secret_provider=lambda: "offline-tool-argument-test")
    state = await cold.load(owner=OWNER, session_id=SID)
    assert await cold.list_events(owner=OWNER, session_id=SID) == [event.to_dict() for event in events]
    calls = [call for row in state.conversation for call in row.get("tool_calls", ())]
    results = [row for row in state.conversation if row["role"] == "tool"]
    assert [call["call_id"] for call in calls] == [row["tool_call_id"] for row in results]
    return state


async def run_calls(store, protocol, tools, steps, *, malformed_first=False):
    replies = [reply(protocol, name=name, arguments=arguments, index=index)
               for index, (name, arguments) in enumerate(steps)]
    if malformed_first:
        body = json.loads(replies[0][0])
        if protocol == "chat_completions":
            body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"ref":'
        else:
            body["output"][0]["arguments"] = '{"ref":'
        replies[0] = encode(body).encode(), "application/json"
    wire = Wire(protocol, [*replies, reply(protocol, text=CLAIM)])
    session = make_session(store, wire.adapter, tools)
    events = [event async for event in session.run(AgentInput(
        owner=OWNER, session_id=SID, message="核验指定对象，缺少能力时先发现，再完成原查询。"))]
    wire.verify(len(steps) + 1)
    assert not wire.replies
    return events, await cold_history(store, events), wire


def assert_unfinished(events, state, *, partial=False, code="tool_not_available"):
    terminal = events[-1]
    assert terminal.type is E.TURN_COMPLETED
    assert terminal.payload["status"] == "partial"
    assert terminal.payload["finish_reason"] in {"stop", "end_turn"}
    assert terminal.payload["answer"] == CLAIM
    if not partial:
        assert terminal.payload["successful_read_queries"] == 0
    assert public_conversation_messages(state.conversation)[-1]["content"] == CLAIM
    outcomes = [json.loads(row["content"]) for row in state.conversation if row["role"] == "tool"]
    assert any(row.get("code", row.get("status")) == code for row in outcomes)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("kind", ["unauthorized", "unknown"])
def test_rejected_native_call_cannot_persist_a_zero_query_success(store, protocol, kind):
    async def run():
        calls = []
        target = replace(reader(LOOKUP, calls), authorize=lambda _context: False)
        tools = [reader(HEALTH, calls), *([target] if kind == "unauthorized" else [])]
        name = target.model_name if kind == "unauthorized" else "unregistered__lookup"
        events, state, _wire = await run_calls(store, protocol, tools, [(name, X)])
        assert calls == []
        assert [event.payload["code"] for event in events if event.type is E.TOOL_FAILED] == ["tool_not_available"]
        result = next(row for row in state.conversation if row["role"] == "tool")
        assert json.loads(result["content"])["code"] == "tool_not_available"
        assert_unfinished(events, state)

    asyncio.run(run())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("arguments", [{}, X], ids=["implicit-object", "explicit-object"])
@pytest.mark.parametrize("alias", [True, False], ids=["wire-alias", "canonical"])
def test_direct_call_of_visible_read_tool_executes_without_discovery_round(
    store, protocol, arguments, alias,
):
    async def run():
        calls = []
        target = replace(reader(LOOKUP, calls), validator=lambda raw: {"limit": 20, **raw})
        steps = [(target.model_name if alias else target.name, arguments)]
        events, state, wire = await run_calls(store, protocol, [reader(HEALTH, calls), target], steps)
        assert calls == [(LOOKUP, {"limit": 20, **arguments})]
        assert not [event for event in events if event.type is E.TOOL_FAILED]
        loaded = [event.payload for event in events if event.type is E.CAPABILITIES_SELECTED][-1]
        assert LOOKUP in loaded["tools"]
        assert events[-1].type is E.TURN_COMPLETED and events[-1].payload["status"] == "success"
        assert events[-1].payload["answer"] == CLAIM
        assert events[-1].payload["successful_read_queries"] == 1
        assert any(event.type is E.MODEL_DELTA and event.payload["delta"] == CLAIM for event in events)
        assert public_conversation_messages(state.conversation)[-1]["content"] == CLAIM
        results = [row for row in state.conversation if row["role"] == "tool"]
        assert [json.loads(row["content"])["ok"] for row in results] == [True]
        assert "library__lookup" in [tool.get("function", tool)["name"] for tool in wire.requests[0]["tools"]]
        assert "library__lookup" in [tool.get("function", tool)["name"] for tool in wire.requests[1]["tools"]]

    asyncio.run(run())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("case", ["health-only", "other-object", "other-tool", "implicit-context", "different-raw-default"])
def test_discovery_or_a_different_call_cannot_clear_the_original_admission_error(store, protocol, case):
    async def run():
        calls = []
        target = replace(reader(LOOKUP, calls), validator=lambda raw: {"limit": 20, **raw})
        # 未注册名称在新准入规则下仍被拒绝；后续发现或其他调用都不能清算它。
        rejected = "unregistered__lookup"
        steps = {
            "health-only": [(rejected, X), FIND],
            "other-object": [(rejected, X), FIND, (LOOKUP, Y)],
            "other-tool": [(rejected, X), (HEALTH, {})],
            "implicit-context": [(rejected, {}), (HEALTH, {}), FIND, (LOOKUP, {})],
            "different-raw-default": [(rejected, X), FIND, (LOOKUP, {**X, "limit": 20})],
        }[case]
        tools = [reader(HEALTH, calls)] if case == "health-only" else [reader(HEALTH, calls), target]
        events, state, _wire = await run_calls(store, protocol, tools, steps)
        assert_unfinished(events, state, partial=True)
        if case == "health-only":
            assert calls == [(HEALTH, {})]
        elif case == "other-object":
            assert calls == [(HEALTH, {}), (LOOKUP, {**Y, "limit": 20})]
        elif case == "other-tool":
            assert calls == [(HEALTH, {})]
        elif case == "implicit-context":
            assert calls == [(HEALTH, {}), (HEALTH, {}), (LOOKUP, {"limit": 20})]
        else:
            assert calls == [(HEALTH, {}), (LOOKUP, {**X, "limit": 20})]

    asyncio.run(run())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("dto", [False, True], ids=["exception", "dto"])
def test_a_failed_same_object_retry_does_not_become_success(store, protocol, dto):
    async def run():
        calls = []

        def failed(arguments, _context):
            calls.append(dict(arguments))
            if dto:
                return {"ok": False, "status": "provider_timeout", "summary": "指定对象查询超时"}
            raise ToolPipelineError("指定对象查询超时", code="provider_timeout")

        target = replace(reader(LOOKUP, []), read=failed)
        events, state, _wire = await run_calls(store, protocol, [reader(HEALTH, []), target],
            [(target.model_name, X), (LOOKUP, X)])
        # 首次点名即真实执行并失败；同对象重试再次失败，不能变成成功。
        assert calls == [X, X]
        assert_unfinished(events, state, code="provider_timeout")

    asyncio.run(run())


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
def test_an_unparsed_unavailable_call_cannot_be_reinterpreted_as_an_empty_object(store, protocol):
    async def run():
        calls = []
        target = reader(LOOKUP, calls)
        events, state, _wire = await run_calls(store, protocol, [reader(HEALTH, calls), target],
            [(target.model_name, X), (LOOKUP, {})], malformed_first=True)
        # 坏 JSON 绝不按 {} 执行；只有模型随后显式提交的 {} 被执行，
        # 其成功按窗口内只读工具的既有规则清算这次参数错误。
        assert calls == [(LOOKUP, {})]
        assert events[-1].type is E.TURN_COMPLETED and events[-1].payload["status"] == "success"
        call = next(call for row in state.conversation for call in row.get("tool_calls", ()))
        assert call["arguments"] is None and call["argument_error"] == "invalid_json"

    asyncio.run(run())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("rejected_arguments", [X, Y, {**X, "enabled": False}],
                         ids=["same-attempt", "other-object", "other-options"])
def test_scoped_confirmation_completion_uses_the_original_admitted_arguments(store, protocol, rejected_arguments):
    async def run():
        writes = []
        tool = KernelToolSpec(name="library.change", domain="library", description="变更指定对象", effect=ToolEffect.WRITE,
            input_schema={"type": "object", "required": ["ref"], "properties": {
                "ref": {"type": "string"}, "enabled": {"type": "boolean"}}, "additionalProperties": False},
            validator=lambda raw: {"enabled": True, **raw},
            prepare=lambda arguments, _context: PreparedEffect(
                preview={"ok": True, "summary": "确认后变更指定对象"}, arguments=arguments, snapshot_fingerprint="original"),
            execute_confirmed=lambda arguments, _fingerprint, _context: (
                writes.append(dict(arguments)) or {"ok": True, "status": "completed", "summary": "指定对象已变更"}),
            metadata={"risk": "low_write"}, approval_scope=lambda _name, arguments, _metadata: {"ref": arguments["ref"]})
        preview_wire = Wire(protocol, [reply(protocol, name=tool.model_name, arguments=X)])
        session = make_session(store, preview_wire.adapter, [reader(HEALTH, []), tool])
        preview = [event async for event in session.run(AgentInput(
            owner=OWNER, session_id=SID, message="为指定对象生成确认卡。"))]
        preview_wire.verify()
        assert preview[-1].payload["status"] == "approval_required"
        plan_id = preview[-1].payload["plan_id"]
        granted = await session.set_scoped_trust(owner=OWNER, session_id=SID, operation="grant",
            plan_id=plan_id, tool_name=tool.name, scope=X, ttl_seconds=300, max_actions=5)
        assert granted["active"] is True and writes == []
        assert await session.cancel_effect(owner=OWNER, session_id=SID, plan_id=plan_id)

        replay = Wire(protocol, [reply(protocol, name="unregistered__change", arguments=rejected_arguments, index=1),
            reply(protocol, name=HEALTH, arguments={}, index=2),
            reply(protocol, name=tool.name, arguments=X, index=3), reply(protocol, text=CLAIM)])
        session.model = replay.adapter
        events = [event async for event in session.run(AgentInput(
            owner=OWNER, session_id=SID, message="完成已预授权的指定对象变更，以实际回执为准。"))]
        replay.verify(4)
        assert writes == [{**X, "enabled": True}]
        assert sum(event.type is E.EFFECT_COMPLETED for event in events) == 1
        assert [event.payload["code"] for event in events if event.type is E.TOOL_FAILED] == ["tool_not_available"]
        assert events[-1].type is E.TURN_COMPLETED
        assert events[-1].payload["status"] == "partial"
        assert events[-1].payload["finish_reason"] == "tool_not_available"
        cold = SQLiteKernelStore(secret_provider=lambda: "offline-tool-argument-test")
        state = await cold.load(owner=OWNER, session_id=SID)
        assert state.metadata["scoped_trust"]["remaining"] == 4
        assert not state.pending_effect_plan_id
        calls = [call for row in state.conversation for call in row.get("tool_calls", ())]
        results = [row for row in state.conversation if row["role"] == "tool"]
        assert len(calls) == len(results) == 4
        assert calls[1]["arguments"] == rejected_arguments
        assert calls[-1]["arguments"] == X
        assert json.loads(results[1]["content"])["code"] == "tool_not_available"
        assert json.loads(results[-1]["content"])["status"] == "completed"
        assert len([row for row in state.conversation if row.get("completion_receipt_id")]) == 1

    asyncio.run(run())
