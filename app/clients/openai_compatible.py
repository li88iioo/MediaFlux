"""LLM Provider URL、协议、认证与结构化输出适配。"""
from __future__ import annotations

import codecs
from dataclasses import dataclass
import json
from typing import Any, AsyncIterator
from urllib.parse import urlsplit, urlunsplit

ANTHROPIC_VERSION = "2023-06-01"
KNOWN_ENDPOINT_SUFFIXES = (
    "/chat/completions",
    "/responses",
    "/messages",
    "/models",
)
PROTOCOLS = frozenset({
    "auto",
    "responses",
    "chat_completions",
    "anthropic_messages",
})
SUPPORTED_PROTOCOLS_TEXT = "auto、responses、chat_completions 或 anthropic_messages"


@dataclass(frozen=True, slots=True)
class NativeToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class NativeToolTurn:
    text: str
    tool_calls: tuple[NativeToolCall, ...]
    assistant_entry: object | None = None


@dataclass(frozen=True, slots=True)
class ProviderLocation:
    base_url: str
    host: str

    def endpoint(self, protocol: str) -> str:
        normalized = normalize_protocol(protocol)
        suffix = {
            "responses": "/responses",
            "chat_completions": "/chat/completions",
            "anthropic_messages": "/messages",
        }.get(normalized, "/responses")
        return self.base_url.rstrip("/") + suffix

    @property
    def models_url(self) -> str:
        return self.base_url.rstrip("/") + "/models"


def normalize_protocol(value: object) -> str:
    protocol = str(value or "auto").strip().lower().replace("-", "_")
    return protocol if protocol in PROTOCOLS else "auto"


def infer_protocol_from_url(raw_url: object) -> str | None:
    """仅依据显式端点后缀推断协议；普通 Base URL 不做猜测。"""
    try:
        path = urlsplit(str(raw_url or "").strip()).path.rstrip("/").lower()
    except ValueError:
        return None
    if path.endswith("/chat/completions"):
        return "chat_completions"
    if path.endswith("/responses"):
        return "responses"
    if path.endswith("/messages"):
        return "anthropic_messages"
    return None


def resolve_protocol(value: object, raw_url: object = "") -> str:
    """解析配置协议；auto 仅在 URL 已写明端点时固定到对应协议。"""
    protocol = normalize_protocol(value)
    if protocol != "auto":
        return protocol
    return infer_protocol_from_url(raw_url) or "auto"


def protocol_attempts(protocol: object) -> tuple[str, ...]:
    """返回实际尝试顺序；auto 只在 OpenAI Responses 与 Chat 之间回退。"""
    normalized = normalize_protocol(protocol)
    if normalized == "auto":
        return ("responses", "chat_completions")
    return (normalized,)


def provider_headers(
    protocol: object,
    api_key: object = "",
    *,
    include_content_type: bool = True,
    stream: bool = False,
) -> dict[str, str]:
    """生成协议所需认证头，避免把 Anthropic Key 当 Bearer Token 发送。"""
    normalized = normalize_protocol(protocol)
    key = str(api_key or "").strip()
    if "\r" in key or "\n" in key or len(key) > 512:
        raise ValueError("AI API Key 格式无效")
    headers = {"Accept": "text/event-stream" if stream else "application/json"}
    if include_content_type:
        headers["Content-Type"] = "application/json"
    if normalized == "anthropic_messages":
        headers["anthropic-version"] = ANTHROPIC_VERSION
        if key:
            headers["x-api-key"] = key
    elif key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def normalize_provider_location(
    raw_url: object,
    *,
    https_only: bool = True,
    public_only: bool = True,
    max_length: int = 2048,
) -> ProviderLocation:
    raw = str(raw_url or "").strip().rstrip("/")
    if not raw or len(raw) > max_length or "\r" in raw or "\n" in raw:
        raise ValueError("AI Base URL 格式无效")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("AI Base URL 格式无效") from exc
    schemes = {"https"} if https_only else {"http", "https"}
    if (
        parsed.scheme.lower() not in schemes
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (https_only and port not in {None, 443})
    ):
        raise ValueError("AI Base URL 必须是无内嵌凭据、无查询参数的 HTTPS 地址")
    host = parsed.hostname.rstrip(".").lower()
    if public_only and (
        host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost")
    ):
        raise ValueError("AI Base URL 不允许 localhost")
    path = parsed.path.rstrip("/")
    for suffix in KNOWN_ENDPOINT_SUFFIXES:
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    if not path:
        path = "/v1"
    base_url = urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", "")).rstrip("/")
    return ProviderLocation(base_url=base_url, host=host)


def structured_request_body(
    *,
    protocol: str,
    model: str,
    system_prompt: str,
    user_content: str,
    schema_name: str,
    schema: dict[str, Any],
    max_tokens: int,
) -> dict[str, Any]:
    if protocol == "responses":
        return {
            "model": model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "input_text", "text": user_content}]},
            ],
            "temperature": 0,
            "max_output_tokens": max_tokens,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
        }
    if protocol == "anthropic_messages":
        return {
            "model": model,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
            "temperature": 0,
            "max_tokens": max_tokens,
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": schema,
                }
            },
        }
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
    }


def _text_from_blocks(blocks: object, *, allowed_types: set[str]) -> str:
    if not isinstance(blocks, list):
        raise ValueError("AI 响应格式无效")
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") not in allowed_types:
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    if not parts:
        raise ValueError("AI 响应格式无效")
    return "".join(parts)


def extract_output_text(envelope: object, protocol: str) -> str:
    if not isinstance(envelope, dict):
        raise ValueError("AI 响应格式无效")
    if protocol == "anthropic_messages":
        if envelope.get("type") == "error" or envelope.get("stop_reason") in {
            "max_tokens",
            "refusal",
        }:
            raise ValueError("AI 响应未完整结束")
        return _text_from_blocks(envelope.get("content"), allowed_types={"text"})
    if protocol == "chat_completions":
        content = envelope["choices"][0]["message"]["content"]
        if isinstance(content, str):
            return content
        return _text_from_blocks(content, allowed_types={"text", "output_text"})
    direct = envelope.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    parts: list[str] = []
    for item in envelope.get("output") or []:
        if not isinstance(item, dict):
            continue
        try:
            parts.append(
                _text_from_blocks(
                    item.get("content"), allowed_types={"output_text", "text"}
                )
            )
        except ValueError:
            continue
    if parts:
        return "".join(parts)
    raise ValueError("AI 响应格式无效")


def _supports_strict_function_schema(schema: object) -> bool:
    """仅为真正满足 OpenAI strict tools 约束的 schema 开启 strict。"""
    if not isinstance(schema, dict):
        return False
    schema_type = schema.get("type")
    if schema_type == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict) or schema.get("additionalProperties") is not False:
            return False
        required = schema.get("required", [])
        if not isinstance(required, list) or set(required) != set(properties):
            return False
        return all(_supports_strict_function_schema(item) for item in properties.values())
    if schema_type == "array":
        return _supports_strict_function_schema(schema.get("items"))
    if "anyOf" in schema:
        variants = schema.get("anyOf")
        return isinstance(variants, list) and bool(variants) and all(
            _supports_strict_function_schema(item) for item in variants
        )
    return schema_type in {"string", "number", "integer", "boolean", "null"}


def native_tool_definitions(
    protocol: str, capabilities: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """把服务端只读能力转换成各 Provider 的原生工具定义。"""
    definitions: list[dict[str, Any]] = []
    for capability in capabilities:
        name = str(capability.get("name") or "").strip()
        description = str(capability.get("description") or "").strip()[:600]
        parameters = capability.get("parameters")
        if not name or not isinstance(parameters, dict):
            continue
        if protocol == "anthropic_messages":
            definitions.append({
                "name": name,
                "description": description,
                "input_schema": parameters,
            })
        else:
            function = {
                "name": name,
                "description": description,
                "parameters": parameters,
            }
            if _supports_strict_function_schema(parameters):
                function["strict"] = True
            definitions.append(
                {"type": "function", **function}
                if protocol == "responses"
                else {"type": "function", "function": function}
            )
    return definitions


def native_tool_initial_history(
    protocol: str, *, system_prompt: str, user_content: str
) -> list[dict[str, Any]]:
    if protocol == "responses":
        return [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user_content}],
            },
        ]
    if protocol == "chat_completions":
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
    return [{"role": "user", "content": user_content}]


def native_tool_request_body(
    *,
    protocol: str,
    model: str,
    system_prompt: str,
    history: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int,
    stream: bool = False,
) -> dict[str, Any]:
    if protocol == "responses":
        body = {
            "model": model,
            "input": history,
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "temperature": 0,
            "max_output_tokens": max_tokens,
        }
    elif protocol == "anthropic_messages":
        body = {
            "model": model,
            "system": system_prompt,
            "messages": history,
            "tools": tools,
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
            "temperature": 0,
            "max_tokens": max_tokens,
        }
    else:
        body = {
            "model": model,
            "messages": history,
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "temperature": 0,
            "max_tokens": max_tokens,
        }
    if stream:
        body["stream"] = True
    return body


def text_stream_request_body(
    *,
    protocol: str,
    model: str,
    system_prompt: str,
    user_content: str,
    max_tokens: int,
) -> dict[str, Any]:
    """构造不携带工具的纯文本流式请求。"""
    normalized = normalize_protocol(protocol)
    if normalized == "responses":
        return {
            "model": model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": system_prompt}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": user_content}],
                },
            ],
            "temperature": 0,
            "max_output_tokens": max_tokens,
            "stream": True,
        }
    if normalized == "anthropic_messages":
        return {
            "model": model,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": True,
        }
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": True,
    }


class ProviderStreamError(ValueError):
    """Provider 流不完整、越界或返回错误。"""


async def _iter_sse_data(
    chunks: AsyncIterator[bytes], *, max_event_bytes: int = 64 * 1024
) -> AsyncIterator[str]:
    if max_event_bytes <= 0:
        raise ValueError("max_event_bytes must be positive")
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    pending = ""
    data_lines: list[str] = []
    event_bytes = 0

    async def _decoded() -> AsyncIterator[str]:
        nonlocal pending
        async for chunk in chunks:
            try:
                pending += decoder.decode(chunk, final=False)
            except UnicodeDecodeError as exc:
                raise ProviderStreamError("Provider 流包含无效 UTF-8") from exc
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                yield line[:-1] if line.endswith("\r") else line
        try:
            pending += decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise ProviderStreamError("Provider 流包含无效 UTF-8") from exc
        if pending:
            yield pending[:-1] if pending.endswith("\r") else pending
            pending = ""

    async for line in _decoded():
        if line == "":
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
                event_bytes = 0
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if field != "data":
            continue
        if separator and value.startswith(" "):
            value = value[1:]
        event_bytes += len(value.encode("utf-8"))
        if event_bytes > max_event_bytes:
            raise ProviderStreamError("Provider SSE 事件过大")
        data_lines.append(value)
    if data_lines:
        yield "\n".join(data_lines)


async def iter_provider_text_deltas(
    chunks: AsyncIterator[bytes],
    *,
    protocol: str,
    max_event_bytes: int = 64 * 1024,
) -> AsyncIterator[str]:
    """把三类 Provider SSE 统一成经过终止校验的文本增量。"""
    normalized = normalize_protocol(protocol)
    if normalized == "auto":
        raise ValueError("流式解析需要具体协议")
    completed = False
    accepted_finish = False

    async for data in _iter_sse_data(chunks, max_event_bytes=max_event_bytes):
        if normalized == "chat_completions" and data.strip() == "[DONE]":
            completed = True
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ProviderStreamError("Provider SSE 数据不是有效 JSON") from exc
        if not isinstance(event, dict):
            raise ProviderStreamError("Provider SSE 数据格式无效")
        if event.get("type") == "error" or isinstance(event.get("error"), dict):
            raise ProviderStreamError("Provider 返回流式错误")

        if normalized == "responses":
            event_type = str(event.get("type") or "")
            if event_type == "response.output_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    yield delta
            elif event_type == "response.completed":
                response = event.get("response")
                if isinstance(response, dict) and response.get("status") not in {None, "completed"}:
                    raise ProviderStreamError("Responses API 未完整结束")
                completed = True
                break
            elif event_type in {"response.failed", "response.incomplete", "error"}:
                raise ProviderStreamError("Responses API 未完整结束")
            continue

        if normalized == "chat_completions":
            choices = event.get("choices")
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    if delta.get("tool_calls"):
                        raise ProviderStreamError("纯文本流意外包含工具调用")
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        yield content
                finish_reason = choice.get("finish_reason")
                if finish_reason in {"stop", "end_turn"}:
                    accepted_finish = True
                elif finish_reason not in {None, ""}:
                    raise ProviderStreamError("Chat Completions 未完整结束")
            continue

        event_type = str(event.get("type") or "")
        if event_type == "content_block_delta":
            delta = event.get("delta")
            if not isinstance(delta, dict):
                continue
            if delta.get("type") == "text_delta":
                text = delta.get("text")
                if isinstance(text, str) and text:
                    yield text
            elif delta.get("type") == "input_json_delta":
                raise ProviderStreamError("纯文本流意外包含工具参数")
        elif event_type == "message_delta":
            delta = event.get("delta")
            stop_reason = delta.get("stop_reason") if isinstance(delta, dict) else None
            if stop_reason in {"end_turn", "stop_sequence"}:
                accepted_finish = True
            elif stop_reason not in {None, ""}:
                raise ProviderStreamError("Anthropic Messages 未完整结束")
        elif event_type == "message_stop":
            completed = True
            break

    if normalized == "chat_completions" and accepted_finish:
        completed = True
    if normalized == "anthropic_messages" and not completed:
        completed = False
    if not completed:
        raise ProviderStreamError("Provider 流在完成事件前中断")


def _native_arguments(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or len(value) > 8_192:
        raise ValueError("AI 工具参数格式无效")
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("AI 工具参数格式无效")
    return parsed


def parse_native_tool_turn(envelope: object, protocol: str) -> NativeToolTurn:
    """解析完整的 Provider 回合；不会执行任何工具。"""
    if not isinstance(envelope, dict) or envelope.get("type") == "error":
        raise ValueError("AI 响应格式无效")
    calls: list[NativeToolCall] = []
    text_parts: list[str] = []

    if protocol == "responses":
        output = envelope.get("output")
        if not isinstance(output, list):
            raise ValueError("AI 响应格式无效")
        assistant_items: list[dict[str, Any]] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "function_call":
                call = NativeToolCall(
                    call_id=str(item.get("call_id") or item.get("id") or ""),
                    name=str(item.get("name") or ""),
                    arguments=_native_arguments(item.get("arguments")),
                )
                calls.append(call)
                assistant_items.append({
                    "type": "function_call",
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": json.dumps(
                        call.arguments, ensure_ascii=False, separators=(",", ":")
                    ),
                })
            elif item_type == "message":
                try:
                    text_parts.append(
                        _text_from_blocks(
                            item.get("content"), allowed_types={"output_text", "text"}
                        )
                    )
                except ValueError:
                    continue
        direct = envelope.get("output_text")
        if isinstance(direct, str) and direct and not text_parts:
            text_parts.append(direct)
        return NativeToolTurn(
            text="".join(text_parts),
            tool_calls=tuple(calls),
            assistant_entry=assistant_items,
        )

    if protocol == "chat_completions":
        message = envelope["choices"][0]["message"]
        if not isinstance(message, dict):
            raise ValueError("AI 响应格式无效")
        raw_content = message.get("content")
        if isinstance(raw_content, str):
            text_parts.append(raw_content)
        elif isinstance(raw_content, list):
            try:
                text_parts.append(
                    _text_from_blocks(raw_content, allowed_types={"text", "output_text"})
                )
            except ValueError:
                pass
        assistant_calls: list[dict[str, Any]] = []
        for item in message.get("tool_calls") or []:
            if not isinstance(item, dict) or item.get("type") != "function":
                continue
            function = item.get("function")
            if not isinstance(function, dict):
                raise ValueError("AI 工具调用格式无效")
            call = NativeToolCall(
                call_id=str(item.get("id") or ""),
                name=str(function.get("name") or ""),
                arguments=_native_arguments(function.get("arguments")),
            )
            calls.append(call)
            assistant_calls.append({
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(
                        call.arguments, ensure_ascii=False, separators=(",", ":")
                    ),
                },
            })
        assistant_entry: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(text_parts) or None,
        }
        if assistant_calls:
            assistant_entry["tool_calls"] = assistant_calls
        return NativeToolTurn(
            text="".join(text_parts),
            tool_calls=tuple(calls),
            assistant_entry=assistant_entry,
        )

    if envelope.get("stop_reason") in {"max_tokens", "refusal"}:
        raise ValueError("AI 响应未完整结束")
    content = envelope.get("content")
    if not isinstance(content, list):
        raise ValueError("AI 响应格式无效")
    assistant_blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
                assistant_blocks.append({"type": "text", "text": text})
        elif block.get("type") == "tool_use":
            call = NativeToolCall(
                call_id=str(block.get("id") or ""),
                name=str(block.get("name") or ""),
                arguments=_native_arguments(block.get("input")),
            )
            calls.append(call)
            assistant_blocks.append({
                "type": "tool_use",
                "id": call.call_id,
                "name": call.name,
                "input": call.arguments,
            })
    return NativeToolTurn(
        text="".join(text_parts),
        tool_calls=tuple(calls),
        assistant_entry={"role": "assistant", "content": assistant_blocks},
    )


def append_native_tool_results(
    protocol: str,
    history: list[dict[str, Any]],
    turn: NativeToolTurn,
    outputs: list[tuple[NativeToolCall, str]],
) -> list[dict[str, Any]]:
    """追加完整、已校验工具结果；调用方必须先完成服务端安全投影。"""
    updated = list(history)
    if protocol == "responses":
        assistant_items = turn.assistant_entry
        if isinstance(assistant_items, list):
            updated.extend(item for item in assistant_items if isinstance(item, dict))
        updated.extend(
            {"type": "function_call_output", "call_id": call.call_id, "output": output}
            for call, output in outputs
        )
        return updated
    if isinstance(turn.assistant_entry, dict):
        updated.append(turn.assistant_entry)
    if protocol == "chat_completions":
        updated.extend(
            {"role": "tool", "tool_call_id": call.call_id, "content": output}
            for call, output in outputs
        )
        return updated
    updated.append({
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": call.call_id, "content": output}
            for call, output in outputs
        ],
    })
    return updated
