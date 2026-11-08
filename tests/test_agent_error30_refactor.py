"""参数错误返回 schema，原工具直接重试仍经过业务门禁。"""
from __future__ import annotations

import asyncio
import json
from typing import cast

import pytest

from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.conversation_context import tool_error_message
from app.agent.kernel.effects import AuthorizationPolicy
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.session_service import SessionService
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.projection import ToolOutcome
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import AgentInput, CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from tests.test_agent_kernel_core import ScriptedModel
from tests.test_agent_recovered_progress import reply
from tests.test_agent_tool_argument_ingress import PROTOCOLS, Wire
from tests.test_agent_tool_argument_ingress import offline as offline
from tests.test_agent_tool_argument_ingress import store as store


async def _context(state, *, owner="owner", session_id="direct"):
    lease, _ = await state.begin_turn(owner=owner, session_id=session_id, request_id="r")
    return ToolCallContext(owner=owner, session_id=session_id, request_id=lease.request_id,
        turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(),
        report_progress=lambda _: asyncio.sleep(0))


@pytest.mark.parametrize("stage", ["schema", "validator", "admission", "handler"])
@pytest.mark.parametrize("effect", [ToolEffect.READ, ToolEffect.WRITE, ToolEffect.DANGER])
def test_invalid_arguments_returns_schema_and_retry_rechecks_business_gates(stage, effect):
    async def run():
        invoked = []
        schema = {"type": "object", "required": ["query"], "properties": {
            "query": {"type": "string"}, "limit": {"type": "integer"}}}

        def check(arguments):
            if arguments["query"] == "old":
                raise ToolPipelineError("query 形状无效", code="invalid_arguments")
            return arguments

        def handler(arguments, _context):
            if stage == "handler":
                check(arguments)
            invoked.append(arguments)
            return ToolOutcome(model_content="无需变更", public_content={"ok": True, "status": "no_changes"})

        class Authorization:
            async def authorize(self, _tool, arguments, _context):
                if stage == "admission":
                    check(arguments)

        tool = KernelToolSpec(name="library.lookup", domain="library", description="原工具重试",
            input_schema=schema, effect=effect, validator=check if stage == "validator" else dict,
            read=handler if effect is ToolEffect.READ else None,
            prepare=handler if effect is not ToolEffect.READ else None,
            execute_confirmed=None if effect is ToolEffect.READ else lambda *_: pytest.fail("预检不得写入"))
        state = InMemorySessionStateStore()
        context = await _context(state)
        pipeline = ToolPipeline(catalog=ToolCatalog([tool]), state_store=state,
            authorization=cast(AuthorizationPolicy, Authorization()))
        bad = {"query": "old", "limit": "bad" if stage == "schema" else 1}
        with pytest.raises(ToolPipelineError) as caught:
            await pipeline.execute(tool.name, bad, context=context)
        assert caught.value.code == "invalid_arguments" and not invoked
        feedback = json.loads(tool_error_message(
            ModelToolCall("bad", tool.name, bad), caught.value, input_schema=schema).content)
        assert feedback["expected_input_schema"] == schema
        with pytest.raises(ToolPipelineError) as required:
            await pipeline.execute(tool.name, {}, context=context)
        assert required.value.code == "invalid_arguments" and not invoked
        result = await pipeline.execute(tool.name, {"query": "new"}, context=context)
        assert invoked == [{"query": "new"}]
        assert result.outcome.public_content["status"] == "no_changes" and result.effect_plan is None

    asyncio.run(run())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("effect,preview", [
    (ToolEffect.READ, False), (ToolEffect.WRITE, False), (ToolEffect.DANGER, False),
    (ToolEffect.WRITE, True), (ToolEffect.DANGER, True),
])
def test_direct_retries_preserve_wire_history_and_only_preview_writes(store, protocol, effect, preview):
    async def run():
        invoked = []
        schema = {"type": "object", "required": ["target", "limit"], "properties": {
            "target": {"type": "string"}, "limit": {"type": "integer"}}, "additionalProperties": False}

        def handler(arguments, _context):
            invoked.append(arguments)
            if preview:
                return PreparedEffect(preview={"summary": "尚未执行，等待确认"}, snapshot_fingerprint="snapshot")
            return ToolOutcome(model_content="核验完成", public_content={"ok": True, "status": "no_changes"})

        tool = KernelToolSpec(name="library.check", domain="library", description="核验目标",
            input_schema=schema, effect=effect, read=handler if effect is ToolEffect.READ else None,
            prepare=handler if effect is not ToolEffect.READ else None,
            execute_confirmed=None if effect is ToolEffect.READ else lambda *_: pytest.fail("预览不得执行"))
        attempts = [{"target": "x", "limit": "bad"}, {"target": "x"}, {"target": "y", "limit": 1}]
        wire = Wire(protocol, [reply(protocol, name=tool.model_name, arguments=args, index=i)
            for i, args in enumerate(attempts)] + [reply(protocol, text="核验完成。")])
        catalog = ToolCatalog([tool])
        session = AgentSession(model=wire.adapter, catalog=catalog,

            pipeline=ToolPipeline(catalog=catalog, state_store=store), state_store=store, journal=store)
        events = [event async for event in session.run(AgentInput(
            message="核验目标", owner="owner", session_id="direct-retry"))]
        wire.verify(3 if preview else 4)
        assert invoked == [attempts[-1]]
        assert events[-1].type is AgentEventType.TURN_COMPLETED
        assert events[-1].payload["status"] == ("approval_required" if preview else "success")
        saved = await store.load(owner="owner", session_id="direct-retry")
        calls = [call for row in saved.conversation for call in row.get("tool_calls", [])]
        assert [call["arguments"] for call in calls] == attempts
        assert all(call["name"] == tool.model_name for call in calls)
        tool_rows = [row for row in saved.conversation if row["role"] == "tool"]
        assert len(tool_rows) == 3
        feedback = [json.loads(row["content"]) for row in tool_rows[:2]]
        assert [item["code"] for item in feedback] == ["invalid_arguments"] * 2
        assert all(item["expected_input_schema"] == schema for item in feedback)
        assert "expected_input_schema" in json.dumps(wire.requests[1])
        assert bool(saved.pending_effect_plan_id) is preview
        assert not any(event.type is AgentEventType.EFFECT_COMPLETED for event in events)
        assert await store.list_events(owner="owner", session_id="direct-retry") == [e.to_dict() for e in events]

    asyncio.run(run())


@pytest.mark.parametrize("effect", [ToolEffect.READ, ToolEffect.WRITE, ToolEffect.DANGER])
def test_cancelled_retry_never_enters_business_handler(effect):
    async def run():
        state = InMemorySessionStateStore()
        context = await _context(state)
        tool = KernelToolSpec(name="library.check", domain="library", description="取消重试",
            input_schema={"type": "object", "required": ["target"], "properties": {"target": {"type": "string"}}},
            effect=effect, read=(lambda *_: pytest.fail("取消后不得查询")) if effect is ToolEffect.READ else None,
            prepare=None if effect is ToolEffect.READ else lambda *_: pytest.fail("取消后不得预检"),
            execute_confirmed=None if effect is ToolEffect.READ else lambda *_: pytest.fail("取消后不得写入"))
        pipeline = ToolPipeline(catalog=ToolCatalog([tool]), state_store=state)
        with pytest.raises(ToolPipelineError) as error:
            await pipeline.execute(tool.name, {}, context=context)
        assert error.value.code == "invalid_arguments"
        context.cancellation.cancel("用户停止")
        with pytest.raises(asyncio.CancelledError):
            await pipeline.execute(tool.name, {"target": "x"}, context=context)
        assert not (await state.load(owner="owner", session_id="direct")).pending_effect_plan_id

    asyncio.run(run())


def test_actual_reference_reader_retries_path_without_relaxing_scope():
    from app.agent.kernel import telegram_reference

    async def run():
        state = InMemorySessionStateStore()
        pipeline = ToolPipeline(catalog=ToolCatalog([]), state_store=state)
        context = await _context(state, session_id="reader")
        reference = await telegram_reference.prepare({"text": "keep this reference", "source": {
            "kind": "telegram", "chat_id": 1, "message_id": 2}}, context.lease, pipeline.reference_store)
        await state.commit(context.lease, conversation=[{"role": "user", "content": "read", "reply_context": reference}])
        handle = reference["reference_handle"]
        assert isinstance(handle, str)
        args = {"handle": handle, "path": "/other"}
        with pytest.raises(ToolPipelineError) as error:
            await SessionService(pipeline, context).result(**args)
        assert error.value.code == "invalid_arguments"
        corrected = {**args, "path": "/text"}
        result = await SessionService(pipeline, context).result(**corrected)
        assert result["data"] == "keep this reference"
        for owner, sid in [("other", "reader"), ("owner", "other")]:
            foreign = await _context(state, owner=owner, session_id=sid)
            with pytest.raises(ToolPipelineError):
                await SessionService(pipeline, foreign).result(**corrected)

    asyncio.run(run())


@pytest.mark.parametrize("effect", [ToolEffect.READ, ToolEffect.WRITE, ToolEffect.DANGER])
def test_multiple_unparsed_calls_can_be_followed_by_one_legal_original_call(store, effect):
    async def run():
        invoked = []

        def handler(args, _):
            invoked.append(args)
            return ToolOutcome(model_content="核验完成", public_content={"ok": True, "status": "no_changes"})

        tool = KernelToolSpec(name="library.check", domain="library", description="核验目标",
            input_schema={"type": "object", "required": ["target"], "properties": {"target": {"type": "string"}}},
            effect=effect, read=handler if effect is ToolEffect.READ else None,
            prepare=handler if effect is not ToolEffect.READ else None,
            execute_confirmed=None if effect is ToolEffect.READ else lambda *_: pytest.fail("预检不得写入"))
        rounds = [[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(
            f"unparsed-{i}", tool.name, None, argument_error="invalid_json", raw_arguments="{bad")) for i in range(2)]]
        rounds[0].append(ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"))
        rounds += [[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                    tool_call=ModelToolCall("retry", tool.name, {"target": "x"})),
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
                   [ModelEvent(ModelEventType.TEXT_DELTA, text="已核验"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")]]
        catalog = ToolCatalog([tool])
        session = AgentSession(model=ScriptedModel(rounds), catalog=catalog,

            pipeline=ToolPipeline(catalog=catalog, state_store=store), state_store=store)
        events = [event async for event in session.run(AgentInput(
            owner="owner", session_id="unparsed", message="核验目标"))]
        assert events[-1].payload["status"] == "success", events[-1].payload
        assert invoked == [{"target": "x"}]
        assert sum(event.type is AgentEventType.TOOL_FAILED for event in events) == 2

    asyncio.run(run())
