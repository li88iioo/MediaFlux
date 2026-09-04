"""新 Kernel 资源搜索到统一下载提交的端到端引用链路。"""

from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import AsyncIterator
from dataclasses import replace
from unittest.mock import patch

from app.agent.domain_catalog import build_tool_specs
from app.agent.ingest_actions import AgentIngestSessionStore
from app.agent.kernel.capabilities import CapabilityRetriever
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import (
    ModelEvent,
    ModelEventType,
    ModelRequest,
    ModelToolCall,
)
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.existing_actions import catalog_from_tool_specs
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state import AgentInput, InMemorySessionStateStore
from app.agent.models import ToolReference, ToolResult
from app.agent.recent_resource_candidates import (
    RecentResourceCandidateStore,
    new_resource_search_id,
    safe_resource_snapshot,
)


def _search_result() -> ToolResult:
    result = ToolResult(
        True,
        "success",
        "找到 1 项可提交资源",
        data={
            "query": "绿灯军团",
            "items": [
                {
                    "result_id": "green-lantern-4k-001",
                    "title": "Lanterns.S01E01.2160p.WEB-DL.DV.HDR",
                    "site_id": "demo",
                    "site_name": "Demo",
                    "size_text": "3.60 GB",
                    "download_state": "ready",
                    "download_kinds": ["magnet"],
                }
            ],
        },
    )
    result.references.append(
        ToolReference(
            "resource_candidates",
            safe_resource_snapshot(result, search_id=new_resource_search_id()),
        )
    )
    return result


def _reference_from(request: ModelRequest) -> str:
    for message in reversed(request.messages):
        if message.role != "tool":
            continue
        for line in message.content.splitlines():
            if not line.startswith("reference_arguments="):
                continue
            payload = json.loads(line.partition("=")[2])
            return str(payload["resource_candidates_ref"])
    raise AssertionError("resource_candidates_ref missing from model history")


class SameTurnSearchSubmitModel:
    def __init__(self) -> None:
        self.round = 0
        self.requests: list[ModelRequest] = []
        self.resource_candidates_ref = ""

    async def stream(
        self, request: ModelRequest, *, cancellation
    ) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        cancellation.raise_if_cancelled()
        await asyncio.sleep(0)
        if self.round == 0:
            self.round += 1
            yield ModelEvent(
                ModelEventType.TOOL_CALL_COMPLETED,
                tool_call=ModelToolCall(
                    "search-1",
                    "indexer.search_resources",
                    {"title": "绿灯军团"},
                ),
            )
            yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")
            return
        self.resource_candidates_ref = _reference_from(request)
        yield ModelEvent(
            ModelEventType.TOOL_CALL_COMPLETED,
            tool_call=ModelToolCall(
                "submit-1",
                "ingest.submit",
                {
                    "source_type": "resource_candidates",
                    "target": "guangya",
                    "positions": [1],
                    "resource_candidates_ref": self.resource_candidates_ref,
                },
            ),
        )
        yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")


class SearchThenAnswerModel:
    def __init__(self) -> None:
        self.round = 0

    async def stream(
        self, request: ModelRequest, *, cancellation
    ) -> AsyncIterator[ModelEvent]:
        del request
        cancellation.raise_if_cancelled()
        await asyncio.sleep(0)
        if self.round == 0:
            self.round += 1
            yield ModelEvent(
                ModelEventType.TOOL_CALL_COMPLETED,
                tool_call=ModelToolCall(
                    "search-1",
                    "indexer.search_resources",
                    {"title": "绿灯军团"},
                ),
            )
            yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")
            return
        yield ModelEvent(ModelEventType.TEXT_DELTA, text="已找到 4K 候选。")
        yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")


class SubmitFromHistoryModel:
    def __init__(self) -> None:
        self.resource_candidates_ref = ""

    async def stream(
        self, request: ModelRequest, *, cancellation
    ) -> AsyncIterator[ModelEvent]:
        cancellation.raise_if_cancelled()
        await asyncio.sleep(0)
        self.resource_candidates_ref = _reference_from(request)
        yield ModelEvent(
            ModelEventType.TOOL_CALL_COMPLETED,
            tool_call=ModelToolCall(
                "submit-history-1",
                "ingest.submit",
                {
                    "source_type": "resource_candidates",
                    "target": "guangya",
                    "positions": [1],
                    "resource_candidates_ref": self.resource_candidates_ref,
                },
            ),
        )
        yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")


async def _collect(stream) -> list:
    return [event async for event in stream]


def _runtime(model):
    resource_store = RecentResourceCandidateStore()
    ingest_store = AgentIngestSessionStore()
    specs = {spec.name: spec for spec in build_tool_specs(resource_store, ingest_store)}
    search_spec = replace(
        specs["indexer.search_resources"],
        handler=lambda _arguments: _search_result(),
    )
    catalog = catalog_from_tool_specs((search_spec, specs["ingest.submit"]))
    state = InMemorySessionStateStore()
    pipeline = ToolPipeline(catalog=catalog, state_store=state)
    session = AgentSession(
        model=model,
        catalog=catalog,
        retriever=CapabilityRetriever(),
        pipeline=pipeline,
        state_store=state,
    )
    return session, pipeline, state


class AgentKernelResourceIngestTests(unittest.IsolatedAsyncioTestCase):
    @patch("app.agent.indexer_candidate_actions.submit_resource_confirmed")
    @patch("app.agent.indexer_candidate_actions.prepare_submit_resource")
    async def test_same_turn_search_can_preview_and_confirm_cloud_submit(
        self, prepare_resource, submit_resource
    ) -> None:
        prepare_resource.side_effect = lambda arguments: (
            ToolResult(
                True,
                "confirmation_required",
                "确认后提交 1 项资源",
                data={"resource": {"title": "4K"}},
            ),
            f"{arguments['result_id']}:{arguments['target']}",
        )
        submit_resource.return_value = ToolResult(True, "accepted", "已提交到光鸭")
        model = SameTurnSearchSubmitModel()
        session, _pipeline, _state = _runtime(model)

        events = await _collect(
            session.run(
                AgentInput(
                    message="搜索绿灯军团资源，有的话推送 4K 版到云盘",
                    owner="owner-1",
                    session_id="session-1",
                )
            )
        )

        failures = [
            event for event in events if event.type is AgentEventType.TOOL_FAILED
        ]
        self.assertEqual(failures, [])
        approval = next(
            event
            for event in events
            if event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED
        )
        plan = approval.payload["plan"]
        self.assertTrue(model.resource_candidates_ref.startswith("ref_"))
        self.assertNotIn("arguments", plan)

        confirmed = await _collect(
            session.confirm(
                owner="owner-1",
                session_id="session-1",
                plan_id=plan["plan_id"],
            )
        )
        self.assertTrue(
            any(event.type is AgentEventType.EFFECT_COMPLETED for event in confirmed)
        )
        submit_resource.assert_called_once_with(
            {"result_id": "green-lantern-4k-001", "target": "guangya"},
            "green-lantern-4k-001:guangya",
        )

    @patch("app.agent.indexer_candidate_actions.prepare_submit_resource")
    async def test_followup_turn_reuses_persisted_resource_reference(
        self, prepare_resource
    ) -> None:
        prepare_resource.side_effect = lambda arguments: (
            ToolResult(
                True,
                "confirmation_required",
                "确认后提交 1 项资源",
                data={"resource": {"title": "4K"}},
            ),
            f"{arguments['result_id']}:{arguments['target']}",
        )
        first_session, pipeline, state = _runtime(SearchThenAnswerModel())
        first_events = await _collect(
            first_session.run(
                AgentInput(
                    message="搜索绿灯军团资源",
                    owner="owner-1",
                    session_id="session-1",
                )
            )
        )
        self.assertTrue(
            any(event.type is AgentEventType.TOOL_COMPLETED for event in first_events)
        )

        followup_model = SubmitFromHistoryModel()
        followup = AgentSession(
            model=followup_model,
            catalog=first_session.catalog,
            retriever=CapabilityRetriever(),
            pipeline=pipeline,
            state_store=state,
        )
        followup_events = await _collect(
            followup.run(
                AgentInput(
                    message="推送 4K 版到云盘",
                    owner="owner-1",
                    session_id="session-1",
                )
            )
        )

        self.assertFalse(
            any(event.type is AgentEventType.TOOL_FAILED for event in followup_events)
        )
        next(
            event
            for event in followup_events
            if event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED
        )
        self.assertTrue(followup_model.resource_candidates_ref.startswith("ref_"))
