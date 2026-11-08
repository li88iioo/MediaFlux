"""Positive #23 tracer and legacy controls. Future API RED is not a live bug."""


import asyncio
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pytest

from app import config
from app import database as db
from app.agent.kernel import bootstrap
from app.agent.kernel.provider_model import OpenAICompatibleModelAdapter
from app.agent.kernel.state_types import AgentInput
from tests import test_agent_generation_profiles as support
from tests.test_agent_generation_profiles import BASE, Wire, response

local_config = support.local_config
config_api = support.config_api


KEY = 'AGENT_LLM_BUDGET_PROFILES'


MODEL = 'tenant/opaque-alias'


OWNER = 'webk:v1:' + 'a' * 64


OUT = Path(__file__).parent


def row(model=MODEL, context=65536, output=1024, route=BASE):
    return dict(base_url=route, model=model, context_window_tokens=context, max_output_tokens=output)


def cap(body):
    keys = set(body) & {'max_tokens', 'max_output_tokens', 'max_completion_tokens'}
    assert len(keys) == 1
    return body[keys.pop()]


def publish(api, rows, **values):
    result = api.post('/api/config', json={KEY: json.dumps(rows, ensure_ascii=True), **values})
    assert result.status_code == 200, result.text
    return api.get('/api/config').json()[KEY]


def turn(session, channel='api', owner=OWNER, session_id='probe', text='hello'):
    async def run():
        return [e async for e in session.run(AgentInput(message=text, owner=owner,
                       session_id=session_id, channel=channel))]
    return asyncio.run(run())


def complete(events):
    assert events[-1].type.value == 'turn.completed', [(e.type.value, e.payload) for e in events]


@contextmanager
def trace_budget():
    traces = []
    old = sys.getprofile()
    def observe(frame, event, arg):
        if event == 'call' and frame.f_code.co_name == 'bounded_model_messages':
            values = frame.f_locals
            traces.append({k: values[k] for k in ('context_window_tokens', 'output_tokens')})
    sys.setprofile(observe)
    try:
        yield traces
    finally:
        sys.setprofile(old)


@pytest.fixture
def harness(local_config, request, tmp_path):
    wire = Wire('Topic')
    async def handler(request):
        protocol = 'responses' if request.url.path.endswith('/responses') else (
            'anthropic_messages' if request.url.path.endswith('/messages') else 'chat_completions')
        wire.requests.append({'protocol': protocol, 'path': request.url.path,
                              'host': request.headers['host'], 'body': json.loads(request.content),
                              'bytes': len(request.content), 'sha256': hashlib.sha256(request.content).hexdigest()})
        return httpx.Response(200, json=response(protocol, wire.text))
    wire.handler = handler
    with patch.dict(OpenAICompatibleModelAdapter.__init__.__kwdefaults__, client_factory=wire.factory), trace_budget() as traces:
        yield wire, traces
    assert all(c._client.is_closed for c in wire.clients)
    directory = Path(os.environ.get('EVIDENCE_DIR', str(tmp_path / 'wire')))
    directory.mkdir(exist_ok=True, parents=True)
    name = request.node.name.split('[')[0] + '-' + hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
    (directory / (name + '.json')).write_text(
        json.dumps({'test': request.node.nodeid, 'requests': wire.requests, 'traces': traces}, ensure_ascii=True, indent=2))


def select(model, owner, session_id):
    import telebot

    from app.bot import agent_adapter
    from app.modules import telegram_model_preferences as prefs
    token = prefs.create_model_callback(owner, session_id, 41, action='select', model_id=model)
    call = SimpleNamespace(id='callback', data='tgm:' + token, from_user=SimpleNamespace(id=7),
        message=SimpleNamespace(chat=SimpleNamespace(id=-100123), message_id=1, message_thread_id=41))
    bot = Mock()
    bot.edit_message_text.return_value = True
    agent_adapter.handle_agent_model_callback(bot, call, telebot)
    assert prefs.get_telegram_model_preference(owner, session_id) == model
    assert model in db.kv_get(prefs._key(owner, session_id))


@pytest.mark.parametrize('protocol', ['chat_completions', 'responses', 'anthropic_messages'])
def test_supported_save_runtime_wire(harness, config_api, protocol):
    wire, traces = harness
    publish(config_api, [row()], AGENT_LLM_PROTOCOL=protocol, AGENT_LLM_API_URL=BASE)
    complete(turn(bootstrap.build_agent_kernel_runtime().session))
    assert len(wire.requests) == 1
    assert wire.requests[0]['body']['model'] == MODEL
    assert cap(wire.requests[0]['body']) == 1024
    assert traces == [dict(context_window_tokens=65536, output_tokens=1024)]


def test_legacy_global_clamp(harness):
    wire, traces = harness
    config.set_and_save({'AGENT_LLM_CONTEXT_WINDOW_TOKENS': '16384'})
    complete(turn(bootstrap.build_agent_kernel_runtime().session))
    assert cap(wire.requests[0]['body']) == 4096
    assert traces == [dict(context_window_tokens=16384, output_tokens=4096)]


@pytest.mark.parametrize('output', [1, 127, 128])
def test_small_output_exact(harness, config_api, output):
    wire, traces = harness
    publish(config_api, [row(output=output)])
    complete(turn(bootstrap.build_agent_kernel_runtime().session))
    assert cap(wire.requests[0]['body']) == output
    assert traces[0]['output_tokens'] == output


@pytest.mark.parametrize('context', [1024, 16383])
def test_small_context_blocks_complete_chain(harness, config_api, context):
    wire, traces = harness
    publish(config_api, [row(context=context, output=127)])
    from app.agent.model_context_budget import conservative_tokens

    # 16383 is a supported profile, not a minimum-window rejection rule.
    # Keep this chain over budget even after resident prompt compaction.
    text = 'hello' if context == 1024 else '界' * 6000
    if context == 16383:
        assert conservative_tokens(text) > context
    events = turn(bootstrap.build_agent_kernel_runtime().session, text=text)
    assert not wire.requests
    assert any(e.payload.get('code') == 'context_budget_exceeded' for e in events)
    # 历史全文、精简视图和归档回读提示可分别试装；每次必须保持原窗口/输出额度。
    assert traces
    assert all(trace == dict(context_window_tokens=context, output_tokens=127) for trace in traces)




def test_small_context_fitting_complete_chain(harness, config_api):
    from app.agent.model_context_budget import conservative_tokens

    wire, traces = harness
    publish(config_api, [row(context=16383, output=127)],
            AGENT_LLM_PROTOCOL='chat_completions', AGENT_LLM_API_URL=BASE)
    events = turn(bootstrap.build_agent_kernel_runtime().session)
    complete(events)
    assert not any(e.payload.get('code') == 'context_budget_exceeded' for e in events)
    assert len(wire.requests) == 1
    request = wire.requests[0]
    body = request['body']
    assert request['protocol'] == 'chat_completions'
    assert body['model'] == MODEL
    assert cap(body) == 127
    assert any(m['role'] == 'user' and m['content'] == 'hello' for m in body['messages'])
    assert any(m['role'] == 'system' and m['content'] for m in body['messages'])
    assert body['tools']
    # Match the real adapter's whole serialized-body guard, including schemas,
    # protocol wrappers, output reservation and its fixed safety headroom.
    assert conservative_tokens(body) == request['bytes']
    assert conservative_tokens(body) + cap(body) + 512 <= 16383
    assert traces == [dict(context_window_tokens=16383, output_tokens=127)]


@pytest.mark.parametrize('rows', [[], [row(model='other')], [row(route='https://other.example.com/v1')],
                                [row(route=BASE + '/responses/responses')], [row(context=2000000, output=1000000)]])
def test_no_match_or_large_cap_preserves_cost(harness, config_api, rows):
    wire, traces = harness
    publish(config_api, rows)
    complete(turn(bootstrap.build_agent_kernel_runtime().session))
    assert cap(wire.requests[0]['body']) == 6000
    assert traces == [dict(context_window_tokens=128000, output_tokens=6000)]


def test_tg_callback_effective_model_and_web_default(harness, config_api):
    from app.bot import agent_adapter
    wire, traces = harness
    config.set_and_save(dict(TG_AGENT_ENABLED='1', TG_CHAT_ID='-100123', TG_AGENT_ALLOWED_USER_IDS='7'))
    publish(config_api, [row('small', output=1024), row('smaller', output=512)])
    session = bootstrap.build_agent_kernel_runtime().session
    owner = agent_adapter.telegram_agent_owner('-100123', '7')
    complete(turn(session, 'telegram', owner, 'topic'))
    for name in ('small', 'smaller'):
        select(name, owner, 'topic')
        complete(turn(session, 'telegram', owner, 'topic'))
    complete(turn(session, 'api', owner, 'topic'))
    assert [r['body']['model'] for r in wire.requests] == [MODEL, 'small', 'smaller', MODEL]
    assert [cap(r['body']) for r in wire.requests] == [6000, 1024, 512, 6000]
    assert [t['output_tokens'] for t in traces] == [6000, 1024, 512, 6000]


def test_fallback_keeps_output_and_next_runtime_reads_publisher(harness, config_api):
    wire, traces = harness
    publish(config_api, [row()], AGENT_LLM_PROTOCOL='auto', AGENT_LLM_API_URL=BASE)
    original = wire.handler
    async def changing(request):
        result = await original(request)
        if len(wire.requests) == 1:
            # Direct real publisher deliberately does not perform the API generation bump.
            config.set_and_save({KEY: json.dumps([row(output=2048)]), 'AGENT_LLM_CONTEXT_WINDOW_TOKENS': '65536'})
            return httpx.Response(404, text='responses api is not supported')
        return result
    wire.handler = changing
    complete(turn(bootstrap.build_agent_kernel_runtime().session))
    complete(turn(bootstrap.build_agent_kernel_runtime().session, session_id='next'))
    assert [r['protocol'] for r in wire.requests] == ['responses', 'chat_completions', 'responses']
    assert [cap(r['body']) for r in wire.requests] == [1024, 1024, 2048]
    assert [t['output_tokens'] for t in traces] == [1024, 2048]


def test_current_turn_native_tool_group_keeps_model_budget(harness, config_api):
    from app.bot import agent_adapter
    wire, traces = harness
    config.set_and_save(dict(TG_AGENT_ENABLED='1', TG_CHAT_ID='-100123', TG_AGENT_ALLOWED_USER_IDS='7'))
    publish(config_api, [row('first'), row('second', output=512)], AGENT_LLM_API_URL=BASE,
            AGENT_LLM_PROTOCOL='anthropic_messages')
    owner = agent_adapter.telegram_agent_owner('-100123', '7')
    select('first', owner, 'topic')
    original = wire.handler
    opaque = {'type': 'thinking', 'thinking': 'fixture-thought', 'signature': 'opaque/signature+unchanged=='}
    async def tool_then_answer(request):
        result = await original(request)
        if len(wire.requests) == 1:
            select('second', owner, 'topic')
            return httpx.Response(200, json={'content': [opaque, {'type': 'tool_use', 'id': 'native-call-1',
                'name': 'agent__run_code', 'input': {'code': "print('fixture')"}}], 'stop_reason': 'tool_use'})
        return result
    wire.handler = tool_then_answer
    session = bootstrap.build_agent_kernel_runtime().session
    complete(turn(session, 'telegram', owner, 'topic'))
    assert len(wire.requests) == 2
    assert [r['body']['model'] for r in wire.requests] == ['first', 'first']
    assert [cap(r['body']) for r in wire.requests] == [1024, 1024]
    replay = wire.requests[1]['body']['messages']
    assert any(opaque in m['content'] for m in replay if isinstance(m['content'], list))
    assert any(block.get('tool_use_id') == 'native-call-1' for m in replay
               if isinstance(m['content'], list) for block in m['content'])
    complete(turn(session, 'telegram', owner, 'topic'))
    assert wire.requests[-1]['body']['model'] == 'second'
    assert cap(wire.requests[-1]['body']) == 512
    assert [t['output_tokens'] for t in traces] == [1024, 1024, 1024, 512]
