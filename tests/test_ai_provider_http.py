"""管理员配置的 HTTP 模型服务：真实 TCP，隔离配置，不访问外部服务。"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.agent.kernel.model import ModelMessage, ModelRequest
from app.agent.kernel.provider_model import OpenAICompatibleModelAdapter, ProviderSettings
from app.agent.kernel.state_types import CancellationToken
from app.clients.ai_recognition import AIRecognitionClient
from app.clients.http import FixedHostHttpClient, HttpSecurityError
from app.clients.openai_compatible import normalize_provider_location
from tests.test_agent_generation_profiles import (
    HighReasoningWire, PATHS, PROTOCOLS, config_api as config_api, local_config as local_config,
)


@pytest.fixture
def http_provider():
    seen = []
    wire = HighReasoningWire()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def reply(self, body, content_type='application/json'):
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            seen.append((self.path, self.headers.get('Host')))
            assert self.path == '/v1/models'
            self.reply(b'{"data":[{"id":"local-model"}]}')

        def do_POST(self):
            seen.append((self.path, self.headers.get('Host')))
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            protocol = next(key for key, path in PATHS.items() if path == self.path)
            if not body.get('stream'):
                request = httpx.Request('POST', 'http://localhost' + self.path, json=body)
                response = asyncio.run(wire.handler(request))
                assert response.status_code == 200
                self.reply(response.content)
                return
            frames = {
                'chat_completions': [
                    {'choices': [{'delta': {'content': 'OK'}, 'finish_reason': None}]},
                    {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
                ],
                'responses': [
                    {'type': 'response.output_text.delta', 'delta': 'OK'},
                    {'type': 'response.completed', 'response': {'status': 'completed', 'output': []}},
                ],
                'anthropic_messages': [
                    {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                    {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'OK'}},
                    {'type': 'content_block_stop', 'index': 0},
                    {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}},
                    {'type': 'message_stop'},
                ],
            }[protocol]
            payload = ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames)
            if protocol == 'chat_completions':
                payload += 'data: [DONE]\n\n'
            self.reply(payload.encode(), 'text/event-stream')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_http_save_models_probe_stream_and_shared_recognition(config_api, http_provider, protocol):
    base, seen = http_provider
    saved = config_api.post('/api/config', json={
        'AGENT_LLM_API_URL': base, 'AGENT_LLM_MODEL': 'local-model',
        'AGENT_LLM_PROTOCOL': protocol, 'AGENT_LLM_REASONING_EFFORT': 'high',
    })
    assert saved.status_code == 200, saved.text
    assert config_api.get('/api/config').json()['AGENT_LLM_API_URL'] == base
    models = config_api.post('/api/tools/ai/models', json={})
    assert models.status_code == 200, models.text
    assert models.json()['models'] == ['local-model']
    probe = config_api.post('/api/tools/ai/test', json={})
    assert probe.status_code == 200, probe.text
    assert probe.json()['capabilities'] == {
        'structured_output': True, 'tool_calling': True, 'streaming': True,
    }

    async def run():
        adapter = OpenAICompatibleModelAdapter(ProviderSettings.from_config())
        request = ModelRequest(system_prompt='test', messages=(ModelMessage('user', 'hello'),), tools=())
        return [event async for event in adapter.stream(request, cancellation=CancellationToken())]

    events = asyncio.run(run())
    assert ''.join(event.text for event in events) == 'OK'
    assert events[-1].type.value == 'finish'
    client = AIRecognitionClient(api_url=base, model='local-model', api_key='fixture', protocol=protocol)
    assert client._request_structured(system_prompt='test', payload={}, schema_name='test', schema={
        'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'],
        'additionalProperties': False,
    }) == {'ok': True}
    assert len(seen) == 6  # 模型列表、三种探测、流式调用、识别复用。
    assert {host for _, host in seen} == {base.split('/')[2]}


@pytest.mark.parametrize('url', [
    'http://localhost:8080/v1', 'http://192.168.1.2:8080/v1',
    'http://[::1]:8080/v1', 'http://api.example.com/v1', 'https://api.example.com:8443/v1',
])
def test_admin_endpoint_normalization_allows_http_and_custom_ports(url):
    assert normalize_provider_location(url + '/chat/completions').base_url == url


def test_configured_origin_cannot_send_credentials_to_other_scheme_port_or_host():
    async def run():
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(302, headers={'location': 'http://localhost:8081/v1/models'})
        client = FixedHostHttpClient(allowed_hosts={'localhost'}, configured_origin='http://localhost:8080/v1',
                                     transport=httpx.MockTransport(handler), resolver=lambda _, port: [
                                         (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port)),
                                     ])
        try:
            for url in ('https://localhost:8080/v1/models', 'http://localhost:8081/v1/models',
                        'http://other.example/v1/models', 'http://user:pass@localhost:8080/v1/models'):
                with pytest.raises(HttpSecurityError):
                    await client.get(url, headers={'Authorization': 'Bearer fixture'})
            assert not seen
            with pytest.raises(HttpSecurityError):
                await client.get('http://localhost:8080/v1/models', headers={'Authorization': 'Bearer fixture'})
            assert len(seen) == 1
        finally:
            await client.aclose()
    asyncio.run(run())
