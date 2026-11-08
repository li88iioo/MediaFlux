"""同工具 READ 成功仅结清语法阻塞；业务失败和原始事实仍按上下文保留。"""
from __future__ import annotations

import asyncio
import copy
import json
from types import MappingProxyType

import pytest

from app.agent.errors import ToolPipelineError
from app.agent.kernel.adapters import TurnViewBuilder, iter_ndjson
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.events import AgentEvent, AgentEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.projection import ToolOutcome
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.state_types import AgentInput
from app.agent.public_view import public_conversation_messages
from tests.test_agent_invalid_argument_progress import offline as offline
from tests.test_agent_invalid_argument_progress import store as store

from tests.test_agent_recovered_progress import reply
from tests.test_agent_tool_argument_ingress import PROTOCOLS, Wire, encode

A, B, C = "library.lookup", "library.health", "library.inspect"
X = {"ref": "object-x"}
FINAL = "按已取得的证据完成核验。"
ERRORS = {"schema", "invalid_arguments", "provider_timeout", "rate_limited"}


async def observe(store, steps, *, protocol="chat_completions", schema=False, ending="answer"):
    """Real validation/session/wire/storage; replace only read-side business I/O."""
    entered, business, returned = [], [], []
    pending = [step for step in steps if step[2] != "schema"]

    def handler(name):
        def read(arguments, context):
            expected_name, expected_arguments, outcome = pending.pop(0)
            assert name == expected_name and dict(arguments) == expected_arguments
            assert context.owner == "owner-a"
            entered.append((name, dict(arguments), outcome))
            if outcome in ERRORS:
                # Handler validation rejects before the isolated business boundary.
                raise ToolPipelineError("缺少当前对象参数" if outcome == "invalid_arguments" else "对象z仍未完成", code=outcome)
            business.append((name, dict(arguments), outcome))
            value = {"ok": True, "status": "success", "summary": outcome,
                     "data": {"items": [{"title": outcome}]}}
            returned.append((value, copy.deepcopy(value)))
            return ToolOutcome(model_content=encode(value), public_content=MappingProxyType(value))
        return read

    catalog = ToolCatalog([KernelToolSpec(
        name=name, domain="library", description="核验当前对象", effect=ToolEffect.READ,
        input_schema={"type": "object", "properties": {"ref": {"type": "string"}},
                      "required": ["ref"] if schema and name == A else [], "additionalProperties": False},
        read=handler(name),
    ) for name in sorted({step[0] for step in steps})])
    replies = [reply(protocol, name=catalog.get(name).model_name, arguments=args, index=index, private=True)
               for index, (name, args, _) in enumerate(steps)]
    limits = None
    if ending == "round-empty":
        limits = SessionLimits(max_model_rounds=len(steps))
    elif ending == "tool-empty":
        limits = SessionLimits(max_tool_calls=len(steps))
        replies.append(reply(protocol, name=catalog.get(steps[0][0]).model_name,
                             arguments=steps[0][1], index=len(steps)))
    else:
        replies.append((b"not-json", "application/json") if ending == "model-fault" else reply(protocol, text=FINAL))
    wire = Wire(protocol, replies)
    session = AgentSession(
        model=wire.adapter, catalog=catalog,
        pipeline=ToolPipeline(catalog=catalog, state_store=store), state_store=store, journal=store, limits=limits,
    )
    events = [event async for event in session.run(AgentInput(
        message="分别核验当前对象；切换后的成功不替代旧请求。", owner="owner-a", session_id="context-fence",
    ))]
    wire.verify(posts=len(steps) + (ending != "round-empty"))
    assert not wire.replies and not pending
    assert entered == [step for step in steps if step[2] != "schema"]
    assert business == [step for step in steps if step[2] not in ERRORS]
    assert all(value == original for value, original in returned)

    fresh = SQLiteKernelStore(secret_provider=lambda: "offline-invalid-progress")
    saved = await fresh.load(owner="owner-a", session_id="context-fence")
    journal = await fresh.list_events(owner="owner-a", session_id="context-fence")
    assert journal == [event.to_dict() for event in events]
    tools = [row for row in saved.conversation if row["role"] == "tool"]
    assert [row["tool_call_id"] for row in tools] == [f"call-{i}" for i in range(len(steps) + (ending == "tool-empty"))]
    failed = [event for event in journal if event["type"] == "tool.failed"]
    codes = ["invalid_arguments" if outcome == "schema" else outcome for _, _, outcome in steps if outcome in ERRORS]
    assert [event["payload"]["code"] for event in failed] == codes + (["tool_budget_exceeded"] if ending == "tool-empty" else [])
    for index, ((name, _, outcome), row) in enumerate(zip(steps, tools)):
        result = json.loads(row["content"])
        assert f"call-{index}" in encode(saved.conversation)
        if ending != "round-empty":
            assert f"call-{index}" in encode(wire.requests[-1])
        if outcome in ERRORS:
            assert result["code"] == ("invalid_arguments" if outcome == "schema" else outcome)
            assert not result.get("data", {}).get("items")
            assert "repair" not in result
        elif name != "fixture.catalog_help":
            assert result["data"]["items"][0]["title"] == outcome

    replayed = [AgentEvent(type=AgentEventType(row["type"]), sequence=row["sequence"], request_id=row["request_id"], session_id=row["session_id"], turn_id=row["turn_id"], occurred_at=row["occurred_at"], event_id=row["event_id"], payload=row["payload"]) for row in journal]
    builder = TurnViewBuilder()
    for event in replayed:
        builder.apply(event)

    async def stream():
        for event in replayed:
            yield event

    lines = [json.loads(line) async for line in iter_ndjson(stream())]
    assert lines == journal
    final = dict(events[-1].payload)
    view = builder.build().to_dict()
    answers = [final["answer"], saved.conversation[-1]["content"],
               public_conversation_messages(saved.conversation)[-1]["content"],
               view["answer"], lines[-1]["payload"]["answer"]]
    assert all(answer == answers[0] for answer in answers)
    assert final["status"] == view["status"] == lines[-1]["payload"]["status"]
    assert len([row for row in saved.conversation if row.get("provider_state")]) == len(steps)
    private_marker = {"chat_completions": "PRIVATETHINKING", "responses": "PRIVATEOPAQUE",
                      "anthropic_messages": "PRIVATESIGNATURE"}[protocol]
    for index in range(len(steps) - (ending == "round-empty")):
        assert f"{private_marker}{index}" in encode(wire.requests[-1])
    public = encode(journal) + encode(public_conversation_messages(saved.conversation)) + encode(view)
    for forbidden in ("PRIVATETHINKING", "PRIVATEOPAQUE", "PRIVATESIGNATURE", "_recovery_tracked", "伪造旧对象资料"):
        assert forbidden not in public
    return final, answers


def assert_partial(final, answers, *, reason="invalid_arguments", titles=()):
    assert final["status"] == "partial"
    assert final["finish_reason"] == reason
    if reason in {"stop", "end_turn"}:
        assert all(answer == FINAL for answer in answers)
        return
    for answer in answers:
        assert "未完成的查询不能据此判断没有内容" in answer
        for title in titles:
            assert title in answer


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("code", ["invalid_arguments", "provider_timeout", "rate_limited"])
def test_read_retry_settles_only_syntax_across_business_context(store, protocol, code):
    steps = [(A, {}, code), (B, {}, "健康检查独有资料"), (A, {}, "切换后对象独有资料")]
    final, answers = asyncio.run(observe(store, steps, protocol=protocol))
    if code == "invalid_arguments":
        assert final["status"] == "success" and final["answer"] == FINAL
    else:
        assert_partial(final, answers, reason="end_turn" if protocol == "anthropic_messages" else "stop")


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("schema", [False, True], ids=["handler", "schema"])
def test_immediate_empty_read_to_direct_retry_keeps_success(store, protocol, schema):
    steps = [(A, {}, "schema" if schema else "invalid_arguments"), (A, X, "原对象修参独有资料")]
    final, _ = asyncio.run(observe(store, steps, protocol=protocol, schema=schema))
    assert final["status"] == "success"
    assert final["answer"] == FINAL
    assert final["finish_reason"] == ("end_turn" if protocol == "anthropic_messages" else "stop")


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("schema", [False, True], ids=["handler", "schema"])
def test_direct_read_retry_settles_old_syntax_after_context_switch(store, protocol, schema):
    steps = [(A, {}, "schema" if schema else "invalid_arguments"),
             (B, {}, "中间健康检查资料"), (A, X, "新上下文显式对象资料")]
    final, _ = asyncio.run(observe(store, steps, protocol=protocol, schema=schema))
    assert final["status"] == "success" and final["answer"] == FINAL


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("case", ["empty-retry", "explicit-across-switch", "new-failure-after-switch"])
def test_existing_context_and_explicit_identity_controls(store, protocol, case):
    steps = [(A, {}, "invalid_arguments"), (A, {}, "当前对象重试资料")]
    if case == "explicit-across-switch":
        steps = [(A, X, "invalid_arguments"), (B, {}, "独立健康资料"), (A, X, "原显式对象资料")]
    elif case == "new-failure-after-switch":
        steps = [(B, {}, "先前健康资料"), (A, {}, "invalid_arguments"), (A, {}, "新上下文重试资料")]
    final, _ = asyncio.run(observe(store, steps, protocol=protocol))
    assert final["status"] == "success"
    assert final["answer"] == FINAL


@pytest.mark.parametrize("schema", [False, True], ids=["handler", "schema"])
@pytest.mark.parametrize("case", ["new-failure-after-switch", "discovery", "repeated-failure"])
def test_direct_read_retry_uses_current_business_context(store, schema, case):
    bad = (A, {}, "schema" if schema else "invalid_arguments")
    steps = [bad, (A, X, "立即修参对象资料")]
    if case == "new-failure-after-switch":
        steps.insert(0, (B, {}, "先前健康检查资料"))
    else:
        steps.insert(1, bad)
    final, _ = asyncio.run(observe(store, steps, schema=schema))
    assert final["status"] == "success"
    assert final["answer"] == FINAL


@pytest.mark.parametrize("case", ["old-and-current-failures", "two-failed-tools", "multiple-switches"])
@pytest.mark.parametrize("code", ["invalid_arguments", "provider_timeout"])
def test_multiple_read_failures_preserve_business_not_syntax_blockers(store, case, code):
    steps = [(A, {}, code), (B, {}, "独立健康检查资料")]
    titles = ("独立健康检查资料", "最近对象资料")
    if case == "old-and-current-failures":
        steps.append((A, {}, code))
    elif case == "two-failed-tools":
        steps = [(A, {}, code), (B, {}, code), (C, {}, "第三项独有资料")]
        titles = ("第三项独有资料", "最近对象资料", "后来健康资料")
    else:
        steps += [(C, {}, "第三项核验资料"), (B, {}, "再次健康资料")]
        titles += ("第三项核验资料", "再次健康资料")
    steps.append((A, {}, "最近对象资料"))
    if case == "two-failed-tools":
        steps.append((B, {}, "后来健康资料"))
    final, answers = asyncio.run(observe(store, steps))
    if code == "invalid_arguments":
        assert final["status"] == "success" and final["answer"] == FINAL
    else:
        assert_partial(final, answers, reason="stop")


@pytest.mark.parametrize("ending,reason", [
    ("answer", "invalid_arguments"), ("model-fault", "model_provider_error"),
    ("round-empty", "model_round_budget_exceeded"), ("tool-empty", "tool_budget_exceeded"),
])
@pytest.mark.parametrize("mixed", [False, True], ids=["only-old-context", "unrecovered-z"])
def test_context_failure_and_independent_success_survive_terminal_paths(store, ending, reason, mixed):
    steps = [(A, {}, "invalid_arguments")]
    if mixed:
        steps.append((C, {"ref": "object-z"}, "provider_timeout"))
    steps += [(B, {}, "独立健康结果"), (A, X, "切换后显式成功结果")]
    final, answers = asyncio.run(observe(store, steps, ending=ending))
    # 原语法失败仍在历史；只有未解决的网络错误继续阻断终态。
    if not mixed and ending == "answer":
        assert final["status"] == "success" and final["answer"] == FINAL
        return
    if ending == "answer":
        assert_partial(final, answers, reason="stop")
        return
    if mixed and ending != "model-fault":
        reason = "provider_timeout"
    assert_partial(final, answers, reason=reason, titles=("独立健康结果", "切换后显式成功结果"))
    for answer in answers:
        assert "缺少当前对象参数" not in answer
        assert ("对象z仍未完成" in answer) is mixed
