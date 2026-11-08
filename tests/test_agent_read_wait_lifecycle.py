"""Native quota rejection closes its call and parks the tracked task atomically."""
from tests.agent_task_native_helpers import task_call
import asyncio
import json
from dataclasses import replace
from datetime import datetime
from unittest.mock import patch

import httpx
import pytest

from app import database as db
from app.agent.errors import ToolRateLimitError
from app.agent.feature_gate import initialize_runtime_fence
from app.agent.kernel.bootstrap import (
    _capture_creation_binding,
    build_agent_kernel_runtime as _build_agent_kernel_runtime,
)
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.state_types import AgentInput
from app.agent.rate_limit import RateLimitDecision
from tests.test_agent_task_first_slice import GOAL, OWNER, NativeScript, declaration


def build_wait_runtime(**kwargs):
    from app.agent.kernel.capabilities import KernelToolSpec, ToolEffect

    runtime = _build_agent_kernel_runtime(**kwargs)
    runtime.session.catalog.register(KernelToolSpec(
        name="fixture.lookup", domain="fixture", description="读取等待测试对象",
        input_schema={"type": "object", "required": ["query"],
                      "properties": {"query": {"type": "string"}}, "additionalProperties": False},
        effect=ToolEffect.READ,
        read=lambda arguments, context: {"ok": True, "summary": arguments["query"]},
    ))
    runtime.session.model_tools = (runtime.session.model_tools or frozenset()) | {"fixture.lookup"}
    return runtime


@pytest.fixture(autouse=True)
def isolated_database(tmp_path):
    previous = db.DB_PATH, bool(getattr(db, '_configured_test_mode', False))
    try:
        db.configure_database(tmp_path / 'wait.db', test_mode=True)
        yield
    finally:
        db.configure_database(previous[0], test_mode=previous[1])


class WaitScript(NativeScript):
    def __init__(self, resume=False):
        super().__init__('tmdb')
        self.steps = ['finish'] if resume else ['discover', 'create', 'read']
        self.prefix = 'resume' if resume else 'initial'

    async def respond(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        step = self.steps.pop(0)
        if step == 'finish':
            message = {'content': '已取得真实查询结果。'}
        else:
            name, args = {
                'discover': ('agent__run_code', {'language': 'shell', 'code': 'mf --help'}),
                'create': task_call(declaration(), wire=True),
                'read': ('fixture__lookup', {'query': 'quota fixture'}),
            }[step]
            message = {'content': '', 'tool_calls': [{'id': self.prefix + '-' + step, 'type': 'function',
                       'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}]}
        return httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': 'stop' if step == 'finish' else 'tool_calls'}]})


class Quota:
    async def acquire(self, *, tool_name, **kwargs):
        if tool_name == 'fixture.lookup':
            raise ToolRateLimitError(RateLimitDecision(False, 6, 1, 0, retry_after_seconds=10,
                                                      retry_at_epoch=110, reason='quota'))


@pytest.mark.parametrize("cancel_entry", ["native", "stop"])
def test_native_quota_park_and_cold_cancel(cancel_entry):
    async def run():
        db.init_db()
        initialize_runtime_fence()
        script = WaitScript()
        store = SQLiteKernelStore(clock=lambda: 100)
        runtime = build_wait_runtime(model=script.adapter, store=store)
        # Explicit trusted injection; ordinary model metadata cannot supply authority.
        runtime.session.continuation_binding = _capture_creation_binding()
        runtime.session.pipeline.rate_limiter = Quota()
        events = [e async for e in runtime.session.run(AgentInput(message=GOAL, owner=OWNER,
                            session_id='wait-park', request_id='wait-source'))]
        assert events[-1].payload.get('status') == 'waiting', [(e.type.value, dict(e.payload)) for e in events[-3:]]
        cold = SQLiteKernelStore(clock=lambda: 100)
        state = await cold.load(owner=OWNER, session_id='wait-park')
        record = state.metadata['read_wait']
        assert record['state'] == 'pending'
        assert record['budget']['model_spent'] == 3
        assert record['budget']['tool_spent'] == 3
        assert record['budget']['wait_segments'] == 1
        assert record['due_at'] == 110
        assert record['expires_at'] == 400
        results = [r for r in state.conversation if r.get('tool_call_id') == 'initial-read']
        assert len(results) == 1 and 'rate_limited' in results[0]['content']
        fresh = build_wait_runtime(model=WaitScript(True).adapter, store=cold)
        if cancel_entry == 'native':
            assert await fresh.session.cancel(owner=OWNER, session_id='wait-park', request_id='wait-source')
        else:
            from app.agent.task_stop import stop_agent_session
            other = await stop_agent_session(fresh, OWNER, 'another-session')
            assert other['cancelled_continuations'] == []
            assert (await cold.load(owner=OWNER, session_id='wait-park')).metadata['read_wait'] == record
            stopped = await stop_agent_session(fresh, OWNER, 'wait-park')
            assert stopped['status'] == 'stopped' and stopped['stopped'] is True
            assert stopped['cancelled_continuations'] == [record['wait_id']]
            assert (await stop_agent_session(fresh, OWNER, 'wait-park'))['status'] == 'already_stopped'
        cancelled = await cold.load(owner=OWNER, session_id='wait-park')
        assert cancelled.metadata['read_wait']['state'] == 'cancelled'
        assert cancelled.metadata['read_wait']['budget'] == record['budget']
        assert cancelled.metadata['task'] == state.metadata['task']
        assert cancelled.conversation == state.conversation
    asyncio.run(run())


def test_scheduler_cold_native_resume_uses_new_id_and_one_read():
    from app.modules.agent_jobs_scheduler import AgentJobsScheduler
    timer = [100]
    async def park():
        db.init_db()
        initialize_runtime_fence()
        script = WaitScript()
        runtime = build_wait_runtime(model=script.adapter, store=SQLiteKernelStore(clock=lambda: timer[0]))
        runtime.session.continuation_binding = _capture_creation_binding()
        runtime.session.pipeline.rate_limiter = Quota()
        events = [e async for e in runtime.session.run(AgentInput(message=GOAL, owner=OWNER,
                          session_id='wait-resume', request_id='resume-source'))]
        assert events[-1].payload.get('status') == 'waiting'
    asyncio.run(park())
    timer[0] = 110
    resumed = WaitScript(True)
    handlers = []
    def factory(candidate, binding, store):
        runtime = build_wait_runtime(model=resumed.adapter, store=store)
        runtime.session.continuation_binding = binding
        async def read(args, context):
            handlers.append(args)
            return {'ok': True, 'summary': 'fixture READ complete'}
        tool = runtime.session.catalog.get('fixture.lookup')
        runtime.session.catalog._tools['fixture.lookup'] = replace(tool, read=read)
        return replace(runtime, creation_binding=binding)
    scheduler = AgentJobsScheduler(clock=lambda: datetime.fromtimestamp(timer[0]))
    scheduler._continuation_runtime_factory = factory
    assert getattr(scheduler, '_run_due_continuation_once', lambda: 0)() == 1
    assert len(handlers) == 1
    assert len(resumed.requests) == 1
    assert any('fixture READ complete' in str(row.get('content', '')) for row in resumed.requests[0]['messages'])
    old_wire = [r for r in resumed.requests[0]['messages'] if r.get('tool_call_id') == 'initial-read']
    assert len(old_wire) == 1 and 'rate_limited' in old_wire[0]['content']
    async def verify():
        state = await SQLiteKernelStore().load(owner=OWNER, session_id='wait-resume')
        assert state.metadata['read_wait']['state'] == 'terminal'
        assert state.metadata['read_wait']['terminal_reason'] == 'completed'
        budget = state.metadata['read_wait']['budget']
        assert budget['model_spent'] == 4 and budget['tool_spent'] == 4
        assert budget['wait_seconds_spent'] == 10
        results = {r.get('tool_call_id'): r for r in state.conversation if r.get('role') == 'tool'}
        assert 'rate_limited' in results['initial-read']['content']
        resumed_results = [row for key, row in results.items() if key != 'initial-read' and 'fixture READ complete' in row.get('content', '')]
        assert len(resumed_results) == 1
    asyncio.run(verify())
    assert scheduler._run_due_continuation_once() == 0


def test_restored_read_runs_before_model_without_regenerating_arguments():
    async def run():
        timer = [100]
        _, candidate = await parked_runtime(timer)
        timer[0] = 110
        script = WaitScript(True)
        runtime = build_wait_runtime(model=script.adapter, store=SQLiteKernelStore(clock=lambda: timer[0]))
        runtime.session.continuation_binding = _capture_creation_binding()
        invoked = []
        async def read(arguments, context):
            assert not script.requests, 'pending READ must run before model synthesis'
            invoked.append(arguments)
            return {'ok': True, 'summary': 'trusted pending READ completed'}
        tool = runtime.session.catalog.get('fixture.lookup')
        runtime.session.catalog._tools['fixture.lookup'] = replace(tool, read=read)
        assert await runtime.session.resume_wait(candidate=candidate)
        assert invoked == [{'query': 'quota fixture'}]
        assert len(script.requests) == 1
        state = await runtime.store.load(owner=OWNER, session_id='wait-control')
        assert state.metadata['read_wait']['terminal_reason'] == 'completed'
        assert state.metadata['read_wait']['budget']['model_spent'] == 4
        assert not await runtime.session.resume_wait(candidate=candidate)
        assert len(invoked) == 1
    asyncio.run(run())


@pytest.mark.parametrize('advance', [180, 301, -1])
def test_cold_wait_expiry_or_clock_reversal_never_requests_model(advance):
    async def run():
        timer = [100]
        _, candidate = await parked_runtime(timer)
        timer[0] += advance
        script = WaitScript(True)
        runtime = build_wait_runtime(model=script.adapter, store=SQLiteKernelStore(clock=lambda: timer[0]))
        runtime.session.continuation_binding = _capture_creation_binding()
        assert not await runtime.session.resume_wait(candidate=candidate)
        state = await runtime.store.load(owner=OWNER, session_id='wait-control')
        assert state.metadata['read_wait']['state'] == 'terminal'
        assert len(script.requests) == 0
    asyncio.run(run())


def test_claim_exclusion_running_never_replays_after_lease():
    from contextlib import suppress

    from app.agent.kernel.model import ModelEvent, ModelEventType
    async def run():
        timer = [100]
        _, candidate = await parked_runtime(timer)
        timer[0] = 110
        entered, release = asyncio.Event(), asyncio.Event()
        class Held:
            async def stream(self, request, *, cancellation):
                entered.set()
                await release.wait()
                yield ModelEvent(ModelEventType.TEXT_DELTA, text='not a read')
                yield ModelEvent(ModelEventType.FINISH, finish_reason='stop')
        runtime = build_wait_runtime(model=Held(), store=SQLiteKernelStore(clock=lambda: timer[0]))
        runtime.session.continuation_binding = _capture_creation_binding()
        worker = asyncio.create_task(runtime.session.resume_wait(candidate=candidate))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            cold = SQLiteKernelStore(clock=lambda: timer[0])
            timer[0] = 151
            assert await cold.claim_continuation(candidate, runtime.session.continuation_binding) is None
            assert await cold.due_continuations() == []
            state = await cold.load(owner=OWNER, session_id='wait-control')
            assert state.metadata['read_wait']['state'] == 'running'
            assert state.metadata['read_wait']['budget']['model_spent'] == 4
        finally:
            release.set()
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(worker, 5)
    asyncio.run(run())


def test_repark_segments_and_task_budgets_survive_cold_runtimes():
    async def run():
        timer = [100]
        runtime, candidate = await parked_runtime(timer)
        for index in range(3):
            timer[0] += 10
            script = WaitScript(True)
            script.prefix = f'resume-{index}'
            fresh = build_wait_runtime(model=script.adapter, store=SQLiteKernelStore(clock=lambda: timer[0]))
            fresh.session.continuation_binding = _capture_creation_binding()
            class Again:
                async def acquire(self, **kwargs):
                    raise ToolRateLimitError(RateLimitDecision(False, 6, 1, 0, retry_after_seconds=10,
                                                              retry_at_epoch=timer[0]+10, reason='quota'))
            fresh.session.pipeline.rate_limiter = Again()
            assert await fresh.session.resume_wait(candidate=candidate)
            state = await fresh.store.load(owner=OWNER, session_id='wait-control')
            candidate = state.metadata['read_wait']
            assert candidate['budget']['wait_segments'] == min(3, index + 2)
            assert candidate['budget']['tool_spent'] == index + 4
            assert candidate['expires_at'] == 400
            assert candidate['state'] == ('terminal' if index == 2 else 'pending')
        assert candidate['budget']['wait_seconds_spent'] == 30
    asyncio.run(run())


@pytest.mark.parametrize('change', ['foreground', 'fence'])
def test_new_turn_or_durable_epoch_revokes_old_wait(change):
    from app.agent.feature_gate import begin_runtime_change, read_runtime_fence
    async def run():
        timer = [100]
        runtime, candidate = await parked_runtime(timer)
        timer[0] = 110
        script = WaitScript(True)
        fresh = build_wait_runtime(model=script.adapter, store=SQLiteKernelStore(clock=lambda: timer[0]))
        fresh.session.continuation_binding = _capture_creation_binding()
        if change == 'foreground':
            script.steps = ['finish']
            events = [e async for e in fresh.session.run(AgentInput(message='继续', owner=OWNER,
                          session_id='wait-control', request_id='new-request'))]
            assert events[-1].payload.get('status') == 'success'
            after = await fresh.store.load(owner=OWNER, session_id='wait-control')
            assert after.metadata['read_wait']['budget']['model_spent'] == 3
            assert after.metadata['read_wait']['budget']['tool_spent'] == 3
            count = len(script.requests)
            assert not await fresh.session.resume_wait(candidate=candidate)
            assert len(script.requests) == count
        else:
            with db.get_conn() as conn:
                conn.execute('BEGIN IMMEDIATE')
                begin_runtime_change(conn, read_runtime_fence(conn))
            try:
                await fresh.session.resume_wait(candidate=candidate)
            except Exception:
                pass
            assert len(script.requests) == 0
    asyncio.run(run())


def test_scheduler_fault_branch_does_not_block_jobs_or_receipts():
    from app.modules.agent_jobs_scheduler import AgentJobsScheduler
    seen = []
    scheduler = AgentJobsScheduler()
    async def receipt(*args, **kwargs):
        seen.append('receipt')
        raise RuntimeError('fixture receipt error')
    def job():
        seen.append('job')
        return 0
    def read():
        seen.append('read')
        scheduler._stop_event.set()
        scheduler._wake_event.set()
        raise RuntimeError('fixture read error')
    with patch('app.agent.effect_completion.poll_effect_receipts', side_effect=receipt), patch.object(scheduler, 'run_once', side_effect=job), patch.object(scheduler, '_run_due_continuation_once', side_effect=read), patch.object(scheduler._wake_event, 'wait', side_effect=lambda **kw: scheduler._stop_event.set()), patch('app.modules.agent_jobs_scheduler.threading.Thread'):
        scheduler._loop()
        scheduler._stop_event.clear()
        scheduler._continuation_loop()
    assert seen == ['receipt', 'job', 'read']
    assert scheduler._thread is None


def test_cancel_during_journal_io_cannot_publish_late_tool_event():
    import threading
    from contextlib import suppress
    async def run():
        timer = [100]
        _, candidate = await parked_runtime(timer)
        timer[0] = 110
        store = SQLiteKernelStore(clock=lambda: timer[0])
        script = WaitScript(True)
        runtime = build_wait_runtime(model=script.adapter, store=store)
        runtime.session.continuation_binding = _capture_creation_binding()
        async def read(args, context):
            return {'ok': True, 'summary': 'read-before-cancel'}
        tool = runtime.session.catalog.get('fixture.lookup')
        runtime.session.catalog._tools['fixture.lookup'] = replace(tool, read=read)
        entered, release = threading.Event(), threading.Event()
        original = store._append_event_sync
        def delayed(event, owner, *args):
            if event.type.value == 'tool.completed' and event.payload.get('tool') == 'fixture.lookup':
                entered.set()
                assert release.wait(5), 'journal release timeout'
            return original(event, owner, *args)
        with patch.object(store, '_append_event_sync', side_effect=delayed):
            worker = asyncio.create_task(runtime.session.resume_wait(candidate=candidate))
            try:
                assert await asyncio.to_thread(entered.wait, 5), 'journal entry timeout'
                cold = build_wait_runtime(model=WaitScript(True).adapter, store=SQLiteKernelStore(clock=lambda: timer[0]))
                assert await cold.session.cancel(owner=OWNER, session_id='wait-control', request_id='source')
                release.set()
                await asyncio.wait_for(worker, 5)
                events = await cold.store.list_events(owner=OWNER, session_id='wait-control')
                assert not any(e['type'] == 'tool.completed' and e['payload'].get('tool') == 'fixture.lookup' for e in events)
                assert len(script.requests) == 0
            finally:
                release.set()
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(worker, 5)
    asyncio.run(run())


async def parked_runtime(timer, session_id='wait-control'):
    db.init_db()
    initialize_runtime_fence()
    script = WaitScript()
    runtime = build_wait_runtime(model=script.adapter, store=SQLiteKernelStore(clock=lambda: timer[0]))
    runtime.session.continuation_binding = _capture_creation_binding()
    runtime.session.pipeline.rate_limiter = Quota()
    events = [e async for e in runtime.session.run(AgentInput(message=GOAL, owner=OWNER,
                      session_id=session_id, request_id='source'))]
    assert events[-1].payload.get('status') == 'waiting'
    state = await runtime.store.load(owner=OWNER, session_id=session_id)
    return runtime, state.metadata['read_wait']


@pytest.mark.parametrize('phase', ['acquire', 'handler'])
def test_cold_cancel_blocks_handler_start_and_late_reference(phase):
    from contextlib import suppress

    from app.agent.kernel.projection import ReferenceValue, ToolOutcome
    async def run():
        timer = [100]
        runtime, candidate = await parked_runtime(timer)
        timer[0] = 110
        script = WaitScript(True)
        fresh = build_wait_runtime(model=script.adapter, store=SQLiteKernelStore(clock=lambda: timer[0]))
        fresh.session.continuation_binding = _capture_creation_binding()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        class Limiter:
            async def acquire(self, **kwargs):
                if phase == 'acquire':
                    entered.set()
                    await release.wait()
        async def read(args, context):
            calls.append(args)
            if phase == 'handler':
                entered.set()
                await release.wait()
            return ToolOutcome(model_content='late private result', public_content={'ok': True},
                               refs=(ReferenceValue(kind='late_ref', value={'late': True}),))
        fresh.session.pipeline.rate_limiter = Limiter()
        tool = fresh.session.catalog.get('fixture.lookup')
        fresh.session.catalog._tools['fixture.lookup'] = replace(tool, read=read)
        worker = asyncio.create_task(fresh.session.resume_wait(candidate=candidate))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            cold = build_wait_runtime(model=WaitScript(True).adapter, store=SQLiteKernelStore(clock=lambda: timer[0]))
            assert await cold.session.cancel(owner=OWNER, session_id='wait-control', request_id='source')
            stopped = await cold.store.load(owner=OWNER, session_id='wait-control')
            release.set()
            await asyncio.wait_for(worker, 5)
            assert len(calls) == (0 if phase == 'acquire' else 1)
            with db.get_conn() as conn:
                assert conn.execute("SELECT count(*) FROM agent_kernel_refs WHERE kind='late_ref'").fetchone()[0] == 0
            assert (await cold.store.load(owner=OWNER, session_id='wait-control')) == stopped
            assert len(script.requests) == 0
        finally:
            release.set()
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(worker, 5)
    asyncio.run(run())
