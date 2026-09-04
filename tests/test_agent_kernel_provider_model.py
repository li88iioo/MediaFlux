from __future__ import annotations

import json
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import patch

from app.agent.kernel.model import ModelEventType, ModelRequest
from app.agent.kernel.provider_model import (
    OpenAICompatibleModelAdapter,
    ProviderSettings,
    _network_idle_timeout_seconds,
    _stream_deadline_seconds,
    iter_protocol_model_events,
)
from app.agent.kernel.state import CancellationToken


async def chunks(events: list[dict | str], *, split: int = 0) -> AsyncIterator[bytes]:
    payload = "".join(
        f"data: {item if isinstance(item, str) else json.dumps(item)}\n\n"
        for item in events
    ).encode()
    if split:
        for index in range(0, len(payload), split):
            yield payload[index : index + split]
    else:
        yield payload


async def collect(stream):
    return [item async for item in stream]


class ProviderModelStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_retries_one_transient_http_failure_before_output(
        self,
    ) -> None:
        class Response:
            def __init__(self, status_code, body=b""):
                self.status_code = status_code
                self.headers = {"content-type": "text/event-stream"}
                self.body = body

            async def aiter_bytes(self):
                yield self.body

        class Client:
            def __init__(self):
                self.calls = 0
                self.closed = False

            @asynccontextmanager
            async def stream_post_json(self, *args, **kwargs):
                del args, kwargs
                self.calls += 1
                if self.calls == 1:
                    yield Response(503)
                    return
                yield Response(
                    200,
                    (
                        b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                        b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
                    ),
                )

            async def aclose(self):
                self.closed = True

        client = Client()
        adapter = OpenAICompatibleModelAdapter(
            ProviderSettings(
                api_url="https://api.example.com/v1",
                model="test-model",
                protocol="responses",
            ),
            client_factory=lambda **_kwargs: client,
        )

        with patch(
            "app.agent.kernel.provider_model._MODEL_RETRY_DELAY_SECONDS", 0
        ):
            events = await collect(
                adapter.stream(
                    ModelRequest(system_prompt="test", messages=(), tools=()),
                    cancellation=CancellationToken(),
                )
            )

        self.assertEqual(client.calls, 2)
        self.assertTrue(client.closed)
        self.assertEqual(events[0].text, "ok")
        self.assertEqual(events[-1].type, ModelEventType.FINISH)

    async def test_adapter_retries_incomplete_stream_before_any_output(self) -> None:
        class Response:
            def __init__(self, body: bytes):
                self.status_code = 200
                self.headers = {"content-type": "text/event-stream"}
                self.body = body

            async def aiter_bytes(self):
                if self.body:
                    yield self.body

        class Client:
            def __init__(self):
                self.calls = 0

            @asynccontextmanager
            async def stream_post_json(self, *args, **kwargs):
                del args, kwargs
                self.calls += 1
                if self.calls == 1:
                    yield Response(b"")
                    return
                yield Response(
                    b'data: {"type":"response.output_text.delta","delta":"recovered"}\n\n'
                    b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
                )

            async def aclose(self):
                return None

        client = Client()
        adapter = OpenAICompatibleModelAdapter(
            ProviderSettings(
                api_url="https://api.example.com/v1",
                model="test-model",
                protocol="responses",
            ),
            client_factory=lambda **_kwargs: client,
        )

        with patch(
            "app.agent.kernel.provider_model._MODEL_RETRY_DELAY_SECONDS", 0
        ):
            events = await collect(
                adapter.stream(
                    ModelRequest(system_prompt="test", messages=(), tools=()),
                    cancellation=CancellationToken(),
                )
            )

        self.assertEqual(client.calls, 2)
        self.assertEqual(events[0].text, "recovered")

    def test_network_idle_timeout_has_model_safe_floor_and_upper_bound(self) -> None:
        self.assertEqual(_network_idle_timeout_seconds(2), 30)
        self.assertEqual(_network_idle_timeout_seconds(30), 30)
        self.assertEqual(_network_idle_timeout_seconds(90), 90)
        self.assertEqual(_network_idle_timeout_seconds(240), 120)

    def test_stream_deadline_is_wider_than_network_idle_timeout_but_bounded(
        self,
    ) -> None:
        self.assertEqual(_stream_deadline_seconds(2), 60)
        self.assertEqual(_stream_deadline_seconds(30), 120)
        self.assertEqual(_stream_deadline_seconds(120), 300)

    async def test_chat_completions_stream_assembles_tool_call(self) -> None:
        events = await collect(
            iter_protocol_model_events(
                chunks(
                    [
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": "call-1",
                                                "function": {
                                                    "name": "cloud.",
                                                    "arguments": '{"dir',
                                                },
                                            }
                                        ]
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        },
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "function": {
                                                    "name": "list",
                                                    "arguments": '":"root"}',
                                                },
                                            }
                                        ]
                                    },
                                    "finish_reason": "tool_calls",
                                }
                            ],
                        },
                        "[DONE]",
                    ],
                    split=7,
                ),
                protocol="chat_completions",
            )
        )
        call = next(
            item.tool_call
            for item in events
            if item.type is ModelEventType.TOOL_CALL_COMPLETED
        )
        self.assertEqual(call.name, "cloud.list")
        self.assertEqual(call.arguments, {"dir": "root"})
        self.assertEqual(events[-1].finish_reason, "tool_calls")

    async def test_responses_stream_emits_text_and_tool(self) -> None:
        events = await collect(
            iter_protocol_model_events(
                chunks(
                    [
                        {"type": "response.output_text.delta", "delta": "先检查。"},
                        {
                            "type": "response.output_item.added",
                            "output_index": 1,
                            "item": {
                                "type": "function_call",
                                "id": "item-1",
                                "call_id": "call-1",
                                "name": "library.search",
                                "arguments": "",
                            },
                        },
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": "item-1",
                            "output_index": 1,
                            "delta": '{"query":"光阴之外"}',
                        },
                        {
                            "type": "response.function_call_arguments.done",
                            "item_id": "item-1",
                            "output_index": 1,
                            "arguments": '{"query":"光阴之外"}',
                        },
                        {
                            "type": "response.completed",
                            "response": {"status": "completed"},
                        },
                    ],
                    split=11,
                ),
                protocol="responses",
            )
        )
        self.assertEqual(events[0].text, "先检查。")
        call = next(
            item.tool_call
            for item in events
            if item.type is ModelEventType.TOOL_CALL_COMPLETED
        )
        self.assertEqual(call.name, "library.search")
        self.assertEqual(call.arguments["query"], "光阴之外")
        self.assertEqual(events[-1].type, ModelEventType.FINISH)

    async def test_anthropic_stream_assembles_tool_and_filters_thinking(self) -> None:
        events = await collect(
            iter_protocol_model_events(
                chunks(
                    [
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": "<think>secret"},
                        },
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {
                                "type": "text_delta",
                                "text": " plan</think>开始检查",
                            },
                        },
                        {
                            "type": "content_block_start",
                            "index": 1,
                            "content_block": {
                                "type": "tool_use",
                                "id": "tool-1",
                                "name": "download.list",
                                "input": {},
                            },
                        },
                        {
                            "type": "content_block_delta",
                            "index": 1,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": '{"limit":10}',
                            },
                        },
                        {"type": "content_block_stop", "index": 1},
                        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
                        {"type": "message_stop"},
                    ],
                    split=5,
                ),
                protocol="anthropic_messages",
            )
        )
        text = "".join(
            item.text for item in events if item.type is ModelEventType.TEXT_DELTA
        )
        self.assertEqual(text, "开始检查")
        call = next(
            item.tool_call
            for item in events
            if item.type is ModelEventType.TOOL_CALL_COMPLETED
        )
        self.assertEqual(call.name, "download.list")
        self.assertEqual(call.arguments, {"limit": 10})
        self.assertEqual(events[-1].finish_reason, "tool_use")
