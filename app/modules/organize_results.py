"""整理任务的版本化结构化结果协议。

Web、Telegram 和调度器都直接消费该结构，禁止从自由格式日志反向解析。
新增字段只能追加；旧任务记录缺字段时按默认值补齐，保证跨版本可读。
"""
from __future__ import annotations

from typing import Any

# 递增规则：仅追加字段不升版本；语义变化或字段移除必须升版本并保留读取兼容。
ORGANIZE_RESULT_SCHEMA_VERSION = 1

_COUNTER_FIELDS = (
    "total", "matched", "moved", "renamed", "metadata_moved",
    "skipped", "need_confirm", "failed", "conflict",
    "subtitle_moved", "subtitle_skipped",
    "empty_dirs_cleaned", "replacement_cleanup_failed", "audit_failures",
)


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _dict_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def build_organize_result(
    stats: dict,
    *,
    status: str,
    source_results: object = (),
    notification_sent: bool = False,
    task_id: str = "",
    current_source: str = "",
    error: str = "",
) -> dict[str, Any]:
    """把任务级统计收敛成版本化结果。"""
    stats = stats if isinstance(stats, dict) else {}
    counters = {name: _int(stats.get(name)) for name in _COUNTER_FIELDS}
    strm = stats.get("strm")
    strm_changes = _dict_list(stats.get("strm_changes"))
    changed_dirs = _text_list(stats.get("changed_target_dirs"))
    if not changed_dirs and strm_changes:
        # 任务级 stats 不单独维护该字段，从权威的变化清单推导。
        from app.modules.organize_groups import changed_target_dirs as _derive

        changed_dirs = _derive(strm_changes)
    return {
        "schema_version": ORGANIZE_RESULT_SCHEMA_VERSION,
        "task_id": str(task_id or ""),
        "status": str(status or ""),
        "current_source": str(current_source or ""),
        "error": str(error or ""),
        "counters": counters,
        "groups": _dict_list(stats.get("group_results")),
        "group_progress": (
            dict(stats["group_progress"])
            if isinstance(stats.get("group_progress"), dict) else {}
        ),
        "changed_target_dirs": changed_dirs,
        "strm": dict(strm) if isinstance(strm, dict) else {},
        "strm_changes": strm_changes,
        "media_refresh": (
            dict(stats["media_refresh"])
            if isinstance(stats.get("media_refresh"), dict) else {}
        ),
        "notification": {"sent": bool(notification_sent)},
        "confirmations": _text_list(stats.get("confirmations")),
        "confirmation_groups": _dict_list(stats.get("confirmation_groups")),
        "scan_errors": _text_list(stats.get("scan_errors")),
        "skip_reasons": _text_list(stats.get("skip_reasons")),
        "sources": _dict_list(source_results),
    }


def read_organize_result(payload: object) -> dict[str, Any]:
    """读取任意历史版本的任务结果，缺字段按默认值补齐。

    未知的高版本按“尽力读取”处理：已知字段照常提供，未知字段原样保留，
    禁止因为版本号更高就丢弃整段结果。
    """
    if not isinstance(payload, dict):
        return build_organize_result({}, status="")
    version = payload.get("schema_version")
    if version is None:
        # 历史上存在两类未版本化记录：直接 stats，以及任务包装器
        # {task_id, stats, source_results, current_source}。兼容读取时必须
        # 先解开包装器，否则 counters 会全部退化为 0。
        legacy_stats = payload.get("stats")
        stats = legacy_stats if isinstance(legacy_stats, dict) else payload
        return build_organize_result(
            stats,
            status=str(payload.get("status") or stats.get("status") or ""),
            source_results=payload.get("source_results") or payload.get("sources") or (),
            notification_sent=bool(payload.get("notification_sent")),
            task_id=str(payload.get("task_id") or ""),
            current_source=str(payload.get("current_source") or ""),
            error=str(payload.get("error") or ""),
        )
    normalized = build_organize_result({}, status=str(payload.get("status") or ""))
    normalized.update({
        key: value for key, value in payload.items() if key != "schema_version"
    })
    counters = payload.get("counters")
    normalized["counters"] = {
        name: _int((counters or {}).get(name)) for name in _COUNTER_FIELDS
    } if isinstance(counters, dict) else normalized["counters"]
    try:
        normalized["schema_version"] = int(version)
    except (TypeError, ValueError):
        normalized["schema_version"] = ORGANIZE_RESULT_SCHEMA_VERSION
    return normalized
