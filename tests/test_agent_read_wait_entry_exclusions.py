"""Wait-bearing effect and live authorization entry controls."""
import asyncio
from datetime import datetime

import pytest

from app import config
from app import database as db
from app.agent.errors import ToolPipelineError
from app.agent.feature_gate import initialize_runtime_fence
from app.agent.kernel.bootstrap import (
    _capture_creation_binding,
)
from app.agent.kernel.capabilities import KernelToolSpec, ToolEffect
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.state_types import AgentInput, CancellationToken, PublicationLease
from app.agent.kernel.tool_calls import ToolCallContext
from app.modules.agent_jobs_scheduler import AgentJobsScheduler
from tests.test_agent_read_wait_lifecycle import (
    GOAL,
    OWNER,
    Quota,
    WaitScript,
    build_wait_runtime,
    parked_runtime,
)
from tests.test_agent_read_wait_lifecycle import (
    isolated_database as isolated_database,
)

from .test_agent_read_wait_contract_repair import resumed


@pytest.mark.parametrize('mode', ['pending', 'confirmed', 'unknown', 'explain'])
def test_wait_bearing_effect_entry_excludes_auto_execution(mode):
    async def run():
        clock = [100]
        runtime, candidate = await parked_runtime(clock)
        preparations, writes = [], []

        def prepare(args, context):
            preparations.append(dict(args))
            return PreparedEffect(preview={'summary': 'offline confirmation'}, snapshot_fingerprint='frozen')

        def execute(args, fingerprint, context):
            writes.append(dict(args))
            if mode == 'unknown':
                raise ToolPipelineError('result unknown; do not replay', code='outcome_unknown')
            return {'ok': True, 'status': 'success', 'summary': 'confirmed offline'}

        tool = KernelToolSpec(name='fixture.change', domain='library', description='offline write',
            input_schema={'type': 'object', 'properties': {}, 'additionalProperties': False},
            effect=ToolEffect.WRITE, prepare=prepare, execute_confirmed=execute)
        runtime.session.catalog._tools[tool.name] = tool
        state = await runtime.store.load(owner=OWNER, session_id='wait-control')
        lease = PublicationLease(OWNER, 'wait-control', state.generation, candidate['source_turn_id'], 'source')
        async def progress(event):
            pass
        context = ToolCallContext(owner=OWNER, session_id='wait-control', lease=lease,
            request_id='source', turn_id=lease.turn_id, report_progress=progress,
            cancellation=CancellationToken(), user_source=GOAL)
        result = await runtime.session.pipeline.execute(tool.name, {}, context=context)
        assert result.effect_plan is not None
        plan_id = result.effect_plan.plan_id
        assert preparations == [{}] and writes == []
        state = await runtime.store.load(owner=OWNER, session_id='wait-control')
        assert state.pending_effect_plan_id == plan_id
        receipt = None
        if mode != 'pending':
            script = WaitScript(True)
            runtime.session.model = script.adapter
            if mode == 'explain':
                events = [e async for e in runtime.session.explain(owner=OWNER, session_id='wait-control', plan_id=plan_id, message='只解释')]
            else:
                events = [e async for e in runtime.session.confirm(owner=OWNER, session_id='wait-control', plan_id=plan_id)]
            assert not any(e.payload.get('status') == 'waiting' for e in events)
            assert len(writes) == (0 if mode == 'explain' else 1)
            if mode == 'unknown':
                assert any(e.type.value == 'effect.failed' for e in events)
                saved = await runtime.store.load(owner=OWNER, session_id='wait-control')
                receipt = next(r for r in saved.conversation if r.get('completion_receipt_id') == plan_id)
                assert 'outcome_unknown' in receipt['content']
        clock[0] = 110
        fresh, script, reads = resumed(clock)
        await fresh.session.resume_wait(candidate=candidate)
        assert script.requests == [] and reads == []
        assert preparations == [{}]
        assert len(writes) == (1 if mode in {'confirmed', 'unknown'} else 0)
        if mode == 'unknown':
            assert receipt is not None
            saved = await fresh.store.load(owner=OWNER, session_id='wait-control')
            assert receipt in saved.conversation
    asyncio.run(run())


@pytest.fixture
def tg_config(tmp_path, monkeypatch):
    # Keep actual publisher, snapshots, preference store and admission predicates.
    import os
    from unittest.mock import patch
    monkeypatch.setattr(config, 'ENV_FILE', tmp_path / 'user.env')
    monkeypatch.setattr(config, '_cache', None)
    monkeypatch.setattr(config, '_STARTUP_ENV_OVERRIDES', frozenset())
    with patch.dict(os.environ):
        config.write_env_file(config.ENV_FILE, {}, replace=False)
        db.init_db()
        initialize_runtime_fence()
        config.set_and_save({'AGENT_ENABLED': '1', 'AGENT_LLM_ENABLED': '0'})
        yield


@pytest.mark.parametrize('change', ['tg_revoke', 'disable_enable', 'other_worker'])
def test_real_authorization_changes_block_wait_stream(tg_config, change):
    db.init_db()
    initialize_runtime_fence()
    config.set_and_save({'TG_AGENT_ENABLED': '1', 'TG_CHAT_ID': '-100123', 'TG_AGENT_ALLOWED_USER_IDS': '7'})
    clock = [100]
    owner, sid = 'tg:v1:-100123\x1f7', 'actual-tg'
    script = WaitScript()
    runtime = build_wait_runtime(model=script.adapter, store=SQLiteKernelStore(clock=lambda: clock[0]))
    runtime.session.continuation_binding = _capture_creation_binding()
    runtime.session.pipeline.rate_limiter = Quota()

    async def park():
        events = [e async for e in runtime.session.run(AgentInput(message=GOAL, owner=owner, session_id=sid, request_id='source', channel='telegram'))]
        assert events[-1].payload.get('status') == 'waiting'
        return (await runtime.store.load(owner=owner, session_id=sid)).metadata['read_wait']

    candidate = asyncio.run(park())
    old = runtime.session.continuation_binding
    if change == 'tg_revoke':
        config.set_and_save({'TG_AGENT_ALLOWED_USER_IDS': '8'})
    elif change == 'disable_enable':
        config.set_and_save({'AGENT_ENABLED': '0'})
        config.set_and_save({'AGENT_ENABLED': '1'})
    else:
        # Independent publisher changes real config while old object is retained.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as worker:
            worker.submit(config.set_and_save, {'AGENT_LLM_MODEL': 'other-worker-model'}).result(timeout=5)
    assert _capture_creation_binding() != old
    clock[0] = 110
    script.requests.clear()
    if change == 'tg_revoke':
        with pytest.raises(ToolPipelineError) as raised:
            asyncio.run(runtime.session.resume_wait(candidate=candidate))
        assert raised.value.code == 'authorization_denied'
    elif change == 'other_worker':
        from app.agent.feature_gate import RuntimeFenceError
        with pytest.raises(RuntimeFenceError):
            asyncio.run(runtime.session.resume_wait(candidate=candidate))
    factories = []

    def forbidden(*args):
        factories.append(True)
        raise AssertionError('revoked wait constructed runtime')

    scheduler = AgentJobsScheduler(clock=lambda: datetime.fromtimestamp(clock[0]), continuation_runtime_factory=forbidden)
    assert scheduler._run_due_continuation_once() == 0
    assert factories == [] and script.requests == []


@pytest.mark.parametrize('telegram', [False, True])
def test_legacy_policy_absence_never_infers_telegram_authority(tg_config, telegram):
    from unittest.mock import patch

    from app.modules import telegram_model_preferences as prefs

    async def run():
        config.set_and_save({'TG_AGENT_ENABLED': '1', 'TG_CHAT_ID': '-100123', 'TG_AGENT_ALLOWED_USER_IDS': '7'})
        clock = [100]
        owner = 'tg:v1:-100123\x1f7' if telegram else OWNER
        sid = 'legacy-policy'
        script = WaitScript()
        store = SQLiteKernelStore(clock=lambda: clock[0])
        runtime = build_wait_runtime(model=script.adapter, store=store)
        runtime.session.continuation_binding = _capture_creation_binding()
        runtime.session.pipeline.rate_limiter = Quota()
        write = store._write_state

        def legacy_encoding(conn, state):
            # Omit only the new field before the real signed SQLite write.
            # This is a legacy-schema fixture, not model-authored state authority.
            if state.metadata.get('read_wait', {}).get('state') == 'pending':
                state.metadata['read_wait'].pop('model_policy', None)
            return write(conn, state)

        with patch.object(store, '_write_state', side_effect=legacy_encoding):
            events = [e async for e in runtime.session.run(AgentInput(message=GOAL, owner=owner,
                session_id=sid, request_id='source', channel='telegram' if telegram else 'api'))]
        assert events[-1].payload.get('status') == 'waiting'
        candidate = (await store.load(owner=owner, session_id=sid)).metadata['read_wait']
        assert 'model_policy' not in candidate
        if telegram:
            prefs.set_telegram_model_preference(owner, sid, 'new-preference')
        clock[0] = 110
        fresh, script, reads = resumed(clock)
        await fresh.session.resume_wait(candidate=candidate)
        record = (await store.load(owner=owner, session_id=sid)).metadata['read_wait']
        assert record['terminal_reason'] == ('model_policy_changed' if telegram else 'completed')
        assert len(script.requests) == (0 if telegram else 1)
        assert len(reads) == (0 if telegram else 1)
    asyncio.run(run())


def test_scheduler_rejects_cached_factory_snapshot(tg_config):
    from dataclasses import replace
    clock = [100]
    old, script, _ = resumed(clock)
    old = replace(old, creation_binding=old.session.continuation_binding)
    config.set_and_save({'AGENT_LLM_MODEL': 'new-default'})
    asyncio.run(parked_runtime(clock))
    clock[0] = 110
    factories = []

    def stale_factory(candidate, binding, store):
        factories.append(binding)
        assert old.creation_binding != binding
        return old

    scheduler = AgentJobsScheduler(clock=lambda: datetime.fromtimestamp(clock[0]), continuation_runtime_factory=stale_factory)
    assert scheduler._run_due_continuation_once() == 0
    assert len(factories) == 1
    assert script.requests == []
