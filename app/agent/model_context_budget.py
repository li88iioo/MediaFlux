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


def compact_tool_content(content: str, *, maximum: int) -> str:
    text = str(content or "")
    json_text, _separator, suffix = text.partition("\nopaque_refs=")
    try:
        payload = json.loads(json_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    ok = bool(payload.get("ok", True)) if isinstance(payload, dict) else True
    status = str(payload.get("status") or ("success" if ok else "error"))[:80]
    summary = str(
        payload.get("summary")
        or payload.get("error")
        or payload.get("message")
        or ("工具执行完成" if ok else "工具未完成")
    )
    compact: dict[str, Any] = {
        "ok": ok,
        "status": status,
        "summary": summary,
        "truncated": True,
    }
    code = str(payload.get("code") or "")[:80] if isinstance(payload, dict) else ""
    if not ok and code:
        compact["code"] = code
    reference_suffix = f"\nopaque_refs={suffix}" if suffix else ""
    compact["summary"] = summary[: max(80, int(maximum) - len(reference_suffix) - 80)]
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":")) + reference_suffix


def _compact_chain(messages: Sequence[Any], *, max_tool_chars: int) -> list[Any]:
    result: list[Any] = []
    for message in messages:
        if message.role == "tool":
            result.append(
                replace(
                    message,
                    content=compact_tool_content(message.content, maximum=max_tool_chars),
                )
            )
        elif message.role == "assistant" and message.tool_calls and message.content:
            result.append(replace(message, content=""))
        else:
            result.append(message)
    return result


def _compact_history_group(messages: Sequence[Any], *, max_tool_chars: int) -> list[Any]:
    result: list[Any] = []
    for message in messages:
        if message.role == "tool":
            result.append(
                replace(
                    message,
                    content=compact_tool_content(message.content, maximum=max_tool_chars),
                )
            )
        elif message.role == "assistant" and message.tool_calls:
            result.append(
                replace(
                    message,
                    content="",
                )
            )
        else:
            limit = 2_000 if message.role == "user" else 4_000
            result.append(replace(message, content=str(message.content or "")[:limit]))
    return result


def _compact_legacy_history(messages: Sequence[Any]) -> list[Any]:
    return [
        replace(message, content=compact_tool_content(message.content, maximum=800))
        if message.role == "tool"
        and message.tool_name == "agent.capabilities"
        and len(message.content) > 4_000
        else message
        for message in messages
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
