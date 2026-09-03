"""整理时间线的安全只读审计摘要。"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any
from urllib.parse import unquote

from app import database as db
from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolResult
from app.logger import get_logger

logger = get_logger(__name__)

_ORIGINS = ("all", "guangya", "local")
_STATUSES = ("all", "failed", "manual", "processing", "success", "skipped", "reverted")
_PUBLIC_ORIGINS = {"guangya", "local"}
_PUBLIC_STATUSES = set(_STATUSES) - {"all"}
_PUBLIC_MEDIA_TYPES = {"movie", "tv"}
_SENSITIVE_PATTERNS = (
    re.compile(r"(?:https?|magnet|ed2k)://", re.IGNORECASE),
    re.compile(r"(?:^|\s)(?:/[^\s]+|[A-Za-z]:[\\/][^\s]+)"),
    re.compile(r"(?:token|secret|password|passwd|api[_-]?key)\s*[:=]", re.IGNORECASE),
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
    re.compile(r"\b(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b"),
)
_SAFE_TIMESTAMP = re.compile(r"^[0-9T:+.Z -]{10,40}$")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def organize_audit_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    extra = set(arguments) - {"origin", "status", "limit"}
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    origin = arguments.get("origin", "all")
    status = arguments.get("status", "all")
    limit = arguments.get("limit", 10)
    if not isinstance(origin, str) or origin.strip().lower() not in _ORIGINS:
        raise AgentToolError("origin 必须是 all、guangya 或 local")
    if not isinstance(status, str) or status.strip().lower() not in _STATUSES:
        raise AgentToolError("status 不是受支持的整理状态")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise AgentToolError("limit 必须是 1 到 50 的整数")
    return {
        "origin": origin.strip().lower(),
        "status": status.strip().lower(),
        "limit": limit,
    }


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _decode(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    for _ in range(2):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return " ".join(text.replace("\x00", " ").split()).strip()


def _safe_title(value: Any) -> str:
    title = _decode(value)
    if not title or any(pattern.search(title) for pattern in _SENSITIVE_PATTERNS):
        return "未命名条目"
    return title[:120]


def _safe_year(value: Any) -> str:
    year = str(value or "").strip()
    if not re.fullmatch(r"\d{4}", year):
        return ""
    return year if 1800 <= int(year) <= 2200 else ""


def _safe_positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if 0 < number <= 9999 else None


def _safe_timestamp(value: Any) -> str:
    timestamp = str(value or "").strip()
    if not timestamp or not _SAFE_TIMESTAMP.fullmatch(timestamp):
        return ""
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return timestamp


def _empty_counts() -> dict[str, dict[str, int]]:
    return {
        "by_origin": {"guangya": 0, "local": 0},
        "by_status": {key: 0 for key in _STATUSES if key != "all"},
    }


def _empty_data(arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "probe_mode": "database",
        "network_accessed": False,
        "filesystem_accessed": False,
        "origin": arguments["origin"],
        "status_filter": arguments["status"],
        "limit": arguments["limit"],
        "total": 0,
        "counts": _empty_counts(),
        "records": [],
        "truncated": False,
    }


def audit_organize_logs(arguments: dict[str, Any]) -> ToolResult:
    try:
        raw = db.get_agent_organize_audit(owner="admin", **arguments)
    except Exception as exc:
        logger.warning("Agent 整理审计不可用 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法读取整理记录摘要",
            data=_empty_data(arguments),
            evidence=[
                Evidence(
                    "sqlite:organize_timeline",
                    "尝试读取整理时间线的固定脱敏视图；未读取或返回路径、标识、文件名、错误正文，也未访问网络或执行整理。",
                    _now(),
                )
            ],
            suggestions=["请检查本地数据库状态后重试。"],
            error="整理记录摘要当前不可用。",
        )

    raw = raw if isinstance(raw, dict) else {}
    raw_origins = raw.get("by_origin") if isinstance(raw.get("by_origin"), dict) else {}
    raw_statuses = (
        raw.get("by_status") if isinstance(raw.get("by_status"), dict) else {}
    )
    counts = _empty_counts()
    for key in counts["by_origin"]:
        counts["by_origin"][key] = _count(raw_origins.get(key))
    for key in counts["by_status"]:
        counts["by_status"][key] = _count(raw_statuses.get(key))

    records: list[dict[str, Any]] = []
    raw_records = raw.get("records") if isinstance(raw.get("records"), list) else []
    for raw_record in raw_records[: arguments["limit"]]:
        if not isinstance(raw_record, dict):
            continue
        origin = str(raw_record.get("origin") or "").strip().lower()
        status = str(raw_record.get("status") or "").strip().lower()
        media_type = str(raw_record.get("media_type") or "").strip().lower()
        records.append(
            {
                "origin": origin if origin in _PUBLIC_ORIGINS else "local",
                "status": status if status in _PUBLIC_STATUSES else "processing",
                "title": _safe_title(raw_record.get("title")),
                "media_type": media_type if media_type in _PUBLIC_MEDIA_TYPES else "",
                "year": _safe_year(raw_record.get("year")),
                "season": _safe_positive_int(raw_record.get("season")),
                "episode": _safe_positive_int(raw_record.get("episode")),
                "updated_at": _safe_timestamp(raw_record.get("updated_at")),
            }
        )

    total = _count(raw.get("total"))
    failed = counts["by_status"]["failed"]
    manual = counts["by_status"]["manual"]
    processing = counts["by_status"]["processing"]
    if failed or manual:
        status = "attention"
        summary = f"整理记录中有 {failed} 条失败、{manual} 条需要人工处理"
        ok = False
    elif processing:
        status = "running"
        summary = f"当前有 {processing} 条整理记录仍在处理中"
        ok = True
    elif total:
        status = "healthy"
        summary = f"最近整理记录共 {total} 条，没有失败或待人工处理项"
        ok = True
    else:
        status = "empty"
        summary = "当前筛选范围没有整理记录"
        ok = True

    suggestions: list[str] = []
    if failed:
        suggestions.append(
            "可继续查看失败记录摘要，确认是光鸭整理还是本地整理需要处理。"
        )
    if manual:
        suggestions.append("可查看本地媒体待确认队列摘要，了解积压来源与时长。")

    return ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data={
            "probe_mode": "database",
            "network_accessed": False,
            "filesystem_accessed": False,
            "origin": arguments["origin"],
            "status_filter": arguments["status"],
            "limit": arguments["limit"],
            "total": total,
            "counts": counts,
            "records": records,
            "truncated": bool(raw.get("truncated")),
        },
        evidence=[
            Evidence(
                "sqlite:organize_timeline",
                "仅返回整理来源、规范状态、媒体标题摘要与时间；未读取或返回路径、任务标识、文件名、外部 ID、错误正文或凭据。",
                _now(),
            )
        ],
        suggestions=suggestions,
    )
