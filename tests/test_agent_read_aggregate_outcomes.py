"""真实 indexer 聚合 DTO 完整交给模型；正常答案不被失败模板覆写。"""
import asyncio
import json
from typing import cast
from unittest.mock import patch

import pytest

from app.modules import missing_resources as actions
from tests.test_missing_resources import _audit_result
from app.agent.kernel.capabilities import ToolCatalog, KernelToolSpec, ToolEffect
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput
from app.indexers.errors import IndexerUnavailable
from app.indexers.providers.base import IndexerAdapter
from app.indexers.registry import IndexerRegistry
from app.indexers.result_store import IndexerResultStore
from app.indexers.service import IndexerService
from tests.test_indexer_service import FakeAdapter
from tests.test_agent_tool_argument_ingress import Wire
from tests.test_agent_tool_argument_ingress import offline as offline
from tests.test_agent_tool_argument_ingress import store as store

CLAIM = "本轮已响应来源未找到资源；未响应来源不能据此判断。"

def reply(protocol, name=None):
    if protocol == "chat_completions":
        message = {"tool_calls": [{"id": "search", "type": "function", "function": {
            "name": name, "arguments": '{"query":"Demo","season":1,"episode":1}'}}]} if name else {"content": CLAIM}
        body = {"choices": [{"message": message, "finish_reason": "tool_calls" if name else "stop"}]}
    elif protocol == "responses":
        item = {"type": "function_call", "id": "fc_search", "call_id": "search", "name": name,
                "arguments": '{"query":"Demo","season":1,"episode":1}'} if name else {"type": "message", "content": [{"type": "output_text", "text": CLAIM}]}
        body = {"status": "completed", "output": [item]}
    else:
        item = {"type": "tool_use", "id": "search", "name": name, "input": {"query": "Demo", "season": 1, "episode": 1}} if name else {"type": "text", "text": CLAIM}
        body = {"content": [item], "stop_reason": "tool_use" if name else "end_turn"}
    return json.dumps(body).encode(), "application/json"

@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic_messages"])
@pytest.mark.parametrize("mode", ["all-failed", "mixed", "successful-empty-control"])
def test_service_failure_is_not_completed(protocol, mode, store):
    async def scenario():
        failed = mode != "successful-empty-control"
        provider = FakeAdapter("nyaa", error=IndexerUnavailable() if failed else None)
        providers = {"nyaa": provider}
        if mode == "mixed":
            providers["backup"] = FakeAdapter("backup")
        service = IndexerService(registry=IndexerRegistry(cast(dict[str, IndexerAdapter], providers)), result_store=IndexerResultStore())
        def read(arguments, context):
            r=actions.search_missing([{"query":arguments["query"],"season":arguments["season"],"episodes":[arguments["episode"]]}], owner=context.owner)
            return r
        tool = KernelToolSpec(name="fixture.native_missing_resources",domain="resources",description="缺集检索事实",effect=ToolEffect.READ,
            input_schema={"type":"object","required":["query","season","episode"],"properties":{"query":{"type":"string"},"season":{"type":"integer"},"episode":{"type":"integer"}},"additionalProperties":False},read=read)
        catalog = ToolCatalog([tool])
        wire = Wire(protocol, [reply(protocol, tool.model_name), reply(protocol)])
        session = AgentSession(model=wire.adapter, catalog=catalog, state_store=store, journal=store,
            pipeline=ToolPipeline(catalog=catalog, state_store=store))
        original = actions.config.get_bool
        audit = _audit_result(missing=[{"season": 1, "episode": 1}], target_missing=True)
        audit.data["title"] = "Demo"
        with patch.object(actions, "audit_series_episodes", return_value=audit), patch.object(actions, "get_indexer_service", return_value=service), patch.object(
            actions.config, "get_bool", side_effect=lambda key, *a, **kw: True if key == "INDEXER_SEARCH_ENABLED" else original(key, *a, **kw)):
            events = [e async for e in session.run(AgentInput(owner="owner", session_id="audit-partial", message="请查完所有来源，确认是否有资源"))]
        wire.verify(posts=2)
        assert provider.calls >= 1
        state = await store.load(owner="owner", session_id="audit-partial")
        dto = json.loads(next(row["content"] for row in state.conversation if row.get("role") == "tool"))
        if mode == "all-failed":
            assert dto["ok"] is False and dto["status"] == "partial" and dto["groups"][0]["searches"][0]["errors"]
            assert events[-1].payload["status"] == "partial"
        elif mode == "mixed":
            assert dto["ok"] is True and dto["status"] == "partial" and dto["groups"][0]["searches"][0]["errors"]
            assert events[-1].payload["status"] == "partial", "部分索引服务失败时不能发布全部完成"
        else:
            assert dto["ok"] is True and dto["status"] == "empty"
            assert events[-1].payload["status"] == "success"
        assert events[-1].type.value == "turn.completed"
        assert events[-1].payload["answer"] == state.conversation[-1]["content"] == CLAIM
        request = wire.requests[-1]
        if protocol == "chat_completions":
            content = next(row["content"] for row in request["messages"] if row["role"] == "tool")
        elif protocol == "responses":
            content = next(row["output"] for row in request["input"] if row.get("type") == "function_call_output")
        else:
            content = next(block["content"] for row in request["messages"]
                           for block in row["content"] if isinstance(block, dict) and block["type"] == "tool_result")
        assert json.loads(content) == dto
    asyncio.run(scenario())
