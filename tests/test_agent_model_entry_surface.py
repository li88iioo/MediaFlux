"""固定模型入口与内部完整执行目录的隔离；不模拟旧发现协议。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import Mock

import pytest

from app import config
from app.agent.kernel import sandbox
from app.agent.kernel.bootstrap import build_agent_kernel_runtime
from app.agent.transports import QueryEnvelope
from tests.support import initialized_web_runtime

from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import AgentInput
from app.agent.kernel.tool_calls import PreparedEffect
from app.clients.openai_compatible import native_tool_definitions
from tests.test_agent_kernel_core import ScriptedModel, collect, read_tool


def finish(text='已完成只读检查'):
    return [ModelEvent(ModelEventType.TEXT_DELTA, text=text), ModelEvent(ModelEventType.FINISH, finish_reason='stop')]


def invoke(name, arguments=None):
    return [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall('call-1',name,arguments or {})),
            ModelEvent(ModelEventType.FINISH,finish_reason='tool_calls')]


@pytest.mark.parametrize('effect', list(ToolEffect))
def test_explicit_tools_preserve_full_schema_and_confirmation_boundary(effect):
    description='完整接口约束；'*160+'末尾/skills/example/SKILL.md'
    tool=read_tool('inspect.details',description=description)
    if effect is not ToolEffect.READ:
        tool=replace(tool,effect=effect,read=None,prepare=lambda *_:None,execute_confirmed=lambda *_:None)
    definition=tool.model_definition()
    assert definition['description'].startswith(description)
    assert definition['parameters']==tool.input_schema
    if effect is not ToolEffect.READ:
        assert '实际写入必须等用户确认' in definition['description']
    for protocol in ('responses','anthropic_messages','chat_completions'):
        exported=native_tool_definitions(protocol,[definition])[0]
        assert exported.get('function',exported)['description']==definition['description']


def test_small_explicit_catalog_has_all_authorized_tools_without_discovery():
    async def run():
        tools=[read_tool('sample.'+name) for name in ('first','second','third')]
        catalog=ToolCatalog(tools)
        states=InMemorySessionStateStore()
        model=ScriptedModel([finish()])
        session=AgentSession(model=model,catalog=catalog,state_store=states,
                             pipeline=ToolPipeline(catalog=catalog,state_store=states),model_tools=[tool.name for tool in tools])
        await collect(session.run(AgentInput(message='核对样本',owner='owner',session_id='explicit')))
        assert {tool['name'] for tool in model.requests[0].tools}=={tool.model_name for tool in tools}
        assert 'agent.capabilities' not in model.requests[0].system_prompt
    asyncio.run(run())


@pytest.mark.parametrize('name',['private.change','private__change'])
def test_internal_write_cannot_be_called_by_guessing_its_model_name(name):
    async def run():
        prepare=Mock(return_value=PreparedEffect(preview={'summary':'预检'},snapshot_fingerprint='frozen'))
        write=KernelToolSpec(name='private.change',domain='private',description='内部SDK写入',effect=ToolEffect.WRITE,
            input_schema={'type':'object','properties':{}},prepare=prepare,execute_confirmed=Mock())
        workspace=read_tool('agent.run_code')
        catalog=ToolCatalog([workspace,write])
        states=InMemorySessionStateStore()
        pipeline=ToolPipeline(catalog=catalog,state_store=states)
        model=ScriptedModel([invoke(name),finish('未执行')])
        session=AgentSession(model=model,catalog=catalog,pipeline=pipeline,state_store=states,model_tools=[workspace.name])
        events=await collect(session.run(AgentInput(message='检查但不执行',owner='owner',session_id='hidden')))
        assert all({tool['name'] for tool in request.tools}=={workspace.model_name} for request in model.requests)
        assert any(event.payload.get('code')=='tool_not_available' for event in events)
        prepare.assert_not_called()
        assert catalog.get(write.name) is write
        assert not (await states.load(owner='owner',session_id='hidden')).pending_effect_plan_id
    asyncio.run(run())


def test_server_initial_read_executes_without_adding_its_schema_to_model():
    async def run():
        read=Mock(return_value={'ok':True,'status':'success','summary':'可信接力读取'})
        target=read_tool('private.inspect',handler=read)
        workspace=read_tool('agent.run_code')
        catalog=ToolCatalog([workspace,target])
        states=InMemorySessionStateStore()
        model=ScriptedModel([finish()])
        session=AgentSession(model=model,catalog=catalog,state_store=states,
            pipeline=ToolPipeline(catalog=catalog,state_store=states),model_tools=[workspace.name])
        await collect(session.run(AgentInput(message='执行宿主固定只读接力',owner='owner',session_id='handoff',initial_read_tool=target.name)))
        read.assert_called_once()
        assert {tool['name'] for tool in model.requests[0].tools}=={workspace.model_name}
        assert not any(message.tool_calls or message.role == 'tool' for message in model.requests[0].messages)
        assert any('可信接力读取' in message.content for message in model.requests[0].messages)
        saved = await states.load(owner='owner', session_id='handoff')
        assert any(row.get('tool_name') == target.model_name for row in saved.conversation)
    asyncio.run(run())


def test_explicit_tool_set_still_checks_availability_and_authorization():
    allowed=read_tool('sample.allowed')
    denied=replace(read_tool('sample.denied'),authorize=lambda _:False)
    missing=replace(read_tool('sample.missing'),context_requirements=frozenset({'observation'}))
    catalog=ToolCatalog([allowed,denied,missing])
    states=InMemorySessionStateStore()
    session=AgentSession(model=ScriptedModel([]),catalog=catalog,state_store=states,
        pipeline=ToolPipeline(catalog=catalog,state_store=states),model_tools=[tool.name for tool in (allowed,denied,missing)])
    assert [tool.name for tool in session.visible_model_tools({})]==[allowed.name]
    assert [tool.name for tool in session.visible_model_tools({'reference_kinds':['observation']})]==[allowed.name,missing.name]


def test_production_bootstrap_explicitly_exposes_only_workspace(monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "1")
    from unittest.mock import patch
    from app.agent.kernel.bootstrap import build_agent_kernel_runtime
    async def run():
        from app.agent.kernel.persistence import SQLiteKernelStore
        from app.agent.owner_routes import web_kernel_owner
        states=SQLiteKernelStore(secret_provider=lambda:'model-surface-test')
        owner=web_kernel_owner('model-surface')
        model=ScriptedModel([invoke('subscriptions__create'),finish('未执行')])
        with patch('app.agent.kernel.bootstrap.workspace_unavailable_reason',return_value=''):
            runtime=build_agent_kernel_runtime(model=model,store=states)
        events=await collect(runtime.session.run(AgentInput(message='查看订阅',owner=owner,session_id='production')))
        assert runtime.session.catalog.has('subscriptions.create')
        assert runtime.session.catalog.selection is not None
        assert runtime.session.model_tools==frozenset({'agent.run_code'})
        assert all({tool['name'] for tool in request.tools}=={'agent__run_code'} for request in model.requests)
        assert any(event.payload.get('code')=='tool_not_available' for event in events)
        assert not (await states.load(owner=owner,session_id='production')).pending_effect_plan_id
    from tests.support import isolated_test_database
    with isolated_test_database():
        asyncio.run(run())


@pytest.fixture
def backend():
    with initialized_web_runtime():
        config.set_and_save({
            "TG_AGENT_ENABLED": "1", "TG_CHAT_ID": "-100", "TG_AGENT_ALLOWED_USER_IDS": "7",
        })
        yield


async def query(runtime, channel):
    request = QueryEnvelope(
        owner="webk:v1:" + "a" * 64 if channel == "web" else "tg:v1:-100\x1f7",
        session_id="workspace-availability", request_id="availability-request", message="检查媒体库状态",
    )
    return await runtime.web.query_view(request) if channel == "web" else await runtime.telegram.query(request)


@pytest.mark.parametrize("channel", ["web", "telegram"])
def test_unavailable_workspace_returns_actual_reason_without_model_calls(backend, monkeypatch, tmp_path, channel):
    # 系统依赖缺失必须在付费调用之前直接说明，不能退回无隔离执行。
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    reason = sandbox.workspace_unavailable_reason()
    assert reason
    model = ScriptedModel([[ModelEvent(ModelEventType.TEXT_DELTA, text="不应调用模型"),
                            ModelEvent(ModelEventType.FINISH, finish_reason="stop")]])
    runtime = build_agent_kernel_runtime(model=model)
    view = asyncio.run(query(runtime, channel))
    assert view.status == "failed"
    assert view.error_code == "workspace_unavailable"
    assert view.error_message == reason
    assert view.request_id == "availability-request" and view.event_count == 1
    assert model.requests == []
    assert runtime.session.metrics.snapshot()["recent"][0]["model_calls"] == 0
    state = asyncio.run(runtime.store.load(owner="webk:v1:" + "a" * 64 if channel == "web" else "tg:v1:-100\x1f7",
                                         session_id="workspace-availability"))
    assert state.conversation == []


def test_workspace_failure_preserves_telegram_authorization(backend, monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    config.set_and_save({"TG_AGENT_ALLOWED_USER_IDS": "8"})
    model = ScriptedModel([])
    view = asyncio.run(query(build_agent_kernel_runtime(model=model), "telegram"))
    assert view.status == "failed" and view.error_code == "authorization_denied"
    assert view.error_message != sandbox.workspace_unavailable_reason()
    assert model.requests == []


@pytest.mark.parametrize("channel", ["web", "telegram"])
def test_available_workspace_keeps_existing_model_entry(backend, channel):
    if not sandbox.sandbox_supported():
        pytest.skip(sandbox.workspace_unavailable_reason())
    model = ScriptedModel([[ModelEvent(ModelEventType.TEXT_DELTA, text="入口可用"),
                            ModelEvent(ModelEventType.FINISH, finish_reason="stop")]])
    runtime = build_agent_kernel_runtime(model=model)
    view = asyncio.run(query(runtime, channel))
    assert view.status == "success" and view.answer == "入口可用"
    assert len(model.requests) == 1
    assert {tool["name"] for tool in model.requests[0].tools} == {"agent__run_code"}
