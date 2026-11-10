"""LLM Provider URL、协议、认证与结构化输出适配。"""
from __future__ import annotations

import codecs
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal
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
# 三种协议共同支持的推理强度；空值表示不发送，沿用服务商默认。
REASONING_EFFORTS = ("low", "medium", "high")
_REASONING_MODEL_PREFIXES = (
    "o1",
    "o3",
    "o4",
    "deepseek-reasoner",
    "deepseek-r1",
    "qwq",
)
_THINK_BLOCK_RE = re.compile(r"(?is)<think\b[^>]*>.*?</think\s*>")
PROTOCOL_FALLBACK_MAX_BYTES = 8192
PROTOCOL_FALLBACK_STATUS_CODES = frozenset({400, 404, 405, 415, 422, 501})
_PROTOCOL_FALLBACK_MESSAGES = frozenset({
    "unknown endpoint: /v1/responses",
    "unsupported endpoint: /v1/responses",
    "cannot post /v1/responses",
    "cannot post /responses",
    "not found: /v1/responses",
    "unknown parameter 'input'",
    'unknown parameter "input"',
    "unrecognized request argument supplied: input",
    "responses api is not supported",
})
_PROTOCOL_FALLBACK_CODES = frozenset({
    "invalid_request_error", "invalid_request", "unsupported_endpoint",
    "unknown_endpoint", "not_found_error",
})
_NON_FALLBACK_MARKERS = (
    "api key",
    "authentication",
    "authorization",
    "billing",
    "credit",
    "quota",
    "insufficient",
    "model not found",
    "content policy",
)


@dataclass(frozen=True, slots=True)
class NativeToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any] | None
    argument_error: Literal["invalid_json"] | None = None
    raw_arguments: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Provider 明确返回的 token 用量；缺失时保持 ``None``。"""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int = 0
    reasoning_tokens: int = 0

    def __add__(self, other: object) -> ProviderUsage:
        if not isinstance(other, ProviderUsage):
            return self
        return ProviderUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


@dataclass(frozen=True, slots=True)
class NativeToolTurn:
    text: str
    tool_calls: tuple[NativeToolCall, ...]
    finish_reason: str
    assistant_entry: object | None = None
    usage: ProviderUsage | None = None
    private_layout_json: str = field(default="", repr=False)


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
    """依据显式端点或官方 Anthropic 主机推断协议。"""
    try:
        parsed = urlsplit(str(raw_url or "").strip())
        path = parsed.path.rstrip("/").lower()
    except ValueError:
        return None
    if path.endswith("/chat/completions"):
        return "chat_completions"
    if path.endswith("/responses"):
        return "responses"
    if path.endswith("/messages"):
        return "anthropic_messages"
    host = (parsed.hostname or "").rstrip(".").lower()
    if host == "api.anthropic.com" or host.endswith(".anthropic.com"):
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


def is_reasoning_model(model: object) -> bool:
    """保守识别已知推理模型，避免发送其不接受的采样参数。"""
    model_id = str(model or "").strip().lower().rsplit("/", 1)[-1]
    return any(model_id.startswith(prefix) for prefix in _REASONING_MODEL_PREFIXES)


def is_protocol_fallback_error(
    status_code: int,
    response_text: object = "",
    *,
    protocol: object = "",
) -> bool:
    """仅完整、有界的明确协议错误授权回退；未知措辞保守终止。"""
    if (status_code not in PROTOCOL_FALLBACK_STATUS_CODES
            or normalize_protocol(protocol) != "responses"
            or not isinstance(response_text, str)):
        return False
    try:
        if len(response_text.encode("utf-8")) > PROTOCOL_FALLBACK_MAX_BYTES:
            return False
    except UnicodeError:
        return False
    text = response_text.strip().casefold()
    if any(marker in text for marker in _NON_FALLBACK_MARKERS):
        return False
    if text.startswith(("{", "[")):
        def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            fields = dict(pairs)
            if len(fields) != len(pairs):
                raise ValueError("ambiguous error fields")
            return fields

        try:
            envelope = json.loads(text, object_pairs_hook=unique_fields)
        except ValueError:
            return False
        if not isinstance(envelope, dict) or envelope.keys() - {"error", "code", "type"}:
            return False
        error = envelope.get("error")
        fields = [envelope]
        if isinstance(error, dict):
            if error.keys() - {"message", "code", "type"}:
                return False
            fields.append(error)
            error = error.get("message")
        for container in fields:
            for field in ("code", "type"):
                value = container.get(field, "")
                if not isinstance(value, str) or (value and value.casefold() not in _PROTOCOL_FALLBACK_CODES):
                    return False
        if not isinstance(error, str):
            return False
        text = error.strip()
    return text.removesuffix(".") in _PROTOCOL_FALLBACK_MESSAGES


def strip_reasoning_markup(value: object) -> str:
    """移除部分兼容网关塞进正文的 ``<think>`` 内部思考。"""
    text = str(value or "")
    while True:
        cleaned = _THINK_BLOCK_RE.sub("", text)
        if cleaned == text:
            break
        text = cleaned
    opening = re.search(r"(?is)<think\b", text)
    if opening:
        text = text[: opening.start()]
    return re.sub(r"(?is)</think\s*>", "", text)


def parse_generation_profiles(raw: str) -> tuple[dict[str, str], ...]:
    """Validate locally without retaining raw configuration in exception chains."""
    try:
        return _parse_generation_profiles(raw)
    except (ValueError, TypeError, RecursionError):
        pass
    raise ValueError("模型参数档案格式无效或超过安全上限")


def _parse_generation_profiles(raw: str) -> tuple[dict[str, str], ...]:
    """Bounded explicit route/model/protocol profiles; no provider discovery."""
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 65_536:
        raise ValueError("模型参数档案格式无效或过大")
    if not raw.strip():
        return ()

    def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("模型参数档案字段重复")
        return result

    rows = json.loads(raw, object_pairs_hook=unique_fields)
    if not isinstance(rows, list) or len(rows) > 64:
        raise ValueError("模型参数档案必须是最多 64 项的数组")
    from app.agent.model_catalog import normalize_provider_model_id

    keys = {"base_url", "model", "protocol", "token_parameter", "temperature"}
    parsed: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        if (not isinstance(row, dict) or row.keys() != keys
                or not all(isinstance(value, str) for value in row.values())):
            raise ValueError("模型参数档案字段无效")
        for value in row.values():
            value.encode("utf-8")
        if normalize_provider_model_id(row["model"]) != row["model"] or not row["model"]:
            raise ValueError("模型参数档案模型 ID 无效")
        base_url = normalize_provider_location(row["base_url"]).base_url
        protocol = row["protocol"]
        allowed = {
            "responses": {"max_output_tokens"},
            "chat_completions": {"max_tokens", "max_completion_tokens"},
            "anthropic_messages": {"max_tokens"},
        }
        if (row["token_parameter"] not in allowed.get(protocol, set())
                or row["temperature"] not in {"omit", "zero"}):
            raise ValueError("模型参数档案协议或生成参数无效")
        identity = (base_url, row["model"], protocol)
        if identity in seen:
            raise ValueError("模型参数档案路由身份重复")
        seen.add(identity)
        parsed.append({**row, "base_url": base_url})
    return tuple(parsed)


def resolve_generation_profile(
    raw: str, base_url: str, model: str, protocol: str,
) -> Mapping[str, str] | None:
    """Match an already-normalized route; never strip its endpoint suffix again."""
    profiles = parse_generation_profiles(raw)
    if not profiles:
        return None
    identity = (base_url, model, protocol)
    return next((row for row in profiles
                 if (row["base_url"], row["model"], row["protocol"]) == identity), None)


def _generation_parameters(
    *, protocol: str, model: str, max_tokens: int,
    generation_profile: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    normalized = normalize_protocol(protocol)
    parameters: dict[str, Any]
    if generation_profile is not None:
        if generation_profile["protocol"] != normalized:
            raise ValueError("模型参数档案协议不匹配")
        parameters = {generation_profile["token_parameter"]: max_tokens}
        if generation_profile["temperature"] == "zero":
            parameters["temperature"] = 0
        return parameters
    if normalized == "responses":
        parameters = {"max_output_tokens": max_tokens}
    elif normalized == "chat_completions" and is_reasoning_model(model):
        parameters = {"max_completion_tokens": max_tokens}
    else:
        parameters = {"max_tokens": max_tokens}
    if not is_reasoning_model(model):
        parameters["temperature"] = 0
    return parameters


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
    max_length: int = 2048,
) -> ProviderLocation:
    """规范化管理员配置的模型端点；允许本机、内网与公网 HTTP(S)。"""
    raw = str(raw_url or "").strip().rstrip("/")
    if not raw or len(raw) > max_length or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError("AI Base URL 格式无效")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("AI Base URL 格式无效") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise ValueError("AI Base URL 必须是无内嵌凭据、无查询参数和片段的 HTTP 或 HTTPS 地址")
    host = parsed.hostname.rstrip(".").lower()
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
    generation_profile: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if protocol == "responses":
        body = {
            "model": model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "input_text", "text": user_content}]},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        body.update(_generation_parameters(
            protocol=protocol, model=model, max_tokens=max_tokens,
            generation_profile=generation_profile,
        ))
        return body
    if protocol == "anthropic_messages":
        body = {
            "model": model,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": schema,
                }
            },
        }
        body.update(_generation_parameters(
            protocol=protocol, model=model, max_tokens=max_tokens,
            generation_profile=generation_profile,
        ))
        return body
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
    }
    body.update(_generation_parameters(
        protocol=protocol, model=model, max_tokens=max_tokens,
        generation_profile=generation_profile,
    ))
    return body


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
        return strip_reasoning_markup(
            _text_from_blocks(envelope.get("content"), allowed_types={"text"})
        )
    if protocol == "chat_completions":
        content = envelope["choices"][0]["message"]["content"]
        if isinstance(content, str):
            return strip_reasoning_markup(content)
        return strip_reasoning_markup(
            _text_from_blocks(content, allowed_types={"text", "output_text"})
        )
    direct = envelope.get("output_text")
    if isinstance(direct, str) and direct:
        return strip_reasoning_markup(direct)
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
        return strip_reasoning_markup("".join(parts))
    raise ValueError("AI 响应格式无效")


def _usage_int(value: Any, *, maximum: int = 4_000_000) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        return None
    return value


def extract_provider_usage(
    envelope: object, protocol: str
) -> ProviderUsage | None:
    """解析三类非流式协议的 usage；缺失或畸形时不伪造零值。"""
    if not isinstance(envelope, dict) or not isinstance(envelope.get("usage"), dict):
        return None
    raw = envelope["usage"]
    normalized = normalize_protocol(protocol)
    if normalized == "anthropic_messages":
        prompt = _usage_int(raw.get("input_tokens"), maximum=2_000_000)
        completion = _usage_int(raw.get("output_tokens"), maximum=2_000_000)
        cached = _usage_int(raw.get("cache_read_input_tokens", 0), maximum=2_000_000)
        reasoning = 0
        total = _usage_int(raw.get("total_tokens"))
    elif normalized == "responses":
        prompt = _usage_int(raw.get("input_tokens"), maximum=2_000_000)
        completion = _usage_int(raw.get("output_tokens"), maximum=2_000_000)
        # Responses 协议字段为复数 *_tokens_details；单数写法会让缓存与推理计数恒为 0。
        input_details = raw.get("input_tokens_details")
        output_details = raw.get("output_tokens_details")
        cached = _usage_int(
            input_details.get("cached_tokens", 0) if isinstance(input_details, dict) else 0,
            maximum=2_000_000,
        )
        reasoning = _usage_int(
            output_details.get("reasoning_tokens", 0) if isinstance(output_details, dict) else 0,
            maximum=2_000_000,
        )
        total = _usage_int(raw.get("total_tokens"))
    else:
        prompt = _usage_int(raw.get("prompt_tokens"), maximum=2_000_000)
        completion = _usage_int(raw.get("completion_tokens"), maximum=2_000_000)
        prompt_details = raw.get("prompt_tokens_details")
        completion_details = raw.get("completion_tokens_details")
        cached = _usage_int(
            prompt_details.get("cached_tokens", 0) if isinstance(prompt_details, dict) else 0,
            maximum=2_000_000,
        )
        reasoning = _usage_int(
            completion_details.get("reasoning_tokens", 0)
            if isinstance(completion_details, dict) else 0,
            maximum=2_000_000,
        )
        total = _usage_int(raw.get("total_tokens"))
    if prompt is None or completion is None or cached is None or reasoning is None:
        return None
    expected_total = prompt + completion
    if total is None:
        total = expected_total
    if total < expected_total:
        return None
    return ProviderUsage(prompt, completion, total, cached, reasoning)


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
        description = str(capability.get("description") or "").strip()
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
            function: dict[str, Any] = {
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
    history = [native_text_message(protocol, "user", user_content)]
    if protocol in {"responses", "chat_completions"}:
        history.insert(0, native_text_message(protocol, "system", system_prompt))
    return history


def native_tool_request_body(
    *,
    protocol: str,
    model: str,
    system_prompt: str,
    history: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int,
    generation_profile: Mapping[str, str] | None = None,
    stream: bool = False,
    allow_parallel_tool_calls: bool = False,
    reasoning_effort: str = "",
    stable_system_chars: int = 0,
) -> dict[str, Any]:
    if reasoning_effort and reasoning_effort not in REASONING_EFFORTS:
        raise ValueError("推理强度无效")
    body: dict[str, Any]
    if protocol == "responses":
        body = {
            "model": model,
            "input": history,
            # Explicit history is replayed across requests/restarts; request the
            # native opaque state rather than depending on provider-side IDs.
            "include": ["reasoning.encrypted_content"],
        }
        if tools:
            body.update({
                "tools": tools,
                "tool_choice": "auto",
                "parallel_tool_calls": allow_parallel_tool_calls is True,
            })
    elif protocol == "anthropic_messages":
        body = {
            "model": model,
            "system": _anthropic_cached_system(system_prompt, stable_system_chars),
            "messages": _anthropic_cached_history(history),
        }
        if tools:
            body.update({
                "tools": tools,
                "tool_choice": {
                    "type": "auto",
                    "disable_parallel_tool_use": allow_parallel_tool_calls is not True,
                },
            })
    else:
        body = {
            "model": model,
            "messages": history,
        }
        if tools:
            body.update({
                "tools": tools,
                "tool_choice": "auto",
                "parallel_tool_calls": allow_parallel_tool_calls is True,
            })
    body.update(_generation_parameters(
        protocol=protocol, model=model, max_tokens=max_tokens,
        generation_profile=generation_profile,
    ))
    if reasoning_effort:
        # 推理模式不接受采样参数；thinking/reasoning 计入输出额度。
        body.pop("temperature", None)
        if protocol == "responses":
            body["reasoning"] = {"effort": reasoning_effort}
        elif protocol == "anthropic_messages":
            body["thinking"] = {"type": "adaptive"}
            body["output_config"] = {"effort": reasoning_effort}
        else:
            body["reasoning_effort"] = reasoning_effort
    if stream:
        body["stream"] = True
    return body


def _anthropic_cached_system(system_prompt: str, stable_chars: int) -> str | list[dict[str, Any]]:
    """稳定头部单独设缓存断点；易变尾部变化时，工具定义与头部仍可命中。"""
    head = system_prompt[:stable_chars] if 0 < stable_chars < len(system_prompt) else system_prompt
    if not head.strip():
        return system_prompt
    blocks: list[dict[str, Any]] = [{"type": "text", "text": head, "cache_control": {"type": "ephemeral"}}]
    tail = system_prompt[len(head):]
    if tail.strip():
        blocks.append({"type": "text", "text": tail})
    return blocks


def _anthropic_cached_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """滚动断点放在末条用户输入，下一轮工具循环按前缀复用整段历史。"""
    if not history or history[-1].get("role") != "user":
        return history
    content = history[-1].get("content")
    if isinstance(content, str) and content.strip():
        blocks: list[dict[str, Any]] = [{"type": "text", "text": content}]
    elif isinstance(content, list) and content and all(isinstance(block, dict) for block in content):
        blocks = [dict(block) for block in content]
    else:
        return history
    if blocks[-1].get("type") not in {"text", "tool_result"}:
        return history
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return [*history[:-1], {**history[-1], "content": blocks}]


def text_stream_request_body(
    *,
    protocol: str,
    model: str,
    system_prompt: str,
    user_content: str,
    max_tokens: int,
    generation_profile: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """构造不携带工具的纯文本流式请求。"""
    normalized = normalize_protocol(protocol)
    if normalized == "responses":
        body = {
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
            "stream": True,
        }
        body.update(_generation_parameters(
            protocol=normalized, model=model, max_tokens=max_tokens,
            generation_profile=generation_profile,
        ))
        return body
    if normalized == "anthropic_messages":
        body = {
            "model": model,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
            "stream": True,
        }
        body.update(_generation_parameters(
            protocol=normalized, model=model, max_tokens=max_tokens,
            generation_profile=generation_profile,
        ))
        return body
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "stream": True,
    }
    body.update(_generation_parameters(
        protocol=normalized, model=model, max_tokens=max_tokens,
        generation_profile=generation_profile,
    ))
    return body


class ProviderStreamError(ValueError):
    """Provider 响应不完整、越界或返回错误。"""


def provider_finish_reason(protocol: str, reason: object) -> str:
    """流式和 JSON 回合共用的终态校验；缺失结束原因不冒充正常 stop。"""
    allowed = {
        "chat_completions": {"stop", "tool_calls"},
        "responses": {"completed"},
        "anthropic_messages": {"end_turn", "stop_sequence", "tool_use"},
    }
    if not isinstance(reason, str) or reason not in allowed.get(protocol, set()):
        raise ProviderStreamError("Provider 回复未完整结束：结束原因缺失或非成功终态")
    return "stop" if protocol == "responses" else reason


class ReasoningDeltaFilter:
    """跨 SSE 分片移除兼容模型混入正文的 ``<think>`` 段。"""

    def __init__(self) -> None:
        self._pending = ""
        self._inside = False

    @staticmethod
    def _ambiguous_suffix_length(value: str, marker: str) -> int:
        lowered = value.casefold()
        target = marker.casefold()
        for length in range(min(len(lowered), len(target) - 1), 0, -1):
            if lowered.endswith(target[:length]):
                return length
        return 0

    def feed(self, value: str) -> str:
        self._pending += value
        visible: list[str] = []
        while self._pending:
            lowered = self._pending.casefold()
            if self._inside:
                close_at = lowered.find("</think")
                if close_at < 0:
                    keep = self._ambiguous_suffix_length(self._pending, "</think")
                    self._pending = self._pending[-keep:] if keep else ""
                    return "".join(visible)
                close_end = self._pending.find(">", close_at)
                if close_end < 0:
                    self._pending = self._pending[close_at:]
                    return "".join(visible)
                self._pending = self._pending[close_end + 1:]
                self._inside = False
                continue

            open_at = lowered.find("<think")
            if open_at >= 0:
                visible.append(self._pending[:open_at])
                open_end = self._pending.find(">", open_at)
                if open_end < 0:
                    self._pending = self._pending[open_at:]
                    return "".join(visible)
                self._pending = self._pending[open_end + 1:]
                self._inside = True
                continue

            keep = self._ambiguous_suffix_length(self._pending, "<think")
            if keep:
                visible.append(self._pending[:-keep])
                self._pending = self._pending[-keep:]
            else:
                visible.append(self._pending)
                self._pending = ""
            return "".join(visible)
        return "".join(visible)

    def finalize(self) -> str:
        if self._inside:
            self._pending = ""
            return ""
        visible = strip_reasoning_markup(self._pending)
        self._pending = ""
        return visible


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


async def iter_provider_sse_events(
    chunks: AsyncIterator[bytes], *, max_event_bytes: int = 64 * 1024
) -> AsyncIterator[dict[str, Any] | None]:
    """Shared SSE framing/JSON validation; protocol completion stays with callers."""
    async for data in _iter_sse_data(chunks, max_event_bytes=max_event_bytes):
        if data.strip() == "[DONE]":
            yield None
            return
        try:
            event = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ProviderStreamError("Provider SSE 数据不是有效 JSON") from exc
        if not isinstance(event, dict):
            raise ProviderStreamError("Provider SSE 数据格式无效")
        if event.get("type") == "error" or isinstance(event.get("error"), dict):
            raise ProviderStreamError("Provider 返回流式错误")
        yield event


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
    reasoning_filter = ReasoningDeltaFilter()

    async for event in iter_provider_sse_events(chunks, max_event_bytes=max_event_bytes):
        if event is None:
            if normalized != "chat_completions":
                raise ProviderStreamError("Provider SSE 数据不是有效 JSON")
            completed = True
            break

        if normalized == "responses":
            event_type = str(event.get("type") or "")
            if event_type == "response.output_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    visible = reasoning_filter.feed(delta)
                    if visible:
                        yield visible
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
                        visible = reasoning_filter.feed(content)
                        if visible:
                            yield visible
                finish_reason = choice.get("finish_reason")
                if finish_reason not in {None, "", "stop", "end_turn"}:
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
                    visible = reasoning_filter.feed(text)
                    if visible:
                        yield visible
            elif delta.get("type") == "input_json_delta":
                raise ProviderStreamError("纯文本流意外包含工具参数")
        elif event_type == "message_delta":
            delta = event.get("delta")
            stop_reason = delta.get("stop_reason") if isinstance(delta, dict) else None
            if stop_reason not in {None, "", "end_turn", "stop_sequence"}:
                raise ProviderStreamError("Anthropic Messages 未完整结束")
        elif event_type == "message_stop":
            completed = True
            break

    if normalized == "anthropic_messages" and not completed:
        completed = False
    if not completed:
        raise ProviderStreamError("Provider 流在完成事件前中断")
    tail = reasoning_filter.finalize()
    if tail:
        yield tail


_MAX_TOOL_ARGUMENTS_CHARS = 32_768


class ToolArgumentsDecodeError(ValueError):
    """工具参数无法解码；reason 供协议适配层保留既有错误语义。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("AI 工具参数格式无效")


def parse_tool_arguments(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ToolArgumentsDecodeError("invalid_json") from exc
    elif value is None or isinstance(value, str):
        text = (value or "").strip() or "{}"
    else:
        raise ToolArgumentsDecodeError("not_object")
    if len(text) > _MAX_TOOL_ARGUMENTS_CHARS:
        raise ToolArgumentsDecodeError("too_large")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolArgumentsDecodeError("invalid_json") from exc
    if not isinstance(parsed, dict):
        raise ToolArgumentsDecodeError("not_object")
    return parsed


def parse_native_call(
    call_id: object, name: object, value: object, *, allow_argument_errors: bool = False,
    require_identity: bool = False,
) -> NativeToolCall:
    try:
        arguments = parse_tool_arguments(value)
    except ToolArgumentsDecodeError as exc:
        if not (allow_argument_errors and exc.reason == "invalid_json"
                and isinstance(value, str) and len(value.encode("utf-8")) <= _MAX_TOOL_ARGUMENTS_CHARS
                and isinstance(call_id, str) and call_id.strip()
                and isinstance(name, str) and name.strip()):
            raise
        return NativeToolCall(call_id, name, None, "invalid_json", value)
    if require_identity:
        call_id, name = _require_native_identity(call_id, name)
        call = NativeToolCall(call_id, name, arguments)
        validate_repair_batch([call], [(call_id, name)], require_identity=True)
        return call
    return NativeToolCall(str(call_id or ""), str(name or ""), arguments)


def _require_native_identity(call_id: object, name: object) -> tuple[str, str]:
    if not isinstance(call_id, str) or not call_id.strip():
        raise ValueError("Ambiguous native tool identity")
    call_id.encode("utf-8")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Ambiguous native tool identity")
    name.encode("utf-8")
    return call_id, name


def validate_repair_batch(
    calls: list[NativeToolCall], identities: list[tuple[object, object]], *, require_identity: bool = False,
) -> None:
    if require_identity or any(call.argument_error is not None for call in calls):
        for call_id, name in identities:
            _require_native_identity(call_id, name)
        ids = [pair[0] for pair in identities]
        if len(set(ids)) != len(ids) or len(ids) != len(calls):
            raise ValueError("Ambiguous native tool identity")


def validate_repair_layout(output: list[Any]) -> None:
    ids = []
    for item in output:
        if not isinstance(item, dict):
            raise ValueError("Invalid native repair layout")
        if item.get("type") in {"function_call", "reasoning"}:
            for key in ("id", "encrypted_content"):
                if key in item and (not isinstance(item[key], str) or not item[key]):
                    raise ValueError("Invalid native repair layout")
            if "id" in item:
                ids.append(item["id"])
    if len(set(ids)) != len(ids):
        raise ValueError("Ambiguous native item identity")


def native_tool_call(
    protocol: str, call_id: str, name: str, arguments: Mapping[str, Any] | None,
    *, raw_arguments: str | None = None,
) -> dict[str, Any]:
    """Serialize a validated call for both parsed turns and explicit history."""
    if raw_arguments is not None:
        if arguments is not None or protocol not in {"responses", "chat_completions"}:
            raise ValueError("Cannot replay malformed native arguments")
    if arguments is None and raw_arguments is None:
        raise TypeError("'NoneType' object is not iterable")
    if protocol not in {"responses", "chat_completions"}:
        assert arguments is not None
        return {"type": "tool_use", "id": call_id, "name": name, "input": dict(arguments)}
    if raw_arguments is None:
        assert arguments is not None
        raw_arguments = json.dumps(dict(arguments), ensure_ascii=False, separators=(",", ":"))
    function = {"name": name, "arguments": raw_arguments}
    if protocol == "responses":
        return {"type": "function_call", "call_id": call_id, **function}
    return {"type": "function", "id": call_id, "function": function}


def native_text_message(protocol: str, role: str, text: str) -> dict[str, Any]:
    if protocol == "responses":
        kind = "output_text" if role == "assistant" else "input_text"
        return {"role": role, "content": [{"type": kind, "text": text}]}
    return {"role": role, "content": text}


def native_tool_result(protocol: str, call_id: str, output: str) -> dict[str, Any]:
    """Serialize one result; Anthropic callers group the blocks in a user turn."""
    if protocol == "responses":
        return {"type": "function_call_output", "call_id": call_id, "output": output}
    if protocol == "chat_completions":
        return {"role": "tool", "tool_call_id": call_id, "content": output}
    return {"type": "tool_result", "tool_use_id": call_id, "content": output}


def native_reasoning_item(item: dict[str, Any]) -> dict[str, Any]:
    """Responses input allowlist, not an arbitrary raw-envelope replay."""
    result: dict[str, Any] = {"type": "reasoning"}
    for key in ("id", "encrypted_content", "status"):
        if isinstance(item.get(key), str):
            result[key] = item[key]
    for key, block_type in (("summary", "summary_text"), ("content", "reasoning_text")):
        if isinstance(item.get(key), list):
            result[key] = [{"type": block_type, "text": block["text"]}
                           for block in item[key] if isinstance(block, dict)
                           and block.get("type") == block_type and isinstance(block.get("text"), str)]
    return result


def anthropic_private_layout(content: list[Any]) -> str:
    """Capture native layout from already-normalized ordinary text, without filtering twice."""
    layout = []
    has_private = False
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in {"thinking", "redacted_thinking"}:
            fields = ("type", "thinking", "signature") if kind == "thinking" else ("type", "data")
            if any(not isinstance(block.get(key), str) for key in fields):
                raise ValueError("Invalid native thinking block")
            if not block["signature" if kind == "thinking" else "data"]:
                raise ValueError("Incomplete native thinking block")
            layout.append({key: block[key] for key in fields})
            has_private = True
        elif kind == "text" and isinstance(block.get("text"), str):
            if block["text"]:
                layout.append({"type": "text", "text": block["text"]})
        elif kind in {"tool_use", "tool_ref"}:
            layout.append({"type": "tool_ref", "call_id": block.get("id") if kind == "tool_use" else block.get("call_id")})
    return json.dumps(layout, ensure_ascii=False) if has_private else ""


def responses_private_layout(output: list[Any], *, required: bool = False) -> str:
    layout = []
    has_private = required
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "reasoning":
            layout.append(native_reasoning_item(item))
            has_private = True
        elif item.get("type") == "function_call":
            ref = {"type": "tool_ref", "call_id": str(item.get("call_id") or item.get("id") or "")}
            if isinstance(item.get("id"), str):
                ref["id"] = item["id"]
            layout.append(ref)
        elif item.get("type") == "message":
            for block in item.get("content") or ():
                if isinstance(block, dict) and block.get("type") in {"output_text", "text"} and isinstance(block.get("text"), str):
                    text = strip_reasoning_markup(block["text"])
                    if text:
                        layout.append({"type": "text", "text": text})
    return json.dumps(layout, ensure_ascii=False) if has_private else ""


def parse_native_tool_turn(envelope: object, protocol: str, *, allow_argument_errors: bool = False) -> NativeToolTurn:
    """解析完整的 Provider 回合；不会执行任何工具。"""
    if not isinstance(envelope, dict) or envelope.get("type") == "error":
        raise ValueError("AI 响应格式无效")
    calls: list[NativeToolCall] = []
    text_parts: list[str] = []
    usage = extract_provider_usage(envelope, protocol)

    if protocol == "responses":
        finish_reason = provider_finish_reason(protocol, envelope.get("status"))
        output = envelope.get("output")
        if not isinstance(output, list):
            raise ValueError("AI 响应格式无效")
        assistant_items: list[dict[str, Any]] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "function_call":
                call = parse_native_call(
                    item.get("call_id") if allow_argument_errors else item.get("call_id") or item.get("id"),
                    item.get("name"), item.get("arguments"), allow_argument_errors=allow_argument_errors,
                )
                if call.argument_error is None and not call.call_id:
                    call = NativeToolCall(str(item.get("id") or ""), call.name, call.arguments)
                calls.append(call)
                assistant_items.append(native_tool_call(protocol, call.call_id, call.name, call.arguments, raw_arguments=call.raw_arguments))
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
        visible_text = strip_reasoning_markup("".join(text_parts))
        if any(call.argument_error for call in calls):
            validate_repair_batch(calls, [(item.get("call_id"), item.get("name"))
                                         for item in output if isinstance(item, dict) and item.get("type") == "function_call"])
            if envelope.get("error"):
                raise ValueError("Invalid native repair envelope")
            validate_repair_layout(output)
        return NativeToolTurn(
            text=visible_text,
            tool_calls=tuple(calls),
            finish_reason=finish_reason,
            assistant_entry=assistant_items,
            usage=usage,
            private_layout_json=responses_private_layout(output, required=any(call.argument_error for call in calls)),
        )

    if protocol == "chat_completions":
        choice = envelope["choices"][0]
        finish_reason = provider_finish_reason(protocol, choice.get("finish_reason"))
        message = choice["message"]
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
            call = parse_native_call(
                item.get("id"), function.get("name"), function.get("arguments"),
                allow_argument_errors=allow_argument_errors,
            )
            calls.append(call)
            assistant_calls.append(native_tool_call(protocol, call.call_id, call.name, call.arguments, raw_arguments=call.raw_arguments))
        visible_text = strip_reasoning_markup("".join(text_parts))
        assistant_entry: dict[str, Any] = {
            "role": "assistant",
            "content": visible_text or None,
        }
        if assistant_calls:
            assistant_entry["tool_calls"] = assistant_calls
        if any(call.argument_error for call in calls):
            validate_repair_batch(calls, [(item.get("id"), item["function"].get("name"))
                                         for item in message.get("tool_calls") or []
                                         if isinstance(item, dict) and isinstance(item.get("function"), dict)])
            if envelope.get("error") or (message.get("reasoning_content") is not None and not isinstance(message["reasoning_content"], str)):
                raise ValueError("Invalid native repair envelope")
        return NativeToolTurn(
            text=visible_text,
            tool_calls=tuple(calls),
            finish_reason=finish_reason,
            assistant_entry=assistant_entry,
            usage=usage,
            private_layout_json=json.dumps([{"type": "reasoning_content", "text": message["reasoning_content"]}], ensure_ascii=False)
            if isinstance(message.get("reasoning_content"), str) else "[]" if any(call.argument_error for call in calls) else "",
        )

    finish_reason = provider_finish_reason(protocol, envelope.get("stop_reason"))
    content = envelope.get("content")
    if not isinstance(content, list):
        raise ValueError("AI 响应格式无效")
    # Normalize JSON once; public text and private layout share these blocks.
    # SSE supplies its own finalized per-block text without changing its semantics.
    content = [
        {**block, "text": strip_reasoning_markup(block["text"])}
        if isinstance(block, dict) and block.get("type") == "text"
        and isinstance(block.get("text"), str) else block
        for block in content
    ]
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
            call = parse_native_call(
                block.get("id"), block.get("name"), block.get("input"), require_identity=True,
            )
            calls.append(call)
            assistant_blocks.append(native_tool_call(protocol, call.call_id, call.name, call.arguments))
    validate_repair_batch(calls, [(block.get("id"), block.get("name"))
                                 for block in content if isinstance(block, dict) and block.get("type") == "tool_use"],
                          require_identity=True)
    return NativeToolTurn(
        text="".join(text_parts),
        tool_calls=tuple(calls),
        finish_reason=finish_reason,
        assistant_entry={"role": "assistant", "content": assistant_blocks},
        usage=usage,
        private_layout_json=anthropic_private_layout(content),
    )


def append_native_tool_results(
    protocol: str,
    history: list[dict[str, Any]],
    turn: NativeToolTurn,
    outputs: list[tuple[NativeToolCall, str]],
) -> list[dict[str, Any]]:
    """追加完整、已校验工具结果；调用方必须先完成服务端安全投影。"""
    updated = list(history)
    results = [native_tool_result(protocol, call.call_id, output) for call, output in outputs]
    if protocol == "responses":
        assistant_items = turn.assistant_entry
        if isinstance(assistant_items, list):
            updated.extend(item for item in assistant_items if isinstance(item, dict))
    elif isinstance(turn.assistant_entry, dict):
        updated.append(turn.assistant_entry)
    if protocol in {"responses", "chat_completions"}:
        updated.extend(results)
    else:
        updated.append({"role": "user", "content": results})
    return updated
