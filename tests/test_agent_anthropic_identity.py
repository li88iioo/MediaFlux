"""Anthropic identities are native, not synthesized or normalized tool keys."""
from typing import Any
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from app import database as db
from app.agent.kernel.model import ModelEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.provider_model import ModelProviderError
from app.agent.kernel.session import SessionLimits
from app.agent.kernel.state_types import AgentInput
from app.agent.public_view import public_conversation_messages
from tests.test_agent_tool_argument_ingress import Wire, observe
from tests.test_agent_tool_argument_ingress import offline as offline
from tests.test_agent_tool_argument_ingress import store as store
from tests.test_agent_tool_argument_repair_replay import make_session

P = 'anthropic_messages'
PRIVATE = {'type': 'thinking', 'thinking': ' synthetic-private-STUDY\n',
           'signature': 'fixture-only-NOT-A-REAL-SIGNATURE'}
OPAQUE = {'type': 'redacted_thinking', 'data': 'synthetic-opaque-STUDY'}
MISSING = object()
FINAL = (json.dumps({'content': [{'type': 'text', 'text': '已核验'}],
                     'stop_reason': 'end_turn'}).encode(), 'application/json')


def block(cid='call-original', name='lookup', value=None) -> dict[str, Any]:
    result = {'type': 'tool_use', 'input': {'query': 42} if value is None else value}
    if cid is not MISSING:
        result['id'] = cid
    if name is not MISSING:
        result['name'] = name
    return result


def payload(mode, blocks):
    content = [PRIVATE, OPAQUE, *blocks]
    if mode == 'json':
        return json.dumps({'content': content, 'stop_reason': 'tool_use'}).encode(), 'application/json'
    events: list[dict[str, Any]] = [{'type': 'message_start', 'message': {'role': 'assistant', 'content': []}}]
    for index, item in enumerate(content):
        start = {**item, 'input': {}} if item['type'] == 'tool_use' else item
        events.append({'type': 'content_block_start', 'index': index, 'content_block': start})
        if item['type'] == 'tool_use':
            events.append({'type': 'content_block_delta', 'index': index, 'delta': {
                'type': 'input_json_delta', 'partial_json': json.dumps(item['input'])}})
        events.append({'type': 'content_block_stop', 'index': index})
    events += [{'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}}, {'type': 'message_stop'}]
    # JSON escapes permit invalid UTF-8 scalars to reach the real decoder.
    return ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode(), 'text/event-stream'


@pytest.mark.parametrize('mode', ['json', 'sse'])
@pytest.mark.parametrize('field', ['id', 'name'])
@pytest.mark.parametrize('value', [MISSING, None, '', '   ', 123, '\ud800'],
                         ids=['missing', 'null', 'empty', 'blank', 'number', 'surrogate'])
@pytest.mark.parametrize('bad_first', [True, False])
def test_invalid_original_identity_rejects_complete_batch(mode, field, value, bad_first):
    bad = block(cid=value) if field == 'id' else block(name=value)
    good = block('call-good', value={'query': 'safe'})
    seen, error = asyncio.run(observe(P, payload(mode, [bad, good] if bad_first else [good, bad])))
    assert isinstance(error, ModelProviderError), 'raw native identities must fail at provider boundary'
    assert not any(event.type is ModelEventType.FINISH for event in seen)
    assert [event.tool_call.call_id for event in seen if event.tool_call] == (
        ['call-good'] if mode == 'sse' and not bad_first else [])
    # Invalid private identity values must not leak through exception chains.
    pending: list[BaseException | None] = [error]
    visited = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in visited:
            continue
        visited.add(id(current))
        assert 'STUDY' not in repr(current.args) and '\\ud800' not in repr(current.args)
        pending.extend((current.__cause__, current.__context__))


@pytest.mark.parametrize('mode', ['json', 'sse'])
def test_duplicate_native_ids_with_distinct_arguments_never_finish(mode):
    seen, error = asyncio.run(observe(P, payload(mode, [
        block('same', value={'query': 'first'}), block('same', value={'query': 'different'})])))
    assert isinstance(error, ModelProviderError)
    assert not any(event.type is ModelEventType.FINISH for event in seen)
    if mode == 'sse':
        actual_calls = []
        for event in seen:
            if event.tool_call:
                assert event.tool_call.arguments is not None
                actual_calls.append(dict(event.tool_call.arguments))
        assert actual_calls == [
            {'query': 'first'}, {'query': 'different'}]
    else:
        assert not any(event.tool_call for event in seen)


def test_repeated_sse_block_index_cannot_replace_original_identity():
    data, kind = payload('sse', [block('same', value={'query': 'first'}),
                                 block('same', value={'query': 'different'})])
    frames = [json.loads(line[6:]) for line in data.decode().splitlines() if line.startswith('data: ')]
    for frame in frames:
        if frame.get('index') == 3:
            frame['index'] = 2
    reply = (''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames).encode(), kind)
    seen, error = asyncio.run(observe(P, reply))
    assert isinstance(error, ModelProviderError)
    assert not any(event.type is ModelEventType.FINISH for event in seen)
    actual_calls = []
    for event in seen:
        if event.tool_call:
            assert event.tool_call.arguments is not None
            actual_calls.append(dict(event.tool_call.arguments))
    assert actual_calls == [{'query': 'first'}]


@pytest.mark.parametrize('mode', ['json', 'sse'])
@pytest.mark.parametrize('cid,name', [('调用-原始', '查找'), ('  native-id  ', '  lookup  ')])
def test_valid_identity_and_private_layout_are_exact(mode, cid, name):
    seen, error = asyncio.run(observe(P, payload(mode, [block(cid, name)])))
    assert error is None
    call = next(event.tool_call for event in seen if event.tool_call)
    assert call.arguments is not None
    assert (call.call_id, call.name, dict(call.arguments)) == (cid, name, {'query': 42})
    assert seen[-1].type is ModelEventType.FINISH
    assert seen[-1].provider_state is not None
    assert json.loads(seen[-1].provider_state.layout_json) == [
        PRIVATE, OPAQUE, {'type': 'tool_ref', 'call_id': cid}]


@pytest.mark.parametrize('mode', ['json', 'sse'])
@pytest.mark.parametrize('kind', ['invalid-late', 'surrogate-name', 'duplicate'])
def test_real_session_buffers_rejected_batch_without_history_or_execution(mode, kind, store, tmp_path):
    async def run():
        good = block('first', value={'query': 'safe'})
        bad = (block(None) if kind == 'invalid-late' else block('second', '\ud800')
               if kind == 'surrogate-name' else block('first', value={'query': 'different'}))
        executed = []
        wire = Wire(P, [payload(mode, [good, bad])])
        events = [event async for event in make_session(wire.adapter, store, executed).run(
            AgentInput(message='只读核验', owner='owner', session_id='rejected'))]
        wire.verify()
        assert not executed
        assert not any(event.type.name == 'TOOL_STARTED' for event in events)
        assert events[-1].payload['code'] == 'model_provider_error'
        cold = await SQLiteKernelStore(secret_provider=lambda: 'offline-tool-argument-test').load(
            owner='owner', session_id='rejected')
        assert not any(row.get('tool_calls') or row.get('role') == 'tool' for row in cold.conversation)
        assert_database(tmp_path)
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['json', 'sse'])
def test_schema_error_cold_repair_preserves_native(mode, store, tmp_path):
    async def run():
        executed = []
        first = Wire(P, [payload(mode, [block()])])
        entered = asyncio.Event()
        class Boundary:
            count = 0
            async def stream(self, request, *, cancellation):
                self.count += 1
                if self.count == 2:
                    entered.set()
                    await asyncio.Event().wait()
                async for event in first.adapter.stream(request, cancellation=cancellation):
                    yield event
        session = make_session(Boundary(), store, executed)
        task = asyncio.create_task(collect(session.run(AgentInput(message='只读核验', owner='owner', session_id='cold'))))
        waiter = asyncio.create_task(entered.wait())
        try:
            await asyncio.wait((task, waiter), timeout=3, return_when=asyncio.FIRST_COMPLETED)
            assert entered.is_set(), 'schema-invalid native object must reach bounded model repair'
            assert await session.cancel(owner='owner', session_id='cold')
            events = await asyncio.wait_for(task, 3)
        finally:
            for pending in (task, waiter):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, waiter, return_exceptions=True)
        first.verify()
        assert not executed
        cold = SQLiteKernelStore(secret_provider=lambda: 'offline-tool-argument-test')
        state = await cold.load(owner='owner', session_id='cold')
        errors = [json.loads(row['content']) for row in state.conversation if row['role'] == 'tool']
        assert errors[0]['code'] == 'invalid_arguments'
        rows = [row for row in state.conversation if row.get('tool_calls')]
        assert rows[0]['tool_calls'][0]['arguments'] == {'query': 42}
        assert json.loads(rows[0]['provider_state']['layout_json']) == [
            PRIVATE, OPAQUE, {'type': 'tool_ref', 'call_id': 'call-original'}]
        assert 'STUDY' not in json.dumps(public_conversation_messages(state.conversation))
        assert 'STUDY' not in json.dumps([dict(event.payload) for event in events])
        nextwire = Wire(P, [payload(mode, [block('call-corrected', value={'query': 'example'})]), FINAL])
        after = await collect(make_session(nextwire.adapter, cold, executed).run(
            AgentInput(message='继续核验', owner='owner', session_id='cold')))
        nextwire.verify(2)
        assert executed == [{'query': 'example'}]
        assert after[-1].payload['status'] == 'success'
        history = nextwire.requests[0]['messages']
        blocks = [item for message in history if message['role'] == 'assistant'
                  for item in message['content'] if isinstance(item, dict)]
        assert PRIVATE in blocks and OPAQUE in blocks
        assert blocks[:3] == [PRIVATE, OPAQUE, block()]
        assert {'type': 'tool_use', 'id': 'call-original', 'name': 'lookup', 'input': {'query': 42}} in blocks
        results = [item for message in history for item in message['content']
                   if isinstance(item, dict) and item.get('type') == 'tool_result']
        assert any(item['tool_use_id'] == 'call-original' and json.loads(item['content'])['code'] == 'invalid_arguments'
                   for item in results)
        assert_database(tmp_path)
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['json', 'sse'])
def test_schema_errors_obey_existing_tool_budget(mode, store):
    async def run():
        executed = []
        wire = Wire(P, [payload(mode, [block(f'call-{index}')]) for index in range(3)])
        session = make_session(wire.adapter, store, executed,
                               SessionLimits(max_model_rounds=3, max_tool_calls=2))
        events = await collect(session.run(AgentInput(message='只读核验', owner='owner', session_id='budget')))
        wire.verify(3)
        assert not executed
        assert events[-1].type.name == 'TURN_FAILED'
        assert events[-1].payload['code'] == 'invalid_arguments'
        assert wire.requests[-1].get('tools')
        failures = [event.payload for event in events if event.type.name == 'TOOL_FAILED']
        assert [(event['call_id'], event['code']) for event in failures] == [
            ('call-0', 'invalid_arguments'), ('call-1', 'invalid_arguments'),
            ('call-2', 'tool_budget_exceeded'),
        ]
        cold = await SQLiteKernelStore(secret_provider=lambda: 'offline-tool-argument-test').load(
            owner='owner', session_id='budget')
        assert [call['call_id'] for row in cold.conversation for call in row.get('tool_calls', [])] == [
            'call-0', 'call-1', 'call-2',
        ]
        assert [row['tool_call_id'] for row in cold.conversation if row['role'] == 'tool'] == [
            'call-0', 'call-1', 'call-2',
        ]
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['json', 'sse'])
def test_unknown_tool_keeps_availability_boundary_without_schema_disclosure(mode, store):
    async def run():
        executed = []
        wire = Wire(P, [payload(mode, [block(name='not_loaded')]), FINAL])
        await collect(make_session(wire.adapter, store, executed).run(
            AgentInput(message='只读核验', owner='owner', session_id='unknown')))
        wire.verify(2)
        assert not executed
        results = [item for message in wire.requests[1]['messages'] for item in message['content']
                   if isinstance(item, dict) and item.get('type') == 'tool_result']
        error = json.loads(results[0]['content'])
        assert error['code'] == 'tool_not_available'
        assert 'expected_input_schema' not in error
    asyncio.run(run())


async def collect(iterator):
    return [event async for event in iterator]


def assert_database(tmp_path):
    assert db.resolve_db_path() == tmp_path / 'arguments.db'
    with sqlite3.connect(db.resolve_db_path()) as connection:
        assert Path(connection.execute('PRAGMA database_list').fetchone()[2]) == tmp_path / 'arguments.db'
