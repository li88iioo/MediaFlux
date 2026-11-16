"""Fail-closed protocol negotiation, with offline real-client wire regressions."""
from __future__ import annotations
from typing import Any, cast, TypedDict

import asyncio
import json
import socket
from contextlib import asynccontextmanager, nullcontext
from dataclasses import replace
from unittest.mock import patch

import httpx
import pytest

from app.agent.kernel import provider_model as pm
from app.agent.kernel.model import ModelEvent
from app.agent.kernel.model import (
    ModelMessage,
    ModelRequest,
    ModelToolCall,
    ProviderState,
)
from app.agent.kernel.state_types import CancellationToken
from app.clients import openai_compatible as oc
from app.clients.http import FixedHostHttpClient
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414


@pytest.mark.parametrize('status,text', [
    (404, ''),
    (404, 'model not found'),
    (404, '{"error":{"code":"model_not_found","message":"unsupported endpoint"}}'),
    (400, 'responses api is not supported' + ' ' * 8200 + ' insufficient quota'),
])
def test_classifier_rejects_original_counterexamples(status, text):
    assert oc.is_protocol_fallback_error(status, text, protocol='responses') is False


@pytest.mark.parametrize('status,text', [
    (400, 'responses api is not supported'),
    (404, 'responses api is not supported'),
    (422, "unknown parameter 'input'"),
    (405, 'Cannot POST /v1/responses.'),
    (415, '{"error":"responses api is not supported"}'),
    (501, '{"error":{"code":"unsupported_endpoint","type":"invalid_request_error","message":"responses api is not supported"}}'),
    (400, 'responses api is not supported'.ljust(8192)),
    (400, 'responses api is not supported' + '\u3000' * 2720 + '  '),
])
def test_classifier_complete_explicit_evidence(status, text):
    assert oc.is_protocol_fallback_error(status, text, protocol='responses') is True


@pytest.mark.parametrize('text', [
    '{}', '', '404 Not Found', 'route not found', 'cannot post /wrong/responses',
    'input is not a valid temperature', 'example: responses api is not supported',
    'responses api is not supported; actually insufficient quota',
    'responses api is not supported'.ljust(8193),
    'responses api is not supported' + '\u3000' * 2722,
    'responses api is not supported\ud800',
    '{"error":"responses api is not supported"',
    '["responses api is not supported"]',
    '{"metadata":"responses api is not supported"}',
    '{"error":{"message":null}}',
    '{"error":{"message":["responses api is not supported"]}}',
    *[json.dumps({'error': {field: value, 'message': 'responses api is not supported'}})
      for field in ('code', 'type') for value in ('model_not_found', 'mystery_code', None, 42)],
    json.dumps({'error': {'code': 'model_not_found', 'message': 'responses api is not supported'}}),
])
def test_classifier_unknown_incomplete_or_business_evidence_is_rejected(text):
    assert oc.is_protocol_fallback_error(404, text, protocol='responses') is False


@pytest.mark.parametrize('status', [401, 403, 429, 500, 503, 200])
def test_classifier_status_is_necessary_not_sufficient(status):
    assert not oc.is_protocol_fallback_error(status, 'responses api is not supported', protocol='responses')


@pytest.mark.parametrize('status', [400, 404, 405, 415, 422, 501])
def test_classifier_empty_or_other_protocol_never_authorizes(status):
    assert not oc.is_protocol_fallback_error(status, '', protocol='responses')
    assert not oc.is_protocol_fallback_error(status, 'responses api is not supported', protocol='chat_completions')


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('real network is forbidden')
    for target, names in ((socket.socket, ('connect', 'connect_ex', 'sendto')),
                          (socket, ('create_connection', 'getaddrinfo'))):
        for name in names:
            monkeypatch.setattr(target, name, deny)


PROTOCOL_PATHS = {
    'responses': '/v1/responses',
    'chat_completions': '/v1/chat/completions',
    'anthropic_messages': '/v1/messages',
}
UNSUPPORTED = 'responses api is not supported'
SECRET = 'provider-secret-sentinel'


def good(protocol):
    return {
        'responses': {'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': 'ok'}]}]},
        'chat_completions': {'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}]},
        'anthropic_messages': {'content': [{'type': 'text', 'text': 'ok'}], 'stop_reason': 'end_turn'},
    }[protocol]


def sse(*events):
    return ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()


class Body(httpx.AsyncByteStream):
    def __init__(self, data, *, fault=None, hanging=False, close_fault=None, task_cancel=False, deadline_scope=None):
        self.data, self.fault, self.hanging = data, fault, hanging
        self.deadline_scope = deadline_scope
        self.close_fault, self.task_cancel = close_fault, task_cancel
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        for start in range(0, len(self.data), 257):
            chunk = self.data[start:start + 257]
            self.yielded += len(chunk)
            yield chunk
        if self.task_cancel:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)
        if self.fault:
            raise self.fault
        if self.hanging:
            if self.deadline_scope is not None:
                self.deadline_scope.reschedule(asyncio.get_running_loop().time())
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True
        if self.close_fault:
            raise self.close_fault(SECRET)


def http_error(status=400, text=UNSUPPORTED, **kwargs):
    return dict(status=status, data=text.encode() if isinstance(text, str) else text, **kwargs)


class WireResult(TypedDict):
    requests: list[dict[str, Any]]
    streams: list[Body]
    events: list[ModelEvent]
    error: BaseException | None
    clients: list[FixedHostHttpClient]


def wire(actions, *, configured='auto', messages=(), model='', api_url='https://api.example.com/v1',
         maximum=2 * 1024 * 1024, deadline=60, token=None, sleep=None, exit_error=None,
         generation_profiles_json='', deadline_in_body=False, header_delay=0):
    """Exercise real adapter/client; only network transport and clocks are controlled."""
    async def run() -> WireResult:
        requests, streams, clients, events = [], [], [], []
        cancellation = token or CancellationToken()
        deadline_scope = asyncio.Timeout(None) if deadline_in_body else None

        async def handler(request):
            protocol = next(p for p, path in PROTOCOL_PATHS.items() if path == request.url.path)
            requests.append({'protocol': protocol, 'body': json.loads(request.content)})
            if header_delay:
                await asyncio.sleep(header_delay)
            action = actions[min(len(requests) - 1, len(actions) - 1)] if actions else None
            if isinstance(action, BaseException):
                raise action
            if action is None:
                action = dict(status=200, data=json.dumps(good(protocol)).encode())
            stream = Body(action.get('data', b''), fault=action.get('fault'), hanging=action.get('hanging', False),
                          close_fault=action.get('close_fault'), task_cancel=action.get('task_cancel', False), deadline_scope=deadline_scope)
            streams.append(stream)
            headers = action.get('headers', {})
            assert isinstance(headers, dict)
            return httpx.Response(cast(int, action.get('status', 200)), headers=headers, stream=stream)

        class ExitFaultClient(FixedHostHttpClient):
            @asynccontextmanager
            async def stream_post_json(self, *args, **kwargs):
                async with super().stream_post_json(*args, **kwargs) as response:
                    yield response
                if exit_error:
                    raise exit_error

        def factory(**kwargs):
            cls = ExitFaultClient if exit_error else FixedHostHttpClient
            client = cls(**kwargs, transport=httpx.MockTransport(handler), resolver=lambda host, port: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))])
            clients.append(client)
            return client

        adapter = pm.OpenAICompatibleModelAdapter(pm.ProviderSettings(
            api_url=api_url, model='fixture', api_key=SECRET, protocol=configured,
            generation_profiles_json=generation_profiles_json,
            max_response_bytes=maximum), client_factory=factory)
        request = ModelRequest(system_prompt='offline regression', messages=messages, tools=(), model=model)
        error = None
        deadline_control = patch.object(pm.asyncio, 'timeout', return_value=deadline_scope) if deadline_scope else nullcontext()
        with deadline_control, patch.object(pm, '_MODEL_RETRY_DELAY_SECONDS', 0), patch.object(
            pm, '_stream_deadline_seconds', return_value=deadline
        ):
            try:
                if sleep:
                    with patch.object(pm.asyncio, 'sleep', sleep):
                        async for event in adapter.stream(request, cancellation=cancellation):
                            events.append(event)
                else:
                    async for event in adapter.stream(request, cancellation=cancellation):
                        events.append(event)
            except BaseException as exc:
                error = exc
        if deadline_scope is not None:
            assert deadline_scope.expired(), "目标响应体必须实际触发Timeout上下文"
        assert all(client._client.is_closed for client in clients)
        assert all(stream.closed for stream in streams)
        return dict(requests=requests, streams=streams, events=events, error=error, clients=clients)
    return asyncio.run(run())


def protocols(result):
    return [request['protocol'] for request in result['requests']]


def error_chain(error):
    """Traverse both edges, rejecting cycles but allowing shared ancestors."""
    collected = []

    def visit(exc, ancestors):
        if exc is None:
            return
        assert id(exc) not in ancestors, 'cyclic exception chain'
        collected.append(exc)
        visit(exc.__cause__, ancestors | {id(exc)})
        visit(exc.__context__, ancestors | {id(exc)})
    visit(error, set())
    return collected


BUSINESS = [
    (401, '{"error":{"message":"invalid api key"}}'),
    (403, '{"error":{"message":"permission denied"}}'),
    (400, '{"error":{"code":"insufficient_quota","message":"insufficient balance"}}'),
    (404, '{"error":{"code":"model_not_found","message":"The requested model does not exist"}}'),
    (400, '{"error":{"message":"model not found"}}'),
    (400, '{"error":{"message":"temperature out of range"}}'),
    (400, '{"error":{"code":"context_length_exceeded"}}'),
    (429, '{"error":{"message":"rate limit"}}'),
    (404, '404 Not Found'),
    (404, ''),
    (404, '{"error":{"code":"model_not_found","message":"unsupported endpoint"}}'),
    (400, UNSUPPORTED + ' ' * 8200 + ' insufficient quota'),
    (400, UNSUPPORTED + ' ' * 17000),
]


@pytest.mark.parametrize('status,text', BUSINESS, ids=[f'business-{i}' for i in range(len(BUSINESS))])
def test_http_business_or_unknown_stops(agent_llm_enabled, status, text, caplog):
    result = wire([http_error(status, text), None])
    if (status, text) == BUSINESS[6]:
        assert protocols(result) == ['responses', 'responses']
        first_body, second_body = [request['body'] for request in result['requests']]
        assert first_body['max_output_tokens'] == 1500
        assert second_body['max_output_tokens'] == 750
        assert second_body == dict(first_body, max_output_tokens=750)
        assert result['error'] is None
        assert [str(event.type) for event in result['events']] == ['text_delta', 'finish']
        assert result['events'][0].text == 'ok'
        assert SECRET not in caplog.text + repr(result['events']) + repr(result['error'])
        return
    assert protocols(result) == ['responses']
    assert result['error'] and not result['events']
    chain = error_chain(result['error'])
    assert any(f'HTTP {status}' in str(exc) for exc in chain)
    assert SECRET not in caplog.text + str(result['error'])
    if status in (401, 403, 429):
        assert result['streams'][0].yielded == 0


@pytest.mark.parametrize('text', [
    '{"error":{"message":"responses api is not supported","reason":"temperature out of range"}}',
    '{"error":{"message":"responses api is not supported","detail":"\\u0071uota exceeded"}}',
    '{"error":"responses api is not supported","code":"model_not_found"}',
    '{"error":"responses api is not supported","type":"mystery_error"}',
    '{"error":"responses api is not supported","metadata":{"hint":"unreviewed"}}',
])
def test_structured_conflicting_or_unknown_reasons_stop_at_responses(agent_llm_enabled, text):
    result = wire([http_error(404, text), None])
    assert protocols(result) == ['responses']
    assert isinstance(result['error'], pm.ModelProviderError) and not result['events']
    assert any('HTTP 404' in str(exc) for exc in error_chain(result['error']))


@pytest.mark.parametrize('status,text', [
    *[(status, json.dumps({'error': UNSUPPORTED})) for status in (400, 404, 422)],
    (404, json.dumps({'error': UNSUPPORTED, 'code': 'unsupported_endpoint'})),
    (404, json.dumps({'error': UNSUPPORTED}).ljust(8192)),
    (400, UNSUPPORTED), (404, UNSUPPORTED), (422, "unknown parameter 'input'"),
    (400, UNSUPPORTED.ljust(8192)),
])
def test_http_complete_explicit_evidence_allows_one_fallback(agent_llm_enabled, status, text):
    result = wire([http_error(status, text), None])
    assert protocols(result) == ['responses', 'chat_completions']
    assert result['error'] is None and str(result['events'][-1].type) == 'finish'
    assert result['streams'][0].yielded == len(text.encode())


@pytest.mark.parametrize('action,options', [
    (http_error(text=b'\xff' + UNSUPPORTED.encode()), {}),
    (http_error(text='{"error":"' + UNSUPPORTED), {}),
    (http_error(text='{"error":"' + UNSUPPORTED, fault=httpx.ReadError(SECRET)), {}),
    (http_error(fault=httpx.ReadTimeout(SECRET)), {}),
    (http_error(fault=OSError(SECRET)), {}),
    (http_error(text=UNSUPPORTED.ljust(8193)), {}),
    (http_error(text=UNSUPPORTED.ljust(8193), headers={'content-length': '8193'}), {}),
    (http_error(text=UNSUPPORTED.ljust(100)), {'maximum': 40}),
    (http_error(headers={'content-length': '9000000'}), {}),
    (http_error(hanging=True), {'deadline': 0.02, 'deadline_in_body': True}),
    (http_error(fault=asyncio.CancelledError()), {}),
])
def test_http_error_read_must_finish_within_bounds(agent_llm_enabled, action, options, caplog):
    result = wire([action, None], **options)
    assert protocols(result) == ['responses']
    assert result['error'] is not None and not result['events']
    assert SECRET not in str(result['error']) + caplog.text
    assert all(SECRET not in str(exc) for exc in error_chain(result['error']))
    if isinstance(action.get('fault'), asyncio.CancelledError):
        assert isinstance(result['error'], asyncio.CancelledError)


@pytest.mark.parametrize('mode', ['read', 'synthetic_cancel', 'task_cancel', 'deadline'])
@pytest.mark.parametrize('close_fault', [None, httpx.ReadError, ValueError])
def test_error_read_terminal_state_survives_real_stream_cleanup(agent_llm_enabled, mode, close_fault):
    options = {
        'read': {'fault': httpx.ReadError(SECRET)},
        'synthetic_cancel': {'fault': asyncio.CancelledError(SECRET)},
        'task_cancel': {'task_cancel': True},
        'deadline': {'hanging': True},
    }[mode]
    result = wire([http_error(404, json.dumps({'error': UNSUPPORTED}), close_fault=close_fault, **options), None],
                  deadline=0.02 if mode == 'deadline' else 60, deadline_in_body=mode == 'deadline')
    assert protocols(result) == ['responses']
    assert not result['events']
    assert all(SECRET not in str(exc) for exc in error_chain(result['error']))
    if 'cancel' in mode:
        assert isinstance(result['error'], asyncio.CancelledError)
    else:
        assert isinstance(result['error'], pm.ModelProviderError)
        chain = error_chain(result['error'])
        assert any('HTTP 404' in str(exc) for exc in chain)
        if mode == 'deadline':
            assert str(result['error']) == 'Provider 请求超时'
            assert isinstance(result['error'].__cause__, TimeoutError)
        else:
            assert '读取失败' in str(result['error'])


def test_bounded_reader_checks_before_yielding_oversize_chunk():
    async def collect():
        async def chunks():
            yield b'a' * 8192
            yield b'b'
        raw = bytearray()
        with pytest.raises(pm.ModelProviderError):
            async for chunk in pm._bounded_response_bytes(chunks(), 8192):
                raw.extend(chunk)
        assert len(raw) == 8192
    asyncio.run(collect())


def stream_action(*events):
    return dict(status=200, data=sse(*events), headers={'content-type': 'text/event-stream'})


@pytest.mark.parametrize('action,count', [
    (httpx.ConnectError('offline connect'), 2),
    (httpx.ReadTimeout('offline timeout'), 2),
    (stream_action(), 2),
    (stream_action({'type': 'response.failed'}), 2),
    (http_error(503, 'temporary'), 2),
    (stream_action({'type': 'error', 'error': {'message': SECRET}}), 1),
    (ValueError('offline unexpected'), 1),
    (dict(status=200, data=b'{'), 1),
])
def test_nonnegotiation_exhaustion_stays_on_original_protocol(agent_llm_enabled, action, count):
    result = wire([action])
    assert protocols(result) == ['responses'] * count
    assert result['error'] and not result['events']
    error_chain(result['error'])
    if isinstance(action, httpx.TransportError):
        assert result['error'].__cause__ is action
    if isinstance(action, dict) and action.get('data') == b'':
        assert isinstance(result['error'], pm.UncommittableModelTurn)


@pytest.mark.parametrize('action', [
    httpx.ConnectError('offline connect'), httpx.ReadTimeout('offline timeout'),
    stream_action(), stream_action({'type': 'response.failed'}), http_error(503, 'temporary'),
])
def test_original_same_protocol_retry_can_recover(agent_llm_enabled, action):
    result = wire([action, None])
    assert protocols(result) == ['responses', 'responses']
    assert result['error'] is None


def real_retry_wire(mode, close_fault, fallback, *, header_delay=0):
    """Real retry sleep/timeout, controlled by an independent consuming task.

    Response cleanup and retry sleep remain real. Deadline expiry is armed
    at retry-sleep entry, not an assumed number of milliseconds after setup.
    """
    async def run():
        requests, streams, clients, events, cancel_calls = [], [], [], [], []
        retry_response = asyncio.Event()
        token = CancellationToken()
        request_times = []
        deadline_scope = asyncio.Timeout(None) if mode == 'deadline' else None
        real_sleep = asyncio.sleep
        retry_sleep_entered = False

        async def observed_retry_sleep(delay):
            nonlocal retry_sleep_entered
            if delay == 0.03:
                retry_sleep_entered = True
                assert deadline_scope is not None
                deadline_scope.reschedule(asyncio.get_running_loop().time())
            await real_sleep(delay)

        async def handler(request):
            protocol = next(p for p, path in PROTOCOL_PATHS.items() if path == request.url.path)
            requests.append({'protocol': protocol, 'body': json.loads(request.content)})
            request_times.append(asyncio.get_running_loop().time())
            if header_delay:
                await real_sleep(header_delay)
            if fallback and len(requests) == 1:
                status, data, fault = 404, json.dumps({'error': UNSUPPORTED}).encode(), None
            elif len(requests) == (2 if fallback else 1) or mode == 'exhaust':
                status, data, fault = 503, b'', close_fault
                retry_response.set()
            else:
                status, data, fault = 200, json.dumps(good(protocol)).encode(), None
            stream = Body(data, close_fault=fault)
            streams.append(stream)
            return httpx.Response(status, stream=stream)

        def factory(**kwargs):
            client = FixedHostHttpClient(**kwargs, transport=httpx.MockTransport(handler), resolver=lambda host, port: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))])
            clients.append(client)
            return client

        adapter = pm.OpenAICompatibleModelAdapter(pm.ProviderSettings(
            api_url='https://api.example.com/v1', model='fixture', api_key=SECRET), client_factory=factory)

        async def consume():
            error = None
            try:
                async for event in adapter.stream(ModelRequest(system_prompt='retry terminal', messages=(), tools=()),
                                                  cancellation=token):
                    events.append(event)
            except BaseException as exc:
                error = exc
            task = asyncio.current_task()
            assert task is not None
            return error, task.cancelling()

        deadline_control = patch.object(pm.asyncio, 'timeout', return_value=deadline_scope) if deadline_scope else nullcontext()
        sleep_control = patch.object(pm.asyncio, 'sleep', observed_retry_sleep) if deadline_scope else nullcontext()
        with deadline_control, sleep_control, patch.object(pm, '_MODEL_RETRY_DELAY_SECONDS', 0.03), patch.object(
            pm, '_stream_deadline_seconds', return_value=0.01 if mode == 'deadline' else 60
        ):
            task = asyncio.create_task(consume())
            if mode in ('cancel', 'token'):
                await asyncio.wait_for(retry_response.wait(), 1)
                if mode == 'cancel':
                    cancel_calls.append((task.cancel(), task.done()))
                else:
                    token.cancel()
            error, cancelling = await asyncio.wait_for(task, 1)
        if deadline_scope is not None:
            assert retry_sleep_entered and deadline_scope.expired()
        assert all(client._client.is_closed for client in clients)
        assert all(stream.closed for stream in streams)
        return dict(requests=requests, streams=streams, events=events, error=error,
                    cancel_calls=cancel_calls, cancelling=cancelling, request_times=request_times)
    return asyncio.run(run())


@pytest.mark.parametrize('fallback', [False, True], ids=['responses', 'fallback_chat'])
@pytest.mark.parametrize('close_fault', [None, httpx.ReadError], ids=['clean', 'ReadError'])
@pytest.mark.parametrize('mode', ['cancel', 'deadline'])
def test_real_retry_terminal_cause_survives_response_cleanup(agent_llm_enabled, mode, close_fault, fallback):
    result = real_retry_wire(mode, close_fault, fallback)
    assert protocols(result) == (['responses', 'chat_completions'] if fallback else ['responses'])
    assert not result['events']
    chain = error_chain(result['error'])
    assert all(SECRET not in str(exc) for exc in chain)
    if mode == 'cancel':
        assert result['cancel_calls'] == [(True, False)]
        assert isinstance(result['error'], asyncio.CancelledError)
        assert result['cancelling'] == 1
    else:
        assert isinstance(result['error'], pm.ModelProviderError)
        assert str(result['error']) == 'Provider 请求超时'
        assert isinstance(result['error'].__cause__, TimeoutError)
        assert result['cancelling'] == 0
        assert any('HTTP 503' in str(exc) for exc in chain)
        if fallback:
            assert any('HTTP 404' in str(exc) for exc in chain)


@pytest.mark.parametrize('fallback', [False, True], ids=['responses', 'fallback_chat'])
@pytest.mark.parametrize('close_fault', [None, httpx.ReadError], ids=['clean', 'ReadError'])
@pytest.mark.parametrize('mode', ['recover', 'exhaust'])
def test_real_retry_wait_preserves_attempt_budget_and_recovery(agent_llm_enabled, mode, close_fault, fallback):
    result = real_retry_wire(mode, close_fault, fallback)
    protocol = 'chat_completions' if fallback else 'responses'
    assert protocols(result) == (['responses'] if fallback else []) + [protocol, protocol]
    assert result['request_times'][-1] - result['request_times'][-2] >= 0.029
    assert result['cancelling'] == 0
    chain = error_chain(result['error'])
    assert all(SECRET not in str(exc) for exc in chain)
    if mode == 'recover':
        assert result['error'] is None
        assert [str(event.type) for event in result['events']] == ['text_delta', 'finish']
    else:
        assert isinstance(result['error'], pm.ModelProviderError) and not result['events']
        assert any('HTTP 503' in str(exc) for exc in chain)
        if fallback:
            assert any('HTTP 404' in str(exc) for exc in chain)


@pytest.mark.parametrize('fallback', [False, True], ids=['responses', 'fallback_chat'])
def test_real_retry_value_error_cleanup_stops_before_wait(agent_llm_enabled, fallback):
    # Non-transient cleanup failure ends the attempt before retry waiting;
    # no task cancellation is requested after that terminal failure.
    result = real_retry_wire('recover', ValueError, fallback)
    assert protocols(result) == (['responses', 'chat_completions'] if fallback else ['responses'])
    assert isinstance(result['error'], pm.ModelProviderError) and not result['events']
    chain = error_chain(result['error'])
    assert all(SECRET not in str(exc) for exc in chain)
    assert any('ValueError' in str(exc) for exc in chain)
    assert any('HTTP 503' in str(exc) for exc in chain)
    assert result['cancelling'] == 0


@pytest.mark.parametrize('fallback', [False, True], ids=['responses', 'fallback_chat'])
@pytest.mark.parametrize('close_fault', [None, httpx.ReadError], ids=['clean', 'ReadError'])
def test_real_retry_token_cancel_is_not_task_cancel(agent_llm_enabled, close_fault, fallback):
    result = real_retry_wire('token', close_fault, fallback)
    assert protocols(result) == (['responses', 'chat_completions'] if fallback else ['responses'])
    assert isinstance(result['error'], asyncio.CancelledError) and not result['events']
    assert result['cancelling'] == 0 and not result['cancel_calls']
    assert all(SECRET not in str(exc) for exc in error_chain(result['error']))


@pytest.mark.parametrize('action', [http_error(503), httpx.ConnectError('offline')])
def test_cancel_during_retry_delay_prevents_second_post(agent_llm_enabled, action):
    token = CancellationToken()

    async def cancel_sleep(delay):
        token.cancel()

    result = wire([action, None], token=token, sleep=cancel_sleep)
    assert protocols(result) == ['responses']
    assert isinstance(result['error'], asyncio.CancelledError)


def test_cancel_before_call_and_during_transport(agent_llm_enabled):
    token = CancellationToken()
    token.cancel()
    result = wire([None], token=token)
    assert protocols(result) == [] and isinstance(result['error'], asyncio.CancelledError)
    result = wire([asyncio.CancelledError()])
    assert protocols(result) == ['responses'] and isinstance(result['error'], asyncio.CancelledError)


@pytest.mark.parametrize('event', [
    {'type': 'response.output_text.delta', 'delta': 'visible'},
    {'type': 'response.output_item.done', 'output_index': 0, 'item': {
        'type': 'function_call', 'id': 'i1', 'call_id': 'c1', 'name': 'inspect', 'arguments': '{}'}},
])
def test_emitted_text_or_tool_prevents_replay(agent_llm_enabled, event):
    result = wire([stream_action(event, {'type': 'response.failed'})])
    assert protocols(result) == ['responses']
    assert result['events'] and result['error']


def test_emitted_usage_prevents_retry(agent_llm_enabled):
    result = wire([stream_action({'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})],
                  configured='chat_completions')
    assert protocols(result) == ['chat_completions']
    assert [str(e.type) for e in result['events']] == ['usage'] and result['error']


def test_json_usage_and_finish_prevent_replay_on_context_exit(agent_llm_enabled):
    payload = dict(status='completed', output=[], usage={'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2})
    result = wire([dict(status=200, data=json.dumps(payload).encode())],
                  exit_error=httpx.ReadError('offline exit'))
    assert protocols(result) == ['responses']
    assert [str(e.type) for e in result['events']] == ['usage', 'finish'] and result['error']


@pytest.mark.parametrize('failure', [
    http_error(403, 'permission denied'), ValueError(SECRET), dict(status=200, data=b'{'),
])
def test_fallback_failure_preserves_safe_original_http_cause(agent_llm_enabled, failure):
    result = wire([http_error(404), failure])
    assert protocols(result) == ['responses', 'chat_completions']
    chain = error_chain(result['error'])
    assert any('HTTP 404' in str(exc) for exc in chain)
    if isinstance(failure, dict) and failure.get('status') == 403:
        assert 'HTTP 403' in str(result['error'])
    if isinstance(failure, ValueError):
        assert failure not in chain and any('ValueError' in str(exc) for exc in chain)
    assert all(SECRET not in str(exc) for exc in chain)


def test_context_exit_failure_revokes_classified_fallback(agent_llm_enabled):
    failure = ValueError(SECRET)
    result = wire([http_error(404)], exit_error=failure)
    assert protocols(result) == ['responses']
    chain = error_chain(result['error'])
    assert failure not in chain and any('HTTP 404' in str(exc) for exc in chain)
    assert any('ValueError' in str(exc) for exc in chain)
    assert all(SECRET not in str(exc) for exc in chain)


@pytest.mark.parametrize('mode', ['read', 'classified', 'chat', 'partial_chat', 'prior_transport'])
def test_negotiation_error_graph_keeps_only_safe_diagnostics(agent_llm_enabled, mode, caplog):
    body = json.dumps({'error': UNSUPPORTED})
    failure = ValueError(SECRET)
    failure.__cause__ = OSError(SECRET)
    failure.__context__ = RuntimeError(SECRET)
    actions = {
        'read': [http_error(404, body, fault=httpx.ReadError(SECRET), close_fault=ValueError)],
        'classified': [http_error(404, body, close_fault=ValueError)],
        'chat': [http_error(404, body), failure],
        'partial_chat': [http_error(404, body), dict(stream_action(
            {'choices': [{'delta': {'content': 'visible'}}]}), fault=failure)],
        'prior_transport': [httpx.ConnectError(SECRET), http_error(404, '{}')],
    }[mode]
    result = wire(actions)
    expected = ['responses'] if mode in ('read', 'classified') else [
        'responses', 'responses' if mode == 'prior_transport' else 'chat_completions']
    assert protocols(result) == expected
    assert isinstance(result['error'], pm.ModelProviderError)
    assert all(str(event.type) != 'finish' for event in result['events'])
    chain = error_chain(result['error'])
    assert any('HTTP 404' in str(exc) for exc in chain)
    assert all(SECRET not in str(exc) and exc is not failure for exc in chain)
    assert SECRET not in caplog.text


@pytest.mark.parametrize('protocol', list(PROTOCOL_PATHS))
def test_explicit_protocol_or_endpoint_never_negotiates(agent_llm_enabled, protocol):
    result = wire([http_error(404)], configured=protocol)
    assert protocols(result) == [protocol] and result['error']
    result = wire([http_error(404)], api_url='https://api.example.com' + PROTOCOL_PATHS[protocol])
    assert protocols(result) == [protocol] and result['error']


NATIVE_LAYOUTS = {
    'responses': [
        {'type': 'reasoning', 'id': 'r1', 'encrypted_content': 'opaque-fixture/+==\n字'},
        {'type': 'tool_ref', 'call_id': 'c1', 'id': 'i1'},
    ],
    'chat_completions': [{'type': 'reasoning_content', 'text': 'private-fixture\n字'}],
    'anthropic_messages': [
        {'type': 'thinking', 'thinking': 'private-fixture\n字', 'signature': 'signature-fixture/+==\n字'},
        {'type': 'tool_ref', 'call_id': 'c1'},
    ],
}


def native_messages(protocol):
    return (
        ModelMessage('assistant', tool_calls=(ModelToolCall('c1', 'inspect', {}),), provider_state=ProviderState(
            protocol, 'fixture', 'https://api.example.com' + PROTOCOL_PATHS[protocol],
            json.dumps(NATIVE_LAYOUTS[protocol], ensure_ascii=False))),
        ModelMessage('tool', content='{"ok":true}', tool_call_id='c1'),
    )


def assert_native_payload(protocol, body):
    if protocol == 'responses':
        assert NATIVE_LAYOUTS[protocol][0] in body['input']
        assert any(item.get('call_id') == 'c1' and item.get('type') == 'function_call' for item in body['input'])
        assert any(item.get('call_id') == 'c1' and item.get('type') == 'function_call_output' for item in body['input'])
    elif protocol == 'chat_completions':
        assistant = next(item for item in body['messages'] if item['role'] == 'assistant')
        assert assistant['reasoning_content'] == NATIVE_LAYOUTS[protocol][0]['text']
        assert assistant['tool_calls'][0]['id'] == 'c1'
        assert any(item.get('tool_call_id') == 'c1' for item in body['messages'])
    else:
        assistant = next(item for item in body['messages'] if item['role'] == 'assistant')
        assert NATIVE_LAYOUTS[protocol][0] in assistant['content']
        assert any(item.get('type') == 'tool_use' and item.get('id') == 'c1' for item in assistant['content'])
        assert any(item.get('type') == 'tool_result' and item.get('tool_use_id') == 'c1'
                   for message in body['messages'] if message['role'] == 'user' for item in message['content'])


@pytest.mark.parametrize('protocol', list(PROTOCOL_PATHS))
def test_auto_native_binding_selects_exact_protocol(agent_llm_enabled, protocol, caplog):
    messages = native_messages(protocol)
    assert messages[0].provider_state is not None
    before = messages[0].provider_state.layout_json
    result = wire([None], messages=messages, api_url='https://api.example.com/v1/')
    assert protocols(result) == [protocol]
    assert result['error'] is None
    assert_native_payload(protocol, result['requests'][0]['body'])
    assert messages[0].provider_state.layout_json == before
    assert all(marker not in caplog.text for marker in ('opaque-fixture', 'signature-fixture', 'private-fixture'))


@pytest.mark.parametrize('protocol', list(PROTOCOL_PATHS))
def test_bound_native_unsupported_never_strips_state_to_negotiate(agent_llm_enabled, protocol):
    result = wire([http_error(404), None], messages=native_messages(protocol))
    assert protocols(result) == [protocol] and result['error']
    assert_native_payload(protocol, result['requests'][0]['body'])
    assert result['streams'][0].yielded == 0


@pytest.mark.parametrize('protocol', list(PROTOCOL_PATHS))
@pytest.mark.parametrize('action', [http_error(503), httpx.ConnectError('offline native')])
def test_native_same_protocol_retry_keeps_full_identical_payload(agent_llm_enabled, protocol, action):
    result = wire([action, None], messages=native_messages(protocol))
    assert protocols(result) == [protocol, protocol] and result['error'] is None
    assert result['requests'][0]['body'] == result['requests'][1]['body']
    for request in result['requests']:
        assert_native_payload(protocol, request['body'])


def test_conflicting_bound_protocols_fail_before_client_creation(agent_llm_enabled):
    messages = native_messages('responses') + native_messages('chat_completions')
    result = wire([None], messages=messages)
    assert protocols(result) == [] and result['clients'] == []
    assert isinstance(result['error'], pm.ModelProviderError)
    assert all(marker not in str(result['error']) for marker in ('opaque-fixture', 'private-fixture'))


@pytest.mark.parametrize('change', ['model', 'endpoint', 'user_role', 'unknown_protocol'])
def test_unbound_or_user_state_does_not_lock_or_leak(agent_llm_enabled, change):
    assistant, tool = native_messages('anthropic_messages')
    state = assistant.provider_state
    assert state is not None
    if change == 'user_role':
        assistant = replace(assistant, role='user', tool_calls=())
    else:
        state = replace(state, **{
            'model': {'model': 'other'}, 'endpoint': {'endpoint': 'https://other.example.com/v1/messages'},
            'unknown_protocol': {'protocol': 'unknown'},
        }[change])
        assistant = replace(assistant, provider_state=state)
    result = wire([http_error(), None], messages=(assistant, tool))
    assert protocols(result) == ['responses', 'chat_completions'] and result['error'] is None
    for request in result['requests']:
        serialized = json.dumps(request['body'])
        assert 'signature-fixture' not in serialized and 'private-fixture' not in serialized


def test_request_model_override_is_native_binding_authority(agent_llm_enabled):
    assistant, tool = native_messages('chat_completions')
    assert assistant.provider_state is not None
    assistant = replace(assistant, provider_state=replace(assistant.provider_state, model='override'))
    result = wire([None], messages=(assistant, tool), model='override')
    assert protocols(result) == ['chat_completions']
    assert result['requests'][0]['body']['model'] == 'override'
    assert_native_payload('chat_completions', result['requests'][0]['body'])
    result = wire([None], messages=native_messages('chat_completions'), model='override')
    assert protocols(result) == ['responses']
    assert 'private-fixture' not in json.dumps(result['requests'][0]['body'])


def test_explicit_configuration_retains_existing_native_isolation(agent_llm_enabled):
    result = wire([None], configured='responses', messages=native_messages('anthropic_messages'))
    assert protocols(result) == ['responses'] and result['error'] is None
    assert 'signature-fixture' not in json.dumps(result['requests'][0]['body'])
    result = wire([None], configured='anthropic_messages', messages=native_messages('anthropic_messages'))
    assert protocols(result) == ['anthropic_messages'] and result['error'] is None
    assert_native_payload('anthropic_messages', result['requests'][0]['body'])


@pytest.mark.parametrize('text', [
    '{"error":{"code":"model_not_found","code":"unsupported_endpoint","message":"responses api is not supported"}}',
    '{"error":{"message":"model not found"},"error":"responses api is not supported"}',
    '{"error":' + '[' * 3500 + '0' + ']' * 3500 + '}',
])
def test_classifier_ambiguous_duplicate_or_overdeep_json_is_closed(text):
    assert oc.is_protocol_fallback_error(400, text, protocol='responses') is False


def test_error_body_timeout_preserves_safe_http_diagnostic(agent_llm_enabled):
    result = wire([http_error(404, hanging=True)], deadline=0.02, deadline_in_body=True)
    assert protocols(result) == ['responses']
    assert isinstance(result['error'], pm.ModelProviderError)
    assert isinstance(result['error'].__cause__, TimeoutError)
    assert any('HTTP 404' in str(exc) for exc in error_chain(result['error']))


def test_empty_json_finish_is_already_emitted_on_exit_failure(agent_llm_enabled):
    result = wire([dict(status=200, data=b'{"status":"completed","output":[]}')],
                  exit_error=httpx.ReadError('offline exit'))
    assert protocols(result) == ['responses']
    assert [str(event.type) for event in result['events']] == ['finish']
    assert result['error']


def test_fallback_then_partial_chat_failure_keeps_both_diagnostics(agent_llm_enabled):
    action = stream_action({'choices': [{'delta': {'content': 'visible'}}]})
    result = wire([http_error(404), action])
    assert protocols(result) == ['responses', 'chat_completions']
    assert result['events'] and result['error']
    chain = error_chain(result['error'])
    assert any('HTTP 404' in str(exc) for exc in chain)
    assert any('流在完成事件前中断' in str(exc) for exc in chain)


@pytest.mark.parametrize('maximum,declared', [(2 * 1024 * 1024, '8193'), (128, '129')])
def test_declared_error_length_above_effective_cap_is_not_read(agent_llm_enabled, maximum, declared):
    result = wire([http_error(headers={'content-length': declared}), None], maximum=maximum)
    assert protocols(result) == ['responses'] and result['error']
    assert result['streams'][0].yielded == 0


def test_raw_error_text_never_enters_diagnostics_or_logs(agent_llm_enabled, caplog):
    result = wire([http_error(400, json.dumps({'error': {'code': 'insufficient_quota', 'message': SECRET}}))])
    assert protocols(result) == ['responses'] and result['error']
    assert SECRET not in caplog.text
    assert all(SECRET not in str(exc) for exc in error_chain(result['error']))


@pytest.mark.parametrize('protocol', ['chat_completions', 'anthropic_messages'])
def test_multiple_matching_states_are_not_a_conflict(agent_llm_enabled, protocol):
    messages = native_messages(protocol)
    result = wire([None], messages=messages + messages)
    assert protocols(result) == [protocol] and result['error'] is None


def test_transport_fault_after_classification_revokes_permission_when_exhausted(agent_llm_enabled):
    result = wire([http_error(404)], exit_error=httpx.ReadError('offline repeated exit'))
    assert protocols(result) == ['responses', 'responses']
    assert result['error']
    assert any('HTTP 404' in str(exc) for exc in error_chain(result['error']))


def test_fallback_chat_keeps_its_original_retry_budget(agent_llm_enabled):
    result = wire([http_error(404), http_error(503)])
    assert protocols(result) == ['responses', 'chat_completions', 'chat_completions']
    chain = error_chain(result['error'])
    assert any('HTTP 404' in str(exc) for exc in chain)
    assert 'HTTP 503' in str(result['error'])


def real_success_body_wire(mode, close_fault, fallback, encoding, *, header_delay=0):
    """Keep a real HTTP 200 body pending through cancellation and cleanup.

    SSE text reaches the consumer before the reader signals the emitted case.
    Deadline expiry uses a real asyncio.Timeout, armed only once the intended
    body phase is reached; slow setup must not accidentally test a different phase.
    """
    async def run():
        entered = asyncio.Event()
        requests, streams, clients, events, trace, cancel_calls = [], [], [], [], [], []
        request_times = []

        class PendingBody(httpx.AsyncByteStream):
            def __init__(self, data, pending):
                self.data, self.pending = data, pending
                self.closed = False

            async def __aiter__(self):
                yield self.data
                if self.pending:
                    trace.append('body_pending')
                    entered.set()
                    if mode == 'synthetic':
                        raise asyncio.CancelledError(SECRET)
                    if mode == 'recover':
                        raise httpx.ReadTimeout(SECRET)
                    await asyncio.Event().wait()

            async def aclose(self):
                self.closed = True
                if self.pending:
                    trace.append('body_closed')
                    if close_fault:
                        raise close_fault(SECRET) from OSError(SECRET)

        async def handler(request):
            if header_delay:
                await asyncio.sleep(header_delay)
            protocol = next(p for p, path in PROTOCOL_PATHS.items() if path == request.url.path)
            requests.append({'protocol': protocol, 'body': json.loads(request.content)})
            request_times.append(asyncio.get_running_loop().time())
            trace.append('POST_' + protocol)
            status, headers, pending = 200, {}, False
            if fallback and len(requests) == 1:
                status, data = 404, json.dumps({'error': UNSUPPORTED}).encode()
            elif len(requests) == (2 if fallback else 1):
                pending = True
                if encoding == 'json':
                    data = b'{'
                else:
                    headers = {'content-type': 'text/event-stream'}
                    event = ({'type': 'response.output_text.delta', 'delta': 'visible'}
                             if protocol == 'responses' else {'choices': [{'delta': {'content': 'visible'}}]})
                    data = sse(event) if encoding == 'emitted_sse' else b': keepalive\n\n'
            else:
                data = json.dumps(good(protocol)).encode()
            stream = PendingBody(data, pending)
            streams.append(stream)
            return httpx.Response(status, headers=headers, stream=stream)

        def factory(**kwargs):
            client = FixedHostHttpClient(**kwargs, transport=httpx.MockTransport(handler), resolver=lambda host, port: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))])
            clients.append(client)
            return client

        adapter = pm.OpenAICompatibleModelAdapter(pm.ProviderSettings(
            api_url='https://api.example.com/v1', model='fixture', api_key=SECRET), client_factory=factory)

        async def consume():
            error = None
            try:
                async for event in adapter.stream(ModelRequest(system_prompt='success body terminal', messages=(), tools=()),
                                                  cancellation=CancellationToken()):
                    events.append(event)
                    trace.append('emitted_' + str(event.type))
            except BaseException as exc:
                error = exc
            trace.append('consumer_terminal')
            task = asyncio.current_task()
            assert task is not None
            return error, task.cancelling()

        deadline_scope = asyncio.Timeout(None) if mode == 'deadline' else None
        deadline_control = patch.object(pm.asyncio, 'timeout', return_value=deadline_scope) if deadline_scope else nullcontext()
        with patch.object(pm, '_MODEL_RETRY_DELAY_SECONDS', 0.005), patch.object(
            pm, '_stream_deadline_seconds', return_value=0.04 if mode == 'deadline' else 60
        ), deadline_control:
            task = asyncio.create_task(consume())
            if mode in {'cancel', 'deadline'}:
                await asyncio.wait_for(entered.wait(), 1)
                assert not task.done()
                if deadline_scope is not None:
                    trace.append('controller_deadline')
                    deadline_scope.reschedule(asyncio.get_running_loop().time())
                else:
                    cancel_calls.append((task.cancel(SECRET), task.done()))
                    trace.append('controller_cancel')
            error, cancelling = await asyncio.wait_for(task, 1)
        if deadline_scope is not None:
            assert deadline_scope.expired()
            assert trace.index('body_pending') < trace.index('controller_deadline') < trace.index('body_closed')
        assert entered.is_set()
        assert clients and all(client._client.is_closed for client in clients)
        assert streams and all(stream.closed for stream in streams)
        assert trace.index('body_pending') < trace.index('body_closed') < trace.index('consumer_terminal')
        if encoding == 'emitted_sse':
            assert trace.index('emitted_text_delta') < trace.index('body_pending')
            assert events[0].text == 'visible'
        if mode == 'cancel':
            assert cancel_calls == [(True, False)]
            assert trace.index('body_pending') < trace.index('controller_cancel') < trace.index('body_closed')
        return dict(requests=requests, events=events, error=error, trace=trace,
                    cancel_calls=cancel_calls, cancelling=cancelling, request_times=request_times)
    return asyncio.run(run())


@pytest.mark.parametrize('fallback', [False, True], ids=['responses', 'fallback_chat'])
@pytest.mark.parametrize('encoding', ['sse', 'json', 'emitted_sse'])
@pytest.mark.parametrize('close_fault', [None, httpx.ReadError, ValueError], ids=['clean', 'ReadError', 'ValueError'])
@pytest.mark.parametrize('mode', ['cancel', 'deadline', 'synthetic'])
def test_success_body_terminal_survives_real_cleanup(agent_llm_enabled, mode, close_fault, fallback, encoding):
    result = real_success_body_wire(mode, close_fault, fallback, encoding)
    assert protocols(result) == (['responses', 'chat_completions'] if fallback else ['responses'])
    assert [str(event.type) for event in result['events']] == (['text_delta'] if encoding == 'emitted_sse' else [])
    if mode == 'deadline':
        assert isinstance(result['error'], pm.ModelProviderError)
        assert str(result['error']) == 'Provider 请求超时'
        assert isinstance(result['error'].__cause__, TimeoutError)
        assert result['cancelling'] == 0
    else:
        assert isinstance(result['error'], asyncio.CancelledError)
        assert result['cancelling'] == (1 if mode == 'cancel' else 0)
    chain = error_chain(result['error'])
    assert all(SECRET not in str(exc) for exc in chain)
    if fallback:
        assert any('HTTP 404' in str(exc) for exc in chain)


@pytest.mark.parametrize('fallback', [False, True], ids=['responses', 'fallback_chat'])
@pytest.mark.parametrize('encoding', ['sse', 'json', 'emitted_sse'])
@pytest.mark.parametrize('close_fault', [None, httpx.ReadError, ValueError], ids=['clean', 'ReadError', 'ValueError'])
def test_success_body_read_timeout_preserves_normal_retry(agent_llm_enabled, close_fault, fallback, encoding):
    result = real_success_body_wire('recover', close_fault, fallback, encoding)
    prefix = ['responses', 'chat_completions'] if fallback else ['responses']
    should_retry = close_fault is not ValueError and encoding != 'emitted_sse'
    assert protocols(result) == (prefix + [prefix[-1]] if should_retry else prefix)
    assert result['cancelling'] == 0 and not result['cancel_calls']
    if should_retry:
        assert result['error'] is None
        assert [str(event.type) for event in result['events']] == ['text_delta', 'finish']
        assert result['requests'][-1]['body'] == result['requests'][-2]['body']
        assert result['request_times'][-1] - result['request_times'][-2] >= 0.004
    else:
        assert result['error'] is not None
        assert [str(event.type) for event in result['events']] == (['text_delta'] if encoding == 'emitted_sse' else [])
    chain = error_chain(result['error'])
    if fallback:
        assert all(SECRET not in str(exc) for exc in chain)
        if not should_retry:
            assert any('HTTP 404' in str(exc) for exc in chain)


def test_success_stream_total_deadline_never_starts_another_request(agent_llm_enabled):
    action = dict(status=200, data=b'', hanging=True, headers={'content-type': 'text/event-stream'})
    result = wire([action, None], deadline=0.02, deadline_in_body=True)
    assert protocols(result) == ['responses'] and not result['events']
    assert isinstance(result['error'], pm.ModelProviderError)
    assert str(result['error']) == 'Provider 请求超时'
    assert isinstance(result['error'].__cause__, TimeoutError)


def test_success_body_deadline_is_triggered_after_slow_headers_not_during_setup(agent_llm_enabled):
    # 两次响应头均超过旧40ms全轮期限；本用例要验证的是body清理而非建连超时。
    result = real_success_body_wire('deadline', httpx.ReadError, True, 'sse', header_delay=0.06)
    assert protocols(result) == ['responses', 'chat_completions']
    assert isinstance(result['error'], pm.ModelProviderError)
    assert isinstance(result['error'].__cause__, TimeoutError)
    assert result['trace'].index('body_pending') < result['trace'].index('body_closed')
    assert result['cancelling'] == 0 and not result['cancel_calls']
    assert all(SECRET not in str(exc) for exc in error_chain(result['error']))


def test_error_body_deadline_waits_for_slow_headers_and_keeps_http_evidence(agent_llm_enabled):
    result = wire([http_error(404, hanging=True)], deadline=0.02,
                  deadline_in_body=True, header_delay=0.06)
    assert protocols(result) == ['responses'] and not result['events']
    assert result['error'] is not None
    assert isinstance(result['error'].__cause__, TimeoutError)
    assert any('HTTP 404' in str(exc) for exc in error_chain(result['error']))


def test_retry_deadline_waits_for_real_retry_sleep_after_slow_headers(agent_llm_enabled):
    result = real_retry_wire('deadline', httpx.ReadError, True, header_delay=0.06)
    assert protocols(result) == ['responses', 'chat_completions']
    assert result['error'] is not None
    assert isinstance(result['error'].__cause__, TimeoutError)
    assert isinstance(result['error'], pm.ModelProviderError)
    assert result['error'].failure is not None
    assert result['error'].failure.phase == 'retry_wait'
    assert not result['events'] and result['cancelling'] == 0
    chain = error_chain(result['error'])
    assert any('HTTP 503' in str(exc) for exc in chain)
    assert any('HTTP 404' in str(exc) for exc in chain)


def test_real_total_deadline_before_headers_still_stops_without_retry(agent_llm_enabled):
    # 不控制目标阶段：仍直接使用真正的短整轮deadline，证明请求前等待照常受限。
    result = wire([None], deadline=0.01, header_delay=0.06)
    assert len(result['requests']) <= 1 and not result['events']
    assert result['error'] is not None
    assert isinstance(result['error'].__cause__, TimeoutError)
    assert isinstance(result['error'], pm.ModelProviderError)
    assert result['error'].failure is not None
    assert result['error'].failure.kind == 'deadline'
    assert isinstance(result['error'], pm.ModelProviderError)
    assert result['error'].failure.phase == 'request'
    assert isinstance(result['error'], pm.ModelProviderError)
    assert result['error'].failure.http_status is None


@pytest.mark.parametrize('prior_budget_rejection', [False, True])
def test_anthropic_unreplayable_arguments_keep_reason_without_adapter_resend(agent_llm_enabled, prior_budget_rejection):
    malformed = {
        'content': [
            {'type': 'thinking', 'thinking': 'private thought', 'signature': 'signed-private'},
            {'type': 'tool_use', 'id': 'uncommitted-call', 'name': 'lookup',
             'input': '{"query":"' + SECRET + '",'},
        ],
        'stop_reason': 'tool_use',
    }
    actions = ([http_error(400, '{"error":{"code":"context_length_exceeded"}}')]
               if prior_budget_rejection else [])
    actions.append(dict(status=200, data=json.dumps(malformed).encode()))
    result = wire(actions, configured='anthropic_messages')
    assert protocols(result) == ['anthropic_messages'] * (2 if prior_budget_rejection else 1)
    assert not result['events']
    error = result['error']
    assert isinstance(error, pm.UncommittableModelTurn)
    assert error.reason == 'unreplayable_arguments'
    assert error.failure is not None
    assert error.failure.kind == 'invalid_tool_arguments'
    assert error.failure.http_status == 200 and error.failure.emitted is False
    assert all(SECRET not in str(exc) for exc in error_chain(error))
    if prior_budget_rejection:
        assert any('HTTP 400' in str(exc) for exc in error_chain(error))
        assert result['requests'][1]['body']['max_tokens'] < result['requests'][0]['body']['max_tokens']
        assert result['requests'][1]['body']['messages'] == result['requests'][0]['body']['messages']
