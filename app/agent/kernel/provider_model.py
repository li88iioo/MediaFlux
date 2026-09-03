"""OpenAI Responses、Chat Completions 与 Anthropic Messages 的统一模型流。"""

from __future__ import annotations

import asyncio
import codecs
import json
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.clients.openai_compatible import (
    extract_provider_usage,
    native_tool_definitions,
    native_tool_request_body,
    normalize_provider_location,
    parse_native_tool_turn,
    protocol_attempts,
    provider_headers,
    resolve_protocol,
    strip_reasoning_markup,
)
from app.indexers.http import FixedHostHttpClient

from .model import (
    ModelAdapter,
    ModelEvent,
    ModelEventType,
    ModelMessage,
    ModelRequest,
    ModelToolCall,
)
from .state import CancellationToken


class ModelProviderError(RuntimeError):
    pass


_MODEL_IDLE_TIMEOUT_FLOOR_SECONDS = 30


def _network_idle_timeout_seconds(configured_timeout_seconds: int) -> int:
    """模型可能在原生工具调用前长时间不输出，给流式读取保留合理下限。"""
    return min(
        120, max(_MODEL_IDLE_TIMEOUT_FLOOR_SECONDS, int(configured_timeout_seconds))
    )


def _stream_deadline_seconds(network_timeout_seconds: int) -> int:
    """流式响应使用网络空闲超时，同时保留独立的总时限保险丝。"""
    return min(300, max(60, int(network_timeout_seconds) * 4))


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    api_url: str
    model: str
    api_key: str = ""
    protocol: str = "auto"
    timeout_seconds: int = 30
    max_response_bytes: int = 2 * 1024 * 1024

    def __post_init__(self) -> None:
        if not str(self.api_url or "").strip():
            raise ValueError("api_url is required")
        if not str(self.model or "").strip() or len(str(self.model)) > 200:
            raise ValueError("model is invalid")
        if not 2 <= int(self.timeout_seconds) <= 120:
            raise ValueError("timeout_seconds out of range")

    @classmethod
    def from_config(cls) -> ProviderSettings:
        from app import config

        return cls(
            api_url=str(config.get("AGENT_LLM_API_URL", "") or "").strip(),
            model=str(config.get("AGENT_LLM_MODEL", "") or "").strip(),
            api_key=str(config.get("AGENT_LLM_API_KEY", "") or "").strip(),
            protocol=str(config.get("AGENT_LLM_PROTOCOL", "auto") or "auto").strip(),
            timeout_seconds=max(
                2,
                min(120, config.get_int("AGENT_LLM_TIMEOUT_SECONDS", 30)),
            ),
        )


class _ReasoningFilter:
    def __init__(self) -> None:
        self.pending = ""
        self.inside = False

    @staticmethod
    def _suffix_length(value: str, marker: str) -> int:
        lowered = value.casefold()
        target = marker.casefold()
        for length in range(min(len(lowered), len(target) - 1), 0, -1):
            if lowered.endswith(target[:length]):
                return length
        return 0

    def feed(self, value: str) -> str:
        self.pending += value
        visible: list[str] = []
        while self.pending:
            lowered = self.pending.casefold()
            if self.inside:
                close_at = lowered.find("</think")
                if close_at < 0:
                    keep = self._suffix_length(self.pending, "</think")
                    self.pending = self.pending[-keep:] if keep else ""
                    return "".join(visible)
                close_end = self.pending.find(">", close_at)
                if close_end < 0:
                    self.pending = self.pending[close_at:]
                    return "".join(visible)
                self.pending = self.pending[close_end + 1 :]
                self.inside = False
                continue
            open_at = lowered.find("<think")
            if open_at >= 0:
                visible.append(self.pending[:open_at])
                open_end = self.pending.find(">", open_at)
                if open_end < 0:
                    self.pending = self.pending[open_at:]
                    return "".join(visible)
                self.pending = self.pending[open_end + 1 :]
                self.inside = True
                continue
            keep = self._suffix_length(self.pending, "<think")
            if keep:
                visible.append(self.pending[:-keep])
                self.pending = self.pending[-keep:]
            else:
                visible.append(self.pending)
                self.pending = ""
            return "".join(visible)
        return "".join(visible)

    def finalize(self) -> str:
        if self.inside:
            self.pending = ""
            return ""
        result = strip_reasoning_markup(self.pending)
        self.pending = ""
        return result


async def _iter_sse_data(
    chunks: AsyncIterator[bytes], *, max_event_bytes: int = 128 * 1024
) -> AsyncIterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    pending = ""
    lines: list[str] = []
    size = 0

    async def decoded_lines() -> AsyncIterator[str]:
        nonlocal pending
        async for chunk in chunks:
            try:
                pending += decoder.decode(chunk, final=False)
            except UnicodeDecodeError as exc:
                raise ModelProviderError("Provider 流包含无效 UTF-8") from exc
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                yield line.removesuffix("\r")
        try:
            pending += decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise ModelProviderError("Provider 流包含无效 UTF-8") from exc
        if pending:
            yield pending.removesuffix("\r")
            pending = ""

    async for line in decoded_lines():
        if not line:
            if lines:
                yield "\n".join(lines)
                lines = []
                size = 0
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if field != "data":
            continue
        if separator and value.startswith(" "):
            value = value[1:]
        size += len(value.encode("utf-8"))
        if size > max_event_bytes:
            raise ModelProviderError("Provider SSE 事件过大")
        lines.append(value)
    if lines:
        yield "\n".join(lines)


async def _iter_sse_json(chunks: AsyncIterator[bytes]) -> AsyncIterator[dict[str, Any]]:
    async for data in _iter_sse_data(chunks):
        if data.strip() == "[DONE]":
            yield {"__done__": True}
            return
        try:
            value = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ModelProviderError("Provider SSE 数据不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise ModelProviderError("Provider SSE 数据格式无效")
        if value.get("type") == "error" or isinstance(value.get("error"), dict):
            raise ModelProviderError("Provider 返回流式错误")
        yield value


def _parse_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    text = str(value or "").strip() or "{}"
    if len(text) > 32_768:
        raise ModelProviderError("模型工具参数过大")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelProviderError("模型工具参数不是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise ModelProviderError("模型工具参数必须是对象")
    return parsed


def _usage_dict(value: Any, protocol: str) -> dict[str, int]:
    usage = extract_provider_usage(value, protocol)
    return usage.to_dict() if usage is not None else {}


async def iter_protocol_model_events(
    chunks: AsyncIterator[bytes], *, protocol: str
) -> AsyncIterator[ModelEvent]:
    """把三个 Provider 的真实 SSE 统一为 Kernel ModelEvent。"""
    normalized = resolve_protocol(protocol)
    reasoning = _ReasoningFilter()
    calls: dict[str, dict[str, Any]] = {}
    emitted_calls: set[str] = set()
    finish_reason = ""
    completed = False

    def call_key(*, index: Any = None, item_id: Any = None) -> str:
        if item_id not in {None, ""}:
            return f"id:{item_id}"
        return f"index:{index if index is not None else 0}"

    async def emit_call(key: str) -> AsyncIterator[ModelEvent]:
        if key in emitted_calls:
            return
        raw = calls.get(key)
        if not raw:
            return
        name = str(raw.get("name") or "").strip()
        if not name:
            return
        call_id = str(raw.get("call_id") or raw.get("id") or key).strip()
        arguments = _parse_arguments(raw.get("arguments"))
        emitted_calls.add(key)
        yield ModelEvent(
            ModelEventType.TOOL_CALL_COMPLETED,
            tool_call=ModelToolCall(call_id=call_id, name=name, arguments=arguments),
        )

    async for event in _iter_sse_json(chunks):
        if event.get("__done__"):
            completed = True
            break
        if normalized == "responses":
            event_type = str(event.get("type") or "")
            if event_type == "response.output_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    visible = reasoning.feed(delta)
                    if visible:
                        yield ModelEvent(ModelEventType.TEXT_DELTA, text=visible)
            elif event_type == "response.output_item.added":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "function_call":
                    key = call_key(
                        index=event.get("output_index"), item_id=item.get("id")
                    )
                    calls[key] = {
                        "id": item.get("id"),
                        "call_id": item.get("call_id"),
                        "name": item.get("name"),
                        "arguments": str(item.get("arguments") or ""),
                    }
            elif event_type == "response.function_call_arguments.delta":
                key = call_key(
                    index=event.get("output_index"), item_id=event.get("item_id")
                )
                raw = calls.setdefault(key, {})
                raw["arguments"] = str(raw.get("arguments") or "") + str(
                    event.get("delta") or ""
                )
            elif event_type == "response.function_call_arguments.done":
                key = call_key(
                    index=event.get("output_index"), item_id=event.get("item_id")
                )
                raw = calls.setdefault(key, {})
                if event.get("arguments") is not None:
                    raw["arguments"] = event.get("arguments")
                async for item in emit_call(key):
                    yield item
            elif event_type == "response.output_item.done":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "function_call":
                    key = call_key(
                        index=event.get("output_index"), item_id=item.get("id")
                    )
                    raw = calls.setdefault(key, {})
                    raw.update(
                        {
                            "id": item.get("id"),
                            "call_id": item.get("call_id"),
                            "name": item.get("name"),
                            "arguments": item.get(
                                "arguments", raw.get("arguments", "")
                            ),
                        }
                    )
                    async for output in emit_call(key):
                        yield output
            elif event_type == "response.completed":
                response = event.get("response")
                if isinstance(response, dict):
                    usage = _usage_dict(response, "responses")
                    if usage:
                        yield ModelEvent(ModelEventType.USAGE, usage=usage)
                    finish_reason = "stop"
                completed = True
                break
            elif event_type in {"response.failed", "response.incomplete", "error"}:
                raise ModelProviderError("Responses API 未完整结束")
            continue

        if normalized == "chat_completions":
            usage = _usage_dict(event, "chat_completions")
            if usage:
                yield ModelEvent(ModelEventType.USAGE, usage=usage)
            choices = event.get("choices")
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        visible = reasoning.feed(content)
                        if visible:
                            yield ModelEvent(ModelEventType.TEXT_DELTA, text=visible)
                    for tool_call in delta.get("tool_calls") or ():
                        if not isinstance(tool_call, dict):
                            continue
                        key = call_key(index=tool_call.get("index"))
                        raw = calls.setdefault(key, {})
                        if tool_call.get("id"):
                            raw["call_id"] = tool_call["id"]
                        function = tool_call.get("function")
                        if isinstance(function, dict):
                            if function.get("name"):
                                raw["name"] = str(raw.get("name") or "") + str(
                                    function["name"]
                                )
                            if function.get("arguments"):
                                raw["arguments"] = str(
                                    raw.get("arguments") or ""
                                ) + str(function["arguments"])
                reason = str(choice.get("finish_reason") or "")
                if reason:
                    finish_reason = reason
                    completed = reason in {"stop", "tool_calls", "end_turn"}
            continue

        event_type = str(event.get("type") or "")
        if event_type == "message_start":
            message = event.get("message")
            usage = (
                _usage_dict(message, "anthropic_messages")
                if isinstance(message, dict)
                else {}
            )
            if usage:
                yield ModelEvent(ModelEventType.USAGE, usage=usage)
        elif event_type == "content_block_start":
            block = event.get("content_block")
            if not isinstance(block, dict):
                continue
            index = event.get("index")
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    visible = reasoning.feed(text)
                    if visible:
                        yield ModelEvent(ModelEventType.TEXT_DELTA, text=visible)
            elif block.get("type") == "tool_use":
                key = call_key(index=index)
                calls[key] = {
                    "call_id": block.get("id"),
                    "name": block.get("name"),
                    "arguments": json.dumps(
                        block.get("input") or {}, ensure_ascii=False
                    )
                    if block.get("input")
                    else "",
                }
        elif event_type == "content_block_delta":
            delta = event.get("delta")
            if not isinstance(delta, dict):
                continue
            if delta.get("type") == "text_delta":
                text = delta.get("text")
                if isinstance(text, str) and text:
                    visible = reasoning.feed(text)
                    if visible:
                        yield ModelEvent(ModelEventType.TEXT_DELTA, text=visible)
            elif delta.get("type") == "input_json_delta":
                key = call_key(index=event.get("index"))
                raw = calls.setdefault(key, {})
                raw["arguments"] = str(raw.get("arguments") or "") + str(
                    delta.get("partial_json") or ""
                )
        elif event_type == "content_block_stop":
            key = call_key(index=event.get("index"))
            async for output in emit_call(key):
                yield output
        elif event_type == "message_delta":
            delta = event.get("delta")
            if isinstance(delta, dict) and delta.get("stop_reason"):
                finish_reason = str(delta["stop_reason"])
            usage = _usage_dict(event, "anthropic_messages")
            if usage:
                yield ModelEvent(ModelEventType.USAGE, usage=usage)
        elif event_type == "message_stop":
            completed = True
            break

    for key in list(calls):
        async for output in emit_call(key):
            yield output
    tail = reasoning.finalize()
    if tail:
        yield ModelEvent(ModelEventType.TEXT_DELTA, text=tail)
    if not completed:
        raise ModelProviderError("Provider 流在完成事件前中断")
    yield ModelEvent(ModelEventType.FINISH, finish_reason=finish_reason or "stop")


def _history_for_protocol(
    protocol: str,
    system_prompt: str,
    messages: Sequence[ModelMessage],
) -> list[dict[str, Any]]:
    if protocol == "responses":
        history: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            }
        ]
        for message in messages:
            if message.role == "tool":
                history.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id,
                        "output": message.content,
                    }
                )
                continue
            content_type = (
                "output_text" if message.role == "assistant" else "input_text"
            )
            if message.content:
                history.append(
                    {
                        "role": message.role,
                        "content": [{"type": content_type, "text": message.content}],
                    }
                )
            for call in message.tool_calls:
                history.append(
                    {
                        "type": "function_call",
                        "call_id": call.call_id,
                        "name": call.name,
                        "arguments": json.dumps(
                            dict(call.arguments),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    }
                )
        return history

    if protocol == "chat_completions":
        history = [{"role": "system", "content": system_prompt}]
        for message in messages:
            item: dict[str, Any] = {
                "role": message.role,
                "content": message.content or None,
            }
            if message.role == "tool":
                item["tool_call_id"] = message.tool_call_id
                item["content"] = message.content
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(
                                dict(call.arguments),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                    for call in message.tool_calls
                ]
            history.append(item)
        return history

    history = []
    for message in messages:
        if message.role == "tool":
            item = {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id,
                        "content": message.content,
                    }
                ],
            }
        elif message.role == "assistant" and message.tool_calls:
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": call.call_id,
                    "name": call.name,
                    "input": dict(call.arguments),
                }
                for call in message.tool_calls
            )
            item = {"role": "assistant", "content": blocks}
        else:
            item = {"role": message.role, "content": message.content}
        if history and history[-1].get("role") == item.get("role") == "user":
            previous = history[-1]
            previous_content = previous.get("content")
            current_content = item.get("content")
            if not isinstance(previous_content, list):
                previous_content = [
                    {"type": "text", "text": str(previous_content or "")}
                ]
            if not isinstance(current_content, list):
                current_content = [{"type": "text", "text": str(current_content or "")}]
            previous["content"] = previous_content + current_content
        else:
            history.append(item)
    return history


class OpenAICompatibleModelAdapter(ModelAdapter):
    """唯一 Provider 适配器；不包含任何媒体业务路由。"""

    def __init__(
        self,
        settings: ProviderSettings,
        *,
        client_factory: Callable[..., FixedHostHttpClient] = FixedHostHttpClient,
    ) -> None:
        self.settings = settings
        self.client_factory = client_factory

    async def stream(
        self,
        request: ModelRequest,
        *,
        cancellation: CancellationToken,
    ) -> AsyncIterator[ModelEvent]:
        location = normalize_provider_location(
            self.settings.api_url,
            https_only=True,
            public_only=True,
        )
        configured = resolve_protocol(self.settings.protocol, self.settings.api_url)
        network_idle_timeout = _network_idle_timeout_seconds(
            self.settings.timeout_seconds
        )
        client = self.client_factory(
            allowed_hosts={location.host},
            # FixedHostHttpClient 为普通索引 GET 预留整请求重试，会把传入
            # 预算折半为单次网络空闲超时。模型 POST 不做整请求重放，因此
            # 传入双倍预算。原生工具模型在首个 chunk 前可能需要较长推理，
            # Kernel 对过低的旧配置保留 30 秒网络空闲下限。
            timeout_seconds=min(240, network_idle_timeout * 2),
            max_response_bytes=self.settings.max_response_bytes,
            max_redirects=0,
            user_agent="MediaFlux-Agent-Kernel/1.0",
            pin_resolved_address=True,
        )
        emitted = False
        last_error: Exception | None = None
        try:
            # HTTP 客户端已经按配置限制连接/读写空闲时间；流式工具调用可能
            # 持续产生有效数据并超过该时长，不能再用同一个值把总响应硬切断。
            # 这里仅保留更宽松且有上限的整轮保险丝。
            async with asyncio.timeout(_stream_deadline_seconds(network_idle_timeout)):
                for protocol in protocol_attempts(configured):
                    cancellation.raise_if_cancelled()
                    definitions = native_tool_definitions(protocol, list(request.tools))
                    history = _history_for_protocol(
                        protocol, request.system_prompt, request.messages
                    )
                    body = native_tool_request_body(
                        protocol=protocol,
                        model=self.settings.model,
                        system_prompt=request.system_prompt,
                        history=history,
                        tools=definitions,
                        max_tokens=request.max_output_tokens,
                        stream=True,
                    )
                    try:
                        async with client.stream_post_json(
                            location.endpoint(protocol),
                            json=body,
                            headers=provider_headers(
                                protocol,
                                self.settings.api_key,
                                stream=True,
                            ),
                            max_redirects=0,
                        ) as response:
                            if (
                                response.status_code < 200
                                or response.status_code >= 300
                            ):
                                if emitted:
                                    raise ModelProviderError(
                                        "Provider 在输出后返回错误"
                                    )
                                last_error = ModelProviderError(
                                    f"Provider 请求失败（HTTP {response.status_code}）"
                                )
                                continue
                            content_type = str(
                                response.headers.get("content-type") or ""
                            ).lower()
                            if "text/event-stream" in content_type:
                                async for event in iter_protocol_model_events(
                                    response.aiter_bytes(), protocol=protocol
                                ):
                                    cancellation.raise_if_cancelled()
                                    emitted = True
                                    yield event
                                return
                            raw = bytearray()
                            async for chunk in response.aiter_bytes():
                                raw.extend(chunk)
                            try:
                                envelope = json.loads(raw.decode("utf-8"))
                                turn = parse_native_tool_turn(envelope, protocol)
                            except (
                                UnicodeDecodeError,
                                json.JSONDecodeError,
                                ValueError,
                            ) as exc:
                                last_error = ModelProviderError("Provider 返回格式无效")
                                if emitted:
                                    raise last_error from exc
                                continue
                            if turn.text:
                                emitted = True
                                yield ModelEvent(
                                    ModelEventType.TEXT_DELTA, text=turn.text
                                )
                            for call in turn.tool_calls:
                                emitted = True
                                yield ModelEvent(
                                    ModelEventType.TOOL_CALL_COMPLETED,
                                    tool_call=ModelToolCall(
                                        call_id=call.call_id,
                                        name=call.name,
                                        arguments=call.arguments,
                                    ),
                                )
                            if turn.usage is not None:
                                yield ModelEvent(
                                    ModelEventType.USAGE, usage=turn.usage.to_dict()
                                )
                            yield ModelEvent(
                                ModelEventType.FINISH, finish_reason="stop"
                            )
                            return
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        last_error = exc
                        if emitted:
                            raise
                        continue
        except TimeoutError as exc:
            raise ModelProviderError("Provider 请求超时") from exc
        finally:
            await client.aclose()
        if isinstance(last_error, ModelProviderError):
            raise last_error
        raise ModelProviderError("Provider 暂时不可用") from last_error
