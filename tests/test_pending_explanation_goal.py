"""Trusted task goal survives explanation, successor cards and cold confirmation."""

import asyncio
import json
from dataclasses import replace

import pytest

from app.agent.errors import ToolPipelineError
from app.agent.kernel.adapters import consume_events
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.state_types import AgentInput
from tests.support import isolated_test_database
from tests.test_pending_explanation_transports import NativeRuntime

GOAL = "改名一个对象，然后处理第二个对象"
REPLY = {"text": "原引用：对象甲和对象乙"}


@pytest.mark.parametrize("goal", [GOAL, "把 /source/test.txt 改名后继续处理第二个对象"])
@pytest.mark.parametrize("explain", [False, True])
def test_continuation_plan_keeps_original_goal_after_explanation(explain, goal):
    async def run():
        runtime = NativeRuntime()
        owner, sid = "owner", "goal-chain"
        first = await consume_events(runtime.session.run(AgentInput(
            message=goal, owner=owner, session_id=sid, reply_context=REPLY,
        )))
        assert first.approval is not None
        if explain:
            await consume_events(runtime.session.explain(
                owner=owner, session_id=sid, plan_id=first.approval.plan_id,
                message="只解释第一张卡的影响",
            ))
        runtime.once_call = True
        runtime.call_arguments = '{"step":2}'
        second = await consume_events(runtime.session.confirm(
            owner=owner, session_id=sid, plan_id=first.approval.plan_id,
        ))
        assert second.approval is not None, second
        assert second.approval.plan_id != first.approval.plan_id
        runtime.reopen()
        state = await runtime.store.load(owner=owner, session_id=sid)
        final = await consume_events(runtime.session.confirm(
            owner=owner, session_id=sid, plan_id=second.approval.plan_id,
        ))
        system = runtime.wires[-1]["messages"][0]["content"]
        assert final.approval is None
        assert runtime.writes == [({"step": 1}, "frozen-1"), ({"step": 2}, "frozen-1")]
        assert runtime.prepares == [{"step": 1}, {"step": 2}]
        assert state.metadata["pending_effect_goal"]["content"] == goal
        assert state.metadata["pending_effect_goal"]["reply_context"] == REPLY
        assert "原任务目标（后续解释问题不是新任务）：" + json.dumps(goal, ensure_ascii=False) in system
        assert any(REPLY["text"] in row.get("content", "") and "<reply_context" in row["content"]
                   for row in runtime.wires[-1]["messages"] if row["role"] == "user")
        cleared = await runtime.store.load(owner=owner, session_id=sid)
        assert "pending_effect_goal" not in cleared.metadata
    with isolated_test_database("goal-chain.db"):
        asyncio.run(run())


def test_stale_reobservation_keeps_original_goal_and_reply():
    async def run():
        runtime = NativeRuntime()
        owner, sid = "owner", "goal-stale"
        snapshots, observations = [], []
        def prepare(arguments, context):
            runtime.prepares.append(dict(arguments))
            return PreparedEffect(preview={"summary": "改名一个对象"},
                                  snapshot_fingerprint="old" if len(runtime.prepares) == 1 else "fresh")
        def execute(arguments, snapshot, context):
            snapshots.append(snapshot)
            if snapshot == "old":
                raise ToolPipelineError("快照已变化", code="confirmation_stale")
            runtime.writes.append((dict(arguments), snapshot))
            return {"ok": True, "summary": "已改名"}
        def inspect(arguments, context):
            observations.append("fresh")
            runtime.call_name = None
            runtime.call_arguments = '{"step":1}'
            runtime.once_call = True
            return {"ok": True, "status": "success", "summary": "已重新读取，对象快照已变化"}
        read = KernelToolSpec(name="cloud.inspect", domain="cloud", description="重新读取改名对象",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect=ToolEffect.READ, read=inspect)
        runtime.catalog = ToolCatalog([replace(runtime.catalog.get("cloud.change"),
                                              prepare=prepare, execute_confirmed=execute), read])
        runtime.reopen()
        first = await consume_events(runtime.session.run(AgentInput(
            message=GOAL, owner=owner, session_id=sid, reply_context=REPLY,
        )))
        assert first.approval is not None
        await consume_events(runtime.session.explain(
            owner=owner, session_id=sid, plan_id=first.approval.plan_id,
            message="只解释第一张卡的影响",
        ))
        wire_count = len(runtime.wires)
        runtime.once_call = True
        runtime.call_name = read.model_name
        runtime.call_arguments = '{}'
        refreshed = await consume_events(runtime.session.confirm(
            owner=owner, session_id=sid, plan_id=first.approval.plan_id,
        ))
        assert refreshed.approval is not None, refreshed
        assert refreshed.approval.plan_id != first.approval.plan_id
        assert len(runtime.wires) == wire_count + 2
        assert observations == ["fresh"]
        assert runtime.writes == []
        assert runtime.prepares == [{"step": 1}, {"step": 1}]
        runtime.reopen()
        state = await runtime.store.load(owner=owner, session_id=sid)
        await consume_events(runtime.session.confirm(
            owner=owner, session_id=sid, plan_id=refreshed.approval.plan_id,
        ))
        system = runtime.wires[-1]["messages"][0]["content"]
        assert snapshots == ["old", "fresh"]
        assert runtime.writes == [({"step": 1}, "fresh")]
        assert state.metadata["pending_effect_goal"]["content"] == GOAL
        assert state.metadata["pending_effect_goal"]["reply_context"] == REPLY
        assert "原任务目标（后续解释问题不是新任务）：" + json.dumps(GOAL, ensure_ascii=False) in system
        assert any(REPLY["text"] in row.get("content", "") and "<reply_context" in row["content"]
                   for row in runtime.wires[-1]["messages"] if row["role"] == "user")
    with isolated_test_database("goal-stale.db"):
        asyncio.run(run())


def test_ordinary_new_request_uses_its_own_goal_and_reply():
    async def run():
        runtime = NativeRuntime()
        owner, sid = "owner", "goal-replace"
        first = await consume_events(runtime.session.run(AgentInput(
            message=GOAL, owner=owner, session_id=sid, reply_context=REPLY,
        )))
        assert first.approval is not None
        await consume_events(runtime.session.explain(
            owner=owner, session_id=sid, plan_id=first.approval.plan_id,
            message="只解释第一张卡的影响",
        ))
        runtime.once_call = True
        runtime.call_arguments = '{"step":3}'
        second = await consume_events(runtime.session.run(AgentInput(
            message="改名第三个对象", owner=owner, session_id=sid,
            reply_context={"text": "新引用：对象丙"},
        )))
        assert second.approval is not None
        assert runtime.writes == []
        rejected = await consume_events(runtime.session.confirm(
            owner=owner, session_id=sid, plan_id=first.approval.plan_id,
        ))
        assert rejected.status == "failed"
        runtime.reopen()
        state = await runtime.store.load(owner=owner, session_id=sid)
        await consume_events(runtime.session.confirm(
            owner=owner, session_id=sid, plan_id=second.approval.plan_id,
        ))
        assert runtime.writes == [({"step": 3}, "frozen-1")]
        assert state.metadata["pending_effect_goal"]["content"] == "改名第三个对象"
        assert state.metadata["pending_effect_goal"]["reply_context"] == {"text": "新引用：对象丙"}
        system = runtime.wires[-1]["messages"][0]["content"]
        assert '原任务目标（后续解释问题不是新任务）："改名第三个对象"' in system
        assert any("新引用：对象丙" in row.get("content", "") and "<reply_context" in row["content"]
                   for row in runtime.wires[-1]["messages"] if row["role"] == "user")
        assert '原任务目标（后续解释问题不是新任务）："改名一个对象' not in system
    with isolated_test_database("goal-replace.db"):
        asyncio.run(run())
