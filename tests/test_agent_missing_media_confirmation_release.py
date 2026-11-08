"""注销过期确认也释放补库预占，但不报告成功取消有效卡。"""
from __future__ import annotations

import asyncio

import pytest

from app.agent.confirmation import ConfirmationStore, SQLiteConfirmationStore
from app.agent.domains.library.missing_workflow_runtime import MissingMediaWorkflowRuntime
from app.repositories.agent_download_verification import SQLiteMissingMediaWorkflowRepository
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.models import ToolResult
from tests.support import isolated_test_database
from tests.test_agent_kernel_safety import _context
from tests.test_agent_missing_media_workflows import _VERIFICATION, _search_result


@pytest.mark.parametrize("persistent", [False, True], ids=["memory-ticket", "sqlite-ticket"])
@pytest.mark.parametrize("expired", [False, True], ids=["active", "expired"])
def test_cancellation_releases_frozen_followup_without_reviving_expired_card(persistent, expired):
    async def scenario():
        repository = SQLiteMissingMediaWorkflowRepository()
        repository.capture_search(owner="owner", **_search_result())
        writes = []

        def execute(arguments, expected, context):
            writes.append(arguments)
            return ToolResult(True, "accepted", "已受理")

        tool = KernelToolSpec(
            name="fixture.write", domain="fixture", description="补库确认生命周期", effect=ToolEffect.WRITE,
            input_schema={"type": "object", "properties": {}},
            prepare=lambda arguments, context: PreparedEffect(
                preview={"ok": True, "summary": "待确认"}, snapshot_fingerprint="snapshot",
                metadata={"missing_media_candidates": [{
                    "result_id": "resource-result-secret", "verification": _VERIFICATION, "candidate_title": "Show.E3", "target": "qb",
                }]},
            ),
            execute_confirmed=execute,
        )
        clock = [100.0]
        store = SQLiteKernelStore(secret_provider=lambda: "confirmation-release")
        tickets = (SQLiteConfirmationStore if persistent else ConfirmationStore)(clock=lambda: clock[0], ttl_seconds=10)
        pipeline = ToolPipeline(
            catalog=ToolCatalog([tool]), state_store=store,
            effect_store=ConfirmationEffectPlanStore(tickets),
            effect_lifecycle=MediaFluxEffectLifecycle(missing_media_runtime=MissingMediaWorkflowRuntime(repository=repository)),
        )
        context = await _context(store, owner="owner")
        preview = await pipeline.execute(tool.name, {}, context=context)
        plan = preview.effect_plan
        assert plan is not None
        assert repository.list_for_owner(owner="owner")[0]["item_state"] == "confirmation_required"
        if expired:
            clock[0] += 11
            assert await pipeline.get_pending_plan(owner="owner", session_id=context.session_id) is None
            if persistent:
                pipeline.effect_store = ConfirmationEffectPlanStore(SQLiteConfirmationStore(clock=lambda: clock[0], ttl_seconds=10))
        assert await pipeline.cancel_effect(plan.plan_id, lease=context.lease) is (not expired)
        assert not await pipeline.cancel_effect(plan.plan_id, lease=context.lease)
        assert await pipeline.get_confirmation_evidence(owner="owner", session_id=context.session_id) is None
        assert repository.list_for_owner(owner="owner")[0]["item_state"] == "selection_required"
        assert repository.select_candidate(
            owner="owner", verification=_VERIFICATION, candidate_title="Show.E3.new", target="qb",
        ) is not None
        assert writes == []

    with isolated_test_database():
        asyncio.run(scenario())
