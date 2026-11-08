"""Global context and provider configuration share one supported snapshot."""
from app.agent.kernel.provider_model import OpenAICompatibleModelAdapter
from app.agent.kernel.effects import ConfirmationEffectPlanStore
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx
import pytest

from app import config
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.kernel import bootstrap
from app.agent.kernel import model_rounds as model_rounds_module
from app.agent.kernel.model import ModelEvent, ModelEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.provider_model import (
    ProviderSettings,
)
from app.agent.kernel.state_types import AgentInput
from tests import test_agent_generation_profiles as profile_support

local_config = profile_support.local_config
config_api = profile_support.config_api
no_external_io = profile_support.no_external_io
CONTEXT = 'AGENT_LLM_CONTEXT_WINDOW_TOKENS'
OWNER = 'webk:v1:' + 'a' * 64


@pytest.fixture
def harness(local_config):
    wire = profile_support.Wire('Topic')
    original = wire.handler

    async def capture(request):
        response = await original(request)
        wire.requests[-1]['host'] = request.headers['host']
        return response

    wire.handler = capture
    with (
        patch.dict(OpenAICompatibleModelAdapter.__init__.__kwdefaults__, client_factory=wire.factory),
        patch.object(model_rounds_module, 'bounded_model_messages', wraps=model_rounds_module.bounded_model_messages) as compression,
    ):
        yield wire, compression
    assert all(client._client.is_closed for client in wire.clients)


def turn(session, session_id='context-snapshot'):
    async def run():
        return [event async for event in session.run(AgentInput(
            message='hello', owner=OWNER, session_id=session_id, channel='api'))]
    return asyncio.run(run())


def complete(events):
    assert events[-1].type.value == 'turn.completed', [(e.type.value, e.payload) for e in events]


def output(body):
    keys = set(body) & {'max_tokens', 'max_output_tokens', 'max_completion_tokens'}
    assert len(keys) == 1
    return body[keys.pop()]


def compressed_limits(compression):
    return [(call.kwargs['context_window_tokens'], call.kwargs['output_tokens'])
            for call in compression.call_args_list]


@pytest.mark.parametrize('initial,updated,old_output,new_output', [
    ('128000', '16384', 6000, 4096),
    ('16384', '128000', 4096, 6000),
])
def test_runtime_context_uses_same_provider_snapshot(harness, config_api, initial, updated, old_output, new_output):
    wire, compression = harness
    config.set_and_save({CONTEXT: initial})
    original = config.get_many
    ready = threading.Event()
    captured = []
    updates = {
        CONTEXT: updated,
        'AGENT_LLM_API_URL': 'https://other.example.com/v1',
        'AGENT_LLM_MODEL': 'updated-model',
        'AGENT_LLM_API_KEY': 'synthetic-updated-key',
        'AGENT_LLM_PROTOCOL': 'responses',
    }

    def publish():
        assert ready.wait(10), 'runtime never obtained provider snapshot'
        config.set_and_save(updates)

    def read_then_publish(keys, *args, **kwargs):
        values = original(keys, *args, **kwargs)
        if 'AGENT_LLM_MODEL' in keys:
            captured.append(values)
            ready.set()
            # Real publisher runs after get_many releases its RLock. Calling the
            # API here would deadlock its invalidation against the build lock.
            pending.result(timeout=10)
        return values

    # Retain a genuinely admitted old runtime for every original old-wire check.
    runtime = bootstrap.get_agent_kernel_runtime()
    bootstrap.invalidate_agent_kernel_runtime()
    from app.agent.feature_gate import RuntimeFenceError
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(publish)
        try:
            with patch.object(config, 'get_many', side_effect=read_then_publish):
                with pytest.raises(RuntimeFenceError, match='runtime_changed_during_creation'):
                    bootstrap.get_agent_kernel_runtime()
        finally:
            ready.set()
            pending.result(timeout=10)

    assert bootstrap._runtime is None
    assert not wire.requests
    assert len(captured) == 1
    assert config.get(CONTEXT) == updated
    assert config.get('AGENT_LLM_MODEL') == 'updated-model'
    assert config._read_env_file(config.ENV_FILE)[CONTEXT] == updated
    assert isinstance(runtime.session.model, OpenAICompatibleModelAdapter)
    settings = runtime.session.model.settings
    assert settings.model == 'tenant/opaque-alias'
    assert settings.api_url == profile_support.BASE + '/chat/completions'
    assert settings.api_key == 'synthetic-preserved-key'
    assert settings.protocol == 'chat_completions'
    complete(turn(runtime.session))
    assert [(r['host'], r['protocol'], r['body']['model']) for r in wire.requests] == [
        ('api.example.com', 'chat_completions', 'tenant/opaque-alias')]
    assert compressed_limits(compression) == [(int(initial), old_output)]
    assert output(wire.requests[0]['body']) == old_output
    assert runtime.session.limits.context_window_tokens == int(initial)

    # A genuine timeout change triggers normal API invalidation outside the
    # construction lock; resaving the identical values is correctly a no-op.
    # No generation is patched and no stale admission token is resurrected.
    reply = config_api.post('/api/config', json={**updates, 'AGENT_LLM_TIMEOUT_SECONDS': '31'})
    assert reply.status_code == 200, reply.text
    rebuilt = bootstrap.get_agent_kernel_runtime()
    assert rebuilt is not runtime
    complete(turn(rebuilt.session, session_id='after-publish'))
    assert compressed_limits(compression) == [(int(initial), old_output), (int(updated), new_output)]
    assert [output(r['body']) for r in wire.requests] == [old_output, new_output]
    assert (wire.requests[-1]['host'], wire.requests[-1]['protocol'], wire.requests[-1]['body']['model']) == (
        'other.example.com', 'responses', 'updated-model')
    assert isinstance(rebuilt.session.model, OpenAICompatibleModelAdapter)
    assert rebuilt.session.model.settings.api_key == 'synthetic-updated-key'
    assert rebuilt.session.limits.context_window_tokens == int(updated)


@pytest.mark.parametrize('raw,context,cap', [
    (None, 128000, 6000), ('', 128000, 6000), ('bad', 128000, 6000),
    ('0', 16384, 4096), ('1024', 16384, 4096), ('16383', 16384, 4096),
    ('16384', 16384, 4096), ('128000', 128000, 6000),
    ('2000000', 2000000, 6000), ('2000001', 2000000, 6000),
    (' 16384 ', 16384, 4096), ('inf', 128000, 6000),
])
def test_global_context_defaults_and_clamp_unchanged(harness, raw, context, cap):
    wire, compression = harness
    if raw is not None:
        config.set_and_save({CONTEXT: raw})
    else:
        assert CONTEXT not in config.all_items()
    runtime = bootstrap.build_agent_kernel_runtime()
    assert runtime.session.limits.context_window_tokens == context
    assert runtime.session.limits.max_output_tokens == 6000
    complete(turn(runtime.session))
    assert compressed_limits(compression) == [(context, cap)]
    assert output(wire.single_body()) == cap


def test_saved_reasoning_effort_rebuilds_runtime_with_widened_output_budget(harness, config_api):
    wire, _compression = harness
    assert config_api.get('/api/config').json()['AGENT_LLM_REASONING_EFFORT'] == ''
    before = bootstrap.get_agent_kernel_runtime()
    assert before.session.limits.max_output_tokens == 6000
    complete(turn(before.session))

    reply = config_api.post('/api/config', json={'AGENT_LLM_REASONING_EFFORT': ' High '})
    assert reply.status_code == 200, reply.text
    assert config.get('AGENT_LLM_REASONING_EFFORT') == 'high'
    after = bootstrap.get_agent_kernel_runtime()
    assert after is not before and after.session.limits.max_output_tokens == 16000
    complete(turn(after.session, session_id='after-effort'))

    legacy, reasoning = (call['body'] for call in wire.requests)
    assert (output(legacy), legacy['temperature'], 'reasoning_effort' in legacy) == (6000, 0, False)
    assert (output(reasoning), reasoning['reasoning_effort'], 'temperature' in reasoning) == (16000, 'high', False)


class InjectedModel:
    def __init__(self, truthy=True):
        self.truthy = truthy
        self.bool_calls = 0
        self.requests = []

    def __bool__(self):
        self.bool_calls += 1
        assert self.bool_calls == 1, 'model truthiness must be evaluated only once'
        return self.truthy

    @property
    def settings(self):
        raise AssertionError('injected model settings must not be inspected')

    async def stream(self, request, *, cancellation):
        self.requests.append(request)
        yield ModelEvent(ModelEventType.TEXT_DELTA, text='Topic')
        yield ModelEvent(ModelEventType.FINISH, finish_reason='stop')


def test_injected_model_keeps_global_limits_and_custom_stores(harness):
    wire, compression = harness
    config.set_and_save({CONTEXT: '16384', 'AGENT_LLM_API_URL': '', 'AGENT_LLM_API_KEY': '', 'AGENT_LLM_MODEL': ''})
    model = InjectedModel()
    store = SQLiteKernelStore()
    confirmations = SQLiteConfirmationStore()
    with patch.object(config, 'get_many', side_effect=AssertionError('injected build must not read provider config')):
        runtime = bootstrap.build_agent_kernel_runtime(model=model, store=store, confirmation_store=confirmations)
    assert model.bool_calls == 1
    assert runtime.session.model is model
    assert runtime.store is store
    assert runtime.session.state_store is store
    assert runtime.session.journal is store
    assert isinstance(runtime.session.pipeline.effect_store, ConfirmationEffectPlanStore)
    assert runtime.session.pipeline.effect_store.store is confirmations
    assert runtime.session.limits.context_window_tokens == 16384
    complete(turn(runtime.session))
    assert compressed_limits(compression) == [(16384, 4096)]
    assert [request.max_output_tokens for request in model.requests] == [4096]
    assert not wire.requests


def test_falsey_injected_model_never_falls_back_to_default_adapter(harness):
    wire, compression = harness
    config.set_and_save({CONTEXT: '16384'})
    model = InjectedModel(truthy=False)
    with patch.object(bootstrap, 'OpenAICompatibleModelAdapter', side_effect=AssertionError('explicit injection must not construct default adapter')):
        runtime = bootstrap.build_agent_kernel_runtime(model=model)
    assert model.bool_calls == 1
    assert runtime.session.model is model
    assert runtime.creation_binding is None
    complete(turn(runtime.session))
    assert len(model.requests) == 1
    assert compressed_limits(compression) == [(16384, 4096)]
    assert model.requests[0].max_output_tokens == 4096
    assert not wire.requests


def test_direct_adapter_injection_is_not_reinterpreted_as_default_policy(harness):
    wire, compression = harness
    config.set_and_save({CONTEXT: '128000'})
    model = OpenAICompatibleModelAdapter(ProviderSettings.from_config(), client_factory=wire.factory)
    config.set_and_save({CONTEXT: '16384'})
    with patch.object(config, 'get_many', side_effect=AssertionError('injection must not rebuild provider settings')):
        runtime = bootstrap.build_agent_kernel_runtime(model=model)
    assert runtime.session.model is model
    assert runtime.session.limits.context_window_tokens == 16384
    complete(turn(runtime.session))
    assert compressed_limits(compression) == [(16384, 4096)]
    assert output(wire.single_body()) == 4096


@pytest.mark.parametrize('value,error', [(object(), TypeError), (float('inf'), OverflowError)])
def test_non_config_context_types_are_not_silently_swallowed(harness, value, error):
    # Real config publishes strings; these deliberately invalid getter values
    # characterize the legacy exception boundary, not a supported config input.
    original = config.get
    with patch.object(config, 'get', side_effect=lambda key, default='': value if key == CONTEXT else original(key, default)):
        with pytest.raises(error):
            bootstrap.build_agent_kernel_runtime()
    assert not harness[0].requests


def test_global_context_api_change_preserves_internal_auto_fallback(harness, config_api):
    wire, compression = harness
    config.set_and_save({'AGENT_LLM_PROTOCOL': 'auto', 'AGENT_LLM_API_URL': profile_support.BASE})
    original = wire.handler

    async def change(request):
        response = await original(request)
        if len(wire.requests) == 1:
            reply = config_api.post('/api/config', json={CONTEXT: '16384'})
            assert reply.status_code == 200, reply.text
            return httpx.Response(404, text='responses api is not supported')
        return response

    wire.handler = change
    runtime = bootstrap.get_agent_kernel_runtime()
    events = turn(runtime.session)
    assert any(e.type.value == 'turn.cancelled' and e.payload.get('reason') == 'runtime_changed' for e in events)
    assert [r['protocol'] for r in wire.requests] == ['responses', 'chat_completions']
    assert [output(r['body']) for r in wire.requests] == [6000, 6000]
    assert compressed_limits(compression) == [(128000, 6000)]
    rebuilt = bootstrap.get_agent_kernel_runtime()
    assert rebuilt is not runtime
    complete(turn(rebuilt.session, session_id='next-runtime'))
    assert [output(r['body']) for r in wire.requests] == [6000, 6000, 4096]
    assert compressed_limits(compression) == [(128000, 6000), (16384, 4096)]
