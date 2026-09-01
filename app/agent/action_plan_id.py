"""Agent 行动计划 ID 的唯一格式契约。"""
from __future__ import annotations

import re
from typing import Any

_ACTION_PLAN_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,256}\Z")


def normalize_action_plan_id(value: Any) -> str:
    """返回规范化的 URL-safe 行动计划 ID；无效输入返回空字符串。"""
    if not isinstance(value, str):
        return ""
    token = value.strip()
    return token if _ACTION_PLAN_ID_PATTERN.fullmatch(token) else ""
