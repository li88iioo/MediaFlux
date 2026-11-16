"""Positive supported-route contracts; no proposed budget configuration."""
import asyncio
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import telebot

from app import config
from app import database as db
from app.agent.errors import ToolPipelineError
from app.agent.kernel import bootstrap
from app.agent.kernel.model import ModelRequest
from app.agent.kernel.provider_model import (
    OpenAICompatibleModelAdapter,
    ProviderSettings,
)
from app.agent.kernel.state_types import AgentInput, CancellationToken
from app.agent.model_catalog import provider_channel_fingerprint
from app.bot import agent_adapter
from app.modules import telegram_model_preferences as prefs
from tests import test_agent_generation_profiles as profile_support

# Reuse the real config publisher, isolated API and offline transport fixtures.
local_config = profile_support.local_config
config_api = profile_support.config_api
no_external_io = profile_support.no_external_io
Wire = profile_support.Wire
BASE = profile_support.BASE

MODEL = 'tenant/opaque-alias'


@pytest.mark.parametrize('protocol', profile_support.PROTOCOLS)
def test_model_switch_is_fresh_inside_retained_snapshot(local_config, monkeypatch, protocol):
    config.set_and_save({'AGENT_LLM_PROTOCOL': protocol})
    wire = Wire()
    handler = wire.handler
    async def close_in_flight(request):
        response = await handler(request)
        config.set_and_save({'AGENT_LLM_ENABLED': '0'})
        return response
    wire.handler = close_in_flight
    adapter = OpenAICompatibleModelAdapter(ProviderSettings.from_config(), client_factory=wire.factory)
    snapshot = config.capture_runtime_config_snapshot()

    async def request():
        return [e async for e in adapter.stream(
            ModelRequest(system_prompt='offline', messages=(), tools=()), cancellation=CancellationToken())]

    events = asyncio.run(request())
    assert events[-1].type.value == 'finish'  # 已发出的请求正常收尾。
    with config.use_runtime_config_snapshot(snapshot):
        assert config.get_bool('AGENT_LLM_ENABLED') is True
        with pytest.raises(ToolPipelineError) as denied:
            asyncio.run(request())
        assert denied.value.code == 'model_disabled'
        assert len(wire.requests) == 1
        # Deployment overrides remain authoritative even inside an old context.
        monkeypatch.setitem(config._SYSTEM_ENV_SNAPSHOT, 'AGENT_LLM_ENABLED', '1')
        asyncio.run(request())
        assert len(wire.requests) == 2
    assert all(c._client.is_closed for c in wire.clients)


@pytest.mark.parametrize('boundary', ['default', 'first', 'retry', 'fallback'])
def test_model_switch_blocks_next_http_attempt(local_config, boundary):
    from tests import test_agent_protocol_fallback as protocol

    async def close_during_retry(_delay):
        config.set_and_save({'AGENT_LLM_ENABLED': '0'})

    close = protocol.Body.aclose
    async def close_after_response(body):
        await close(body)
        config.set_and_save({'AGENT_LLM_ENABLED': '0'})

    if boundary == 'default':
        config.write_env_file(local_config, {}, replace=True)
        result = protocol.wire([])
    elif boundary == 'first':
        config.set_and_save({'AGENT_LLM_ENABLED': '0'})
        result = protocol.wire([])
    elif boundary == 'retry':
        result = protocol.wire([protocol.http_error(503, 'unavailable'), None], sleep=close_during_retry)
    else:
        with patch.object(protocol.Body, 'aclose', close_after_response):
            result = protocol.wire([protocol.http_error(), None])
    assert isinstance(result['error'], ToolPipelineError)
    assert result['error'].code == 'model_disabled'
    assert len(result['requests']) == (0 if boundary in {'default', 'first'} else 1)


def test_disabled_model_preserves_default_topic_and_naming_eligibility(local_config):
    from app.modules import telegram_topic_routing as routing
    from tests.test_telegram_topic_naming import _FakeBot, _event_message

    owner = 'tg:v1:501\x1f501'
    routing.set_topic_mode(owner, True)
    routing.record_private_topic_service_message(owner, _event_message(
        created=SimpleNamespace(is_name_implicit=True)))
    key = routing._topic_naming_key(owner, 501, 901)
    bot, wire = _FakeBot(), Wire('电影推荐')
    snapshot = config.capture_runtime_config_snapshot()

    def name():
        return asyncio.run(routing.auto_name_private_topic(bot, owner=owner, session_id='topic',
            chat_id=501, chat_type='private', thread_id=901, topic_open=True, first_user_message='推荐电影'))

    config.set_and_save({'AGENT_LLM_ENABLED': '0'})
    with patch.dict(OpenAICompatibleModelAdapter.__init__.__kwdefaults__, client_factory=wire.factory):
        with config.use_runtime_config_snapshot(snapshot), patch.object(ProviderSettings, 'from_config') as settings:
            assert name() is False
            settings.assert_not_called()
        assert wire.requests == bot.edits == []
        assert routing._topic_naming_status(key) == 'eligible'
        config.set_and_save({'AGENT_LLM_ENABLED': '1'})
        assert name() is True
    assert len(wire.requests) == len(bot.edits) == 1
    assert bot.edits[0]['name'] == '电影推荐'
    assert routing._topic_naming_status(key) == 'auto'


@pytest.fixture
def harness(local_config):
    wire = Wire('Topic')
    original = wire.handler
    async def capture(request):
        response = await original(request)
        wire.requests[-1]['host'] = request.headers['host']
        credential = request.headers.get('authorization') or request.headers.get('x-api-key', '')
        wire.requests[-1]['auth_sha256'] = hashlib.sha256(credential.encode()).hexdigest()
        return response
    wire.handler = capture
    config.set_and_save({'TG_AGENT_ENABLED': '1', 'TG_CHAT_ID': '-100123', 'TG_AGENT_ALLOWED_USER_IDS': '7'})
    with patch.dict(OpenAICompatibleModelAdapter.__init__.__kwdefaults__, client_factory=wire.factory):
        yield wire
    assert all(c._client.is_closed for c in wire.clients)


def source(number=1):
    return SimpleNamespace(chat=SimpleNamespace(id=-100123, type='supergroup'),
        from_user=SimpleNamespace(id=7), message_id=number, message_thread_id=41,
        text='hello ' + str(number), reply_to_message=None, quote=None)


def scope():
    return agent_adapter._session_for_source('-100123', '7', source())[:2]


def select(model):
    owner, session_id = scope()
    token = prefs.create_model_callback(owner, session_id, 41, action='select', model_id=model)
    call = SimpleNamespace(id='callback', data='tgm:' + token, from_user=SimpleNamespace(id=7), message=source())
    bot = Mock()
    bot.edit_message_text.return_value = True
    agent_adapter.handle_agent_model_callback(bot, call, telebot)
    assert prefs.get_telegram_model_preference(owner, session_id) == model
    stored = json.loads(db.kv_get(prefs._key(owner, session_id)))
    assert stored == {'channel': provider_channel_fingerprint(config.get_many(("AGENT_LLM_API_URL", "AGENT_LLM_PROTOCOL", "AGENT_LLM_API_KEY"))), 'model_id': model}
    assert 'synthetic' not in json.dumps(stored)


class Bot:
    def __init__(self, ready=None, release=None):
        self.ready, self.release = ready, release
        self.blocked = False
        self.retained_runtime = None
        self.call_path = []

    def send_chat_action(self, *args, **kwargs):
        if self.ready is not None and not self.blocked:
            import inspect
            currentframe_result = inspect.currentframe()
            assert currentframe_result is not None
            frame = currentframe_result.f_back
            while frame is not None:
                self.call_path.append(frame.f_code.co_name)
                if frame.f_code.co_name == '_execute_query':
                    self.retained_runtime = frame.f_locals['runtime']
                    break
                frame = frame.f_back
            assert self.retained_runtime is not None
            self.blocked = True
            self.ready.set()
            assert self.release is not None
            assert self.release.wait(10), 'handshake release timeout'
        return True

    def send_message(self, *args, **kwargs):
        return SimpleNamespace(message_id=500)

    def edit_message_text(self, *args, **kwargs):
        return True

    def edit_message_reply_markup(self, *args, **kwargs):
        return True

    def reply_to(self, *args, **kwargs):
        return SimpleNamespace(message_id=501)


def entry(bot, number):
    assert agent_adapter.handle_agent_message(bot, telebot, source(number)) is True


def turn(session, channel='telegram', number=50):
    owner, session_id = scope()
    async def run():
        return [e async for e in session.run(AgentInput(message='hello', owner=owner,
            session_id=session_id, request_id='probe-' + str(number), channel=channel))]
    events = asyncio.run(run())
    assert events[-1].type.value == 'turn.completed', [(e.type.value, e.payload) for e in events]


def output(body):
    fields = [k for k in ('max_tokens', 'max_output_tokens', 'max_completion_tokens') if k in body]
    assert len(fields) == 1
    return body[fields[0]]


@pytest.mark.parametrize('changed', [
    {'AGENT_LLM_API_URL': 'https://other.example.com/v1'},
    {'AGENT_LLM_API_KEY': 'synthetic-replacement-key'},
    {'AGENT_LLM_PROTOCOL': 'responses'},
], ids=['url', 'key', 'protocol'])
def test_official_entry_retained_runtime_uses_own_channel(harness, config_api, changed):
    """Real handler retains A while its bot IO waits; official API invalidates A."""
    wire = harness
    ready, release = threading.Event(), threading.Event()
    old_runtime = bootstrap.get_agent_kernel_runtime()
    bot = Bot(ready, release)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(entry, bot, 1)
        try:
            assert ready.wait(10), 'handler never reached post-runtime bot IO'
            assert bot.retained_runtime is old_runtime
            assert bot.call_path == ["send_typing", "begin", "_execute_query"]
            assert not wire.requests
            assert bootstrap.get_agent_kernel_runtime() is old_runtime
            from app.agent.feature_gate import current_agent_runtime_generation
            old_generation = current_agent_runtime_generation()
            reply = config_api.post('/api/config', json=changed)
            assert reply.status_code == 200, reply.text
            assert current_agent_runtime_generation() > old_generation
            assert bootstrap._runtime is None
            select('route-b')
        finally:
            release.set()
            pending.result(timeout=15)
    entry(Bot(), 2)
    assert bootstrap.get_agent_kernel_runtime() is not old_runtime
    assert len(wire.requests) == 2, wire.requests
    assert [output(r['body']) for r in wire.requests] == [6000, 6000]
    expected_host = 'other.example.com' if 'AGENT_LLM_API_URL' in changed else 'api.example.com'
    assert [r['host'] for r in wire.requests] == ['api.example.com', expected_host]
    expected_protocol = 'responses' if 'AGENT_LLM_PROTOCOL' in changed else 'chat_completions'
    assert [r['protocol'] for r in wire.requests] == ['chat_completions', expected_protocol]
    assert (wire.requests[0]['auth_sha256'] != wire.requests[1]['auth_sha256']) == ('AGENT_LLM_API_KEY' in changed)
    assert [r['body']['model'] for r in wire.requests] == [MODEL, 'route-b']


def test_retained_runtime_can_still_use_its_own_saved_preference(harness, config_api):
    select('old-route-preference')
    ready, release = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(entry, Bot(ready, release), 1)
        try:
            assert ready.wait(10)
            result = config_api.post('/api/config', json={'AGENT_LLM_API_URL': 'https://other.example.com/v1'})
            assert result.status_code == 200, result.text
        finally:
            release.set()
            pending.result(timeout=15)
    entry(Bot(), 2)
    assert [(r['host'], r['body']['model']) for r in harness.requests] == [
        ('api.example.com', 'old-route-preference'), ('other.example.com', MODEL)]
    assert all(output(r['body']) == 6000 for r in harness.requests)


def test_same_channel_next_turn_and_web_ignores_preference(harness):
    session = bootstrap.get_agent_kernel_runtime().session
    turn(session)
    select('opaque/unknown-but-valid')
    turn(session, number=51)
    select('second')
    turn(session, number=52)
    turn(session, 'api', number=53)
    assert [r['body']['model'] for r in harness.requests] == [MODEL, 'opaque/unknown-but-valid', 'second', MODEL]
    assert all(output(r['body']) == 6000 for r in harness.requests)


def test_already_selected_model_survives_same_channel_callback(harness):
    select('first')
    original = harness.handler
    async def switch(request):
        response = await original(request)
        if len(harness.requests) == 1:
            select('second')
        return response
    harness.handler = switch
    entry(Bot(), 1)
    entry(Bot(), 2)
    assert [r['body']['model'] for r in harness.requests] == ['first', 'second']
    assert all(output(r['body']) == 6000 for r in harness.requests)


def test_inflight_request_keeps_selected_model_after_route_change(harness, config_api):
    select('old-selected')
    original = harness.handler
    async def switch(request):
        response = await original(request)
        if len(harness.requests) == 1:
            result = config_api.post('/api/config', json={'AGENT_LLM_API_URL': 'https://other.example.com/v1'})
            assert result.status_code == 200, result.text
            select('new-selected')
        return response
    harness.handler = switch
    entry(Bot(), 1)
    entry(Bot(), 2)
    assert [(r['host'], r['body']['model']) for r in harness.requests] == [
        ('api.example.com', 'old-selected'), ('other.example.com', 'new-selected')]
    assert all(output(r['body']) == 6000 for r in harness.requests)


def test_default_model_change_keeps_same_channel_preference(harness, config_api):
    ready, release = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(entry, Bot(ready, release), 1)
        try:
            assert ready.wait(10)
            result = config_api.post('/api/config', json={'AGENT_LLM_MODEL': 'new-default'})
            assert result.status_code == 200, result.text
            select('still-valid-same-channel')
        finally:
            release.set()
            pending.result(timeout=15)
    entry(Bot(), 2)
    assert [r['body']['model'] for r in harness.requests] == ['still-valid-same-channel'] * 2


@pytest.mark.parametrize('raw,context,cap', [('', 128000, 6000), ('bad', 128000, 6000),
    ('0', 16384, 4096), ('16384', 16384, 4096), ('2000001', 2000000, 6000)])
def test_existing_global_limits_unchanged(harness, raw, context, cap):
    config.set_and_save({'AGENT_LLM_CONTEXT_WINDOW_TOKENS': raw})
    runtime = bootstrap.get_agent_kernel_runtime()
    assert runtime.session.limits.context_window_tokens == context
    turn(runtime.session)
    assert output(harness.requests[0]['body']) == cap


@pytest.mark.parametrize('raw,expected', [(None, 12), ('', 12), ('bad', 12), ('0', 2), ('1', 2), ('2', 2),
    ('12', 12), ('30', 30), (' 60 ', 60), ('120', 120), ('121', 120)])
def test_timeout_shares_one_default_and_range_at_every_model_client(local_config, raw, expected):
    """缺失/空/非法回退 12，越界截断到 2–120；断言各链路 HTTP 客户端实际收到的超时。"""
    from app.clients.ai_recognition import AIRecognitionClient
    from app.modules.scraper import TMDBScraper
    from app.routes import tools_api

    if raw is not None:
        config.set_and_save({'AGENT_LLM_TIMEOUT_SECONDS': raw})
    settings = ProviderSettings.from_config()
    assert settings.timeout_seconds == expected

    # Agent 流式请求：网络空闲等待保留设置页已说明的 30 秒下限，不低于配置值。
    wire, agent_timeouts = Wire(), []
    def factory(**kwargs):
        agent_timeouts.append(kwargs['timeout_seconds'])
        return wire.factory(**kwargs)
    adapter = OpenAICompatibleModelAdapter(settings, client_factory=factory)
    async def stream():
        return [e async for e in adapter.stream(
            ModelRequest(system_prompt='offline', messages=(), tools=()), cancellation=CancellationToken())]
    assert asyncio.run(stream())[-1].type.value == 'finish'
    assert agent_timeouts == [max(30, expected)]

    # AI 辅助识别：按设置值请求，不再静默截断到 30/60 秒。
    ai_timeout = TMDBScraper._ai_provider_settings()[3]
    posted = []
    session = SimpleNamespace(post=lambda *args, **kwargs: posted.append(kwargs['timeout']))
    client = AIRecognitionClient(api_url=BASE + '/chat/completions', model=MODEL,
                                 timeout_seconds=ai_timeout, session=session)
    client._post(client.api_url, protocol=client.protocol, body={})
    assert posted == [(min(5, expected), expected)]

    # 连接测试未带草稿超时时使用同一有效值，而非对存量值另行报错。
    probe_timeouts = []
    class FakeClient:
        def __init__(self, **kwargs):
            probe_timeouts.append(kwargs['timeout_seconds'])
        async def post_json(self, url, **kwargs):
            return SimpleNamespace(status_code=401, text='{}')
        async def aclose(self):
            pass
    request = SimpleNamespace(session={}, client=SimpleNamespace(host='127.0.0.1'), headers={})
    with patch.object(tools_api, 'require_api_login', return_value=None), patch.object(
        tools_api.agent_rate_limiter, 'allow', return_value=True), patch.object(
        tools_api, 'FixedHostHttpClient', FakeClient):
        response = tools_api.ai_model_test(request, {
            'base_url': BASE, 'api_key': 'fixture', 'protocol': 'responses', 'model': MODEL})
    assert response.status_code == 502
    assert probe_timeouts == [expected]


@pytest.mark.parametrize('raw,error', [(object(), TypeError), (float('inf'), OverflowError)])
def test_timeout_type_errors_not_silently_swallowed(harness, raw, error, monkeypatch):
    original = config.get
    monkeypatch.setattr(config, 'get', lambda key, default='': raw if key == 'AGENT_LLM_TIMEOUT_SECONDS' else original(key, default))
    with pytest.raises(error):
        ProviderSettings.from_config()


def test_provider_settings_is_one_real_config_snapshot(harness, monkeypatch):
    ready, attempted, published = threading.Event(), threading.Event(), threading.Event()
    lock = config._lock
    changes = {'AGENT_LLM_API_URL': 'https://other.example.com/v1',
               'AGENT_LLM_API_KEY': 'synthetic-replacement-key', 'AGENT_LLM_MODEL': 'new-default',
               'AGENT_LLM_PROTOCOL': 'responses', 'AGENT_LLM_TIMEOUT_SECONDS': '91',
               'AGENT_LLM_GENERATION_PROFILES': '[]'}
    # Observe the real publisher's lock acquisition; do not replace config/storage.
    class ObservedLock:
        def __enter__(self):
            if threading.current_thread().name == 'snapshot-publisher':
                attempted.set()
            lock.acquire()
            return self

        def __exit__(self, *args):
            lock.release()

    monkeypatch.setattr(config, '_lock', ObservedLock())
    original = config.get
    first = True
    def observe_get(key, default=''):
        nonlocal first
        value = original(key, default)
        if key == 'AGENT_LLM_API_URL' and first and threading.current_thread().name != 'snapshot-publisher':
            first = False
            owned = getattr(lock, "_is_owned")()
            ready.set()
            assert attempted.wait(10)
            if not owned:
                assert published.wait(10)
        return value
    monkeypatch.setattr(config, 'get', observe_get)
    errors = []
    def publish():
        try:
            assert ready.wait(10)
            config.set_and_save(changes)
        except BaseException as exc:
            errors.append(exc)
        finally:
            published.set()
    writer = threading.Thread(target=publish, name='snapshot-publisher')
    writer.start()
    try:
        result = ProviderSettings.from_config()
    finally:
        writer.join(12)
    assert not writer.is_alive() and not errors
    assert published.is_set()
    assert (result.api_url, result.model, result.protocol, result.timeout_seconds, result.generation_profiles_json) == (
        BASE + '/chat/completions', MODEL, 'chat_completions', 12, '')


def test_direct_provider_settings_never_read_live_channel(harness):
    settings = ProviderSettings(api_url=config.get('AGENT_LLM_API_URL'), model=MODEL,
        api_key=config.get('AGENT_LLM_API_KEY'), protocol='chat_completions')
    runtime = bootstrap.build_agent_kernel_runtime(model=OpenAICompatibleModelAdapter(settings))
    config.set_and_save({'AGENT_LLM_API_URL': 'https://other.example.com/v1'})
    select('new-route-model')
    turn(runtime.session)
    assert harness.requests[0]['host'] == 'api.example.com'
    assert harness.requests[0]['body']['model'] == MODEL


def test_legacy_fingerprint_preserves_raw_key_and_url(harness):
    config.set_and_save({'AGENT_LLM_API_KEY': ' synthetic-spaced-key ',
                         'AGENT_LLM_API_URL': BASE + '/responses/responses/'})
    # No HTTP here: repeated endpoint suffix is part of the historical KV identity.
    select('same-route')
    settings = ProviderSettings.from_config()
    owner, session_id = scope()
    import inspect
    kwargs = {'settings': settings} if 'settings' in inspect.signature(prefs.get_telegram_model_preference).parameters else {}
    assert prefs.get_telegram_model_preference(owner, session_id, **kwargs) == 'same-route'
    raw = json.loads(db.kv_get(prefs._key(owner, session_id)))
    material = b'\0'.join((b'mediaflux-telegram-model-channel:v1',
        (BASE + '/responses/responses').encode(), b'chat_completions',
        hashlib.sha256(b' synthetic-spaced-key ').digest()))
    assert raw['channel'] == hashlib.sha256(material).hexdigest()


def test_script_adapter_without_settings_keeps_supported_selection(harness):
    from app.agent.kernel.model import ModelEvent, ModelEventType
    class Script:
        def __init__(self):
            self.requests = []

        async def stream(self, request, *, cancellation):
            self.requests.append(request)
            yield ModelEvent(ModelEventType.TEXT_DELTA, text='Topic')
            yield ModelEvent(ModelEventType.FINISH, finish_reason='stop')
    model = Script()
    runtime = bootstrap.build_agent_kernel_runtime(model=model)
    select('script-choice')
    turn(runtime.session)
    assert model.requests[0].model == 'script-choice'
    assert model.requests[0].max_output_tokens == 6000
    assert not harness.requests


def test_title_snapshot_is_not_live_config(harness, config_api):
    import sys

    from app.modules.telegram_topic_routing import _generate_private_topic_title
    owner, session_id = scope()
    previous = sys.getprofile()
    fired = False
    def profile(frame, event, arg):
        nonlocal fired
        if not fired and event == 'return' and frame.f_code is ProviderSettings.from_config.__func__.__code__:
            fired = True
            assert config_api.post('/api/config', json={'AGENT_LLM_API_URL': 'https://other.example.com/v1'}).status_code == 200
            select('new-title-route')
    sys.setprofile(profile)
    try:
        title = asyncio.run(_generate_private_topic_title(owner=owner, session_id=session_id, first_user_message='hello'))
    finally:
        sys.setprofile(previous)
    assert fired and title == 'Topic'
    assert harness.requests[0]['host'] == 'api.example.com'
    assert output(harness.requests[0]['body']) == 96
    assert harness.requests[0]['body']['model'] == MODEL


def test_model_menu_current_model_uses_captured_settings(harness, config_api):
    """The displayed selection belongs to the settings used to fetch the list.

    Callback issuance after this fetch is a separate, intentionally unclaimed
    configuration-transaction boundary.
    """
    import httpx

    async def models(request):
        assert request.method == 'GET'
        assert request.url.path == '/v1/models'
        assert request.headers['host'] == 'api.example.com'
        response = config_api.post('/api/config', json={'AGENT_LLM_API_URL': 'https://other.example.com/v1'})
        assert response.status_code == 200, response.text
        select('new-menu-route')
        return httpx.Response(200, json={'data': [{'id': MODEL}, {'id': 'new-menu-route'}]})

    harness.handler = models
    bot = Mock()
    with patch('app.clients.http.FixedHostHttpClient', harness.factory):
        agent_adapter.handle_agent_model_command(bot, source(), telebot)
    bot.reply_to.assert_called_once()
    body = bot.reply_to.call_args.args[1]
    assert f'当前模型：<code>{MODEL}</code>' in body
    markup = bot.reply_to.call_args.kwargs['reply_markup']
    labels = [button.text for row in markup.keyboard for button in row]
    assert '✓ ' + MODEL in labels
    assert '✓ new-menu-route' not in labels
    assert harness.clients and all(client._client.is_closed for client in harness.clients)


def test_title_uses_selected_model_and_own_cap(harness):
    from app.modules.telegram_topic_routing import _generate_private_topic_title
    select('title-choice')
    owner, session_id = scope()
    title = asyncio.run(_generate_private_topic_title(owner=owner, session_id=session_id, first_user_message='hello'))
    assert title == 'Topic'
    assert harness.requests[0]['body']['model'] == 'title-choice'
    assert output(harness.requests[0]['body']) == 96


def test_provider_channel_fingerprint_preserves_persisted_identity_and_raw_snapshot():
    values = {
        'AGENT_LLM_API_URL': ' https://provider.invalid/v1/ ',
        'AGENT_LLM_PROTOCOL': ' CHAT_COMPLETIONS ',
        'AGENT_LLM_API_KEY': ' fixture-key ',
        'AGENT_LLM_MODEL': 'fixture-model',
        'AGENT_LLM_TIMEOUT_SECONDS': '',
        'AGENT_LLM_CONTEXT_WINDOW_TOKENS': '',
        'AGENT_LLM_BUDGET_PROFILES': '',
    }
    # 固定值来自迁移前的实现，保护已持久化的偏好/回调渠道标识。
    expected = 'd41f8a21d551ba64e27b2ec00dfa2c75b9f7bb0cb56031b1e3d10ff613177c33'
    assert provider_channel_fingerprint(values) == expected
    with patch.object(config, 'get_many', return_value=values) as snapshot:
        settings = ProviderSettings.from_config()
    snapshot.assert_called_once()
    assert settings.telegram_channel == expected
    assert settings.api_key == 'fixture-key'
    assert provider_channel_fingerprint({**values, 'AGENT_LLM_API_KEY': settings.api_key}) != expected
