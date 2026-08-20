"""Agent 会话滚动摘要的受限结构与校验。"""
from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import unquote

from app.sensitive_data import contains_sensitive_credential

SUMMARY_SCHEMA_VERSION = 1
SUMMARY_FIELDS = (
    "current_goal",
    "user_preferences",
    "confirmed_facts",
    "completed_actions",
    "open_tasks",
    "important_entities",
)
_SUMMARY_LIST_LIMITS = {
    "user_preferences": (6, 180),
    "confirmed_facts": (8, 220),
    "completed_actions": (6, 220),
    "open_tasks": (8, 220),
    "important_entities": (8, 160),
}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_UNSAFE_CONTEXT_RE = re.compile(
    r"(?ix)(?:"
    r"\b(?:magnet|ed2k)\s*:\s*"
    r"|\bfile\s*:\s*(?:/{1,3}|\\)"
    r"|\b(?:cookie|set-cookie)\s*[:=]\s*[^\s;,]+"
    r"|\b[a-z][a-z0-9+.-]{1,20}\s*:\s*//\S+"
    r"|(?:^|[\s:：(\"'\[\{])/(?!/)[^\s]+"
    r"|(?:^|[\s:：(\"'\[\{])\\\\[^\s]+"
    r"|(?:^|[^A-Za-z0-9])[A-Za-z]:[\\/][^\s]+"
    r")"
)


def conversation_summary_schema() -> dict[str, Any]:
    """返回供 OpenAI-compatible strict JSON Schema 使用的固定结构。"""
    properties: dict[str, Any] = {
        "schema_version": {"type": "integer", "const": SUMMARY_SCHEMA_VERSION},
        "current_goal": {"type": "string", "maxLength": 240},
    }
    for field, (max_items, max_length) in _SUMMARY_LIST_LIMITS.items():
        properties[field] = {
            "type": "array",
            "maxItems": max_items,
            "items": {"type": "string", "minLength": 1, "maxLength": max_length},
        }
    return {
        "type": "object",
        "required": ["schema_version", *SUMMARY_FIELDS],
        "properties": properties,
        "additionalProperties": False,
    }


def contains_unsafe_summary_text(value: Any) -> bool:
    """摘要属于辅助记忆：无法确定安全时直接拒绝，而不是尝试清洗。"""
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    for _ in range(4):
        decoded = unquote(normalized)
        if decoded == normalized:
            break
        normalized = decoded
    return bool(
        _CONTROL_RE.search(normalized)
        or contains_sensitive_credential(normalized)
        or _UNSAFE_CONTEXT_RE.search(normalized)
        or _PERCENT_ESCAPE_RE.search(normalized)
    )


def _safe_text(value: Any, *, max_length: int, allow_empty: bool) -> str | None:
    raw = unicodedata.normalize("NFKC", str(value or ""))
    if contains_unsafe_summary_text(raw):
        return None
    normalized = " ".join(raw.split()).strip()
    if not normalized:
        return "" if allow_empty else None
    if len(normalized) > max_length:
        return None
    return normalized


def normalize_conversation_summary(value: Any) -> dict[str, Any] | None:
    """严格验证摘要形状和文字边界；任何异常都 fail closed。"""
    if not isinstance(value, dict) or set(value) != {"schema_version", *SUMMARY_FIELDS}:
        return None
    if value.get("schema_version") != SUMMARY_SCHEMA_VERSION:
        return None
    current_goal = _safe_text(value.get("current_goal"), max_length=240, allow_empty=True)
    if current_goal is None:
        return None
    normalized: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "current_goal": current_goal,
    }
    total_characters = len(current_goal)
    for field, (max_items, max_length) in _SUMMARY_LIST_LIMITS.items():
        raw_items = value.get(field)
        if not isinstance(raw_items, list) or len(raw_items) > max_items:
            return None
        items: list[str] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, str):
                return None
            item = _safe_text(raw_item, max_length=max_length, allow_empty=False)
            if item is None:
                return None
            if item not in items:
                items.append(item)
                total_characters += len(item)
        normalized[field] = items
    if total_characters > 3_000:
        return None
    return normalized


def render_conversation_summary(value: Any) -> str:
    """将已验证的结构摘要渲染成有限、明确标注的 LLM 参考上下文。"""
    summary = normalize_conversation_summary(value)
    if summary is None:
        return ""
    labels = {
        "current_goal": "当前目标",
        "user_preferences": "用户偏好",
        "confirmed_facts": "已确认事实",
        "completed_actions": "已完成动作",
        "open_tasks": "待处理事项",
        "important_entities": "重要对象",
    }
    lines: list[str] = []
    goal = summary["current_goal"]
    if goal:
        lines.append(f"{labels['current_goal']}：{goal}")
    for field in SUMMARY_FIELDS[1:]:
        items = summary[field]
        if items:
            lines.append(f"{labels[field]}：" + "；".join(items))
    return "\n".join(lines)[:4_000]
