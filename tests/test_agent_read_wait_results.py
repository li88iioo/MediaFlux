"""A resumed READ consumes real success/error TaskPatches without retrying."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app import database as db
from app.agent.errors import ToolPipelineError, ToolRateLimitError
from app.agent.feature_gate import initialize_runtime_fence
from app.agent.kernel.bootstrap import (
    _capture_creation_binding,
)
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.state_types import AgentInput
from app.agent.kernel.task_state import identity
from app.agent.rate_limit import RateLimitDecision
from tests.agent_task_native_helpers import task_call
from tests.test_agent_read_wait_lifecycle import build_wait_runtime
from tests.test_agent_read_wait_lifecycle import isolated_database as isolated_database
from tests.test_agent_task_first_slice import GOAL, OWNER, NativeScript, declaration


class ResultScript(NativeScript):
    def __init__(self, tool, *, resume=False, retry=False):
        self.tool = tool
        super().__init__('tmdb')
        self.steps = (['read', 'read', 'finish'] if retry else ['read', 'finish']) if resume else ['discover', 'create', 'read']
        self.prefix = 'resumed' if resume else 'initial'

    async def respond(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        step = self.steps.pop(0)
        if step == 'finish':
            message = {'content': '已取得本次读取结果；其余任务未宣称完成。'}
        else:
            name, args = {
                'discover': ('agent__run_code', {'language': 'shell', 'code': 'mf --help'}),
                'create': task_call(declaration(), wire=True),
                'read': (self.tool.replace('.', '__'), {'component': 'tmdb'} if self.tool == 'fixture.configuration' else {'query': 'quota fixture'}),
            }[step]
            message = {'content': '', 'tool_calls': [{'id': f'{self.prefix}-{len(self.requests)}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}]}
        return httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': 'stop' if step == 'finish' else 'tool_calls'}]})


def runtime_for(script, clock):
    runtime = build_wait_runtime(model=script.adapter, store=SQLiteKernelStore(clock=lambda: clock[0]))
    from app.agent.kernel.capabilities import KernelToolSpec, ToolEffect
    from app.agent.kernel.task_state import configuration_observations
    from app.modules.diagnostics import component
    runtime.session.catalog.register(KernelToolSpec(name="fixture.configuration", domain="fixture", effect=ToolEffect.READ,
        description="配置观察回执的等待/恢复夹具",
        input_schema={"type": "object", "required": ["component"], "additionalProperties": False,
                      "properties": {"component": {"type": "string", "enum": ["tmdb"]}}},
        read=lambda arguments, context: component(**arguments), observe=configuration_observations))
    runtime.session.continuation_binding = _capture_creation_binding()
    runtime.session.model_tools = (runtime.session.model_tools or frozenset()) | {"fixture.configuration"}
    return runtime


async def park(clock, tool):
    db.init_db()
    initialize_runtime_fence()
    runtime = runtime_for(ResultScript(tool), clock)

    class Quota:
        async def acquire(self, *, tool_name, **kwargs):
            if tool_name == tool:
                raise ToolRateLimitError(RateLimitDecision(False, 6, 1, 0, retry_after_seconds=10, retry_at_epoch=110, reason='quota'))

    runtime.session.pipeline.rate_limiter = Quota()
    events = [e async for e in runtime.session.run(AgentInput(message=GOAL, owner=OWNER, session_id='results', request_id='source'))]
    assert events[-1].payload.get('status') == 'waiting'
    state = await runtime.store.load(owner=OWNER, session_id='results')
    return state.metadata['read_wait'], state


@pytest.mark.parametrize('mode', ['observation', 'plain', 'handler_failure', 'observation_failure', 'invalid_observation', 'handler_rate_limited'])
def test_resumed_read_result_branches(mode, tmp_path):
    async def run():
        clock = [100]
        observing = mode in {'observation', 'observation_failure', 'invalid_observation'}
        success = mode in {'observation', 'plain'}
        tool_name = 'fixture.configuration' if observing else 'fixture.lookup'
        candidate, parked = await park(clock, tool_name)
        clock[0] = 110
        script = ResultScript(tool_name, resume=True)
        script.steps = ["finish"]
        runtime = runtime_for(script, clock)
        tool = runtime.session.catalog.get(tool_name)
        calls = []
        code = 'invalid_observation' if mode == 'invalid_observation' else 'rate_limited' if mode == 'handler_rate_limited' else 'tool_execution_failed'

        async def read(args, context):
            calls.append(dict(args))
            if mode == 'observation':
                assert tool.read is not None
                return await asyncio.to_thread(tool.read, args, context)
            if mode == 'invalid_observation':
                return {'ok': True, 'summary': 'not verified configuration evidence'}
            if not success and len(calls) == 1:
                raise ToolPipelineError('offline started READ failed', code=code)
            return {'ok': True, 'summary': 'offline READ result'}

        runtime.session.catalog._tools[tool_name] = replace(tool, read=read)
        assert await runtime.session.resume_wait(candidate=candidate)
        cold = await SQLiteKernelStore(clock=lambda: clock[0]).load(owner=OWNER, session_id='results')
        with db.get_conn() as conn:
            actual = Path(conn.execute('PRAGMA database_list').fetchall()[0][2]).resolve()
        assert actual == db.resolve_db_path().resolve() and actual.is_relative_to(tmp_path)
        wait, task = cold.metadata['read_wait'], cold.metadata['task']
        assert len(calls) == 1, 'a started READ must not authorize a second automatic handler'
        assert wait['state'] == 'terminal'
        assert wait['budget']['tool_spent'] == 4
        assert wait['budget']['model_spent'] == (4 if success else 3)
        assert wait['budget']['wait_seconds_spent'] == 10
        assert wait['expected_task'] == identity(task)
        assert task['revision'] == (2 if observing else 1)
        assert task['status'] != 'completed'
        rows = {r.get('tool_call_id'): r for r in cold.conversation if r.get('role') == 'tool'}
        old = next(r for r in parked.conversation if r.get('tool_call_id') == 'initial-3')
        assert rows['initial-3'] == old
        resumed = [row for key, row in rows.items() if key not in {r.get('tool_call_id') for r in parked.conversation}]
        assert len(resumed) == 1
        result = resumed[0]
        events = await runtime.store.list_events(owner=OWNER, session_id='results')
        if success:
            assert len(script.requests) == 1
            assert wait['terminal_reason'] == 'completed'
            assert result['content'] in [r.get('content') for r in script.requests[0]['messages']]
            if observing:
                assert any(f['call_id'] == result['tool_call_id'] for f in task['verified_facts'])
        else:
            assert len(script.requests) == 0
            assert wait['terminal_reason'] == code
            assert code in result['content']
            assert not task['verified_facts']
            assert events[-1]['type'] == 'turn.failed'
            assert events[-1]['payload']['code'] == code
            assert events[-1]['payload']['turn_status'] == 'failed'
            assert events[-1]['payload']['task']['status'] != 'completed'
            if mode != 'invalid_observation':
                assert 'offline started READ failed' in result['content']
            assert not await runtime.session.resume_wait(candidate=candidate)
            assert len(script.requests) == 0
    asyncio.run(run())


@pytest.mark.parametrize('tool_name', ['fixture.lookup', 'fixture.configuration'])
def test_foreground_error_still_allows_model_repair(tool_name):
    async def run():
        clock = [100]
        await park(clock, tool_name)
        script = ResultScript(tool_name, resume=True, retry=True)
        script.steps.insert(0, 'discover')
        runtime = runtime_for(script, clock)
        tool = runtime.session.catalog.get(tool_name)
        calls = []

        async def read(args, context):
            calls.append(dict(args))
            if len(calls) == 1:
                raise ToolPipelineError('repairable foreground error', code='tool_execution_failed')
            if tool.observe is not None:
                assert tool.read is not None
                return await asyncio.to_thread(tool.read, args, context)
            return {'ok': True, 'summary': 'foreground repaired'}

        runtime.session.catalog._tools[tool_name] = replace(tool, read=read)
        events = [e async for e in runtime.session.run(AgentInput(message=GOAL, owner=OWNER, session_id='results', request_id='foreground'))]
        assert len(calls) == 2
        assert len(script.requests) == 4
        assert events[-1].payload['status'] == 'success'
        assert 'tool_execution_failed' in json.dumps(script.requests[2])
    asyncio.run(run())


def test_interrupted_synthesis_does_not_repeat_completed_resumed_read():
    async def run():
        clock = [100]
        candidate, parked = await park(clock, 'fixture.lookup')
        clock[0] = 110

        class Interrupted(ResultScript):
            async def respond(self, request):
                if not self.requests:
                    self.requests.append(json.loads(request.content))
                    # 真正SSE只交付正文而没有终态；native adapter抛UncommittableModelTurn。
                    partial = {'choices': [{'delta': {'content': 'READ续行截断残稿'}}]}
                    return httpx.Response(200, content=('data: ' + json.dumps(partial) + '\n\n').encode(),
                                          headers={'content-type': 'text/event-stream'})
                response = await super().respond(request)
                body = response.json()
                return httpx.Response(200, json=body)

        script = Interrupted('fixture.lookup', resume=True)
        script.steps = ['finish']
        runtime = runtime_for(script, clock)
        calls = []

        async def read(arguments, context):
            calls.append(dict(arguments))
            return {'ok': True, 'summary': '原范围核验完成'}

        tool = runtime.session.catalog.get('fixture.lookup')
        runtime.session.catalog._tools[tool.name] = replace(tool, read=read)
        assert await runtime.session.resume_wait(candidate=candidate)
        assert script.requests[0]['tools'] == script.requests[1]['tools']
        assert 'READ续行截断残稿' not in json.dumps(script.requests[1]['messages'], ensure_ascii=False)
        cold = await SQLiteKernelStore(clock=lambda: clock[0]).load(owner=OWNER, session_id='results')
        wait = cold.metadata['read_wait']
        assert wait['state'] == 'terminal'
        assert wait['terminal_reason'] == 'completed'
        assert calls == [{'query': 'quota fixture'}]
        assert wait['budget']['model_spent'] == 5
        assert wait['budget']['tool_spent'] == 4
        assert len(script.requests) == 2
        old = next(r for r in parked.conversation if r.get('tool_call_id') == 'initial-3')
        assert old in cold.conversation
        assert 'READ续行截断残稿' not in json.dumps(cold.conversation, ensure_ascii=False)
        before = len(script.requests)
        assert not await runtime.session.resume_wait(candidate=candidate)
        assert len(script.requests) == before
        assert calls == [{'query': 'quota fixture'}]

    asyncio.run(run())
