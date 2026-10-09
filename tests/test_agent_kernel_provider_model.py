from __future__ import annotations

import json
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import patch

from app.agent.kernel.model import ModelEventType, ModelRequest
from app.agent.kernel.provider_model import (
    ModelProviderError,
    OpenAICompatibleModelAdapter,
    ProviderSettings,
    _network_idle_timeout_seconds,
    _stream_deadline_seconds,
    iter_protocol_model_events,
)
from app.agent.kernel.state import CancellationToken
from app.clients.openai_compatible import ProviderStreamError, iter_provider_text_deltas


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
    async def test_responses_empty_completion_fields_do_not_erase_streamed_arguments(self):
        arguments = '{"profile_ref":"configured:jellyfin","item_ref":"ref_test"}'
        for final_type in ("response.function_call_arguments.done", "response.output_item.done"):
            for final_arguments in (None, "", "   "):
                with self.subTest(final_type=final_type, final_arguments=final_arguments):
                    final = {"type": final_type, "output_index": 0, "item_id": "item1", "arguments": final_arguments}
                    if final_type.endswith("output_item.done"):
                        final["item"] = {"type": "function_call", "id": "item1", "call_id": "call1", "name": "inspect", "arguments": final_arguments}
                    frames = [
                        {"type": "response.output_item.added", "output_index": 0, "item": {"type": "function_call", "id": "item1", "call_id": "call1", "name": "inspect", "arguments": ""}},
                        {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "item1", "delta": arguments},
                        final,
                        {"type": "response.completed", "response": {"status": "completed"}},
                    ]
                    events = await collect(iter_protocol_model_events(chunks(frames, split=11), protocol="responses"))
                    calls = [event.tool_call for event in events if event.type is ModelEventType.TOOL_CALL_COMPLETED]
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(dict(calls[0].arguments), json.loads(arguments))

    async def test_responses_completion_keeps_explicit_empty_object_but_rejects_partial_json(self):
        for delta, final, expected in (("{invalid", "", None), ('{"old":1}', "{}", {})):
            frames = [
                {"type": "response.output_item.added", "output_index": 0, "item": {"type": "function_call", "id": "i", "call_id": "c", "name": "inspect", "arguments": ""}},
                {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "i", "delta": delta},
                {"type": "response.output_item.done", "output_index": 0, "item": {"type": "function_call", "id": "i", "call_id": "c", "name": "inspect", "arguments": final}},
                {"type": "response.completed", "response": {"status": "completed"}},
            ]
            stream = iter_protocol_model_events(chunks(frames), protocol="responses")
            if expected is None:
                with self.assertRaises(ModelProviderError):
                    await collect(stream)
            else:
                events = await collect(stream)
                calls = [e.tool_call for e in events if e.type is ModelEventType.TOOL_CALL_COMPLETED]
                self.assertEqual(dict(calls[0].arguments), expected)

    async def test_per_request_model_does_not_mutate_shared_provider_settings(self):
        seen = []
        class Response:
            status_code = 200
            headers = {"content-type": "text/event-stream"}
            async def aiter_bytes(self):
                async for chunk in chunks([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}, "[DONE]"]):
                    yield chunk
        class Client:
            @asynccontextmanager
            async def stream_post_json(self, url, **kwargs):
                seen.append((url, kwargs["json"]["model"]))
                yield Response()
            async def aclose(self):
                pass
        settings = ProviderSettings(api_url="https://api.example.com/v1", model="global", protocol="chat_completions")
        adapter = OpenAICompatibleModelAdapter(settings, client_factory=lambda **_: Client())
        for model in ("chat-selected", ""):
            await collect(adapter.stream(ModelRequest(system_prompt="", messages=(), tools=(), model=model), cancellation=CancellationToken()))
        self.assertEqual([x[1] for x in seen], ["chat-selected", "global"])
        self.assertEqual(seen[0][0], seen[1][0])
        self.assertEqual(settings.model, "global")

    async def test_responses_failure_before_output_keeps_original_bounded_retry(self):
        class Response:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def __init__(self, frames):
                self.frames = frames

            async def aiter_bytes(self):
                async for chunk in chunks(self.frames):
                    yield chunk

        class Client:
            calls = 0

            @asynccontextmanager
            async def stream_post_json(self, *_args, **_kwargs):
                self.calls += 1
                yield Response([{"type": failure_type}] if self.calls == 1 else [
                    {"type": "response.output_text.delta", "delta": "已恢复"},
                    {"type": "response.completed", "response": {"status": "completed"}},
                ])

            async def aclose(self):
                pass

        for failure_type in ("response.failed", "error"):
            with self.subTest(failure_type=failure_type), patch(
                "app.agent.kernel.provider_model._MODEL_RETRY_DELAY_SECONDS", 0
            ):
                client = Client()
                adapter = OpenAICompatibleModelAdapter(
                    ProviderSettings(api_url="https://api.example.com/v1", model="sample", protocol="responses"),
                    client_factory=lambda **_: client,
                )
                stream = adapter.stream(
                    ModelRequest(system_prompt="检查状态", messages=(), tools=()), cancellation=CancellationToken()
                )
                if failure_type == "error":
                    with self.assertRaisesRegex(ModelProviderError, "流式错误"):
                        await collect(stream)
                    self.assertEqual(client.calls, 1)
                    continue
                events = await collect(stream)
                self.assertEqual(client.calls, 2)
                self.assertEqual("".join(event.text for event in events), "已恢复")
                self.assertEqual(events[-1].type, ModelEventType.FINISH)

    async def test_chat_length_finish_never_emits_a_success_or_executable_call(self):
        observed = []
        with self.assertRaises(ModelProviderError):
            async for event in iter_protocol_model_events(chunks([
                {"choices": [{"delta": {"content": "部分结果", "tool_calls": [{
                    "index": 0, "id": "partial-call", "function": {"name": "write.test", "arguments": "{}"},
                }]}, "finish_reason": "length"}]}, "[DONE]",
            ]), protocol="chat_completions"):
                observed.append(event)
        self.assertNotIn(ModelEventType.FINISH, [event.type for event in observed])
        self.assertNotIn(ModelEventType.TOOL_CALL_COMPLETED, [event.type for event in observed])

    async def test_anthropic_max_tokens_is_incomplete_even_with_message_stop(self):
        with self.assertRaises(ModelProviderError):
            await collect(iter_protocol_model_events(chunks([
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "部分结果"}},
                {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}},
                {"type": "message_stop"},
            ]), protocol="anthropic_messages"))

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

    def test_provider_settings_accepts_2_to_120_second_timeout_range(self) -> None:
        for timeout_seconds in (2, 12, 30, 120):
            with self.subTest(timeout_seconds=timeout_seconds):
                settings = ProviderSettings(
                    api_url="https://api.example.com/v1",
                    model="sample",
                    timeout_seconds=timeout_seconds,
                )
                self.assertEqual(settings.timeout_seconds, timeout_seconds)
        for timeout_seconds in (1, 121):
            with self.subTest(timeout_seconds=timeout_seconds), self.assertRaises(
                ValueError
            ):
                ProviderSettings(
                    api_url="https://api.example.com/v1",
                    model="sample",
                    timeout_seconds=timeout_seconds,
                )

    def test_stream_deadline_is_wider_than_network_idle_timeout_but_bounded(
        self,
    ) -> None:
        self.assertEqual(_stream_deadline_seconds(2), 60)
        self.assertEqual(_stream_deadline_seconds(30), 120)
        self.assertEqual(_stream_deadline_seconds(120), 300)
        self.assertEqual(
            _stream_deadline_seconds(_network_idle_timeout_seconds(2)), 120
        )
        self.assertEqual(
            _stream_deadline_seconds(_network_idle_timeout_seconds(120)), 300
        )

    async def test_chat_stream_rejects_eof_after_stop_without_done(self) -> None:
        truncated = [
            {
                "choices": [
                    {"delta": {"content": "partial"}, "finish_reason": "stop"}
                ]
            }
        ]

        with self.assertRaises(ModelProviderError) as caught:
            await collect(
                iter_protocol_model_events(
                    chunks(truncated), protocol="chat_completions"
                )
            )

        self.assertIn("完成事件前中断", str(caught.exception))

    async def test_text_stream_rejects_eof_after_stop_without_done(self) -> None:
        truncated = [
            {
                "choices": [
                    {"delta": {"content": "partial"}, "finish_reason": "stop"}
                ]
            }
        ]

        with self.assertRaises(ProviderStreamError):
            _ = [
                delta
                async for delta in iter_provider_text_deltas(
                    chunks(truncated), protocol="chat_completions"
                )
            ]

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


class ToolArgumentDecoderParityTests(unittest.IsolatedAsyncioTestCase):
    async def stream_tool_arguments(self, arguments):
        if isinstance(arguments, dict):
            protocol = "anthropic_messages"
            events = [
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "call_args",
                        "name": "inspect",
                        "input": arguments,
                    },
                },
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
                {"type": "message_stop"},
            ]
        else:
            protocol = "chat_completions"
            events = [
                {
                    "choices": [{
                        "delta": {"tool_calls": [{
                            "index": 0,
                            "id": "call_args",
                            "function": {"name": "inspect", "arguments": arguments},
                        }]},
                        "finish_reason": "tool_calls",
                    }]
                },
                "[DONE]",
            ]
        return await collect(iter_protocol_model_events(chunks(events), protocol=protocol))

    async def test_streaming_tool_arguments_share_json_and_size_contract(self):
        valid_dict = {"value": "x" * (32_768 - 12)}
        valid_json = '{"value":"' + "x" * (32_768 - 12) + '"}'
        for arguments, expected in (
            ({"limit": 5}, {"limit": 5}),
            ('{"limit":5}', {"limit": 5}),
            (None, {}),
            ("", {}),
            (valid_dict, valid_dict),
            (valid_json, valid_dict),
        ):
            with self.subTest(argument_type=type(arguments).__name__, size=len(str(arguments))):
                events = await self.stream_tool_arguments(arguments)
                call = next(event.tool_call for event in events if event.tool_call)
                self.assertEqual(call.arguments, expected)

        invalid = (
            ("{broken", "模型工具参数不是有效 JSON"),
            ("[]", "模型工具参数必须是对象"),
            ('{"value":"' + "x" * (32_768 - 11) + '"}', "模型工具参数过大"),
            ({"value": "x" * (32_768 - 11)}, "模型工具参数过大"),
        )
        for arguments, message in invalid:
            with self.subTest(
                message=message,
                argument_type=type(arguments).__name__,
            ), self.assertRaisesRegex(ModelProviderError, message):
                await self.stream_tool_arguments(arguments)


class CompleteTurnContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_done_without_finish_reason_rejects_text_and_tool_execution(self):
        seen = []
        with self.assertRaises(ModelProviderError):
            async for item in iter_protocol_model_events(chunks([
                {"choices": [{"delta": {
                    "content": "### 第1项：`sample-",
                    "tool_calls": [{"index": 0, "id": "write-1", "function": {
                        "name": "cloud.rename", "arguments": "{}",
                    }}],
                }, "finish_reason": None}]},
                "[DONE]",
            ]), protocol="chat_completions"):
                seen.append(item)
        self.assertTrue(any(item.type == ModelEventType.TEXT_DELTA for item in seen))
        self.assertFalse(any(item.type in {
            ModelEventType.FINISH, ModelEventType.TOOL_CALL_COMPLETED,
        } for item in seen))

    async def test_protocol_completion_markers_cannot_mask_missing_or_failed_reason(self):
        cases = [
            ("chat_completions", [{"choices": [{"delta": {"content": "partial"}, "finish_reason": reason}]}, "[DONE]"])
            for reason in ("length", "content_filter", "unknown", "")
        ] + [
            ("responses", [{"type": "response.output_text.delta", "delta": "partial"},
                           {"type": "response.completed", "response": {"status": reason}}])
            for reason in ("incomplete", "failed", "", None)
        ] + [
            ("anthropic_messages", [{"type": "message_delta", "delta": {"stop_reason": reason}},
                                    {"type": "message_stop"}])
            for reason in ("max_tokens", "pause_turn", "refusal", "", None)
        ] + [
            (protocol, ["[DONE]"]) for protocol in ("responses", "anthropic_messages")
        ]
        for protocol, events in cases:
            with self.subTest(protocol=protocol, events=events), self.assertRaises(ModelProviderError):
                await collect(iter_protocol_model_events(chunks(events), protocol=protocol))

    async def test_json_fallback_validates_finish_before_exposing_calls(self):
        class Response:
            status_code = 200
            headers = {"content-type": "application/json"}

            async def aiter_bytes(self):
                yield json.dumps(envelope).encode()

        class Client:
            @asynccontextmanager
            async def stream_post_json(self, *_args, **_kwargs):
                yield Response()

            async def aclose(self):
                pass

        for reason in ("length", None, "tool_calls"):
            envelope = {"choices": [{"finish_reason": reason, "message": {
                "role": "assistant", "content": "partial", "tool_calls": [{
                    "id": "call-1", "type": "function", "function": {
                        "name": "cloud.rename", "arguments": "{}",
                    },
                }],
            }}]}
            adapter = OpenAICompatibleModelAdapter(
                ProviderSettings(api_url="https://api.example.com/v1", model="test", protocol="chat_completions"),
                client_factory=lambda **_: Client(),
            )
            seen = []
            with self.subTest(reason=reason):
                if reason == "tool_calls":
                    seen = await collect(adapter.stream(ModelRequest(system_prompt="test", messages=(), tools=()), cancellation=CancellationToken()))
                    self.assertEqual(seen[-1].finish_reason, "tool_calls")
                else:
                    with self.assertRaises(ModelProviderError):
                        async for event in adapter.stream(ModelRequest(system_prompt="test", messages=(), tools=()), cancellation=CancellationToken()):
                            seen.append(event)
                    self.assertEqual(seen, [])


class NativeAnswerCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def run_adapter(self, protocol, *, streamed=True, complete=True, tool=False, text='核对完成，未执行写操作。'):
        from app.agent.kernel.model import ModelMessage

        class Client:
            calls = 0
            request_body = None

            @asynccontextmanager
            async def stream_post_json(self, _url, *, json, **_kwargs):
                self.calls += 1
                self.request_body = json
                if protocol == 'chat_completions':
                    content = {'content': text}
                    if tool:
                        content['tool_calls'] = [{'index': 0, 'id': 'c1', 'type': 'function', 'function': {'name': 'read_status', 'arguments': '{}'}}]
                    reason = ('tool_calls' if tool else 'stop') if complete else 'length'
                    envelope = {'choices': [{'message': content, 'finish_reason': reason}]}
                    frames = [{'choices': [{'delta': content, 'finish_reason': reason}]}, '[DONE]']
                elif protocol == 'responses':
                    item = ({'type': 'function_call', 'id': 'item1', 'call_id': 'c1', 'name': 'read_status', 'arguments': '{}'} if tool else
                            {'type': 'message', 'content': [{'type': 'output_text', 'text': text}]})
                    envelope = {'status': 'completed' if complete else 'incomplete', 'output': [item]}
                    frames = ([{'type': 'response.output_item.done', 'item': item}] if tool else
                              [{'type': 'response.output_text.delta', 'delta': text}])
                    frames.append({'type': 'response.completed' if complete else 'response.incomplete', 'response': envelope})
                else:
                    block = ({'type': 'tool_use', 'id': 'c1', 'name': 'read_status', 'input': {}} if tool else {'type': 'text', 'text': text})
                    reason = ('tool_use' if tool else 'end_turn') if complete else 'max_tokens'
                    envelope = {'stop_reason': reason, 'content': [block]}
                    frames = [{'type': 'content_block_start', 'index': 0, 'content_block': block},
                              {'type': 'message_delta', 'delta': {'stop_reason': reason}},
                              {'type': 'message_stop'}]

                class Response:
                    status_code = 200
                    headers = {'content-type': 'text/event-stream' if streamed else 'application/json'}

                    async def aiter_bytes(self):
                        if streamed:
                            async for chunk in chunks(frames, split=3):
                                yield chunk
                        else:
                            yield __import__('json').dumps(envelope).encode()

                yield Response()

            async def aclose(self):
                pass

        client = Client()
        adapter = OpenAICompatibleModelAdapter(
            ProviderSettings(api_url='https://api.example.com/v1', model='sample', protocol=protocol),
            client_factory=lambda **_kwargs: client,
        )
        events = []
        error = None
        try:
            async for event in adapter.stream(ModelRequest(
                system_prompt='文件管理', messages=(ModelMessage(role='user', content='检查状态'),), tools=(),
            ), cancellation=CancellationToken()):
                events.append(event)
        except ModelProviderError as exc:
            error = exc
        self.assertEqual(client.calls, 1, '模型输出后不自动重放原请求')
        self.assertNotIn('mf_answer_end_', json.dumps(client.request_body))
        return events, error

    async def test_normal_native_completion_needs_no_extra_model_text_marker(self):
        for protocol in ('chat_completions', 'responses', 'anthropic_messages'):
            for streamed in (True, False):
                with self.subTest(protocol=protocol, streamed=streamed):
                    text = '工作区当前没有需要处理的下一步。'
                    events, error = await self.run_adapter(protocol, streamed=streamed, text=text)
                    self.assertIsNone(error)
                    self.assertEqual(events[-1].type, ModelEventType.FINISH)
                    self.assertEqual(''.join(e.text for e in events), text)

    async def test_native_truncation_is_still_incomplete_even_with_complete_looking_text(self):
        from app.agent.kernel.provider_model import IncompleteModelAnswer
        for protocol in ('chat_completions', 'responses', 'anthropic_messages'):
            for streamed in (True, False):
                with self.subTest(protocol=protocol, streamed=streamed):
                    events, error = await self.run_adapter(protocol, streamed=streamed, complete=False)
                    self.assertIsInstance(error, IncompleteModelAnswer)
                    self.assertNotIn(ModelEventType.FINISH, [e.type for e in events])

    async def test_complete_native_tool_rounds_preserve_tool_calls(self):
        for protocol in ('chat_completions', 'responses', 'anthropic_messages'):
            for streamed in (True, False):
                with self.subTest(protocol=protocol, streamed=streamed):
                    events, error = await self.run_adapter(protocol, streamed=streamed, tool=True, text='')
                    self.assertIsNone(error)
                    self.assertEqual(events[-1].type, ModelEventType.FINISH)
                    self.assertEqual(len([e for e in events if e.tool_call]), 1)

    async def test_missing_native_terminal_is_incomplete(self):
        from app.agent.kernel.provider_model import IncompleteModelAnswer
        frames = {
            'chat_completions': [{'choices': [{'delta': {'content': '正文已输出'}, 'finish_reason': 'stop'}]}],
            'responses': [{'type': 'response.output_text.delta', 'delta': '正文已输出'}],
            'anthropic_messages': [{'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': '正文已输出'}}],
        }
        for protocol, events in frames.items():
            with self.subTest(protocol=protocol), self.assertRaises(IncompleteModelAnswer):
                await collect(iter_protocol_model_events(chunks(events, split=3), protocol=protocol))
