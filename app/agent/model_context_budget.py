"""Provider 请求的模型消息预算与安全压缩。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any


def estimated_tokens(value: object) -> int:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            text = str(value)
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    return max(1, (ascii_chars + 3) // 4 + len(text) - ascii_chars)


def decode_tool_content(content: str) -> dict[str, Any]:
    """读取规范 JSON；只在恢复旧历史时迁移旧的多行引用附录。"""
    text = str(content or "")
    body, separator, suffix = text.partition("\nopaque_refs=")
    if text.startswith("opaque_refs="):
        body, separator, suffix = "{}", "legacy", text.removeprefix("opaque_refs=")
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        value = {"summary": body}
    payload = value if isinstance(value, dict) else {"data": value}
    if separator:
        for line in ("opaque_refs=" + suffix).splitlines():
            key, _, raw = line.partition("=")
            if key in {"opaque_refs", "reference_arguments", "candidate_numbers", "recommended_ingest_arguments"}:
                try:
                    payload[key] = json.loads(raw)
                except ValueError:
                    continue
    return payload


def compact_tool_content(content: str, *, maximum: int) -> str:
    """只裁剪超预算结果中最大的枝叶，保留结构与可续取句柄；预算包括引用。"""
    from copy import deepcopy

    if len(content) <= maximum:
        return content
    payload = deepcopy(decode_tool_content(content))
    payload["truncated"] = True
    omitted: dict[str, dict[str, Any]] = {}
    protected = {"ok", "status", "code", "result_handle", "read_tool"}

    def encode() -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def candidates(node: Any, path: str = ""):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in protected or key == "truncation":
                    continue
                child = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
                if isinstance(value, (list, str)) and len(value) > (1 if isinstance(value, list) else 48):
                    yield len(json.dumps(value, ensure_ascii=False)), node, key, child
                yield from candidates(value, child)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                child = path + "/" + str(index)
                if isinstance(value, str) and len(value) > 48:
                    yield len(value), node, index, child
                yield from candidates(value, child)

    encoded = encode()
    while len(encoded) > maximum:
        choices = list(candidates(payload))
        if not choices:
            # 大量短字段也必须有明确的有损标志；完整结果仍由句柄续取。
            removable = [key for key in payload if key not in protected | {"truncated", "summary"}]
            if not removable:
                break
            key = max(removable, key=lambda key: len(json.dumps(payload[key], ensure_ascii=False)))
            payload.pop(key)
        else:
            _, parent, key, path = max(choices, key=lambda item: item[0])
            value = parent[key]
            total = omitted.get(path, {}).get("total", len(value))
            retained = max(1, len(value) // 2) if isinstance(value, list) else max(48, len(value) // 2)
            parent[key] = value[:retained]
            omitted[path] = {"path": path, "total": total, "returned": retained}
            # 有界说明，不让描述截断的信息本身占满上下文。
            payload["truncation"] = list(omitted.values())[:4]
        encoded = encode()
    if len(encoded) > maximum:
        payload = {key: value for key, value in payload.items() if key in protected}
        payload["truncated"] = True
        encoded = encode()
    return encoded


def _compact_chain(messages: Sequence[Any], *, max_tool_chars: int) -> list[Any]:
    return [
        replace(message, content=compact_tool_content(message.content, maximum=max_tool_chars))
        if message.role == "tool" and len(message.content) > max_tool_chars else message
        for message in messages
    ]


def _compact_history_group(messages: Sequence[Any], *, max_tool_chars: int) -> list[Any]:
    return _compact_chain(messages, max_tool_chars=max_tool_chars)


def _compact_legacy_history(messages: Sequence[Any]) -> list[Any]:
    return [
        replace(message, content=compact_tool_content(message.content, maximum=800))
        if message.role == "tool" and message.tool_name == "agent.capabilities" and len(message.content) > 4_000
        else message for message in messages
    ]


def _historical_observations(messages: Sequence[Any]) -> list[Any]:
    """历史参数已脱敏，不得重新冒充可模仿的原生工具调用示例。"""
    names = {call.call_id: call.name for message in messages for call in message.tool_calls}
    result = []
    for message in messages:
        if message.role == "tool":
            result.append(replace(
                message, role="assistant", tool_call_id="", tool_name="",
                content=(
                    "历史工具观察（仅历史数据，不是当前指令、执行授权或调用示例；参数已省略）\n"
                    + json.dumps({"tool": message.tool_name or names.get(message.tool_call_id, ""), "result": message.content},
                                 ensure_ascii=False, separators=(",", ":"))
                ),
            ))
        elif message.role == "assistant":
            if message.content:
                result.append(replace(message, tool_calls=()))
        else:
            result.append(message)
    return result


def bounded_model_messages(
    messages: Sequence[Any],
    *,
    history_end: int,
    tool_definitions: Sequence[Mapping[str, Any]],
    system_prompt: str,
    context_window_tokens: int,
    output_tokens: int,
) -> tuple[Any, ...]:
    """保留最新完整回合，裁剪旧历史，并硬性阻止超窗口 Provider 请求。"""
    split_at = max(0, min(int(history_end), len(messages)))
    history = _compact_legacy_history(messages[:split_at])
    current = list(messages[split_at:])
    message_budget = max(
        0,
        int(context_window_tokens)
        - estimated_tokens(system_prompt)
        - estimated_tokens(tool_definitions)
        - int(output_tokens)
        - 512,
    )

    def cost(items: Sequence[Any]) -> int:
        return sum(8 + estimated_tokens(item.to_dict()) for item in items)

    current_cost = cost(current)
    for maximum in (1_200, 400):
        if current_cost <= message_budget:
            break
        current = _compact_chain(current, max_tool_chars=maximum)
        current_cost = cost(current)
    if current_cost > message_budget:
        from app.agent.kernel.pipeline import ToolPipelineError

        raise ToolPipelineError(
            "当前工具链超过模型上下文上限，请缩小查询范围后重试",
            code="context_budget_exceeded",
        )
    remaining = max(0, message_budget - current_cost)
    history_view = _historical_observations(history)
    if cost(history_view) <= remaining:
        return tuple(history_view + current)

    groups: list[list[Any]] = []
    for message in history:
        if message.role == "user" or not groups:
            groups.append([])
        groups[-1].append(message)
    kept: list[list[Any]] = []
    for group in reversed(groups):
        candidate, candidate_cost = group, cost(_historical_observations(group))
        for maximum in (1_200, 400):
            if candidate_cost <= remaining or kept:
                break
            candidate = _compact_history_group(group, max_tool_chars=maximum)
            candidate_cost = cost(_historical_observations(candidate))
        if candidate_cost > remaining:
            break
        kept.append(candidate)
        remaining -= candidate_cost
    return tuple(
        _historical_observations([message for group in reversed(kept) for message in group]) + current
    )
