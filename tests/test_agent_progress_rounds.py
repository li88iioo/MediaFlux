"""原生Session预算控制实验；真实LLM验收另行记录，不把脚本模型冒充实测。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from dataclasses import replace

import pytest

from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import AgentInput

from tests.test_agent_kernel_core import read_tool
from tests.test_agent_task_read_observations import control, declared


def run_pages(*, pages=13, mode="pages", limits=None, status="success", tracked=False):
    requests, attempted, read = [], [], []

    def handler(arguments, _context):
        attempted.append(arguments["page"])
        if mode == "exception" or (mode == "recover" and len(attempted) <= 12):
            raise ToolPipelineError("测试源不可用", code="unavailable")
        if status in {"success", "completed", "empty"}:
            read.append(arguments["page"])
        return {"ok": status != "failed", "status": status,
                "data": {"page": arguments["page"], "has_more": arguments["page"] < pages}}

    tool = replace(read_tool("records.page", handler=handler), input_schema={
        "type": "object", "properties": {"page": {"type": "integer", "minimum": 1}},
        "required": ["page"], "additionalProperties": False,
    }, metadata={"kernel_utility": mode == "utility"})

    class Model:
        async def stream(self, request, *, cancellation):
            requests.append(request)
            cancellation.raise_if_cancelled()
            if not request.tools or (mode in {"pages", "recover"} and len(read) >= pages):
                yield ModelEvent(ModelEventType.TEXT_DELTA, text=f"实际读取{len(set(read))}页。")
                yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")
                return
            name, arguments = tool.model_name, {"page": 1 if mode in {"repeat", "alias_repeat", "recover"} else len(requests)}
            if mode == "alias_repeat" and len(requests) % 2:
                name = tool.name
            yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                             tool_call=ModelToolCall(f"call-{len(requests)}", name, arguments))
            yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")

    catalog = ToolCatalog([tool])
    state = InMemorySessionStateStore()
    session = AgentSession(model=Model(), catalog=catalog, state_store=state,

                           pipeline=ToolPipeline(catalog=catalog, state_store=state,
                               rate_limiter=SimpleNamespace(acquire=AsyncMock())), limits=limits)

    async def run():
        if tracked:
            lease, _ = await state.begin_turn(owner="test-owner", session_id="read-pages", request_id="seed")
            await state.commit(lease, task_patch=control(declared()))
        return [event async for event in session.run(AgentInput(
            owner="test-owner", session_id="read-pages", message="读取全部记录后统计数量"))]

    events = asyncio.run(run())
    return events, requests, attempted, read


@pytest.mark.parametrize("pages", [13, 33])
@pytest.mark.parametrize("tracked", [False, True])
def test_distinct_successful_reads_can_finish_beyond_twelve_rounds(pages, tracked):
    events, requests, attempted, read = run_pages(pages=pages, tracked=tracked,
        limits=SessionLimits(max_output_tokens=128))
    assert attempted == read == list(range(1, pages + 1))
    assert len(requests) == pages + 1
    assert events[-1].payload["status"] == "success"


@pytest.mark.parametrize("tracked", [False, True])
def test_explicit_small_hard_round_limit_is_not_extended(tracked):
    _events, requests, attempted, _read = run_pages(limits=SessionLimits(max_model_rounds=2), tracked=tracked)
    assert len(requests) == 2
    assert attempted == [1, 2]
    assert requests[-1].tools
    assert _events[-1].payload["finish_reason"] == "model_round_budget_exceeded"


def test_unsuccessful_observations_do_not_shorten_recovery_opportunity():
    events, requests, attempted, read = run_pages(pages=1, mode="recover")
    assert len(requests) == 14
    assert attempted == [1] * 13
    assert read == [1]
    assert events[-1].payload["status"] == "success"


@pytest.mark.parametrize("entrypoint", ["write", "unavailable"])
def test_larger_read_allowance_does_not_admit_an_oversized_write_batch(entrypoint):
    from app.agent.kernel.capabilities import KernelToolSpec, ToolEffect
    from app.agent.kernel.tool_calls import PreparedEffect
    from tests.test_agent_kernel_core import ScriptedModel, collect

    prepared, written = [], []

    def prepare(*_args):
        prepared.append(True)
        return PreparedEffect(preview={"summary": "write"}, snapshot_fingerprint="fixed")

    tool = KernelToolSpec(
        name="cloud.move", domain="cloud", description="移动文件",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effect=ToolEffect.WRITE, prepare=prepare,
        execute_confirmed=lambda *_: written.append(True),
    )
    calls = [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                       tool_call=ModelToolCall(
                           f"write-{i}", tool.model_name if entrypoint == "write" else "unregistered__move", {},
                       ))
             for i in range(17)]
    model = ScriptedModel([
        [*calls, ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
        [ModelEvent(ModelEventType.TEXT_DELTA, text="本批未执行"),
         ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
    ])
    catalog, state = ToolCatalog([tool]), InMemorySessionStateStore()
    session = AgentSession(
        model=model, catalog=catalog, state_store=state,
        pipeline=ToolPipeline(catalog=catalog, state_store=state),
    )
    events = asyncio.run(collect(session.run(AgentInput(
        owner="owner", session_id="write-budget", message="移动这批文件"))))
    assert SessionLimits().max_tool_calls is None
    assert len(calls) > SessionLimits().max_write_calls
    assert prepared == written == []
    assert events[-1].payload["finish_reason"] == ("tool_budget_exceeded" if entrypoint == "write" else "stop")
    assert events[-1].payload["status"] == "partial"
    failures = [event.payload["code"] for event in events if event.type.value == "tool.failed"]
    assert failures == ["tool_budget_exceeded" if entrypoint == "write" else "tool_not_available"] * len(calls)


@pytest.mark.parametrize("tracked", [False, True])
def test_explicit_tool_budget_remains_a_hard_boundary(tracked):
    _events, requests, attempted, _read = run_pages(
        pages=17, tracked=tracked, limits=SessionLimits(max_tool_calls=16))
    assert len(attempted) == 16
    assert len(requests) == 17
    assert requests[-1].tools


@pytest.mark.parametrize("mode,status", [
    ("repeat", "success"), ("alias_repeat", "success"),
    ("exception", "success"), ("pages", "failed"), ("pages", "partial"),
    ("pages", "submitted"), ("pages", "running"),
    ("utility", "success"),
])
@pytest.mark.parametrize("tracked", [False, True])
def test_failures_and_repeats_still_stop_at_explicit_hard_limits(mode, status, tracked):
    _events, requests, _attempted, _read = run_pages(pages=99, mode=mode, status=status, tracked=tracked,
        limits=SessionLimits(max_model_rounds=6))
    assert len(requests) == 6
    assert requests[-1].tools


@pytest.mark.parametrize("mode,status,expected", [
    ("pages", "success", 3), ("repeat", "success", 1),
    ("alias_repeat", "success", 1), ("exception", "success", 0),
    ("pages", "failed", 0), ("pages", "partial", 0),
    ("utility", "success", 0),
])
@pytest.mark.parametrize("tracked", [False, True])
def test_observation_progress_is_measured_without_broadcasting_a_countdown(mode, status, expected, tracked):
    events, requests, _attempted, _read = run_pages(pages=3, mode=mode, status=status, tracked=tracked,
        limits=SessionLimits(max_model_rounds=6))
    terminal = next(e for e in reversed(events) if "successful_read_queries" in e.payload)
    assert terminal.payload["successful_read_queries"] == expected
    assert "实际剩余工具调用" not in requests[0].system_prompt
    assert "只有已完成的新业务只读观察可扩展规划额度" not in requests[0].system_prompt


def test_budget_boundary_does_not_add_countdown_or_remove_tools():
    _events, requests, _attempted, _read = run_pages(pages=16, limits=SessionLimits(max_tool_calls=16))
    assert all("实际剩余工具调用" not in request.system_prompt and request.tools for request in requests)


def test_estimated_token_limit_rejects_request_before_model_or_tool():
    events, requests, attempted, _read = run_pages(limits=SessionLimits(max_estimated_tokens=1))
    assert requests == attempted == []
    assert events[-1].payload["finish_reason"] == "token_budget_exceeded"


@pytest.mark.parametrize("stage", ["model", "read"])
def test_active_deadline_interrupts_blocked_model_or_read(stage):
    from tests.test_agent_kernel_core import ScriptedModel, collect

    async def run():
        entered, interrupted = [], []

        async def block(*_args):
            entered.append(stage)
            try:
                await asyncio.sleep(10)
            finally:
                interrupted.append(stage)

        class WaitingModel:
            async def stream(self, request, *, cancellation):
                await block()
                yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")

        model = WaitingModel() if stage == "model" else ScriptedModel([[
            ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("r", "library.status", {})),
            ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
        ]])
        store = InMemorySessionStateStore()
        catalog = ToolCatalog([read_tool("library.status", handler=block)])
        session = AgentSession(model=model, catalog=catalog,
            pipeline=ToolPipeline(catalog=catalog, state_store=store), state_store=store,
            limits=SessionLimits(max_active_seconds=0.1))
        async with asyncio.timeout(2):
            events = await collect(session.run(AgentInput(owner="owner", session_id="deadline", message="检查当前状态")))
        assert entered == interrupted == [stage]
        assert events[-1].payload.get("finish_reason", events[-1].payload.get("code")) == "active_time_budget_exceeded"
        saved = await store.load(owner="owner", session_id="deadline")
        assert any(row.get("role") == "assistant" for row in saved.conversation)

    asyncio.run(run())


def test_confirmation_wait_is_idle_and_overrun_write_finishes_receipt_first():
    from app.agent.kernel.capabilities import KernelToolSpec, ToolEffect
    from app.agent.kernel.tool_calls import PreparedEffect
    from tests.test_agent_kernel_core import ScriptedModel, collect

    async def run():
        clock, writes, budgets = [100.0], [], []

        def execute(_arguments, _snapshot, context):
            budget = context.continuation_policy["budget"]
            budgets.append(dict(budget))
            writes.append(True)
            clock[0] += 0.2  # 已开始的真实写入超过剩余额度，也须先保存回执。
            return {"ok": True, "status": "success", "summary": "写入回执已核验"}

        tool = KernelToolSpec(name="cloud.change", domain="cloud", description="变更",
            input_schema={"type": "object", "properties": {}}, effect=ToolEffect.WRITE,
            prepare=lambda *_: PreparedEffect(preview={"summary": "待确认"}, snapshot_fingerprint="snapshot"),
            execute_confirmed=execute)
        model = ScriptedModel([[
            ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("write", tool.model_name, {})),
            ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
        ]])
        store, catalog = InMemorySessionStateStore(clock=lambda: clock[0]), ToolCatalog([tool])
        session = AgentSession(model=model, catalog=catalog,
            pipeline=ToolPipeline(catalog=catalog, state_store=store), state_store=store,
            limits=SessionLimits(max_active_seconds=0.1))
        preview = await collect(session.run(AgentInput(owner="owner", session_id="write-time", message="请变更")))
        plan_id = preview[-1].payload["plan_id"]
        clock[0] += 100
        events = await collect(session.confirm(owner="owner", session_id="write-time", plan_id=plan_id))
        assert budgets, [(event.type.value, dict(event.payload)) for event in events]
        assert budgets[0]["active_spent"] == 0, budgets  # 等待确认不计入active。
        assert budgets[0]["model_spent"] == 1 and budgets[0]["token_spent"] > 0, budgets
        assert writes == [True] and len(model.requests) == 1
        assert any(e.type is AgentEventType.EFFECT_COMPLETED for e in events)
        assert events[-1].payload["finish_reason"] == "active_time_budget_exceeded"
        saved = await store.load(owner="owner", session_id="write-time")
        receipts = [row for row in saved.conversation if row.get("completion_receipt_id") == plan_id]
        assert len(receipts) == 1 and "写入回执已核验" in receipts[0]["content"]

    asyncio.run(run())
