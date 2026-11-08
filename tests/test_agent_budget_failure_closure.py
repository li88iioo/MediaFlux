"""预算收尾保留真实错误、已确认事实和完整持久工具协议。"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.state_types import AgentInput
from app.agent.models import ToolResult
from app.agent.public_view import public_conversation_messages
from tests.test_agent_kernel_core import ScriptedModel, collect
from tests.test_agent_read_failure_results import round_calls
from tests.test_agent_tool_argument_ingress import offline as offline
from tests.test_agent_tool_argument_ingress import store as store

CLAIM = "所有目录均已核验完成，没有任何错误。"
ERROR = "实际查询服务超时"
OWNER, SESSION = "budget-owner", "budget-failure"


def final_round(text=CLAIM):
    return [ModelEvent(ModelEventType.TEXT_DELTA, text=text),
            ModelEvent(ModelEventType.FINISH, finish_reason="stop")]


def session_for(store, catalog, model, limits):
    return AgentSession(model=model, catalog=catalog, state_store=store, journal=store,
        pipeline=ToolPipeline(catalog=catalog, state_store=store), limits=limits)


async def persisted_facts(events, *, call_ids):
    fresh = SQLiteKernelStore(secret_provider=lambda: "offline-tool-argument-test")
    saved = await fresh.load(owner=OWNER, session_id=SESSION)
    journal = await fresh.list_events(owner=OWNER, session_id=SESSION)
    assert journal == [event.to_dict() for event in events]
    calls = [call["call_id"] for row in saved.conversation for call in row.get("tool_calls", [])]
    tools = [row for row in saved.conversation if row["role"] == "tool"]
    assert calls == call_ids
    assert [row["tool_call_id"] for row in tools] == call_ids
    assert len({row["tool_call_id"] for row in tools}) == len(tools)
    return saved, tools


async def read_budget(store, *, returned=False, prior=False, ending="oversized", recover=False):
    observed = []

    def read(arguments, _context):
        query = arguments["query"]
        observed.append(query)
        if query == "broken" and observed.count("broken") == 1:
            if returned:
                return ToolResult(False, "unavailable", ERROR)
            raise ToolPipelineError(ERROR, code="provider_timeout")
        return ToolResult(True, "success", "可信查询完成", data={"items": [{"title": "真实已有内容"}]})

    tool = KernelToolSpec(name="fixture.lookup", domain="fixture", description="核验目录", effect=ToolEffect.READ,
        input_schema={"type": "object", "properties": {"query": {"type": "string"}},
                      "required": ["query"], "additionalProperties": False}, validator=dict, read=read)
    catalog = ToolCatalog([tool])
    queries = (["good"] if prior else []) + ["broken"] + (["broken"] if recover else [])
    calls = [ModelToolCall(f"read-{index}", tool.model_name, {"query": query})
             for index, query in enumerate(queries)]
    rounds = [round_calls([call]) for call in calls]
    blocked = []
    if ending == "oversized":
        blocked = [ModelToolCall(f"blocked-{index}", tool.model_name, {"query": "must-not-run"})
                   for index in range(2)]
        rounds.append(round_calls(blocked))
        limits = SessionLimits(max_tool_calls=len(calls) + 1)
    elif ending == "hard-limit":
        limits = SessionLimits(max_model_rounds=1)
    else:
        blocked = [ModelToolCall("blocked-final", tool.model_name, {"query": "must-not-run"})] if ending == "final-tools" else []
        if blocked:
            rounds.append(round_calls(blocked))
            limits = SessionLimits(max_tool_calls=len(calls))
        else:
            limits = SessionLimits(max_model_rounds=len(rounds))
    model = ScriptedModel(rounds)
    events = await collect(session_for(store, catalog, model, limits).run(AgentInput(
        owner=OWNER, session_id=SESSION, message="逐一核验目录，只交付真实取得的结果")))
    assert observed == queries
    assert len(model.requests) == len(rounds)
    assert not model.rounds
    saved, tools = await persisted_facts(events, call_ids=[call.call_id for call in [*calls, *blocked]])
    started = [event.payload["call_id"] for event in events if event.type.value == "tool.started"]
    assert started == [call.call_id for call in calls]
    for row in tools[-len(blocked):] if blocked else []:
        assert json.loads(row["content"])["code"] == "tool_budget_exceeded"
    if returned:
        assert json.loads(tools[len(calls) - 1 if not recover else len(calls) - 2]["content"]) == ToolResult(False, "unavailable", ERROR).to_dict()
    return events[-1], saved


@pytest.mark.parametrize("returned", [False, True], ids=["exception", "returned-failure"])
@pytest.mark.parametrize("prior", [False, True], ids=["no-success", "prior-success"])
def test_oversized_batch_cannot_replace_real_failure_or_lose_closed_calls(store, returned, prior):
    terminal, saved = asyncio.run(read_budget(store, returned=returned, prior=prior))
    final = dict(terminal.payload)
    code = "unavailable" if returned else "provider_timeout"
    assert CLAIM not in json.dumps(saved.conversation, ensure_ascii=False)
    if prior:
        assert terminal.type.value == "turn.completed"
        assert final["status"] == "partial" and final["finish_reason"] == code
        assert "真实已有内容" in final["answer"] and ERROR in final["answer"]
        assert saved.conversation[-1]["content"] == final["answer"]
        assert public_conversation_messages(saved.conversation)[-1]["content"] == final["answer"]
    else:
        assert terminal.type.value == "turn.failed"
        assert final["code"] == code and ERROR in final["message"]


@pytest.mark.parametrize("ending", ["hard-limit", "final-empty", "final-tools"])
def test_all_model_budget_exits_keep_the_unrecovered_operational_error(store, ending):
    terminal, saved = asyncio.run(read_budget(store, ending=ending))
    assert terminal.type.value == "turn.failed"
    assert terminal.payload["code"] == "provider_timeout"
    assert CLAIM not in json.dumps(saved.conversation, ensure_ascii=False)


def test_recovered_read_keeps_budget_partial_without_resurrecting_old_error(store):
    terminal, saved = asyncio.run(read_budget(store, recover=True))
    assert terminal.type.value == "turn.completed"
    assert terminal.payload["status"] == "partial"
    assert terminal.payload["finish_reason"] == "tool_budget_exceeded"
    assert CLAIM not in terminal.payload["answer"]  # 超额后不再调用模型重写答案。
    assert "真实已有内容" in terminal.payload["answer"]
    assert ERROR not in terminal.payload["answer"]
    assert saved.conversation[-1]["content"] == terminal.payload["answer"]


def test_confirmed_write_then_failed_read_budget_keeps_receipt_without_false_completion(store):
    async def scenario():
        writes, reads = [], []

        def execute(*_):
            writes.append("write")
            return {"ok": True, "status": "success", "summary": "已确认的写入完成"}

        def read(*_):
            reads.append("read")
            raise ToolPipelineError(ERROR, code="provider_timeout")

        write = KernelToolSpec(name="cloud.submit", domain="cloud", description="提交",
            input_schema={"type": "object", "properties": {}}, effect=ToolEffect.WRITE,
            prepare=lambda *_: PreparedEffect(preview={"summary": "待确认预览"}, snapshot_fingerprint="snapshot"),
            execute_confirmed=execute)
        lookup = KernelToolSpec(name="library.inspect", domain="library", description="核验目录",
            input_schema={"type": "object", "properties": {}}, effect=ToolEffect.READ, read=read)
        catalog = ToolCatalog([write, lookup])
        model = ScriptedModel([
            round_calls([ModelToolCall("prepare", write.model_name, {})]),
            round_calls([ModelToolCall("broken", lookup.model_name, {})]),
            round_calls([ModelToolCall("blocked-1", lookup.model_name, {}),
                         ModelToolCall("blocked-2", lookup.model_name, {})]),
        ])
        session = session_for(store, catalog, model, SessionLimits(max_tool_calls=2))
        preview = await collect(session.run(AgentInput(owner=OWNER, session_id=SESSION,
            message="提交后核验全部目录")))
        assert writes == [] and reads == []
        plan_id = preview[-1].payload["plan_id"]
        events = await collect(session.confirm(owner=OWNER, session_id=SESSION, plan_id=plan_id))
        assert writes == ["write"] and reads == ["read"]
        assert len(model.requests) == 3 and not model.rounds
        saved, _ = await persisted_facts([*preview, *events],
            call_ids=["prepare", "broken", "blocked-1", "blocked-2"])
        final = dict(events[-1].payload)
        assert final["status"] == "partial" and final["finish_reason"] == "provider_timeout"
        for answer in (final["answer"], saved.conversation[-1]["content"],
                       public_conversation_messages(saved.conversation)[-1]["content"]):
            assert "已确认的写入完成" in answer and ERROR in answer
            assert "所有目录均已核验完成" not in answer and "没有任何错误" not in answer
        receipts = [row for row in saved.conversation if row.get("completion_receipt_id") == plan_id]
        assert len(receipts) == 1 and "已确认的写入完成" in receipts[0]["content"]
        duplicate = await collect(session.confirm(owner=OWNER, session_id=SESSION, plan_id=plan_id))
        assert duplicate[-1].payload["code"] == "confirmation_invalid"
        assert writes == ["write"]
    asyncio.run(scenario())
