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


def normalize_organize_source_ids(
    raw: Any,
    *,
    configured_sources: list[dict[str, str]] | None = None,
    require_nonempty: bool = False,
) -> tuple[list[str], str]:
    """解析来源 ID 子集配置，并可限制为当前正式整理来源。

    该结构用于“某些来源启用专用处理链”一类配置。只保存稳定 ID，名称
    始终从 ``GY_ORGANIZE_SOURCE_DIRS`` 读取，避免目录改名后产生两份漂移数据。
    """
    if raw is None or raw == "":
        items: Any = []
    elif isinstance(raw, str):
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            return [], "来源范围不是有效 JSON"
    else:
        items = raw
    if not isinstance(items, list):
        return [], "来源范围必须是数组"
    if len(items) > _MAX_ORGANIZE_SOURCES:
        return [], f"来源范围最多允许 {_MAX_ORGANIZE_SOURCES} 项"

    source_ids: list[str] = []
    for item in items:
        source_id = str(item.get("id") if isinstance(item, dict) else item or "").strip()
        if not source_id or source_id == "0":
            return [], "来源范围包含无效目录 ID"
        if len(source_id) > _MAX_SOURCE_FIELD_LENGTH:
            return [], "来源范围包含过长目录 ID"
        if source_id not in source_ids:
            source_ids.append(source_id)

    if configured_sources is not None:
        allowed = {
            str(item.get("id") or "").strip()
            for item in configured_sources
            if str(item.get("id") or "").strip()
        }
        unknown = [source_id for source_id in source_ids if source_id not in allowed]
        if unknown:
            return [], "来源范围包含未配置的整理源目录"
    if require_nonempty and not source_ids:
        return [], "至少选择一个来源目录"
    return source_ids, ""


def encode_organize_source_ids(source_ids: list[str]) -> str:
    """输出稳定、紧凑的来源 ID 数组。"""
    return json.dumps(
        list(dict.fromkeys(str(item).strip() for item in source_ids if str(item).strip())),
        ensure_ascii=False,
        separators=(",", ":"),
    )
