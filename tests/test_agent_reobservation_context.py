"""确认来源仅在同一失败任务内传给业务 READ，不进入模型或公开回执。"""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from app.agent.kernel.state_types import AgentInput
from tests.test_agent_failed_effect_followup import case as case


def test_confirmed_observation_source_is_private_scoped_and_not_a_new_user_default(case):
    async def run():
        source = {
            "version": 1, "kind": "fixture_files", "owner": case.owner,
            "session_id": case.sid, "credential_generation": 1,
            "read": {"tool": "library.fixture_inspect", "arguments": {"entry": "A"}},
            "baseline": {"selected": [{"identity": "PRIVATE-SOURCE-ENTITY-2734"}]},
        }
        original_prepare, original_inspect = case.media.prepare, case.media.inspect
        observed = []

        def prepare(arguments, context):
            assert context.reobservation_source is None
            return replace(original_prepare(arguments, context),
                           metadata={"reobservation_source": deepcopy(source)})

        def inspect(arguments, context):
            observed.append(deepcopy(context.reobservation_source))
            return original_inspect(arguments, context)

        case.media.prepare, case.media.inspect = prepare, inspect
        case.build(["read_a", "finish"])
        plan_id, users = await case.preview()
        frozen = await case.session.pipeline.get_pending_plan(owner=case.owner, session_id=case.sid)
        assert frozen.metadata["reobservation_source"] == source
        events, state = await case.confirm(plan_id)
        assert events[-1].payload["status"] == "partial"
        assert observed == [source]
        case.assert_source(state, users)
        case.assert_io(prepares=["A"], executions=["A"], reads=1, requests=3)
        wire = json.dumps(case.script.requests, ensure_ascii=False)
        public = json.dumps([event.to_dict() for event in events], ensure_ascii=False)
        assert "PRIVATE-SOURCE-ENTITY-2734" not in wire + public
        assert "reobservation_source" not in wire + public

        case.script.steps.extend(["read_a", "finish"])
        new_message = "只读取当前状态，不再执行改名。"
        new_events = [event async for event in case.session.run(AgentInput(
            owner=case.owner, session_id=case.sid, request_id="new-inspection",
            message=new_message,
        ))]
        assert new_events[-1].payload["status"] == "success"
        assert observed == [source, None], "新用户回合不能继承旧来源或授权"
        assert case.media.executed == ["A"]
        saved = await case.cold_store().load(owner=case.owner, session_id=case.sid)
        new_users = case.users(saved)
        assert len(new_users) == 2 and new_users[-1]["history_id"] != users[0]["history_id"]
        assert new_users == users + [{"role": "user", "content": new_message,
                                     "history_id": new_users[-1]["history_id"], "history_order": 1}]
    asyncio.run(run())


@pytest.mark.parametrize("policy", ["availability", "authorize"])
def test_saved_source_does_not_reexpose_a_hidden_or_unauthorized_read(case, policy):
    async def run():
        from app.agent.kernel.events import AgentEventType
        from tests.test_agent_failed_effect_followup import READ

        source = {"version": 1, "available": True,
                  "read": {"tool": READ, "arguments": {"operation": "reobserve"}}}
        original_prepare = case.media.prepare
        case.media.prepare = lambda arguments, context: replace(
            original_prepare(arguments, context), metadata={"reobservation_source": source})
        case.build(["finish"])
        plan_id, users = await case.preview()
        spec = case.session.catalog.get(READ)
        # Even a registered source READ that supports reobserve must pass the normal window policy.
        schema = {**spec.input_schema, "properties": {
            **spec.input_schema["properties"], "operation": {"enum": ["reobserve"]}}}
        case.session.catalog._tools[READ] = replace(spec, input_schema=schema, **{policy: lambda _context: False})
        events, state = await case.confirm(plan_id)
        selected = next(event for event in events if event.type is AgentEventType.CAPABILITIES_SELECTED)
        assert READ not in selected.payload["tools"]
        assert "只传 operation=reobserve" not in case.script.requests[-1]["messages"][0]["content"]
        assert not case.media.reads
        case.assert_source(state, users)
        case.assert_failure(events, state, plan_id)
    asyncio.run(run())
