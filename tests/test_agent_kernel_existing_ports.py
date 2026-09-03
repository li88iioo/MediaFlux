from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import patch

from app.agent.domain_catalog import build_tool_specs
from app.agent.kernel.capabilities import CapabilityRetriever, ToolEffect
from app.agent.kernel.pipeline import ToolCallContext, ToolPipeline, ToolPipelineError
from app.agent.kernel.ports.existing_actions import (
    adapt_tool_spec,
    catalog_from_tool_specs,
)
from app.agent.kernel.state import CancellationToken, InMemorySessionStateStore
from app.agent.models import RiskLevel, ToolResult, ToolSpec


async def context_for(state, *, owner="owner", session="session"):
    lease, _ = await state.begin_turn(
        owner=owner, session_id=session, request_id="request"
    )

    async def progress(_payload):
        return None

    return ToolCallContext(
        owner=owner,
        session_id=session,
        request_id="request",
        turn_id=lease.turn_id,
        lease=lease,
        cancellation=CancellationToken(),
        report_progress=progress,
    )


class ExistingDomainPortTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_existing_atomic_tools_can_be_declared_to_kernel(self) -> None:
        catalog = catalog_from_tool_specs(build_tool_specs())
        self.assertEqual(len(catalog), 139)
        self.assertFalse(catalog.has("agent.cancel_pending_action"))
        selection = CapabilityRetriever().retrieve(
            "规整光鸭云盘动漫目录并按 TMDB 集数重命名",
            catalog,
        )
        self.assertLessEqual(len(selection.tools), 12)
        self.assertTrue(any(tool.domain == "cloud" for tool in selection.tools))
        self.assertTrue(all("." not in tool.model_name for tool in selection.tools))
        self.assertTrue(
            all(
                tool.model_definition()["name"] == tool.model_name
                for tool in selection.tools
            )
        )

    async def test_existing_read_action_executes_through_new_pipeline_only(
        self,
    ) -> None:
        catalog = catalog_from_tool_specs(build_tool_specs())
        state = InMemorySessionStateStore()
        pipeline = ToolPipeline(catalog=catalog, state_store=state)
        result = await pipeline.execute(
            "agent.runtime_status",
            {},
            context=await context_for(state),
        )
        self.assertEqual(result.tool.effect, ToolEffect.READ)
        self.assertTrue(result.outcome.public_content["ok"])
        self.assertIn("agent_enabled", result.outcome.public_content["data"])

    async def test_domain_owned_opaque_refs_are_not_misresolved_as_kernel_refs(
        self,
    ) -> None:
        captured = {}

        def handler(arguments):
            captured.update(arguments)
            return ToolResult(True, "success", "领域引用已接收")

        spec = ToolSpec(
            name="cloud.domain_ref_check",
            description="验证领域自己的 observation_ref",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["observation_ref"],
                "properties": {"observation_ref": {"type": "string"}},
                "additionalProperties": False,
            },
            validator=lambda value: dict(value),
            handler=handler,
        )
        tool = adapt_tool_spec(spec)
        from app.agent.kernel.capabilities import ToolCatalog

        catalog = ToolCatalog([tool])
        state = InMemorySessionStateStore()
        pipeline = ToolPipeline(catalog=catalog, state_store=state)
        await pipeline.execute(
            tool.name,
            {"observation_ref": "GYOBS-v1-domain-owned"},
            context=await context_for(state),
        )
        self.assertEqual(captured["observation_ref"], "GYOBS-v1-domain-owned")

    async def test_production_effect_plan_binds_runtime_generation(self) -> None:
        calls = []

        def prepare(_arguments, _context):
            return ToolResult(True, "preview", "将执行"), "domain-snapshot"

        def execute(_arguments, expected, _context):
            calls.append(expected)
            return ToolResult(True, "success", "已执行")

        legacy = ToolSpec(
            name="config.runtime_bound",
            description="验证运行态绑定",
            risk=RiskLevel.WRITE,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            validator=lambda value: dict(value),
            requires_confirmation=True,
            context_confirmation_preparer=prepare,
            context_confirmed_handler=execute,
        )
        tool = adapt_tool_spec(legacy)
        from app.agent.kernel.capabilities import ToolCatalog

        catalog = ToolCatalog([tool])
        state = InMemorySessionStateStore()
        pipeline = ToolPipeline(catalog=catalog, state_store=state)
        owner = "webk:v1:" + "a" * 64
        context = await context_for(state, owner=owner)

        with patch(
            "app.agent.kernel.ports.existing_actions.current_agent_runtime_generation",
            return_value=7,
        ):
            preview = await pipeline.execute(tool.name, {}, context=context)
        self.assertTrue(preview.effect_plan.snapshot_fingerprint.startswith("mfkr1:7:"))

        @contextmanager
        def admitted(*, require_telegram, expected_generation):
            self.assertFalse(require_telegram)
            self.assertEqual(expected_generation, 7)
            yield expected_generation

        with patch(
            "app.agent.kernel.ports.existing_actions.agent_runtime_admission",
            side_effect=admitted,
        ):
            await pipeline.execute_confirmed(
                preview.effect_plan.plan_id, context=context
            )
        self.assertEqual(calls, ["domain-snapshot"])

    async def test_unexpected_domain_exception_cannot_leak_paths_or_credentials(
        self,
    ) -> None:
        def handler(_arguments):
            raise RuntimeError(
                "failed at /home/aio/private/media with token=sk-secretsecretsecret1234"
            )

        spec = ToolSpec(
            name="cloud.failure_boundary",
            description="验证异常边界",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            validator=lambda value: dict(value),
            handler=handler,
        )
        tool = adapt_tool_spec(spec)
        from app.agent.kernel.capabilities import ToolCatalog

        catalog = ToolCatalog([tool])
        state = InMemorySessionStateStore()
        pipeline = ToolPipeline(catalog=catalog, state_store=state)

        with self.assertRaises(ToolPipelineError) as raised:
            await pipeline.execute(tool.name, {}, context=await context_for(state))

        self.assertEqual(raised.exception.code, "tool_execution_failed")
        self.assertEqual(str(raised.exception), "领域能力暂时不可用")
        self.assertNotIn("/home/aio", str(raised.exception))
        self.assertNotIn("sk-secret", str(raised.exception))

    async def test_declared_safe_domain_error_is_preserved_after_sanitizing(
        self,
    ) -> None:
        from app.agent.errors import AgentToolError

        def handler(_arguments):
            raise AgentToolError(
                "目标状态已变化,请重新检查", code="precondition_failed"
            )

        spec = ToolSpec(
            name="cloud.safe_failure",
            description="验证安全领域错误",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            validator=lambda value: dict(value),
            handler=handler,
        )
        tool = adapt_tool_spec(spec)
        from app.agent.kernel.capabilities import ToolCatalog

        catalog = ToolCatalog([tool])
        state = InMemorySessionStateStore()
        pipeline = ToolPipeline(catalog=catalog, state_store=state)

        with self.assertRaises(ToolPipelineError) as raised:
            await pipeline.execute(tool.name, {}, context=await context_for(state))

        self.assertEqual(raised.exception.code, "precondition_failed")
        self.assertEqual(str(raised.exception), "目标状态已变化,请重新检查")

    async def test_existing_write_contract_becomes_frozen_effect(self) -> None:
        calls = {"execute": 0}

        def validate(arguments):
            return {"name": str(arguments["name"])}

        def prepare(arguments, _context):
            return ToolResult(
                True, "preview", f"将更新 {arguments['name']}"
            ), "fingerprint-v1"

        # Keep the real legacy tuple contract explicit.
        def legacy_prepare(arguments, _context):
            result, fingerprint = prepare(arguments, _context)
            return result, fingerprint

        def execute(arguments, expected, _context):
            self.assertEqual(expected, "fingerprint-v1")
            calls["execute"] += 1
            return ToolResult(True, "success", f"已更新 {arguments['name']}")

        legacy = ToolSpec(
            name="config.example_update",
            description="更新示例配置",
            risk=RiskLevel.WRITE,
            parameters={
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
                "additionalProperties": False,
            },
            validator=validate,
            requires_confirmation=True,
            context_confirmation_preparer=legacy_prepare,
            context_confirmed_handler=execute,
        )
        tool = adapt_tool_spec(legacy)
        state = InMemorySessionStateStore()
        from app.agent.kernel.capabilities import ToolCatalog

        catalog = ToolCatalog([tool])
        pipeline = ToolPipeline(catalog=catalog, state_store=state)
        context = await context_for(state)
        preview = await pipeline.execute(tool.name, {"name": "x"}, context=context)
        self.assertEqual(calls["execute"], 0)
        self.assertIsNotNone(preview.effect_plan)
        await pipeline.execute_confirmed(preview.effect_plan.plan_id, context=context)
        self.assertEqual(calls["execute"], 1)
