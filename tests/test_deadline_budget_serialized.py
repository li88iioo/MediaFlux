"""Final serialized native bodies must remain inside explicit model caps."""
import asyncio
import json

import pytest

from app.agent.kernel.model import ModelMessage, ModelRequest
from app.agent.kernel.provider_model import (
    ModelProviderError,
    OpenAICompatibleModelAdapter,
    ProviderSettings,
)
from app.agent.kernel.state_types import CancellationToken
from tests import test_deadline_budget as support
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414
from tests.test_agent_generation_profiles import BASE, Wire
from tests.test_deadline_budget import publish, row, turn

harness = support.harness
config_api = support.config_api
local_config = support.local_config


@pytest.mark.parametrize('protocol', ['chat_completions', 'responses', 'anthropic_messages'])
def test_final_native_body_rejected_before_http(agent_llm_enabled, protocol):
    wire = Wire()
    settings = ProviderSettings(api_url=BASE, model='small', protocol=protocol,
        budget_profiles_json=json.dumps([row('small', context=1024, output=127)]))
    adapter = OpenAICompatibleModelAdapter(settings, client_factory=wire.factory)
    request = ModelRequest(system_prompt='offline', messages=(ModelMessage('user', '界' * 1000),),
        tools=(), max_output_tokens=127, model='small')
    async def run():
        with pytest.raises(ModelProviderError, match='上下文预算'):
            [event async for event in adapter.stream(request, cancellation=CancellationToken())]
    asyncio.run(run())
    assert not wire.requests
    assert all(client._client.is_closed for client in wire.clients)


def test_reasoning_reservation_consumed_by_main_session(harness, config_api):
    from app.agent.kernel import bootstrap
    wire, _ = harness
    profile = dict(row(context=65536, output=127), reasoning_reserved_tokens=65000)
    publish(config_api, [profile])
    events = turn(bootstrap.build_agent_kernel_runtime().session)
    assert not wire.requests
    assert any(e.payload.get('code') == 'context_budget_exceeded' for e in events)
