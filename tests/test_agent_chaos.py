"""第 23 批：Provider、SSE 与 SQLite 故障注入回归。"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from app import database as db
from app.agent.llm_router import _request_structured_json
from app.agent.metrics import agent_metrics
from app.clients.openai_compatible import ProviderStreamError, iter_provider_text_deltas
from app.indexers.http import IndexerHttpResponse


_VALUES = {
    "AGENT_LLM_API_URL": "https://ai.invalid/v1/chat/completions",
    "AGENT_LLM_MODEL": "chaos-model",
    "AGENT_LLM_PROTOCOL": "chat_completions",
    "AGENT_LLM_TIMEOUT_SECONDS": "1",
}


def _settings(key: str, default: str = "") -> str:
    return _VALUES.get(key, default)



def _provider_outcomes() -> dict[str, int]:
    providers = agent_metrics.snapshot()["llm"]["providers"]
    return providers[0]["outcomes"] if providers else {}


def test_repeated_429_and_5xx_have_distinct_metric_outcomes() -> None:
    class StatusClient:
        status_code = 429

        def __init__(self, **kwargs):
            pass

        async def post_json(self, url, *, json, headers, max_redirects):
            return IndexerHttpResponse(
                url=url,
                status_code=self.status_code,
                headers={"retry-after": "0"},
                body=b"{}",
            )

        async def aclose(self):
            return None

    with patch("app.agent.llm_router.get", side_effect=_settings):
        agent_metrics.reset()
        StatusClient.status_code = 429
        assert asyncio.run(_request_structured_json(
            system_prompt="probe", user_content="probe", schema_name="probe",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            max_tokens=16, client_factory=StatusClient, max_content_length=1024,
        )) is None
        assert _provider_outcomes()["rate_limited"] == 1

        agent_metrics.reset()
        StatusClient.status_code = 503
        assert asyncio.run(_request_structured_json(
            system_prompt="probe", user_content="probe", schema_name="probe",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            max_tokens=16, client_factory=StatusClient, max_content_length=1024,
        )) is None
        assert _provider_outcomes()["upstream_5xx"] == 1


def test_slow_provider_is_bounded_and_reported_as_timeout() -> None:
    closed = False

    class SlowClient:
        def __init__(self, **kwargs):
            pass

        async def post_json(self, url, *, json, headers, max_redirects):
            await asyncio.sleep(0.05)
            raise AssertionError("超时信封应先取消请求")

        async def aclose(self):
            nonlocal closed
            closed = True

    agent_metrics.reset()
    with patch("app.agent.llm_router.get", side_effect=_settings), patch(
        "app.agent.llm_router._timeout", return_value=0.01
    ):
        assert asyncio.run(_request_structured_json(
            system_prompt="probe", user_content="probe", schema_name="probe",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            max_tokens=16, client_factory=SlowClient, max_content_length=1024,
        )) is None
    assert closed is True
    assert _provider_outcomes()["timeout"] == 1


def test_invalid_provider_json_is_distinct_from_other_failures() -> None:
    class InvalidJsonClient:
        def __init__(self, **kwargs):
            pass

        async def post_json(self, url, *, json, headers, max_redirects):
            return IndexerHttpResponse(
                url=url,
                status_code=200,
                headers={"content-type": "application/json"},
                body=b"not-json",
            )

        async def aclose(self):
            return None

    agent_metrics.reset()
    with patch("app.agent.llm_router.get", side_effect=_settings):
        assert asyncio.run(_request_structured_json(
            system_prompt="probe", user_content="probe", schema_name="probe",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            max_tokens=16, client_factory=InvalidJsonClient, max_content_length=1024,
        )) is None
    assert _provider_outcomes()["invalid_json"] == 1


def test_malformed_sse_and_midstream_disconnect_are_rejected() -> None:
    async def malformed_chunks():
        yield b"data: <html>gateway failure</html>\n\n"

    async def disconnected_chunks():
        payload = {"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]}
        yield f"data: {json.dumps(payload)}\n\n".encode()

    async def consume(chunks) -> list[str]:
        return [
            delta
            async for delta in iter_provider_text_deltas(
                chunks, protocol="chat_completions"
            )
        ]

    for chunks in (malformed_chunks(), disconnected_chunks()):
        try:
            asyncio.run(consume(chunks))
        except ProviderStreamError:
            pass
        else:
            raise AssertionError("损坏或中断的 SSE 必须失败关闭")


def test_sqlite_locked_fault_is_visible_without_exposing_sql() -> None:
    db._reset_sqlite_contention_metrics_for_tests()
    db._observe_sqlite_contention(
        db.sqlite3.OperationalError("database table is locked: secret_table"),
        phase="commit",
        elapsed_ms=9,
    )
    snapshot = agent_metrics.snapshot()
    assert snapshot["sqlite_contention"]["locked"] == 1
    assert snapshot["sqlite_contention"]["commit"] == 1
    assert "secret_table" not in agent_metrics.prometheus()
    db._reset_sqlite_contention_metrics_for_tests()
