"""工作区接力专属回归：客户端仅传 action_key，共用原生 READ 执行链。"""
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from app.agent.errors import ToolPipelineError, ToolRateLimitError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.effects import AuthorizationPolicy, RateLimiter
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.model_rounds import next_round
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession, TurnAdmissionPolicy
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import AgentInput
from app.agent.kernel.turn import AgentTurn
from app.agent.rate_limit import RateLimitDecision
from app.agent.transports import (
    QueryEnvelope,
    TransportInputError,
    WebKernelTransport,
)
from app.modules import workspace as workspace_next_actions
from app.routes import agent_api
from tests import test_agent_ux_backend as backend_support
from tests.test_agent_kernel_api import FakeWeb

from tests.test_agent_kernel_core import ScriptedModel, collect, read_tool
from tests.test_agent_ux_backend import CSRF, SESSION, _login, _web_owner

store = backend_support.store
web_api = backend_support.api

TARGET = "agent.run_code"
ACTION = {"action_key": "review_downloads", "session_id": SESSION, "request_id": "handoff-1"}


@pytest.fixture
def handoff_api(web_api, monkeypatch):
    api = web_api
    api.runtime.web = FakeWeb()
    api.runtime.session = SimpleNamespace(catalog=ToolCatalog([read_tool(TARGET)]))
    # 快照中的工具名/参数不可信；resolver 必须使用自己的固定映射。
    snapshot = Mock(return_value={"ok": True, "status": "attention", "data": {"actions": [{
        "action_key": "review_downloads", "target_tool": "unsafe.write", "arguments": {"delete": True},
    }]}})
    monkeypatch.setattr(workspace_next_actions, "next_actions", snapshot)
    api.snapshot = snapshot
    _login(api)
    return api


@pytest.mark.parametrize("stream", [True, False])
def test_action_resolves_current_snapshot_to_server_read_envelope(handoff_api, stream):
    result = handoff_api.client.post("/api/agent/query", headers=CSRF, json={**ACTION, "stream": stream})
    assert result.status_code == 200, result.text
    handoff_api.snapshot.assert_called_once_with()
    envelope, = handoff_api.runtime.web.queries
    assert envelope.initial_read_tool == TARGET
    assert "downloads.diagnose()" in envelope.initial_read_arguments["code"]
    assert envelope.to_agent_input().initial_read_arguments == envelope.initial_read_arguments
    assert envelope.message == "检查下载队列"
    assert envelope.owner == _web_owner(handoff_api)
    assert envelope.session_id == SESSION and envelope.request_id == "handoff-1"
    assert envelope.to_agent_input().initial_read_tool == TARGET
    assert envelope.selection is None and not envelope.metadata
    if stream:
        assert result.headers["cache-control"] == "no-store"
        assert json.loads(result.text.splitlines()[-1])["type"] == "turn.completed"


@pytest.mark.parametrize("extra", [
    {"message": ""}, {"message": "直接写入"}, {"selection": None}, {"selection": {}},
    {"tool": "unsafe.write"}, {"target_tool": TARGET}, {"arguments": {}},
    {"initial_read_tool": TARGET}, {"initial_read_arguments": {"code": "injected"}}, {"metadata": {}}, {"owner": "other-owner"},
])
def test_client_cannot_mix_prompt_or_inject_tool_arguments(handoff_api, extra):
    result = handoff_api.client.post("/api/agent/query", headers=CSRF, json={**ACTION, **extra})
    assert result.status_code == 400
    assert handoff_api.runtime.web.queries == []
    handoff_api.snapshot.assert_not_called()


def test_action_keeps_request_owner_when_snapshot_wait_rotates_account(handoff_api):
    owner = _web_owner(handoff_api)
    snapshot = handoff_api.snapshot.return_value

    def rotate():
        handoff_api.principal["username"] = "another-account"
        return snapshot

    handoff_api.snapshot.side_effect = rotate
    result = handoff_api.client.post("/api/agent/query", headers=CSRF, json={**ACTION, "stream": False})
    assert result.status_code == 200, result.text
    envelope, = handoff_api.runtime.web.queries
    assert envelope.owner == owner != _web_owner(handoff_api)


@pytest.mark.parametrize("key", [None, 1, [], "unsafe.write", "unknown_action"])
def test_unknown_action_is_rejected_before_snapshot_or_runtime(handoff_api, key):
    result = handoff_api.client.post("/api/agent/query", headers=CSRF, json={**ACTION, "action_key": key})
    assert result.status_code == 400
    handoff_api.snapshot.assert_not_called()
    assert handoff_api.runtime.web.queries == []


@pytest.mark.parametrize("ok", [True, False])
def test_stale_or_unavailable_snapshot_never_enters_kernel(handoff_api, monkeypatch, ok):
    handoff_api.snapshot.return_value = {"ok": ok, "status": "empty", "data": {"actions": []}}
    runtime = Mock(side_effect=AssertionError("陈旧快照不得启动 Kernel"))
    monkeypatch.setattr(agent_api, "get_agent_kernel_runtime", runtime)
    result = handoff_api.client.post("/api/agent/query", headers=CSRF, json=ACTION)
    assert result.status_code == 409, result.text
    runtime.assert_not_called()
    assert handoff_api.runtime.web.queries == []


def test_handoff_keeps_login_csrf_enable_and_owner_rate_admission(handoff_api, monkeypatch):
    handoff_api.client.cookies.clear()
    assert handoff_api.client.post("/api/agent/query", json=ACTION).status_code == 401
    _login(handoff_api)
    assert handoff_api.client.post("/api/agent/query", json=ACTION).status_code == 403
    monkeypatch.setattr(agent_api, "is_agent_enabled", lambda: False)
    assert handoff_api.client.post("/api/agent/query", headers=CSRF, json=ACTION).status_code == 400
    monkeypatch.setattr(agent_api, "is_agent_enabled", lambda: True)
    limiter = Mock(return_value=False)
    monkeypatch.setattr(agent_api.agent_rate_limiter, "allow", limiter)
    assert handoff_api.client.post("/api/agent/query", headers=CSRF, json=ACTION).status_code == 429
    assert limiter.call_args.kwargs == {"limit": 20, "window_seconds": 60, "cost": 1}
    handoff_api.snapshot.assert_not_called()
    assert handoff_api.runtime.web.queries == []


def test_changed_catalog_cannot_turn_fixed_read_into_write(handoff_api):
    handoff_api.runtime.session.catalog = ToolCatalog([KernelToolSpec(
        name=TARGET, domain="downloads", description="不可用于接力的写能力", input_schema={},
        effect=ToolEffect.WRITE, prepare=Mock(), execute_confirmed=Mock(),
    )])
    result = handoff_api.client.post("/api/agent/query", headers=CSRF, json=ACTION)
    assert result.status_code == 409, result.text
    assert handoff_api.runtime.web.queries == []


def test_plain_prompt_stays_ordinary_without_resolving_action(handoff_api):
    result = handoff_api.client.post("/api/agent/query", headers=CSRF, json={
        "message": "我想先修改提示草稿", "session_id": SESSION, "stream": False,
    })
    assert result.status_code == 200
    envelope, = handoff_api.runtime.web.queries
    assert envelope.message == "我想先修改提示草稿" and envelope.initial_read_tool == ""
    handoff_api.snapshot.assert_not_called()


def test_server_handoff_field_is_preserved_and_cannot_mix_selection():
    request = QueryEnvelope(owner="owner", session_id="session", message="检查下载队列", initial_read_tool=TARGET)
    assert replace(request, channel="web").to_agent_input().initial_read_tool == TARGET
    assert request.to_agent_input().metadata == {}
    with pytest.raises(TransportInputError):
        replace(request, selection={"ref": "candidate", "positions": [1], "target": "guangya"}).to_agent_input()
    with pytest.raises(ValueError):
        AgentInput(message="检查", owner="owner", session_id="session", initial_read_tool=cast(str, {"tool": TARGET}))


@pytest.mark.parametrize("stream", [True, False])
def test_http_handoff_runs_real_web_transport_and_kernel(handoff_api, stream):
    run = make_handoff_session([final()])
    handoff_api.runtime.web = WebKernelTransport(run.session)
    handoff_api.runtime.session = run.session
    response = handoff_api.client.post("/api/agent/query", headers=CSRF, json={**ACTION, "stream": stream})
    assert response.status_code == 200, response.text
    assert len(run.observed) == 1 and len(run.model.requests) == 1
    assert run.observed[0][2] == 0
    assert run.observed[0][1].owner == _web_owner(handoff_api)
    assert run.observed[0][1].read_only is True
    assert "downloads.diagnose()" in run.observed[0][0]["code"]
    if stream:
        events = [json.loads(line) for line in response.text.splitlines()]
        assert any(event["type"] == "tool.completed" for event in events)
        assert events[-1]["type"] == "turn.completed"
    else:
        assert response.json()["status"] == "success"


def final():
    return [ModelEvent(ModelEventType.TEXT_DELTA, text="依据当前只读结果回答。"),
            ModelEvent(ModelEventType.FINISH, finish_reason="stop")]


def call(name, arguments=None):
    return [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("model-call", name, arguments or {})),
            ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]


def make_handoff_session(rounds):
    observed = []
    model = ScriptedModel(rounds)
    def read(arguments, context):
        observed.append((dict(arguments), context, len(model.requests)))
        return {"ok": True, "summary": "已读取当前队列", "data": {"count": 2}}
    write = KernelToolSpec(name="unsafe.write", domain="unsafe", description="不应生成写卡", input_schema={},
                           effect=ToolEffect.WRITE, prepare=Mock(), execute_confirmed=Mock())
    catalog = ToolCatalog([replace(read_tool(TARGET, handler=read), input_schema={"type": "object", "properties": {"code": {"type": "string"}}, "additionalProperties": False}),  write])
    state = InMemorySessionStateStore()
    authorization = SimpleNamespace(authorize=AsyncMock())
    limiter = SimpleNamespace(acquire=AsyncMock())
    admission = SimpleNamespace(begin=AsyncMock(return_value="owner-admitted"), is_current=AsyncMock(return_value=True))
    pipeline = ToolPipeline(catalog=catalog, state_store=state, authorization=cast(AuthorizationPolicy | None, authorization), rate_limiter=cast(RateLimiter | None, limiter))
    session = AgentSession(model=model, catalog=catalog,  pipeline=pipeline,
                           state_store=state, turn_admission=cast(TurnAdmissionPolicy | None, admission))
    request = QueryEnvelope(owner="owner-handoff", session_id=SESSION, message="检查下载队列", channel="web",
                            initial_read_tool=TARGET).to_agent_input()
    return SimpleNamespace(session=session, request=request, observed=observed, model=model,
                           authorization=authorization, limiter=limiter, admission=admission, write=write)


class WorkspaceHandoffKernelTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_read_uses_original_pipeline_before_only_synthesis_model_call(self):
        run = make_handoff_session([final()])
        events = await collect(run.session.run(run.request))
        assert events[-1].type is AgentEventType.TURN_COMPLETED
        assert events[-1].payload["status"] == "success"
        assert len(run.model.requests) == 1 and len(run.observed) == 1
        arguments, context, prior_model_calls = run.observed[0]
        assert arguments == {} and prior_model_calls == 0
        assert context.owner == run.request.owner and context.session_id == SESSION
        assert context.read_only is True
        run.admission.begin.assert_awaited_once_with(run.request)
        run.authorization.authorize.assert_awaited_once()
        run.limiter.acquire.assert_awaited_once_with(owner=run.request.owner, tool_name=TARGET, cost=1.0, arguments={})
        types = [event.type for event in events]
        assert types.index(AgentEventType.TOOL_STARTED) < types.index(AgentEventType.MODEL_STARTED)
        assert any(message.role == "tool" for message in run.model.requests[0].messages)
        state = await run.session.state_store.load(owner=run.request.owner, session_id=SESSION)
        assert "read_wait" not in state.metadata
        assert not state.pending_effect_plan_id


    async def test_original_admission_denials_do_not_run_read_handler(self):
        for blocked in ("authorization", "quota", "turn_admission"):
            with self.subTest(blocked=blocked):
                await self.check_denial(blocked)

    async def check_denial(self, blocked):
        run = make_handoff_session([final()])
        error = ToolRateLimitError(RateLimitDecision(False, 1, 1, 0, 30, None, "quota")) if blocked == "quota" else (
            ToolPipelineError("本次未执行", code="forbidden"))
        target = run.authorization.authorize if blocked == "authorization" else (
            run.limiter.acquire if blocked == "quota" else run.admission.begin)
        target.side_effect = error
        events = await collect(run.session.run(run.request))
        assert not run.observed
        if blocked == "turn_admission":
            assert events[-1].type is AgentEventType.TURN_FAILED
            assert events[-1].payload["code"] == error.code
            assert run.model.requests == []
        else:
            assert events[-1].type is AgentEventType.TURN_COMPLETED
            assert events[-1].payload["status"] == "partial"
            # 普通工具拒绝交还模型，不隐藏错误，也不把未执行记为查询成功。
            errors = [json.loads(message.content) for message in run.model.requests[0].messages if message.role == "tool"]
            assert errors[-1]["code"] == error.code
        assert not any(event.type is AgentEventType.TOOL_COMPLETED for event in events)
        if blocked != "turn_admission":
            assert any(event.type is AgentEventType.TOOL_FAILED and event.payload["code"] == error.code for event in events)
        if blocked == "authorization":
            run.limiter.acquire.assert_not_awaited()
        assert not any(event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED for event in events)
        run.write.prepare.assert_not_called()


    async def test_read_handoff_cannot_expand_model_write_surface_or_prepare_card(self):
        run = make_handoff_session([
            call("unsafe__write"), final(),
        ])
        events = await collect(run.session.run(run.request))
        run.write.prepare.assert_not_called()
        run.write.execute_confirmed.assert_not_called()
        assert len(run.observed) == 1
        assert not any(event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED for event in events)
        for request in run.model.requests:
            assert "unsafe__write" not in {tool["name"] for tool in request.tools}
        assert any(event.type is AgentEventType.TOOL_FAILED
                   and event.payload["tool"] in {"unsafe.write", "unsafe__write"}
                   and event.payload["code"] in {"tool_not_available", "read_scope_required"} for event in events)
        assert events[-1].type is AgentEventType.TURN_COMPLETED
        assert events[-1].payload["status"] == "partial"
        answer = events[-1].payload["answer"]
        assert answer.startswith("部分完成：")
        assert "依据当前只读结果回答。" in answer
        assert "本轮未执行新的写操作" in answer
        observations = [json.loads(message.content) for message in run.model.requests[-1].messages if message.role == "tool"]
        assert any(row.get("summary") == "已读取当前队列" for row in observations)


    async def test_initial_read_round_does_not_claim_or_settle_quota_resume(self):
        initial = ModelToolCall("initial", TARGET, {})
        pending_resume = {"arguments": {"page": 2}}
        turn = SimpleNamespace(pending_initial_read=initial, pending_resume_call=pending_resume,
                               tool_definitions=(), session=SimpleNamespace(system_prompt="system"))
        result = await next_round(cast(AgentTurn, turn), None)
        assert result is not None
        assert result.calls == (initial,) and result.resuming_read is False
        assert turn.pending_initial_read is None and turn.pending_resume_call is pending_resume
