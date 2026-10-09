"""新 Kernel 资源搜索到统一下载提交的端到端引用链路。"""

from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import AsyncIterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
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
    validate_safe_resource_snapshot,
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
    return str(_model_json_field(request, "reference_arguments")["resource_candidates_ref"])


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
    def __init__(self, position: int = 1) -> None:
        self.resource_candidates_ref = ""
        self.position = position

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
                    "positions": [self.position],
                    "resource_candidates_ref": self.resource_candidates_ref,
                },
            ),
        )
        yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")


async def _collect(stream) -> list:
    return [event async for event in stream]


def _runtime(model, search_result=None):
    resource_store = RecentResourceCandidateStore()
    ingest_store = AgentIngestSessionStore()
    specs = {spec.name: spec for spec in build_tool_specs(resource_store, ingest_store)}
    search_spec = replace(
        specs["indexer.search_resources"],
        handler=lambda _arguments: search_result if search_result is not None else _search_result(),
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


_MULTI_WORKS = (
    ("光阴之外", "1001"),
    ("择日飞升", "1002"),
    ("大主宰", "1003"),
    ("牧神记", "1004"),
    ("沧元图", "1005"),
    ("一斩苍穹", "1006"),
    ("无匹配作品", "1007"),
)


def _multi_search_items() -> list[dict[str, Any]]:
    return [
        {"query": title, "season": 1, "tmdb_id": tmdb_id}
        for title, tmdb_id in _MULTI_WORKS
    ]


def _multi_work_candidate(
    position: int,
    title: str,
    tmdb_id: str,
) -> dict[str, Any]:
    release_range = (
        "S01E07-08"
        if position == 1
        else "S01E08-09"
        if position == 6
        else "S01E08"
    )
    match = "episode_pack" if position in {1, 6} else "exact_episode"
    result_id = f"multi-work-candidate-{position:02d}"
    return {
        "position": position,
        "season": 1,
        "episode": 8,
        "episode_label": "S01E08",
        "result_id": result_id,
        "title": f"{title}.{release_range}.2160p.WEB-DL",
        "site_id": "fixture-indexer",
        "site_name": "离线索引夹具",
        "rank": position,
        "score": 100 - position,
        "confidence": "high",
        "match": match,
        "download_state": "ready",
        "reasons": ["已核验媒体库缺集"],
        "warnings": [],
        "tags": {"resolution": "2160p"},
        "_verification_context": {
            "title": title,
            "tmdb_id": tmdb_id,
            "season": 1,
            "episode": 8,
            "as_of": "2026-09-13",
        },
    }


def _multi_work_search_result() -> ToolResult:
    candidates = [
        _multi_work_candidate(position, title, tmdb_id)
        for position, (title, tmdb_id) in enumerate(_MULTI_WORKS[:6], start=1)
    ]
    search_id = new_resource_search_id()
    snapshot = {
        "search_id": search_id,
        "search_status": "partial",
        "candidates": candidates,
    }
    # 这是领域层交给 Kernel 的合法脱敏候选快照；私有核验上下文只留在
    # owner/session 绑定引用中，不放入公开模型字段或伪造任何生产凭据。
    assert validate_safe_resource_snapshot(snapshot) == snapshot
    public_items = [
        {
            key: candidate[key]
            for key in (
                "result_id",
                "title",
                "site_id",
                "site_name",
                "download_state",
            )
        }
        | {"download_kinds": ["magnet"]}
        for candidate in candidates
    ]
    queries = [
        {
            "query": title,
            "season": 1,
            "tmdb_id": tmdb_id,
            "verified_missing": True,
            "matched": index <= 6,
            "candidate_count": int(index <= 6),
        }
        for index, (title, tmdb_id) in enumerate(_MULTI_WORKS, start=1)
    ]
    return ToolResult(
        True,
        "partial",
        "已核验 7 部作品：6 部找到已验证候选，1 部无匹配",
        data={
            "items": public_items,
            "queries": queries,
            "matched": 6,
            "unmatched": 1,
        },
        references=[ToolReference("resource_candidates", snapshot)],
    )


def _model_json_field(request: ModelRequest, field: str) -> Any:
    prefix = f"{field}="
    for message in reversed(request.messages):
        content = message.content
        if message.role == "assistant" and content.startswith("历史工具观察"):
            content = json.loads(content.partition("\n")[2])["result"]
        elif message.role != "tool":
            continue
        for line in content.splitlines():
            if line.startswith(prefix):
                return json.loads(line.partition("=")[2])
    raise AssertionError(f"{field} missing from model history")


class MultiWorkSearchThenAnswerModel:
    def __init__(self) -> None:
        self.round = 0
        self.requests: list[ModelRequest] = []
        self.search_arguments: dict[str, Any] = {}

    async def stream(
        self, request: ModelRequest, *, cancellation
    ) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        cancellation.raise_if_cancelled()
        await asyncio.sleep(0)
        if self.round == 0:
            self.round += 1
            self.search_arguments = {"items": _multi_search_items()}
            yield ModelEvent(
                ModelEventType.TOOL_CALL_COMPLETED,
                tool_call=ModelToolCall(
                    "search-multi-1",
                    "library.search_missing_season_resources",
                    self.search_arguments,
                ),
            )
            yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")
            return
        yield ModelEvent(ModelEventType.TEXT_DELTA, text="已完成七部作品的缺集核对。")
        yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")


class DownloadAllFromRecommendationModel:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []
        self.candidate_numbers: list[dict[str, Any]] = []
        self.recommended_ingest_arguments: dict[str, Any] = {}
        self.submitted_arguments: dict[str, Any] = {}

    async def stream(
        self, request: ModelRequest, *, cancellation
    ) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        cancellation.raise_if_cancelled()
        await asyncio.sleep(0)
        self.candidate_numbers = _model_json_field(request, "candidate_numbers")
        self.recommended_ingest_arguments = _model_json_field(
            request, "recommended_ingest_arguments"
        )
        # 不重新计算或硬编码 positions；模拟模型把 Kernel 给出的整批建议
        # 原样复制，只按本轮用户意图把 target 改成显式的光鸭。
        self.submitted_arguments = dict(self.recommended_ingest_arguments)
        self.submitted_arguments["target"] = "guangya"
        yield ModelEvent(
            ModelEventType.TOOL_CALL_COMPLETED,
            tool_call=ModelToolCall(
                "submit-multi-1",
                "ingest.submit",
                self.submitted_arguments,
            ),
        )
        yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")


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

    @patch("app.agent.indexer_candidate_actions.submit_resource_confirmed")
    @patch("app.agent.indexer_candidate_actions.prepare_submit_resource")
    async def test_review_seventh_candidate_survives_followup_and_only_previews(self, prepare_resource, submit_resource):
        from app.agent.resource_recommendation import rank_episode_search
        from app.agent.recent_resource_candidates import attach_resource_candidate_reference
        items = [{"result_id": f"review-resource-{number:04d}", "title": f"理想禁区 {number:02d} 黑历史 1080P",
                  "site_id": "mikan", "site_name": "Mikan", "download_state": "ready", "download_kinds": ["magnet"]}
                 for number in range(1, 12)]
        result = ToolResult(True, "success", "找到11项待核对资源", data={
            "verification": {"title": "理想禁区", "tmdb_id": "74088", "season": 1, "episode": 7, "as_of": "2026-10-01", "verified_missing": True},
            "search": rank_episode_search({"items": items}, season=1, episode=7),
        })
        attach_resource_candidate_reference(result)
        first, pipeline, store = _runtime(SearchThenAnswerModel(), result)
        events = await _collect(first.run(AgentInput(message="查找理想禁区第7集资源", owner="o", session_id="s")))
        completed = next(e.payload["result"] for e in events if e.type is AgentEventType.TOOL_COMPLETED)
        self.assertIsNone(completed["candidate_view"])
        prepare_resource.return_value = (ToolResult(True, "confirmation_required", "预览第7项", data={"resource": {"title": items[6]["title"]}}), "seventh-context")
        followup = AgentSession(model=SubmitFromHistoryModel(7), catalog=first.catalog, retriever=CapabilityRetriever(), pipeline=pipeline, state_store=store)
        output = await _collect(followup.run(AgentInput(message="已核对候选7的集号，请预览到光鸭", owner="o", session_id="s")))
        self.assertFalse(any(e.type is AgentEventType.TOOL_FAILED for e in output))
        plan = next(e.payload["plan"] for e in output if e.type is AgentEventType.EFFECT_APPROVAL_REQUIRED)
        self.assertTrue(plan["plan_id"])
        prepare_resource.assert_called_once_with({"result_id": "review-resource-0007", "target": "guangya"})
        submit_resource.assert_not_called()
        state = await store.load(owner="o", session_id="s")
        model_text = "\n".join(str(m) for m in state.conversation)
        self.assertIn("candidate_numbers=", model_text)
        self.assertNotIn("recommended_ingest_arguments=", model_text)

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


    async def test_multi_work_search_recommendation_batch_preview_and_confirm_is_idempotent(
        self,
    ) -> None:
        search_model = MultiWorkSearchThenAnswerModel()
        resource_store = RecentResourceCandidateStore()
        ingest_store = AgentIngestSessionStore()
        specs = {
            spec.name: spec
            for spec in build_tool_specs(resource_store, ingest_store)
        }
        # 仅模拟外部检索结果；输入校验、候选引用、预检与确认使用真实实现。
        search_spec = replace(
            specs["library.search_missing_season_resources"],
            context_handler=lambda _arguments, _context: _multi_work_search_result(),
        )
        catalog = catalog_from_tool_specs(
            (search_spec, specs["ingest.submit"])
        )
        state = InMemorySessionStateStore()
        pipeline = ToolPipeline(catalog=catalog, state_store=state)
        first_session = AgentSession(
            model=search_model,
            catalog=catalog,
            retriever=CapabilityRetriever(),
            pipeline=pipeline,
            state_store=state,
        )

        first_events = await _collect(
            first_session.run(
                AgentInput(
                    message="先核对七部作品的季度缺集资源",
                    owner="multi-owner",
                    session_id="multi-session",
                )
            )
        )
        self.assertFalse(
            any(event.type is AgentEventType.TOOL_FAILED for event in first_events)
        )
        self.assertEqual(len(search_model.search_arguments["items"]), 7)
        search_event = next(
            event
            for event in first_events
            if event.type is AgentEventType.TOOL_COMPLETED
            and event.payload["tool"]
            == "library.search_missing_season_resources"
        )
        search_public = search_event.payload["result"]
        candidate_view = search_public["candidate_view"]
        state_view = (
            await state.load(owner="multi-owner", session_id="multi-session")
        ).metadata["ux_candidate_view"]
        candidate_ref = candidate_view["ref"]
        self.assertEqual(candidate_ref, state_view["ref"])
        self.assertEqual(candidate_ref, candidate_view["ref"])
        self.assertIn("1 部无匹配", search_public["summary"])
        self.assertEqual(
            [item["position"] for item in candidate_view["items"]],
            list(range(1, 7)),
        )
        self.assertEqual(candidate_view["recommended_positions"], list(range(1, 7)))
        self.assertEqual(
            [item["media_title"] for item in candidate_view["items"]],
            [title for title, _tmdb_id in _MULTI_WORKS[:6]],
        )
        self.assertEqual(candidate_view["items"][0]["coverage"], [1, 7, 8])
        self.assertEqual(candidate_view["items"][5]["coverage"], [1, 8, 9])
        self.assertTrue(
            all(
                item["requested_episode"] == [1, 8]
                for item in candidate_view["items"]
            )
        )

        download_model = DownloadAllFromRecommendationModel()
        followup = AgentSession(
            model=download_model,
            catalog=catalog,
            retriever=CapabilityRetriever(),
            pipeline=pipeline,
            state_store=state,
        )
        capture_calls: list[dict[str, Any]] = []
        candidate_ids = [
            f"multi-work-candidate-{position:02d}" for position in range(1, 7)
        ]

        def fake_capture(arguments: dict[str, Any]) -> dict[str, Any]:
            result_ids = list(arguments["result_ids"])
            capture_calls.append(
                {"result_ids": result_ids, "target": arguments["target"]}
            )
            items = [
                SimpleNamespace(
                    site_id="fixture-indexer",
                    site_name="离线索引夹具",
                    title=f"已核验资源 {result_id}",
                    download_state="ready",
                    download_kinds=("magnet",),
                )
                for result_id in result_ids
            ]
            return {
                "enabled": True,
                "service": object(),
                "items": items,
                "resources": [
                    {
                        "result_id": result_id,
                        "site_id": item.site_id,
                        "site_name": item.site_name,
                        "title": item.title,
                        "download_state": item.download_state,
                        "download_kinds": ["magnet"],
                    }
                    for result_id, item in zip(result_ids, items)
                ],
                "readiness": {"guangya": True},
                "fingerprint": "fixture-batch-context",
            }

        def fake_download(
            _service: object, result_id: str, target: str, **_kwargs: Any
        ) -> dict[str, Any]:
            return {
                "result_id": result_id,
                "request_id": 8_000 + candidate_ids.index(result_id),
                "target": target,
                "status": "submitted",
                "ok": True,
                "handled": True,
                "succeeded": [target],
                "failed": [],
                "duplicate": False,
                "error": "",
            }

        with (
            patch(
                "app.agent.indexer_actions._capture_submit_resource_batch",
                side_effect=fake_capture,
            ) as capture_batch,
            patch(
                "app.indexers.downloads.download_indexer_result",
                side_effect=fake_download,
            ) as download_result,
            patch(
                "app.agent.ingest_actions._receiving_folders",
                return_value=( ["光鸭：测试接收目录"], "fixture-destination"),
            ),
        ):
            followup_events = await _collect(
                followup.run(
                    AgentInput(
                        message="下载全部六部到光鸭",
                        owner="multi-owner",
                        session_id="multi-session",
                    )
                )
            )
            self.assertFalse(
                any(
                    event.type is AgentEventType.TOOL_FAILED
                    for event in followup_events
                )
            )
            self.assertEqual(
                [
                    (
                        item["position"],
                        item.get("media_title"),
                        item.get("requested_episode"),
                    )
                    for item in download_model.candidate_numbers
                ],
                [
                    (
                        item["position"],
                        item.get("media_title"),
                        item.get("requested_episode"),
                    )
                    for item in candidate_view["items"]
                ],
            )
            self.assertEqual(
                download_model.recommended_ingest_arguments,
                {
                    "source_type": "resource_candidates",
                    "resource_candidates_ref": candidate_ref,
                    "positions": list(range(1, 7)),
                    "target": "preferred",
                },
            )
            self.assertEqual(
                download_model.submitted_arguments,
                {
                    "source_type": "resource_candidates",
                    "resource_candidates_ref": candidate_ref,
                    "positions": list(range(1, 7)),
                    "target": "guangya",
                },
            )
            self.assertEqual(
                sum(
                    event.type is AgentEventType.TOOL_STARTED
                    and event.payload.get("tool") == "ingest.submit"
                    for event in followup_events
                ),
                1,
            )
            approval = next(
                event
                for event in followup_events
                if event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED
            )
            plan = approval.payload["plan"]
            preview_data = plan["preview"]["data"]
            self.assertEqual(preview_data["count"], 6)
            self.assertEqual(
                [item["position"] for item in preview_data["resources"]],
                list(range(1, 7)),
            )
            self.assertEqual(
                capture_calls[0],
                {"result_ids": candidate_ids, "target": "guangya"},
            )
            capture_batch.assert_called()
            download_result.assert_not_called()

            confirmed_events = await _collect(
                followup.confirm(
                    owner="multi-owner",
                    session_id="multi-session",
                    plan_id=plan["plan_id"],
                )
            )
            self.assertTrue(
                any(
                    event.type is AgentEventType.EFFECT_COMPLETED
                    for event in confirmed_events
                )
            )
            executed_ids = [
                call.args[1] for call in download_result.call_args_list
            ]
            self.assertEqual(executed_ids, candidate_ids)
            self.assertEqual(
                [call.args[2] for call in download_result.call_args_list],
                ["guangya"] * 6,
            )

            repeated_confirm_events = await _collect(
                followup.confirm(
                    owner="multi-owner",
                    session_id="multi-session",
                    plan_id=plan["plan_id"],
                )
            )
            self.assertFalse(
                any(
                    event.type is AgentEventType.EFFECT_COMPLETED
                    for event in repeated_confirm_events
                )
            )
            self.assertEqual(download_result.call_count, 6)
