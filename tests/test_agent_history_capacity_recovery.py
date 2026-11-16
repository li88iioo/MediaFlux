"""历史容量不足不得锁死后续查询，当前调用仍保留完整配对与未知状态。"""
from typing import Any
import asyncio
import json
from unittest.mock import patch

from app import database as db
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.conversation_codec import (
    PendingText,
    conversation_cost,
    pending_content,
)
from app.agent.kernel.conversation_state import historical_user_sources, retain_conversation
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import ModelEvent, ModelEventType
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.sandbox import run_code_tool
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.state_types import AgentInput
from app.agent.transports import QueryEnvelope, WebKernelTransport
from tests.support import isolated_test_database
from tests.test_agent_kernel_core import ScriptedModel
from tests.test_cancelled_read_inline import sqlite_store


def large_history():
    rows: list[dict[str, Any]] = [{"role": "user", "content": "查询媒体"}]
    for i in range(5):
        rows.extend([
            {"role": "assistant", "tool_calls": [{"call_id": str(i), "name": "library.search", "arguments": {"query": "媒体"}}]},
            {"role": "tool", "tool_call_id": str(i), "tool_name": "library.search",
             "content": json.dumps({"ok": True, "data": {"items": ["媒体名称" * 250] * 12}}, ensure_ascii=False)},
        ])
    rows.extend([
        {"role": "assistant", "tool_calls": [{"call_id": "interrupted", "name": "library.search", "arguments": {"query": "媒体"}}]},
        {"role": "tool", "tool_call_id": "interrupted", "tool_name": "library.search", "content": pending_content(True)},
    ])
    return rows


def test_historical_interruption_does_not_prevent_retention():
    rows = large_history()
    assert conversation_cost(rows) > 160 * 1024
    retained = retain_conversation(rows, _required_from=0)
    assert conversation_cost(retained) <= 160 * 1024
    assert len(retained) == len(rows)
    assert isinstance(retained[-1]["content"], PendingText)
    assert retained[-1]["content"] == pending_content(True)
    assert json.loads(retained[2]["content"])["truncated"]


def test_cold_followup_survives_oversize_history_without_replaying_tools():
    async def run():
        store = sqlite_store()
        lease, _ = await store.begin_turn(owner="o", session_id="history", request_id="seed")
        await store.commit(lease, conversation=large_history())
        for question in ("什么意思", "统计还有多少", "现在能正常对话吗"):
            cold = sqlite_store()
            model = ScriptedModel([[ModelEvent(ModelEventType.TEXT_DELTA, text="历史查询有中断，不能将部分结果当作总数。"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")]])
            catalog = ToolCatalog([])
            session = AgentSession(model=model, catalog=catalog,
                pipeline=ToolPipeline(catalog=catalog, state_store=cold, reference_store=cold),
                state_store=cold, limits=SessionLimits(context_window_tokens=16384))
            events = [e async for e in session.run(AgentInput(owner="o", session_id="history", message=question))]
            assert events[-1].type == AgentEventType.TURN_COMPLETED
            assert len(model.requests) == 1
            assert not any(e.type == AgentEventType.TOOL_STARTED for e in events)
            state = await cold.load(owner="o", session_id="history")
            assert any(row.get("content") == question for row in state.conversation)
    with isolated_test_database():
        asyncio.run(run())


def test_uncompactable_old_fact_does_not_block_new_question():
    from app.agent.kernel.model import ModelMessage, ModelToolCall
    from app.agent.model_context_budget import bounded_model_messages
    history = [ModelMessage(role="user", content="查询"),
               ModelMessage(role="assistant", tool_calls=(ModelToolCall("old", "library.search", {}),)),
               ModelMessage(role="tool", tool_call_id="old", tool_name="library.search",
                            content=json.dumps({"ok": False, "code": "x" * 100000}))]
    current = ModelMessage(role="user", content="换个问题")
    assert bounded_model_messages(history + [current], history_end=3, tool_definitions=(),
        system_prompt="", context_window_tokens=16384, output_tokens=4096) == (current,)


def test_cold_web_queries_keep_accumulated_user_sources_beyond_hot_state_capacity():
    async def run():
        questions = [f"合成资料{i:02d}：" + "媒体" * 5995 for i in range(12)] + ["继续", "你好"]
        for question in questions:
            store = sqlite_store()
            model = ScriptedModel([[ModelEvent(ModelEventType.TEXT_DELTA, text="收到资料。"),
                                    ModelEvent(ModelEventType.FINISH, finish_reason="stop")]])
            catalog = ToolCatalog()
            pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store)
            catalog.register(run_code_tool(pipeline))
            session = AgentSession(model=model, catalog=catalog,
                pipeline=pipeline,
                state_store=store, journal=store)
            view = await WebKernelTransport(session).query_view(QueryEnvelope("o", "history-web", question))
            assert view.status == "success" and len(model.requests) == 1
        cold = await sqlite_store().load(owner="o", session_id="history-web")
        assert historical_user_sources(cold) == tuple(questions)
        assert len(cold.summary.encode()) > 256 * 1024
        with db.get_conn() as conn:
            row = conn.execute("SELECT state_json FROM agent_kernel_sessions").fetchone()
            assert len(row[0].encode()) <= 256 * 1024

    with isolated_test_database(), patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
        asyncio.run(run())


def test_admitted_web_input_persistence_failure_returns_same_lease_terminal_event():
    async def run():
        store = sqlite_store()
        catalog = ToolCatalog()
        model = ScriptedModel([])
        session = AgentSession(model=model, catalog=catalog,
            pipeline=ToolPipeline(catalog=catalog, state_store=store, reference_store=store),
            state_store=store, journal=store)
        commit = store.commit

        async def fail_input(lease, **kwargs):
            if kwargs.get("conversation") is not None:
                raise ValueError("synthetic input persistence refusal")
            return await commit(lease, **kwargs)

        with patch.object(store, "commit", side_effect=fail_input):
            view = await WebKernelTransport(session).query_view(QueryEnvelope(
                "o", "failed-admission", "合成输入", request_id="failed-input"))
        assert view.status == "failed" and view.error_code == "internal_error"
        assert not model.requests
        state = await store.load(owner="o", session_id="failed-admission")
        assert state.conversation == []
        assert view.turn_id == state.metadata["active_publication"]["turn_id"]

    with isolated_test_database():
        asyncio.run(run())
