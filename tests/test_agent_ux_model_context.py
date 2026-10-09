"""真实领域 DTO 经 Kernel 到模型下一轮的兼容性；不调用真实 Provider。"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from unittest.mock import Mock, patch

import pytest

from app.agent.episode_resource_actions import (
    missing_episode_resource_arguments,
    missing_season_resource_arguments,
    search_missing_episode_resources,
    search_missing_season_resources,
)
from app.agent.indexer_actions import search_arguments, search_resources
from app.agent.kernel.capabilities import (
    CapabilityRetriever,
    KernelToolSpec,
    ToolCatalog,
    ToolEffect,
)
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.projection import DefaultProjector
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state import AgentInput, InMemorySessionStateStore
from app.indexers.models import AggregatedIndexerResult, IndexerProviderError
from tests.test_agent_indexer_actions import (
    _SECRET_DETAIL_URL,
    _SECRET_MAGNET,
    _SECRET_TORRENT_URL,
    _resource_item,
)
from tests.test_agent_missing_episode_resources import _audit_result


class FixtureIndexer:
    """仅替换 I/O，搜索 DTO、推荐排序与引用封装仍运行正式实现。"""

    enabled_site_ids = frozenset({"nyaa"})

    def __init__(self, *, empty=False):
        self.empty = empty
        self.items = {}
        self.result_store = Mock(get=lambda result_id: self.items[result_id])

    def site_ids_for_scope(self, scope="manual"):
        return tuple(self.enabled_site_ids)

    async def search_media(self, request, sites=None, *, scope="manual", timeout_seconds=None):
        label = (
            f"S{request.season:02d}E{request.episode:02d}"
            if request.season is not None and request.episode is not None
            else request.title.rsplit(" ", 1)[-1]
        )
        label = label if label.startswith("S02E") else "S02E03"
        items = [] if self.empty else [
            _resource_item(
                result_id=f"fixture-{label}-candidate-{index}",
                title=f"Example.{label}.{resolution}.WEB-DL",
                seeders=40 - index,
            )
            for index, resolution in enumerate(("2160p", "1080p"), 1)
        ]
        self.items.update({item.result_id: item for item in items})
        return AggregatedIndexerResult(
            query=request.title,
            page=request.page,
            items=items,
            sites_attempted=("nyaa", "broken"),
            sites_succeeded=("nyaa",),
            errors=[IndexerProviderError("broken", "unavailable", "站点暂不可用")],
            partial=True,
            cached=True,
            has_more=True,
        )


def real_dto(kind):
    service = FixtureIndexer(empty=kind == "empty")
    with (
        patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
        patch("sqlite3.connect", side_effect=AssertionError("database forbidden")),
        patch("app.agent.indexer_actions.config.get_bool", return_value=True),
        patch("app.agent.indexer_actions.get_indexer_service", return_value=service),
    ):
        if kind in {"search", "empty"}:
            return search_resources(search_arguments({"title": "Example S02E03", "page": 2, "limit": 10}))
        if kind in {"episode", "not_missing"}:
            audit = _audit_result(missing=[{"season": 2, "episode": 3}], target_missing=kind != "not_missing")
            with patch("app.agent.episode_resource_actions.audit_series_episodes", return_value=audit):
                return search_missing_episode_resources(missing_episode_resource_arguments({
                    "query": "示例剧", "season": 2, "episode": 3, "as_of": "2026-08-15",
                }))
        audit = _audit_result(missing=[{"season": 2, "episode": number} for number in (3, 4, 5, 6)])
        with patch("app.agent.episode_resource_actions.audit_series_episodes", return_value=audit):
            return search_missing_season_resources(missing_season_resource_arguments({
                "query": "示例剧", "season": 2, "max_episodes": 2, "as_of": "2026-08-15",
            }))


class NextRoundModel:
    def __init__(self, name):
        self.name = name
        self.requests = []

    async def stream(self, request, *, cancellation):
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("read-resources", self.name, {}))
            yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")
        else:
            yield ModelEvent(ModelEventType.TEXT_DELTA, text="已读取本轮结构化工具结果。")
            yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")


def next_model_round(dto, *, kind="search", maximum=24_000):
    async def exercise():
        name = {
            "episode": "library.search_missing_episode_resources",
            "season": "library.search_missing_season_resources",
            "not_missing": "library.search_missing_episode_resources",
        }.get(kind, "indexer.search_resources")
        # 只隔离工具参数/外部 I/O；模型循环、projector、refs、事件与提交均为正式 Kernel。
        tool = KernelToolSpec(
            name=name, domain="indexer", description="搜索资源并返回已验证的检索上下文",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect=ToolEffect.READ, read=lambda _args, _context: dto,
            metadata={"source_kind": "resource_index"},
        )
        catalog = ToolCatalog([tool])
        states = InMemorySessionStateStore()
        pipeline = ToolPipeline(catalog=catalog, state_store=states, projector=DefaultProjector(max_model_chars=maximum))
        model = NextRoundModel(name)
        session = AgentSession(model=model, catalog=catalog, retriever=CapabilityRetriever(), pipeline=pipeline, state_store=states)
        events = [event async for event in session.run(AgentInput(message="搜索资源", owner="ux-model-owner", session_id="ux-model-session"))]
        assert not any(event.type in {AgentEventType.TURN_FAILED, AgentEventType.TOOL_FAILED} for event in events)
        assert len(model.requests) == 2
        message = next(message for message in reversed(model.requests[1].messages) if message.role == "tool")
        encoded, _, suffix = message.content.partition("\nopaque_refs=")
        public = next(event.payload["result"] for event in events if event.type is AgentEventType.TOOL_COMPLETED)
        return json.loads(encoded), encoded, suffix, message.content, public, model.requests[1]
    return asyncio.run(exercise())


def assert_private_absent(content):
    for private in (_SECRET_DETAIL_URL, _SECRET_MAGNET, _SECRET_TORRENT_URL, "/private/", "10.0.0.9", "SECRET", "rawresult", "raw_result"):
        assert private not in content


@pytest.mark.parametrize("kind", ["search", "episode", "season"])
def test_real_resource_dto_reaches_next_model_request_without_business_field_loss(kind):
    dto = real_dto(kind)
    expected = json.loads(DefaultProjector().project(dto).model_content)
    actual, _, suffix, content, public, request = next_model_round(dto, kind=kind)
    # 对真正下一轮 ModelRequest 做全结构比较，而非只检查领域输出或 refs 存在。
    assert actual == expected
    assert "data" in actual and not actual.get("truncated")
    assert request.round_index == 1
    assert "resource_candidates_ref" in suffix
    assert "candidate_view" not in actual and "selection" not in content
    assert_private_absent(content)
    assert "result_id" not in json.dumps(public)
    if kind == "search":
        assert public["candidate_view"] is None
        assert "candidate_numbers=" in suffix
    else:
        assert public["candidate_view"]["selection_ref"] not in content
    data = actual["data"]
    if kind == "search":
        assert data["page"] == 2 and data["has_more"] is True
        assert data["cached"] is True and data["partial"] is True
        assert data["sites_attempted"] == ["nyaa", "broken"] and data["sites_succeeded"] == ["nyaa"]
        assert data["errors"][0]["code"] == "unavailable"
        assert data["items"][0]["result_id"] == "fixture-S02E03-candidate-1"
        assert data["items"][0]["seeders"] == 39 and data["items"][0]["download_state"] == "ready"
    elif kind == "episode":
        assert data["verification"]["verified_missing"] is True
        assert data["verification"]["tmdb_id"] == "12345"
        search = data["search"]
        assert search["recommendation"]["candidate_count"] == 2
        assert search["recommendation"]["selected"]["confidence"] == "high"
        assert search["recommendation"]["alternatives"][0]["rank"] == 2
        assert search["download_plan"]["requires_confirmation"] is True
        assert search["download_plan"]["auto_submit"] is False
        assert search["download_plan"]["supported_targets"] == ["qb", "guangya", "both"]
    else:
        assert {key: data[key] for key in ("missing_total", "processed", "remaining", "failed", "truncated")} == {
            "missing_total": 4, "processed": 2, "remaining": 2, "failed": 0, "truncated": True,
        }
        assert data["episodes"][0]["search"]["download_plan"]["mode"] == "read_only"
        assert data["episodes"][1]["search"]["has_more"] is True


@pytest.mark.parametrize("kind", ["empty", "not_missing"])
def test_no_reference_branch_preserves_search_and_verification_in_next_request(kind):
    dto = real_dto(kind)
    assert not dto.references
    expected = json.loads(DefaultProjector().project(dto).model_content)
    actual, _, suffix, content, public, _ = next_model_round(dto, kind=kind)
    assert actual == expected
    assert suffix == "" and public["candidate_view"] is None
    assert_private_absent(content)
    if kind == "empty":
        assert actual["data"]["page"] == 2 and actual["data"]["has_more"] is True
        assert actual["data"]["partial"] is True and actual["data"]["errors"]
    else:
        assert actual["status"] == "not_missing"
        assert actual["data"]["verification"]["verified_missing"] is False


@pytest.mark.parametrize("has_refs", [False, True])
def test_model_data_override_is_kept_but_private_branches_and_ui_tickets_are_removed(has_refs):
    dto = real_dto("search")
    if not has_refs:
        dto.references.clear()
    safe = {
        "paging": {"page": 7, "has_more": True},
        "verification": {"verified_missing": True, "episode": 3},
        "recommendation": {"selected": {"result_id": "fixture-S02E03-candidate-1", "confidence": "high"}},
        "download_plan": {"auto_submit": False, "requires_confirmation": True},
        "remaining": 2,
    }
    dto.model_data = {
        **deepcopy(safe),
        "rawresult": {"download_url": "https://10.0.0.9/SECRET"},
        "raw_result": {"cookie": "SECRET"},
        "_private_items": [{"magnet": "magnet:?xt=urn:btih:SECRET"}],
        "details": {
            "downloadUrl": "https://10.0.0.9/SECRET", "torrent_url": "https://10.0.0.9/SECRET",
            "api_key": "SECRET", "credentials": {"password": "SECRET"},
            "note": "局部提示 https://10.0.0.9/SECRET", "location": "/private/SECRET",
            "safe_counter": 3,
        },
        "candidate_view": {"selection": {"ref": "ref_ui_only_ticket_1234567890", "position": 1}},
        "selection": {"ref": "ref_ui_only_ticket_1234567890", "position": 1},
    }
    actual, _, _, content, _, _ = next_model_round(dto)
    assert {key: actual["data"][key] for key in safe} == safe
    assert "items" not in actual["data"], "不能用 public.data 覆盖 model_data"
    assert "selection" not in content and "ref_ui_only_ticket" not in content
    assert actual["data"]["details"]["safe_counter"] == 3
    assert_private_absent(content)
    assert set(actual["data"]["details"]) == {"note", "location", "safe_counter"}


@pytest.mark.parametrize("has_refs", [False, True])
def test_model_next_request_still_respects_projector_length_budget(has_refs):
    dto = real_dto("search")
    if not has_refs:
        dto.references.clear()
    dto.model_data = {"safe_details": ["x" * 1500 for _ in range(40)]}
    maximum = 2000
    expected = json.loads(DefaultProjector(max_model_chars=maximum).project(dto).model_content)
    actual, encoded, suffix, content, public, _ = next_model_round(dto, maximum=maximum)
    assert expected.get("truncated") is True and actual == expected
    assert len(encoded) <= maximum
    # 继承原协议：JSON 预算之外仅追加一个资源引用的有界 suffix，绝不追加大卡片 DTO。
    assert len(content) <= maximum + 512
    assert bool(suffix) is has_refs
    assert public["candidate_view"] is None
    assert ("candidate_numbers=" in suffix) is has_refs
    assert "candidate_view" not in content and "selection" not in content
