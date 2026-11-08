"""只读 UX DTO 与会话显示元数据；不承载领域执行。"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from app.agent.kernel.session_state import SessionState
from app.agent.public_safety import sanitize_public_text


def display_text(value: Any, *, limit: int = 300) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<[^>]*>", "", html.unescape(value))
    text = "".join(
        char for char in text
        if char.isspace() or not unicodedata.category(char).startswith("C")
    )
    return sanitize_public_text(" ".join(text.split()), limit=limit)


def session_display_patch(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value or not set(value) <= {"title", "pinned"}:
        raise ValueError("请求字段无效")
    patch: dict[str, Any] = {}
    if "title" in value:
        raw = value["title"]
        if not isinstance(raw, str) or len(raw) > 1_000:
            raise ValueError("title 必须是 1–80 字的安全显示标题")
        title = display_text(raw, limit=1_001)
        if not title or len(title) > 80:
            raise ValueError("title 必须是 1–80 字的安全显示标题")
        patch["title"] = title
    if "pinned" in value:
        if type(value["pinned"]) is not bool:
            raise ValueError("pinned 必须是布尔值")
        patch["pinned"] = value["pinned"]
    return patch


def session_summary(state: SessionState, *, updated_at: float) -> dict[str, Any]:
    title = display_text(state.metadata.get("title"), limit=80)
    if not title:
        for item in state.conversation:
            if isinstance(item, dict) and item.get("role") == "user":
                title = display_text(item.get("content"), limit=80)
                if title:
                    break
    return {
        "session_id": state.session_id,
        "generation": state.generation,
        "title": title or "新对话",
        "pinned": state.metadata.get("pinned") is True,
        "message_count": len(state.conversation),
        "pending_approval": bool(state.pending_effect_plan_id),
        "updated_at": updated_at,
    }


def next_actions_view(result: Any) -> dict[str, Any]:
    data = result.get("data", {}) if isinstance(result, Mapping) else {}
    status = data.get("snapshot_status")
    allowed = {"attention", "active", "waiting", "empty", "partial", "success", "unavailable"}
    if not isinstance(result, Mapping) or not result.get("ok") or status not in allowed:
        return {"actions": [], "snapshot_status": "unavailable"}
    actions: list[dict[str, str]] = []
    raw_actions = data.get("actions")
    for raw in raw_actions[:20] if isinstance(raw_actions, list) else []:
        if not isinstance(raw, dict):
            continue
        action = {
            "id": raw["action_key"] if isinstance(raw.get("action_key"), str)
            and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", raw["action_key"]) else "",
            "title": display_text(raw.get("label"), limit=80),
            "description": display_text(raw.get("why"), limit=300),
            "prompt": display_text(raw.get("prompt"), limit=300),
        }
        if all(action.values()) and not any(item["id"] == action["id"] for item in actions):
            actions.append(action)
        if len(actions) == 3:
            break
    return {"actions": actions if status != "unavailable" else [], "snapshot_status": status}
