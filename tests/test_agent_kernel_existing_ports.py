from __future__ import annotations

import unittest
from unittest.mock import patch

from app.agent.kernel.bootstrap import build_tool_catalog
from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect

from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.projection import DefaultProjector
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.agent.models import ToolResult


async def context_for(
    state, *, owner="owner", session="session", wait_for_completion=False
):
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
        wait_for_completion=wait_for_completion,
    )


class ExistingDomainPortTests(unittest.IsolatedAsyncioTestCase):
    def test_native_share_schema_requires_real_files_and_no_candidate_reference(self):
        from app.agent.schema import validate_json_schema
        from app.agent.kernel.ports.sdk_effects import share_transfer_tool
        tool = share_transfer_tool()
        valid = {"url": "https://www.guangyapan.com/s/share", "file_ids": ["real-file-201"]}
        validate_json_schema(valid, tool.input_schema)
        for arguments in ({}, {"url": valid["url"]}, {**valid, "file_ids": []},
                          {**valid, "file_ids": [True]}, {**valid, "target": "qb"},
                          {**valid, "ingest_snapshot_ref": "ref_"+"x"*24}):
            with self.subTest(arguments=arguments), self.assertRaises(ToolPipelineError):
                validate_json_schema(arguments, tool.input_schema)

    async def test_direct_retry_noop_returns_without_freezing_or_execution(self):
        from unittest.mock import Mock
        for effect in (ToolEffect.WRITE, ToolEffect.DANGER):
            with self.subTest(effect=effect):
                execute = Mock(side_effect=AssertionError("no cloud write"))
                tool = KernelToolSpec(domain="fixture", name="config.noop", description="预检明确无需变更", effect=effect,
                    input_schema={"type": "object", "required": ["value"], "properties": {"value": {"type": "integer"}}},
                    validator=dict,
                    prepare=lambda _args, _ctx: DefaultProjector().project(ToolResult(True, "no_changes", "已符合目标，不执行任何写入")),
                    execute_confirmed=execute)
                state = InMemorySessionStateStore()
                pipeline = ToolPipeline(catalog=ToolCatalog([tool]), state_store=state)
                context = await context_for(state)
                with self.assertRaises(ToolPipelineError):
                    await pipeline.execute(tool.name, {"value": "invalid"}, context=context)
                with patch.object(pipeline.effect_store, "freeze", side_effect=AssertionError("must not issue an empty confirmation")):
                    result = await pipeline.execute(tool.name, {"value": 0}, context=context)
                self.assertIsNone(result.effect_plan)
                self.assertEqual(result.outcome.public_content["status"], "no_changes")
                self.assertFalse((await state.load(owner="owner", session_id="session")).pending_effect_plan_id)
                execute.assert_not_called()

    async def test_post_write_verifier_keeps_failed_outcome_and_receipt(self):
        for status in ("conflict", "outcome_unknown"):
            with self.subTest(status=status):
                domain_result = ToolResult(False, status, "最终状态未确认", data={"receipt": "safe-receipt"}, error="请先核验而非重试")
                tool = KernelToolSpec(domain="fixture", name="config.test_write", description="测试配置写入", effect=ToolEffect.WRITE,
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    validator=lambda arguments: arguments,
                    prepare=lambda _args, _ctx: PreparedEffect(preview={"summary": "预检"}, snapshot_fingerprint="fingerprint"),
                    execute_confirmed=lambda _args, _snapshot, _ctx, result=domain_result: result,
                    verify=lambda _args, result, _ctx: result)
                catalog = ToolCatalog([tool])
                state = InMemorySessionStateStore()
                pipeline = ToolPipeline(catalog=catalog, state_store=state)
                context = await context_for(state)
                preview = await pipeline.execute(tool.name, {}, context=context)
                assert preview.effect_plan is not None
                outcome = await pipeline.execute_confirmed(preview.effect_plan.plan_id, context=context)
                public = outcome.outcome.public_content
                self.assertFalse(public["ok"])
                self.assertEqual(public["status"], status)
                self.assertEqual(public["data"]["receipt"], "safe-receipt")
                self.assertEqual(public["error"], "请先核验而非重试")

    async def test_native_executor_catalog_contains_only_complete_effect_contracts(self):
        catalog = build_tool_catalog()
        self.assertTrue(catalog)
        for tool in catalog.visible():
            if tool.effect is ToolEffect.READ:
                self.assertIsNotNone(tool.read)
            else:
                self.assertIsNotNone(tool.prepare)
                self.assertIsNotNone(tool.execute_confirmed)
            self.assertNotIn(".", tool.model_name)
            self.assertEqual(tool.model_definition()["name"], tool.model_name)


    async def test_resource_and_web_search_retire_without_legacy_fallback(self):
        from app import sdk
        catalog = build_tool_catalog()
        for name in ("indexer.search_resources", "web.search", "web.read"):
            self.assertFalse(catalog.has(name))
        self.assertIn("resources.search", sdk.METHODS)
        self.assertIn("resources.present", sdk.METHODS)

    async def test_retired_inventory_reads_are_replaced_by_native_sdk(self):
        from app import sdk
        catalog = build_tool_catalog()
        for name in ("library.count_series_episodes", "library.batch_presence"):
            self.assertFalse(catalog.has(name))
            with self.assertRaises(KeyError):
                catalog.get(name)
        self.assertIn("library.episodes", sdk.METHODS)

    async def test_native_download_remains_an_internal_confirmed_effect(self):
        from app import sdk
        tool = build_tool_catalog().get("resources.download")
        self.assertIn(tool.name, sdk.WRITE_METHODS)
        self.assertIn(tool.effect, (ToolEffect.WRITE, ToolEffect.DANGER))
        self.assertIsNotNone(tool.prepare)
        self.assertIsNotNone(tool.execute_confirmed)


    async def test_local_playback_reads_are_native_sdk_capabilities(self) -> None:
        from app import sdk
        catalog = build_tool_catalog()
        methods = {item["name"]: item for item in sdk.describe_methods()}
        for name in ("library.history", "library.resume", "library.recommend"):
            self.assertIn(name, methods)
            self.assertNotIn(name, sdk.WRITE_METHODS)
        for name in ("media.recommend_from_library", "media.recently_played", "media.continue_watching"):
            self.assertFalse(catalog.has(name))
            self.assertFalse(catalog.has(name.replace(".", "__")))

    async def test_person_filmography_uses_native_sdk_without_retired_inventory_tool(self):
        from app import sdk
        catalog = build_tool_catalog()
        self.assertFalse(catalog.has("discovery.person_filmography"))
        self.assertFalse(catalog.has("library.batch_presence"))
        self.assertTrue({"media.people", "media.filmography", "media.credits"} <= sdk.METHODS.keys())


    async def test_domain_owned_opaque_refs_are_not_misresolved_as_kernel_refs(
        self,
    ) -> None:
        captured = {}

        def handler(arguments, _context):
            captured.update(arguments)
            return ToolResult(True, "success", "领域引用已接收")

        tool = KernelToolSpec(domain="fixture",
            name="fixture.domain_ref_check",
            description="验证夹具自有字段透传",
            effect=ToolEffect.READ,
            input_schema={
                "type": "object",
                "required": ["fixture_payload"],
                "properties": {"fixture_payload": {"type": "string"}},
                "additionalProperties": False,
            },
            validator=lambda value: dict(value),
            read=handler,
        )
        from app.agent.kernel.capabilities import ToolCatalog

        catalog = ToolCatalog([tool])
        state = InMemorySessionStateStore()
        pipeline = ToolPipeline(catalog=catalog, state_store=state)
        await pipeline.execute(
            tool.name,
            {"fixture_payload": "fixture-value"},
            context=await context_for(state),
        )
        self.assertEqual(captured["fixture_payload"], "fixture-value")

    async def test_unexpected_domain_exception_cannot_leak_paths_or_credentials(
        self,
    ) -> None:
        def handler(_arguments, _context):
            raise RuntimeError(
                "failed at /home/aio/private/media with token=sk-secretsecretsecret1234"
            )

        tool = KernelToolSpec(domain="fixture",
            name="cloud.failure_boundary",
            description="验证异常边界",
            effect=ToolEffect.READ,
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            validator=lambda value: dict(value),
            read=handler,
        )
        from app.agent.kernel.capabilities import ToolCatalog

        catalog = ToolCatalog([tool])
        state = InMemorySessionStateStore()
        pipeline = ToolPipeline(catalog=catalog, state_store=state)

        with self.assertRaises(ToolPipelineError) as raised:
            await pipeline.execute(tool.name, {}, context=await context_for(state))

        self.assertEqual(raised.exception.code, "tool_execution_failed")
        self.assertEqual(str(raised.exception), "工具执行失败")
        self.assertNotIn("/home/aio", str(raised.exception))
        self.assertNotIn("sk-secret", str(raised.exception))

    async def test_declared_safe_domain_error_is_preserved_after_sanitizing(
        self,
    ) -> None:
        def handler(_arguments, _context):
            raise ToolPipelineError(
                "目标状态已变化,请重新检查", code="precondition_failed"
            )

        tool = KernelToolSpec(domain="fixture",
            name="cloud.safe_failure",
            description="验证安全领域错误",
            effect=ToolEffect.READ,
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            validator=lambda value: dict(value),
            read=handler,
        )
        from app.agent.kernel.capabilities import ToolCatalog

        catalog = ToolCatalog([tool])
        state = InMemorySessionStateStore()
        pipeline = ToolPipeline(catalog=catalog, state_store=state)

        with self.assertRaises(ToolPipelineError) as raised:
            await pipeline.execute(tool.name, {}, context=await context_for(state))

        self.assertEqual(raised.exception.code, "precondition_failed")
        self.assertEqual(str(raised.exception), "目标状态已变化,请重新检查")

    async def test_native_write_contract_becomes_frozen_effect(self) -> None:
        calls = {"execute": 0}

        def validate(arguments):
            return {"name": str(arguments["name"])}

        def prepare(arguments, _context):
            return PreparedEffect(preview={"summary": f"将更新 {arguments['name']}"},
                                  snapshot_fingerprint="fingerprint-v1")

        def execute(arguments, expected, _context):
            self.assertEqual(expected, "fingerprint-v1")
            calls["execute"] += 1
            return ToolResult(True, "success", f"已更新 {arguments['name']}")

        tool = KernelToolSpec(domain="fixture",
            name="config.example_update",
            description="更新示例配置",
            effect=ToolEffect.WRITE,
            input_schema={
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
                "additionalProperties": False,
            },
            validator=validate,
            prepare=prepare,
            execute_confirmed=execute,
        )
        state = InMemorySessionStateStore()
        from app.agent.kernel.capabilities import ToolCatalog

        catalog = ToolCatalog([tool])
        pipeline = ToolPipeline(catalog=catalog, state_store=state)
        context = await context_for(state)
        preview = await pipeline.execute(tool.name, {"name": "x"}, context=context)
        self.assertEqual(calls["execute"], 0)
        assert (preview.effect_plan) is not None
        await pipeline.execute_confirmed(preview.effect_plan.plan_id, context=context)
        self.assertEqual(calls["execute"], 1)
