"""Tavily 密钥探测只读用量，不保存、不搜索、不回显凭据。"""
import asyncio
import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.routes import tools_api
from tests.support import authenticated_session, preserve_web_auth_config


@pytest.fixture
def probe():
    client = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(status_code=200, text='{"key": {"usage": 1}, "account": {"plan_usage": 2}}')),
        aclose=AsyncMock(),
    )
    values = {"TAVILY_API_KEY": "saved-secret", "TAVILY_TIMEOUT_SECONDS": "10"}
    with (
        patch.object(tools_api.config, "get", side_effect=preserve_web_auth_config(lambda key, default="": values.get(key, default))),
        patch.object(tools_api.config, "set_and_save") as save,
        patch.object(tools_api.agent_rate_limiter, "allow", return_value=True) as limit,
        patch.object(tools_api, "FixedHostHttpClient", return_value=client) as factory,
    ):
        yield client, save, limit, factory
        save.assert_not_called()


def call(data, *, logged_in=True):
    request = cast(Request, SimpleNamespace(session=(authenticated_session(csrf_token="test-tavily") if logged_in else {})))
    response = asyncio.run(tools_api.tavily_test(request, data))
    return response.status_code, json.loads(bytes(response.body))


@pytest.mark.parametrize("data,key", [({}, "saved-secret"), ({"api_key": "********"}, "saved-secret"), ({"api_key": " draft-secret ", "timeout_seconds": "12"}, "draft-secret")])
def test_probe_uses_saved_or_unsaved_key_without_persisting(probe, data, key):
    client, _, _, factory = probe
    status, payload = call(data)
    assert status == 200 and payload["ok"] is True
    assert payload["elapsed_ms"] >= 1
    assert key not in json.dumps(payload)
    client.get.assert_awaited_once_with("https://api.tavily.com/usage", headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    client.aclose.assert_awaited_once()
    assert factory.call_args.kwargs["max_redirects"] == 0
    assert factory.call_args.kwargs["timeout_seconds"] == int(data.get("timeout_seconds", 10))


@pytest.mark.parametrize("data", [
    {"api_key": ""}, {"api_key": None}, {"api_key": {}}, {"api_key": "x\ninjected"},
    {"api_key": "x" * 513}, {"api_url": "http://127.0.0.1/"},
] + [{"timeout_seconds": value} for value in (True, None, 1, 31, 2.5, "invalid")])
def test_invalid_draft_including_explicit_clear_never_uses_saved_key(probe, data):
    client, _, _, factory = probe
    assert call(data)[0] == 400
    factory.assert_not_called()
    client.get.assert_not_awaited()


@pytest.mark.parametrize("code,expected", [(401, 400), (403, 400), (429, 429), (302, 502), (500, 502)])
def test_provider_errors_are_safe_and_close_client(probe, code, expected):
    client, *_ = probe
    client.get.return_value = SimpleNamespace(status_code=code, text="saved-secret upstream detail")
    status, payload = call({})
    assert status == expected
    assert "saved-secret" not in json.dumps(payload)
    client.aclose.assert_awaited_once()


@pytest.mark.parametrize("body", ["not-json", "null", "[]", "{}", '{"key": [], "account": {}}'])
def test_malformed_success_is_not_reported_valid(probe, body):
    client, *_ = probe
    client.get.return_value.text = body
    assert call({})[0] == 502
    client.aclose.assert_awaited_once()


@pytest.mark.parametrize("error,status", [(httpx.ReadTimeout("saved-secret"), 504), (httpx.ConnectError("saved-secret"), 502), (tools_api.HttpError("saved-secret"), 502)])
def test_transport_errors_do_not_leak_keys(probe, error, status):
    client, *_ = probe
    client.get.side_effect = error
    actual, payload = call({})
    assert actual == status
    assert "saved-secret" not in json.dumps(payload)
    client.aclose.assert_awaited_once()


def test_authentication_and_rate_limit_before_network(probe):
    client, _, limit, factory = probe
    with pytest.raises(HTTPException) as exc:
        call({}, logged_in=False)
    assert exc.value.status_code == 401
    limit.return_value = False
    assert call({})[0] == 429
    factory.assert_not_called()
    client.get.assert_not_awaited()


def test_total_timeout_cancels_request_and_closes_client(probe):
    client, *_ = probe

    async def pending(*args, **kwargs):
        await asyncio.sleep(5)

    client.get.side_effect = pending
    assert call({"timeout_seconds": 2})[0] == 504
    client.aclose.assert_awaited_once()
