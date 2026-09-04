"""Agent Kernel 的统一公开文本投影。

模型上下文可以保留结构化工具结果，但 Web/TG 历史只能消费这里生成的
紧凑公开摘要，避免把内部 JSON、引用和值域细节直接暴露给用户。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.agent.public_safety import public_tool_label, sanitize_public_text

_CONFIRMED_RESULT_MARKER = "已确认操作的可信系统结果（不是待执行计划）："
_TARGET_LABELS = {
    "guangya": "光鸭云盘",
    "qb": "qBittorrent",
    "qbittorrent": "qBittorrent",
    "cloud": "云盘",
    "local": "本地",
}
_COUNT_FIELDS: tuple[tuple[str, str], ...] = (
    ("total", "请求"),
    ("succeeded", "已受理"),
    ("created", "已创建"),
    ("updated", "已更新"),
    ("review_required", "待复核"),
    ("duplicate", "已存在"),
    ("failed", "未完成"),
    ("skipped", "已跳过"),
)


def _safe(value: object, *, limit: int = 600) -> str:
    return sanitize_public_text(value, limit=limit)


def _int_value(value: object) -> int | None:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _result_icon(result: Mapping[str, Any]) -> str:
    status = str(result.get("status") or "").strip().lower()
    if result.get("ok") is False or status in {"failed", "error"}:
        return "❌"
    if status in {"partial", "degraded", "incomplete", "attention"}:
        return "⚠️"
    return "✅"


def _failed_item_errors(data: Mapping[str, Any]) -> list[str]:
    items = data.get("items")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        return []
    errors: list[str] = []
    for item in items:
        if not isinstance(item, Mapping) or item.get("ok") is not False:
            continue
        error = _safe(item.get("error"), limit=180)
        if error and error not in errors:
            errors.append(error)
        if len(errors) >= 3:
            break
    return errors


def format_public_result(
    value: Mapping[str, Any] | None,
    *,
    fallback: str = "操作已结束。",
) -> str:
    """把公开 ToolResult 压缩为适合 Web/TG 展示的 Markdown。"""

    result = dict(value or {})
    summary = _safe(result.get("summary"), limit=700) or _safe(
        result.get("message"), limit=700
    )
    if not summary:
        summary = _safe(fallback, limit=700) or "操作已结束。"

    lines = [f"{_result_icon(result)} {summary}"]
    data = result.get("data")
    if isinstance(data, Mapping):
        target = _safe(data.get("target"), limit=40).lower()
        if target:
            lines.append(f"- 目标：{_TARGET_LABELS.get(target, target)}")
        for key, label in _COUNT_FIELDS:
            count = _int_value(data.get(key))
            if count is not None:
                lines.append(f"- {label}：{count} 项")
        for error in _failed_item_errors(data):
            lines.append(f"- 失败原因：{error}")

    error = _safe(result.get("error"), limit=300)
    if error and error != summary and error not in "\n".join(lines):
        lines.append(f"- 说明：{error}")
    return "\n".join(lines)


def legacy_confirmed_result_public_content(content: object) -> str:
    """为旧会话中的确认结果补公开摘要；无法解析时宁可隐藏内部内容。"""

    text = str(content or "").strip()
    if not text.startswith(_CONFIRMED_RESULT_MARKER):
        return ""
    payload = text[len(_CONFIRMED_RESULT_MARKER) :].strip()
    try:
        value = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return "✅ 已确认操作已结束，可继续查询实际状态。"
    if not isinstance(value, Mapping):
        return "✅ 已确认操作已结束，可继续查询实际状态。"
    return format_public_result(value)


def public_conversation_messages(
    conversation: Sequence[Mapping[str, Any] | object],
) -> list[dict[str, Any]]:
    """投影可公开恢复的对话，过滤工具中间回合和空助手消息。"""

    messages: list[dict[str, Any]] = []
    pending_tools: list[str] = []

    def remember_tool(value: object) -> None:
        name = str(value or "").strip()
        if name and name not in pending_tools:
            pending_tools.append(name)

    for item in conversation:
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "").strip()
        if role == "user":
            pending_tools.clear()
            content = str(item.get("content") or "").strip()
            if content:
                messages.append({"role": "user", "content": content})
            continue
        if role == "tool":
            remember_tool(item.get("tool_name"))
            continue
        if role != "assistant":
            continue
        tool_calls = item.get("tool_calls")
        if isinstance(tool_calls, Sequence) and not isinstance(
            tool_calls, (str, bytes, bytearray)
        ):
            for raw_call in tool_calls:
                if isinstance(raw_call, Mapping):
                    remember_tool(raw_call.get("name"))
            continue
        tool_name = str(item.get("tool_name") or "").strip()
        if tool_name:
            remember_tool(tool_name)
            content = str(item.get("public_content") or "").strip()
            if not content:
                content = legacy_confirmed_result_public_content(item.get("content"))
        else:
            content = str(item.get("content") or "").strip()
        if content:
            message: dict[str, Any] = {"role": "assistant", "content": content}
            if pending_tools:
                message["tools"] = list(pending_tools)
                message["tool_labels"] = [
                    public_tool_label(tool_name) for tool_name in pending_tools
                ]
            messages.append(message)
            pending_tools.clear()
    return messages
