from typing import Any, TypedDict
import asyncio
import json
from copy import deepcopy

import pytest

from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.conversation_context import restore_messages
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelMessage, ModelRequest
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.provider_model import ModelProviderError, UncommittableModelTurn
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.state_types import AgentInput, CancellationToken
from app.agent.public_view import public_conversation_messages
from app.clients.openai_compatible import parse_native_tool_turn
from tests.test_agent_tool_argument_ingress import (
    Wire,
    encode,
    event_wire,
    frames,
    observe,
    wire,
)
from tests.test_agent_tool_argument_ingress import offline as offline
from tests.test_agent_tool_argument_ingress import store as store

RAW = '  {"query":"私有原串-SENTINEL", \n'
OPAQUE = ' opaque\nPRIVATE-SENTINEL  '
PROTOCOLS = ['chat_completions', 'responses']


def payload(protocol, mode, raw=RAW, cid='call-original', name='lookup'):
    data, kind = wire(protocol, mode, raw)
    if mode == 'json':
        value = json.loads(data)
        if protocol == 'chat_completions':
            message = value['choices'][0]['message']
            call = message['tool_calls'][0]
            call['id'] = cid
            call['function']['name'] = name
            message['reasoning_content'] = OPAQUE
        else:
            call = value['output'][0]
            call['call_id'] = cid
            call['name'] = name
            value['output'].insert(0, {'type':'reasoning','id':'rs-private','encrypted_content':OPAQUE})
        return encode(value).encode(), kind
    events = frames(protocol, raw, final=raw)
    for event in events:
        if isinstance(event, dict):
            if protocol == 'chat_completions':
                delta = event['choices'][0]['delta']
                delta['reasoning_content'] = OPAQUE if delta.get('tool_calls') else ''
                for call in delta.get('tool_calls', []):
                    call['id'] = cid
                    call['function']['name'] = name
            elif isinstance(event.get('item'), dict):
                event['item']['call_id'] = cid
                event['item']['name'] = name
    if protocol == 'responses':
        events.insert(-1, {'type':'response.output_item.done','output_index':1,'item':{'type':'reasoning','id':'rs-private','encrypted_content':OPAQUE}})
    return event_wire(events)


def final(protocol):
    value = ({'choices':[{'message':{'content':'已核验'},'finish_reason':'stop'}]} if protocol == 'chat_completions' else
             {'status':'completed','output_text':'已核验','output':[]})
    return encode(value).encode(), 'application/json'


def make_session(model, store, executed, limits=None):
    def handler(arguments, context):
        executed.append(dict(arguments))
        return {'ok':True,'status':'success','summary':'已核验','data':{}}
    catalog = ToolCatalog([KernelToolSpec(name='lookup', domain='library', description='只读核验', input_schema={'type':'object','properties':{'query':{'type':'string'}},'required':['query']}, effect=ToolEffect.READ, read=handler)])
    return AgentSession(model=model, catalog=catalog,  pipeline=ToolPipeline(catalog=catalog,state_store=store), state_store=store, **({'limits':limits} if limits else {}))


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('mode', ['json','sse'])
def test_real_cold_restore_next_wire_and_one_correction(protocol, mode, store):
    async def run():
        executed = []
        first = Wire(protocol, [payload(protocol, mode)])
        entered = asyncio.Event()
        class StopAtNextRequest:
            count = 0
            async def stream(self, request, *, cancellation):
                self.count += 1
                if self.count == 2:
                    entered.set()
                    await asyncio.Event().wait()
                async for event in first.adapter.stream(request, cancellation=cancellation):
                    yield event
        session = make_session(StopAtNextRequest(), store, executed)
        async def consume():
            return [e async for e in session.run(AgentInput(message='只读核验',owner='owner',session_id='cold'))]
        task = asyncio.create_task(consume())
        waiter = asyncio.create_task(entered.wait())
        done, _ = await asyncio.wait((task, waiter),timeout=3,return_when=asyncio.FIRST_COMPLETED)
        assert done, 'bounded model boundary handshake did not complete'
        if entered.is_set():
            assert await session.cancel(owner='owner',session_id='cold')
        else:
            waiter.cancel()
        before = await asyncio.wait_for(task,3)
        await asyncio.gather(waiter,return_exceptions=True)
        first.verify()
        cold = SQLiteKernelStore(secret_provider=lambda:'offline-tool-argument-test')
        state = await cold.load(owner='owner',session_id='cold')
        rows = [row for row in state.conversation if row.get('tool_calls')]
        assert rows, 'Malformed identifiable call must be durably repairable'
        call = rows[0]['tool_calls'][0]
        assert call['arguments'] is None and call['argument_error'] == 'invalid_json' and call['raw_arguments'] == RAW
        restored = restore_messages(state)
        assert any(m.tool_calls and m.tool_calls[0].raw_arguments == RAW for m in restored)
        errors = [row for row in state.conversation if row['role'] == 'tool']
        assert json.loads(errors[0]['content'])['code'] == 'invalid_arguments'
        assert 'expected_input_schema' in json.loads(errors[0]['content'])
        assert executed == []
        assert 'SENTINEL' not in json.dumps(public_conversation_messages(state.conversation),ensure_ascii=False)
        assert not any(e.type.name in ('TOOL_STARTED','EFFECT_PREVIEW_STARTED') for e in before)
        public = json.dumps([dict(e.payload) for e in before],ensure_ascii=False)
        assert 'SENTINEL' not in public
        next_wire = Wire(protocol, [payload(protocol, mode, '{"query":"example"}', 'call-corrected'), final(protocol)])
        after = [e async for e in make_session(next_wire.adapter, cold, executed).run(AgentInput(message='继续核验',owner='owner',session_id='cold'))]
        next_wire.verify(2)
        assert executed == [{'query':'example'}]
        history = next_wire.requests[0]['messages' if protocol == 'chat_completions' else 'input']
        if protocol == 'chat_completions':
            original = next(m for m in history if m.get('tool_calls'))
            assert original['tool_calls'][0]['id'] == 'call-original'
            assert original['tool_calls'][0]['function']['arguments'] == RAW
            assert original['reasoning_content'] == OPAQUE
            assert any(m.get('tool_call_id') == 'call-original' for m in history)
        else:
            original = next(m for m in history if m.get('call_id') == 'call-original' and m['type'] == 'function_call')
            assert original['arguments'] == RAW and original['id'] == 'item-0'
            assert any(m.get('encrypted_content') == OPAQUE for m in history)
            assert any(m.get('call_id') == 'call-original' and m['type'] == 'function_call_output' for m in history)
        assert 'SENTINEL' not in json.dumps([dict(e.payload) for e in after],ensure_ascii=False)
    asyncio.run(run())


def mixed_payload(protocol, bad_first, spaced):
    good_id = '  good-native  ' if spaced else 'good-native'
    items = [('bad', RAW), (good_id, '{"query":"example"}')]
    if not bad_first:
        items.reverse()
    if protocol == 'chat_completions':
        calls = [{'index':i, 'id':cid, 'function':{'name':'lookup', 'arguments':args}}
                 for i, (cid, args) in enumerate(items)]
        events = [{'choices':[{'delta':{'tool_calls':calls, 'reasoning_content':OPAQUE},
                               'finish_reason':'tool_calls'}]}, '[DONE]']
    else:
        events = []
        for i, (cid, args) in enumerate(items):
            item = {'type':'function_call', 'id':f'item-{i}', 'call_id':cid,
                    'name':'lookup', 'arguments':args}
            events.extend([{'type':'response.output_item.added', 'output_index':i, 'item':item},
                           {'type':'response.output_item.done', 'output_index':i, 'item':item}])
        events.extend([{'type':'response.output_item.done', 'output_index':2,
                        'item':{'type':'reasoning','id':'private-mixed','encrypted_content':OPAQUE}},
                       {'type':'response.completed','response':{'status':'completed'}}])
    return event_wire(events), items


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('bad_first', [True, False])
@pytest.mark.parametrize('spaced', [True, False])
def test_mixed_sse_cold_next_wire_exact_or_safe_rejection(protocol, bad_first, spaced, store):
    async def run():
        response, items = mixed_payload(protocol, bad_first, spaced)
        observed, error = await observe(protocol, response)
        if protocol == 'responses' and spaced:
            assert isinstance(error, ModelProviderError)
            assert not any(e.type is ModelEventType.FINISH for e in observed)
        else:
            assert error is None
            assert observed[-1].type is ModelEventType.FINISH
        transport = Wire(protocol, [response, final(protocol)])
        executed = []
        events = [e async for e in make_session(transport.adapter, store, executed).run(
            AgentInput(message='只读核验', owner='owner', session_id='mixed'))]
        cold = SQLiteKernelStore(secret_provider=lambda:'offline-tool-argument-test')
        state = await cold.load(owner='owner', session_id='mixed')
        # Responses may already have emitted a trimmed successful call. Reject
        # the entire turn before FINISH rather than mutate that event's identity.
        if protocol == 'responses' and spaced:
            transport.verify(1)
            assert executed == []
            assert not any(row.get('tool_calls') or row.get('role') == 'tool' for row in state.conversation)
            assert not any(e.type.name == 'TOOL_STARTED' for e in events)
            return
        transport.verify(2)
        assert executed == [{'query':'example'}]
        stored = [call for row in state.conversation for call in row.get('tool_calls', [])]
        assert sorted(c['call_id'] for c in stored) == sorted(cid for cid, _ in items)
        assert [c['name'] for c in stored] == ['lookup', 'lookup']
        assert next(c for c in stored if c['call_id'] == 'bad')['raw_arguments'] == RAW
        replay = Wire(protocol, [final(protocol)])
        _ = [e async for e in make_session(replay.adapter, cold, executed).run(
            AgentInput(message='继续核验', owner='owner', session_id='mixed'))]
        replay.verify(1)
        assert executed == [{'query':'example'}]
        for body in [transport.requests[1], replay.requests[0]]:
            history = body['messages' if protocol == 'chat_completions' else 'input']
            if protocol == 'chat_completions':
                assistant = next(row for row in history if row.get('tool_calls'))
                calls = assistant['tool_calls']
                assert [c['id'] for c in calls] == [cid for cid, _ in items]
                assert [c['function']['name'] for c in calls] == ['lookup', 'lookup']
                assert next(c for c in calls if c['id'] == 'bad')['function']['arguments'] == RAW
                assert assistant['reasoning_content'] == OPAQUE
                assert [row['tool_call_id'] for row in history if row.get('role') == 'tool'] == [cid for cid, _ in items]
            else:
                calls = [row for row in history if row.get('type') == 'function_call']
                assert [(c['call_id'], c['id'], c['name']) for c in calls] == [(cid, f'item-{i}', 'lookup') for i, (cid, _) in enumerate(items)]
                assert next(c for c in calls if c['call_id'] == 'bad')['arguments'] == RAW
                assert any(row.get('encrypted_content') == OPAQUE for row in history)
                assert sorted(row['call_id'] for row in history if row.get('type') == 'function_call_output') == sorted(cid for cid, _ in items)
    asyncio.run(run())


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('bad_first', [True, False])
@pytest.mark.parametrize('field,value', [('name', 123), ('call_id', 123), ('call_id', '\ud800')])
def test_mixed_invalid_success_identity_rejects_whole_turn(protocol, bad_first, field, value, store):
    async def run():
        response, _ = mixed_payload(protocol, bad_first, False)
        frames = [json.loads(line[6:]) if line[6:] != '[DONE]' else '[DONE]'
                  for line in response[0].decode().splitlines() if line.startswith('data: ')]
        for frame in frames:
            if not isinstance(frame, dict):
                continue
            if protocol == 'chat_completions':
                for call in frame['choices'][0]['delta']['tool_calls']:
                    if call['id'] == 'good-native':
                        if field == 'name':
                            call['function']['name'] = value
                        else:
                            call['id'] = value
            elif frame.get('item', {}).get('call_id') == 'good-native':
                frame['item'][field] = value
        # ensure_ascii transports an invalid scalar to the actual JSON decoder.
        response = (''.join('data: ' + (frame if isinstance(frame, str) else json.dumps(frame)) + '\n\n' for frame in frames).encode(), 'text/event-stream')
        observed, error = await observe(protocol, response)
        assert isinstance(error, ModelProviderError)
        assert not any(e.type is ModelEventType.FINISH for e in observed)
        executed = []
        transport = Wire(protocol, [response])
        events = [e async for e in make_session(transport.adapter, store, executed).run(
            AgentInput(message='只读核验', owner='owner', session_id='invalid-mixed'))]
        transport.verify(1)
        assert not executed
        assert not any(e.type.name == 'TOOL_STARTED' for e in events)
        state = await SQLiteKernelStore(secret_provider=lambda:'offline-tool-argument-test').load(owner='owner', session_id='invalid-mixed')
        assert not any(row.get('tool_calls') or row.get('role') == 'tool' for row in state.conversation)
    asyncio.run(run())


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('mode', ['json','sse'])
@pytest.mark.parametrize('cid', [None, '', '   '])
def test_missing_identity_stays_provider_error(protocol, mode, cid):
    events, error = asyncio.run(observe(protocol, payload(protocol,mode,cid=cid)))
    assert error is not None
    assert not any(e.type is ModelEventType.FINISH for e in events)
    pending: list[BaseException | None] = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        assert 'SENTINEL' not in repr(current.args)
        pending.extend((current.__cause__, current.__context__))


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_strict_shared_parser_unchanged(protocol):
    data, _ = payload(protocol,'json')
    with pytest.raises(ValueError):
        parse_native_tool_turn(json.loads(data), protocol)


@pytest.mark.parametrize('raw', [' ' * 32768 + '{', '中' * 11000 + '{', '\ud800{'])
def test_invalid_raw_utf8_and_capacity_fail_closed(raw):
    # ASCII escaping permits the invalid scalar to reach decoded JSON validation.
    value = {'choices':[{'message':{'tool_calls':[{'type':'function','id':'real','function':{'name':'lookup','arguments':raw}}]},'finish_reason':'tool_calls'}]}
    events, error = asyncio.run(observe('chat_completions',(json.dumps(value).encode(),'application/json')))
    assert error is not None
    assert not any(e.type is ModelEventType.FINISH for e in events)


def test_anthropic_malformed_signed_input_stays_rejected():
    events = frames('anthropic_messages', {}, [RAW])
    events[:0] = [{'type':'content_block_start','index':2,'content_block':{'type':'thinking','thinking':OPAQUE,'signature':'signed-opaque'}}, {'type':'content_block_stop','index':2}]
    seen,error = asyncio.run(observe('anthropic_messages',event_wire(events)))
    assert error is not None and not any(e.type is ModelEventType.FINISH for e in seen)


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('mode', ['json', 'sse'])
def test_exact_opaque_identity_is_not_trimmed(protocol, mode):
    events, error = asyncio.run(observe(protocol, payload(protocol, mode, cid='  native-id  ')))
    assert error is None
    call = next(e.tool_call for e in events if e.tool_call)
    assert call.call_id == '  native-id  ' and call.raw_arguments == RAW
    restored = ModelMessage.from_dict(ModelMessage(role='assistant', tool_calls=(call,)).to_dict())
    assert restored.tool_calls[0] == call


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_duplicate_native_identity_and_invalid_private_reject(protocol):
    data, kind = payload(protocol, 'json')
    value = json.loads(data)
    if protocol == 'chat_completions':
        calls = value['choices'][0]['message']['tool_calls']
        calls.append(dict(calls[0]))
    else:
        value['output'].append(dict(value['output'][1]))
    events, error = asyncio.run(observe(protocol, (encode(value).encode(), kind)))
    assert error is not None and not any(e.type is ModelEventType.FINISH for e in events)
    value = json.loads(data)
    if protocol == 'chat_completions':
        value['choices'][0]['message']['reasoning_content'] = {'bad':OPAQUE}
    else:
        value['output'][0]['encrypted_content'] = {'bad':OPAQUE}
    events, error = asyncio.run(observe(protocol, (encode(value).encode(), kind)))
    assert error is not None and not any(e.type is ModelEventType.FINISH for e in events)


@pytest.mark.parametrize('mode', ['json', 'sse'])
@pytest.mark.parametrize('reasoning', [None, ''])
def test_chat_nullable_reasoning_does_not_block_argument_repair(mode, reasoning):
    if mode == 'json':
        data, kind = payload('chat_completions', mode)
        value = json.loads(data)
        value['choices'][0]['message']['reasoning_content'] = reasoning
        response = encode(value).encode(), kind
    else:
        events = frames('chat_completions', RAW, final=RAW)
        for event in events:
            if isinstance(event, dict):
                event['choices'][0]['delta']['reasoning_content'] = reasoning
        response = event_wire(events)
    events, error = asyncio.run(observe('chat_completions', response))
    assert error is None
    finish = next(event for event in events if event.type is ModelEventType.FINISH)
    assert finish.provider_state is not None
    assert finish.provider_state.layout() == ([] if reasoning is None else [{'type': 'reasoning_content', 'text': ''}])
    call = next(event.tool_call for event in events if event.type is ModelEventType.TOOL_CALL_COMPLETED)
    assert call is not None and call.argument_error == 'invalid_json'


def test_unknown_tool_precedes_syntax_repair(store):
    async def run():
        executed=[]
        transport=Wire('chat_completions',[payload('chat_completions','json',name='not_loaded'), final('chat_completions')])
        events=[e async for e in make_session(transport.adapter,store,executed).run(AgentInput(message='只读核验',owner='owner',session_id='unknown'))]
        transport.verify(2)
        assert executed == []
        feedback=[m for m in transport.requests[1]['messages'] if m.get('role') == 'tool']
        error=json.loads(feedback[0]['content'])
        assert error['code'] == 'tool_not_available' and 'expected_input_schema' not in error
        assert not any(e.type.name == 'TOOL_STARTED' for e in events)
    asyncio.run(run())


def test_actual_raw_storage_capacity_rejects_whole_batch(store):
    async def run():
        executed=[]
        raw='{' + 'x' * 32000
        data,kind=payload('chat_completions','json',raw)
        value=json.loads(data)
        item=value['choices'][0]['message']['tool_calls'][0]
        value['choices'][0]['message']['tool_calls']=[{**item,'id':f'large-{i}'} for i in range(6)]
        transport=Wire('chat_completions',[(encode(value).encode(),kind)])
        events=[e async for e in make_session(transport.adapter,store,executed).run(AgentInput(message='只读核验',owner='owner',session_id='capacity'))]
        transport.verify()
        assert not executed and not any(e.type.name == 'TOOL_STARTED' for e in events)
        assert events[-1].payload['code'] == 'history_capacity_exceeded'
        state=await SQLiteKernelStore(secret_provider=lambda:'offline-tool-argument-test').load(owner='owner',session_id='capacity')
        assert not any(row.get('tool_calls') for row in state.conversation)
    asyncio.run(run())


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_malformed_native_binding_cannot_be_dropped(protocol):
    async def run():
        transport=Wire(protocol,[payload(protocol,'json')])
        events=[e async for e in transport.adapter.stream(ModelRequest(system_prompt='',messages=(),tools=()),cancellation=CancellationToken())]
        transport.verify()
        call=next(e.tool_call for e in events if e.tool_call)
        assert call.arguments is None
        message=ModelMessage(role='assistant',tool_calls=(call,),provider_state=events[-1].provider_state)
        replay=Wire(protocol,[])
        with pytest.raises(ModelProviderError):
            _=[e async for e in replay.adapter.stream(ModelRequest(system_prompt='',messages=(message,),tools=(),model='different-model'),cancellation=CancellationToken())]
        assert replay.requests == [] and all(c._client.is_closed for c in replay.clients)
    asyncio.run(run())


def test_second_batch_repair_does_not_replay_first_result(store):
    async def run():
        executed=[]
        transport=Wire('chat_completions',[payload('chat_completions','json','{"query":"first"}','call-first'),payload('chat_completions','json',cid='call-bad'),payload('chat_completions','json','{"query":"corrected"}','call-corrected'),final('chat_completions')])
        events=[e async for e in make_session(transport.adapter,store,executed).run(AgentInput(message='只读核验',owner='owner',session_id='second'))]
        transport.verify(4)
        assert executed == [{'query':'first'},{'query':'corrected'}]
        assert events[-1].payload['status'] == 'success'
        assert events[-1].payload['tool_calls'] == 3
    asyncio.run(run())


def test_repeated_errors_exhaust_normal_budget(store):
    async def run():
        executed=[]
        transport=Wire('chat_completions',[payload('chat_completions','json',cid=f'call-{i}') for i in range(3)])
        events=[e async for e in make_session(transport.adapter,store,executed,SessionLimits(max_model_rounds=3,max_tool_calls=2)).run(AgentInput(message='只读核验',owner='owner',session_id='budget'))]
        transport.verify(3)
        assert not executed
        assert events[-1].type is AgentEventType.TURN_FAILED
        assert events[-1].payload['code'] == 'invalid_arguments'
        assert transport.requests[-1].get('tools')
        failures = [event.payload for event in events if event.type is AgentEventType.TOOL_FAILED]
        assert [(event['call_id'], event['code']) for event in failures] == [
            ('call-0', 'invalid_arguments'), ('call-1', 'invalid_arguments'),
            ('call-2', 'tool_budget_exceeded'),
        ]
        cold = SQLiteKernelStore(secret_provider=lambda: 'offline-tool-argument-test')
        saved = await cold.load(owner='owner', session_id='budget')
        assert [call['call_id'] for row in saved.conversation for call in row.get('tool_calls', [])] == [
            'call-0', 'call-1', 'call-2',
        ]
        assert [row['tool_call_id'] for row in saved.conversation if row['role'] == 'tool'] == [
            'call-0', 'call-1', 'call-2',
        ]
    asyncio.run(run())


_NATIVE_PROTOCOL = 'anthropic_messages'
_NATIVE_SESSION = 'native-recovery'
_NATIVE_HISTORY_ID = 'previous-completed-original-id'
_NATIVE_HISTORY_QUERY = '已完成历史核验'
_NATIVE_HISTORY_BLOCKS = [
    {'type': 'thinking', 'thinking': OPAQUE, 'signature': '  previous-completed-signature\nDO-NOT-STRIP  '},
    {'type': 'redacted_thinking', 'data': ' previously-completed-opaque\nDO-NOT-STRIP  '},
    {'type': 'tool_use', 'id': _NATIVE_HISTORY_ID, 'name': 'lookup',
     'input': {'query': _NATIVE_HISTORY_QUERY}},
]
_NATIVE_BAD_ID = 'uncommittable-bad-native-call'
_NATIVE_NEIGHBOR_ID = 'uncommittable-normal-native-call'
_NATIVE_NEIGHBOR_QUERY = 'UNCOMMITTABLE-NORMAL-SENTINEL'
_NATIVE_BAD_RAW = '  {"query":"私有原串-UNCOMMITTABLE-RAW-SENTINEL", \n'
_NATIVE_NEW_ID = 'recovered-new-native-call'
_NATIVE_NEW_QUERY = '恢复后合法核验'


def _native_response(mode, blocks, *, stop_reason='tool_use'):
    """真实 Anthropic JSON/SSE；ASCII 转义也能把非法 Unicode 身份送到解析器。"""
    if mode == 'json':
        return json.dumps({'content': blocks, 'stop_reason': stop_reason}).encode(), 'application/json'
    events = []
    for index, block in enumerate(blocks):
        start = dict(block)
        raw = None
        if block['type'] == 'tool_use' and isinstance(block.get('input'), (dict, str)):
            raw = block['input'] if isinstance(block['input'], str) else encode(block['input'])
            start['input'] = {}
        events.append({'type': 'content_block_start', 'index': index, 'content_block': start})
        if raw is not None:
            events.append({'type': 'content_block_delta', 'index': index,
                           'delta': {'type': 'input_json_delta', 'partial_json': raw}})
        events.append({'type': 'content_block_stop', 'index': index})
    events.extend([{'type': 'message_delta', 'delta': {'stop_reason': stop_reason}},
                   {'type': 'message_stop'}])
    return ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode(), 'text/event-stream'


def _native_final(mode):
    return _native_response(mode, [{'type': 'text', 'text': '已核验'}], stop_reason='end_turn')


def _native_bad_blocks(thinking=True, mixed=True):
    blocks: list[dict[str, Any]] = ([{'type': 'thinking', 'thinking': 'NATIVE-BAD-PRIVATE-SENTINEL',
                'signature': 'NATIVE-BAD-SIGNATURE-SENTINEL'},
               {'type': 'redacted_thinking', 'data': ' NATIVE-BAD-OPAQUE-SENTINEL\n  '}]
              if thinking else [])
    if mixed:
        blocks.append({'type': 'tool_use', 'id': _NATIVE_NEIGHBOR_ID, 'name': 'lookup',
                       'input': {'query': _NATIVE_NEIGHBOR_QUERY}})
    return blocks + [{'type': 'tool_use', 'id': _NATIVE_BAD_ID, 'name': 'lookup',
                      'input': _NATIVE_BAD_RAW}]


def _assert_discarded_native_absent(value, *, allow_neighbor_event=False):
    # Call IDs and ASCII sentinels remain detectable even inside nested JSON strings.
    serialized = json.dumps(value)
    forbidden = [_NATIVE_BAD_ID, 'UNCOMMITTABLE-RAW-SENTINEL',
                 'NATIVE-BAD-PRIVATE-SENTINEL', 'NATIVE-BAD-SIGNATURE-SENTINEL',
                 'NATIVE-BAD-OPAQUE-SENTINEL']
    if not allow_neighbor_event:
        forbidden.extend([_NATIVE_NEIGHBOR_ID, _NATIVE_NEIGHBOR_QUERY])
    for marker in forbidden:
        assert marker not in serialized, f'discarded native turn leaked: {marker}'


def _assert_original_native_history(request):
    blocks = [row['content'] for row in request['messages']
              if row.get('role') == 'assistant' and isinstance(row.get('content'), list)
              and any(block.get('type') == 'tool_use' and block.get('id') == _NATIVE_HISTORY_ID
                      for block in row['content'])]
    assert blocks == [_NATIVE_HISTORY_BLOCKS]
    for original, replayed in zip(_NATIVE_HISTORY_BLOCKS[:2], blocks[0][:2]):
        assert encode(replayed).encode('utf-8') == encode(original).encode('utf-8')
    results = [block for row in request['messages'] if isinstance(row.get('content'), list)
               for block in row['content']
               if block.get('type') == 'tool_result' and block.get('tool_use_id') == _NATIVE_HISTORY_ID]
    assert len(results) == 1, 'the completed historical tool fact must appear exactly once'
    assert json.loads(results[0]['content'])['ok'] is True


class _NativeRound(TypedDict):
    events: list[ModelEvent]
    error: ModelProviderError | None
    executed: list[dict[str, Any]]
    conversation: list[dict[str, Any]]
    state: dict[str, Any]


class _NativeRoundRecorder:
    """只观察真实 adapter；每次请求前冷读 SQLite，不替换模型事件或恢复逻辑。"""
    def __init__(self, adapter, cold_store, executed):
        self.adapter = adapter
        self.cold_store = cold_store
        self.executed = executed
        self.rounds: list[_NativeRound] = []

    async def stream(self, request, *, cancellation):
        state = await self.cold_store.load(owner='owner', session_id=_NATIVE_SESSION)
        record: _NativeRound = {'events': [], 'error': None, 'executed': deepcopy(self.executed),
                  'conversation': deepcopy(state.conversation), 'state': deepcopy(state.to_payload())}
        self.rounds.append(record)
        try:
            async for event in self.adapter.stream(request, cancellation=cancellation):
                record['events'].append(event)
                yield event
        except ModelProviderError as exc:
            record['error'] = exc
            raise


async def _seed_original_native_history(mode, store, executed):
    transport = Wire(_NATIVE_PROTOCOL, [_native_response(mode, _NATIVE_HISTORY_BLOCKS), _native_final(mode)])
    events = [event async for event in make_session(transport.adapter, store, executed).run(
        AgentInput(message='只读核验历史', owner='owner', session_id=_NATIVE_SESSION))]
    transport.verify(2)
    assert events[-1].type is AgentEventType.TURN_COMPLETED and events[-1].payload['status'] == 'success'
    assert executed == [{'query': _NATIVE_HISTORY_QUERY}]
    _assert_original_native_history(transport.requests[1])
    cold = SQLiteKernelStore(secret_provider=lambda: 'offline-tool-argument-test')
    state = await cold.load(owner='owner', session_id=_NATIVE_SESSION)
    return cold, deepcopy(state.conversation)


@pytest.mark.parametrize('mode', ['sse', 'json'])
@pytest.mark.parametrize('thinking', [False, True], ids=['plain', 'signed'])
@pytest.mark.parametrize('mixed', [False, True], ids=['single', 'mixed'])
def test_anthropic_bounded_invalid_json_rebuilds_original_history_once(mode, thinking, mixed, store):
    async def run():
        executed = []
        cold, original = await _seed_original_native_history(mode, store, executed)
        new_block = {'type': 'tool_use', 'id': _NATIVE_NEW_ID, 'name': 'lookup',
                     'input': {'query': _NATIVE_NEW_QUERY}}
        transport = Wire(_NATIVE_PROTOCOL, [_native_response(mode, _native_bad_blocks(thinking, mixed)),
                                          _native_response(mode, [new_block]), _native_final(mode)])
        recorder = _NativeRoundRecorder(transport.adapter, cold, executed)
        events = [event async for event in make_session(
            recorder, cold, executed, SessionLimits(max_model_rounds=3, max_tool_calls=1)).run(
                AgentInput(message='本轮只读核验', owner='owner', session_id=_NATIVE_SESSION))]
        transport.verify(3)
        assert events[-1].type is AgentEventType.TURN_COMPLETED
        assert events[-1].payload['status'] == 'success' and events[-1].payload['tool_calls'] == 1
        assert executed == [{'query': _NATIVE_HISTORY_QUERY}, {'query': _NATIVE_NEW_QUERY}]
        assert len(recorder.rounds) == 3
        bad, rebuilt, completed = recorder.rounds
        assert isinstance(bad['error'], UncommittableModelTurn)
        assert bad['error'].reason == 'unreplayable_arguments'
        assert not any(event.type is ModelEventType.FINISH for event in bad['events'])
        assert all(event.provider_state is None for event in bad['events'])
        # A normal SSE neighbor may already be delivered, but it cannot execute or commit without FINISH.
        delivered = [event.tool_call for event in bad['events'] if event.tool_call]
        assert all(call.call_id == _NATIVE_NEIGHBOR_ID and call.raw_arguments is None
                   and call.argument_error is None for call in delivered)
        assert rebuilt['executed'] == bad['executed'] == [{'query': _NATIVE_HISTORY_QUERY}]
        assert rebuilt['conversation'] == bad['conversation']
        assert rebuilt['conversation'][:len(original)] == original
        _assert_discarded_native_absent(rebuilt['state'])
        assert completed['executed'] == executed
        assert rebuilt['error'] is None and completed['error'] is None
        assert transport.requests[0]['messages'] == transport.requests[1]['messages']
        for request in transport.requests:
            _assert_original_native_history(request)
            _assert_discarded_native_absent(request)
        phases = [event.payload['phase'] for event in events if event.type is AgentEventType.MODEL_STARTED]
        assert phases.count('answer_recovery') == 1
        recovery_prompt = ''.join(block['text'] for block in transport.requests[1]['system'])
        assert '参数' in recovery_prompt and ('JSON' in recovery_prompt or '重建' in recovery_prompt)
        starts = [event for event in events if event.type is AgentEventType.TOOL_STARTED]
        assert len(starts) == 1
        _assert_discarded_native_absent([event.to_dict() for event in events], allow_neighbor_event=True)
        saved = await cold.load(owner='owner', session_id=_NATIVE_SESSION)
        assert saved.conversation[:len(original)] == original
        assert [call['call_id'] for row in saved.conversation for call in row.get('tool_calls', [])] == [
            _NATIVE_HISTORY_ID, _NATIVE_NEW_ID]
        assert [row['tool_call_id'] for row in saved.conversation if row.get('role') == 'tool'] == [
            _NATIVE_HISTORY_ID, _NATIVE_NEW_ID]
        _assert_discarded_native_absent(saved.to_payload())
        # A second cold session must replay both true results, not execute either handler again.
        restored = SQLiteKernelStore(secret_provider=lambda: 'offline-tool-argument-test')
        replay = Wire(_NATIVE_PROTOCOL, [_native_final(mode)])
        later = [event async for event in make_session(replay.adapter, restored, executed).run(
            AgentInput(message='只总结已有核验', owner='owner', session_id=_NATIVE_SESSION))]
        replay.verify(1)
        assert later[-1].payload['status'] == 'success'
        _assert_original_native_history(replay.requests[0])
        _assert_discarded_native_absent(replay.requests[0])
        assert executed == [{'query': _NATIVE_HISTORY_QUERY}, {'query': _NATIVE_NEW_QUERY}]
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['sse', 'json'])
def test_anthropic_repeated_invalid_json_recovers_only_once(mode, store):
    async def run():
        executed = []
        cold, original = await _seed_original_native_history(mode, store, executed)
        bad = _native_response(mode, _native_bad_blocks())
        transport = Wire(_NATIVE_PROTOCOL, [bad, bad, _native_final(mode)])
        recorder = _NativeRoundRecorder(transport.adapter, cold, executed)
        events = [event async for event in make_session(recorder, cold, executed).run(
            AgentInput(message='本轮只读核验', owner='owner', session_id=_NATIVE_SESSION))]
        transport.verify(2)
        assert len(transport.replies) == 1, 'there must not be an adapter retry or a second session rebuild'
        assert events[-1].type is AgentEventType.TURN_FAILED
        assert executed == [{'query': _NATIVE_HISTORY_QUERY}]
        assert len(recorder.rounds) == 2
        assert all(isinstance(record['error'], UncommittableModelTurn)
                   and record['error'].reason == 'unreplayable_arguments' for record in recorder.rounds)
        assert not any(event.type is ModelEventType.FINISH
                       for record in recorder.rounds for event in record['events'])
        assert not any(event.type in {AgentEventType.TOOL_STARTED, AgentEventType.TOOL_COMPLETED}
                       for event in events)
        assert transport.requests[0]['messages'] == transport.requests[1]['messages']
        for request in transport.requests:
            _assert_original_native_history(request)
            _assert_discarded_native_absent(request)
        saved = await cold.load(owner='owner', session_id=_NATIVE_SESSION)
        assert saved.conversation[:len(original)] == original
        _assert_discarded_native_absent(saved.to_payload())
        _assert_discarded_native_absent([event.to_dict() for event in events], allow_neighbor_event=True)
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['sse', 'json'])
@pytest.mark.parametrize('defect', ['too-large', 'too-large-utf8', 'nonobject', 'nonobject-json', 'numeric-id', 'empty-id',
                                  'unicode-id', 'duplicate-id', 'signature-type', 'signature-empty',
                                  'signature-missing', 'redacted-type'])
def test_anthropic_known_invalid_native_group_never_recovers(mode, defect, store):
    async def run():
        executed = []
        cold, original = await _seed_original_native_history(mode, store, executed)
        blocks: list[dict[str, Any]] = _native_bad_blocks()
        if defect == 'too-large':
            blocks[-1]['input'] = '{' + 'x' * 32768
        elif defect == 'too-large-utf8':
            blocks[-1]['input'] = '中' * 11000 + '{'
        elif defect == 'nonobject':
            blocks[-1]['input'] = []
        elif defect == 'nonobject-json':
            blocks[-1]['input'] = '[1]'
        elif defect.endswith('-id'):
            blocks[-1]['id'] = {'numeric-id': 123, 'empty-id': '', 'unicode-id': '\ud800',
                               'duplicate-id': _NATIVE_NEIGHBOR_ID}[defect]
        elif defect == 'signature-type':
            blocks[0]['signature'] = {'not': 'a signature'}
        elif defect == 'signature-empty':
            blocks[0]['signature'] = ''
        elif defect == 'signature-missing':
            blocks[0].pop('signature')
        else:
            blocks[1]['data'] = []
        transport = Wire(_NATIVE_PROTOCOL, [_native_response(mode, blocks), _native_final(mode)])
        recorder = _NativeRoundRecorder(transport.adapter, cold, executed)
        events = [event async for event in make_session(recorder, cold, executed).run(
            AgentInput(message='本轮只读核验', owner='owner', session_id=_NATIVE_SESSION))]
        transport.verify(1)
        assert len(transport.replies) == 1
        assert events[-1].type is AgentEventType.TURN_FAILED
        assert executed == [{'query': _NATIVE_HISTORY_QUERY}]
        assert len(recorder.rounds) == 1 and isinstance(recorder.rounds[0]['error'], ModelProviderError)
        assert getattr(recorder.rounds[0]['error'], 'reason', '') != 'unreplayable_arguments'
        assert not any(event.type is ModelEventType.FINISH for event in recorder.rounds[0]['events'])
        assert not any(event.type is AgentEventType.TOOL_STARTED for event in events)
        saved = await cold.load(owner='owner', session_id=_NATIVE_SESSION)
        assert saved.conversation[:len(original)] == original
        assert [call['call_id'] for row in saved.conversation for call in row.get('tool_calls', [])] == [
            _NATIVE_HISTORY_ID]
        _assert_discarded_native_absent(saved.to_payload())
        _assert_original_native_history(transport.requests[0])
    asyncio.run(run())


def test_anthropic_open_private_block_with_invalid_json_never_recovers(store):
    async def run():
        executed = []
        cold, original = await _seed_original_native_history('sse', store, executed)
        body, _ = _native_response('sse', _native_bad_blocks())
        events = [json.loads(line[6:]) for line in body.decode().splitlines() if line.startswith('data: ')]
        events = [event for event in events
                  if not (event['type'] == 'content_block_stop' and event['index'] == 0)]
        transport = Wire(_NATIVE_PROTOCOL, [event_wire(events), _native_final('sse')])
        recorder = _NativeRoundRecorder(transport.adapter, cold, executed)
        observed = [event async for event in make_session(recorder, cold, executed).run(
            AgentInput(message='本轮只读核验', owner='owner', session_id=_NATIVE_SESSION))]
        transport.verify(1)
        assert observed[-1].type is AgentEventType.TURN_FAILED
        assert executed == [{'query': _NATIVE_HISTORY_QUERY}]
        assert isinstance(recorder.rounds[0]['error'], ModelProviderError)
        assert getattr(recorder.rounds[0]['error'], 'reason', '') != 'unreplayable_arguments'
        assert not any(event.type is ModelEventType.FINISH for event in recorder.rounds[0]['events'])
        assert not any(event.type is AgentEventType.TOOL_STARTED for event in observed)
        saved = await cold.load(owner='owner', session_id=_NATIVE_SESSION)
        assert saved.conversation[:len(original)] == original
        _assert_discarded_native_absent(saved.to_payload())
    asyncio.run(run())


@pytest.mark.parametrize('allow_argument_errors', [False, True])
def test_anthropic_shared_parser_invalid_json_remains_strict(allow_argument_errors):
    envelope = {'content': _native_bad_blocks(), 'stop_reason': 'tool_use'}
    if allow_argument_errors:
        with pytest.raises(ValueError):
            parse_native_tool_turn(envelope, _NATIVE_PROTOCOL, allow_argument_errors=True)
    else:
        with pytest.raises(ValueError):
            parse_native_tool_turn(envelope, _NATIVE_PROTOCOL)


@pytest.mark.parametrize('mode', ['sse', 'json'])
def test_anthropic_valid_object_missing_schema_directly_retries_read(mode, store):
    async def run():
        executed = []
        cold, original = await _seed_original_native_history(mode, store, executed)
        blocks: list[dict[str, Any]] = [{'type': 'thinking', 'thinking': 'valid-schema-thinking', 'signature': 'valid-schema-signature'},
                  {'type': 'tool_use', 'id': 'schema-rejected-native-call', 'name': 'lookup', 'input': {}}]
        transport = Wire(_NATIVE_PROTOCOL, [_native_response(mode, blocks), _native_response(
                mode, [{'type': 'tool_use', 'id': 'schema-repair-native-call',
                        'name': 'lookup', 'input': {'query': _NATIVE_NEW_QUERY}}]), _native_final(mode)])
        recorder = _NativeRoundRecorder(transport.adapter, cold, executed)
        events = [event async for event in make_session(recorder, cold, executed).run(
            AgentInput(message='本轮只读核验', owner='owner', session_id=_NATIVE_SESSION))]
        transport.verify(3)
        assert events[-1].type is AgentEventType.TURN_COMPLETED and events[-1].payload['status'] == 'success'
        assert executed == [{'query': _NATIVE_HISTORY_QUERY}, {'query': _NATIVE_NEW_QUERY}]
        assert events[-1].payload['tool_calls'] == 2
        assert all(record['error'] is None for record in recorder.rounds)
        assert all(event.payload['phase'] != 'answer_recovery'
                   for event in events if event.type is AgentEventType.MODEL_STARTED)
        call = next(event.tool_call for event in recorder.rounds[0]['events'] if event.tool_call)
        assert call.arguments == {} and call.argument_error is None and call.raw_arguments is None
        assert 'agent__repair' not in json.dumps(transport.requests[1]), 'schema feedback uses ordinary direct retry'
        repaired = next(event.tool_call for event in recorder.rounds[1]['events'] if event.tool_call)
        assert repaired.name == 'lookup' and repaired.arguments == {'query': _NATIVE_NEW_QUERY}
        for request in transport.requests:
            _assert_original_native_history(request)
        saved = await cold.load(owner='owner', session_id=_NATIVE_SESSION)
        assert saved.conversation[:len(original)] == original
        assert [call['call_id'] for row in saved.conversation for call in row.get('tool_calls', [])] == [
            _NATIVE_HISTORY_ID, 'schema-rejected-native-call', 'schema-repair-native-call']
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['sse', 'json'])
@pytest.mark.parametrize('round_budget', [1, 2])
def test_anthropic_invalid_json_rebuild_uses_normal_model_budget(mode, round_budget, store):
    async def run():
        executed = []
        cold, original = await _seed_original_native_history(mode, store, executed)
        transport = Wire(_NATIVE_PROTOCOL, [_native_response(mode, _native_bad_blocks()), _native_response(
            mode, [{'type': 'tool_use', 'id': _NATIVE_NEW_ID, 'name': 'lookup',
                    'input': {'query': _NATIVE_NEW_QUERY}}]), _native_final(mode)])
        events = [event async for event in make_session(transport.adapter, cold, executed,
            SessionLimits(max_model_rounds=round_budget, max_tool_calls=1)).run(
                AgentInput(message='本轮只读核验', owner='owner', session_id=_NATIVE_SESSION))]
        transport.verify(round_budget)
        assert len(transport.replies) == 3 - round_budget
        if round_budget == 1:
            assert events[-1].type is AgentEventType.TURN_FAILED
            assert executed == [{'query': _NATIVE_HISTORY_QUERY}]
        else:
            assert events[-1].type is AgentEventType.TURN_COMPLETED
            assert events[-1].payload['finish_reason'] == 'model_round_budget_exceeded'
            assert events[-1].payload['tool_calls'] == 1
            assert executed == [{'query': _NATIVE_HISTORY_QUERY}, {'query': _NATIVE_NEW_QUERY}]
        saved = await cold.load(owner='owner', session_id=_NATIVE_SESSION)
        assert saved.conversation[:len(original)] == original
        _assert_discarded_native_absent(saved.to_payload())
        for request in transport.requests:
            _assert_original_native_history(request)
            _assert_discarded_native_absent(request)
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['sse', 'json'])
def test_anthropic_cancel_at_rebuild_does_not_execute_or_commit_bad_group(mode, store):
    async def run():
        executed = []
        cold, original = await _seed_original_native_history(mode, store, executed)
        transport = Wire(_NATIVE_PROTOCOL, [_native_response(mode, _native_bad_blocks()), _native_final(mode)])
        entered = asyncio.Event()
        class StopAtRebuild(_NativeRoundRecorder):
            async def stream(self, request, *, cancellation):
                if len(self.rounds) == 1:
                    entered.set()
                    await asyncio.Event().wait()
                async for event in super().stream(request, cancellation=cancellation):
                    yield event
        recorder = StopAtRebuild(transport.adapter, cold, executed)
        session = make_session(recorder, cold, executed)
        async def consume():
            return [event async for event in session.run(
                AgentInput(message='本轮只读核验', owner='owner', session_id=_NATIVE_SESSION))]
        task = asyncio.create_task(consume())
        waiter = asyncio.create_task(entered.wait())
        try:
            done, _ = await asyncio.wait((task, waiter), timeout=3, return_when=asyncio.FIRST_COMPLETED)
            assert done and entered.is_set(), 'the bad JSON round must reach the one rebuild boundary'
            assert await session.cancel(owner='owner', session_id=_NATIVE_SESSION)
            events = await asyncio.wait_for(task, 3)
        finally:
            waiter.cancel()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, waiter, return_exceptions=True)
        transport.verify(1)
        assert events[-1].type is AgentEventType.TURN_CANCELLED
        assert executed == [{'query': _NATIVE_HISTORY_QUERY}]
        assert not any(event.type is AgentEventType.TOOL_STARTED for event in events)
        saved = await cold.load(owner='owner', session_id=_NATIVE_SESSION)
        assert saved.conversation[:len(original)] == original
        _assert_discarded_native_absent(saved.to_payload())
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['sse', 'json'])
@pytest.mark.parametrize('decision', ['confirm', 'cancel'])
def test_anthropic_recovered_new_write_still_requires_its_own_confirmation(mode, decision, store):
    async def run():
        executed, previews, writes = [], [], []
        cold, original = await _seed_original_native_history(mode, store, executed)
        arguments = {'query': '新的独立写操作'}
        def prepare(arguments, context):
            previews.append(dict(arguments))
            return PreparedEffect(preview={'summary': '待确认的新写预览'}, snapshot_fingerprint='new-write-snapshot')
        def write(arguments, snapshot_fingerprint, context):
            writes.append(dict(arguments))
            return {'ok': True, 'status': 'success', 'summary': '新的独立写操作已完成', 'data': {}}
        spec = KernelToolSpec(name='update', domain='library', description='需独立确认的写操作',
            input_schema={'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']},
            effect=ToolEffect.WRITE, prepare=prepare, execute_confirmed=write)
        catalog = ToolCatalog([spec])
        transport = Wire(_NATIVE_PROTOCOL, [_native_response(mode, _native_bad_blocks(mixed=False)),
            _native_response(mode, [{'type': 'tool_use', 'id': _NATIVE_NEW_ID, 'name': 'update', 'input': arguments}]),
            _native_final(mode)])
        recorder = _NativeRoundRecorder(transport.adapter, cold, executed)
        session = AgentSession(model=recorder, catalog=catalog,
            pipeline=ToolPipeline(catalog=catalog, state_store=cold), state_store=cold)
        events = [event async for event in session.run(
            AgentInput(message='本轮准备新写操作', owner='owner', session_id=_NATIVE_SESSION))]
        assert len(transport.requests) == 2
        assert any(event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED for event in events)
        assert events[-1].payload['status'] == 'approval_required'
        assert recorder.rounds[1]['executed'] == [{'query': _NATIVE_HISTORY_QUERY}]
        assert previews == [arguments] and writes == []
        plan_id = events[-1].payload['plan_id']
        if decision == 'confirm':
            confirmed = [event async for event in session.confirm(
                owner='owner', session_id=_NATIVE_SESSION, plan_id=plan_id)]
            assert writes == [arguments]
            assert any(event.type is AgentEventType.EFFECT_COMPLETED for event in confirmed)
            transport.verify(3)
        else:
            assert await session.cancel_effect(owner='owner', session_id=_NATIVE_SESSION, plan_id=plan_id)
            assert writes == []
            transport.verify(2)
        duplicate = [event async for event in session.confirm(
            owner='owner', session_id=_NATIVE_SESSION, plan_id=plan_id)]
        assert duplicate[-1].type is AgentEventType.TURN_FAILED
        assert writes == ([arguments] if decision == 'confirm' else [])
        assert executed == [{'query': _NATIVE_HISTORY_QUERY}]
        for request in transport.requests:
            _assert_original_native_history(request)
            _assert_discarded_native_absent(request)
        saved = await cold.load(owner='owner', session_id=_NATIVE_SESSION)
        assert saved.conversation[:len(original)] == original
        _assert_discarded_native_absent(saved.to_payload())
    asyncio.run(run())
