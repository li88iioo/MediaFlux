"""参数风险与冻结票据回归；仅使用内存 handler，不访问业务或网络。"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock, call

import pytest

from app.agent.confirmation import ConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.agent.models import ToolResult


class ArgumentRig:
    def __init__(self):
        self.state = InMemorySessionStateStore()
        self.effects = ConfirmationEffectPlanStore(ConfirmationStore())
        self.prepared, self.writes = [], []

    def tool(self, name="test.arguments", effect=ToolEffect.WRITE, resolver=None):
        def prepare(arguments, _context):
            self.prepared.append((name, deepcopy(arguments)))
            return PreparedEffect(
                preview=ToolResult(True, "preview", "仅准备内存变更").to_dict(),
                arguments=deepcopy(arguments), snapshot_fingerprint="snapshot",
            )

        def execute(arguments, expected, _context):
            assert expected == "snapshot"
            self.writes.append((name, deepcopy(arguments)))
            return ToolResult(True, "completed", "内存变更完成")

        return KernelToolSpec(
            name=name, domain="test", description="参数风险测试工具", effect=effect,
            effect_resolver=resolver, prepare=prepare, execute_confirmed=execute,
            input_schema={"type": "object", "required": ["target", "items"],
                          "additionalProperties": False, "properties": {
                              "target": {"type": "string"},
                              "items": {"type": "array", "items": {"type": "string"}},
                          }},
            validator=lambda a: {**deepcopy(a), "target": a["target"].strip().lower()},
            metadata={"batch_mode": "sync"},
        )

    def pipeline(self, *tools):
        return ToolPipeline(catalog=ToolCatalog(tools), state_store=self.state,
                            effect_store=self.effects)

    async def context(self):
        lease, _ = await self.state.begin_turn(
            owner="argument-owner", session_id="argument-session", request_id="prepare",
        )
        return ToolCallContext(
            owner=lease.owner, session_id=lease.session_id, request_id=lease.request_id,
            turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(),
            report_progress=lambda _payload: asyncio.sleep(0),
        )


@pytest.fixture
def rig():
    return ArgumentRig()


@pytest.mark.parametrize("target,default,resolved", [
    ("local", ToolEffect.WRITE, None),
    ("local", ToolEffect.WRITE, ToolEffect.WRITE),
    ("cloud", ToolEffect.WRITE, ToolEffect.DANGER),
    ("cloud", ToolEffect.DANGER, ToolEffect.DANGER),
])
def test_argument_risk_freezes_and_confirms_once(rig, target, default, resolved):
    async def run():
        resolver = Mock(side_effect=lambda a: ToolEffect.DANGER if a["target"] == "cloud"
                        else default) if resolved is not None else None
        tool = rig.tool(effect=default, resolver=resolver)
        if resolver is None:
            assert tool.for_arguments({}) is tool
        pipeline, context = rig.pipeline(tool), await rig.context()
        supplied = {"target": f" {target.upper()} ", "items": ["original"]}
        frozen = {"target": target, "items": ["original"]}
        preview = await pipeline.execute(tool.name, supplied, context=context)
        plan = preview.effect_plan
        expected = resolved or default
        assert tool.effect is default
        assert preview.tool.effect is plan.effect is expected
        assert plan.public_approval_dict()["effect"] == expected.value
        assert plan.confirmation_contract["effect"] == expected.value
        assert plan.arguments == frozen and not rig.writes
        supplied["target"], supplied["items"][0] = "changed", "changed"
        plan.arguments["items"][0] = "tampered-card"
        result = await pipeline.execute_confirmed(plan.plan_id, context=context)
        assert result.tool.effect is expected and result.arguments == frozen
        assert result.outcome.public_content["ok"] is True, result.outcome.public_content
        assert rig.writes == rig.prepared == [(tool.name, frozen)]
        if resolver is not None:
            assert resolver.call_args_list == [call(frozen)] * 3
        with pytest.raises(ToolPipelineError, match="确认计划") as replay:
            await pipeline.execute_confirmed(plan.plan_id, context=context)
        assert replay.value.code == "confirmation_invalid"
        assert rig.writes == [(tool.name, frozen)]
    asyncio.run(run())


@pytest.mark.parametrize("cloud", [False, True])
def test_real_batch_keeps_sync_write_and_cloud_danger_cards(rig, cloud, monkeypatch):
    async def run():
        tools = [rig.tool("test.first"), rig.tool("test.second"),
                 rig.tool("test.cloud", effect=ToolEffect.DANGER)]
        from tests.test_agent_batch_confirmation import register_sdk_fixture
        register_sdk_fixture(monkeypatch, tools)
        pipeline, context = rig.pipeline(*tools), await rig.context()
        names = ["test.first", "test.cloud" if cloud else "test.second"]
        arguments = {"steps": [{"method": name, "arguments": {
            "target": f" {name.upper()} ", "items": [name],
        }} for name in names]}
        preview = await pipeline.execute("session.batch", arguments, context=context)
        plan = preview.effect_plan
        expected = ToolEffect.DANGER if cloud else ToolEffect.WRITE
        assert pipeline.catalog.get("session.batch").effect is ToolEffect.WRITE
        assert preview.tool.effect is plan.effect is expected
        assert plan.public_approval_dict()["confirmation"]["risk"] == expected.value
        assert [s["arguments"]["target"] for s in plan.arguments["steps"]] == names
        assert not rig.writes
        arguments["steps"][1]["method"] = "test.first"
        result = await pipeline.execute_confirmed(plan.plan_id, context=context)
        assert result.outcome.public_content["ok"] is True, result.outcome.public_content
        assert rig.writes == [(n, {"target": n, "items": [n]}) for n in names]
        with pytest.raises(ToolPipelineError) as replay:
            await pipeline.execute_confirmed(plan.plan_id, context=context)
        assert replay.value.code == "confirmation_invalid" and len(rig.writes) == 2
    asyncio.run(run())


@pytest.mark.parametrize("frozen,current", [
    (ToolEffect.WRITE, ToolEffect.DANGER), (ToolEffect.DANGER, ToolEffect.WRITE),
])
@pytest.mark.parametrize("late", [False, True], ids=["confirmation", "run-effect"])
def test_frozen_risk_drift_rejected_before_any_write(rig, frozen, current, late):
    async def run():
        # WRITE 旧票据由无 resolver 的原工具签发；DANGER 使用同一默认 WRITE 工具。
        tool = rig.tool(resolver=None if frozen is ToolEffect.WRITE else lambda _: frozen)
        pipeline, context = rig.pipeline(tool), await rig.context()
        arguments = {"target": "cloud", "items": ["original"]}
        plan = (await pipeline.execute(tool.name, arguments, context=context)).effect_plan
        assert plan.effect is frozen
        resolver = Mock(side_effect=[frozen, current] if late else [current])
        pipeline = rig.pipeline(replace(tool, effect_resolver=resolver))
        arguments["target"] = "local"
        with pytest.raises(ToolPipelineError) as rejected:
            await pipeline.execute_confirmed(plan.plan_id, context=context)
        assert rejected.value.code == "confirmation_invalid"
        assert resolver.call_args_list == [call(plan.arguments)] * (2 if late else 1)
        assert not rig.writes and len(rig.prepared) == 1
        assert await pipeline.get_pending_plan(
            owner=context.owner, session_id=context.session_id,
        ) is None
    asyncio.run(run())


@pytest.mark.parametrize("default,resolved", [
    (ToolEffect.WRITE, ToolEffect.READ), (ToolEffect.DANGER, ToolEffect.READ),
    (ToolEffect.DANGER, ToolEffect.WRITE), (ToolEffect.WRITE, None),
    (ToolEffect.WRITE, "write"), (ToolEffect.WRITE, "danger"),
    (ToolEffect.WRITE, 1), (ToolEffect.WRITE, {}),
])
def test_invalid_or_downgraded_effect_rejected_by_pipeline(rig, default, resolved):
    async def run():
        tool = rig.tool(effect=default, resolver=Mock(return_value=resolved))
        pipeline, context = rig.pipeline(tool), await rig.context()
        with pytest.raises(ValueError):
            await pipeline.execute(tool.name, {"target": "cloud", "items": []}, context=context)
        assert not rig.prepared and not rig.writes
        assert await pipeline.get_pending_plan(
            owner=context.owner, session_id=context.session_id,
        ) is None
    asyncio.run(run())


@pytest.mark.parametrize("resolved", [ToolEffect.READ, ToolEffect.DANGER])
def test_read_tool_cannot_define_resolver(rig, resolved):
    resolver = Mock(return_value=resolved)
    with pytest.raises(ValueError, match="READ"):
        replace(rig.tool(), effect=ToolEffect.READ, prepare=None, execute_confirmed=None,
                read=lambda *_: ToolResult(True, "completed", "只读"), effect_resolver=resolver)
    resolver.assert_not_called()


@pytest.mark.parametrize("invalid_validator", [False, True], ids=["schema", "validator"])
def test_argument_validation_precedes_effect_resolution(rig, invalid_validator):
    async def run():
        resolver = Mock(return_value=ToolEffect.DANGER)
        tool = rig.tool(resolver=resolver)
        arguments = {"target": "cloud", "items": []} if invalid_validator else {"target": 1}
        if invalid_validator:
            tool = replace(tool, validator=Mock(side_effect=ValueError("invalid")))
        pipeline, context = rig.pipeline(tool), await rig.context()
        with pytest.raises(ToolPipelineError) as rejected:
            await pipeline.execute(tool.name, arguments, context=context)
        assert rejected.value.code == "invalid_arguments"
        resolver.assert_not_called()
        assert not rig.prepared and not rig.writes
    asyncio.run(run())
