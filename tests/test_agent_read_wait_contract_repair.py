"""Budget, model policy and durable rejection through real native waits."""
import asyncio
from dataclasses import replace
from datetime import datetime
from unittest.mock import patch

import pytest

from app import config
from app import database as db
from app.agent.model_catalog import provider_channel_fingerprint
from app.agent.feature_gate import (
    begin_runtime_change,
    finish_runtime_change,
    initialize_runtime_fence,
    read_runtime_fence,
)
from app.agent.kernel.bootstrap import (
    _capture_creation_binding,
)
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.state_types import AgentInput
from app.modules.agent_jobs_scheduler import AgentJobsScheduler
from tests.test_agent_read_wait_lifecycle import build_wait_runtime
from tests.test_agent_read_wait_lifecycle import (
    GOAL,
    OWNER,
    Quota,
    WaitScript,
    parked_runtime,
)
from tests.test_agent_read_wait_lifecycle import (
    isolated_database as isolated_database,
)


def resumed(clock, script=None):
    script = script or WaitScript(True)
    runtime = build_wait_runtime(model=script.adapter, store=SQLiteKernelStore(clock=lambda: clock[0]))
    runtime.session.continuation_binding = _capture_creation_binding()
    runtime.session.model_tools = frozenset({"agent.run_code", "fixture.lookup"})
    calls = []

    async def read(args, context):
        calls.append(args)
        return {'ok': True, 'summary': 'contract READ'}

    tool = runtime.session.catalog.get('fixture.lookup')
    runtime.session.catalog._tools['fixture.lookup'] = replace(tool, read=read)
    return runtime, script, calls


@pytest.mark.parametrize('limit', [3, 4])
def test_tool_budget_precedes_model_without_precharging(limit):
    async def run():
        clock = [100]
        _, candidate = await parked_runtime(clock)
        clock[0] = 110
        runtime, script, calls = resumed(clock)
        runtime.session.limits = replace(runtime.session.limits, max_tool_calls=limit)
        at_wire = []
        respond = script.respond

        async def observe(request):
            cold = await runtime.store.load(owner=OWNER, session_id='wait-control')
            at_wire.append(dict(cold.metadata['read_wait']['budget']))
            return await respond(request)

        script.respond = observe
        await runtime.session.resume_wait(candidate=candidate)
        state = await runtime.store.load(owner=OWNER, session_id='wait-control')
        record = state.metadata['read_wait']
        if limit == 3:
            assert script.requests == []
            assert calls == []
            assert record['budget']['model_spent'] == 3
            assert record['budget']['tool_spent'] == 3
            assert record['budget']['token_spent'] == candidate['budget']['token_spent']
            assert record['terminal_reason'] == 'task_budget_exceeded'
        else:
            assert len(calls) == 1 and len(script.requests) == 1
            assert at_wire[0]['tool_spent'] == 4  # The executor completed the pending READ before asking the model.
            assert at_wire[0]['model_spent'] == 4
            assert record['budget']['model_spent'] == 4
            assert record['budget']['tool_spent'] == 4
            assert record['terminal_reason'] == 'completed'
    asyncio.run(run())


@pytest.mark.parametrize('changed', [False, True])
def test_telegram_issued_model_policy_is_not_rerouted(changed):
    from app.modules import telegram_model_preferences as prefs

    async def run():
        db.init_db()
        initialize_runtime_fence()
        clock = [100]
        owner, sid = 'tg:v1:-100123\x1f7', 'tg-route-wait'
        first = WaitScript()
        first.adapter.settings = replace(first.adapter.settings, telegram_channel=provider_channel_fingerprint(config.get_many(("AGENT_LLM_API_URL", "AGENT_LLM_PROTOCOL", "AGENT_LLM_API_KEY"))))
        runtime = build_wait_runtime(model=first.adapter, store=SQLiteKernelStore(clock=lambda: clock[0]))
        binding = _capture_creation_binding()
        runtime.session.continuation_binding = binding
        runtime.session.pipeline.rate_limiter = Quota()
        # Routing-only control; real authorization is covered separately.
        with patch('app.agent.kernel.ports.mediaflux_policy.telegram_owner_route_is_currently_authorized', return_value=True):
            prefs.set_telegram_model_preference(owner, sid, 'original-model')
            events = [e async for e in runtime.session.run(AgentInput(message=GOAL, owner=owner, session_id=sid, request_id='source', channel='telegram'))]
            assert events[-1].payload.get('status') == 'waiting'
            candidate = (await runtime.store.load(owner=owner, session_id=sid)).metadata['read_wait']
            assert {r['model'] for r in first.requests} == {'original-model'}
            if changed:
                prefs.set_telegram_model_preference(owner, sid, 'different-model')
            assert _capture_creation_binding() == binding
            clock[0] = 110
            script = WaitScript(True)
            script.adapter.settings = replace(script.adapter.settings, telegram_channel=provider_channel_fingerprint(config.get_many(("AGENT_LLM_API_URL", "AGENT_LLM_PROTOCOL", "AGENT_LLM_API_KEY"))))
            fresh, script, calls = resumed(clock, script)
            await fresh.session.resume_wait(candidate=candidate)
            record = (await fresh.store.load(owner=owner, session_id=sid)).metadata['read_wait']
            if changed:
                assert script.requests == [] and calls == []
                assert record['state'] == 'terminal'
                assert record['terminal_reason'] == 'model_policy_changed'
            else:
                assert len(calls) == 1 and len(script.requests) == 1
                assert {r['model'] for r in script.requests} == {'original-model'}
                assert record['terminal_reason'] == 'completed'
    asyncio.run(run())


def test_scheduler_retires_revoked_epoch_without_factory():
    clock = [100]
    asyncio.run(parked_runtime(clock))
    with db.get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old = read_runtime_fence(conn)
        finish_runtime_change(conn, begin_runtime_change(conn, old), old.config_digest)
    clock[0] = 110
    calls = []

    def forbidden(*args):
        calls.append(True)
        raise AssertionError('revoked route constructed runtime')

    scheduler = AgentJobsScheduler(clock=lambda: datetime.fromtimestamp(clock[0]), continuation_runtime_factory=forbidden)
    assert scheduler._run_due_continuation_once() == 0
    assert calls == []

    async def check():
        store = SQLiteKernelStore(clock=lambda: clock[0])
        record = (await store.load(owner=OWNER, session_id='wait-control')).metadata['read_wait']
        assert (record['state'], record['terminal_reason']) == ('terminal', 'runtime_changed')
        assert await store.due_continuations() == []
    asyncio.run(check())


@pytest.mark.parametrize('missing', [False, True, 'nonunique'])
def test_history_anchor_validation_is_durable(missing):
    async def run():
        clock = [100]
        runtime, candidate = await parked_runtime(clock)
        if missing:
            # Synthetic supported history update: not evidence of normal eviction.
            def remove(state):
                if missing == 'nonunique':
                    from copy import deepcopy
                    state.conversation += deepcopy(state.conversation)
                else:
                    state.conversation = [r for r in state.conversation if r.get('role') == 'user']
            await runtime.store.update_effect_state(owner=OWNER, session_id='wait-control', change=remove)
        clock[0] = 110
        fresh, script, calls = resumed(clock)
        await fresh.session.resume_wait(candidate=candidate)
        record = (await fresh.store.load(owner=OWNER, session_id='wait-control')).metadata['read_wait']
        assert record['state'] == 'terminal'
        assert record['terminal_reason'] == ('history_unavailable' if missing else 'completed')
        assert len(script.requests) == (0 if missing else 1)
        assert len(calls) == (0 if missing else 1)
    asyncio.run(run())


@pytest.mark.parametrize('winner', ['cancel', 'task', 'new_wait'])
def test_stale_cleanup_preserves_newer_authority(winner):
    from app.agent.kernel.task_state import TaskPatch, identity

    async def run():
        clock = [100]
        runtime, stale = await parked_runtime(clock)
        clock[0] = 110
        if winner == 'cancel':
            await runtime.session.cancel(owner=OWNER, session_id='wait-control', request_id='source')
        else:
            lease, state = await runtime.store.claim_continuation(stale, runtime.session.continuation_binding)
            task = state.metadata['task']
            await runtime.store.commit(lease, task_patch=TaskPatch(identity(task), 'cancel',
                {'operation': 'cancel', 'source': GOAL}, 'source', GOAL))
            if winner in {'task', 'new_wait'}:
                script = WaitScript()
                if winner == 'task':
                    script.steps = ['discover', 'create', 'finish']
                script.prefix = 'new-task'
                runtime.session.model = script.adapter
                events = [e async for e in runtime.session.run(AgentInput(message=GOAL, owner=OWNER,
                    session_id='wait-control', request_id='new-source'))]
                assert events[-1].payload.get('status') == ('waiting' if winner == 'new_wait' else 'success')
        before = await runtime.store.load(owner=OWNER, session_id='wait-control')
        if winner in {'task', 'new_wait'}:
            assert before.metadata['task']['task_id'] != stale['task_id']
        if winner == 'new_wait':
            assert before.metadata['read_wait']['wait_id'] != stale['wait_id']
        binding = runtime.session.continuation_binding
        assert binding is not None
        changed = replace(binding, effective_digest='different-snapshot')
        assert await runtime.store.claim_continuation(stale, changed) is None
        assert not await runtime.session.resume_wait(candidate=stale)
        after = await runtime.store.load(owner=OWNER, session_id='wait-control')
        assert after.to_payload() == before.to_payload()
    asyncio.run(run())
