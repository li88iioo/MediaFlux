"""Bounded window feedback at the actual native/MockTransport boundary."""
import asyncio
import json

import httpx
import pytest

from app.agent.kernel.model import (
    ModelMessage,
    ModelRequest,
    ModelToolCall,
    ProviderState,
)
from app.agent.kernel.provider_model import (
    ModelProviderError,
    OpenAICompatibleModelAdapter,
    ProviderSettings,
)
from app.agent.kernel.state_types import CancellationToken
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414
from tests.test_agent_generation_profiles import (
    BASE,
    PATHS,
    Wire,
    assert_safe_error,
    response,
)


class FeedbackWire(Wire):
    def __init__(self, replies):
        super().__init__()
        self.replies = list(replies)

    async def handler(self, request):
        protocol = next(key for key, path in PATHS.items() if request.url.path == path)
        self.requests.append({'protocol': protocol, 'path': request.url.path, 'body': json.loads(request.content)})
        return self.replies.pop(0) if self.replies else httpx.Response(200, json=response(protocol, 'ok'))


def window_error(status=400, **overrides):
    return httpx.Response(status, json={'error': {'code': 'context_length_exceeded',
        'message': 'secret-sentinel: untrusted context 99999999 route https://evil.invalid', **overrides}})


def run(wire, *, private=False, output=2048, context=8192, content='full fact tail', protocol=None):
    protocol = protocol or ('auto' if private else 'chat_completions')
    messages = [ModelMessage('user', content)]
    if private:
        messages += [ModelMessage('assistant', tool_calls=(ModelToolCall('c1', 'inspect', {'q': 'keep'}),),
            provider_state=ProviderState('anthropic_messages', 'fixture', BASE + '/messages', json.dumps([
                {'type': 'thinking', 'thinking': 'private-state', 'signature': 'signed-unchanged'},
                {'type': 'tool_ref', 'call_id': 'c1'}]))),
            ModelMessage('tool', '{"full_fact":"tail preserved"}', tool_call_id='c1', tool_name='inspect')]
    settings = ProviderSettings(api_url=BASE, model='fixture', protocol=protocol, context_window_tokens=context)
    adapter = OpenAICompatibleModelAdapter(settings, client_factory=wire.factory)
    request = ModelRequest(system_prompt='offline', messages=tuple(messages), tools=(
        {'type': 'function', 'function': {'name': 'inspect', 'description': 'offline',
            'parameters': {'type': 'object', 'properties': {'q': {'type': 'string'}}}}},), max_output_tokens=output)
    async def consume():
        return [event async for event in adapter.stream(request, cancellation=CancellationToken())]
    try:
        return asyncio.run(consume())
    finally:
        assert wire.clients and all(client._client.is_closed for client in wire.clients)


@pytest.mark.parametrize('private', [False, True])
def test_explicit_window_feedback_reduces_only_output_and_preserves_native(agent_llm_enabled, private):
    wire = FeedbackWire([window_error()])
    events = run(wire, private=private)
    assert ''.join(event.text for event in events) == 'ok'
    assert len(wire.requests) == 2
    first, second = wire.requests
    assert first['protocol'] == second['protocol'] == ('anthropic_messages' if private else 'chat_completions')
    assert first['path'] == second['path']
    expected = dict(first['body'], max_tokens=1024)
    assert first['body']['max_tokens'] == 2048
    assert second['body'] == expected
    if private:
        assert second['body']['messages'][1]['content'][0] == {
            'type': 'thinking', 'thinking': 'private-state', 'signature': 'signed-unchanged'}
        assert 'tail preserved' in json.dumps(second['body'])


@pytest.mark.parametrize('reply', [
    window_error(401), window_error(429),
    window_error(code='invalid_request_error'),
    httpx.Response(400, text='context_length_exceeded secret-sentinel'),
    httpx.Response(400, json={'error': {'message': 'context_length_exceeded'}}),
    httpx.Response(400, content=b'{"error":{"code":"context_length_exceeded","code":"invalid"}}'),
])
def test_other_failures_are_not_feedback_or_protocol_fallback(agent_llm_enabled, reply):
    wire = FeedbackWire([reply])
    with pytest.raises(ModelProviderError) as caught:
        run(wire, protocol='auto')
    assert len(wire.requests) == 1
    assert_safe_error(caught.value)


def test_feedback_is_at_most_once_even_for_private_native(agent_llm_enabled):
    wire = FeedbackWire([window_error(), window_error()])
    with pytest.raises(ModelProviderError) as caught:
        run(wire, private=True)
    assert len(wire.requests) == 2
    assert wire.requests[1]['body']['max_tokens'] == 1024
    assert_safe_error(caught.value)


@pytest.mark.parametrize('output,context,content', [(255,8192,'fact'), (2048,1024,'fact' * 400)])
def test_feedback_refuses_when_safe_output_or_full_body_cannot_fit(agent_llm_enabled, output, context, content):
    wire = FeedbackWire([window_error()])
    with pytest.raises(ModelProviderError):
        run(wire, output=output, context=context, content=content)
    assert len(wire.requests) == 1


def test_feedback_never_unlocks_auto_protocol_fallback(agent_llm_enabled):
    wire = FeedbackWire([window_error(), httpx.Response(404, json={
        'error': {'code': 'unsupported_endpoint', 'message': 'responses endpoint is not supported'}})])
    with pytest.raises(ModelProviderError):
        run(wire, protocol='auto')
    assert len(wire.requests) == 2
    assert [item['protocol'] for item in wire.requests] == ['responses', 'responses']
    assert wire.requests[1]['body']['max_output_tokens'] == 1024


@pytest.mark.parametrize('delta', [
    {'content': 'already visible'},
    {'reasoning_content': 'private already received'},
    {'tool_calls': [{'index': 0, 'id': 'c1', 'type': 'function',
                     'function': {'name': 'inspect', 'arguments': '{"q":"keep"}'}}]},
])
def test_stream_window_error_never_replays_partial_output(agent_llm_enabled, delta):
    frames = [ {'choices': [{'delta': delta}]},
        {'error': {'code': 'context_length_exceeded', 'message': 'secret-sentinel'}} ]
    wire = FeedbackWire([httpx.Response(200, headers={'content-type': 'text/event-stream'},
        content=''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames).encode())])
    with pytest.raises(ModelProviderError) as caught:
        run(wire)
    assert len(wire.requests) == 1
    assert_safe_error(caught.value)


def test_oversized_feedback_is_not_retried(agent_llm_enabled):
    wire = FeedbackWire([httpx.Response(400, content=b'{"error":{"code":"context_length_exceeded","message":"' + b'x' * 65536 + b'"}}')])
    with pytest.raises(ModelProviderError) as caught:
        run(wire)
    assert len(wire.requests) == 1
    assert_safe_error(caught.value)
