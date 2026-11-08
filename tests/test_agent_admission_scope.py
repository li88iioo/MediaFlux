"""只读接力保留真实查询结果，但拒绝的写操作不能被模型总结成成功。"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput
from app.agent.public_view import public_conversation_messages
from tests.test_agent_recovered_progress import reply
from tests.test_agent_tool_argument_ingress import PROTOCOLS, Wire, offline as offline, store as store


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_read_handoff_rejects_write_without_false_success_in_cold_history(protocol, store):
    async def scenario():
        reads = []
        claim = "队列已全部删除，操作完成。"
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        read = KernelToolSpec(
            name="queue.inspect", domain="queue", description="查看队列", input_schema=schema,
            effect=ToolEffect.READ, read=lambda *_: reads.append("read") or {
                "ok": True, "status": "success", "summary": "已读取队列，仍有两个任务"},
        )
        write = KernelToolSpec(
            name="queue.clear", domain="queue", description="删除队列", input_schema=schema,
            effect=ToolEffect.WRITE,
            prepare=lambda *_: pytest.fail("只读接力不得创建写入预览"),
            execute_confirmed=lambda *_: pytest.fail("只读接力不得执行写入"),
        )
        catalog = ToolCatalog([read, write])
        wire = Wire(protocol, [reply(protocol, name=write.model_name, arguments={}), reply(protocol, text=claim)])
        session = AgentSession(
            model=wire.adapter, catalog=catalog,  state_store=store, journal=store,
            pipeline=ToolPipeline(catalog=catalog, state_store=store),
        )
        events = [event async for event in session.run(AgentInput(
            owner="handoff-owner", session_id="admission-scope", message="检查队列", initial_read_tool=read.name,
        ))]
        wire.verify(2)
        assert reads == ["read"]
        assert [event.payload["code"] for event in events if event.type.value == "tool.failed"] == ["tool_not_available"]
        terminal = events[-1]
        assert terminal.type.value == "turn.completed" and terminal.payload["status"] == "partial"
        assert claim not in terminal.payload["answer"]
        assert "仍有两个任务" in terminal.payload["answer"]
        cold = SQLiteKernelStore(secret_provider=lambda: "offline-tool-argument-test")
        state = await cold.load(owner="handoff-owner", session_id="admission-scope")
        assert not state.pending_effect_plan_id
        results = [json.loads(row["content"]) for row in state.conversation if row["role"] == "tool"]
        assert results[0]["ok"] is True and results[1]["code"] == "tool_not_available"
        assert public_conversation_messages(state.conversation)[-1]["content"] == terminal.payload["answer"]
        assert claim not in json.dumps(state.conversation, ensure_ascii=False)

    asyncio.run(scenario())
