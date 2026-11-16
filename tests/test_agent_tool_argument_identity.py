"""Repair-batch identities must be UTF-8 safe before provider completion."""
import asyncio
import json

import pytest

from app.agent.kernel.model import ModelEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.provider_model import ModelProviderError
from app.agent.kernel.state_types import AgentInput
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414
from tests.test_agent_tool_argument_ingress import (
    Wire,
    observe,
)
from tests.test_agent_tool_argument_ingress import (
    offline as offline,
)
from tests.test_agent_tool_argument_ingress import (
    store as store,
)
from tests.test_agent_tool_argument_repair_replay import RAW, make_session, payload


@pytest.mark.parametrize('protocol', ['chat_completions', 'responses'])
@pytest.mark.parametrize('field', ['call_id', 'name'])
def test_json_error_identity_invalid_utf8_rejected_before_finish(agent_llm_enabled, protocol, field):
    data, kind = payload(protocol, 'json')
    value = json.loads(data)
    if protocol == 'chat_completions':
        call = value['choices'][0]['message']['tool_calls'][0]
        if field == 'call_id':
            call['id'] = 'bad\ud800id'
        else:
            call['function']['name'] = 'bad\ud800name'
    else:
        value['output'][1][field] = 'bad\ud800value'
    events, error = asyncio.run(observe(protocol, (json.dumps(value).encode(), kind)))
    assert isinstance(error, ModelProviderError)
    assert not any(e.type is ModelEventType.FINISH for e in events)


def test_json_surrogate_identity_is_provider_failure_not_capacity_error(agent_llm_enabled, store):
    async def run():
        data, kind = payload('chat_completions', 'json')
        value = json.loads(data)
        value['choices'][0]['message']['tool_calls'][0]['id'] = 'bad\ud800id'
        wire = Wire('chat_completions', [(json.dumps(value).encode(), kind)])
        executed = []
        events = [e async for e in make_session(wire.adapter, store, executed).run(
            AgentInput(message='只读核验', owner='owner', session_id='unicode-quality'))]
        wire.verify()
        state = await SQLiteKernelStore(secret_provider=lambda: 'offline-tool-argument-test').load(
            owner='owner', session_id='unicode-quality')
        assert not executed and not any(e.type.name == 'TOOL_STARTED' for e in events)
        assert not any(row.get('tool_calls') or row.get('role') == 'tool' for row in state.conversation)
        assert events[-1].payload['code'] == 'model_provider_error', dict(events[-1].payload)
    asyncio.run(run())


@pytest.mark.parametrize('protocol', ['chat_completions', 'responses'])
@pytest.mark.parametrize('mode', ['json', 'sse'])
def test_repair_identity_preserves_valid_non_ascii(agent_llm_enabled, protocol, mode):
    events, error = asyncio.run(observe(protocol, payload(
        protocol, mode, cid='调用-é-😀', name='查询-é')))
    assert error is None
    calls = [e.tool_call for e in events if e.tool_call]
    assert len(calls) == 1
    assert (calls[0].call_id, calls[0].name) == ('调用-é-😀', '查询-é')
    assert calls[0].raw_arguments == RAW and calls[0].argument_error == 'invalid_json'
    assert events[-1].type is ModelEventType.FINISH


@pytest.mark.parametrize('protocol', ['chat_completions', 'responses'])
@pytest.mark.parametrize('field', ['call_id', 'name'])
@pytest.mark.parametrize('bad_first', [True, False])
def test_json_repair_batch_checks_ordinary_neighbor_identity(agent_llm_enabled, protocol, field, bad_first):
    data, kind = payload(protocol, 'json')
    value = json.loads(data)
    if protocol == 'chat_completions':
        calls = value['choices'][0]['message']['tool_calls']
        ordinary = {'type': 'function', 'id': 'good',
                    'function': {'name': 'lookup', 'arguments': '{}'}}
        if field == 'call_id':
            ordinary['id'] = 'bad\ud800id'
        else:
            ordinary['function']['name'] = 'bad\ud800name'
    else:
        calls = value['output']
        ordinary = {'type': 'function_call', 'id': 'good-item', 'call_id': 'good',
                    'name': 'lookup', 'arguments': '{}'}
        ordinary[field] = 'bad\ud800value'
    calls.insert(len(calls) if bad_first else 0, ordinary)
    events, error = asyncio.run(observe(protocol, (json.dumps(value).encode(), kind)))
    assert isinstance(error, ModelProviderError)
    assert not any(e.type is ModelEventType.FINISH for e in events)
