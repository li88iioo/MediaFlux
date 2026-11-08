"""Late budget pressure must preserve a scoped, readable source on the real wire."""
import asyncio
import json
import socket
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.projection import DefaultProjector
from app.agent.kernel.provider_model import (
    OpenAICompatibleModelAdapter,
    ProviderSettings,
)
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.session_service import SessionService
from app.agent.kernel.state_types import AgentInput, CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.clients.http import FixedHostHttpClient
from tests import test_agent_session_result as session_result_tests
from tests.support import isolated_test_database

native_workspace = session_result_tests.native_workspace


def tool_reply(calls):
    return {"choices": [{"message": {"content": "继续核对依赖。", "reasoning_content": "native-private-exact",
        "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                       for cid, name, args in calls]}, "finish_reason": "tool_calls"}]}


class Wire:
    def __init__(self, calls):
        self.replies = [tool_reply(calls), {"choices": [{"message": {"content": "核对完成。"}, "finish_reason": "stop"}]}]
        self.payloads = []
        self.clients = []

    async def handler(self, request):
        self.payloads.append(json.loads(request.content))
        return httpx.Response(200, json=self.replies.pop(0))

    def factory(self, **kwargs):
        client = FixedHostHttpClient(**kwargs, transport=httpx.MockTransport(self.handler),
            resolver=lambda host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))])
        self.clients.append(client)
        return client


def make_session(store, catalog, wire):
    pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store)
    catalog.register(sandbox.run_code_tool(pipeline))
    model = OpenAICompatibleModelAdapter(ProviderSettings(api_url="https://api.example.com/v1",
        model="offline-model", protocol="chat_completions"), client_factory=wire.factory)
    session = AgentSession(model=model, catalog=catalog,  pipeline=pipeline,
        state_store=store, journal=store, system_prompt="遵守用户条件与写入确认。",
        limits=SessionLimits(context_window_tokens=16384, max_output_tokens=128))
    return session, pipeline


async def run(session, request):
    events = [event async for event in session.run(AgentInput(owner="owner", session_id="source",
        request_id=request, message="只读核对库存依赖，禁止写入。"))]
    assert events[-1].payload["status"] == "success", events[-3:]


def test_two_below_threshold_results_recover_dependency_tails_on_native_wire(native_workspace):
    async def exercise():
        handled = []
        sources = ["甲" * 11000 + "TAIL_ONE", "乙" * 11000 + "TAIL_TWO"]
        tools = []
        for index, source in enumerate(sources):
            def observe(*_, index=index, source=source):
                handled.append(index)
                return {"data": {"dependency": source}}
            projected = DefaultProjector().project(observe())
            assert len(projected.model_content) < 24000
            assert not json.loads(projected.model_content).get("truncated")
            tools.append(KernelToolSpec(name=f"inspect_{index}", domain="library", description="核对库存依赖",
                effect=ToolEffect.READ, input_schema={"type": "object", "properties": {}}, read=observe))
        handled.clear()
        store = SQLiteKernelStore(secret_provider=lambda: "deadline-result-source")
        assert store.max_state_bytes == 256 * 1024
        wire = Wire([(f"query_{i}", f"inspect_{i}", {}) for i in range(2)])
        session, _ = make_session(store, ToolCatalog(tools), wire)
        await run(session, "query")
        messages = wire.payloads[1]["messages"]
        views = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
        assert len(views) == 2 and all(view["truncated"] for view in views)
        assert all(view.get("result_handle") and view.get("read_method") == "session.result" for view in views), \
            "late compaction discarded dependency tails without a readable source"
        caller = next(message for message in messages if message.get("tool_calls"))
        assert caller["reasoning_content"] == "native-private-exact"
        assert [call["id"] for call in caller["tool_calls"]] == [m["tool_call_id"] for m in messages if m["role"] == "tool"]
        # Cold SQLite reader and a real Session execute the public continuation tool.
        reopened = SQLiteKernelStore(secret_provider=lambda: "deadline-result-source")
        reads = []
        for index, view in enumerate(views):
            omission = next(entry for entry in view["truncation"] if entry["path"] == "/data/dependency")
            assert omission["total"] == len(sources[index]) and omission["next_offset"] < 11000
            reads.append((f"tail_{index}", "agent__run_code", {"code": f'from mf import session\nprint(session.result({view["result_handle"]!r}, path={omission["path"]!r})["data"][10900:])'}))
        reader_wire = Wire(reads)
        reader_session, reader = make_session(reopened, ToolCatalog([]), reader_wire)
        await run(reader_session, "recover")
        tail_messages = [m for m in reader_wire.payloads[1]["messages"] if m.get("tool_call_id", "").startswith("tail_")]
        assert len(tail_messages) == 2
        for index, message in enumerate(tail_messages):
            page = json.loads(message["content"])
            assert page["data"]["stdout"].rstrip("\n") == sources[index][10900:]
        lease, _ = await reopened.begin_turn(owner="owner", session_id="source", request_id="deny")
        context = ToolCallContext(owner="owner", session_id="source", request_id=lease.request_id,
            turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=AsyncMock())
        with pytest.raises(ToolPipelineError) as error:
            await SessionService(reader, replace(context, owner="other-owner")).result(views[0]["result_handle"], "/data/dependency")
        assert error.value.code == "reference_invalid"
        assert handled == [0, 1]
        assert all(client._client.is_closed for client in wire.clients + reader_wire.clients)
    with isolated_test_database():
        asyncio.run(exercise())
