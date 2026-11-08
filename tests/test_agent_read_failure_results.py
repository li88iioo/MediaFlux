"""READ 失败作为完整观察交给模型；部分状态不改写正常答案。"""
from __future__ import annotations

import asyncio
import json
import socket
from collections import defaultdict, deque
from functools import partial
from unittest.mock import patch

import httpx
import pytest

from app.modules import web_evidence as actions
from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput
from app.agent.models import ToolResult
from app.clients.http import FixedHostHttpClient
from tests.test_agent_kernel_core import ScriptedModel, collect
from tests.test_agent_tool_argument_ingress import offline as offline
from tests.test_agent_tool_argument_ingress import store as store

CLAIMED_SUCCESS = "已根据本轮返回的观察作答；失败或缺失的来源仍待核验。"
FINAL = [ModelEvent(ModelEventType.TEXT_DELTA, text=CLAIMED_SUCCESS),
         ModelEvent(ModelEventType.FINISH, finish_reason="stop")]


def round_calls(calls):
    return [*(ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=call) for call in calls),
            ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]


def session_for(model, tool, store):
    catalog = ToolCatalog([tool])
    return AgentSession(model=model, catalog=catalog, state_store=store, journal=store,
        pipeline=ToolPipeline(catalog=catalog, state_store=store))


async def run_steps(store, steps, *, together=False, parallel=False, answer=CLAIMED_SUCCESS):
    outcomes = defaultdict(deque)
    for query, value in steps:
        outcomes[query].append(value)
    observed = []

    def read(arguments, _context):
        query = arguments["query"]
        observed.append(query)
        value = outcomes[query].popleft()
        if isinstance(value, ToolPipelineError):
            raise value
        return value

    tool = KernelToolSpec(name="fixture.lookup", domain="fixture", description="核验指定对象", effect=ToolEffect.READ,
        input_schema={"type": "object", "required": ["query"], "additionalProperties": False,
                      "properties": {"query": {"type": "string"}}},
        validator=dict, read=read, metadata={"parallel_read": parallel})
    calls = [ModelToolCall(f"call-{index}", tool.model_name, {"query": query})
             for index, (query, _) in enumerate(steps)]
    rounds = [round_calls(calls)] if together else [round_calls([call]) for call in calls]
    model = ScriptedModel([*rounds, [
        *(ModelEvent(ModelEventType.TEXT_DELTA, text=part) for part in (answer[:20], answer[20:80], answer[80:])),
        ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
    ]])
    events = await collect(session_for(model, tool, store).run(AgentInput(
        owner="owner", session_id="read-result", message="核验指定对象，不以其他对象结果代替")))
    saved = await store.load(owner="owner", session_id="read-result")
    rows = [row for row in saved.conversation if row.get("role") == "tool"]
    assert sorted(observed) == sorted(query for query, _ in steps)
    assert [row["tool_call_id"] for row in rows] == [call.call_id for call in calls]
    # 失败仍为原始领域 DTO，没有在 adapter 或 pipeline 中改造成丢失资料的异常。
    for row, (_, value) in zip(rows, steps):
        if isinstance(value, ToolResult):
            assert json.loads(row["content"]) == value.to_dict()
    assert "_recovery_tracked" not in json.dumps(saved.conversation)
    return events, saved, model


@pytest.mark.parametrize("status", ["success", "partial"])
def test_resource_observation_keeps_model_answer_and_links_once(store, status):
    """复现 live baseline 第二轮：部分站点失败，模型已说明候选与缺口。"""
    summary = "已核对 3 部/季，2 部找到匹配目标集的资源"
    answer = ("时光代理人 S01E11、S04E01 至 S04E12 共 13 集已找到匹配资源；"
              "师兄啊师兄 S01E166 本轮未找到，不代表全网无资源。\n"
              "公开来源：https://www.themoviedb.org/tv/123542\n"
              "Jellyfin 媒体：https://jellyfin.example.org/web/#/details?id=media-123&serverId=home\n"
              "用户指定文件：`/media/剧集/时光代理人/S01E11.mkv`。")
    outcome = ToolResult(True, status, summary, data={
        "items": [{"title": "时光代理人", "matched_count": 13},
                  {"title": "师兄啊师兄", "missing": [[1, 166]], "matched_count": 0}],
        "errors": [{"source": "Nyaa", "status": "timeout"}] if status == "partial" else [],
    })

    async def scenario():
        events, saved, model = await run_steps(store, [("14 缺集", outcome)], answer=answer)
        assert len(model.requests) == 2
        observed = next(row for row in model.requests[-1].messages if row.role == "tool")
        assert json.loads(observed.content) == outcome.to_dict()
        final = events[-1].payload
        assert final["status"] == status
        assert final["finish_reason"] == "stop"
        assert final["answer"] == saved.conversation[-1]["content"] == answer
        deltas = [event.payload["delta"] for event in events if event.type.value == "model.delta"]
        assert deltas == [part for part in (answer[:20], answer[20:80], answer[80:]) if part]
        assert "".join(deltas) == answer
        assert final["model_calls"] == 2 and final["tool_calls"] == 1
        assert not any(event.type.value == "turn.failed" for event in events)
    asyncio.run(scenario())


@pytest.mark.parametrize("status,code", [(503, "unavailable"), (429, "rate_limited")])
def test_real_web_search_http_failure_preserves_observation_and_model_answer(status, code, store):
    async def scenario():
        requests = []

        def transport(request):
            requests.append((request.headers["host"], json.loads(request.content)["query"]))
            return httpx.Response(status, json={"error": "fixture outage"})

        def factory(**kwargs):
            return FixedHostHttpClient(**kwargs, transport=httpx.MockTransport(transport),
                resolver=lambda host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))])

        settings = {"WEB_SEARCH_ENABLED": "1", "TAVILY_API_KEY": "offline-key", "TAVILY_DAILY_CREDIT_LIMIT": "100"}
        original = actions._search_tavily
        from app import sdk
        from app.agent.kernel.capabilities import KernelToolSpec, ToolEffect
        tool = KernelToolSpec(name="fixture.web", domain="fixture", description="调用公共网页 SDK",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            effect=ToolEffect.READ, read=lambda args, context: sdk.web.search(**args))
        query = f"offline outage {status}"
        model = ScriptedModel([round_calls([ModelToolCall("lookup", tool.model_name, {"query": query})]), FINAL])
        with patch.object(actions, "get", side_effect=lambda key, default="": settings.get(key, default)), patch.object(
            actions, "_search_tavily", partial(original, client_factory=factory),
        ):
            events = await collect(session_for(model, tool, store).run(AgentInput(
                owner="owner", session_id="web-outage", message="请搜索并核对公开来源")))
        saved = await store.load(owner="owner", session_id="web-outage")
        result = json.loads(next(row["content"] for row in saved.conversation if row["role"] == "tool"))
        assert requests == [("api.tavily.com", query)]
        assert result["ok"] is False and result["status"] == code
        assert result["message"] == ("网页搜索服务暂时不可用" if status == 503 else "网页搜索服务请求过于频繁，请稍后重试")
        assert events[-1].type.value == "turn.completed" and events[-1].payload["status"] == "partial"
        assert events[-1].payload["answer"] == saved.conversation[-1]["content"] == CLAIMED_SUCCESS
        assert events[-1].payload["finish_reason"] == "stop"
        assert json.loads(next(row.content for row in model.requests[-1].messages if row.role == "tool")) == result
        assert len(model.requests) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["unavailable", "timeout", "authentication", "invalid_response", "http_error",
    "provider_timeout", "upstream_timeout", "provider_auth_failed", "indexer_error", "configuration_missing"])
def test_failed_read_service_codes_do_not_override_model_answer(status, store):
    async def scenario():
        value = ToolResult(False, status, "查询服务尚未完成", data={"retryable": True}, error="查询服务失败")
        events, _, _ = await run_steps(store, [("object-x", value)])
        assert events[-1].type.value == "turn.completed"
        assert events[-1].payload["status"] == "partial"
        assert events[-1].payload["answer"] == CLAIMED_SUCCESS
        assert events[-1].payload["finish_reason"] == "stop"
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["not_found", "disabled", "loading", "empty", "blocked"])
def test_negative_business_observation_is_not_a_service_exception(status, store):
    async def scenario():
        events, _, _ = await run_steps(store, [("object-x", ToolResult(False, status, "已取得业务观察"))])
        assert events[-1].type.value == "turn.completed"
        assert events[-1].payload["status"] == "partial"
        assert events[-1].payload["finish_reason"] == "stop"
        assert events[-1].payload["answer"] == CLAIMED_SUCCESS
    asyncio.run(scenario())


@pytest.mark.parametrize("first_kind", ["dto", "exception", "partial-failed", "partial-mixed"])
@pytest.mark.parametrize("later_status", ["not_found", "loading"])
def test_another_failed_result_for_same_arguments_cannot_erase_service_failure(first_kind, later_status, store):
    async def scenario():
        first = (ToolResult(False, "provider_timeout", "对象x查询超时") if first_kind == "dto"
                 else ToolPipelineError("对象x查询超时", code="provider_timeout"))
        if first_kind.startswith("partial-"):
            first = ToolResult(first_kind == "partial-mixed", "partial", "对象x来源未全部核验")
        events, _, model = await run_steps(store, [
            ("object-x", first), ("object-x", ToolResult(False, later_status, "尚未取得该对象的完整查询结果")),
        ])
        assert events[-1].payload["status"] == "partial"
        assert events[-1].payload["finish_reason"] == "stop"
        assert events[-1].payload["answer"] == CLAIMED_SUCCESS
        observations = [json.loads(row.content) for row in model.requests[-1].messages if row.role == "tool"]
        assert len(observations) == 2
        assert "对象x" in str(observations[0])
        assert observations[1]["status"] == later_status
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["ok", "empty"])
@pytest.mark.parametrize("first_ok,first_status", [(False, "unavailable"), (False, "partial"), (True, "partial")])
def test_success_for_same_arguments_recovers_failed_read_including_successful_empty_result(status, first_ok, first_status, store):
    async def scenario():
        events, _, _ = await run_steps(store, [
            ("object-x", ToolResult(first_ok, first_status, "对象x查询未全部完成")),
            ("object-x", ToolResult(True, status, "已核验对象x", data={"results": []})),
        ])
        assert events[-1].payload["status"] == "success"
        assert events[-1].payload["answer"] == CLAIMED_SUCCESS
    asyncio.run(scenario())


@pytest.mark.parametrize("first_ok,first_status", [(False, "unavailable"), (False, "partial"), (True, "partial")])
def test_success_for_other_arguments_does_not_recover_failed_object(first_ok, first_status, store):
    async def scenario():
        events, saved, model = await run_steps(store, [
            ("object-x", ToolResult(first_ok, first_status, "对象x服务暂不可用")),
            ("object-y", ToolResult(True, "ok", "已核验对象y", data={"query": "object-y"})),
        ])
        terminal = events[-1].payload
        assert terminal["status"] == "partial" and terminal["finish_reason"] == "stop"
        assert terminal["answer"] == CLAIMED_SUCCESS
        for history in (saved.conversation, [row.to_dict() for row in model.requests[-1].messages]):
            observations = [json.loads(row["content"]) for row in history if row["role"] == "tool"]
            assert observations[0]["summary"] == "对象x服务暂不可用"
            assert observations[1]["data"]["query"] == "object-y"
    asyncio.run(scenario())


@pytest.mark.parametrize("parallel", [False, True], ids=["serial", "parallel"])
def test_mixed_read_batch_retains_partial_results_and_discloses_service_failure(parallel, store):
    async def scenario():
        events, _, model = await run_steps(store, [
            ("object-x", ToolResult(True, "ok", "已核验对象x", data={"query": "object-x"})),
            ("object-y", ToolResult(False, "unavailable", "对象y查询服务不可用")),
        ], together=True, parallel=parallel)
        terminal = events[-1].payload
        assert terminal["status"] == "partial" and terminal["finish_reason"] == "stop"
        assert terminal["answer"] == CLAIMED_SUCCESS
        observations = [json.loads(row.content) for row in model.requests[-1].messages if row.role == "tool"]
        assert [row["summary"] for row in observations] == ["已核验对象x", "对象y查询服务不可用"]
        assert model.requests[0].allow_parallel_tool_calls is parallel
    asyncio.run(scenario())
