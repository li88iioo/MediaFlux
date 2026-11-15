"""新前台目标可选择任务；同一任务仍受跨回合累计预算约束。"""
from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.agent.feature_gate import initialize_runtime_fence
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.bootstrap import _capture_creation_binding
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.continuation import _ContinuationAction
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.sandbox import run_code_tool
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.state_types import AgentInput
from app.agent.kernel.task_state import proposal
from tests.agent_task_native_helpers import task_call
from tests.test_agent_kernel_core import ScriptedModel, collect, read_tool
from tests.test_agent_task_cas import task_store as task_store
from tests.test_agent_session_result import native_workspace as native_workspace

OWNER, SID, GOAL = "budget-selection-owner", "budget-selection", "检查旧媒体库状态"
READ = "library.inspect"


@pytest.mark.parametrize("remaining,recheck", [(0, False), (0.25, False), (0.25, True)],
                         ids=["already-exhausted", "inherited-deadline", "next-rpc-exhausted"])
def test_native_budget_stops_script_batch_and_model_but_keeps_completed_facts(
    task_store, native_workspace, monkeypatch, remaining, recheck,
):
    from app.agent.kernel.persistence import SQLiteKernelStore

    queried = []
    clock = [100.0]
    monkeypatch.setattr(task_store, "_clock", lambda: clock[0])
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("禁止外联"))

    def search(query):
        queried.append(query)
        if recheck:
            clock[0] += 1.0
        return {"ok": True, "status": "success", "summary": "真实离线查询", "query": query}

    monkeypatch.setitem(sandbox.METHODS, "media.search", search)

    async def run():
        initialize_runtime_fence()
        binding = _capture_creation_binding()
        lease, _ = await task_store.begin_turn(owner=OWNER, session_id=SID, request_id="active-seed")
        budget = SessionLimits().budget()
        budget.update(active_limit=1.0, active_spent=1.0 - remaining, active_since=None)
        before = await task_store.commit(lease, task_patch=proposal(declaration(), SimpleNamespace(
            request_id=lease.request_id, user_source=GOAL, current_task=None,
        )), updates=(_ContinuationAction("", {"operation": "seed", "budget": budget}, binding=binding),))
        code = f'''import time
try:
    print(mf.media.search('first'), flush=True)
    if {recheck!r}:
        mf.media.search('after')
except mf.Error:
    open('/work/caught', 'w').write('budget was catchable')
time.sleep(0.4)
open('/work/continued', 'w').write('continued after deadline')
mf.media.search('after')'''
        observed = []
        agent, model = session(task_store, [calls(
            (sandbox.RUN_CODE_TOOL, {"code": code, "timeout_seconds": 5}),
            (sandbox.RUN_CODE_TOOL, {"code": "open('/work/next-call', 'w').write('ran')"}),
            (READ, {}),
        ), final("不应再请求模型伪造成功")], observed)
        events = await asyncio.wait_for(collect(agent.run(AgentInput(
            message="继续检查旧任务", owner=OWNER, session_id=SID))), 5)
        workspace, _ = sandbox._workspace_location(OWNER, SID)
        assert queried == (["first"] if remaining else [])
        assert observed == [] and len(model.requests) == 1
        assert events[-1].type.value == "turn.failed"
        assert events[-1].payload["code"] == "active_time_budget_exceeded"
        assert not any((workspace / "work" / name).exists() for name in ("caught", "continued", "next-call"))
        assert not (workspace / "run/mf-requests").exists()
        assert not (workspace / "run/mf-responses").exists()
        cold = SQLiteKernelStore(secret_provider=lambda: "task-test-only", clock=lambda: clock[0])
        after = await cold.load(owner=OWNER, session_id=SID)
        calls_in_history = [call["call_id"] for row in after.conversation for call in row.get("tool_calls", [])]
        results_in_history = [row for row in after.conversation if row["role"] == "tool"]
        assert calls_in_history == ["call-0", "call-1", "call-2"]
        assert [row["tool_call_id"] for row in results_in_history] == calls_in_history
        assert all(json.loads(row["content"])["status"] == "error"
                   and json.loads(row["content"])["code"] == "not_executed" for row in results_in_history[1:])
        assert len(after.metadata["task"]["verified_facts"]) == bool(remaining)
        archived = [json.loads(path.read_text()) for path in (workspace / "output").glob("*.sdk.json")]
        assert [item["result"]["query"] for item in archived] == queried
        if not remaining:
            assert after.metadata["task"] == before.metadata["task"]
            assert after.metadata["read_wait"]["budget"] == before.metadata["read_wait"]["budget"]
    asyncio.run(run())


def test_native_rpc_rechecks_budget_without_recharging_attempts(task_store, native_workspace, monkeypatch):
    from app.agent.kernel.turn import AgentTurn

    uses, queried = [], []
    reserve = AgentTurn.reserve_budget

    async def counted(turn, **kwargs):
        await reserve(turn, **kwargs)
        if kwargs.get("task_operation") == "use":
            stored = await task_store.load(owner=OWNER, session_id=SID)
            uses.append({key: stored.metadata["read_wait"]["budget"][key]
                         for key in ("model_spent", "tool_spent", "token_spent")})

    def search(query):
        queried.append(query)
        return {"ok": True, "status": "success", "summary": "真实离线查询", "query": query}

    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("禁止外联"))
    monkeypatch.setitem(sandbox.METHODS, "media.search", search)

    async def run():
        before = await old_task(task_store, exhaust="none")
        monkeypatch.setattr(AgentTurn, "reserve_budget", counted)
        agent, model = session(task_store, [calls((sandbox.RUN_CODE_TOOL, {
            "code": "for query in ('one', 'two', 'three'):\n    print(mf.media.search(query))",
        })), final()])
        events = await collect(agent.run(AgentInput(message=GOAL, owner=OWNER, session_id=SID)))
        assert events[-1].payload.get("status") == "success"
        assert queried == ["one", "two", "three"] and len(uses) == 3
        assert uses[0] == uses[1] == uses[2]  # 三次准入的真实持久累计值完全相等。
        assert uses[0]["model_spent"] == before.metadata["read_wait"]["budget"]["model_spent"] + 1
        assert uses[0]["tool_spent"] == before.metadata["read_wait"]["budget"]["tool_spent"] + 1
        assert uses[0]["token_spent"] > before.metadata["read_wait"]["budget"]["token_spent"]
        after = await task_store.load(owner=OWNER, session_id=SID)
        assert after.metadata["read_wait"]["budget"]["model_spent"] == before.metadata["read_wait"]["budget"]["model_spent"] + 2
        assert after.metadata["read_wait"]["budget"]["tool_spent"] == before.metadata["read_wait"]["budget"]["tool_spent"] + 1
        assert len(model.requests) == 2 and len(after.metadata["task"]["verified_facts"]) == 3
    asyncio.run(run())


def test_sdk_business_error_with_budget_code_remains_catchable(task_store, native_workspace, monkeypatch):
    queried = []

    def search(query):
        queried.append(query)
        if query == "retry":
            raise ToolPipelineError("普通后端错误，非宿主预算拒绝", code="active_time_budget_exceeded")
        return {"ok": True, "status": "success", "summary": "重试读取完成"}

    monkeypatch.setitem(sandbox.METHODS, "media.search", search)
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("禁止外联"))

    async def run():
        await old_task(task_store, exhaust="none")
        agent, model = session(task_store, [calls((sandbox.RUN_CODE_TOOL, {"code": '''try:
    mf.media.search('retry')
except mf.Error as exc:
    assert exc.code == 'active_time_budget_exceeded'
print(mf.media.search('success'))'''})), final()])
        events = await collect(agent.run(AgentInput(message=GOAL, owner=OWNER, session_id=SID)))
        assert events[-1].payload.get("status") == "success"
        assert queried == ["retry", "success"] and len(model.requests) == 2
        data = next(event.payload["result"]["data"] for event in events if event.type.value == "tool.completed")
        assert data["exit_code"] == 0 and len(data["sdk_calls"]) == 2
    asyncio.run(run())


@pytest.mark.parametrize("ending", ["answer", "provider-error", "cancel"])
def test_request_estimate_is_debited_before_stream_and_never_refunded(task_store, ending):
    from app.agent.kernel.provider_model import ModelProviderError
    from app.agent.model_context_budget import estimated_tokens

    async def run():
        initialize_runtime_fence()
        agent, model = session(task_store, [calls(task_call(declaration()))])
        first_stream, at_wire = model.stream, []

        async def stream(request, *, cancellation):
            if not model.requests:
                async for event in first_stream(request, cancellation=cancellation):
                    yield event
                return
            model.requests.append(request)
            at_wire.append(dict((await task_store.load(owner=OWNER, session_id=SID)).metadata["read_wait"]["budget"]))
            yield ModelEvent(ModelEventType.USAGE, usage={"total_tokens": 7})
            if ending == "provider-error":
                raise ModelProviderError("offline model interrupted")
            if ending == "cancel":
                cancellation.cancel()
                cancellation.raise_if_cancelled()
            for event in final():
                yield event

        model.stream = stream
        events = await collect(agent.run(AgentInput(message=GOAL, owner=OWNER, session_id=SID)))
        budget = (await task_store.load(owner=OWNER, session_id=SID)).metadata["read_wait"]["budget"]
        estimated = sum(estimated_tokens({"system": r.system_prompt, "tools": r.tools,
            "messages": [m.to_dict() for m in r.messages]}) + r.max_output_tokens for r in model.requests)
        assert len(model.requests) == 2
        assert budget["token_spent"] == at_wire[0]["token_spent"] == estimated > 7
        assert budget["model_spent"] == 2
        assert budget["model_limit"] is budget["tool_limit"] is None
        if ending == "answer":
            assert events[-1].payload["usage"] == {"total_tokens": 7}
        elif ending == "cancel":
            assert events[-1].type.value == "turn.cancelled"
        else:
            assert events[-1].payload["finish_reason"] == "model_provider_error"
            assert events[-1].payload["status"] == "partial"

    asyncio.run(run())


def final(text="已依据当前目标回答。"):
    return [ModelEvent(ModelEventType.TEXT_DELTA, text=text),
            ModelEvent(ModelEventType.FINISH, finish_reason="stop")]


def calls(*items):
    return [*(ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall(f"call-{index}", name, arguments))
              for index, (name, arguments) in enumerate(items)),
            ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]


def declaration(source=GOAL, *, component=False):
    return {"operation": "create", "source": source, "root_goal": source,
            "constraints": [], "obligations": [{
                "id": "read", "description": "核对本次状态", "required": True,
                "acceptance": ({"kind": "observed_component", "component": "tmdb"}
                               if component else {"kind": "observed_read", "criterion": "取得本次真实查询结果"}),
            }]}


def session(store, rounds, observed=None, *, parallel=False):
    observed = [] if observed is None else observed

    def read(arguments, context):
        observed.append((dict(arguments), context.current_task))
        return {"ok": True, "status": "completed", "summary": "真实离线读取已完成"}

    tool = replace(read_tool(READ, handler=read), metadata={"parallel_read": parallel})
    catalog = ToolCatalog([tool])
    pipeline = ToolPipeline(catalog=catalog, state_store=store)
    exposed = [READ]
    if not parallel:
        catalog.register(run_code_tool(pipeline))
        exposed.append("agent.run_code")
    model = ScriptedModel(rounds)
    result = AgentSession(model=model, catalog=catalog, model_tools=exposed,
                          pipeline=pipeline, state_store=store)
    result.continuation_binding = _capture_creation_binding()
    return result, model


async def old_task(store, *, exhaust="both", component=False, blocked=False):
    initialize_runtime_fence()
    original, _ = session(store, [calls(task_call(declaration(component=component))), final()])
    # 旧在途记录保留原有限次数，新运行时默认None也不能放宽。
    original.limits = SessionLimits(max_model_rounds=32, max_tool_calls=64)
    new_budget = SessionLimits.budget
    with patch.object(SessionLimits, "budget", lambda limits: {
        key: value for key, value in new_budget(limits).items() if key != "discovery_spent"
    }):
        events = await collect(original.run(AgentInput(message=GOAL, owner=OWNER, session_id=SID)))
    assert events[-1].payload.get("status") == "success"
    lease, state = await store.begin_turn(owner=OWNER, session_id=SID, request_id="budget-boundary")
    if blocked:
        state = await store.commit(lease, task_patch=proposal({
            "operation": "checkpoint", "evidence_ids": [], "blockers": ["旧检查尚未完成"],
        }, SimpleNamespace(request_id=lease.request_id, user_source=GOAL,
                           current_task=state.metadata["task"])))
    budget = state.metadata["read_wait"]["budget"]
    assert "discovery_spent" not in budget  # 原签名预算仍按原总尝试计数，不放宽。
    state = await store.commit(lease, updates=(_ContinuationAction("", {
        "operation": "reserve", "stop": True, "model_limit": budget["model_limit"], "tool_limit": budget["tool_limit"],
        "model_spent": budget["model_limit"] - budget["model_spent"] if exhaust in {"models", "both"} else 0,
        "tool_spent": budget["tool_limit"] - budget["tool_spent"] if exhaust in {"tools", "both"} else 0,
    }, binding=original.continuation_binding),))
    return state


@pytest.mark.parametrize("exhaust", ["models", "tools", "both"])
def test_plain_new_topics_do_not_inherit_or_spend_old_task_budget(task_store, exhaust):
    async def run():
        before = await old_task(task_store, exhaust=exhaust)
        fresh, model = session(task_store, [final() for _ in range(3)])
        for index in range(3):
            events = await collect(fresh.run(AgentInput(message="换个话题，直接说你好，不继续旧任务",
                owner=OWNER, session_id=SID, request_id=f"new-topic-{index}")))
            assert events[-1].payload.get("status") == "success"
        after = await task_store.load(owner=OWNER, session_id=SID)
        assert len(model.requests) == 3
        assert after.metadata["task"] == before.metadata["task"]
        assert after.metadata["read_wait"]["budget"] == before.metadata["read_wait"]["budget"]
    asyncio.run(run())


def test_initial_read_is_new_target_before_model_and_does_not_observe_old_task(task_store):
    async def run():
        before = await old_task(task_store)
        observed = []
        fresh, model = session(task_store, [final()], observed)
        events = await collect(fresh.run(AgentInput(message="检查工作区当前状态", owner=OWNER,
            session_id=SID, initial_read_tool=READ)))
        assert events[-1].payload.get("status") == "success"
        assert observed == [({}, None)] and len(model.requests) == 1
        types = [event.type.value for event in events]
        assert types.index("tool.started") < types.index("model.started")
        after = await task_store.load(owner=OWNER, session_id=SID)
        assert after.metadata["task"] == before.metadata["task"]
        assert after.metadata["read_wait"]["budget"] == before.metadata["read_wait"]["budget"]
    asyncio.run(run())


def test_exhausted_old_task_can_be_cancelled_and_replaced_with_new_task(task_store):
    async def run():
        before = await old_task(task_store)
        goal = "停止旧任务，改为检查新的媒体库状态"
        observed = []
        fresh, model = session(task_store, [
            calls(task_call({"operation": "cancel", "source": goal})),
            calls(task_call(declaration(goal))), calls((READ, {})), final(),
        ], observed)
        events = await collect(fresh.run(AgentInput(message=goal, owner=OWNER, session_id=SID)))
        assert events[-1].payload.get("status") == "success"
        after = await task_store.load(owner=OWNER, session_id=SID)
        assert after.metadata["task"]["task_id"] != before.metadata["task"]["task_id"]
        assert after.metadata["read_wait"]["task_id"] == after.metadata["task"]["task_id"]
        assert len(observed) == 1 and len(model.requests) == 4
        assert after.metadata["read_wait"]["budget"]["model_spent"] == 4
        assert after.metadata["read_wait"]["budget"]["tool_spent"] == 3
    asyncio.run(run())


@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.parametrize("exhaust", ["models", "tools", "both"])
def test_continuing_old_business_cannot_refresh_budget_or_execute_before_reserving(task_store, blocked, exhaust):
    async def run():
        before = await old_task(task_store, blocked=blocked, exhaust=exhaust)
        observed = []
        # 每次使用新 runtime、request、generation，不能让同 task_id 刷出额度。
        for index in range(2):
            fresh, model = session(task_store, [calls((READ, {})), final()], observed)
            events = await collect(fresh.run(AgentInput(message="继续检查原任务", owner=OWNER,
                session_id=SID, request_id=f"continue-{index}")))
            assert len(model.requests) == 1 and observed == []
            assert any(event.payload.get("code") == "task_budget_exceeded" for event in events)
            after = await task_store.load(owner=OWNER, session_id=SID)
            assert after.metadata["task"] == before.metadata["task"]
            assert after.metadata["read_wait"]["budget"] == before.metadata["read_wait"]["budget"]
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["checkpoint", "revise"])
def test_same_task_control_is_rejected_before_patch_when_old_budget_is_exhausted(task_store, operation):
    async def run():
        before = await old_task(task_store)
        args = ({"operation": operation, "evidence_ids": [], "blockers": []} if operation == "checkpoint"
                else {**declaration(), "operation": operation})
        fresh, model = session(task_store, [calls(task_call(args)), final()])
        events = await collect(fresh.run(AgentInput(message=GOAL, owner=OWNER, session_id=SID)))
        assert len(model.requests) == 2
        result = next(event.payload["result"]["data"] for event in events if event.type.value == "tool.completed")
        assert result["exit_code"] != 0 and len(result["sdk_calls"]) == 1
        assert result["sdk_calls"][0]["status"] == "task_budget_exceeded"
        after = await task_store.load(owner=OWNER, session_id=SID)
        assert after.metadata["task"] == before.metadata["task"]
        assert after.metadata["read_wait"]["budget"] == before.metadata["read_wait"]["budget"]
    asyncio.run(run())


def test_cancel_preserves_exhausted_old_counters(task_store):
    async def run():
        before = await old_task(task_store)
        message = "停止旧任务"
        fresh, _ = session(task_store, [calls(task_call({"operation": "cancel", "source": message})), final()])
        events = await collect(fresh.run(AgentInput(message=message, owner=OWNER, session_id=SID)))
        assert events[-1].payload.get("status") == "success"
        after = await task_store.load(owner=OWNER, session_id=SID)
        assert after.metadata["task"]["status"] == "cancelled"
        assert after.metadata["task"]["task_id"] == before.metadata["task"]["task_id"]
        assert after.metadata["read_wait"]["budget"] == before.metadata["read_wait"]["budget"]
    asyncio.run(run())


def test_unknown_and_invalid_attempts_are_backcharged_once(task_store):
    async def run():
        before = await old_task(task_store, exhaust="none")
        malformed = ModelToolCall("broken-json", READ, None, argument_error="invalid_json", raw_arguments="{")
        observed = []
        fresh, model = session(task_store, [
            calls(("unavailable.read", {})), calls(("agent.run_code", {"code": "from mf import session\nsession.revise_task(root_goal=[], constraints=[], obligations=[], source=\"检查旧媒体库状态\")"})),
            calls(("unregistered.read", {})),
            [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=malformed),
             ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
            calls((READ, {})), final(),
        ], observed)
        events = await collect(fresh.run(AgentInput(message="继续检查旧媒体库", owner=OWNER, session_id=SID)))
        after = await task_store.load(owner=OWNER, session_id=SID)
        assert len(observed) == 1 and len(model.requests) == 6
        assert not any(event.payload.get("code") == "task_budget_exceeded" for event in events)
        budget, previous = after.metadata["read_wait"]["budget"], before.metadata["read_wait"]["budget"]
        assert budget["model_spent"] == previous["model_spent"] + 6
        assert budget["tool_spent"] == previous["tool_spent"] + 5
        assert after.metadata["task"]["task_id"] == before.metadata["task"]["task_id"]
    asyncio.run(run())


def test_unknown_call_cost_can_exhaust_budget_before_first_business_handler(task_store):
    async def run():
        before = await old_task(task_store, exhaust="none")
        lease, _ = await task_store.begin_turn(owner=OWNER, session_id=SID, request_id="one-model-left")
        before = await task_store.commit(lease, updates=(_ContinuationAction("", {
            "operation": "reserve", "model_limit": 32, "tool_limit": 16,
            "model_spent": 29, "tool_spent": 0,
        }, binding=_capture_creation_binding()),))
        observed = []
        fresh, model = session(task_store, [calls(("unregistered.read", {})),
                                           calls((READ, {})), final()], observed)
        events = await collect(fresh.run(AgentInput(message="继续检查旧媒体库", owner=OWNER, session_id=SID)))
        assert len(model.requests) == 2 and observed == []
        assert any(event.payload.get("code") == "task_budget_exceeded" for event in events)
        after = await task_store.load(owner=OWNER, session_id=SID)
        assert after.metadata["task"] == before.metadata["task"]
        assert after.metadata["read_wait"]["budget"] == before.metadata["read_wait"]["budget"]
    asyncio.run(run())


@pytest.mark.parametrize("remaining", [1, 2])
def test_parallel_read_batch_reserves_all_slots_before_any_handler(task_store, remaining):
    async def run():
        await old_task(task_store, exhaust="none", component=True)
        lease, _ = await task_store.begin_turn(owner=OWNER, session_id=SID, request_id="one-tool-left")
        before = await task_store.commit(lease, updates=(_ContinuationAction("", {
            "operation": "reserve", "model_limit": 32, "tool_limit": 16,
            "model_spent": 0, "tool_spent": 15 - remaining,
        }, binding=_capture_creation_binding()),))
        observed = []
        fresh, model = session(task_store, [calls((READ, {}), (READ, {})), final()], observed, parallel=True)
        events = await collect(fresh.run(AgentInput(message="继续核对旧任务状态", owner=OWNER, session_id=SID)))
        after = await task_store.load(owner=OWNER, session_id=SID)
        if remaining == 1:
            assert len(model.requests) == 1 and observed == []
            assert any(event.payload.get("code") == "task_budget_exceeded" for event in events)
            assert after.metadata["read_wait"]["budget"] == before.metadata["read_wait"]["budget"]
        else:
            assert events[-1].payload.get("status") == "success"
            assert len(model.requests) == 2 and len(observed) == 2
            assert after.metadata["read_wait"]["budget"]["model_spent"] == 4
            assert after.metadata["read_wait"]["budget"]["tool_spent"] == 16
    asyncio.run(run())


def test_revise_keeps_task_id_and_cumulative_budget(task_store):
    async def run():
        before = await old_task(task_store, exhaust="none")
        observed = []
        fresh, model = session(task_store, [calls(task_call({**declaration(), "operation": "revise"})),
                                           calls((READ, {})), final()], observed)
        events = await collect(fresh.run(AgentInput(message=GOAL, owner=OWNER, session_id=SID)))
        assert events[-1].payload.get("status") == "success"
        after = await task_store.load(owner=OWNER, session_id=SID)
        assert len(model.requests) == 3 and len(observed) == 1
        assert after.metadata["task"]["task_id"] == before.metadata["task"]["task_id"]
        assert after.metadata["task"]["epoch"] == before.metadata["task"]["epoch"] + 1
        assert after.metadata["read_wait"]["budget"]["model_spent"] == 5
        assert after.metadata["read_wait"]["budget"]["tool_spent"] == 3
    asyncio.run(run())


def test_initial_read_followup_stays_independent_and_seed_uses_actual_model_calls(task_store):
    async def run():
        before = await old_task(task_store)
        observed = []
        fresh, model = session(task_store, [calls((READ, {})), final()], observed)
        events = await collect(fresh.run(AgentInput(message="检查工作区状态", owner=OWNER,
            session_id=SID, initial_read_tool=READ)))
        assert events[-1].payload.get("status") == "success"
        assert observed == [({}, None), ({}, None)] and len(model.requests) == 2
        after = await task_store.load(owner=OWNER, session_id=SID)
        assert after.metadata["task"] == before.metadata["task"]
        assert after.metadata["read_wait"]["budget"] == before.metadata["read_wait"]["budget"]
        goal = "停止旧任务，记录本次工作区检查目标"
        selected, model = session(task_store, [
            calls(task_call({"operation": "cancel", "source": goal})),
            calls(task_call(declaration(goal))), final(),
        ])
        events = await collect(selected.run(AgentInput(message=goal, owner=OWNER,
            session_id=SID, initial_read_tool=READ)))
        assert events[-1].payload.get("status") == "success" and len(model.requests) == 3
        after = await task_store.load(owner=OWNER, session_id=SID)
        assert after.metadata["read_wait"]["budget"]["model_spent"] == 3
        assert after.metadata["read_wait"]["budget"]["tool_spent"] == 3
    asyncio.run(run())


def test_real_park_resume_terminal_does_not_charge_twenty_nine_new_topics(task_store):
    from app.agent.kernel.persistence import SQLiteKernelStore
    from tests.test_agent_read_wait_lifecycle import OWNER as wait_owner
    from tests.test_agent_read_wait_lifecycle import (
        WaitScript,
        build_wait_runtime,
        parked_runtime,
    )

    async def run():
        timer = [100]
        _, candidate = await parked_runtime(timer)
        timer[0] = 110
        runtime = build_wait_runtime(model=WaitScript(True).adapter,
            store=SQLiteKernelStore(clock=lambda: timer[0]))
        runtime.session.continuation_binding = _capture_creation_binding()
        tool = runtime.session.catalog.get("fixture.lookup")
        runtime.session.catalog._tools[tool.name] = replace(tool,
            read=lambda _args, _context: {"ok": True, "summary": "真实离线读取完成"})
        assert await runtime.session.resume_wait(candidate=candidate)
        before = await runtime.store.load(owner=wait_owner, session_id="wait-control")
        assert before.metadata["read_wait"]["state"] == "terminal"
        assert before.metadata["read_wait"]["budget"]["model_spent"] == 4
        script = WaitScript(True)
        runtime.session.model = script.adapter
        for index in range(29):
            script.steps = ["finish"]
            events = await collect(runtime.session.run(AgentInput(message="换个话题，直接说你好",
                owner=wait_owner, session_id="wait-control", request_id=f"new-topic-{index}")))
            assert events[-1].payload.get("status") == "success"
        assert len(script.requests) == 29
        after = await runtime.store.load(owner=wait_owner, session_id="wait-control")
        assert after.metadata["task"] == before.metadata["task"]
        assert after.metadata["read_wait"]["budget"] == before.metadata["read_wait"]["budget"]
    asyncio.run(run())


def test_completed_task_does_not_charge_or_collect_new_business_reads(task_store):
    async def run():
        await old_task(task_store, exhaust="none")
        reading, _ = session(task_store, [calls((READ, {})), final()])
        await collect(reading.run(AgentInput(message=GOAL, owner=OWNER, session_id=SID)))
        lease, state = await task_store.begin_turn(owner=OWNER, session_id=SID, request_id="complete-task")
        task = state.metadata["task"]
        state = await task_store.commit(lease, task_patch=proposal({
            "operation": "checkpoint", "evidence_ids": [], "blockers": [],
            "read_evidence": {"read": task["verified_facts"][0]["evidence_id"]},
        }, SimpleNamespace(request_id=lease.request_id, user_source=GOAL, current_task=task)))
        assert state.metadata["task"]["status"] == "complete"
        budget = state.metadata["read_wait"]["budget"]
        before = await task_store.commit(lease, updates=(_ContinuationAction("", {
            "operation": "reserve", "model_limit": 32, "tool_limit": 16,
            "model_spent": 32 - budget["model_spent"], "tool_spent": 16 - budget["tool_spent"],
        }, binding=_capture_creation_binding()),))
        observed = []
        fresh, model = session(task_store, [calls((READ, {})), final()], observed)
        events = await collect(fresh.run(AgentInput(message="检查另一个工作区", owner=OWNER, session_id=SID)))
        assert events[-1].payload.get("status") == "success"
        assert len(observed) == 1 and len(model.requests) == 2
        after = await task_store.load(owner=OWNER, session_id=SID)
        assert after.metadata["task"] == before.metadata["task"]
        assert after.metadata["read_wait"]["budget"] == before.metadata["read_wait"]["budget"]
    asyncio.run(run())


def test_selection_preview_explain_and_confirm_do_not_use_unrelated_exhausted_read_budget(task_store):
    from app.agent.transports import QueryEnvelope
    from tests.support import isolated_indexer_results
    from tests.test_agent_ux_backend import _publish_candidates, _runtime, _selection

    async def run():
        before = await old_task(task_store)
        selected, pipeline, _ = _runtime(task_store, model=ScriptedModel([final(), final()]))
        selected.continuation_binding = _capture_creation_binding()
        view, _ = await _publish_candidates(pipeline, task_store, owner=OWNER, session_id=SID)
        envelope = QueryEnvelope(owner=OWNER, session_id=SID, message="预览所选候选", selection=_selection(view))
        with patch("app.indexers.downloads.preview_indexer_download_batch") as prepare, \
             patch("app.indexers.downloads.submit_indexer_download_batch") as execute:
            from tests.support import _prepare_batch
            prepare.side_effect = _prepare_batch
            execute.return_value = {"ok": True, "status": "success", "summary": "真实离线提交完成", "error": "",
                                    "items": [{"result_id": "ux-resource-result-001", "status": "submitted", "request_id": 81}]}
            preview = await collect(selected.run(envelope.to_agent_input()))
            assert sum(event.type.value == "effect.approval_required" for event in preview) == 1
            assert execute.call_count == 0
            pending = (await task_store.load(owner=OWNER, session_id=SID)).pending_effect_plan_id
            explanation = await collect(selected.explain(owner=OWNER, session_id=SID,
                                                        plan_id=pending, message="解释这张确认卡"))
            assert explanation[-1].payload.get("status") == "success" and execute.call_count == 0
            confirmation = await collect(selected.confirm(owner=OWNER, session_id=SID, plan_id=pending))
            assert confirmation[-1].payload.get("status") == "success"
            assert execute.call_count == 1
        after = await task_store.load(owner=OWNER, session_id=SID)
        assert after.metadata["task"] == before.metadata["task"]
        assert after.metadata["read_wait"]["budget"] == before.metadata["read_wait"]["budget"]
    with isolated_indexer_results():
        asyncio.run(run())
