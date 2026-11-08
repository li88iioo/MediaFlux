"""生产短句的完整能力窗口与搜索引用→批量 EffectPlan 回归。"""
from __future__ import annotations

import unittest
from unittest.mock import ANY, patch

from tests.resource_presentation_test_harness import (
    new_resource_search_id,
    safe_resource_snapshot,
)

from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import (
    ModelEvent,
    ModelEventType,
    ModelToolCall,
)
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput
from app.agent.models import ToolReference
from tests.test_agent_kernel_resource_ingest import (
    SearchThenAnswerModel,
    _collect,
    _native_arguments,
    _search_result,
)


class SubmitTwoFromHistoryModel:
    def __init__(self):
        self.requests = []

    async def stream(self, request, *, cancellation):
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        yield ModelEvent(
            ModelEventType.TOOL_CALL_COMPLETED,
            tool_call=ModelToolCall("batch-followup", "resources.download", _native_arguments(request, [3, 4])),
        )
        yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")


class AgentKernelBtFollowupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from types import SimpleNamespace
        from app.indexers.result_store import IndexerResultStore
        from tests.support import isolated_test_database
        self.enterContext(isolated_test_database())
        service = SimpleNamespace(result_store=IndexerResultStore())
        self.enterContext(patch("app.indexers.runtime.get_indexer_service", return_value=service))
        self.enterContext(patch("app.agent.kernel.ports.sdk_effects.get_indexer_service", return_value=service))



    async def test_real_catalog_followup_builds_batch_plan_and_only_executes_after_confirmation(self):
        result = _search_result()
        template = result.data["items"][0]
        result.data["items"] = [{**template, "result_id": f"fixture-resource-{i}"} for i in range(1, 5)]
        result.references = [ToolReference(
            "resource_candidates", safe_resource_snapshot(result, search_id=new_resource_search_id())
        )]
        from tests.test_agent_kernel_resource_ingest import _runtime
        first, pipeline, state = _runtime(SearchThenAnswerModel("fixture.resource_read", {"title": "Example"}), result)
        catalog = pipeline.catalog
        await _collect(first.run(AgentInput(message="搜一下资源", owner="fixture-owner", session_id="fixture-session")))
        model = SubmitTwoFromHistoryModel()
        followup = AgentSession(model=model, catalog=catalog,
                                pipeline=pipeline, state_store=state)
        from tests.support import _prepare_batch
        with (
                        patch("app.indexers.downloads.preview_indexer_download_batch", side_effect=_prepare_batch) as prepare,
            patch("app.indexers.downloads.submit_indexer_download_batch", return_value={
                "ok": True, "status": "accepted", "summary": "已提交到光鸭", "error": "",
                "total": 2, "succeeded": 2, "failed": 0,
                "items": [{"result_id": f"fixture-resource-{position}", "status": "submitted", "request_id": identity} for position, identity in ((3,81), (4,82))],
            }) as execute,
        ):
            events = await _collect(followup.run(AgentInput(
                message="推送 3，4 到云盘", owner="fixture-owner", session_id="fixture-session"
            )))
            self.assertFalse(any(event.type is AgentEventType.TOOL_FAILED for event in events))
            plan = next(event.payload["plan"] for event in events if event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED)
            prepare.assert_called_once_with(ANY, ["fixture-resource-3", "fixture-resource-4"], "guangya", scope="agent")
            execute.assert_not_called()
            self.assertIn("resources__download", [tool["name"] for tool in model.requests[0].tools])
            confirmed = await _collect(followup.confirm(
                owner="fixture-owner", session_id="fixture-session", plan_id=plan["plan_id"]
            ))
            execute.assert_awaited_once_with(
                ANY, ["fixture-resource-3", "fixture-resource-4"], "guangya", scope="agent", origin_namespace="agent",
                expected_snapshot={"target": "guangya", "resources": [
                    {"result_id": identity, "target": "guangya"}
                    for identity in ("fixture-resource-3", "fixture-resource-4")
                ]}, verification_targets=None, verification_owner="",
            )
            self.assertTrue(any(event.type is AgentEventType.EFFECT_COMPLETED for event in confirmed))
