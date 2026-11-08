"""组合 schema 决定每次原工具调用是否合法，合法重试不继承旧字段。"""
from __future__ import annotations

import asyncio

import pytest

from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext


async def _pipeline(schema, effect, seen):
    def read(arguments, _):
        seen.append(arguments)
        return {"ok": True, "status": "found"}

    def prepare(arguments, _):
        seen.append(arguments)
        return PreparedEffect(preview={"summary": "尚未执行"}, snapshot_fingerprint="snapshot")

    tool = KernelToolSpec(name="lookup", domain="library", description="组合 schema 入口",
        input_schema=schema, effect=effect,
        read=read if effect is ToolEffect.READ else None,
        prepare=None if effect is ToolEffect.READ else prepare,
        execute_confirmed=None if effect is ToolEffect.READ else lambda *_: pytest.fail("预检不得写入"))
    state = InMemorySessionStateStore()
    lease, _ = await state.begin_turn(owner="owner", session_id="composition", request_id="r")
    context = ToolCallContext(lease.owner, lease.session_id, lease.request_id, lease.turn_id, lease,
                             CancellationToken(), lambda _: asyncio.sleep(0))
    return ToolPipeline(catalog=ToolCatalog([tool]), state_store=state), context


async def _reject(pipeline, context, arguments):
    with pytest.raises(ToolPipelineError) as error:
        await pipeline.execute("lookup", arguments, context=context)
    assert error.value.code == "invalid_arguments"


async def _accept(pipeline, context, arguments, effect, seen):
    result = await pipeline.execute("lookup", arguments, context=context)
    assert result.arguments == arguments and seen == [arguments]
    assert bool(result.effect_plan) is (effect is not ToolEffect.READ)
    if result.effect_plan:
        assert dict(result.effect_plan.arguments) == arguments


@pytest.mark.parametrize("composition", ["oneOf", "anyOf"])
@pytest.mark.parametrize("effect", [ToolEffect.READ, ToolEffect.WRITE, ToolEffect.DANGER])
def test_nested_union_retry_can_select_another_valid_operation(composition, effect):
    async def run():
        branches = [{"type": "object", "required": ["operation", "arguments"],
            "additionalProperties": False, "properties": {
                "operation": {"enum": [operation]}, "arguments": {"type": "object"}}}
            for operation in ("first", "second")]
        schema = {"type": "object", "required": ["steps"], "properties": {
            "steps": {"type": "array", "items": {composition: branches}}}}
        seen = []
        pipeline, context = await _pipeline(schema, effect, seen)
        await _reject(pipeline, context, {"steps": [{"operation": "first", "parameters": {"limit": 2}}]})
        await _reject(pipeline, context, {"steps": [{
            "operation": "second", "arguments": {"limit": 2}, "parameters": {}}]})
        assert not seen
        await _accept(pipeline, context, {"steps": [
            {"operation": "second", "arguments": {"limit": 2}}]}, effect, seen)
    asyncio.run(run())


@pytest.mark.parametrize("composition", ["not", "oneOf"])
@pytest.mark.parametrize("correction,valid_without_branch,valid_one_branch", [
    ({"identity": "B", "left": "changed"}, True, True),
    ({"identity": "A", "right": "y", "filter": "wide"}, True, True),
    ({"identity": "A"}, True, False),
    ({}, False, False),
    ({"identity": "A", "left": 4}, False, False),
    ({"identity": "A", "left": "x", "right": "y"}, False, False),
], ids=["change-identity-drop-filter", "select-right-change-filter", "drop-optional-branches",
        "missing-required", "wrong-type", "both-branches"])
@pytest.mark.parametrize("effect", [ToolEffect.READ, ToolEffect.WRITE, ToolEffect.DANGER])
def test_collision_retry_obeys_required_types_and_current_branch(composition, correction,
                                                                 valid_without_branch, valid_one_branch, effect):
    async def run():
        schema = {"type": "object", "required": ["identity"], "properties": {
            key: {"type": "string"} for key in ("identity", "left", "right", "filter")},
            **({"not": {"required": ["left", "right"]}} if composition == "not" else {
                "oneOf": [{"required": ["left"]}, {"required": ["right"]}]})}
        seen = []
        pipeline, context = await _pipeline(schema, effect, seen)
        await _reject(pipeline, context, {"identity": "A", "left": "x", "right": "y", "filter": "narrow"})
        assert not seen
        allowed = valid_without_branch if composition == "not" else valid_one_branch
        if allowed:
            await _accept(pipeline, context, correction, effect, seen)
        else:
            await _reject(pipeline, context, correction)
            assert not seen
            # 再次合法预检不继承前次的类型错误或冲突。
            await _accept(pipeline, context, {"identity": "A", "right": "z"}, effect, seen)
    asyncio.run(run())


@pytest.mark.parametrize("effect", [ToolEffect.READ, ToolEffect.WRITE, ToolEffect.DANGER])
def test_complex_oneof_retry_rechecks_enum_instead_of_preserving_old_branch(effect):
    async def run():
        schema = {"type": "object", "properties": {
            "left": {"type": "string"}, "right": {"type": "string"}}, "oneOf": [
                {"required": ["left"], "properties": {"left": {"enum": ["x"]}}},
                {"required": ["right"]}]}
        seen = []
        pipeline, context = await _pipeline(schema, effect, seen)
        await _reject(pipeline, context, {"left": "x", "right": "y"})
        await _reject(pipeline, context, {"left": "wrong"})
        assert not seen
        await _accept(pipeline, context, {"right": "y"}, effect, seen)
    asyncio.run(run())
