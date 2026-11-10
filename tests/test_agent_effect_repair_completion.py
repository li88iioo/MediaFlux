"""原工具重试与真实写入回执，冻结预览不冒充执行完成。"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from app.repositories import discovery as discovery_repo
from app.agent.errors import ConfirmationClaimError, ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import AgentInput, CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.agent.models import ToolResult
from tests.test_agent_kernel_core import ScriptedModel, collect
from tests.test_agent_tool_argument_ingress import offline as offline
from tests.test_agent_scoped_trust import (
    FINAL,
    OWNER,
    SCOPE,
    SESSION,
    TOOL,
    arguments,
    call,
    rig as scoped_rig,
    task_store as isolated_store,
)

task_store = isolated_store
rig = scoped_rig


async def trusted_session(rig, rounds):
    session = rig.session([])
    session.model = ScriptedModel([call(arguments()), *rounds])
    preview = await collect(session.run(AgentInput(owner=OWNER, session_id=SESSION, message="把这部剧加入探索收藏")))
    await session.set_scoped_trust(owner=OWNER, session_id=SESSION, operation="grant",
        plan_id=preview[-1].payload["plan_id"], tool_name=TOOL, scope=SCOPE, max_actions=5, ttl_seconds=300)
    return session


@pytest.mark.parametrize("mode", ["unknown-field", "invalid-value", "repeated-invalid", "receipt-readback"])
def test_real_watchlist_direct_retry_writes_once_and_preserves_receipt(rig, mode):
    async def run():
        if mode == "unknown-field":
            rounds = [call({**arguments(501), "unsupported": True}), call(arguments(501)), FINAL]
        elif mode == "invalid-value":
            rounds = [call({**arguments(501), "provider": "invalid"}), call(arguments(501)), FINAL]
        elif mode == "repeated-invalid":
            rounds = [call({"external_id": "501"}),
                      call({**arguments(501), "media_type": 4}), call(arguments(501)), FINAL]
        else:
            rounds = [call(arguments(501)), call({**arguments(501), "unsupported": True}), call(arguments(501)), FINAL]
        session = await trusted_session(rig, rounds)
        events = await collect(session.run(AgentInput(owner=OWNER, session_id=SESSION, message="把另一部电视剧也加入探索收藏")))
        assert discovery_repo.get_media_watchlist("tmdb", "501", "tv") is not None
        assert rig.attempts == [arguments(501)] and await rig.remaining() == 4
        assert events[-1].type is AgentEventType.TURN_COMPLETED
        assert events[-1].payload["status"] == "success", events[-1].payload
        assert "未完成的查询" not in events[-1].payload["answer"]
        assert not any(event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED for event in events)
    asyncio.run(run())


def test_other_targets_business_failure_survives_successful_automatic_write(rig):
    async def run():
        original_prepare = rig.tool.prepare

        def prepare(args, context):
            if args['external_id'] == '501':
                raise ToolPipelineError('对象501查询服务超时', code='provider_timeout')
            return original_prepare(args, context)

        rig.pipeline = ToolPipeline(catalog=ToolCatalog([replace(rig.tool, prepare=prepare)]),
            state_store=rig.store, effect_store=rig.pipeline.effect_store)
        session = await trusted_session(rig, [call({**arguments(501), 'unsupported': True}),
            call(arguments(501)), call(arguments(502)), FINAL])
        events = await collect(session.run(AgentInput(owner=OWNER, session_id=SESSION, message='收藏这些电视剧')))
        assert discovery_repo.get_media_watchlist('tmdb', '501', 'tv') is None
        assert discovery_repo.get_media_watchlist('tmdb', '502', 'tv') is not None
        assert rig.attempts == [arguments(502)] and await rig.remaining() == 4
        assert events[-1].payload['status'] == 'partial'
        assert events[-1].payload['finish_reason'] == 'provider_timeout'
        assert '对象501查询服务超时' in events[-1].payload['answer']
    asyncio.run(run())


@pytest.mark.parametrize("status", ["failed", "partial", "accepted"])
def test_nonterminal_or_failed_execution_remains_disclosed_after_retry(rig, status):
    async def run():
        def execute(args, *_):
            rig.attempts.append(args)
            return ToolResult(ok=status != "failed", status=status, summary="执行未完整完成")
        rig.pipeline = ToolPipeline(catalog=ToolCatalog([replace(rig.tool, execute_confirmed=execute)]),
            state_store=rig.store, effect_store=rig.pipeline.effect_store)
        session = await trusted_session(rig, [call({**arguments(501), "unsupported": True}), call(arguments(501)), FINAL])
        events = await collect(session.run(AgentInput(owner=OWNER, session_id=SESSION, message="收藏这部电视剧")))
        assert rig.attempts == [arguments(501)] and await rig.remaining() == 4
        # accepted 是请求受理成功，不等于后台完成；回执必须明确这一点。
        expected = "success" if status == "accepted" else "partial"
        assert events[-1].payload["status"] == expected, events[-1].payload
        assert events[-1].payload["finish_reason"] != "invalid_arguments", events[-1].payload
        assert "执行未完整完成" in events[-1].payload["answer"]
        assert "unsupported" not in events[-1].payload["answer"]
        if status == "accepted":
            assert "后台任务尚未完成" in events[-1].payload["answer"]
    asyncio.run(run())


def test_execution_started_argument_error_survives_readback_and_another_targets_write(rig):
    async def run():
        original_execute = rig.tool.execute_confirmed

        def execute(args, snapshot, context):
            result = original_execute(args, snapshot, context)
            if args['external_id'] == '501':
                raise ToolPipelineError('对象501实际写入后的领域参数错误', code='invalid_arguments')
            return result

        def readback(args, _):
            assert discovery_repo.get_media_watchlist('tmdb', args['external_id'], 'tv') is not None
            return {'ok': True, 'status': 'found', 'summary': '只读核验已收藏，原失败回执仍须保留'}

        reader = KernelToolSpec(name='library.lookup', domain='library', description='核验真实收藏状态',
            input_schema={'type': 'object', 'required': ['external_id'], 'properties': {
                'external_id': {'type': 'string'}}}, effect=ToolEffect.READ, read=readback)
        rig.pipeline = ToolPipeline(catalog=ToolCatalog([replace(rig.tool, execute_confirmed=execute), reader]),
            state_store=rig.store, effect_store=rig.pipeline.effect_store)
        query = [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
            tool_call=ModelToolCall('readback-501', reader.name, {'external_id': '501'})),
            ModelEvent(ModelEventType.FINISH, finish_reason='tool_calls')]
        session = await trusted_session(rig, [call({**arguments(501), 'unsupported': True}),
            call(arguments(501)), query, call(arguments(502)), FINAL])
        events = await collect(session.run(AgentInput(owner=OWNER, session_id=SESSION, message='收藏并核验这些电视剧')))
        assert discovery_repo.get_media_watchlist('tmdb', '501', 'tv') is not None
        assert discovery_repo.get_media_watchlist('tmdb', '502', 'tv') is not None
        assert rig.attempts == [arguments(501), arguments(502)] and await rig.remaining() == 3
        failed = next(event for event in events if event.type is AgentEventType.EFFECT_FAILED)
        assert failed.payload['result']['data']['execution_started'] is True
        assert events[-1].payload['status'] == 'partial', events[-1].payload
        assert events[-1].payload['finish_reason'] == 'invalid_arguments'
        assert '对象501实际写入后的领域参数错误' in events[-1].payload['answer']
        assert not any(event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED for event in events)
    asyncio.run(run())


def test_receipt_save_failure_preserves_real_write_without_claiming_completion(rig, monkeypatch):
    async def run():
        session = await trusted_session(rig, [call({**arguments(501), 'unsupported': True}), call(arguments(501))])

        async def reject_receipt(*_args, **_kwargs):
            return False

        monkeypatch.setattr('app.agent.kernel.turn.remember_result', reject_receipt)
        events = await collect(session.run(AgentInput(owner=OWNER, session_id=SESSION, message='收藏这部电视剧')))
        assert discovery_repo.get_media_watchlist('tmdb', '501', 'tv') is not None
        assert rig.attempts == [arguments(501)] and await rig.remaining() == 4
        assert events[-1].payload['status'] == 'partial'
        assert events[-1].payload['finish_reason'] == 'receipt_unavailable'
    asyncio.run(run())


@pytest.mark.parametrize('status', ['completed', 'failed', 'partial', 'accepted'])
def test_direct_retry_freezes_normalized_arguments_and_confirmation_executes_once(status):
    async def run():
        writes = []

        def execute(args, snapshot, _):
            assert snapshot == 'snapshot'
            writes.append(args)
            return {'ok': status != 'failed', 'status': status, 'summary': '执行结果'}

        tool = KernelToolSpec(name='fixture.change', domain='fixture', description='冻结字段转换',
            input_schema={'type': 'object', 'required': ['target'], 'properties': {
                'target': {'type': 'string'}, 'limit': {'type': 'integer'}}, 'additionalProperties': False},
            effect=ToolEffect.WRITE,
            validator=lambda args: {'target': args['target'].strip(), 'limit': args.get('limit', 1)},
            prepare=lambda args, _: PreparedEffect(preview={'summary': '修改预览'}, snapshot_fingerprint='snapshot',
                arguments={'target_number': args['target'].upper()}), execute_confirmed=execute)
        store = InMemorySessionStateStore()
        lease, _ = await store.begin_turn(owner='owner', session_id='normalization', request_id='r')
        context = ToolCallContext(lease.owner, lease.session_id, lease.request_id, lease.turn_id, lease,
                                 CancellationToken(), lambda _: asyncio.sleep(0))
        pipeline = ToolPipeline(catalog=ToolCatalog([tool]), state_store=store)
        with pytest.raises(ToolPipelineError) as failure:
            await pipeline.execute(tool.name, {'target': ' x ', 'unsupported': True}, context=context)
        assert failure.value.code == 'invalid_arguments'
        corrected = {'target': ' x '}
        preview = await pipeline.execute(tool.name, corrected, context=context)
        assert not writes and preview.arguments == {'target': 'x', 'limit': 1}
        assert preview.effect_plan is not None and dict(preview.effect_plan.arguments) == {'target_number': 'X'}
        corrected['target'] = 'forged-after-preview'
        result = await pipeline.execute_confirmed(preview.effect_plan.plan_id, context=context)
        assert writes == [{'target_number': 'X'}]
        assert result.arguments == {'target_number': 'X'}
        assert result.outcome.public_content['status'] == status
        with pytest.raises(ConfirmationClaimError):
            await pipeline.execute_confirmed(preview.effect_plan.plan_id, context=context)
        assert writes == [{'target_number': 'X'}]
    asyncio.run(run())


@pytest.mark.parametrize('effect', [ToolEffect.WRITE, ToolEffect.DANGER])
def test_retry_preview_replacement_and_cancellation_never_execute_old_plan(effect):
    async def run():
        writes = []
        tool = KernelToolSpec(name='fixture.change', domain='fixture', description='冻结计划替换',
            input_schema={'type': 'object', 'required': ['target'], 'properties': {
                'target': {'type': 'string'}, 'limit': {'type': 'integer'}}, 'additionalProperties': False},
            effect=effect, prepare=lambda args, _: PreparedEffect(
                preview={'summary': '修改预览'}, snapshot_fingerprint=args['target']),
            execute_confirmed=lambda args, *_: writes.append(args) or {'ok': True, 'status': 'completed'})
        store = InMemorySessionStateStore()
        lease, _ = await store.begin_turn(owner='owner', session_id='cancel-preview', request_id='r')
        context = ToolCallContext(lease.owner, lease.session_id, lease.request_id, lease.turn_id, lease,
                                 CancellationToken(), lambda _: asyncio.sleep(0))
        pipeline = ToolPipeline(catalog=ToolCatalog([tool]), state_store=store)
        with pytest.raises(ToolPipelineError):
            await pipeline.execute(tool.name, {'target': 'x', 'limit': 'bad'}, context=context)
        first = (await pipeline.execute(tool.name, {'target': 'x'}, context=context)).effect_plan
        assert first is not None and not writes
        with pytest.raises(ToolPipelineError):
            await pipeline.execute(tool.name, {'target': 'y', 'limit': 'bad'}, context=context)
        unchanged = await pipeline.get_pending_plan(owner=lease.owner, session_id=lease.session_id)
        assert unchanged is not None and unchanged.plan_id == first.plan_id
        second = (await pipeline.execute(tool.name, {'target': 'y'}, context=context)).effect_plan
        assert second is not None and second.plan_id != first.plan_id and not writes
        with pytest.raises(ConfirmationClaimError):
            await pipeline.execute_confirmed(first.plan_id, context=context)
        assert await pipeline.cancel_effect(second.plan_id, lease=lease)
        assert not await pipeline.cancel_effect(second.plan_id, lease=lease)
        with pytest.raises(ConfirmationClaimError):
            await pipeline.execute_confirmed(second.plan_id, context=context)
        assert not writes
        assert not (await store.load(owner=lease.owner, session_id=lease.session_id)).pending_effect_plan_id
    asyncio.run(run())
