"""后台云盘完成后继续原始目标：真实SQLite/签名计划、原生模型wire；Provider显式离线。"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import partial
from unittest.mock import patch

import httpx
import pytest

from app import config
from app import database as db
from app.agent import effect_completion as effects
from app.agent.feature_gate import initialize_runtime_fence
from app.agent.kernel.bootstrap import _capture_creation_binding
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput
from app.agent.models import ToolContext, ToolResult
from app.modules import organize_tasks
from app.repositories import organize_operation_jobs as jobs
from tests.support import initialized_web_runtime, isolated_test_database
from tests.test_agent_effect_admission import (
    _domain_arguments,
    _offline_runtime,
    _OfflineClient,
    _paused_manager,
    _run_real_worker,
    _spec,
)
from tests.test_agent_task_first_slice import NativeScript

OWNER, SID = 'effect-follow-owner', 'effect-follow-session'
GOAL = '将 source 中的测试视频改名为 admitted.mp4，完成后重新读取文件信息告诉我实际名称。'


class FollowScript(NativeScript):
    def __init__(self, *, resume=False):
        super().__init__('tmdb')
        self.steps = ['inspect', 'finish'] if resume else ['change']
        self.prefix = 'resumed' if resume else 'source'
        self.change_arguments = None

    async def respond(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        step = self.steps.pop(0)
        if self.prefix == 'resumed' and step == 'inspect':
            writes = [row for row in body['messages'] if row['role'] == 'tool' and row.get('tool_call_id') == 'source-change']
            assert len(writes) == 1
            result = json.loads(writes[0]['content'])
            assert result['data']['background_job']['status'] == 'completed', 'resume must not feed the stale running result to the model'
        if step == 'finish':
            assert any('admitted.mp4' in row.get('content', '') for row in body['messages'] if row['role'] == 'tool')
            message = {'content': '已重新读取，文件实际名称是 admitted.mp4。'}
        else:
            tool, args = {
                'discover': ('agent__run_code', {'language': 'shell', 'code': 'mf --help'}),
                'change': ('cloud__change', self.change_arguments),
                'inspect': ('guangya__inspect_after', {}),
            }[step]
            message = {'tool_calls': [{'id': self.prefix+'-'+step, 'type': 'function',
                'function': {'name': tool, 'arguments': json.dumps(args)}}]}
        return httpx.Response(200, json={'choices': [{'message': message,
            'finish_reason': 'stop' if step == 'finish' else 'tool_calls'}]})


def _session(store, script, client, reads, *, stack):
    if any(step in {'change', 'execute'} for step in script.steps):
        script.change_arguments = _domain_arguments(
            'fs', client, ToolContext(owner=OWNER, session_id=SID), stack,
            new_name='second.mp4' if isinstance(script, NextWriteScript) else 'admitted.mp4',
        )
    def inspect(_args, _context):
        item = client.file_info('rename')
        assert item.name == 'admitted.mp4', 'must not inspect before the confirmed write has completed'
        reads.append(item.name)
        return ToolResult(True, 'success', '已重新读取真实文件信息', data={'name': item.name, 'size': item.size})
    tools = [
        replace(_spec('fs'), metadata={**_spec('fs').metadata, 'workflow': 'guangya_fs_change', 'workflow_stage': 20}),
        KernelToolSpec(name='guangya.inspect_after', domain='cloud', description='读取改名后的文件信息 名称 云盘 source',
            input_schema={'type': 'object', 'properties': {}}, effect=ToolEffect.READ, read=inspect),
    ]
    catalog = ToolCatalog(tools)
    session = AgentSession(model=script.adapter, catalog=catalog,
        pipeline=ToolPipeline(catalog=catalog, state_store=store), state_store=store)
    session.continuation_binding = _capture_creation_binding()
    return session


@asynccontextmanager
async def _ready_case(tmp_path, *, channel='web', route=None, fail_write=False, runtime_settings=None, active_seconds=900, source_script=None):
    class RefusingClient(_OfflineClient):
        def rename(self, *_args, **_kwargs):
            raise RuntimeError('offline Provider refused rename')
    provider = RefusingClient() if fail_write else _OfflineClient()
    installation = initialized_web_runtime() if runtime_settings is not None else isolated_test_database()
    with installation, _offline_runtime(tmp_path, provider) as stack, _paused_manager():
        initialize_runtime_fence()
        if runtime_settings is not None:
            config.set_and_save(runtime_settings)
        from app.agent.kernel.ports import sdk_effects
        client = sdk_effects.GuangYaClient()
        store, reads, first = SQLiteKernelStore(), [], source_script or FollowScript()
        session = _session(store, first, client, reads, stack=stack)
        session.limits = replace(session.limits, max_active_seconds=active_seconds)
        events = [e async for e in session.run(AgentInput(message=GOAL, owner=OWNER, session_id=SID, channel=channel))]
        assert events[-1].payload['status'] == 'approval_required'
        plan_id = (await store.load(owner=OWNER, session_id=SID)).pending_effect_plan_id
        with patch.object(effects, 'wait_for_effect_completion', partial(effects.wait_for_effect_completion, timeout_seconds=0)):
            confirmation = [e async for e in session.confirm(owner=OWNER, session_id=SID, plan_id=plan_id, channel=channel, delivery_route=route or {})]
        assert confirmation[-1].payload.get('status') == 'waiting', [(e.type.value, dict(e.payload)) for e in confirmation]
        from app.agent.kernel.adapters import TurnViewBuilder
        view = TurnViewBuilder()
        for event in confirmation:
            view.apply(event)
        assert view.build().terminal and view.build().status == 'waiting'
        assert reads == []
        source_users = [r for r in (await store.load(owner=OWNER, session_id=SID)).conversation if r['role'] == 'user']
        assert await store.due_continuations() == []
        with db.get_conn() as conn:
            row = conn.execute('SELECT * FROM organize_operation_jobs').fetchone()
        assert row['status'] == 'pending'
        worker = _run_real_worker(jobs.claim_organize_operation_job(row['job_id']))
        get_organize_operation_job_result = jobs.get_organize_operation_job(row['job_id'])
        assert get_organize_operation_job_result is not None
        assert get_organize_operation_job_result['status'] in ({'partial', 'failed'} if fail_write else {'completed'})
        def due(s):
            s.metadata['effect_waits'][plan_id]['next_poll_at'] = 0
            s.metadata['effect_next_poll_at'] = 0
        await store.update_effect_state(owner=OWNER, session_id=SID, change=due)
        with patch.object(organize_tasks, 'get_organize_manager', lambda: worker):
            assert await effects.poll_effect_receipts(store) == 1
        cold = SQLiteKernelStore()
        candidates = await cold.due_continuations()
        assert len(candidates) == 1
        yield cold, candidates[0], client, reads, stack, source_users


def test_native_effect_wait_resumes_original_cloud_goal_without_a_synthetic_user(tmp_path):
    async def run():
        async with _ready_case(tmp_path) as (cold, candidate, client, reads, stack, source_users):
            follow = FollowScript(resume=True)
            resumed = _session(cold, follow, client, reads, stack=stack)
            assert await resumed.resume_wait(candidate=candidate)
            final = await cold.load(owner=OWNER, session_id=SID)
            assert reads == ['admitted.mp4'], final.conversation[-4:]
            assert not follow.steps
            assert [r for r in final.conversation if r['role'] == 'user'] == source_users
            assert await cold.due_continuations() == []
            assert final.metadata['active_publication']['status'] == 'finished'
            with db.get_conn() as conn:
                assert conn.execute('SELECT COUNT(*) FROM organize_operation_jobs').fetchone()[0] == 1
    asyncio.run(run())


class NextWriteScript(FollowScript):
    def __init__(self):
        super().__init__(resume=True)
        self.steps = ['execute']

    async def respond(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        step = self.steps.pop(0)
        assert self.change_arguments is not None
        assert step == 'execute' and self.change_arguments['operations']
        name = 'cloud__change'
        return httpx.Response(200, json={'choices': [{'message': {'tool_calls': [{
            'id': 'next-'+step, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(self.change_arguments)},
        }]}, 'finish_reason': 'tool_calls'}]})


def test_new_implicit_plan_after_completion_requires_fresh_confirmation(tmp_path):
    async def run():
        async with _ready_case(tmp_path) as (store, candidate, client, reads, stack, source_users):
            script = NextWriteScript()
            session = _session(store, script, client, reads, stack=stack)
            assert await session.resume_wait(candidate=candidate)
            final = await store.load(owner=OWNER, session_id=SID)
            assert not script.steps
            assert final.pending_effect_plan_id and final.pending_effect_plan_id != candidate['wait_id']
            assert final.metadata['pending_effect_goal']['content'] == GOAL
            assert final.metadata['active_publication']['status'] == 'finished'
            file_info_result = client.file_info('rename')
            assert file_info_result is not None
            assert file_info_result.name == 'admitted.mp4', 'new write must not execute without a new confirmation'
            with db.get_conn() as conn:
                assert conn.execute('SELECT COUNT(*) FROM organize_operation_jobs').fetchone()[0] == 1
            assert [r for r in final.conversation if r['role'] == 'user'] == source_users
    asyncio.run(run())


def test_deleted_session_is_not_resurrected_by_old_scan(tmp_path):
    async def run():
        async with _ready_case(tmp_path) as (store, candidate, *_):
            assert await store.delete_session(owner=OWNER, session_id=SID)
            assert await store.claim_continuation(candidate, _capture_creation_binding()) is None
            with db.get_conn() as conn:
                assert conn.execute('SELECT COUNT(*) FROM agent_kernel_sessions').fetchone()[0] == 0
    asyncio.run(run())


@pytest.mark.parametrize("request_identity", ["new", "source"])
def test_active_new_turn_defers_original_goal_instead_of_overwriting_it(tmp_path, request_identity):
    async def run():
        from app.agent.kernel.model import ModelEvent, ModelEventType

        async with _ready_case(tmp_path) as (store, candidate, client, reads, stack, _users):
            entered, release = asyncio.Event(), asyncio.Event()
            request_id = candidate['source_request_id'] if request_identity == 'source' else 'new-user'

            class HeldModel:
                async def stream(self, request, *, cancellation):
                    entered.set()
                    await release.wait()
                    yield ModelEvent(ModelEventType.TEXT_DELTA, text='收到')
                    yield ModelEvent(ModelEventType.FINISH, finish_reason='stop')

            session = _session(store, FollowScript(resume=True), client, reads, stack=stack)
            session.model = HeldModel()

            async def foreground():
                return [event async for event in session.run(AgentInput(
                    owner=OWNER, session_id=SID, request_id=request_id, message='你好，请只回复收到',
                ))]

            running = asyncio.create_task(foreground())
            try:
                await asyncio.wait_for(entered.wait(), 5)
                before = await store.load(owner=OWNER, session_id=SID)
                assert before.metadata['active_publication']['status'] == 'running'
                assert before.metadata['active_publication']['request_id'] == request_id
                assert await store.claim_continuation(candidate, _capture_creation_binding()) is None
                current = await store.load(owner=OWNER, session_id=SID)
                assert current.generation == before.generation
                assert current.metadata['effect_waits'][candidate['wait_id']]['continuation']['state'] == 'pending'
            finally:
                release.set()
                events = await running
            assert events[-1].payload.get('status') == 'success'
            assert await store.claim_continuation(candidate, _capture_creation_binding()) is not None
    asyncio.run(run())


@pytest.mark.parametrize('mutation', ['binding', 'origin', 'expired'])
def test_retired_or_corrupt_original_goal_never_calls_model(tmp_path, mutation):
    async def run():
        async with _ready_case(tmp_path) as (store, candidate, client, reads, stack, _users):
            if mutation == 'binding':
                binding = replace(_capture_creation_binding(), effective_digest='changed')
            else:
                binding = _capture_creation_binding()
                def invalidate(state):
                    row = state.metadata['effect_waits'][candidate['wait_id']]
                    if mutation == 'origin':
                        row['sealed'] = 'not-an-authenticated-seed'
                    else:
                        row['continuation']['expires_at'] = 0
                await store.update_effect_state(owner=OWNER, session_id=SID, change=invalidate)
            assert await store.claim_continuation(candidate, binding) is None
            assert await store.due_continuations() == []
            assert reads == []
    asyncio.run(run())


def test_competing_claims_and_superseding_user_revoke_old_lease(tmp_path):
    async def run():
        from app.agent.kernel.state_types import _PublicationFinished
        async with _ready_case(tmp_path) as (store, candidate, *_):
            binding = _capture_creation_binding()
            lease, _ = await store.claim_continuation(candidate, binding)
            assert await SQLiteKernelStore().claim_continuation(candidate, binding) is None
            newer, _ = await store.begin_turn(owner=OWNER, session_id=SID, request_id='new-user')
            assert not await store.is_current(lease)
            await store.commit(newer, updates=(_PublicationFinished('', None),))
            current_candidates = await store.due_continuations()
            assert len(current_candidates) == 1
            assert await store.claim_continuation(candidate, binding) is None, 'old scan cannot claim requeued identity'
            assert await store.claim_continuation(current_candidates[0], binding) is not None
    asyncio.run(run())


def test_model_budget_is_durable_across_worker_restart(tmp_path):
    async def run():
        from app.agent.kernel.continuation import _ContinuationAction
        script = FollowScript()
        async with _ready_case(tmp_path, source_script=script) as (store, candidate, *_):
            binding = _capture_creation_binding()
            initial = (await store.load(owner=OWNER, session_id=SID)).metadata['effect_waits'][candidate['wait_id']]['continuation']['budget']
            before = initial['model_spent']
            assert before == len(script.requests) > 0
            restarted = SQLiteKernelStore()
            claimed = await restarted.claim_continuation(candidate, binding)
            assert claimed is not None
            lease, state = claimed
            budget = state.metadata['effect_waits'][candidate['wait_id']]['continuation']['budget']
            for kind in ('model', 'tool', 'token'):
                assert budget[kind + '_spent'] == initial[kind + '_spent']
            await restarted.commit(lease, updates=(_ContinuationAction('', {
                'operation': 'reserve', 'model_limit': before + 1, 'tool_limit': 10,
                'model_spent': 1, 'tool_spent': 1,
            }, binding=binding),))
            cold = SQLiteKernelStore()
            saved = (await cold.load(owner=OWNER, session_id=SID)).metadata['effect_waits'][candidate['wait_id']]['continuation']['budget']
            assert saved['model_spent'] == before + 1
            assert saved['tool_spent'] == initial['tool_spent'] + 1
            assert saved['token_spent'] == initial['token_spent']
            assert saved['model_limit'] == before + 1
            with pytest.raises(ValueError, match='model_budget_exceeded'):
                await cold.commit(lease, updates=(_ContinuationAction('', {
                    'operation': 'reserve', 'model_limit': 100, 'tool_limit': 100,
                    'model_spent': 1, 'tool_spent': 0,
                }, binding=binding),))
            row = (await SQLiteKernelStore().load(owner=OWNER, session_id=SID)).metadata['effect_waits'][candidate['wait_id']]['continuation']
            assert row['budget'] == saved, '被拒绝的预留不能丢失成本或放宽原上限'
    asyncio.run(run())


def test_expired_running_lease_can_resume_without_replaying_confirmed_write(tmp_path):
    async def run():
        from app.agent.kernel.continuation import (
            CLAIM_SECONDS,
            EFFECT_ACTIVE_SECONDS,
            _ContinuationAction,
        )
        async with _ready_case(tmp_path, active_seconds=3600) as (store, candidate, client, reads, stack, _users):
            binding = _capture_creation_binding()
            old_lease, _ = await store.claim_continuation(candidate, binding)
            await store.commit(old_lease, updates=(_ContinuationAction('', {
                'operation': 'reserve', 'model_limit': 8, 'tool_limit': 10,
                'model_spent': 1, 'tool_spent': 0,
            }, binding=binding),))
            cold = SQLiteKernelStore(clock=lambda: store._clock() + EFFECT_ACTIVE_SECONDS + CLAIM_SECONDS + 1)
            candidates = await cold.due_continuations()
            assert len(candidates) == 1 and candidates[0]['state'] == 'running'
            session = _session(cold, FollowScript(resume=True), client, reads, stack=stack)
            session.limits = replace(session.limits, max_active_seconds=3600)
            assert await session.resume_wait(candidate=candidates[0])
            assert not await cold.is_current(old_lease)
            assert reads == ['admitted.mp4']
            budget = (await cold.load(owner=OWNER, session_id=SID)).metadata['effect_waits'][candidate['wait_id']]['continuation']['budget']
            assert budget['active_spent'] >= EFFECT_ACTIVE_SECONDS + CLAIM_SECONDS
            assert budget['token_spent'] > candidate['budget']['token_spent']
            with db.get_conn() as conn:
                assert conn.execute('SELECT COUNT(*) FROM organize_operation_jobs').fetchone()[0] == 1
    asyncio.run(run())


def test_background_tg_terminal_persists_native_view_for_delivery_before_stream_ends(tmp_path):
    async def run():
        route = {'chat_id': '12345', 'message_thread_id': 17}
        async with _ready_case(tmp_path, channel='telegram', route=route) as (store, candidate, client, reads, stack, _users):
            session = _session(store, FollowScript(resume=True), client, reads, stack=stack)
            assert await session.resume_wait(candidate=candidate)
            cold = SQLiteKernelStore()
            deliveries = await cold.due_continuations()
            assert len(deliveries) == 1 and deliveries[0]['kind'] == 'delivery'
            assert deliveries[0]['delivery_view']['status'] == 'success'
            assert 'admitted.mp4' in deliveries[0]['delivery_view']['answer']
            state = await cold.load(owner=OWNER, session_id=SID)
            assert deliveries[0]['delivery_route'] == route
            assert state.metadata['active_publication']['status'] == 'finished'
    asyncio.run(run())


def test_new_tg_approval_is_durable_without_executing_it(tmp_path):
    async def run():
        async with _ready_case(tmp_path, channel='telegram', route={'chat_id': '12345'}) as (store, candidate, client, reads, stack, _users):
            session = _session(store, NextWriteScript(), client, reads, stack=stack)
            assert await session.resume_wait(candidate=candidate)
            cold = SQLiteKernelStore()
            deliveries = await cold.due_continuations()
            assert len(deliveries) == 1 and deliveries[0]['kind'] == 'delivery'
            view = deliveries[0]['delivery_view']
            assert view['status'] == 'approval_required'
            assert view['approval']['plan_id'] == (await cold.load(owner=OWNER, session_id=SID)).pending_effect_plan_id
            file_info_result_2 = client.file_info('rename')
            assert file_info_result_2 is not None
            assert file_info_result_2.name == 'admitted.mp4'
    asyncio.run(run())


def test_failed_prior_effect_requires_read_before_new_write_prepare(tmp_path):
    async def run():
        async with _ready_case(tmp_path, fail_write=True) as (store, candidate, client, reads, stack, _users):
            state = await store.load(owner=OWNER, session_id=SID)
            assert state.metadata['effect_waits'][candidate['wait_id']]['final_result']['ok'] is False
            script = NextWriteScript()
            # An immediate WRITE cannot replace an actual diagnostic observation.
            script.steps = ['execute', 'finish']
            original = script.respond
            async def respond(request):
                if script.steps[0] != 'finish':
                    return await original(request)
                body = json.loads(request.content)
                assert any('effect_diagnostic_only' in str(row) for row in body['messages'] if row['role'] == 'tool')
                script.steps.pop(0)
                return httpx.Response(200, json={'choices': [{'message': {'content': '原任务失败，未执行后续写入。'}, 'finish_reason': 'stop'}]})
            script.respond = respond
            session = _session(store, script, client, reads, stack=stack)
            assert await session.resume_wait(candidate=candidate)
            assert not script.steps
            assert not (await store.load(owner=OWNER, session_id=SID)).pending_effect_plan_id
            with db.get_conn() as conn:
                assert conn.execute('SELECT COUNT(*) FROM organize_operation_jobs').fetchone()[0] == 1
    asyncio.run(run())


@pytest.mark.parametrize("fail_write", [False, True])
def test_foreground_completion_consumes_same_goal_without_background_double_run(tmp_path, fail_write):
    class RefusingClient(_OfflineClient):
        def rename(self, *_args, **_kwargs):
            raise RuntimeError("offline provider rejected rename")
    async def run():
        with isolated_test_database(), _offline_runtime(tmp_path, RefusingClient() if fail_write else _OfflineClient()) as stack, _paused_manager():
            initialize_runtime_fence()
            from app.agent.kernel.ports import sdk_effects
            client = sdk_effects.GuangYaClient()
            store, reads, script = SQLiteKernelStore(), [], FollowScript()
            script.steps += ['finish'] if fail_write else ['inspect', 'finish']
            session = _session(store, script, client, reads, stack=stack)
            events = [e async for e in session.run(AgentInput(message=GOAL, owner=OWNER, session_id=SID, channel='web'))]
            assert events[-1].payload['status'] == 'approval_required'
            plan_id = (await store.load(owner=OWNER, session_id=SID)).pending_effect_plan_id
            original_wait = effects.wait_for_effect_completion
            async def complete_before_wait(result, **options):
                with db.get_conn() as conn:
                    job = conn.execute('SELECT job_id FROM organize_operation_jobs').fetchone()
                worker = _run_real_worker(jobs.claim_organize_operation_job(job['job_id']))
                with patch.object(organize_tasks, 'get_organize_manager', lambda: worker):
                    return await original_wait(result, **options)
            with patch.object(effects, 'wait_for_effect_completion', complete_before_wait):
                confirmed = [e async for e in session.confirm(owner=OWNER, session_id=SID, plan_id=plan_id, channel='web')]
            assert confirmed[-1].payload['status'] == ('partial' if fail_write else 'success'), confirmed[-1].payload
            assert not script.steps
            assert reads == ([] if fail_write else ['admitted.mp4'])
            assert await SQLiteKernelStore().due_continuations() == []
    asyncio.run(run())


def test_local_read_quota_parks_effect_followup_without_asking_user_again(tmp_path):
    async def run():
        from app.agent.errors import ToolRateLimitError
        from app.agent.rate_limit import RateLimitDecision
        async with _ready_case(tmp_path) as (store, candidate, client, reads, stack, source_users):
            now = [store._clock()]
            store._clock = lambda: now[0]
            class Quota:
                async def acquire(self, *, tool_name, **_kwargs):
                    if tool_name == 'guangya.inspect_after':
                        raise ToolRateLimitError(RateLimitDecision(False, 6, 1, 0,
                            retry_after_seconds=5, retry_at_epoch=now[0] + 5, reason='quota'))
            session = _session(store, FollowScript(resume=True), client, reads, stack=stack)
            session.pipeline.rate_limiter = Quota()
            assert await session.resume_wait(candidate=candidate)
            assert reads == [] and await store.due_continuations() == []
            now[0] += 6
            next_candidate, = await store.due_continuations()
            assert next_candidate['budget']['wait_segments'] == 1
            summary = FollowScript(resume=True)
            summary.steps = ["finish"]
            resumed = _session(store, summary, client, reads, stack=stack)
            assert await resumed.resume_wait(candidate=next_candidate)
            assert reads == ['admitted.mp4'] and await store.due_continuations() == []
            final = await store.load(owner=OWNER, session_id=SID)
            assert [r for r in final.conversation if r['role'] == 'user'] == source_users
    asyncio.run(run())


def test_real_outbox_handoff_survives_crash_before_delivery_ack_without_new_model(tmp_path):
    async def run():
        from app.agent.kernel.continuation import deliver_continuation_result
        from app.bot.agent_adapter import (
            prepare_agent_continuation_delivery,
            publish_agent_continuation_delivery,
        )
        from app.modules import telegram_notification_center as notifications
        values = {'AGENT_ENABLED': '1', 'TG_AGENT_ENABLED': '1', 'TG_CHAT_ID': '-100', 'TG_AGENT_ALLOWED_USER_IDS': '7'}
        with patch(__name__ + '.OWNER', 'tg:v1:-100\x1f7'), patch.object(notifications, 'wake_telegram_notification_dispatcher', return_value=False):
            async with _ready_case(tmp_path, channel='telegram', route={'chat_id': '-100', 'message_thread_id': 42}, runtime_settings=values) as (store, candidate, client, reads, stack, _users):
                script = FollowScript(resume=True)
                session = _session(store, script, client, reads, stack=stack)
                assert await session.resume_wait(candidate=candidate)
                delivery, = await store.due_continuations()
                original = store.update_effect_state
                async def cut(**_kwargs):
                    raise RuntimeError('fault injection: outbox committed, kernel ACK not committed')
                with patch.object(notifications, '_agent_interaction_enabled', return_value=True):
                    store.update_effect_state = cut
                    with pytest.raises(RuntimeError, match='ACK not committed'):
                        await deliver_continuation_result(
                            store, delivery,
                            prepare_delivery=prepare_agent_continuation_delivery,
                            publish_delivery=publish_agent_continuation_delivery,
                        )
                    store.update_effect_state = original
                    with db.get_conn() as conn:
                        rows = conn.execute("SELECT event_json FROM telegram_notification_outbox WHERE json_extract(event_json,'$._agent_continuation_delivery') IS NOT NULL").fetchall()
                    assert len(rows) == 1
                    saved = json.loads(rows[0]['event_json'])['_agent_continuation_delivery']
                    assert saved['delivery_route'] == {'chat_id': '-100', 'message_thread_id': 42}
                    assert 'admitted.mp4' in saved['html']
                    cold = SQLiteKernelStore()
                    pending, = await cold.due_continuations()
                    assert await deliver_continuation_result(
                        cold, pending,
                        prepare_delivery=prepare_agent_continuation_delivery,
                        publish_delivery=publish_agent_continuation_delivery,
                    )
                    assert await cold.due_continuations() == []
                    with db.get_conn() as conn:
                        assert conn.execute("SELECT COUNT(*) FROM telegram_notification_outbox WHERE json_extract(event_json,'$._agent_continuation_delivery') IS NOT NULL").fetchone()[0] == 1
                    assert len(script.requests) == 2 and reads == ['admitted.mp4']
    asyncio.run(run())


def test_slow_continuation_does_not_block_receipts_or_start_a_second_worker():
    import threading

    from app.modules.agent_jobs_scheduler import AgentJobsScheduler
    entered, release, polled = threading.Event(), threading.Event(), threading.Event()
    scheduler = AgentJobsScheduler(interval=.1)
    calls = []
    def slow_model():
        calls.append('model')
        entered.set()
        assert release.wait(5)
        return 0
    async def receipts(*_args, **_kwargs):
        if entered.is_set():
            polled.set()
        return 0
    with patch('app.agent.effect_completion.poll_effect_receipts', side_effect=receipts), patch(
        'app.agent.feature_gate.is_agent_enabled', return_value=True,
    ), patch.object(scheduler, 'run_once', return_value=0), patch.object(
        scheduler, '_run_due_continuation_once', side_effect=slow_model,
    ):
        try:
            scheduler.start()
            assert entered.wait(3) and polled.wait(3), 'receipt polling must continue during slow model I/O'
            assert not scheduler.stop(timeout=0), 'a running continuation is not a stopped scheduler'
            scheduler.start()
            assert calls == ['model'], 'start cannot spawn another worker before old continuation exits'
        finally:
            release.set()
            assert scheduler.stop(timeout=5)
    assert scheduler._thread is None and scheduler._continuation_thread is None


@pytest.mark.parametrize('batch_size', [1, 2])
def test_new_user_read_uses_native_batch_position_after_old_receipt_merge(tmp_path, batch_size):
    class NewReadScript(FollowScript):
        def __init__(self):
            super().__init__()
            self.steps = ['read', 'finish']

        async def respond(self, request):
            body = json.loads(request.content)
            self.requests.append(body)
            step = self.steps.pop(0)
            if step == 'read':
                message = {'tool_calls': [
                    {'id': f'new-read-{i}', 'type': 'function',
                     'function': {'name': 'guangya__inspect_after', 'arguments': '{}'}}
                    for i in range(batch_size)
                ]}
            else:
                assert sum(r.get('role') == 'tool' and r.get('tool_call_id', '').startswith('new-read-')
                           and 'admitted.mp4' in r['content'] for r in body['messages']) == batch_size
                message = {'content': '已按新请求读取文件，实际名称为 admitted.mp4。'}
            return httpx.Response(200, json={'choices': [{'message': message,
                'finish_reason': 'tool_calls' if step == 'read' else 'stop'}]})

    async def run():
        async with _ready_case(tmp_path) as (store, candidate, client, reads, stack, users):
            script = NewReadScript()
            session = _session(store, script, client, reads, stack=stack)
            events = [event async for event in session.run(AgentInput(
                message='先读取改名后的文件，告诉我实际名称', owner=OWNER, session_id=SID,
                request_id='new-read-during-continuation', channel='web',
            ))]
            assert events[-1].payload.get('status') == 'success', dict(events[-1].payload)
            assert reads == ['admitted.mp4'] * batch_size and not script.steps
            state = await store.load(owner=OWNER, session_id=SID)
            assert len([r for r in state.conversation if r['role'] == 'user']) == len(users) + 1
            # Retain the older goal without letting it steal the newer foreground turn.
            assert state.metadata['effect_waits'][candidate['wait_id']]['continuation']['state'] == 'pending'
            assert state.metadata['active_publication']['request_id'] == 'new-read-during-continuation'
            with db.get_conn() as conn:
                assert conn.execute('SELECT COUNT(*) FROM organize_operation_jobs').fetchone()[0] == 1
    asyncio.run(run())
