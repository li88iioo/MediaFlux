"""Explicit generation profiles at the real, offline HTTP boundary."""
from starlette.responses import JSONResponse
from starlette.requests import Request
from typing import cast
import asyncio
import json
import socket
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest

from app import config
from app import database as db
from app.agent.kernel.model import ModelMessage, ModelRequest
from app.agent.kernel.provider_model import (
    OpenAICompatibleModelAdapter,
    ProviderSettings,
)
from app.agent.kernel.state_types import CancellationToken
from app.clients.ai_recognition import AIRecognitionClient
from app.clients.http import FixedHostHttpClient

BASE = 'https://api.example.com/v1'
KEY = 'AGENT_LLM_GENERATION_PROFILES'
PROTOCOLS = ('chat_completions', 'responses', 'anthropic_messages')
PATHS = dict(zip(PROTOCOLS, ('/v1/chat/completions', '/v1/responses', '/v1/messages')))


def profiles(model, protocol='chat_completions'):
    return json.dumps([{'base_url': BASE, 'model': model, 'protocol': protocol,
                        'token_parameter': {'chat_completions': 'max_completion_tokens', 'responses': 'max_output_tokens', 'anthropic_messages': 'max_tokens'}[protocol],
                        'temperature': 'omit'}])


@contextmanager
def settings(model='o3', protocol='chat_completions', profile='', effort=''):
    values = {'AGENT_LLM_API_URL': BASE, 'AGENT_LLM_MODEL': model,
              'AGENT_LLM_API_KEY': 'offline-fixture-only', 'AGENT_LLM_PROTOCOL': protocol,
              'AGENT_LLM_CONTEXT_WINDOW_TOKENS': '128000', KEY: profile,
              'AGENT_LLM_REASONING_EFFORT': effort}
    old = config.get
    with patch.object(config, 'get', side_effect=lambda key, default='': values.get(key, old(key, default))):
        yield values


def response(protocol, text):
    return {
        'chat_completions': {'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}]},
        'responses': {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}]},
        'anthropic_messages': {'content': [{'type': 'text', 'text': text}], 'stop_reason': 'end_turn'},
    }[protocol]


class Wire:
    def __init__(self, text='ok'):
        self.text = text
        self.requests = []
        self.clients = []

    async def handler(self, request):
        protocol = next(key for key, path in PATHS.items() if request.url.path == path)
        # No headers/credentials recorded. All prompts below are synthetic.
        self.requests.append({'protocol': protocol, 'path': request.url.path, 'body': json.loads(request.content)})
        return httpx.Response(200, json=response(protocol, self.text))

    def factory(self, **kwargs):
        client = FixedHostHttpClient(**kwargs, transport=httpx.MockTransport(self.handler),
            resolver=lambda host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))])
        self.clients.append(client)
        return client

    def single_body(self):
        assert len(self.requests) == 1, self.requests
        assert self.clients and all(client._client.is_closed for client in self.clients)
        return self.requests[0]['body']


def native(model, protocol, profile='', selected='', effort=''):
    wire = Wire()
    async def run():
        adapter = OpenAICompatibleModelAdapter(ProviderSettings.from_config(), client_factory=wire.factory)
        request = ModelRequest(system_prompt='offline', messages=(ModelMessage('user', 'hello'),), tools=(), model=selected)
        events = [event async for event in adapter.stream(request, cancellation=CancellationToken())]
        assert events[-1].type.value == 'finish'
    with settings(model, protocol, profile, effort):
        asyncio.run(run())
    return wire


def structured(model, protocol, profile=''):
    # tests bootstrap points this to its disposable scratch database.
    db.init_db()
    wire = Wire('{"ok":true}')
    with settings(model, protocol, profile), patch('app.clients.ai_recognition.FixedHostHttpClient', wire.factory):
        client = AIRecognitionClient(api_url=BASE, model=model, api_key='offline-fixture-only', protocol=protocol)
        result = client._request_structured(system_prompt='offline', payload={'probe': 'hello'},
            schema_name='offline', schema={'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False})
        assert result == {'ok': True}
    return wire


@pytest.mark.parametrize('entry', ['native', 'structured'])
@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('model', ['o3', 'gpt-5', 'tenant/opaque-alias'])
def test_compat_legacy_shape(entry, protocol, model):
    wire = globals()[entry](model, protocol)
    body = wire.single_body()
    expected_key = 'max_output_tokens' if protocol == 'responses' else 'max_completion_tokens' if protocol == 'chat_completions' and model == 'o3' else 'max_tokens'
    assert {key for key in ('max_tokens', 'max_completion_tokens', 'max_output_tokens') if key in body} == {expected_key}
    assert body[expected_key] == (1500 if entry == 'native' else 800)
    assert ('temperature' in body) is (model != 'o3')
    if model != 'o3':
        assert body['temperature'] == 0


@pytest.mark.parametrize('entry', ['native', 'structured'])
def test_explicit_profile_overrides_alias_guess(entry):
    model = 'tenant/opaque-alias'
    wire = globals()[entry](model, 'chat_completions', profiles(model))
    body = wire.single_body()
    assert body.get('max_completion_tokens') == (1500 if entry == 'native' else 800), body
    assert 'max_tokens' not in body and 'temperature' not in body


def test_selected_model_profile_uses_effective_identity():
    model = 'tenant/opaque-alias'
    wire = native('o3', 'chat_completions', profiles(model), selected=model)
    body = wire.single_body()
    assert body['model'] == model
    assert body.get('max_completion_tokens') == 1500 and 'temperature' not in body, body


def test_compat_profile_does_not_cross_route_or_protocol():
    raw = json.loads(profiles('tenant/opaque-alias'))
    raw[0]['base_url'] = 'https://other.example.com/v1'
    body = native('tenant/opaque-alias', 'chat_completions', json.dumps(raw)).single_body()
    assert body['max_tokens'] == 1500 and body['temperature'] == 0
    body = native('tenant/opaque-alias', 'responses', profiles('tenant/opaque-alias')).single_body()
    assert body['max_output_tokens'] == 1500 and body['temperature'] == 0


def assert_safe_error(error):
    # Inspect both retained exception edges, not just displayed traceback suppression.
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        assert 'secret-sentinel' not in repr(current)
        assert not isinstance(current, (json.JSONDecodeError, UnicodeError))
        pending.extend(e for e in (current.__cause__, current.__context__) if e)


@pytest.mark.parametrize('bad', [
    'secret-sentinel not json',
    '[{"base_url":"https://api.example.com:secret-sentinel/v1","model":"alias",'
    '"protocol":"chat_completions","token_parameter":"max_tokens","temperature":"zero"}]',
    'secret-sentinel\ud800',
    profiles('secret-sentinel\ud800'),
    '[' * 2000 + '"secret-sentinel"' + ']' * 2000,
])
def test_invalid_profiles_are_safe_local_failures(bad):
    from app.clients.openai_compatible import parse_generation_profiles

    with pytest.raises(ValueError) as caught:
        parse_generation_profiles(bad)
    assert_safe_error(caught.value)


@pytest.fixture
def local_config(tmp_path, monkeypatch):
    import os

    from app.agent.kernel import bootstrap

    for key in list(os.environ):
        if key.startswith('AGENT_'):
            monkeypatch.delenv(key)
    monkeypatch.setattr(config, 'ENV_FILE', tmp_path / 'user.env')
    monkeypatch.setattr(config, '_cache', None)
    monkeypatch.setattr(config, '_STARTUP_ENV_OVERRIDES', frozenset())
    monkeypatch.setattr(bootstrap, '_runtime', None)
    # Restore environment mutations made by the real atomic config publisher.
    from app.agent import feature_gate
    from tests.support import isolated_test_database
    with patch.dict(os.environ), isolated_test_database():
        config.write_env_file(config.ENV_FILE, {}, replace=False)
        feature_gate.initialize_runtime_fence()
        config.set_and_save({'AGENT_ENABLED': '1', 'AGENT_LLM_ENABLED': '1',
                             'AGENT_LLM_API_URL': BASE + '/chat/completions',
                             'AGENT_LLM_MODEL': 'tenant/opaque-alias',
                             'AGENT_LLM_PROTOCOL': 'chat_completions',
                             'AGENT_LLM_API_KEY': 'synthetic-preserved-key',
                             'CUSTOM_DEPLOYMENT_FIELD': 'opaque-value'})
        yield config.ENV_FILE


def admin_request():
    from types import SimpleNamespace

    return SimpleNamespace(session={'logged_in': True}, app=SimpleNamespace(
        state=SimpleNamespace(background_services_enabled=False, media_proxy_manager=None)))


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('real socket/DNS forbidden by profiles tests')
    for obj, names in ((socket.socket, ('connect', 'connect_ex', 'sendto')),
                       (socket, ('create_connection', 'getaddrinfo'))):
        for name in names:
            monkeypatch.setattr(obj, name, forbidden)


@pytest.fixture
def config_api(local_config):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routes.api import router
    from app.routes.tools_api import router as tools_router

    application = FastAPI()
    application.state.background_services_enabled = False
    application.state.media_proxy_manager = None
    @application.middleware('http')
    async def authenticated_fixture(request, call_next):
        request.scope['session'] = {'logged_in': True}
        return await call_next(request)
    application.include_router(router)
    application.include_router(tools_router)
    with TestClient(application) as client:
        yield client


def test_config_save_read_rebuild_to_wire(local_config, config_api):
    from app.agent.kernel import bootstrap

    db.init_db()
    wire = Wire()
    original = config._read_env_file(local_config)
    with patch.dict(OpenAICompatibleModelAdapter.__init__.__kwdefaults__, client_factory=wire.factory):
        before = bootstrap.get_agent_kernel_runtime()
        result = config_api.post('/api/config', json={KEY: profiles('tenant/opaque-alias'),
                                                      'AGENT_LLM_API_KEY': '********'})
        assert result.status_code == 200, result.text
        assert result.json() == {'success': True}
        saved = config_api.get('/api/config').json()
        assert json.loads(saved[KEY])[0]['model'] == 'tenant/opaque-alias'
        assert saved['AGENT_LLM_API_KEY'] != original['AGENT_LLM_API_KEY']
        rebuilt = bootstrap.get_agent_kernel_runtime()
        assert rebuilt is not before
        async def run():
            from app.agent.kernel.state_types import AgentInput
            return [e async for e in rebuilt.session.run(AgentInput(
                message='hello', owner='webk:v1:' + 'a' * 64, session_id='profile-api', channel='api'))]
        events = asyncio.run(run())
        assert events[-1].type.value == 'turn.completed', [(e.type.value, e.payload) for e in events]
    assert {key: config._read_env_file(local_config)[key] for key in original} == original
    body = wire.single_body()
    assert body['max_completion_tokens'] == 6000 and 'temperature' not in body


def test_config_large_unicode_roundtrip_is_rereadable(local_config, config_api):
    from app.clients.openai_compatible import parse_generation_profiles

    rows = [dict(json.loads(profiles('alias'))[0], model=str(i) + '模' * 180) for i in range(64)]
    raw = json.dumps(rows, ensure_ascii=False)
    assert 10_000 < len(raw.encode('utf-8')) < 65_536
    assert len(json.dumps(rows, ensure_ascii=True).encode('utf-8')) > 65_536
    response = config_api.post('/api/config', json={KEY: raw})
    assert response.status_code == 200, response.text
    assert response.json() == {'success': True}
    saved = config_api.get('/api/config').json()[KEY]
    assert len(saved.encode('utf-8')) <= 65_536
    assert len(parse_generation_profiles(saved)) == 64
    assert len(parse_generation_profiles(ProviderSettings.from_config().generation_profiles_json)) == 64
    padded = raw + ' ' * (65_536 - len(raw.encode('utf-8')))
    assert len(padded.encode('utf-8')) == 65_536
    assert config_api.post('/api/config', json={KEY: padded}).status_code == 200
    before = local_config.read_bytes()
    assert config_api.post('/api/config', json={KEY: padded + ' '}).status_code == 400
    assert local_config.read_bytes() == before
    assert len(parse_generation_profiles(config._read_env_file(local_config)[KEY])) == 64


@pytest.mark.parametrize('separator', ['\x85', '\u2028', '\u2029'])
def test_config_json_unicode_separators_roundtrip(local_config, config_api, separator):
    from app.clients.openai_compatible import parse_generation_profiles

    row = json.loads(profiles('alias'))[0]
    row['base_url'] = BASE + '/path' + separator + 'suffix'
    result = config_api.post('/api/config', json={KEY: json.dumps([row])})
    assert result.status_code == 200, result.text
    saved = config._read_env_file(local_config)[KEY]
    assert separator not in saved
    assert parse_generation_profiles(saved)[0] == row


def test_canonical_serialization_overflow_is_rejected_before_save(local_config, config_api):
    from app.clients.openai_compatible import parse_generation_profiles

    row = json.loads(profiles('alias'))[0]
    row['base_url'] = BASE + '/path' + '\x85' * 300 + 'end'
    rows = [dict(row, model=f'alias{index}') for index in range(64)]
    raw = json.dumps(rows, ensure_ascii=False, separators=(',', ':'))
    assert len(raw.encode('utf-8')) < 65_536
    assert len(raw.replace('\x85', '\\u0085').encode('utf-8')) > 65_536
    assert len(parse_generation_profiles(raw)) == 64
    before = local_config.read_bytes()
    result = config_api.post('/api/config', json={KEY: raw})
    assert result.status_code == 400
    assert local_config.read_bytes() == before
    assert ProviderSettings.from_config().generation_profiles_json == ''


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('model,temperature', [('alias', 'omit'), ('o3', 'zero')])
def test_management_three_probes_share_one_snapshot(protocol, model, temperature):
    from app.routes import tools_api

    row = json.loads(profiles(model, protocol))[0]
    row['temperature'] = temperature
    if protocol == 'chat_completions' and model == 'o3':
        row['token_parameter'] = 'max_tokens'
    wire = Wire('{"ok":true}')
    with settings(model, protocol, json.dumps([row])) as values:
        handler = wire.handler
        async def changing_handler(request):
            result = await handler(request)
            values[KEY] = '[]'
            return result
        wire.handler = changing_handler
        with patch.object(tools_api, 'FixedHostHttpClient', wire.factory):
            result = asyncio.run(tools_api._test_ai_provider(
                base_url=BASE, api_key='offline-fixture-only', protocol=protocol,
                model=model, timeout_seconds=12))
    assert result['ok'] is True
    assert len(wire.requests) == 3
    assert all(client._client.is_closed for client in wire.clients)
    for call, limit in zip(wire.requests, (32, 64, 16)):
        body = call['body']
        assert body[row['token_parameter']] == limit
        assert ('temperature' in body) is (temperature == 'zero')
        assert set(body) & {'max_tokens', 'max_completion_tokens', 'max_output_tokens'} == {row['token_parameter']}


@pytest.mark.parametrize('entry', ['native', 'structured'])
@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('model,temperature', [('alias', 'omit'), ('o3', 'zero')])
def test_explicit_profiles_all_protocols_reverse_legacy(entry, protocol, model, temperature):
    row = json.loads(profiles(model, protocol))[0]
    row['temperature'] = temperature
    if protocol == 'chat_completions' and model == 'o3':
        row['token_parameter'] = 'max_tokens'
    body = globals()[entry](model, protocol, json.dumps([row])).single_body()
    assert body[row['token_parameter']] == (1500 if entry == 'native' else 800)
    assert set(body) & {'max_tokens', 'max_completion_tokens', 'max_output_tokens'} == {row['token_parameter']}
    assert ('temperature' in body) is (temperature == 'zero')
    if temperature == 'zero':
        assert body['temperature'] == 0


def test_normalized_location_exact_model_and_alias_equivalence():
    row = json.loads(profiles('opaque/alias'))[0]
    row['base_url'] = BASE + '/chat/completions/'
    other = dict(row, model='different-alias')
    raw = json.dumps([row, other])
    bodies = [native(model, 'chat_completions', raw).single_body()
              for model in (row['model'], other['model'])]
    for body in bodies:
        assert body['max_completion_tokens'] == 1500 and 'temperature' not in body
        body.pop('model')
    assert bodies[0] == bodies[1]
    for model in ('opaque/ALIAS', 'opaque/alias ', ' opaque/alias', 'unrelated'):
        body = native('o3', 'chat_completions', raw, selected=model).single_body()
        assert body['model'] == model
        assert body['max_tokens'] == 1500 and body['temperature'] == 0


def invalid_profile_cases():
    row = json.loads(profiles('alias'))[0]
    cases = [None, [], {}, 1, 'null', '{}', '[null]', '[[]]', '[1]',
             ' ' * 65_537, 'secret-sentinel not JSON',
             '[' * 2000 + ']' * 2000,
             json.dumps([row] * 65), json.dumps([row, dict(row, base_url=BASE + '/responses')]),
             json.dumps([row]).replace('"model":', '"model":"duplicate", "model":')]
    for field, value in [('protocol', 'auto'), ('protocol', 'Responses'),
                         ('token_parameter', 'extra_body'), ('temperature', '1'),
                         ('temperature', 0), ('model', ' alias'), ('model', 'alias '),
                         ('model', ''), ('model', 'x' * 201), ('model', 'x\n'),
                         ('model', 'x\udfff'), ('base_url', 'https://api.example.com:secret-sentinel'),
                         ('base_url', 'https://secret-sentinel@api.example.com'),
                         ('base_url', 'https://api.example.com/?key=secret-sentinel'),
                         ('base_url', 'ftp://api.example.com'), ('base_url', 'http://localhost:0')]:
        cases.append(json.dumps([dict(row, **{field: value})]))
    cases.extend([json.dumps([dict(row, extra_body='secret-sentinel')]),
                  json.dumps([{k: v for k, v in row.items() if k != 'model'}]),
                  json.dumps([dict(row, protocol='responses', token_parameter='max_tokens')]),
                  json.dumps([dict(row, protocol='anthropic_messages')])])
    return cases


@pytest.mark.parametrize('bad', invalid_profile_cases(),
                         ids=[f'invalid-{index}' for index in range(len(invalid_profile_cases()))])
def test_invalid_configuration_no_save_no_post(bad, local_config, caplog):
    from app.routes import tools_api
    from app.routes.api import save_config

    before = local_config.read_bytes()
    result = save_config(cast(Request, admin_request()), {KEY: bad})
    assert isinstance(result, JSONResponse)
    assert result.status_code == 400
    assert b'secret-sentinel' not in result.body
    assert local_config.read_bytes() == before
    wire = Wire()
    with settings('alias', profile=bad), patch('app.clients.ai_recognition.FixedHostHttpClient', wire.factory), patch.object(tools_api, 'FixedHostHttpClient', wire.factory):
        with pytest.raises(ValueError) as caught:
            ProviderSettings.from_config()
        assert_safe_error(caught.value)
        client = AIRecognitionClient(api_url=BASE, model='alias', api_key='fixture', protocol='auto')
        with pytest.raises(ValueError) as caught:
            client._request_structured(system_prompt='offline', payload={}, schema_name='test', schema={})
        assert_safe_error(caught.value)
        with pytest.raises(ValueError) as caught:
            asyncio.run(tools_api._test_ai_provider(base_url=BASE, api_key='fixture',
                        protocol='auto', model='alias', timeout_seconds=12))
        assert_safe_error(caught.value)
    assert wire.requests == [] and wire.clients == []
    assert 'secret-sentinel' not in caplog.text


def test_profile_limits_and_empty_defaults(local_config, config_api):
    from dataclasses import FrozenInstanceError

    from app.clients.openai_compatible import parse_generation_profiles

    for raw in ('', '[]', ' \n\t ', '[]' + ' ' * 65_534):
        assert parse_generation_profiles(raw) == ()
    with pytest.raises(ValueError):
        parse_generation_profiles('[]' + ' ' * 65_535)
    with pytest.raises(ValueError):
        parse_generation_profiles('模' * 21_846)
    rows = [dict(json.loads(profiles('alias'))[0], model=f'alias{i}') for i in range(64)]
    assert len(parse_generation_profiles(json.dumps(rows))) == 64
    assert config_api.get('/api/config').json()[KEY] == '[]'
    value = ProviderSettings.from_config()
    with pytest.raises(FrozenInstanceError):
        setattr(value, 'generation_profiles_json', '[]')


def test_deployment_managed_fields_and_unknown_keys_unchanged(local_config, config_api, monkeypatch):
    import os

    before = local_config.read_bytes()
    with monkeypatch.context() as managed:
        managed.setattr(config, '_STARTUP_ENV_OVERRIDES', frozenset({KEY, 'AGENT_LLM_API_KEY'}))
        managed.setenv(KEY, profiles('managed-alias'))
        managed.setenv('AGENT_LLM_API_KEY', 'synthetic-managed-key')
        for data in ({KEY: '[]'}, {'AGENT_LLM_API_KEY': 'replacement'}, {'UNKNOWN_DEPLOYMENT_KEY': 'new'}):
            result = config_api.post('/api/config', json=data)
            assert result.status_code == (400 if 'UNKNOWN_DEPLOYMENT_KEY' in data else 409)
            assert local_config.read_bytes() == before
        assert json.loads(config_api.get('/api/config').json()[KEY])[0]['model'] == 'managed-alias'
        assert os.environ['AGENT_LLM_API_KEY'] == 'synthetic-managed-key'
        assert config_api.post('/api/config', json={'AGENT_LLM_API_KEY': '********'}).json() == {'success': True}
        assert local_config.read_bytes() == before


def test_cached_shared_client_refreshes_only_at_logical_request_start():
    from tests.support import isolated_test_database

    wire = Wire('{"ok":true}')
    raw = json.dumps([json.loads(profiles('alias', p))[0] for p in ('responses', 'chat_completions')])
    with isolated_test_database(), settings('alias', 'auto', raw) as values:
        handler = wire.handler
        async def changing_handler(request):
            success = await handler(request)
            if len(wire.requests) == 1:
                values[KEY] = '[]'
                return httpx.Response(404, text='responses api is not supported')
            return success
        wire.handler = changing_handler
        with patch('app.clients.ai_recognition.FixedHostHttpClient', wire.factory):
            client = AIRecognitionClient(api_url=BASE, model='alias', api_key='fixture', protocol='auto')
            for _ in range(2):
                assert client._request_structured(system_prompt='offline', payload={},
                    schema_name='test', schema={}) == {'ok': True}
    assert [call['protocol'] for call in wire.requests] == ['responses', 'chat_completions', 'responses']
    first, fallback, refreshed = [call['body'] for call in wire.requests]
    assert first['max_output_tokens'] == fallback['max_completion_tokens'] == refreshed['max_output_tokens'] == 800
    assert 'temperature' not in first and 'temperature' not in fallback
    assert refreshed['temperature'] == 0
    assert all(client._client.is_closed for client in wire.clients)


def test_native_retry_and_fallback_match_attempt_protocol_with_fixed_snapshot():
    from app.agent.kernel import provider_model as pm

    wire = Wire()
    raw = json.dumps([json.loads(profiles('alias', p))[0] for p in ('responses', 'chat_completions')])
    with settings('alias', 'auto', raw) as values:
        handler = wire.handler
        async def changing_handler(request):
            success = await handler(request)
            values[KEY] = 'malformed configuration after request began'
            if len(wire.requests) == 1:
                return httpx.Response(503, text='temporary', headers={'retry-after': '0'})
            if len(wire.requests) == 2:
                return httpx.Response(404, text='responses api is not supported')
            return success
        wire.handler = changing_handler
        async def run():
            adapter = OpenAICompatibleModelAdapter(ProviderSettings.from_config(), client_factory=wire.factory)
            return [e async for e in adapter.stream(ModelRequest(system_prompt='offline', messages=(), tools=()),
                                                   cancellation=CancellationToken())]
        with patch.object(pm, '_MODEL_RETRY_DELAY_SECONDS', 0):
            events = asyncio.run(run())
    assert events[-1].type.value == 'finish'
    assert [call['protocol'] for call in wire.requests] == ['responses', 'responses', 'chat_completions']
    assert wire.requests[0]['body'] == wire.requests[1]['body']
    assert wire.requests[1]['body']['max_output_tokens'] == wire.requests[2]['body']['max_completion_tokens'] == 1500
    assert all('temperature' not in call['body'] for call in wire.requests)
    assert all(client._client.is_closed for client in wire.clients)


def test_telegram_callback_real_kv_next_turn_title_and_web_isolation():
    from types import SimpleNamespace
    from unittest.mock import Mock

    import telebot

    from app.agent.kernel.capabilities import ToolCatalog
    from app.agent.kernel.pipeline import ToolPipeline
    from app.agent.kernel.ports.mediaflux_policy import select_channel_model
    from app.agent.kernel.session import AgentSession
    from app.agent.kernel.state import InMemorySessionStateStore
    from app.agent.kernel.state_types import AgentInput
    from app.bot import agent_adapter
    from app.modules import telegram_model_preferences as prefs
    from app.modules.telegram_topic_routing import _generate_private_topic_title
    from tests.support import isolated_test_database

    wire = Wire('Topic')
    owner = agent_adapter.telegram_agent_owner('-100123', '7')
    raw = json.dumps([json.loads(profiles(model))[0] for model in ('alias-one', 'alias-two')])
    with isolated_test_database(), settings('gpt-5', 'chat_completions', raw) as values:
        values.update({'AGENT_ENABLED': '1', 'TG_AGENT_ENABLED': '1', 'TG_CHAT_ID': '-100123',
                       'TG_AGENT_ALLOWED_USER_IDS': '7'})
        bot = Mock()
        bot.edit_message_text.return_value = True
        def select(model):
            token = prefs.create_model_callback(owner, 'profile-topic', 41, action='select', model_id=model)
            call = SimpleNamespace(id='callback', data='tgm:' + token, from_user=SimpleNamespace(id=7),
                message=SimpleNamespace(chat=SimpleNamespace(id=-100123), message_id=1, message_thread_id=41))
            agent_adapter.handle_agent_model_callback(bot, call, telebot)
            assert prefs.get_telegram_model_preference(owner, 'profile-topic') == model
            assert model in db.kv_get(prefs._key(owner, 'profile-topic'))
        state = InMemorySessionStateStore()
        catalog = ToolCatalog([])
        session = AgentSession(model=OpenAICompatibleModelAdapter(ProviderSettings.from_config(), client_factory=wire.factory),
            catalog=catalog, pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state,
             model_selector=select_channel_model)
        async def turn(channel='telegram', principal=owner, session_id='profile-topic'):
            events = [e async for e in session.run(AgentInput(message='hello', owner=principal,
                session_id=session_id, channel=channel))]
            assert events[-1].type.value == 'turn.completed', [(e.type.value, e.payload) for e in events]
        asyncio.run(turn())
        select('alias-one')
        asyncio.run(turn())
        select('alias-two')
        asyncio.run(turn())
        asyncio.run(turn('api'))
        asyncio.run(turn(session_id='other-session'))
        asyncio.run(turn(principal='tg:v1:-100123\x1f8'))
        with patch.dict(OpenAICompatibleModelAdapter.__init__.__kwdefaults__, client_factory=wire.factory):
            assert asyncio.run(_generate_private_topic_title(owner=owner, session_id='profile-topic',
                                                            first_user_message='hello')) == 'Topic'
        assert prefs.get_telegram_model_preference(owner, 'other-session') == ''
        assert prefs.get_telegram_model_preference('tg:v1:-100123\x1f8', 'profile-topic') == ''
    assert [call['body']['model'] for call in wire.requests] == [
        'gpt-5', 'alias-one', 'alias-two', 'gpt-5', 'gpt-5', 'gpt-5', 'alias-two']
    for index, call in enumerate(wire.requests):
        body = call['body']
        if index in (1, 2, 6):
            assert body['max_completion_tokens'] == (96 if index == 6 else 6000)
            assert 'temperature' not in body and 'max_tokens' not in body
        else:
            assert body['max_tokens'] == 6000 and body['temperature'] == 0
    assert all(client._client.is_closed for client in wire.clients)


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('unsupported', [False, True])
def test_explicit_profile_preserves_bound_native_auto(protocol, unsupported):
    from tests.test_agent_protocol_fallback import (
        assert_native_payload,
        http_error,
        native_messages,
        wire,
    )

    messages = native_messages(protocol)
    assert messages[0].provider_state is not None
    original = messages[0].provider_state.layout_json
    result = wire([http_error(404) if unsupported else None, None],
                  messages=messages, generation_profiles_json=profiles('fixture', protocol))
    assert [call['protocol'] for call in result['requests']] == [protocol]
    assert bool(result['error']) is unsupported
    body = result['requests'][0]['body']
    assert_native_payload(protocol, body)
    assert 'temperature' not in body
    assert messages[0].provider_state.layout_json == original


@pytest.mark.parametrize('status,text,attempts', [
    (401, 'responses api is not supported', 1),
    (403, 'forbidden', 1),
    (429, 'insufficient quota', 1),
    (404, '{"error":{"code":"model_not_found","message":"responses api is not supported"}}', 1),
    (400, '{"error":{"message":"unknown business validation"}}', 1),
    (400, 'responses api is not supported' + ' ' * 8192 + ' insufficient quota', 1),
    (503, 'temporary', 2),
])
def test_profiles_do_not_grant_business_or_exhausted_fallback(status, text, attempts):
    from tests.test_agent_protocol_fallback import http_error, wire

    raw = json.dumps([json.loads(profiles('fixture', p))[0] for p in PROTOCOLS])
    result = wire([http_error(status, text)], generation_profiles_json=raw)
    assert result['error']
    assert [call['protocol'] for call in result['requests']] == ['responses'] * attempts
    assert all('temperature' not in call['body'] for call in result['requests'])


@pytest.mark.parametrize('mode', ['cancel', 'deadline'])
@pytest.mark.parametrize('fallback', [False, True])
@pytest.mark.parametrize('close_fault', [None, httpx.ReadError, ValueError])
def test_profile_cancel_deadline_with_real_cleanup(mode, fallback, close_fault):
    from app.agent.kernel.provider_model import ModelProviderError
    from tests.test_agent_protocol_fallback import SECRET, error_chain, http_error, wire

    action = dict(status=200, data=b'', headers={'content-type': 'text/event-stream'},
                  task_cancel=mode == 'cancel', hanging=mode == 'deadline', close_fault=close_fault)
    raw = json.dumps([json.loads(profiles('fixture', p))[0] for p in PROTOCOLS])
    actions = ([http_error(404)] if fallback else []) + [action, None]
    result = wire(actions, generation_profiles_json=raw, deadline=0.02 if mode == 'deadline' else 60)
    assert [call['protocol'] for call in result['requests']] == (
        ['responses', 'chat_completions'] if fallback else ['responses'])
    assert not result['events']
    assert all('temperature' not in call['body'] for call in result['requests'])
    if mode == 'cancel':
        assert isinstance(result['error'], asyncio.CancelledError)
    else:
        assert isinstance(result['error'], ModelProviderError)
        assert str(result['error']) == 'Provider 请求超时'
        assert isinstance(result['error'].__cause__, TimeoutError)
    assert all(SECRET not in repr(error) for error in error_chain(result['error']))


class RouteWire(Wire):
    """Real restricted client; endpoint-like gateway prefixes are legal routes."""
    async def handler(self, request):
        protocol = next(p for p, path in PATHS.items()
                        if request.url.path.endswith(path.removeprefix('/v1')))
        assert request.method == 'POST'
        assert request.headers['host'] == 'api.example.com'
        header = 'x-api-key' if protocol == 'anthropic_messages' else 'authorization'
        key = config.get('AGENT_LLM_API_KEY')
        assert request.headers[header] == (key if protocol == 'anthropic_messages' else 'Bearer ' + key)
        self.requests.append({'protocol': protocol, 'path': request.url.path,
                              'body': json.loads(request.content)})
        return httpx.Response(200, json=response(protocol, '{"ok":true}'))


def configured_route_wire(entry='native'):
    wire = RouteWire()
    async def run():
        adapter = OpenAICompatibleModelAdapter(ProviderSettings.from_config(), client_factory=wire.factory)
        return [event async for event in adapter.stream(
            ModelRequest(system_prompt='offline', messages=(), tools=()), cancellation=CancellationToken())]
    if entry == 'native':
        assert asyncio.run(run())[-1].type.value == 'finish'
    elif entry == 'shared':
        from tests.support import isolated_test_database
        with isolated_test_database(), patch('app.clients.ai_recognition.FixedHostHttpClient', wire.factory):
            client = AIRecognitionClient(api_url=config.get('AGENT_LLM_API_URL'),
                api_key=config.get('AGENT_LLM_API_KEY'), model=config.get('AGENT_LLM_MODEL'),
                protocol=config.get('AGENT_LLM_PROTOCOL'))
            assert client._request_structured(system_prompt='offline', payload={},
                schema_name='probe', schema={}) == {'ok': True}
    else:
        from app.routes import tools_api
        with patch.object(tools_api, 'FixedHostHttpClient', wire.factory):
            result = asyncio.run(tools_api._test_ai_provider(base_url=config.get('AGENT_LLM_API_URL'),
                api_key=config.get('AGENT_LLM_API_KEY'), model=config.get('AGENT_LLM_MODEL'),
                protocol=config.get('AGENT_LLM_PROTOCOL'), timeout_seconds=12))
            assert result['ok']
    assert wire.clients and all(client._client.is_closed for client in wire.clients)
    assert len(wire.requests) == (3 if entry == 'management' else 1)
    return wire


def assert_route_generation(calls, protocol, path, matched, entry='native'):
    limits = (32, 64, 16) if entry == 'management' else (800,) if entry == 'shared' else (1500,)
    assert len(calls) == len(limits)
    token = ('max_output_tokens' if protocol == 'responses' else
             'max_completion_tokens' if protocol == 'chat_completions' and matched else 'max_tokens')
    for call, limit in zip(calls, limits):
        assert call['protocol'] == protocol and call['path'] == path
        body = call['body']
        assert body['model'] == 'tenant/opaque-alias'
        assert {key: body[key] for key in ('max_tokens', 'max_completion_tokens', 'max_output_tokens', 'temperature')
                if key in body} == ({token: limit} if matched else {token: limit, 'temperature': 0})


@pytest.mark.parametrize('entry', ['native', 'shared', 'management'])
@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('mode', ['matched', 'different-route', 'legacy'])
def test_repeated_endpoint_route_identity_on_wire(entry, protocol, mode):
    from app.clients.openai_compatible import (
        normalize_provider_location,
        parse_generation_profiles,
    )

    suffix = PATHS[protocol].removeprefix('/v1')
    raw_url = BASE + suffix + suffix
    row = json.loads(profiles('tenant/opaque-alias', protocol))[0]
    row['base_url'] = raw_url if mode == 'matched' else BASE + suffix
    raw = json.dumps([row]) if mode != 'legacy' else ''
    location = normalize_provider_location(raw_url)
    assert location.base_url == BASE + suffix
    assert location.endpoint(protocol) == raw_url
    if mode != 'legacy':
        assert (parse_generation_profiles(raw)[0]['base_url'] == location.base_url) is (mode == 'matched')
    with settings(row['model'], protocol, raw) as values:
        values['AGENT_LLM_API_URL'] = raw_url
        calls = configured_route_wire(entry).requests
    assert_route_generation(calls, protocol, '/v1' + suffix + suffix, mode == 'matched', entry)


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_repeated_endpoint_equivalent_identity_duplicate_rejected(protocol, local_config, config_api):
    from app.clients.openai_compatible import (
        normalize_provider_location,
        parse_generation_profiles,
    )

    suffix = PATHS[protocol].removeprefix('/v1')
    row = json.loads(profiles('tenant/opaque-alias', protocol))[0]
    row['base_url'] = BASE + suffix + '/chat/completions/'
    other = dict(row, base_url=BASE + suffix + '/responses')
    assert row['base_url'] != other['base_url']
    assert normalize_provider_location(row['base_url']).base_url == normalize_provider_location(other['base_url']).base_url
    raw = json.dumps([row, other])
    with pytest.raises(ValueError) as caught:
        parse_generation_profiles(raw)
    assert_safe_error(caught.value)
    before = local_config.read_bytes()
    assert config_api.post('/api/config', json={KEY: raw}).status_code == 400
    assert local_config.read_bytes() == before


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_api_saved_profile_does_not_cross_normalized_route(protocol, local_config, config_api):
    from app.clients.openai_compatible import (
        normalize_provider_location,
        parse_generation_profiles,
    )

    config.set_and_save({'AGENT_LLM_PROTOCOL': protocol})
    row = json.loads(profiles('tenant/opaque-alias', protocol))[0]
    row['base_url'] = BASE + '/responses/responses'
    raw = json.dumps([row])
    expected = parse_generation_profiles(raw)
    assert expected[0]['base_url'] != normalize_provider_location(config.get('AGENT_LLM_API_URL')).base_url
    assert_route_generation(configured_route_wire().requests, protocol, PATHS[protocol], False)
    result = config_api.post('/api/config', json={KEY: raw})
    assert result.status_code == 200, result.text
    saved = config_api.get('/api/config').json()[KEY]
    assert config._read_env_file(local_config)[KEY] == saved
    # Wire oracle first: saving an unrelated profile must not change this route.
    assert_route_generation(configured_route_wire().requests, protocol, PATHS[protocol], False)
    assert parse_generation_profiles(saved) == expected


_COLD_PROFILE_RELOAD = '''
import json
import os
from pathlib import Path
import socket
import sys
from contextlib import ExitStack
from unittest.mock import patch


def deny(*args, **kwargs):
    raise AssertionError('cold profile fixture forbids real sockets and DNS')


def main():
    root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(root))
    with ExitStack() as stack:
        for obj, names in ((socket.socket, ('connect', 'connect_ex', 'sendto')),
                           (socket, ('create_connection', 'getaddrinfo'))):
            for name in names:
                stack.enter_context(patch.object(obj, name, deny))
        import tests
        import app
        from app import config
        from app.clients import openai_compatible as oc
        from tests import test_agent_generation_profiles as fixture
        for module in (tests, app, oc, fixture):
            assert Path(module.__file__).resolve().is_relative_to(root)
        for key in tuple(os.environ):
            if key.startswith('AGENT_'):
                del os.environ[key]
        config.ENV_FILE = Path(sys.argv[2])
        config._cache = None
        raw = fixture.ProviderSettings.from_config().generation_profiles_json
        wire = fixture.configured_route_wire()
        print('COLD_PROFILE=' + json.dumps({'raw': raw, 'rows': oc.parse_generation_profiles(raw),
                                          'calls': wire.requests}))


if __name__ == '__main__':
    main()
'''


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('matched', [True, False])
def test_profile_identity_survives_repeated_save_get_post_cold_load(protocol, matched, local_config, config_api):
    import subprocess
    import sys
    from pathlib import Path

    from app.clients.openai_compatible import parse_generation_profiles

    suffix = PATHS[protocol].removeprefix('/v1')
    raw_url = BASE + suffix + suffix
    config.set_and_save({'AGENT_LLM_API_URL': raw_url if matched else BASE,
                         'AGENT_LLM_PROTOCOL': protocol})
    original = config._read_env_file(local_config)
    row = json.loads(profiles('tenant/opaque-alias', protocol))[0]
    row['base_url'] = raw_url
    submitted = json.dumps([row])
    expected = parse_generation_profiles(submitted)
    path = '/v1' + suffix + suffix if matched else PATHS[protocol]
    first_saved = None
    for _ in range(3):
        result = config_api.post('/api/config', json={KEY: submitted, 'AGENT_LLM_API_KEY': '********'})
        assert result.status_code == 200, result.text
        saved = config_api.get('/api/config').json()[KEY]
        assert config._read_env_file(local_config)[KEY] == saved
        assert {key: config._read_env_file(local_config)[key] for key in original} == original
        assert_route_generation(configured_route_wire().requests, protocol, path, matched)
        assert parse_generation_profiles(saved) == expected
        assert len(saved.encode('utf-8')) <= 65_536
        if first_saved is None:
            first_saved = saved
        assert saved == first_saved
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run([sys.executable, '-c', _COLD_PROFILE_RELOAD, str(root), str(local_config)],
            cwd=root, capture_output=True, text=True, timeout=30)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        line = next(line for line in completed.stdout.splitlines() if line.startswith('COLD_PROFILE='))
        loaded = json.loads(line.split('=', 1)[1])
        assert loaded['raw'] == saved and loaded['rows'] == list(expected)
        assert_route_generation(loaded['calls'], protocol, path, matched)
        submitted = saved


REASONING_FIELDS = {'reasoning', 'reasoning_effort', 'thinking', 'output_config'}
REASONING_SHAPES = {
    'responses': {'reasoning': {'effort': 'high'}},
    'chat_completions': {'reasoning_effort': 'high'},
    'anthropic_messages': {'thinking': {'type': 'adaptive'}, 'output_config': {'effort': 'high'}},
}
NATIVE_REASONING_SHAPES = {**REASONING_SHAPES, 'responses': {'reasoning': {'effort': 'high', 'summary': 'auto'}}}


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_reasoning_effort_reaches_native_tool_loop_without_sampling(protocol):
    body = native('tenant/opaque-alias', protocol, effort='high').single_body()
    assert {key: body[key] for key in REASONING_FIELDS & body.keys()} == NATIVE_REASONING_SHAPES[protocol]
    assert 'temperature' not in body


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_unset_reasoning_effort_keeps_legacy_request_shape(protocol):
    body = native('tenant/opaque-alias', protocol).single_body()
    assert not REASONING_FIELDS & body.keys()
    assert body['temperature'] == 0


def test_reasoning_effort_overrides_profile_sampling_but_keeps_token_parameter():
    row = json.loads(profiles('tenant/opaque-alias', 'chat_completions'))[0]
    row['temperature'] = 'zero'
    body = native('tenant/opaque-alias', 'chat_completions', json.dumps([row]), effort='low').single_body()
    assert body['reasoning_effort'] == 'low' and 'temperature' not in body
    assert body['max_completion_tokens'] == 1500 and 'max_tokens' not in body


@pytest.mark.parametrize('stored,expected', [
    ('', ''), ('low', 'low'), (' HIGH ', 'high'), ('max', ''), ('none', ''),
])
def test_provider_settings_treat_invalid_stored_effort_as_unset(stored, expected):
    with settings(effort=stored):
        assert ProviderSettings.from_config().reasoning_effort == expected


def test_reasoning_effort_rejects_values_outside_shared_protocol_set():
    from app.clients.openai_compatible import native_tool_request_body

    with pytest.raises(ValueError):
        ProviderSettings(api_url=BASE, model='alias', reasoning_effort='xhigh')
    with pytest.raises(ValueError):
        native_tool_request_body(protocol='responses', model='alias', system_prompt='', history=[],
                                 tools=[], max_tokens=64, reasoning_effort='max')


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('effort', ['', 'low', 'medium', 'high'])
def test_connection_probe_preserves_user_reasoning_effort(protocol, effort):
    from app.routes import tools_api

    wire = Wire('{"ok":true}')
    with settings('tenant/opaque-alias', protocol), patch.object(tools_api, 'FixedHostHttpClient', wire.factory):
        asyncio.run(tools_api._test_ai_provider(
            base_url=BASE, api_key='offline-fixture-only', protocol=protocol,
            model='tenant/opaque-alias', timeout_seconds=12, reasoning_effort=effort))
    structured_body, tool_body, stream_body = (call['body'] for call in wire.requests)
    # 只有工具循环探测携带推理参数；结构化输出与纯文本流式不受影响。
    for body in (structured_body, stream_body):
        assert not {'reasoning', 'reasoning_effort', 'thinking'} & body.keys()
        assert 'effort' not in body.get('output_config', {})
    if effort:
        expected = {key: (dict(value, effort=effort) if isinstance(value, dict) and 'effort' in value else
                          effort if key == 'reasoning_effort' else value)
                    for key, value in REASONING_SHAPES[protocol].items()}
        assert {key: tool_body[key] for key in REASONING_FIELDS & tool_body.keys()} == expected
        assert 16_000 in tool_body.values()
        assert 'temperature' not in tool_body
    else:
        assert not REASONING_FIELDS & tool_body.keys()
        assert 64 in tool_body.values()


class HighReasoningWire(Wire):
    """模拟只接受 high、且需先消耗推理额度再输出工具调用的服务。"""
    def __init__(self, *, tool_delay=0):
        super().__init__('{"ok":true}')
        self.tool_delay = tool_delay

    async def handler(self, request):
        reply = await super().handler(request)
        protocol, body = self.requests[-1]['protocol'], self.requests[-1]['body']
        if not body.get('tools'):
            return reply
        reasoning = {key: body[key] for key in REASONING_FIELDS & body.keys()}
        if reasoning not in (REASONING_SHAPES[protocol], NATIVE_REASONING_SHAPES[protocol]):
            return httpx.Response(400, json={'error': {'message': 'Only high reasoning effort is supported'}})
        budget = max(body.get(key, 0) for key in ('max_tokens', 'max_completion_tokens', 'max_output_tokens'))
        if budget < 4096:
            return httpx.Response(400, json={'error': {'message': 'Insufficient output budget for reasoning'}})
        await asyncio.sleep(self.tool_delay)
        name = 'mediaflux_connectivity_probe'
        envelopes = {
            'chat_completions': {'choices': [{'finish_reason': 'tool_calls', 'message': {
                'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call-probe',
                    'type': 'function', 'function': {'name': name, 'arguments': '{}'}}]}}]},
            'responses': {'status': 'completed', 'output': [{'type': 'function_call',
                'id': 'item-probe', 'call_id': 'call-probe', 'name': name, 'arguments': '{}'}]},
            'anthropic_messages': {'stop_reason': 'tool_use', 'content': [{'type': 'tool_use',
                'id': 'call-probe', 'name': name, 'input': {}}]},
        }
        if protocol == 'anthropic_messages':
            # 推理正文可能超过旧探测的 64 KiB，仍应解析出随后的工具调用。
            envelopes[protocol]['content'].insert(0, {'type': 'thinking',
                'thinking': 'synthetic reasoning ' * 4096, 'signature': 'offline-fixture-only'})
        return httpx.Response(200, json=envelopes[protocol])


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('effort_source', ['draft', 'stored'])
def test_high_only_provider_accepts_connection_route_and_native_adapter(protocol, effort_source, local_config, config_api):
    from app.routes import tools_api

    wire = HighReasoningWire()
    model = 'tenant/opaque-alias'
    raw = profiles(model, protocol)
    before = local_config.read_bytes()
    data = {'base_url': BASE, 'api_key': 'offline-fixture-only', 'protocol': protocol,
            'model': model, 'timeout_seconds': 12}
    if effort_source == 'draft':
        data['reasoning_effort'] = ' HIGH '
    with settings(model, protocol, raw, effort='low' if effort_source == 'draft' else 'high'), \
            patch.object(tools_api, 'FixedHostHttpClient', wire.factory):
        result = config_api.post('/api/tools/ai/test', json=data)
    assert result.status_code == 200, result.text
    assert result.json()['capabilities']['tool_calling'] is True
    assert wire.clients[0]._client.timeout.read == 12
    # 同一服务确实拒绝 low，不能靠降级得到探测成功。
    with settings(model, protocol, raw), patch.object(tools_api, 'FixedHostHttpClient', wire.factory):
        rejected = config_api.post('/api/tools/ai/test', json={**data, 'reasoning_effort': 'low'})
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()['capabilities']['tool_calling'] is False
    assert local_config.read_bytes() == before

    async def run():
        adapter = OpenAICompatibleModelAdapter(ProviderSettings.from_config(), client_factory=wire.factory)
        request = ModelRequest(system_prompt='offline', messages=(ModelMessage('user', 'call probe'),),
            tools=({'name': 'mediaflux_connectivity_probe', 'parameters': {
                'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}},),
            max_output_tokens=16_000)
        return [event async for event in adapter.stream(request, cancellation=CancellationToken())]

    with settings(model, protocol, raw, effort='high'):
        events = asyncio.run(run())
    calls = [event.tool_call for event in events if event.type.value == 'tool_call_completed']
    assert len(calls) == 1 and calls[0] is not None
    assert calls[0].name == 'mediaflux_connectivity_probe' and calls[0].arguments == {}
    assert events[-1].type.value == 'finish'
    tool_bodies = [call['body'] for call in wire.requests if call['body'].get('tools')]
    assert len(tool_bodies) == 3
    for body, expected in ((tool_bodies[0], REASONING_SHAPES[protocol]),
                           (tool_bodies[-1], NATIVE_REASONING_SHAPES[protocol])):
        assert {key: body[key] for key in REASONING_FIELDS & body.keys()} == expected
        assert 'temperature' not in body
        assert set(body) & {'max_tokens', 'max_completion_tokens', 'max_output_tokens'} == {
            json.loads(raw)[0]['token_parameter']}
    assert all(client._client.is_closed for client in wire.clients)


@pytest.mark.parametrize('delay,timeout_seconds,status', [(4.1, 12, 200), (2.1, 2, 504)])
def test_high_reasoning_probe_uses_configured_deadline(delay, timeout_seconds, status, config_api):
    from app.routes import tools_api

    model, protocol = 'tenant/opaque-alias', 'responses'
    wire = HighReasoningWire(tool_delay=delay)
    with settings(model, protocol, profiles(model, protocol), effort='high'), \
            patch.object(tools_api, 'FixedHostHttpClient', wire.factory):
        result = config_api.post('/api/tools/ai/test', json={'timeout_seconds': timeout_seconds})
    assert result.status_code == status, result.text
    if status == 200:
        assert result.json()['capabilities']['tool_calling'] is True
    assert wire.clients and all(client._client.is_closed for client in wire.clients)
