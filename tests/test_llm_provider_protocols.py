"""LLM Provider 四协议公共适配测试。"""
from __future__ import annotations

import json
import unittest
from typing import Any

from app.clients.openai_compatible import (
    ANTHROPIC_VERSION,
    ProviderUsage,
    ProviderStreamError,
    ToolArgumentsDecodeError,
    extract_output_text,
    extract_provider_usage,
    infer_protocol_from_url,
    is_protocol_fallback_error,
    is_reasoning_model,
    iter_provider_text_deltas,
    native_tool_definitions,
    native_tool_initial_history,
    native_tool_request_body,
    normalize_provider_location,
    native_tool_result,
    parse_native_tool_turn,
    protocol_attempts,
    provider_headers,
    resolve_protocol,
    structured_request_body,
    text_stream_request_body,
)


class LLMProviderProtocolTests(unittest.TestCase):
    def test_provider_usage_is_normalized_for_all_protocols(self):
        cases = (
            (
                "responses",
                {
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 3,
                        "total_tokens": 15,
                        # 与真实网关返回一致（2026-11-04 实测 gemini-3.8-flash via Responses）。
                        "input_tokens_details": {"cached_tokens": 2},
                        "output_tokens_details": {"reasoning_tokens": 1},
                    }
                },
            ),
            (
                "chat_completions",
                {
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 3,
                        "total_tokens": 15,
                        "prompt_tokens_details": {"cached_tokens": 2},
                        "completion_tokens_details": {"reasoning_tokens": 1},
                    }
                },
            ),
            (
                "anthropic_messages",
                {
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 3,
                        "cache_read_input_tokens": 2,
                    }
                },
            ),
        )
        for protocol, envelope in cases:
            with self.subTest(protocol=protocol):
                self.assertEqual(
                    extract_provider_usage(envelope, protocol),
                    ProviderUsage(12, 3, 15, 2, 1 if protocol != "anthropic_messages" else 0),
                )

        self.assertIsNone(extract_provider_usage({}, "responses"))
        self.assertIsNone(extract_provider_usage(
            {"usage": {"prompt_tokens": True, "completion_tokens": 2}},
            "chat_completions",
        ))
        self.assertEqual(
            ProviderUsage(10, 2, 12, 1, 0) + ProviderUsage(20, 5, 25, 2, 3),
            ProviderUsage(30, 7, 37, 3, 3),
        )

    def test_native_turn_carries_provider_usage(self):
        turn = parse_native_tool_turn(
            {
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "完成"}}],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 2,
                    "total_tokens": 10,
                },
            },
            "chat_completions",
        )
        self.assertEqual(turn.usage, ProviderUsage(8, 2, 10))
        self.assertEqual(turn.finish_reason, "stop")

    def test_explicit_endpoint_inference_and_location_normalization(self):
        cases = {
            "https://api.example.com/v1/responses": "responses",
            "https://api.example.com/v1/chat/completions": "chat_completions",
            "https://api.example.com/v1/messages": "anthropic_messages",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(infer_protocol_from_url(url), expected)
                self.assertEqual(resolve_protocol("auto", url), expected)
                self.assertEqual(
                    normalize_provider_location(url).base_url,
                    "https://api.example.com/v1",
                )
        self.assertEqual(resolve_protocol("auto", "https://api.example.com/v1"), "auto")
        self.assertEqual(
            resolve_protocol("auto", "https://api.anthropic.com/v1"),
            "anthropic_messages",
        )
        self.assertEqual(
            protocol_attempts("auto"), ("responses", "chat_completions")
        )
        self.assertEqual(protocol_attempts("anthropic_messages"), ("anthropic_messages",))

    def test_reasoning_models_omit_sampling_and_use_completion_budget(self):
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        for model in ("o3-mini", "deepseek-reasoner", "vendor/qwq-32b"):
            with self.subTest(model=model):
                self.assertTrue(is_reasoning_model(model))
                structured = structured_request_body(
                    protocol="chat_completions", model=model,
                    system_prompt="system", user_content="user",
                    schema_name="test", schema=schema, max_tokens=321,
                )
                self.assertNotIn("temperature", structured)
                self.assertNotIn("max_tokens", structured)
                self.assertEqual(structured["max_completion_tokens"], 321)
                native = native_tool_request_body(
                    protocol="chat_completions", model=model,
                    system_prompt="system", history=[], tools=[], max_tokens=222,
                )
                self.assertEqual(native["max_completion_tokens"], 222)
                self.assertNotIn("temperature", native)
                self.assertNotIn("tools", native)
                self.assertNotIn("tool_choice", native)

        regular = text_stream_request_body(
            protocol="chat_completions", model="gpt-4.1-mini",
            system_prompt="system", user_content="user", max_tokens=111,
        )
        self.assertEqual(regular["temperature"], 0)
        self.assertEqual(regular["max_tokens"], 111)

    def test_protocol_fallback_requires_endpoint_incompatibility_signal(self):
        self.assertFalse(is_protocol_fallback_error(404, "", protocol="responses"))
        self.assertTrue(is_protocol_fallback_error(
            404, 'responses api is not supported', protocol="responses"
        ))
        self.assertTrue(is_protocol_fallback_error(
            422, '{"error":"unknown parameter \'input\'"}', protocol="responses"
        ))
        self.assertFalse(is_protocol_fallback_error(
            400, '{"error":"insufficient quota"}', protocol="responses"
        ))
        self.assertFalse(is_protocol_fallback_error(
            422, '{"error":"unknown parameter \'input\'"}',
            protocol="chat_completions",
        ))

    def test_reasoning_markup_is_removed_from_non_stream_turns(self):
        content = extract_output_text(
            {"choices": [{"finish_reason": "stop", "message": {
                "reasoning_content": "private chain",
                "content": "<think>private chain</think>最终答案",
            }}]},
            "chat_completions",
        )
        self.assertEqual(content, "最终答案")
        turn = parse_native_tool_turn(
            {"choices": [{"finish_reason": "stop", "message": {
                "reasoning_content": "private chain",
                "content": "<think>private chain</think>已完成。",
            }}]},
            "chat_completions",
        )
        self.assertEqual(turn.text, "已完成。")
        self.assertEqual(turn.finish_reason, "stop")
        assert turn.assistant_entry is not None
        assert isinstance(turn.assistant_entry, dict)
        self.assertNotIn("reasoning_content", turn.assistant_entry)

    def test_protocol_endpoints_are_distinct(self):
        location = normalize_provider_location("https://api.example.com/v1")
        self.assertEqual(location.endpoint("responses"), "https://api.example.com/v1/responses")
        self.assertEqual(
            location.endpoint("chat_completions"),
            "https://api.example.com/v1/chat/completions",
        )
        self.assertEqual(
            location.endpoint("anthropic_messages"),
            "https://api.example.com/v1/messages",
        )

    def test_anthropic_headers_do_not_send_bearer_token(self):
        headers = provider_headers("anthropic_messages", "secret-key")
        self.assertEqual(headers["x-api-key"], "secret-key")
        self.assertEqual(headers["anthropic-version"], ANTHROPIC_VERSION)
        self.assertNotIn("Authorization", headers)
        with self.assertRaises(ValueError):
            provider_headers("anthropic_messages", "bad\nkey")

    def test_anthropic_structured_output_body_and_text_extraction(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["answer"],
            "properties": {"answer": {"type": "string"}},
        }
        body = structured_request_body(
            protocol="anthropic_messages",
            model="claude-test",
            system_prompt="system",
            user_content="user",
            schema_name="reply",
            schema=schema,
            max_tokens=200,
        )
        self.assertEqual(body["system"], "system")
        self.assertEqual(body["messages"], [{"role": "user", "content": "user"}])
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(body["output_config"]["format"]["schema"], schema)
        self.assertNotIn("response_format", body)
        self.assertEqual(
            extract_output_text(
                {
                    "type": "message",
                    "stop_reason": "end_turn",
                    "content": [
                        {"type": "text", "text": '{"answer":'},
                        {"type": "text", "text": '"ok"}'},
                    ],
                },
                "anthropic_messages",
            ),
            '{"answer":"ok"}',
        )
        with self.assertRaises(ValueError):
            extract_output_text(
                {
                    "type": "message",
                    "stop_reason": "max_tokens",
                    "content": [{"type": "text", "text": "{}"}],
                },
                "anthropic_messages",
            )

    def test_native_tool_definitions_match_provider_shapes(self):
        capabilities = [{
            "name": "mf_workspace_health",
            "description": "读取工作区健康状态",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        }]
        responses = native_tool_definitions("responses", capabilities)
        self.assertEqual(responses[0]["type"], "function")
        self.assertEqual(responses[0]["name"], "mf_workspace_health")
        self.assertTrue(responses[0]["strict"])
        chat = native_tool_definitions("chat_completions", capabilities)
        self.assertEqual(chat[0]["function"]["name"], "mf_workspace_health")
        anthropic = native_tool_definitions("anthropic_messages", capabilities)
        self.assertEqual(anthropic[0]["name"], "mf_workspace_health")
        self.assertEqual(anthropic[0]["input_schema"], capabilities[0]["parameters"])

        optional_capability = [{
            "name": "mf_optional_search",
            "description": "可选查询参数",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "additionalProperties": False,
            },
        }]
        optional_responses = native_tool_definitions(
            "responses", optional_capability
        )
        self.assertNotIn("strict", optional_responses[0])
        optional_chat = native_tool_definitions(
            "chat_completions", optional_capability
        )
        self.assertNotIn("strict", optional_chat[0]["function"])

    def test_native_tool_request_bodies_and_history(self):
        tools = [{"type": "function", "name": "mf_workspace_health"}]
        responses_history = native_tool_initial_history(
            "responses", system_prompt="system", user_content="user"
        )
        responses = native_tool_request_body(
            protocol="responses", model="model", system_prompt="system",
            history=responses_history, tools=tools, max_tokens=400,
        )
        self.assertEqual(responses["input"], responses_history)
        self.assertFalse(responses["parallel_tool_calls"])
        self.assertNotIn("stream", responses)

        chat_history = native_tool_initial_history(
            "chat_completions", system_prompt="system", user_content="user"
        )
        chat = native_tool_request_body(
            protocol="chat_completions", model="model", system_prompt="system",
            history=chat_history, tools=tools, max_tokens=400, stream=True,
        )
        self.assertEqual(chat["messages"], chat_history)
        self.assertTrue(chat["stream"])

        anthropic_history = native_tool_initial_history(
            "anthropic_messages", system_prompt="system", user_content="user"
        )
        anthropic = native_tool_request_body(
            protocol="anthropic_messages", model="model", system_prompt="system",
            history=anthropic_history, tools=tools, max_tokens=400,
        )
        ephemeral = {"type": "ephemeral"}
        self.assertEqual(anthropic["system"], [{"type": "text", "text": "system", "cache_control": ephemeral}])
        self.assertEqual(anthropic["messages"], [
            {"role": "user", "content": [{"type": "text", "text": "user", "cache_control": ephemeral}]},
        ])
        # 断点只写入请求体，不回写调用方持有的历史。
        self.assertEqual(anthropic_history, [{"role": "user", "content": "user"}])
        self.assertNotIn("stream", anthropic)

    def test_anthropic_cache_breakpoints_follow_stable_head_and_latest_input(self):
        ephemeral = {"type": "ephemeral"}
        result = {"type": "tool_result", "tool_use_id": "call-1", "content": "{}"}
        history = [
            {"role": "user", "content": "find"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "call-1", "name": "x", "input": {}}]},
            {"role": "user", "content": [result]},
        ]
        body = native_tool_request_body(
            protocol="anthropic_messages", model="model", system_prompt="HEAD\nTAIL",
            history=history, tools=[], max_tokens=400, stable_system_chars=4,
        )
        self.assertEqual(body["system"], [
            {"type": "text", "text": "HEAD", "cache_control": ephemeral},
            {"type": "text", "text": "\nTAIL"},
        ])
        self.assertEqual(body["messages"][:2], history[:2])
        self.assertEqual(body["messages"][2]["content"], [{**result, "cache_control": ephemeral}])
        self.assertNotIn("cache_control", result)
        for stable_chars, system in ((0, "HEAD\nTAIL"), (99, "HEAD\nTAIL"), (4, "HEAD\n  ")):
            with self.subTest(stable_chars=stable_chars, system=system):
                body = native_tool_request_body(
                    protocol="anthropic_messages", model="model", system_prompt=system,
                    history=history, tools=[], max_tokens=400, stable_system_chars=stable_chars,
                )
                # 无可用拆分或尾部为空白时整段作为一个缓存块，不发送空文本块。
                expected = system if stable_chars == 0 or stable_chars >= len(system) else "HEAD"
                self.assertEqual(body["system"], [{"type": "text", "text": expected, "cache_control": ephemeral}])
        for tail in ({"role": "assistant", "content": "draft"}, {"role": "user", "content": "  "},
                     {"role": "user", "content": [{"type": "image", "source": {}}]}):
            with self.subTest(tail=tail):
                body = native_tool_request_body(
                    protocol="anthropic_messages", model="model", system_prompt="",
                    history=[tail], tools=[], max_tokens=400,
                )
                self.assertEqual(body["messages"], [tail])
                self.assertEqual(body["system"], "")
        for protocol in ("responses", "chat_completions"):
            with self.subTest(protocol=protocol):
                body = native_tool_request_body(
                    protocol=protocol, model="model", system_prompt="HEAD\nTAIL",
                    history=[], tools=[], max_tokens=400, stable_system_chars=4,
                )
                self.assertNotIn("cache_control", json.dumps(body))

    def test_native_parallel_tool_permission_requires_true_and_tools(self):
        cases: list[dict[str, Any]] = [
            {},  # tools_api 等旧调用不传许可时继续关闭。
            *({"allow_parallel_tool_calls": value}
              for value in (False, True, None, 0, 1, "true", {}, [True])),
        ]
        capability = {"name": "inspect", "parameters": {"type": "object", "properties": {}}}
        for protocol in ("chat_completions", "responses", "anthropic_messages"):
            for has_tools in (False, True):
                tools = native_tool_definitions(protocol, [capability]) if has_tools else []
                for options in cases:
                    with self.subTest(protocol=protocol, tools=has_tools, options=options):
                        body = native_tool_request_body(
                            protocol=protocol, model="sample", system_prompt="system",
                            history=[], tools=tools, max_tokens=400, **options,
                        )
                        if protocol == "responses":
                            self.assertEqual(body["include"], ["reasoning.encrypted_content"])
                        if not has_tools:
                            for field in ("tools", "tool_choice", "parallel_tool_calls", "disable_parallel_tool_use"):
                                self.assertNotIn(field, body)
                            continue
                        enabled = options.get("allow_parallel_tool_calls") is True
                        self.assertEqual(body["tools"], tools)
                        if protocol == "anthropic_messages":
                            self.assertEqual(body["tool_choice"], {
                                "type": "auto", "disable_parallel_tool_use": not enabled,
                            })
                            self.assertNotIn("parallel_tool_calls", body)
                        else:
                            self.assertEqual(body["tool_choice"], "auto")
                            self.assertIs(body["parallel_tool_calls"], enabled)
                            self.assertNotIn("disable_parallel_tool_use", body)

    def test_parse_responses_native_tool_turn_and_build_result(self):
        turn = parse_native_tool_turn({
            "status": "completed",
            "output": [{
                "type": "function_call",
                "call_id": "call_1",
                "name": "mf_workspace_health",
                "arguments": "{}",
            }],
        }, "responses")
        self.assertEqual(turn.tool_calls[0].name, "mf_workspace_health")
        self.assertEqual(turn.finish_reason, "stop")
        result = native_tool_result("responses", turn.tool_calls[0].call_id, '{"ok":true}')
        self.assertEqual(result["type"], "function_call_output")
        self.assertEqual(result["call_id"], "call_1")

    def test_parse_chat_native_tool_turn_and_build_result(self):
        turn = parse_native_tool_turn({
            "choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_2",
                    "type": "function",
                    "function": {
                        "name": "mf_downloads_diagnose",
                        "arguments": '{"limit":5}',
                    },
                }],
            }}],
        }, "chat_completions")
        self.assertEqual(turn.tool_calls[0].arguments, {"limit": 5})
        self.assertEqual(turn.finish_reason, "tool_calls")
        result = native_tool_result("chat_completions", turn.tool_calls[0].call_id, '{"ok":true}')
        self.assertEqual(result["role"], "tool")
        self.assertEqual(result["tool_call_id"], "call_2")

    def test_parse_anthropic_native_tool_turn_and_build_result(self):
        turn = parse_native_tool_turn({
            "type": "message",
            "stop_reason": "tool_use",
            "content": [{
                "type": "tool_use",
                "id": "toolu_1",
                "name": "mf_library_patrol_status",
                "input": {},
            }],
        }, "anthropic_messages")
        self.assertEqual(turn.tool_calls[0].call_id, "toolu_1")
        self.assertEqual(turn.finish_reason, "tool_use")
        result = native_tool_result("anthropic_messages", turn.tool_calls[0].call_id, '{"ok":true}')
        self.assertEqual(result["type"], "tool_result")
        self.assertEqual(result["tool_use_id"], "toolu_1")

    def test_native_tool_turn_rejects_missing_or_truncated_finish_reason(self):
        chat_call = {
            "id": "call_chat",
            "type": "function",
            "function": {"name": "mf_workspace_health", "arguments": "{}"},
        }
        responses_call = {
            "type": "function_call",
            "call_id": "call_responses",
            "name": "mf_workspace_health",
            "arguments": "{}",
        }
        anthropic_call = {
            "type": "tool_use",
            "id": "call_anthropic",
            "name": "mf_workspace_health",
            "input": {},
        }
        cases = (
            ("chat_completions", {"choices": [{"message": {"tool_calls": [chat_call]}}]}),
            ("chat_completions", {"choices": [{"finish_reason": "length", "message": {"tool_calls": [chat_call]}}]}),
            ("responses", {"output": [responses_call]}),
            ("responses", {"status": "incomplete", "output": [responses_call]}),
            ("anthropic_messages", {"type": "message", "content": [anthropic_call]}),
            ("anthropic_messages", {"type": "message", "stop_reason": "max_tokens", "content": [anthropic_call]}),
        )
        for protocol, envelope in cases:
            with self.subTest(protocol=protocol, envelope=envelope), self.assertRaises(
                ProviderStreamError
            ):
                parse_native_tool_turn(envelope, protocol)

    def test_native_tool_arguments_must_be_complete_json_object(self):
        with self.assertRaises(ValueError):
            parse_native_tool_turn({
                "choices": [{"finish_reason": "tool_calls", "message": {
                    "tool_calls": [{
                        "id": "call_bad",
                        "type": "function",
                        "function": {
                            "name": "mf_workspace_health",
                            "arguments": '{"broken":',
                        },
                    }],
                }}],
            }, "chat_completions")

    def test_native_tool_arguments_share_json_and_size_contract(self):
        def parse(arguments):
            return parse_native_tool_turn({
                "choices": [{"finish_reason": "tool_calls", "message": {
                    "tool_calls": [{
                        "id": "call_args",
                        "type": "function",
                        "function": {
                            "name": "mf_workspace_health",
                            "arguments": arguments,
                        },
                    }],
                }}],
            }, "chat_completions").tool_calls[0].arguments

        valid = {"value": "x" * (32_768 - 12)}
        valid_json = '{"value":"' + "x" * (32_768 - 12) + '"}'
        valid_cases = (
            ({"limit": 5}, {"limit": 5}),
            ('{"limit":5}', {"limit": 5}),
            (None, {}),
            ("", {}),
            (valid, valid),
            (valid_json, valid),
        )
        for arguments, expected in valid_cases:
            with self.subTest(argument_type=type(arguments).__name__, size=len(str(arguments))):
                self.assertEqual(parse(arguments), expected)

        invalid = (
            ([], "not_object"), (False, "not_object"), (0, "not_object"),
            ("{broken", "invalid_json"),
            ("[]", "not_object"),
            ('{"value":"' + "x" * (32_768 - 11) + '"}', "too_large"),
            ({"value": "x" * (32_768 - 11)}, "too_large"),
        )
        for arguments, reason in invalid:
            with self.subTest(reason=reason, argument_type=type(arguments).__name__):
                with self.assertRaises(ToolArgumentsDecodeError) as raised:
                    parse(arguments)
                self.assertEqual(raised.exception.reason, reason)


async def _chunks(*parts: bytes):
    for part in parts:
        yield part


class LLMProviderStreamTests(unittest.IsolatedAsyncioTestCase):
    def test_text_stream_bodies_and_accept_header(self):
        for protocol in ("responses", "chat_completions", "anthropic_messages"):
            with self.subTest(protocol=protocol):
                body = text_stream_request_body(
                    protocol=protocol,
                    model="model",
                    system_prompt="system",
                    user_content="user",
                    max_tokens=123,
                )
                self.assertTrue(body["stream"])
                self.assertNotIn("tools", body)
                headers = provider_headers(protocol, "secret", stream=True)
                self.assertEqual(headers["Accept"], "text/event-stream")
        self.assertIn("input", text_stream_request_body(
            protocol="responses", model="model", system_prompt="system",
            user_content="user", max_tokens=123,
        ))
        self.assertIn("messages", text_stream_request_body(
            protocol="chat_completions", model="model", system_prompt="system",
            user_content="user", max_tokens=123,
        ))
        anthropic = text_stream_request_body(
            protocol="anthropic_messages", model="model", system_prompt="system",
            user_content="user", max_tokens=123,
        )
        self.assertEqual(anthropic["system"], "system")

    async def test_responses_stream_handles_fragmented_utf8(self):
        wire = (
            'data: {"type":"response.output_text.delta","delta":"你"}\r\n\r\n'
            'data: {"type":"response.output_text.delta","delta":"好"}\n\n'
            'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
        ).encode("utf-8")
        split = wire.index("你".encode("utf-8")) + 1
        deltas = [delta async for delta in iter_provider_text_deltas(
            _chunks(wire[:split], wire[split:split + 2], wire[split + 2:]),
            protocol="responses",
        )]
        self.assertEqual(deltas, ["你", "好"])

    async def test_stream_filters_split_thinking_markup_and_reasoning_fields(self):
        wire = (
            b'data: {"choices":[{"delta":{"reasoning_content":"private","content":"<thi"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"nk>secret</think>public"},"finish_reason":"stop"}]}\n\n'
            b'data: [DONE]\n\n'
        )
        self.assertEqual(
            [delta async for delta in iter_provider_text_deltas(
                _chunks(wire), protocol="chat_completions"
            )],
            ["public"],
        )

    async def test_chat_completions_stream_requires_terminal_event(self):
        valid = (
            b'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            b'data: [DONE]\n\n'
        )
        self.assertEqual(
            [delta async for delta in iter_provider_text_deltas(
                _chunks(valid), protocol="chat_completions"
            )],
            ["hello"],
        )
        truncated = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        with self.assertRaises(ProviderStreamError):
            _ = [delta async for delta in iter_provider_text_deltas(
                _chunks(truncated), protocol="chat_completions"
            )]

    async def test_anthropic_stream_text_and_completion(self):
        wire = (
            b'event: message_start\ndata: {"type":"message_start"}\n\n'
            b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n\n'
            b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        )
        self.assertEqual(
            [delta async for delta in iter_provider_text_deltas(
                _chunks(wire), protocol="anthropic_messages"
            )],
            ["ok"],
        )

    async def test_pure_text_stream_rejects_tool_arguments_and_oversized_events(self):
        tool_stream = (
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0}]}}]}\n\n'
            b'data: [DONE]\n\n'
        )
        with self.assertRaises(ProviderStreamError):
            _ = [delta async for delta in iter_provider_text_deltas(
                _chunks(tool_stream), protocol="chat_completions"
            )]
        oversized = b'data: {"type":"response.output_text.delta","delta":"0123456789"}\n\n'
        with self.assertRaises(ProviderStreamError):
            _ = [delta async for delta in iter_provider_text_deltas(
                _chunks(oversized), protocol="responses", max_event_bytes=8
            )]


if __name__ == "__main__":
    unittest.main()
