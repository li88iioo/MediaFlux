"""历史容量不足不得锁死后续查询，当前调用仍保留完整配对与未知状态。"""
from typing import Any
import asyncio
import json

from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.conversation_codec import (
    PendingText,
    conversation_cost,
    pending_content,
)
from app.agent.kernel.conversation_state import retain_conversation
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import ModelEvent, ModelEventType
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.state_types import AgentInput
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
