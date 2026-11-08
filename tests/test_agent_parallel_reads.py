"""原生Session的有界READ批次控制；脚本模型/handler不是实机网络验收。"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.effects import InMemoryRateLimiter
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import AgentInput
from tests.test_agent_kernel_core import ScriptedModel


def query_tool(handler, *, metadata=None, observe=None):
    return KernelToolSpec(
        name="library.lookup", domain="library", description="独立读取指定媒体",
        effect=ToolEffect.READ,
        input_schema={"type": "object", "required": ["query"],
                      "properties": {"query": {"type": "string"}}, "additionalProperties": False},
        read=handler, observe=observe,
        metadata={"parallel_read": True} if metadata is None else metadata,
    )


async def execute(tools, calls, *, limiter=None, limits=None):
    catalog, state = ToolCatalog(tools), InMemorySessionStateStore()
    model = ScriptedModel([
        [*(ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=c) for c in calls),
         ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
        [ModelEvent(ModelEventType.TEXT_DELTA, text="已汇总实际查询结果。"),
         ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
    ])
    session = AgentSession(model=model, catalog=catalog, state_store=state,

        pipeline=ToolPipeline(catalog=catalog, state_store=state, rate_limiter=limiter), limits=limits)
    events = [event async for event in session.run(AgentInput(
        owner="owner", session_id="batch", message="读取这些独立媒体的信息"))]
    return events, model, await state.load(owner="owner", session_id="batch")


@pytest.mark.parametrize("count", [2, 6])
def test_independent_reads_overlap_but_never_exceed_four_and_keep_native_order(count):
    async def scenario():
        active, peak, started, finished = 0, 0, [], []
        wave_ready = asyncio.Event()

        async def read(arguments, context):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            started.append(context.call_id)
            if active == min(4, count):
                wave_ready.set()
            await asyncio.wait_for(wave_ready.wait(), timeout=2)
            # 后面的调用先结束，不能据此重排原生tool_result。
            await asyncio.sleep(.005 * (count - int(arguments["query"])))
            finished.append(context.call_id)
            active -= 1
            return {"ok": True, "data": {"query": arguments["query"]}}

        tool = query_tool(read)
        calls = [ModelToolCall(str(i), tool.model_name, {"query": str(i)}) for i in range(count)]
        events, model, state = await execute([tool], calls)
        assert peak == min(4, count)
        assert finished[:2] != started[:2]
        assert model.requests[0].allow_parallel_tool_calls is True
        assert events[-1].payload["status"] == "success"
        outputs = [m for m in model.requests[-1].messages if m.role == "tool"]
        assert [m.tool_call_id for m in outputs] == list(map(str, range(count)))
        assert [json.loads(m.content)["data"]["query"] for m in outputs] == list(map(str, range(count)))
        persisted = [m for m in state.conversation if m.get("role") == "tool"]
        assert [m["tool_call_id"] for m in persisted] == list(map(str, range(count)))
    asyncio.run(scenario())


@pytest.mark.parametrize("metadata,observer", [({}, None), ({"parallel_read": 1}, None),
    ({"parallel_read": True, "source_kind": "resource_index", "publishes_selection": True}, None),
    ({"parallel_read": True, "source_kind": "resource_presentation", "publishes_selection": True}, None),
    ({"parallel_read": True}, lambda result, arguments: None)])
def test_unmarked_stateful_or_observed_reads_remain_serial(metadata, observer):
    async def scenario():
        active, peak = 0, 0
        async def read(arguments, _context):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.005)
            active -= 1
            return {"ok": True, "data": arguments}
        tool = query_tool(read, metadata=metadata, observe=observer)
        _, model, _ = await execute([tool], [ModelToolCall(str(i), tool.name, {"query": str(i)}) for i in range(2)])
        assert peak == 1
        assert model.requests[0].allow_parallel_tool_calls is False
    asyncio.run(scenario())


def test_shared_quota_is_admitted_in_call_order_not_handler_completion_order():
    async def scenario():
        observed = []
        async def read(arguments, _context):
            observed.append(arguments["query"])
            await asyncio.sleep(.01)
            return {"ok": True, "data": arguments}
        tool = query_tool(read)
        events, model, _ = await execute([tool], [ModelToolCall(str(i), tool.name, {"query": str(i)}) for i in range(2)],
            limiter=InMemoryRateLimiter(limit=1))
        assert observed == ["0"]
        assert events[-1].payload["status"] == "partial"
        results = [json.loads(m.content) for m in model.requests[-1].messages if m.role == "tool"]
        assert results[0]["data"]["query"] == "0"
        assert results[1]["code"] == "rate_limited"
    asyncio.run(scenario())


@pytest.mark.parametrize("first_fails_fast", [False, True])
@pytest.mark.parametrize("code", ["invalid_arguments", "provider_timeout", "rate_limited"])
def test_parallel_success_settles_syntax_but_not_operational_read_failure(first_fails_fast, code):
    async def scenario():
        async def read(arguments, _context):
            if arguments["query"] == "bad":
                if not first_fails_fast:
                    await asyncio.sleep(.02)
                raise ToolPipelineError("bad对象参数无效", code=code)
            if first_fails_fast:
                await asyncio.sleep(.02)
            return {"ok": True, "summary": "good对象已核验", "data": {"query": "good"}}
        tool = query_tool(read)
        events, model, _ = await execute([tool], [ModelToolCall(q, tool.name, {"query": q}) for q in ("bad", "good")])
        assert events[-1].payload["status"] == ("success" if code == "invalid_arguments" else "partial")
        outputs = [json.loads(m.content) for m in model.requests[-1].messages if m.role == "tool"]
        assert outputs[0]["code"] == code
        assert "repair" not in outputs[0]
        assert outputs[1]["data"]["query"] == "good"
    asyncio.run(scenario())


def test_mixed_write_batch_keeps_confirmation_and_does_not_prefetch_after_it():
    async def scenario():
        read_ids, prepared, executed = [], [], []
        async def read(arguments, context):
            read_ids.append(context.call_id)
            return {"ok": True, "data": arguments}
        def prepare(arguments, _context):
            prepared.append(arguments)
            return PreparedEffect(preview={"summary": "等待确认"}, snapshot_fingerprint="test-snapshot")
        write = KernelToolSpec(name="files.change", domain="files", description="修改媒体需确认",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect=ToolEffect.WRITE, prepare=prepare,
            execute_confirmed=lambda *args: executed.append(args), metadata={"parallel_read": True})
        tool = query_tool(read)
        events, model, state = await execute([tool, write], [
            ModelToolCall("before", tool.name, {"query": "before"}),
            ModelToolCall("write", write.name, {}),
            ModelToolCall("after", tool.name, {"query": "after"}),
        ])
        assert read_ids == ["before"] and len(prepared) == 1 and not executed
        assert events[-1].payload["status"] == "approval_required"
        assert state.pending_effect_plan_id
        # 多调用提议许可不是执行权限：窗口有可并发READ，但混合写仍暂停在确认处。
        assert model.requests[0].allow_parallel_tool_calls is True
        allowed = model.requests[0].system_prompt.split("本窗口允许并发执行的只读能力：", 1)[1].split("。", 1)[0]
        assert tool.model_name in allowed and write.model_name not in allowed
    asyncio.run(scenario())


def test_over_budget_parallel_batch_starts_no_handlers():
    async def scenario():
        observed = []
        tool = query_tool(lambda arguments, _context: observed.append(arguments))
        events, _, _ = await execute([tool], [ModelToolCall(str(i), tool.name, {"query": str(i)}) for i in range(4)],
            limits=SessionLimits(max_tool_calls=3))
        assert not observed
        assert events[-1].payload["status"] == "partial"
    asyncio.run(scenario())
