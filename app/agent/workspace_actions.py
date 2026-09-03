"""Media Agent 的安全工作区标题搜索。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app import database as db
from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolResult
from app.logger import get_logger
from app.services import search_media_servers

logger = get_logger(__name__)

_SECTION_ORDER = ("library", "rss", "downloads", "organize", "local_media")
_SECTION_SET = set(_SECTION_ORDER)
_RESULT_LIMIT = 8
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
    query = unicodedata.normalize("NFKC", value).strip()
    if (
        not query
        or len(query) > 120
        or any(unicodedata.category(char).startswith("C") for char in query)
    ):
        raise AgentToolError("搜索关键词必须为 1 到 120 个可见字符")
    if _contains_sensitive_text(query):
        raise AgentToolError("搜索关键词疑似包含路径、链接、凭据、哈希或业务标识")
    return query


def workspace_search_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    extra = set(arguments) - {"query", "sections"}
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    raw_query = arguments.get("query")
    if not isinstance(raw_query, str):
        raise AgentToolError("query 必须是字符串")
    query = _normalize_query(raw_query)

    raw_sections = arguments.get("sections")
    if raw_sections is None:
        sections = list(_SECTION_ORDER)
    else:
        if (
            not isinstance(raw_sections, list)
            or not raw_sections
            or len(raw_sections) > len(_SECTION_ORDER)
        ):
            raise AgentToolError("sections 必须是 1 到 5 个来源组成的数组")
        sections = []
        for raw in raw_sections:
            if not isinstance(raw, str):
                raise AgentToolError("sections 只能包含来源名称")
            section = raw.strip().casefold()
            if section not in _SECTION_SET:
                raise AgentToolError("sections 包含不支持的来源")
            if section in sections:
                raise AgentToolError("sections 不能包含重复来源")
            sections.append(section)
        sections = [section for section in _SECTION_ORDER if section in sections]
    return {"query": query, "sections": sections}


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
        "truncated": False,
        "items": [],
    }


def _library_section(query: str) -> tuple[dict[str, Any], bool]:
    try:
        sources = search_media_servers(query, limit=_RESULT_LIMIT)
    except Exception as exc:
        logger.warning("Agent 工作区搜索媒体库不可用 type=%s", type(exc).__name__)
        return _empty_section("library", "unavailable"), True
    if not sources:
        return _empty_section("library", "not_configured"), False

    items: list[dict[str, Any]] = []
    available = 0
    unavailable = 0
    for source in sources:
        if source.get("error"):
            unavailable += 1
            continue
        available += 1
        server_type = str(source.get("server_type") or "").casefold()
        if server_type not in {"jellyfin", "emby"}:
            server_type = "media_server"
        for item in source.get("items", []):
            if len(items) >= _RESULT_LIMIT:
                break
            items.append(
                {
                    "title": _safe_title(item.name or item.display_name, "媒体条目"),
                    "media_type": _safe_status(
                        item.type, {"movie", "series", "episode", "season", "video"}
                    ),
                    "year": _safe_year(item.year),
                    "series_name": _safe_title(item.series_name, "")
                    if item.series_name
                    else "",
                    "season": item.season_number
                    if isinstance(item.season_number, int)
                    else None,
                    "episode": item.episode_number
                    if isinstance(item.episode_number, int)
                    else None,
                    "server_type": server_type,
                }
            )
    if available == 0:
        status = "unavailable"
    elif unavailable:
        status = "partial"
    elif items:
        status = "ready"
    else:
        status = "empty"
    return {
        "source": "library",
        "status": status,
        "returned": len(items),
        "truncated": len(items) >= _RESULT_LIMIT,
        "items": items,
    }, True


def _database_section(
    name: str,
    query: str,
    reader: Callable[..., dict[str, object]],
    projector: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    try:
        raw = reader(query, limit=_RESULT_LIMIT)
        raw_items = raw.get("items") if isinstance(raw, dict) else []
        items = [projector(item) for item in raw_items if isinstance(item, dict)]
        return {
            "source": name,
            "status": "ready" if items else "empty",
            "returned": len(items),
            "truncated": bool(raw.get("truncated")) if isinstance(raw, dict) else False,
            "items": items,
        }
    except Exception as exc:
        logger.warning(
            "Agent 工作区搜索来源不可用 source=%s type=%s", name, type(exc).__name__
        )
        return _empty_section(name, "unavailable")


def _rss_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
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


def search_workspace(arguments: dict[str, Any]) -> ToolResult:
    query = arguments["query"]
    selected = arguments["sections"]
    sections: list[dict[str, Any]] = []
    network_accessed = False

    for name in _SECTION_ORDER:
        if name not in selected:
            continue
        if name == "library":
            section, attempted = _library_section(query)
            network_accessed = network_accessed or attempted
        elif name == "rss":
            section = _database_section(
                name, query, db.search_agent_workspace_rss, _rss_item
            )
        elif name == "downloads":
            section = _database_section(
                name, query, db.search_agent_workspace_downloads, _download_item
            )
        elif name == "organize":
            section = _database_section(
                name, query, db.search_agent_workspace_organize, _organize_item
            )
        else:
            section = _database_section(
                name, query, db.search_agent_workspace_local_media, _local_media_item
            )
        sections.append(section)

    returned = sum(int(section["returned"]) for section in sections)
    unavailable = sum(section["status"] == "unavailable" for section in sections)
    available = sum(
        section["status"] in {"ready", "empty", "partial"} for section in sections
    )
    if available == 0:
        ok = False
        status = "unavailable"
        summary = "工作区数据源暂时不可用"
    elif returned:
        ok = True
        status = "partial" if unavailable else "success"
        summary = f"在工作区中找到 {returned} 项标题匹配记录"
    else:
        ok = True
        status = "partial" if unavailable else "empty"
        summary = "工作区中没有找到标题匹配记录"

    suggestions = ["结果仅按标题匹配，不代表各来源记录属于同一条确定任务链。"]
    if unavailable:
        suggestions.append(f"有 {unavailable} 个数据源暂时不可用，可稍后重试。")
    if not returned:
        suggestions.append("可尝试中文名、原名或去掉季集编号后重新搜索。")

    return ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data={
            "query": query,
            "returned": returned,
            "network_accessed": network_accessed,
            "database_accessed": any(name != "library" for name in selected),
            "filesystem_scanned": False,
            "sections": sections,
        },
        evidence=[
            Evidence(
                "workspace_title_index",
                "仅按标题查询媒体库与本地工作流状态；未读取或返回路径、URL、凭据、哈希、业务标识或错误正文，未扫描媒体文件系统。",
                _now(),
            )
        ],
        suggestions=suggestions,
    )
