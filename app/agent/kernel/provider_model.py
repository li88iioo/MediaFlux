"""OpenAI Responses、Chat Completions 与 Anthropic Messages 的统一模型流。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from time import monotonic
from typing import Any, Literal, NoReturn

import httpx

from app.agent.model_catalog import provider_channel_fingerprint
from app.agent.model_context_budget import conservative_tokens
from app.clients.http import FixedHostHttpClient
from app.clients.openai_compatible import (
    PROTOCOL_FALLBACK_MAX_BYTES,
    PROTOCOL_FALLBACK_STATUS_CODES,
    REASONING_EFFORTS,
    ProviderStreamError,
    ReasoningDeltaFilter,
    ToolArgumentsDecodeError,
    anthropic_private_layout,
    extract_provider_usage,
    is_protocol_fallback_error,
    iter_provider_sse_events,
    native_reasoning_item,
    native_text_message,
    native_tool_call,
    native_tool_definitions,
    native_tool_request_body,
    native_tool_result,
    normalize_provider_location,
    parse_generation_profiles,
    parse_native_call,
    parse_native_tool_turn,
    protocol_attempts,
    provider_finish_reason,
    provider_headers,
    resolve_generation_profile,
    resolve_protocol,
    responses_private_layout,
    validate_repair_batch,
    validate_repair_layout,
)

from .model import (
    ModelAdapter,
    ModelEvent,
    ModelEventType,
    ModelMessage,
    ModelRequest,
    ModelToolCall,
    ProviderState,
)
from .model_budget import is_window_feedback, parse_budget_profiles, reduced_output_body
from .state_types import CancellationToken


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    """仅记录故障阶段和数值，不保存地址、请求体或原始异常文本。"""

    kind: str
    phase: str
    protocol: str
    attempts: int
    http_status: int | None
    emitted: bool
    elapsed_ms: int
    idle_timeout_seconds: int
    deadline_seconds: int


class ModelProviderError(RuntimeError):
    failure: ProviderFailure | None = None


class UncommittableModelTurn(ModelProviderError):
    """当前原生回合不能提交；只允许 Kernel 从之前完整历史有界重建。"""

    def __init__(self, message: str, *, reason: Literal["incomplete", "unreplayable_arguments"] = "incomplete") -> None:
        super().__init__(message)
        self.reason = reason


def _failure_kind(exc: BaseException | None, http_status: int | None) -> str:
    # 使用白名单类型，不记录可能包含 URL/令牌的异常消息或任意类名。
    if isinstance(exc, UncommittableModelTurn):
        return "incomplete_response" if exc.reason == "incomplete" else "invalid_tool_arguments"
    for kind, name in (
        (httpx.ConnectTimeout, "connect_timeout"),
        (httpx.ReadTimeout, "read_timeout"),
        (httpx.WriteTimeout, "write_timeout"),
        (httpx.PoolTimeout, "pool_timeout"),
        (httpx.ConnectError, "connection_error"),
        (httpx.TransportError, "transport_error"),
        (TimeoutError, "deadline"),
        (ProviderStreamError, "incomplete_response"),
        (ValueError, "invalid_response"),
    ):
        if isinstance(exc, kind):
            return name
    if http_status is not None and not 200 <= http_status < 300:
        return "http_error"
    return "provider_error"


def _provider_state(protocol: str, model: str, endpoint: str, layout: str) -> ProviderState:
    try:
        return ProviderState(protocol, model, endpoint, layout)
    except ValueError:
        raise ModelProviderError("Provider 私有状态超过安全上限") from None


def _reject_unreplayable_anthropic_arguments(
    raw_calls: Sequence[dict[str, Any]], blocks: list[Any], *, private_complete: bool = True,
) -> NoReturn:
    """仅校验失败回合，不生成可重放的 input，也不交付当前组的私有状态。"""
    if not private_complete:
        raise ModelProviderError("Provider 私有状态块未闭合")
    try:
        calls = [parse_native_call(raw.get("call_id"), raw.get("name"), raw.get("arguments"),
                                   allow_argument_errors=True, require_identity=True) for raw in raw_calls]
        validate_repair_batch(calls, [(raw.get("call_id"), raw.get("name")) for raw in raw_calls], require_identity=True)
        layout = anthropic_private_layout(blocks)
        if layout:
            _provider_state("anthropic_messages", "", "", layout)
    except ValueError:
        raise ModelProviderError("模型工具参数或原生身份/私有状态无效") from None
    raise UncommittableModelTurn("模型工具参数不是有效 JSON，本回合未执行", reason="unreplayable_arguments")


_MODEL_IDLE_TIMEOUT_FLOOR_SECONDS = 30
_RETRYABLE_MODEL_STATUS_CODES = frozenset({408, 425, 500, 502, 503, 504})
_MODEL_RETRY_DELAY_SECONDS = 0.25


def _network_idle_timeout_seconds(configured_timeout_seconds: int) -> int:
    """将配置值映射为网络空闲预算：保留 30 秒下限，并限制在 120 秒内。"""
    return min(
        120, max(_MODEL_IDLE_TIMEOUT_FLOOR_SECONDS, configured_timeout_seconds)
    )


def _stream_deadline_seconds(network_timeout_seconds: int) -> int:
    """为整轮流式响应计算独立总时限，最长 300 秒，不等同于网络空闲预算。"""
    return min(300, max(60, network_timeout_seconds * 4))


def _retryable_provider_exception(exc: BaseException) -> bool:
    if isinstance(exc, UncommittableModelTurn) and exc.reason == "unreplayable_arguments":
        return False
    if isinstance(exc, (httpx.TransportError, OSError)):
        return True
    if isinstance(exc, ModelProviderError):
        reason = str(exc or "")
        return any(
            marker in reason
            for marker in (
                "流在完成事件前中断",
                "API 未完整结束",
                "暂时不可用",
            )
        )
    return False


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    api_url: str
    model: str
    api_key: str = ""
    protocol: str = "auto"
    timeout_seconds: int = 30
    max_response_bytes: int = 2 * 1024 * 1024
    generation_profiles_json: str = ""
    telegram_channel: str = ""
    context_window_tokens: int = 128_000
    budget_profiles_json: str = ""
    reasoning_effort: str = ""

    def __post_init__(self) -> None:
        parse_generation_profiles(self.generation_profiles_json)
        parse_budget_profiles(self.budget_profiles_json)
        if self.reasoning_effort and self.reasoning_effort not in REASONING_EFFORTS:
            raise ValueError("reasoning_effort is invalid")
        if not (self.api_url or "").strip():
            raise ValueError("api_url is required")
        if not (self.model or "").strip() or len(self.model) > 200:
            raise ValueError("model is invalid")
        if not 2 <= self.timeout_seconds <= 120:
            raise ValueError("timeout_seconds out of range")

    @classmethod
    def from_config(cls) -> ProviderSettings:
        from app import config

        values = config.get_many((
            "AGENT_LLM_API_URL", "AGENT_LLM_MODEL", "AGENT_LLM_API_KEY",
            "AGENT_LLM_PROTOCOL", "AGENT_LLM_TIMEOUT_SECONDS", "AGENT_LLM_GENERATION_PROFILES",
            "AGENT_LLM_CONTEXT_WINDOW_TOKENS", "AGENT_LLM_BUDGET_PROFILES",
            "AGENT_LLM_REASONING_EFFORT",
        ))
        effort = (values.get("AGENT_LLM_REASONING_EFFORT", "") or "").strip().lower()
        try:
            timeout = int(values["AGENT_LLM_TIMEOUT_SECONDS"] or 30)
        except ValueError:
            timeout = 30
        try:
            context = int(values["AGENT_LLM_CONTEXT_WINDOW_TOKENS"] or 128_000)
        except ValueError:
            context = 128_000
        return cls(
            api_url=(values.get("AGENT_LLM_API_URL", "") or "").strip(),
            generation_profiles_json=values.get("AGENT_LLM_GENERATION_PROFILES", ""),
            model=(values.get("AGENT_LLM_MODEL", "") or "").strip(),
            api_key=(values.get("AGENT_LLM_API_KEY", "") or "").strip(),
            protocol=(values.get("AGENT_LLM_PROTOCOL", "auto") or "auto").strip(),
            timeout_seconds=max(2, min(120, timeout)),
            telegram_channel=provider_channel_fingerprint(values),
            context_window_tokens=max(16_384, min(2_000_000, context)),
            budget_profiles_json=values["AGENT_LLM_BUDGET_PROFILES"],
            # 手改配置的无效值按未设置处理，不让一个可选参数使 Agent 整体不可用。
            reasoning_effort=effort if effort in REASONING_EFFORTS else "",
        )


async def _bounded_response_bytes(chunks: AsyncIterator[bytes], maximum: int) -> AsyncIterator[bytes]:
    total = 0
    async for chunk in chunks:
        total += len(chunk)
        if total > maximum:
            raise ModelProviderError("Provider 响应超过安全上限")
        yield chunk


async def _iter_sse_json(chunks: AsyncIterator[bytes]) -> AsyncIterator[dict[str, Any]]:
    try:
        async for event in iter_provider_sse_events(chunks, max_event_bytes=128 * 1024):
            yield {"__done__": True} if event is None else event
    except ProviderStreamError as exc:
        raise ModelProviderError(str(exc)) from exc


def _append_tool_arguments(previous: Any, delta: Any) -> Any:
    """Keep invalid increments as False until validation or full done replacement."""
    if delta is None or delta == {}:
        return previous
    if isinstance(previous, dict):
        previous = json.dumps(previous, ensure_ascii=False, separators=(",", ":")) if previous else ""
    return (previous or "") + delta if isinstance(previous, (str, type(None))) and isinstance(delta, str) else False


def _usage_dict(value: Any, protocol: str) -> dict[str, int]:
    usage = extract_provider_usage(value, protocol)
    return usage.to_dict() if usage is not None else {}


@dataclass(slots=True)
class _ReasoningSummary:
    """只累计供应商公开摘要；完整段落才交给公开脱敏，避免跨片凭据泄漏。"""

    summary_id: str = ""
    text: str = ""
    completed: bool = False

    def finish(self, text: Any = None) -> ModelEvent | None:
        if self.completed:
            return None
        self.completed = True
        if isinstance(text, str) and text:
            self.text = text
        if self.text:
            return ModelEvent(ModelEventType.REASONING_SUMMARY_DELTA, text=self.text, summary_id=self.summary_id)
        return None


@dataclass(slots=True)
class _ProtocolState:
    protocol: str
    reasoning: ReasoningDeltaFilter = field(default_factory=ReasoningDeltaFilter)
    calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    emitted_calls: set[str] = field(default_factory=set)
    finish_reason: str = ""
    completed: bool = False
    native_blocks: dict[int, dict[str, Any]] = field(default_factory=dict)
    chat_reasoning: str | None = None
    invalid_private: bool = False
    repair_calls: bool = False
    open_private_blocks: set[int] = field(default_factory=set)
    text_filters: dict[int, ReasoningDeltaFilter] = field(default_factory=dict)
    reasoning_summaries: dict[tuple[str, int], _ReasoningSummary] = field(default_factory=dict)

    def summary_event(self, event: dict[str, Any], *, complete: bool) -> ModelEvent | None:
        index = event.get("summary_index", 0)
        text = event.get("text" if complete else "delta")
        if type(index) is not int or index < 0 or not isinstance(text, str):
            return None
        item_id = event.get("item_id")
        item_id = item_id if isinstance(item_id, str) else ""
        key = (self.call_key(index=event.get("output_index"), item_id=item_id), index)
        summary = self.reasoning_summaries.setdefault(
            key, _ReasoningSummary(summary_id=f"{item_id}:{index}" if item_id else ""),
        )
        if complete:
            return summary.finish(text)
        if not summary.completed:
            summary.text += text
        return None

    def anthropic_text(self, index: int, text: str | None = None) -> str:
        # Lookahead belongs to its native block, not the next text/tool block.
        # Both public deltas and replay layout consume the same filtered bytes.
        visible = (self.text_filters.pop(index).finalize() if text is None
                   else self.text_filters.setdefault(index, ReasoningDeltaFilter()).feed(text))
        block = self.native_blocks.setdefault(index, {"type": "text", "text": ""})
        block["text"] += visible
        return visible

    def text_event(self, value: Any, index: int | None = None) -> ModelEvent | None:
        if not isinstance(value, str) or not value:
            return None
        visible = self.reasoning.feed(value) if index is None else self.anthropic_text(index, value)
        return ModelEvent(ModelEventType.TEXT_DELTA, text=visible) if visible else None

    @staticmethod
    def call_key(*, index: Any = None, item_id: Any = None) -> str:
        if item_id not in {None, ""}:
            return f"id:{item_id}"
        return f"index:{index if index is not None else 0}"

    def decode_call(self, raw: dict[str, Any]):
        try:
            parsed = parse_native_call(raw.get("call_id"), raw.get("name"), raw.get("arguments"),
                                       allow_argument_errors=self.protocol in {"responses", "chat_completions"},
                                       require_identity=self.protocol == "anthropic_messages")
        except ToolArgumentsDecodeError as exc:
            if self.protocol == "anthropic_messages" and exc.reason == "invalid_json":
                _reject_unreplayable_anthropic_arguments(
                    list(self.calls.values()),
                    [self.native_blocks[index] for index in sorted(self.native_blocks)],
                    private_complete=not self.open_private_blocks,
                )
            failure = {"too_large": "模型工具参数过大", "invalid_json": "模型工具参数不是有效 JSON",
                       "not_object": "模型工具参数必须是对象"}[exc.reason]
        except ValueError:
            failure = "模型工具参数或原生身份无效"
        else:
            return parsed
        raise ModelProviderError(failure)

    def emit_call(self, key: str) -> ModelEvent | None:
        if key in self.emitted_calls:
            return None
        raw = self.calls.get(key)
        if not raw:
            return None
        parsed = self.decode_call(raw)
        if self.repair_calls or self.protocol == "anthropic_messages":
            call_id, name = parsed.call_id, parsed.name
        else:
            name = str(raw.get("name") or "").strip()
            call_id = str(raw.get("call_id") or raw.get("id") or key).strip()
        if not name:
            return None
        if parsed.argument_error and not self.completed:
            return None
        self.emitted_calls.add(key)
        return ModelEvent(
            ModelEventType.TOOL_CALL_COMPLETED,
            tool_call=ModelToolCall(call_id=call_id, name=name, arguments=parsed.arguments,
                                    argument_error=parsed.argument_error, raw_arguments=parsed.raw_arguments),
        )


def _consume_responses_event(state: _ProtocolState, event: dict[str, Any]) -> Iterator[ModelEvent]:
    event_type = str(event.get("type") or "")
    if event_type == "response.output_text.delta":
        if output := state.text_event(event.get("delta")):
            yield output
    elif event_type in {"response.reasoning_summary_text.delta", "response.reasoning_summary_text.done"}:
        if output := state.summary_event(event, complete=event_type.endswith(".done")):
            yield output
    elif event_type == "response.reasoning_summary_part.done":
        part = event.get("part")
        if isinstance(part, dict) and part.get("type") == "summary_text":
            if output := state.summary_event({**event, "text": part.get("text")}, complete=True):
                yield output
    elif event_type == "response.output_item.added":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call":
            key = state.call_key(index=event.get("output_index"), item_id=item.get("id"))
            conflict = key in state.calls
            state.calls[key] = {
                "identity_conflict": conflict,
                "id": item.get("id"),
                "call_id": item.get("call_id"),
                "name": item.get("name"),
                "arguments": item.get("arguments"),
            }
    elif event_type == "response.function_call_arguments.delta":
        key = state.call_key(index=event.get("output_index"), item_id=event.get("item_id"))
        raw = state.calls.setdefault(key, {})
        raw["arguments"] = _append_tool_arguments(raw.get("arguments"), event.get("delta"))
    elif event_type in {"response.function_call_arguments.done", "response.output_item.done"}:
        arguments_done = event_type == "response.function_call_arguments.done"
        item = event if arguments_done else event.get("item")
        if not arguments_done and isinstance(item, dict):
            state.native_blocks[int(event.get("output_index", 0))] = item
            if item.get("type") == "reasoning":
                for index, part in enumerate(item.get("summary") or []):
                    if isinstance(part, dict) and part.get("type") == "summary_text":
                        output = state.summary_event({"item_id": item.get("id"), "output_index": event.get("output_index"),
                            "summary_index": index, "text": part.get("text")}, complete=True)
                        if output:
                            yield output
        if isinstance(item, dict) and (arguments_done or item.get("type") == "function_call"):
            key = state.call_key(
                index=event.get("output_index"),
                item_id=event.get("item_id") if arguments_done else item.get("id"),
            )
            raw = state.calls.setdefault(key, {})
            if arguments_done:
                state.native_blocks.setdefault(int(event.get("output_index", 0)), {"type": "function_call", **raw})
            else:
                if any(raw.get(field) not in (None, "", item.get(field)) for field in ("id", "call_id", "name")):
                    raw["identity_conflict"] = True
                raw.update({field: item.get(field) for field in ("id", "call_id", "name")})
            arguments = item.get("arguments")
            # 完成帧的空占位不能抹掉参数增量；显式 {} 仍是完整参数。
            if arguments is not None and (not isinstance(arguments, str) or arguments.strip()):
                raw["arguments"] = arguments
            if output := state.emit_call(key):
                yield output
    elif event_type == "response.completed":
        response = event.get("response")
        if isinstance(response, dict) and response.get("status") == "completed":
            for summary in state.reasoning_summaries.values():
                if output := summary.finish():
                    yield output
        usage = _usage_dict(response, "responses") if isinstance(response, dict) else {}
        if usage:
            yield ModelEvent(ModelEventType.USAGE, usage=usage)
        if isinstance(response, dict):
            state.finish_reason = str(response.get("status") or "")
        state.completed = True
    elif event_type == "response.incomplete":
        raise UncommittableModelTurn("Responses API 未完整结束")
    elif event_type in {"response.failed", "error"}:
        raise ModelProviderError("Responses API 未完整结束")


def _consume_chat_completions_event(state: _ProtocolState, event: dict[str, Any]) -> Iterator[ModelEvent]:
    usage = _usage_dict(event, "chat_completions")
    if usage:
        yield ModelEvent(ModelEventType.USAGE, usage=usage)
    choices = event.get("choices")
    if not isinstance(choices, list):
        return
    for choice_index, choice in enumerate(choices):
        if not isinstance(choice, dict) or choice.get("index", choice_index) != 0:
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict):
            if delta.get("reasoning_content") is not None and not isinstance(delta["reasoning_content"], str):
                state.invalid_private = True
            if isinstance(delta.get("reasoning_content"), str):
                state.chat_reasoning = (state.chat_reasoning or "") + delta["reasoning_content"]
            if output := state.text_event(delta.get("content")):
                yield output
            for tool_call in delta.get("tool_calls") or ():
                if not isinstance(tool_call, dict):
                    continue
                key = state.call_key(index=tool_call.get("index"))
                raw = state.calls.setdefault(key, {})
                if tool_call.get("id"):
                    if raw.get("call_id") not in (None, "", tool_call["id"]):
                        raw["identity_conflict"] = True
                    raw["call_id"] = tool_call["id"]
                function = tool_call.get("function")
                if isinstance(function, dict):
                    if function.get("name"):
                        if not isinstance(function["name"], str):
                            raw["identity_conflict"] = True
                        raw["name"] = str(raw.get("name") or "") + str(function["name"])
                    raw["arguments"] = _append_tool_arguments(
                        raw.get("arguments"), function.get("arguments")
                    )
        reason = str(choice.get("finish_reason") or "")
        if reason:
            state.finish_reason = reason


def _consume_anthropic_event(state: _ProtocolState, event: dict[str, Any]) -> Iterator[ModelEvent]:
    event_type = str(event.get("type") or "")
    if event_type == "message_start":
        message = event.get("message")
        usage = _usage_dict(message, "anthropic_messages") if isinstance(message, dict) else {}
        if usage:
            yield ModelEvent(ModelEventType.USAGE, usage=usage)
    elif event_type == "content_block_start":
        block = event.get("content_block")
        if not isinstance(block, dict):
            return
        index = int(event.get("index", 0))
        if block.get("type") in {"thinking", "redacted_thinking"}:
            state.open_private_blocks.add(index)
            fields = ("type", "thinking", "signature") if block["type"] == "thinking" else ("type", "data")
            state.native_blocks[index] = {field: block[field] for field in fields if field in block}
        if block.get("type") == "text":
            if output := state.text_event(block.get("text"), index):
                yield output
        elif block.get("type") == "tool_use":
            state.native_blocks[index] = {"type": "tool_ref", "call_id": block.get("id")}
            key = state.call_key(index=index)
            if key in state.calls:
                raise ModelProviderError("模型工具原生身份冲突")
            state.calls[key] = {
                "call_id": block.get("id"),
                "name": block.get("name"),
                "arguments": block.get("input"),
            }
    elif event_type == "content_block_delta":
        delta = event.get("delta")
        if not isinstance(delta, dict):
            return
        index = int(event.get("index", 0))
        if delta.get("type") in {"thinking_delta", "signature_delta"}:
            field = "thinking" if delta["type"] == "thinking_delta" else "signature"
            block = state.native_blocks.get(index)
            if (block is None or block.get("type") != "thinking"
                    or index not in state.open_private_blocks or not isinstance(delta.get(field), str)):
                raise ModelProviderError("Provider 私有状态增量无效")
            if field in block and not isinstance(block[field], str):
                raise ModelProviderError("Provider 私有状态字段无效")
            block[field] = block.get(field, "") + delta[field]
        elif delta.get("type") == "text_delta":
            if output := state.text_event(delta.get("text"), index):
                yield output
        elif delta.get("type") == "input_json_delta":
            key = state.call_key(index=event.get("index"))
            raw = state.calls.setdefault(key, {})
            raw["arguments"] = _append_tool_arguments(raw.get("arguments"), delta.get("partial_json"))
    elif event_type == "content_block_stop":
        index = int(event.get("index", 0))
        if index in state.text_filters:
            tail = state.anthropic_text(index)
            if tail:
                yield ModelEvent(ModelEventType.TEXT_DELTA, text=tail)
        state.open_private_blocks.discard(index)
        if output := state.emit_call(state.call_key(index=event.get("index"))):
            yield output
    elif event_type == "message_delta":
        delta = event.get("delta")
        if isinstance(delta, dict) and delta.get("stop_reason"):
            state.finish_reason = str(delta["stop_reason"])
        usage = _usage_dict(event, "anthropic_messages")
        if usage:
            yield ModelEvent(ModelEventType.USAGE, usage=usage)
    elif event_type == "message_stop":
        state.completed = True


async def _finalize_protocol_events(state: _ProtocolState) -> AsyncIterator[ModelEvent]:
    if not state.completed:
        raise UncommittableModelTurn("Provider 流在完成事件前中断")
    try:
        state.finish_reason = provider_finish_reason(state.protocol, state.finish_reason)
    except ProviderStreamError as exc:
        raise UncommittableModelTurn(str(exc)) from exc
    if state.protocol in {"responses", "chat_completions", "anthropic_messages"}:
        # Validate the complete batch before emitting deferred calls. Checking
        # only previously emitted calls makes identity depend on stream order.
        parsed_calls = [state.decode_call(raw) for raw in state.calls.values()]
        state.repair_calls = any(call.argument_error for call in parsed_calls)
        try:
            if state.protocol == "anthropic_messages":
                validate_repair_batch(parsed_calls, [(raw.get("call_id"), raw.get("name"))
                                                     for raw in state.calls.values()], require_identity=True)
            if state.repair_calls:
                validate_repair_batch(parsed_calls, [(raw.get("call_id"), raw.get("name"))
                                                     for raw in state.calls.values()])
                for key, raw in state.calls.items():
                    for field in ("call_id", "name"):
                        # Ordinary Responses calls can already be emitted. A
                        # mixed turn cannot retroactively repair those events.
                        if key in state.emitted_calls and raw[field] != raw[field].strip():
                            raise ValueError("Previously normalized identity")
                    if raw.get("identity_conflict"):
                        raise ValueError("Conflicting native identity")
        except ValueError:
            raise ModelProviderError("模型工具参数或原生身份无效") from None
    for key in list(state.calls):
        output = state.emit_call(key)
        if output:
            yield output
    tail = state.reasoning.finalize()
    if tail:
        yield ModelEvent(ModelEventType.TEXT_DELTA, text=tail)
    for index in list(state.text_filters):
        tail = state.anthropic_text(index)
        if tail:
            yield ModelEvent(ModelEventType.TEXT_DELTA, text=tail)
    blocks = [state.native_blocks[index] for index in sorted(state.native_blocks)]
    if state.repair_calls and state.invalid_private:
        raise ModelProviderError("参数错误回合的私有状态无效")
    layout = ""
    if state.protocol == "anthropic_messages":
        if state.open_private_blocks:
            raise ModelProviderError("Provider 私有状态块未闭合")
        try:
            layout = anthropic_private_layout(blocks)
        except ValueError:
            raise ModelProviderError("Provider 私有状态格式无效") from None
    elif state.protocol == "responses":
        layout = responses_private_layout(blocks, required=state.repair_calls)
        if state.repair_calls:
            try:
                validate_repair_layout(blocks)
            except ValueError:
                raise ModelProviderError("参数错误回合的私有状态无效") from None
            refs = [block["call_id"] for block in blocks if block.get("type") == "function_call"]
            if sorted(refs) != sorted(raw["call_id"] for raw in state.calls.values()):
                raise ModelProviderError("参数错误回合缺少完整原生关联状态")
    elif state.chat_reasoning is not None:
        layout = json.dumps([{"type": "reasoning_content", "text": state.chat_reasoning}], ensure_ascii=False)
    elif state.repair_calls:
        layout = "[]"
    private_state = _provider_state(state.protocol, "", "", layout) if layout else None
    yield ModelEvent(ModelEventType.FINISH, finish_reason=state.finish_reason, provider_state=private_state)


async def iter_protocol_model_events(
    chunks: AsyncIterator[bytes], *, protocol: str, max_response_bytes: int = 2 * 1024 * 1024
) -> AsyncIterator[ModelEvent]:
    """把三个 Provider 的真实 SSE 统一为 Kernel ModelEvent。"""
    normalized = resolve_protocol(protocol)
    state = _ProtocolState(normalized)
    consumer = {
        "responses": _consume_responses_event,
        "chat_completions": _consume_chat_completions_event,
        "anthropic_messages": _consume_anthropic_event,
    }.get(normalized)
    async for event in _iter_sse_json(_bounded_response_bytes(chunks, max_response_bytes)):
        if event.get("__done__"):
            state.completed = normalized == "chat_completions"
            break
        if consumer is not None:
            for output in consumer(state, event):
                yield output
        if state.completed:
            break
    async for output in _finalize_protocol_events(state):
        yield output


def _history_for_protocol(
    protocol: str,
    system_prompt: str,
    messages: Sequence[ModelMessage],
    *, model: str = "", endpoint: str = "",
) -> list[dict[str, Any]]:
    def private_layout(message: ModelMessage) -> list[dict[str, Any]]:
        state = message.provider_state
        if (message.role != "assistant" or state is None
                or (state.protocol, state.model, state.endpoint) != (protocol, model, endpoint)):
            return []
        layout = state.layout()
        if protocol not in {"anthropic_messages", "responses"}:
            return layout
        # ModelMessage.content is the sole authority for ordinary text. Native
        # text copies are only segmentation hints, including in old SQLite
        # checkpoints. Receipt correction/redaction may have changed content
        # after the adapter captured this layout; never replay that stale copy.
        visible = "".join(block["text"] for block in layout if block.get("type") == "text")
        # Session normalizes only the outer whitespace of accumulated deltas;
        # this is not a factual correction and must not damage native ordering.
        if visible == message.content or visible.strip() == message.content:
            return layout
        if message.tool_calls or any(block.get("type") == "tool_ref" for block in layout):
            # Native tool turns may bind the entire assistant content. Do not
            # invent a signature, change the bound group, or silently fall back
            # to text-only history. Final non-tool answers can project ordinary
            # text independently while keeping the private blocks unchanged.
            raise ModelProviderError("原生工具回合正文已变更，无法安全续传；请新建会话重新核验")
        projected = []
        inserted = False
        for block in layout:
            if block.get("type") != "text":
                projected.append(block)  # Opaque/signature/tool references stay intact.
            elif not inserted:
                if message.content:
                    projected.append({"type": "text", "text": message.content})
                inserted = True
        if not inserted and message.content:
            projected.append({"type": "text", "text": message.content})
        return projected

    history: list[dict[str, Any]] = []
    if protocol != "anthropic_messages":
        history.append(native_text_message(protocol, "system", system_prompt))
    for message in messages:
        if message.completion_receipt_id:
            # 回执是运行时的新输入，不是模型此前的回答。统一投影为输入通知，
            # 否则解释预览后确认时，模型会继续回答旧问题，甚至否认已完成的写入。
            # 不伪造工具调用、不改持久化用户历史，也不授予后续写操作权限。
            message = replace(message, role="user", content=(
                "运行时执行结果通知（不是用户新指令或新增写入授权）：\n" + message.content
            ))
        if any(call.argument_error for call in message.tool_calls):
            state = message.provider_state
            if state is None or (state.protocol, state.model, state.endpoint) != (protocol, model, endpoint):
                raise ModelProviderError("参数错误回合的原生绑定不匹配，无法安全续传")
        layout = private_layout(message)
        calls = [native_tool_call(protocol, call.call_id, call.name, call.arguments, raw_arguments=call.raw_arguments)
                 for call in message.tool_calls]
        calls_by_id = dict(zip((call.call_id for call in message.tool_calls), calls))
        if message.role == "tool":
            result = native_tool_result(protocol, message.tool_call_id, message.content)
            item = {"role": "user", "content": [result]} if protocol == "anthropic_messages" else result
        elif protocol == "responses":
            if not layout:
                if message.content:
                    history.append(native_text_message(protocol, message.role, message.content))
                history.extend(calls)
            for block in layout:
                kind = block.get("type")
                if kind == "reasoning":
                    history.append(native_reasoning_item(block))
                elif kind == "tool_ref":
                    call = dict(calls_by_id[block["call_id"]])
                    if "id" in block:
                        call["id"] = block["id"]
                    history.append(call)
                elif kind == "text":
                    history.append(native_text_message(protocol, "assistant", block["text"]))
            continue
        elif protocol == "chat_completions":
            item: dict[str, Any] = {"role": message.role, "content": message.content or None}
            for block in layout:
                if block.get("type") == "reasoning_content" and isinstance(block.get("text"), str):
                    item["reasoning_content"] = block["text"]
            if calls:
                item["tool_calls"] = calls
        elif layout:
            blocks = [
                calls_by_id[block["call_id"]] if block.get("type") == "tool_ref" else block
                for block in layout
                if block.get("type") in {"tool_ref", "thinking", "redacted_thinking", "text"}
            ]
            item = {"role": "assistant", "content": blocks}
        elif message.role == "assistant" and calls:
            blocks = [{"type": "text", "text": message.content}] if message.content else []
            item = {"role": "assistant", "content": blocks + calls}
        else:
            item = {"role": message.role, "content": message.content}
        if (protocol == "anthropic_messages" and history
                and history[-1].get("role") == item.get("role") == "user"):
            previous = history[-1]
            previous_content = previous.get("content")
            current_content = item.get("content")
            if not isinstance(previous_content, list):
                previous_content = [{"type": "text", "text": str(previous_content or "")}]
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

    def budget_for_model(self, model: str, context: int, output: int) -> tuple[int, int, bool]:
        profiles = parse_budget_profiles(self.settings.budget_profiles_json)
        route = normalize_provider_location(self.settings.api_url).base_url
        cap = profiles.get((route, model or self.settings.model))
        if cap is None:
            return context, output, False
        context = min(context, cap[0])
        # Explicit reserve is headroom, not a provider reasoning-mode switch.
        return max(0, context - cap[2]), min(output, cap[1], context // 4), True

    async def stream(
        self,
        request: ModelRequest,
        *,
        cancellation: CancellationToken,
    ) -> AsyncIterator[ModelEvent]:
        location = normalize_provider_location(self.settings.api_url)
        configured = resolve_protocol(self.settings.protocol, self.settings.api_url)
        if configured == "auto":
            bound_protocols = {
                state.protocol
                for message in request.messages
                if message.role == "assistant"
                and (state := message.provider_state) is not None
                and state.protocol in {"responses", "chat_completions", "anthropic_messages"}
                and state.model == (request.model or self.settings.model)
                and state.endpoint == location.endpoint(state.protocol)
            }
            if len(bound_protocols) > 1:
                raise ModelProviderError("Provider 私有状态的协议绑定冲突")
            if bound_protocols:
                # 原生组只能由其原协议续传，不能剥离私有状态后重放工具历史。
                configured = next(iter(bound_protocols))
        network_idle_timeout = _network_idle_timeout_seconds(
            self.settings.timeout_seconds
        )
        client = self.client_factory(
            allowed_hosts={location.host},
            configured_origin=location.base_url,
            # 原生工具模型在首个 chunk 前可能需要较长推理，保留 30 秒
            # 空闲下限。POST 的重试由模型层按可见输出状态统一决定。
            timeout_seconds=network_idle_timeout,
            get_timeout_retries=0,
            max_response_bytes=self.settings.max_response_bytes,
            max_redirects=0,
            user_agent="MediaFlux-Agent-Kernel/1.0",
            pin_resolved_address=True,
        )
        emitted = False
        phase = "request"
        protocol = configured
        attempts = 0
        http_status: int | None = None
        last_fault: BaseException | None = None
        started = monotonic()
        deadline = _stream_deadline_seconds(network_idle_timeout)
        feedback_used = False
        last_error: Exception | None = None
        last_http_error: ModelProviderError | None = None
        try:
            # HTTP 客户端已经按配置限制连接/读写空闲时间；流式工具调用可能
            # 持续产生有效数据并超过该时长，不能再用同一个值把总响应硬切断。
            # 这里仅保留更宽松且有上限的整轮保险丝。
            async with asyncio.timeout(deadline):
                for protocol in protocol_attempts(configured):
                    can_fallback = False
                    cancellation.raise_if_cancelled()
                    definitions = native_tool_definitions(protocol, [dict(tool) for tool in request.tools])
                    history = _history_for_protocol(
                        protocol, request.system_prompt, request.messages,
                        model=request.model or self.settings.model, endpoint=location.endpoint(protocol),
                    )
                    body = native_tool_request_body(
                        protocol=protocol,
                        model=request.model or self.settings.model,
                        system_prompt=request.system_prompt,
                        history=history,
                        tools=definitions,
                        max_tokens=request.max_output_tokens,
                        allow_parallel_tool_calls=request.allow_parallel_tool_calls,
                        reasoning_effort=self.settings.reasoning_effort,
                        stable_system_chars=request.stable_system_chars,
                        generation_profile=resolve_generation_profile(
                            self.settings.generation_profiles_json, location.base_url,
                            request.model or self.settings.model, protocol,
                        ),
                        stream=True,
                    )
                    # 复用已显式开启的 Responses 推理配置；不向普通模型或
                    # Chat/Anthropic 请求强加摘要参数，也不增加额外总结请求。
                    if protocol == "responses" and isinstance(body.get("reasoning"), dict):
                        body["reasoning"]["summary"] = "auto"
                    context, output, bounded = self.budget_for_model(
                        request.model, self.settings.context_window_tokens, request.max_output_tokens,
                    )
                    if bounded and (
                        request.max_output_tokens > output
                        or conservative_tokens(body) + request.max_output_tokens + 512 > context
                    ):
                        raise ModelProviderError("序列化请求超过模型上下文预算，未发送请求")
                    read_failure: BaseException | None = None
                    for attempt in range(2):
                        # 等待只发生在前次 response 清理和异常处理全部退出后；
                        # 取消/总 deadline 不得被清理异常覆盖，也不继承原异常图。
                        if attempt:
                            phase = "retry_wait"
                            await asyncio.sleep(_MODEL_RETRY_DELAY_SECONDS)
                        cancellation.raise_if_cancelled()
                        phase = "request"
                        http_status = None
                        last_fault = None
                        attempts += 1
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
                                phase = "response"
                                http_status = response.status_code
                                try:
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
                                        last_error.__cause__ = last_http_error
                                        last_http_error = last_error
                                        if (
                                            response.status_code
                                            in _RETRYABLE_MODEL_STATUS_CODES
                                            and attempt == 0
                                        ):
                                            continue
                                        fallback_status = (
                                            not feedback_used and configured == "auto" and protocol == "responses"
                                            and response.status_code in PROTOCOL_FALLBACK_STATUS_CODES
                                        )
                                        if response.status_code in {400, 413, 422} or fallback_status:
                                            cap = min(self.settings.max_response_bytes, PROTOCOL_FALLBACK_MAX_BYTES, 65_536)
                                            raw_error = bytearray()
                                            try:
                                                declared = response.headers.get("content-length")
                                                if declared is not None and int(declared) > cap:
                                                    raise ModelProviderError("Provider 错误响应超过安全上限")
                                                async for chunk in _bounded_response_bytes(response.aiter_bytes(), cap):
                                                    raw_error.extend(chunk)
                                                if is_window_feedback(response.status_code, bytes(raw_error)):
                                                    # Share the existing two-attempt ceiling, never
                                                    # add another loop or change protocol after rejection.
                                                    can_fallback = False
                                                    reduced = reduced_output_body(body, context, output) if attempt == 0 else None
                                                    if reduced is not None:
                                                        feedback_used = True
                                                        body = reduced
                                                        continue
                                                    read_failure = ModelProviderError("Provider 上下文预算不足，无法安全重试")
                                                    read_failure.__cause__ = last_error
                                                elif fallback_status:
                                                    can_fallback = is_protocol_fallback_error(
                                                        response.status_code, raw_error.decode("utf-8"), protocol=protocol,
                                                    )
                                            except Exception as exc:
                                                last_fault = exc
                                                # 延后终态到 context 退出后，清理失败不得覆盖它或重发。
                                                can_fallback = False
                                                read_failure = ModelProviderError("Provider 错误响应读取失败")
                                                read_failure.__cause__ = last_error
                                        break
                                    content_type = (response.headers.get("content-type") or "").lower()
                                    if "text/event-stream" in content_type:
                                        async for event in iter_protocol_model_events(
                                            response.aiter_bytes(), protocol=protocol,
                                            max_response_bytes=self.settings.max_response_bytes,
                                        ):
                                            cancellation.raise_if_cancelled()
                                            if event.provider_state is not None:
                                                event = replace(event, provider_state=replace(event.provider_state,
                                                    model=request.model or self.settings.model, endpoint=location.endpoint(protocol)))
                                            emitted = True
                                            yield event
                                        return
                                    raw = bytearray()
                                    async for chunk in _bounded_response_bytes(response.aiter_bytes(), self.settings.max_response_bytes):
                                        raw.extend(chunk)
                                    try:
                                        envelope = json.loads(raw.decode("utf-8"))
                                        try:
                                            turn = parse_native_tool_turn(envelope, protocol, allow_argument_errors=True)
                                        except ToolArgumentsDecodeError as exc:
                                            if protocol == "anthropic_messages" and exc.reason == "invalid_json":
                                                blocks = envelope["content"]
                                                _reject_unreplayable_anthropic_arguments(
                                                    [{"call_id": block.get("id"), "name": block.get("name"), "arguments": block.get("input")}
                                                     for block in blocks if isinstance(block, dict) and block.get("type") == "tool_use"], blocks,
                                                )
                                            last_fault = exc
                                            last_error = ModelProviderError("Provider 返回格式无效")
                                            last_error.__cause__ = last_http_error
                                            break
                                    except ProviderStreamError as exc:
                                        raise UncommittableModelTurn(str(exc)) from exc
                                    except ValueError as exc:
                                        last_fault = exc
                                        last_error = ModelProviderError(
                                            "Provider 返回格式无效"
                                        )
                                        last_error.__cause__ = last_http_error
                                        if emitted:
                                            raise last_error from exc
                                        break
                                    if protocol == "responses":
                                        summary_state = _ProtocolState(protocol)
                                        for index, item in enumerate(envelope.get("output", [])):
                                            if isinstance(item, dict) and item.get("type") == "reasoning":
                                                for event in _consume_responses_event(summary_state, {
                                                    "type": "response.output_item.done", "output_index": index, "item": item,
                                                }):
                                                    emitted = True
                                                    yield event
                                    if turn.text:
                                        emitted = True
                                        yield ModelEvent(ModelEventType.TEXT_DELTA, text=turn.text)
                                    for call in turn.tool_calls:
                                        emitted = True
                                        yield ModelEvent(
                                            ModelEventType.TOOL_CALL_COMPLETED,
                                            tool_call=ModelToolCall(
                                                call_id=call.call_id,
                                                name=call.name,
                                                arguments=call.arguments,
                                                argument_error=call.argument_error,
                                                raw_arguments=call.raw_arguments,
                                            ),
                                        )
                                    if turn.usage is not None:
                                        emitted = True
                                        yield ModelEvent(
                                            ModelEventType.USAGE,
                                            usage=turn.usage.to_dict(),
                                        )
                                    emitted = True
                                    yield ModelEvent(
                                        ModelEventType.FINISH, finish_reason=turn.finish_reason,
                                        provider_state=_provider_state(protocol, request.model or self.settings.model, location.endpoint(protocol), turn.private_layout_json)
                                        if turn.private_layout_json else None,
                                    )
                                    return
                                except asyncio.CancelledError:
                                    # 所有响应读取/输出共用终态保护，先退出清理再抛安全原因。
                                    can_fallback = False
                                    read_failure = asyncio.CancelledError()
                                    read_failure.__cause__ = last_http_error
                                    break
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            can_fallback = False
                            if read_failure is not None:
                                break
                            last_fault = exc
                            last_error = exc
                            if last_http_error is not None:
                                if isinstance(exc, UncommittableModelTurn):
                                    last_error = UncommittableModelTurn(str(exc), reason=exc.reason)
                                else:
                                    category = next((kind.__name__ for kind in (
                                        httpx.TransportError, OSError, ValueError,
                                    ) if isinstance(exc, kind)), "Exception")
                                    last_error = ModelProviderError(f"Provider 请求失败（传输/响应处理：{category}）")
                                last_error.__cause__ = last_http_error
                            if emitted:
                                break
                            if attempt == 0 and _retryable_provider_exception(exc):
                                continue
                            break
                    if read_failure is not None:
                        raise read_failure
                    if not can_fallback:
                        break
        except TimeoutError:
            last_fault = TimeoutError()
            last_error = ModelProviderError("Provider 请求超时")
            last_error.__cause__ = TimeoutError()
            last_error.__cause__.__cause__ = last_http_error
        except ModelProviderError as exc:
            # 已有协议/读取失败保留原类型和原因，补充同一份安全诊断。
            # 取消及请求构建中的内部编程错误仍按原异常传播。
            last_error = exc
            last_fault = last_fault or exc
        finally:
            await client.aclose()
        if isinstance(last_error, ModelProviderError):
            error = last_error
        else:
            error = ModelProviderError("Provider 暂时不可用")
            error.__cause__ = last_error
        error.failure = ProviderFailure(
            kind=_failure_kind(last_fault or last_error, http_status),
            phase=phase, protocol=protocol, attempts=attempts,
            http_status=http_status, emitted=emitted,
            elapsed_ms=max(0, round((monotonic() - started) * 1000)),
            idle_timeout_seconds=network_idle_timeout, deadline_seconds=deadline,
        )
        raise error
