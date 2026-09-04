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
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state import (
    CancellationToken,
    InMemorySessionStateStore,
    SessionState,
)
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
        self.assertEqual(len(catalog), 142)
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

    async def test_web_search_and_read_are_neighboring_read_capabilities(self) -> None:
        catalog = catalog_from_tool_specs(build_tool_specs())
        search = catalog.get("web.search")
        read = catalog.get("web.read")

        self.assertEqual(search.effect, ToolEffect.READ)
        self.assertEqual(read.effect, ToolEffect.READ)
        self.assertIn("web.read", search.metadata["related_tools"])
        self.assertIn("web.search", read.metadata["related_tools"])
        selection = CapabilityRetriever().retrieve(
            "搜索 2026 年新番并打开官方公告核对发布日期",
            catalog,
        )
        self.assertIn("web.search", selection.names)
        self.assertIn("web.read", selection.names)

    async def test_short_followup_reuses_recent_media_library_context(self) -> None:
        catalog = catalog_from_tool_specs(build_tool_specs())
        state = SessionState(
            owner="owner",
            session_id="session",
            conversation=[
                {
                    "role": "user",
                    "content": "黄泉使者 我的 Jellyfin 媒体库中有吗",
                },
                {"role": "assistant", "content": "已查询。"},
            ],
        )
        selection = CapabilityRetriever().retrieve(
            "绿灯军团呢",
            catalog,
            context=AgentSession._capability_retrieval_context(state),
        )

        self.assertIn("library.search", selection.names)
        self.assertIn("provider.capabilities", selection.names)
        self.assertIn("provider.query", selection.names)

    async def test_resource_version_followup_keeps_inspect_and_submit_tools(self) -> None:
        catalog = catalog_from_tool_specs(build_tool_specs())
        state = SessionState(
            owner="owner",
            session_id="session",
            conversation=[
                {
                    "role": "user",
                    "content": "搜索绿灯军团资源，有的话推送到云盘",
                },
                {
                    "role": "assistant",
                    "content": "找到候选。",
                    "tool_calls": [
                        {
                            "call_id": "search-1",
                            "name": "indexer.search_resources",
                            "arguments": {},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "content": "候选已保存",
                    "tool_call_id": "search-1",
                    "tool_name": "indexer.search_resources",
                },
                {"role": "assistant", "content": "请选择版本。"},
            ],
        )
        selection = CapabilityRetriever().retrieve(
            "推送4K版",
            catalog,
            context=AgentSession._capability_retrieval_context(state),
        )

        self.assertIn("indexer.search_resources", selection.names)
        self.assertIn("ingest.inspect", selection.names)
        self.assertIn("ingest.submit", selection.names)

    async def test_global_library_count_selects_provider_count_path(self) -> None:
        catalog = catalog_from_tool_specs(build_tool_specs())
        selection = CapabilityRetriever().retrieve(
            "我媒体库中有多少资源", catalog
        )

        self.assertIn("provider.capabilities", selection.names)
        self.assertIn("provider.query", selection.names)

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

    async def test_agent_capabilities_returns_compact_domain_summary(self) -> None:
        catalog = catalog_from_tool_specs(build_tool_specs())
        state = InMemorySessionStateStore()
        result = await ToolPipeline(catalog=catalog, state_store=state).execute(
            "agent.capabilities",
            {},
            context=await context_for(state),
        )

        public = result.outcome.public_content
        self.assertEqual(public["data"]["total_tools"], 142)
        self.assertGreaterEqual(len(public["data"]["groups"]), 6)
        self.assertNotIn("tools", public["data"])
        self.assertNotIn("parameters", str(public))
        self.assertLess(len(result.outcome.model_content), 8_000)

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
