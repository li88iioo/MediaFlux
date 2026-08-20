"""光鸭整理多源配置的统一解析与规范化。"""
from __future__ import annotations

import json
from typing import Any

_MAX_ORGANIZE_SOURCES = 64
_MAX_SOURCE_FIELD_LENGTH = 1024


def normalize_organize_sources(
    raw: Any,
    *,
    require_nonempty: bool = False,
) -> tuple[list[dict[str, str]], str]:
    """解析正式多源整理配置。"""
    if raw is None or raw == "":
        items: Any = []
    elif isinstance(raw, str):
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            return [], "GY_ORGANIZE_SOURCE_DIRS 不是有效 JSON"
    else:
        items = raw
    if not isinstance(items, list):
        return [], "GY_ORGANIZE_SOURCE_DIRS 必须是数组"
    if len(items) > _MAX_ORGANIZE_SOURCES:
        return [], f"GY_ORGANIZE_SOURCE_DIRS 最多允许 {_MAX_ORGANIZE_SOURCES} 个来源"

    sources: list[dict[str, str]] = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            source_id = item.strip()
            name = f"源目录{index + 1}"
        elif isinstance(item, dict):
            source_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip() or f"源目录{index + 1}"
        else:
            return [], "GY_ORGANIZE_SOURCE_DIRS 包含无效来源"
        if not source_id or source_id == "0":
            return [], "整理来源 ID 不能为空且不能为根目录"
        if len(source_id) > _MAX_SOURCE_FIELD_LENGTH:
            return [], "整理来源 ID 过长"
        if len(name) > _MAX_SOURCE_FIELD_LENGTH:
            return [], "整理来源名称过长"
        if all(row["id"] != source_id for row in sources):
            sources.append({"id": source_id, "name": name})

    if require_nonempty and not sources:
        return [], "未配置光鸭整理源目录"
    return sources, ""


def encode_organize_sources(sources: list[dict[str, str]]) -> str:
    """输出稳定、紧凑的配置 JSON。"""
    return json.dumps(sources, ensure_ascii=False, separators=(",", ":"))
