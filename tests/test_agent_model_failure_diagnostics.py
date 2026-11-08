"""模型故障边界的离线回归：真实 native adapter、HTTP 流和公开 Session。

仅替换网络 transport 与测试时钟，不替换模型流解析或 Session 异常边界。
真实套接字/DNS 被禁止；这些测试不代表云端模型验收。
"""
from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import FrozenInstanceError, asdict, fields, is_dataclass
from unittest.mock import patch

import httpx
import pytest

from app.agent.kernel import provider_model as pm
from app.agent.kernel.adapters import TurnViewBuilder, iter_ndjson
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.events import TERMINAL_EVENT_TYPES, AgentEventType
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import AgentInput, CancellationToken
from app.clients.http import FixedHostHttpClient
from tests.test_agent_protocol_fallback import (
    PROTOCOL_PATHS,
    SECRET,
    Body,
    good,
    http_error,
    protocols,
    real_retry_wire,
    sse,
    wire,
)

PROTOCOLS = tuple(PROTOCOL_PATHS)
RAW_URL = 'https://model-route-canary.example.com/v1'
RAW_EXCEPTION_BODY = 'RAW_PROVIDER_EXCEPTION_BODY_CANARY'
FAULT_TEXT = f'{SECRET} api_key={SECRET} {RAW_URL}/responses {RAW_EXCEPTION_BODY}'
VISIBLE = '已开始核验，但还没有完整结果。'
FAILURE_FIELDS = {
    'kind', 'phase', 'protocol', 'attempts', 'http_status', 'emitted',
    'elapsed_ms', 'idle_timeout_seconds', 'deadline_seconds',
}
FAILURE_KINDS = {
    'connect_timeout', 'read_timeout', 'write_timeout', 'pool_timeout',
    'connection_error', 'transport_error', 'deadline', 'http_error',
    'incomplete_response', 'invalid_response', 'invalid_tool_arguments', 'provider_error',
}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*_args, **_kwargs):
        raise AssertionError('real sockets and DNS are forbidden')

    for name in ('connect', 'connect_ex', 'sendto'):
        monkeypatch.setattr(socket.socket, name, deny)
    for name in ('create_connection', 'getaddrinfo', 'gethostbyname',
                 'gethostbyname_ex', 'gethostbyaddr'):
        monkeypatch.setattr(socket, name, deny)


def assert_safe(*values):
    public = '\n'.join(str(value) for value in values)
    for private in (SECRET, RAW_URL, RAW_EXCEPTION_BODY):
        assert private not in public


def assert_failure(result, *, kind, phase, protocol, attempts, emitted,
                   http_status=None, deadline=60):
    error = result['error']
    assert isinstance(error, pm.ModelProviderError), type(error).__name__
    failure = getattr(error, 'failure', None)
    assert failure is not None, '模型故障缺少结构化 failure 诊断'
    assert isinstance(failure, pm.ProviderFailure) and is_dataclass(failure)
    assert {field.name for field in fields(failure)} == FAILURE_FIELDS
    assert failure.kind in FAILURE_KINDS
    assert failure.phase in {'request', 'response', 'retry_wait'}
    assert failure.protocol in PROTOCOLS
    assert (failure.kind, failure.phase, failure.protocol) == (kind, phase, protocol)
    assert type(failure.attempts) is int and failure.attempts == attempts
    assert failure.attempts == len(result['requests'])
    assert type(failure.emitted) is bool and failure.emitted is emitted
    assert failure.http_status == http_status
    assert type(failure.elapsed_ms) is int and failure.elapsed_ms >= 0
    assert failure.idle_timeout_seconds == 30
    assert failure.deadline_seconds == deadline
    assert_safe(str(error), json.dumps(asdict(failure), ensure_ascii=False))
    return failure


def partial_sse(protocol, **options):
    events = {
        'responses': [{'type': 'response.output_text.delta', 'delta': VISIBLE}],
        'chat_completions': [{'choices': [{'delta': {'content': VISIBLE}, 'finish_reason': None}]}],
        'anthropic_messages': [
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': VISIBLE}},
        ],
    }[protocol]
    return dict(status=200, data=sse(*events), headers={'content-type': 'text/event-stream'}, **options)


def session_wire(actions, *, protocol='responses', deadline=60):
    """真实 Session + adapter + FixedHostHttpClient；沿用 wire 的字节流夹具。"""
    async def run():
        requests, streams, clients, events = [], [], [], []

        async def handler(request):
            actual = next(name for name, path in PROTOCOL_PATHS.items() if path == request.url.path)
            requests.append({'protocol': actual, 'body': json.loads(request.content)})
            action = actions[min(len(requests) - 1, len(actions) - 1)]
            if isinstance(action, BaseException):
                raise action
            if action is None:
                action = {'status': 200, 'data': json.dumps(good(actual)).encode()}
            stream = Body(action.get('data', b''), fault=action.get('fault'),
                          hanging=action.get('hanging', False), task_cancel=action.get('task_cancel', False))
            streams.append(stream)
            return httpx.Response(action.get('status', 200), headers=action.get('headers', {}), stream=stream)

        def factory(**kwargs):
            client = FixedHostHttpClient(
                **kwargs, transport=httpx.MockTransport(handler),
                resolver=lambda _host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))],
            )
            clients.append(client)
            return client

        adapter = pm.OpenAICompatibleModelAdapter(pm.ProviderSettings(
            api_url=RAW_URL, model='fixture', api_key=SECRET, protocol=protocol,
        ), client_factory=factory)
        catalog, store = ToolCatalog(), InMemorySessionStateStore()
        session = AgentSession(
            model=adapter, catalog=catalog,
            pipeline=ToolPipeline(catalog=catalog, state_store=store), state_store=store,
        )
        builder = TurnViewBuilder()

        async def observed():
            async for event in session.run(AgentInput(
                message='仅核验模型流，不调用业务工具。', owner='offline-owner',
                session_id='model-failure-diagnostics', request_id='offline-model-failure',
            )):
                events.append(event)
                builder.apply(event)
                yield event

        with patch.object(pm, '_MODEL_RETRY_DELAY_SECONDS', 0), patch.object(
            pm, '_stream_deadline_seconds', return_value=deadline,
        ):
            public_events = [json.loads(line) async for line in iter_ndjson(observed())]
        assert all(client._client.is_closed for client in clients)
        assert all(stream.closed for stream in streams)
        return {'requests': requests, 'events': events, 'public_events': public_events,
                'final': builder.build().to_dict()}

    return asyncio.run(run())


@pytest.mark.parametrize('fault_type', [httpx.ReadTimeout, httpx.ReadError])
@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_text_delta_transport_failure_is_model_error_and_never_retried(protocol, fault_type, caplog):
    fault = fault_type(FAULT_TEXT)
    result = wire([partial_sse(protocol, fault=fault), None], configured=protocol, api_url=RAW_URL)
    assert protocols(result) == [protocol]
    assert [str(event.type) for event in result['events']] == ['text_delta']
    assert result['events'][0].text == VISIBLE
    assert_failure(result, kind='read_timeout' if fault_type is httpx.ReadTimeout else 'transport_error',
                   phase='response', protocol=protocol, attempts=1, emitted=True, http_status=200)
    assert result['error'] is not None
    assert result['error'].__cause__ is fault
    assert_safe(caplog.text)


@pytest.mark.parametrize('fault_type,kind', [
    (httpx.ConnectTimeout, 'connect_timeout'), (httpx.ReadTimeout, 'read_timeout'),
    (httpx.WriteTimeout, 'write_timeout'), (httpx.PoolTimeout, 'pool_timeout'),
    (httpx.ConnectError, 'connection_error'), (httpx.ReadError, 'transport_error'),
])
def test_failure_before_headers_retains_retry_budget_and_has_diagnostic(fault_type, kind):
    fault = fault_type(FAULT_TEXT)
    result = wire([fault], api_url=RAW_URL)
    assert protocols(result) == ['responses', 'responses'] and not result['events']
    assert_failure(result, kind=kind, phase='request', protocol='responses', attempts=2, emitted=False)
    assert result['error'] is not None
    assert result['error'].__cause__ is fault
    assert result['requests'][0]['body'] == result['requests'][1]['body']
    for client in result['clients']:
        timeout = client._client.timeout
        assert timeout.connect == 4
        assert timeout.read == timeout.write == timeout.pool == 30


def test_error_response_body_read_timeout_retains_http_status_and_transport_kind():
    result = wire([http_error(404, fault=httpx.ReadTimeout(FAULT_TEXT)), None], api_url=RAW_URL)
    assert protocols(result) == ['responses'] and not result['events']
    assert_failure(result, kind='read_timeout', phase='response', protocol='responses',
                   attempts=1, emitted=False, http_status=404)


@pytest.mark.parametrize('data', [b'{"output":', b'\xff'], ids=['malformed_json', 'invalid_utf8'])
@pytest.mark.parametrize('configured', [*PROTOCOLS, 'auto'])
def test_nonstream_decode_failure_retains_invalid_format_error(configured, data):
    prefix = [http_error(404)] if configured == 'auto' else []
    expected_protocols = ['responses', 'chat_completions'] if prefix else [configured]
    result = wire([*prefix, {'status': 200, 'data': data}], configured=configured, api_url=RAW_URL)
    assert protocols(result) == expected_protocols and not result['events']
    assert_failure(result, kind='invalid_response', phase='response', protocol=expected_protocols[-1],
                   attempts=len(expected_protocols), emitted=False, http_status=200)
    error = result['error']
    assert isinstance(error, pm.ModelProviderError)
    assert str(error) == 'Provider 返回格式无效'
    if prefix:
        assert isinstance(error.__cause__, pm.ModelProviderError)
        assert str(error.__cause__) == 'Provider 请求失败（HTTP 404）'
    else:
        assert error.__cause__ is None


@pytest.mark.parametrize('fault_type', [httpx.ReadTimeout, httpx.ReadError])
def test_real_session_partial_stream_has_model_provider_error_public_final(fault_type, caplog):
    result = session_wire([partial_sse('responses', fault=fault_type(FAULT_TEXT)), None])
    assert protocols(result) == ['responses']
    assert any(event.type is AgentEventType.MODEL_DELTA and event.payload['delta'] == VISIBLE
               for event in result['events'])
    terminal = [event for event in result['events'] if event.type in TERMINAL_EVENT_TYPES]
    assert len(terminal) == 1 and terminal[0].type is AgentEventType.TURN_FAILED
    final = result['final']
    assert final['status'] == 'failed'
    assert final['error']['code'] == 'model_provider_error', final
    assert final['error']['message'] == '模型回复传输中断，本轮未完整生成；请稍后重试。'
    assert terminal[0].payload['code'] == final['error']['code']
    assert 'internal_error' not in json.dumps(result['public_events'])
    assert_safe(json.dumps(result['public_events'], ensure_ascii=False), json.dumps(final, ensure_ascii=False), caplog.text)


def test_failure_contract_is_frozen_safe_fields_and_local_errors_default_to_none():
    assert pm.ModelProviderError('本地校验失败').failure is None
    result = wire([httpx.ReadTimeout(FAULT_TEXT)])
    failure = assert_failure(result, kind='read_timeout', phase='request', protocol='responses',
                             attempts=2, emitted=False)
    with pytest.raises(FrozenInstanceError):
        setattr(failure, 'kind', 'unexpected')


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_read_timeout_after_headers_before_output_retries_same_native_request(protocol):
    result = wire([
        {'status': 200, 'data': b'', 'headers': {'content-type': 'text/event-stream'},
             'fault': httpx.ReadTimeout(FAULT_TEXT)},
    ], configured=protocol, api_url=RAW_URL)
    assert protocols(result) == [protocol, protocol] and not result['events']
    assert result['requests'][0]['body'] == result['requests'][1]['body']
    assert_failure(result, kind='read_timeout', phase='response', protocol=protocol,
                   attempts=2, emitted=False, http_status=200)


@pytest.mark.parametrize('prefix', [
    [http_error(404)], [http_error(503), http_error(404)],
], ids=['fallback', 'retry_then_fallback'])
def test_failure_attempts_count_all_real_http_requests_across_fallback(prefix):
    result = wire([*prefix, httpx.ReadTimeout(FAULT_TEXT)], api_url=RAW_URL)
    assert protocols(result) == ['responses'] * len(prefix) + ['chat_completions'] * 2
    assert result['requests'][-1]['body'] == result['requests'][-2]['body']
    assert not result['events']
    assert_failure(result, kind='read_timeout', phase='request', protocol='chat_completions',
                   attempts=len(prefix) + 2, emitted=False)


def test_partial_chat_timeout_after_fallback_cannot_replay_or_add_a_request():
    result = wire([http_error(404), partial_sse('chat_completions', fault=httpx.ReadTimeout(FAULT_TEXT)), None],
                  api_url=RAW_URL)
    assert protocols(result) == ['responses', 'chat_completions']
    assert [str(event.type) for event in result['events']] == ['text_delta']
    assert_failure(result, kind='read_timeout', phase='response', protocol='chat_completions',
                   attempts=2, emitted=True, http_status=200)


def test_pre_output_transport_retry_can_still_recover_without_diagnostic_failure():
    result = wire([httpx.ReadTimeout(FAULT_TEXT), None], api_url=RAW_URL)
    assert protocols(result) == ['responses', 'responses']
    assert result['requests'][0]['body'] == result['requests'][1]['body']
    assert result['error'] is None
    assert [str(event.type) for event in result['events']] == ['text_delta', 'finish']
    assert_safe(result['events'])


@pytest.mark.parametrize('emitted', [False, True], ids=['before_text', 'after_text'])
def test_real_stream_deadline_is_distinct_from_read_timeout_and_never_retried(emitted):
    action = partial_sse('responses', hanging=True) if emitted else {
        'status': 200, 'data': b'', 'headers': {'content-type': 'text/event-stream'}, 'hanging': True,
    }
    result = wire([action, None], deadline=0.02)
    assert protocols(result) == ['responses']
    assert [str(event.type) for event in result['events']] == (['text_delta'] if emitted else [])
    assert_failure(result, kind='deadline', phase='response', protocol='responses',
                   attempts=1, emitted=emitted, http_status=200, deadline=0.02)
    assert result['error'] is not None
    assert isinstance(result['error'].__cause__, TimeoutError)


@pytest.mark.parametrize('fallback', [False, True], ids=['responses', 'fallback_chat'])
def test_real_retry_wait_deadline_reports_only_requests_actually_sent(fallback):
    result = real_retry_wire('deadline', None, fallback)
    expected = ['responses', 'chat_completions'] if fallback else ['responses']
    assert protocols(result) == expected and not result['events']
    assert result['cancelling'] == 0 and not result['cancel_calls']
    assert_failure(result, kind='deadline', phase='retry_wait', protocol=expected[-1],
                   attempts=len(expected), emitted=False, http_status=503, deadline=0.01)


@pytest.mark.parametrize('mode', ['before_request', 'request', 'body', 'partial_body', 'task_cancel', 'retry_wait'])
def test_cancellation_stays_cancellation_without_failure_diagnostic(mode, caplog):
    token = CancellationToken()
    if mode == 'before_request':
        token.cancel()
        result = wire([None], token=token)
    elif mode == 'request':
        result = wire([asyncio.CancelledError(), None])
    elif mode == 'body':
        result = wire([{'status': 200, 'data': b'', 'fault': asyncio.CancelledError(FAULT_TEXT)}, None])
    elif mode == 'partial_body':
        result = wire([partial_sse('responses', fault=asyncio.CancelledError(FAULT_TEXT)), None])
    elif mode == 'task_cancel':
        result = wire([partial_sse('responses', task_cancel=True), None])
    else:
        async def cancel_wait(_delay):
            token.cancel()

        result = wire([httpx.ReadTimeout(FAULT_TEXT), None], token=token, sleep=cancel_wait)
    assert protocols(result) == ([] if mode == 'before_request' else ['responses'])
    assert [str(event.type) for event in result['events']] == (
        ['text_delta'] if mode in {'partial_body', 'task_cancel'} else []
    )
    assert isinstance(result['error'], asyncio.CancelledError)
    assert getattr(result['error'], 'failure', None) is None
    assert not isinstance(result['error'], pm.ModelProviderError)
    assert_safe(str(result['error']), caplog.text)


@pytest.mark.parametrize('fault_type,expected_message', [
    (httpx.ReadTimeout, '模型服务响应超时，请稍后重试。'),
    (httpx.WriteTimeout, '模型服务响应超时，请稍后重试。'),
    (httpx.PoolTimeout, '模型服务响应超时，请稍后重试。'),
    (httpx.ConnectTimeout, '无法连接模型服务，请检查网络或模型路由后重试。'),
    (httpx.ConnectError, '无法连接模型服务，请检查网络或模型路由后重试。'),
])
def test_real_session_pre_output_failure_has_classified_safe_public_final(fault_type, expected_message, caplog):
    result = session_wire([fault_type(FAULT_TEXT)])
    assert protocols(result) == ['responses', 'responses']
    assert not any(event.type is AgentEventType.MODEL_DELTA for event in result['events'])
    final = result['final']
    assert final['status'] == 'failed'
    assert final['error'] == {'code': 'model_provider_error', 'message': expected_message}
    assert_safe(json.dumps(result['public_events'], ensure_ascii=False), json.dumps(final, ensure_ascii=False), caplog.text)


def test_real_session_error_body_timeout_has_safe_timeout_public_final(caplog):
    result = session_wire([http_error(404, FAULT_TEXT, fault=httpx.ReadTimeout(FAULT_TEXT)), None], protocol='auto')
    assert protocols(result) == ['responses']
    assert result['final']['error'] == {
        'code': 'model_provider_error', 'message': '模型服务响应超时，请稍后重试。',
    }
    assert_safe(json.dumps(result['public_events'], ensure_ascii=False), result['final'], caplog.text)


@pytest.mark.parametrize('emitted', [False, True], ids=['before_text', 'after_text'])
def test_real_session_task_cancellation_emits_cancelled_not_diagnostic_failure(emitted, caplog):
    action = partial_sse('responses', task_cancel=True) if emitted else {
        'status': 200, 'data': b'', 'headers': {'content-type': 'text/event-stream'}, 'task_cancel': True,
    }
    result = session_wire([action, None])
    assert protocols(result) == ['responses']
    terminal = [event for event in result['events'] if event.type in TERMINAL_EVENT_TYPES]
    assert len(terminal) == 1 and terminal[0].type is AgentEventType.TURN_CANCELLED
    assert result['final']['status'] == 'cancelled'
    assert result['final']['error']['code'] == 'cancelled'
    assert not any(event.type is AgentEventType.TURN_FAILED for event in result['events'])
    assert 'model_provider_error' not in json.dumps(result['public_events']) + caplog.text
    assert_safe(json.dumps(result['public_events'], ensure_ascii=False), result['final'], caplog.text)


def test_request_construction_type_error_remains_programming_error_not_provider_failure():
    error = TypeError('request construction regression')
    with patch.object(pm, 'native_tool_request_body', side_effect=error):
        result = wire([None])
    assert not result['requests'] and not result['events']
    assert result['error'] is error
    assert not isinstance(error, pm.ModelProviderError)
    assert getattr(error, 'failure', None) is None


def test_unreplayable_native_arguments_have_safe_classified_diagnostic(caplog):
    envelope = {'content': [{'type': 'tool_use', 'id': 'bad-args', 'name': 'lookup',
                            'input': '{"query":"' + FAULT_TEXT}], 'stop_reason': 'tool_use'}
    result = wire([{'status': 200, 'data': json.dumps(envelope).encode()}],
                  configured='anthropic_messages')
    assert_failure(result, kind='invalid_tool_arguments', phase='response',
                   protocol='anthropic_messages', attempts=1, emitted=False, http_status=200)
    error = result['error']
    assert isinstance(error, pm.UncommittableModelTurn)
    assert error.reason == 'unreplayable_arguments'
    assert error.failure is not None
    assert_safe(str(error), str(error.__cause__), str(error.__context__),
                json.dumps(asdict(error.failure)), caplog.text)
