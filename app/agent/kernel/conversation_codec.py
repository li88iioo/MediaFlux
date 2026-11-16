"""Versioned row encoding; only executor-owned PendingText gets a short token."""
from __future__ import annotations

import json
from hashlib import sha256
from collections.abc import Mapping, Sequence
from typing import Any

KEYS = (
    "role", "content", "tool_calls", "tool_call_id", "tool_name",
    "provider_state", "public_content", "effect_plan_id",
    "completion_receipt_id", "history_before_truncated",
)


class PendingText(str):
    """Private in-process provenance, never inferred from model/business JSON."""


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def state_storage_payload(payload: Mapping[str, Any], *, owner: str) -> dict[str, Any]:
    """容量与 SQLite 共用物理表示；逻辑导出仍保留完整 summary。"""
    stored = dict(payload)
    if payload.get("metadata", {}).get("history_memory_version") == 1:
        summary = payload["summary"]
        identity = json_text([owner, payload["session_id"], payload["lifecycle_id"]])
        stored["summary"] = {"storage": "session-history-v1",
                             "ref": "ref_" + sha256(identity.encode()).hexdigest(),
                             "sha256": sha256(summary.encode()).hexdigest()}
    return stored


def state_json_value(text: str) -> Any:
    """Byte bound and HMAC are checked by the caller before this linear scan."""
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > 1024:
                raise ValueError("state nesting limit exceeded")
        elif char in "]}":
            depth -= 1
    try:
        return json.loads(text)
    except RecursionError as exc:
        raise ValueError("state nesting exceeds JSON runtime limit") from exc


def pending_content(started: bool) -> PendingText:
    return PendingText(json_text({
        "ok": False, "status": "error",
        "code": "result_unknown" if started else "not_executed",
        "error": "工具执行被中断，结果未知" if started else "本调用未执行",
        "instruction": "请根据错误修正参数、换用本轮其他工具，或向用户明确说明限制。",
    }))


def conversation_payload(rows: Sequence[Mapping[str, Any]]) -> list[Any]:
    """Unknown keys cost exactly as before; choose legacy if wrapper would grow."""
    encoded = []
    for row in rows:
        values = []
        for key, value in row.items():
            if not isinstance(key, str):
                raise TypeError("conversation key must be a string")
            token = KEYS.index(key) if key in KEYS else key
            if key == "content" and isinstance(value, PendingText):
                token = -1
                value = (pending_content(False), pending_content(True)).index(value)
            values.extend((token, value))
        encoded.append(values)
    packed = [1, encoded]
    legacy = list(rows)
    return packed if len(json_text(packed).encode()) < len(json_text(legacy).encode()) else legacy


def conversation_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise TypeError("conversation must be a list")
    if not payload or isinstance(payload[0], dict):
        if not all(isinstance(row, dict) for row in payload):
            raise ValueError("malformed legacy conversation")
        return payload
    if len(payload) != 2 or type(payload[0]) is not int or payload[0] != 1 or not isinstance(payload[1], list):
        raise ValueError("unknown or malformed conversation encoding")
    rows = []
    calls = {}
    for values in payload[1]:
        if not isinstance(values, list) or len(values) % 2:
            raise ValueError("malformed conversation row")
        row = {}
        for index in range(0, len(values), 2):
            key, value = values[index:index + 2]
            if type(key) is int and key == -1:
                if type(value) is not int or value not in (0, 1):
                    raise ValueError("invalid pending token")
                key, value = "content", pending_content(bool(value))
            elif type(key) is int and 0 <= key < len(KEYS):
                key = KEYS[key]
            elif not isinstance(key, str):
                raise ValueError("invalid key token")
            if key in row:
                raise ValueError("duplicate row key")
            row[key] = value
        if row.get("role") == "assistant":
            calls = {
                call["call_id"]: call.get("name")
                for call in (row.get("tool_calls") if isinstance(row.get("tool_calls"), list) else [])
                if isinstance(call, dict) and isinstance(call.get("call_id"), str)
            }
        if isinstance(row.get("content"), PendingText) and (
            row.get("role") != "tool" or not isinstance(row.get("tool_call_id"), str)
            or not row["tool_call_id"] or row["tool_call_id"] not in calls
            or not isinstance(row.get("tool_name"), str) or not row["tool_name"]
            or calls[row["tool_call_id"]] != row["tool_name"]
        ):
            raise ValueError("pending token is not paired with a call")
        rows.append(row)
    return rows


def conversation_cost(rows: Sequence[Mapping[str, Any]]) -> int:
    return len(json_text(conversation_payload(rows)).encode("utf-8"))
