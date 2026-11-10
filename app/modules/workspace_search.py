"""公共工作流标题检索；保留真实标识和来源分页，不访问内容目录。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app import database as db
from app.logger import get_logger
from app.services import search_library
from app.discovery.models import normalize_search_query

logger = get_logger(__name__)

_SECTION_ORDER = ("library", "rss", "downloads", "organize", "local_media")
_SECTION_SET = set(_SECTION_ORDER)
_TITLE_LIMIT = 180
_STATUS_LIMIT = 48
_URI_LIKE = re.compile(
    r"(?:\b[a-z][a-z0-9+.-]{1,20}\s*:\s*//|magnet\s*:\s*\?|ed2k\s*:\s*//)",
    re.IGNORECASE,
)
_PATH_LIKE = re.compile(
    r"(?:^|[\s\[({])(?:/|\\\\|[A-Za-z]:[\\/]|\.{1,2}[\\/])",
    re.IGNORECASE,
)
_SECRET_LIKE = re.compile(
    r"\b(?:authorization|bearer|api[_ -]?key|token|password|passwd|cookie|session)"
    r"(?:\s*[:=]\s*|\s+)[^\s,;]{4,}",
    re.IGNORECASE,
)
_HASH_LIKE = re.compile(r"\b[0-9a-f]{32,64}\b", re.IGNORECASE)
_DOMAIN_LIKE = re.compile(
    r"\b(?:www\.)?(?:[a-z0-9-]{1,63}\.)+"
    r"(?:com|net|org|io|tv|me|cn|co|uk|info|biz|cc|ai|jp|de|fr|ca|au|us|"
    r"xyz|top|app|dev|cloud|site|online|live|link|tech|wiki|shop|club|vip|"
    r"local|lan|internal|example)(?:/[^\s]*)?\b"
    r"|\b(?:www\.)?(?:[a-z0-9-]{1,63}\.)+[a-z][a-z0-9-]{1,62}/[^\s]+\b",
    re.IGNORECASE,
)
_EMBEDDED_PATH_LIKE = re.compile(
    r"(?:\b(?:downloads?|users?|home|volumes?|media|mnt|tmp|private|etc|var|opt|data)"
    r"[\\/][^\s]+|[\\/][^\s\\/]+\.(?:mkv|mp4|avi|mov|ts|m2ts|strm|torrent|json|nfo)\b)",
    re.IGNORECASE,
)
_BASIC_AUTH_LIKE = re.compile(r"\b[^\s:/@]+:[^\s@]+@(?:[a-z0-9.-]+)", re.IGNORECASE)
_TOKEN_PREFIX_LIKE = re.compile(
    r"\b(?:gh[pousr]_[a-z0-9_]{20,}|sk-[a-z0-9_-]{20,})\b",
    re.IGNORECASE,
)
_BUSINESS_ID_LIKE = re.compile(
    r"\b(?:tmdb[_ -]?id|file[_ -]?id|task[_ -]?id|source[_ -]?id|operation[_ -]?token)\s*[:=]",
    re.IGNORECASE,
)
_SAFE_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ][0-2]\d:[0-5]\d"
    r"(?::[0-5]\d(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?)?$"
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize_query(value: str) -> str:
    query = normalize_search_query(value)
    if _contains_sensitive_text(query):
        raise ValueError("搜索关键词疑似包含路径、链接、凭据、哈希或业务标识")
    return query


def _contains_sensitive_text(value: str) -> bool:
    return any(
        pattern.search(value)
        for pattern in (
            _URI_LIKE,
            _PATH_LIKE,
            _SECRET_LIKE,
            _HASH_LIKE,
            _DOMAIN_LIKE,
            _EMBEDDED_PATH_LIKE,
            _BASIC_AUTH_LIKE,
            _TOKEN_PREFIX_LIKE,
            _BUSINESS_ID_LIKE,
        )
    )


def _safe_title(value: Any, fallback: str) -> str:
    title = unicodedata.normalize("NFKC", str(value or "")).strip()
    title = " ".join(title.split())
    if not title or _contains_sensitive_text(title):
        return fallback
    return title[:_TITLE_LIMIT]


def _safe_status(value: Any, allowed: set[str]) -> str:
    status = str(value or "").strip().casefold()
    return status[:_STATUS_LIMIT] if status in allowed else "unknown"


def _safe_year(value: Any) -> str:
    year = str(value or "").strip()
    if not re.fullmatch(r"\d{4}", year):
        return ""
    numeric = int(year)
    return year if 1800 <= numeric <= 2200 else ""


def _safe_timestamp(value: Any) -> str:
    timestamp = str(value or "").strip()
    if not timestamp or not _SAFE_TIMESTAMP.fullmatch(timestamp):
        return ""
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return timestamp


def _empty_section(name: str, status: str = "empty") -> dict[str, Any]:
    return {
        "source": name,
        "status": status,
        "returned": 0,
        "items": [],
        "has_more": None if status == "unavailable" else False,
        "next_offset": None,
        "truncated": status == "unavailable",
    }


def _library_section(
    query: str, limit: int = 8, offset: int = 0
) -> tuple[dict[str, Any], bool]:
    try:
        result = search_library(query, limit=limit, offset=offset)
    except Exception as exc:
        logger.warning("工作区媒体检索失败 type=%s", type(exc).__name__)
        return _empty_section("library", "unavailable"), True
    data = result["data"]
    items = [
        {**item, "server_type": source["server_type"]}
        for source in data["sources"]
        for item in source["items"]
    ]
    status = (
        "partial"
        if data["unavailable_sources"] and data["available_sources"]
        else result["status"]
    )
    return {
        "source": "library",
        "status": status,
        "items": items,
        "returned": data["returned"],
        "has_more": data["has_more"],
        "next_offset": data["next_offset"],
        "truncated": data["has_more"] is not False,
        "pagination_scope": "per_server",
        "sources": data["sources"],
    }, bool(data["sources"])


def _database_section(
    name: str,
    query: str,
    reader: Callable[..., dict[str, Any]],
    projector: Callable[[dict[str, Any]], dict[str, Any]],
    limit: int,
    offset: int,
) -> dict[str, Any]:
    try:
        raw = reader(query, limit=limit, offset=offset)
        raw_items = raw.get("items") if isinstance(raw, dict) else []
        assert raw_items is not None
        items = [projector(item) for item in raw_items if isinstance(item, dict)]
        return {
            "source": name,
            "status": "ready" if items else "empty",
            "returned": len(items),
            "truncated": raw["has_more"],
            "has_more": raw["has_more"],
            "next_offset": raw["next_offset"],
            "items": items,
        }
    except Exception as exc:
        logger.warning(
            "Agent 工作区搜索来源不可用 source=%s type=%s", name, type(exc).__name__
        )
        return _empty_section(name, "unavailable")


def _rss_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "rss_item_id": row["rss_item_id"],
        "title": _safe_title(row.get("title"), "RSS 条目"),
        "status": _safe_status(
            row.get("status"),
            {"pending", "submitting", "downloaded", "failed", "skipped"},
        ),
        "processed": bool(row.get("processed")),
        "published_at": _safe_timestamp(row.get("pub_date") or row.get("created_at")),
    }


def _download_item(row: dict[str, Any]) -> dict[str, Any]:
    try:
        progress = max(0.0, min(float(row.get("progress") or 0), 100.0))
    except (TypeError, ValueError, OverflowError):
        progress = 0.0
    return {
        "id": row["id"],
        "record_type": row["record_type"],
        "request_id": row["request_id"],
        "title": _safe_title(row.get("title"), "下载任务"),
        "source": _safe_status(
            row.get("source"), {"qb", "guangya", "both", "download"}
        ),
        "status": _safe_status(
            row.get("status"),
            {
                "pending",
                "submitting",
                "submitted",
                "processing",
                "downloading",
                "success",
                "completed",
                "failed",
                "partial",
            },
        ),
        "progress_percent": round(progress, 2),
        "created_at": _safe_timestamp(row.get("created_at")),
    }


def _organize_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "file_id": row["file_id"],
        "title": _safe_title(row.get("title"), "整理记录"),
        "media_type": _safe_status(
            row.get("media_type"), {"movie", "tv", "series", "episode", "anime"}
        ),
        "year": _safe_year(row.get("year")),
        "season": row.get("season") if isinstance(row.get("season"), int) else None,
        "episode": row.get("episode") if isinstance(row.get("episode"), int) else None,
        "status": _safe_status(
            row.get("status"),
            {
                "planned",
                "success",
                "failed",
                "partial_failed",
                "skipped",
                "interrupted",
                "reorganizing",
                "returning",
                "reverting",
                "deleting",
                "reverted",
                "revert_failed",
                "deleted",
            },
        ),
        "source": _safe_status(row.get("source"), {"guangya", "organize"}),
        "created_at": _safe_timestamp(row.get("created_at")),
    }


def _local_media_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "source_id": row["source_id"],
        "title": _safe_title(row.get("title"), "本地媒体任务"),
        "media_type": _safe_status(
            row.get("media_type"), {"movie", "tv", "series", "episode", "anime"}
        ),
        "year": _safe_year(row.get("year")),
        "status": _safe_status(
            row.get("status"),
            {
                "waiting_stable",
                "recognizing",
                "requires_manual",
                "planned",
                "moving",
                "verifying",
                "refreshing",
                "completed",
                "rolling_back",
                "failed",
            },
        ),
        "trigger": _safe_status(row.get("trigger"), {"qb_completed", "scan", "manual"}),
        "updated_at": _safe_timestamp(row.get("updated_at") or row.get("created_at")),
    }


def search(
    query: str, sections: list[str] | None = None, limit: int = 20, offset: int = 0
) -> dict[str, Any]:
    """按来源独立分页；同名记录不推断为同一任务，partial 不等于已查完。"""
    if not isinstance(query, str):
        raise ValueError("query 必须是字符串")
    query = _normalize_query(query)
    if (
        type(limit) is not int
        or not 1 <= limit <= 50
        or type(offset) is not int
        or offset < 0
    ):
        raise ValueError("limit 必须为1–50，offset必须为非负整数")
    if sections is None:
        sections = list(_SECTION_ORDER)
    if (
        not isinstance(sections, list)
        or not sections
        or len(sections) > len(_SECTION_ORDER)
        or any(not isinstance(name, str) for name in sections)
    ):
        raise ValueError("sections 必须是1–5个来源组成的数组")
    selected = [name.strip().casefold() for name in sections]
    if len(set(selected)) != len(selected) or not set(selected) <= _SECTION_SET:
        raise ValueError("sections 包含不支持或重复的来源")
    loaders = {
        "rss": (db.search_workspace_rss, _rss_item),
        "downloads": (db.search_workspace_downloads, _download_item),
        "organize": (db.search_workspace_organize, _organize_item),
        "local_media": (db.search_workspace_local_media, _local_media_item),
    }
    results, network = [], False
    for name in _SECTION_ORDER:
        if name not in selected:
            continue
        if name == "library":
            result, network = _library_section(query, limit, offset)
        else:
            reader, project = loaders[name]
            result = _database_section(name, query, reader, project, limit, offset)
        result.update(limit=limit, offset=offset)
        results.append(result)
    returned = sum(result["returned"] for result in results)
    unavailable = [
        result["source"] for result in results if result["status"] == "unavailable"
    ]
    partial = [result["source"] for result in results if result["status"] == "partial"]
    available = any(
        result["status"] not in {"unavailable", "not_configured"} for result in results
    )
    status = (
        "unavailable"
        if not available
        else "partial"
        if unavailable or partial
        else "success"
        if returned
        else "empty"
    )
    more = (
        True
        if any(result["has_more"] is True for result in results)
        else False
        if all(result["has_more"] is False for result in results)
        else None
    )
    offsets = [
        result["next_offset"]
        for result in results
        if type(result["next_offset"]) is int
    ]
    return {
        "ok": available,
        "status": status,
        "summary": f"工作区标题匹配：{returned} 项，{status}",
        "data": {
            "query": query,
            "sections": results,
            "returned": returned,
            "limit": limit,
            "offset": offset,
            "has_more": more,
            "next_offset": min(offsets, default=None),
            "pagination_scope": "per_section_and_server",
            "unavailable_sources": unavailable,
            "partial_sources": partial,
            "network_accessed": network,
            "database_accessed": any(name != "library" for name in selected),
            "filesystem_scanned": False,
        },
        "evidence": [
            {
                "source": "workspace_title_index",
                "description": "按标题查询既有业务服务和本地记录；保留真实ID及分页，不扫描媒体文件。",
                "collected_at": _now(),
            }
        ],
        "suggestions": [
            "标题匹配不证明属于同一任务链；逐来源按 has_more/next_offset 继续读取。"
        ],
    }
