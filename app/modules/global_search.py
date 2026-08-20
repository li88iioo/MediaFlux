"""全局搜索聚合：本地媒体、Discovery、RSS、下载与整理日志。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import unicodedata
from urllib.parse import quote_plus, urlencode

from app import config as app_config
from app import database as db
from app.clients.base import MediaItem
from app.discovery.search import get_discovery_search_service
from app.logger import get_logger
from app.routes.discovery_image import encode_poster_token
from app.services import search_media_servers

logger = get_logger(__name__)


def normalize_search_query(value: str) -> str:
    query = unicodedata.normalize("NFKC", str(value or "")).strip()
    if (
        not query
        or len(query) > 120
        or any(unicodedata.category(char).startswith("C") for char in query)
    ):
        raise ValueError("搜索关键词必须为 1 到 120 个可见字符")
    return query


def _type_label(value: str) -> str:
    return {
        "movie": "电影",
        "series": "剧集",
        "episode": "单集",
        "tv": "剧集",
    }.get(str(value or "").strip().lower(), str(value or "媒体"))


def _section(key: str, title: str, target_url: str, items: list[dict], error: str = "") -> dict:
    return {
        "key": key,
        "title": title,
        "target_url": target_url,
        "items": items,
        "error": error,
    }


_LOCAL_MEDIA_SOURCE_LIMIT = 16
_MEDIA_SECTION_LIMIT = 16


def _episode_context(item: MediaItem) -> str:
    parts = [str(item.series_name or "").strip()]
    if item.season_number is not None:
        parts.append(f"第 {item.season_number} 季")
    if item.episode_number is not None:
        parts.append(f"第 {item.episode_number} 集")
    return " · ".join(part for part in parts if part)


def _row_value(row, key: str, default=""):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = default
    return default if value is None else value


def _local_section(query: str) -> dict:
    sources = search_media_servers(query, limit=_LOCAL_MEDIA_SOURCE_LIMIT)
    items: list[dict] = []
    errors = 0
    for source in sources:
        if source.get("error"):
            errors += 1
        server_name = str(source.get("server_name", "") or "媒体服务器")
        provider = str(source.get("server_type", "") or "media").strip().lower()
        for item in source.get("items", []):
            is_episode = str(item.type or "").casefold() == "episode"
            episode_context = _episode_context(item) if is_episode else ""
            title = str(item.name or item.series_name or item.display_name) if is_episode else item.display_name
            type_label = _type_label(item.type)
            resource_term = (item.series_name or item.display_name) if is_episode else item.display_name
            items.append({
                "title": title,
                "subtitle": episode_context or " · ".join(
                    part for part in (server_name, item.year) if part
                ),
                "meta": type_label,
                "url": item.web_url or source.get("web_url", ""),
                "resource_url": f"/discovery?q={quote_plus(resource_term)}",
                "image_url": item.primary_image,
                "external": True,
                "provider": provider,
                "source_label": server_name,
                "type_label": type_label,
                "year": str(item.year or ""),
                "is_local": True,
                "is_episode": is_episode,
                "series_name": str(item.series_name or ""),
                "episode_context": episode_context,
                "overview": str(item.overview or ""),
                "original_title": episode_context if is_episode else (
                    str(item.name or "") if item.name != item.display_name else ""
                ),
                "rating": None,
                "rating_source": "",
            })
    error = "该来源暂时不可用" if sources and errors == len(sources) and not items else ""
    return _section(
        "local", "本地媒体", f"/media/recent?q={quote_plus(query)}", items[:_MEDIA_SECTION_LIMIT], error
    )


def _discovery_section(query: str) -> dict:
    result = get_discovery_search_service().search(query, 1)
    items = []
    for card in result.items[:_MEDIA_SECTION_LIMIT]:
        poster_url = ""
        if card.poster_key:
            poster_url = (
                f"/discovery-poster/{card.provider}/"
                f"{encode_poster_token(card.provider, card.poster_key)}"
            )
        type_label = _type_label(card.media_type)
        detail_url = "/discovery?" + urlencode({
            "detail_provider": card.provider,
            "detail_type": card.media_type,
            "detail_id": card.external_id,
            "return_query": query,
        })
        items.append({
            "title": card.title,
            "subtitle": " · ".join(part for part in (card.provider.upper(), card.year) if part),
            "meta": type_label,
            "url": detail_url,
            "detail_url": detail_url,
            "resource_url": f"/discovery?q={quote_plus(card.title)}",
            "external_id": card.external_id,
            "media_type": card.media_type,
            "image_url": poster_url,
            "external": False,
            "provider": card.provider,
            "source_label": card.provider.upper(),
            "type_label": type_label,
            "year": str(card.year or ""),
            "is_local": False,
            "overview": str(card.overview or ""),
            "original_title": str(card.original_title or ""),
            "rating": card.rating,
            "rating_source": str(card.rating_source or ""),
        })
    error = "该来源暂时不可用" if not items and result.errors else ""
    return _section(
        "discovery", "影视探索", f"/discovery?q={quote_plus(query)}", items, error
    )


def _rss_section(query: str) -> dict:
    folded = query.casefold()
    items = []
    for row in db.list_rss_subscriptions():
        haystack = " ".join(str(_row_value(row, key)) for key in (
            "name", "urls", "parser", "exclude_keywords"
        )).casefold()
        if folded not in haystack:
            continue
        enabled = bool(_row_value(row, "enabled", 0))
        parser = str(_row_value(row, "parser", "RSS") or "RSS").upper()
        action = str(_row_value(row, "action", "subscribe") or "subscribe").lower()
        method = str(_row_value(row, "download_method", "") or "").lower()
        rules = ["自动下载" if action == "download" else "仅订阅"]
        if method:
            rules.append({"qb": "QB", "guangya": "光鸭"}.get(method, method.upper()))
        excluded = str(_row_value(row, "exclude_keywords", "") or "").strip()
        if excluded:
            rules.append(f"排除 {excluded}")
        items.append({
            "title": str(_row_value(row, "name", "未命名订阅")),
            "subtitle": "已启用" if enabled else "已停用",
            "meta": parser,
            "url": "/rss",
            "image_url": "",
            "external": False,
            "provider": parser.lower(),
            "source_label": parser,
            "enabled": enabled,
            "parser": parser,
            "rules": rules,
            "last_refreshed_at": str(_row_value(row, "last_refreshed_at", "") or ""),
        })
        if len(items) >= 8:
            break
    return _section("rss", "RSS 订阅", "/rss", items)


def _progress_percent(value) -> int:
    try:
        progress = float(value or 0)
    except (TypeError, ValueError):
        progress = 0.0
    if progress > 1:
        progress /= 100
    return round(max(0.0, min(progress, 1.0)) * 100)


def _downloads_section(query: str) -> dict:
    rows = db.list_download_logs(keyword=query, limit=8, offset=0)
    items = []
    status_labels = {
        "submitting": "提交中", "submitted": "已提交", "downloading": "下载中",
        "success": "已完成", "completed": "已完成", "failed": "失败",
        "manual_review": "需人工确认", "queued": "等待中",
    }
    source_labels = {"qb": "qBittorrent", "guangya": "光鸭", "guangya_share": "光鸭分享转存"}
    for row in rows:
        source = str(_row_value(row, "source", "download") or "download").lower()
        status = str(_row_value(row, "status", "submitted") or "submitted").lower()
        items.append({
            "title": str(_row_value(row, "title", "未命名任务") or "未命名任务"),
            "subtitle": source_labels.get(source, source.upper()),
            "meta": status_labels.get(status, status),
            "url": f"/downloads?keyword={quote_plus(query)}",
            "image_url": "",
            "external": False,
            "provider": source,
            "source_label": source_labels.get(source, source.upper()),
            "status": status,
            "status_label": status_labels.get(status, status),
            "progress_percent": _progress_percent(_row_value(row, "progress", 0)),
            "path": str(_row_value(row, "path", "") or ""),
            "created_at": str(_row_value(row, "created_at", "") or ""),
            "error": str(_row_value(row, "error", "") or ""),
        })
    return _section(
        "downloads", "下载任务", f"/downloads?keyword={quote_plus(query)}", items
    )


def _logs_section(query: str) -> dict:
    rows = db.list_organize_logs(keyword=query, limit=8, offset=0)
    items = []
    for row in rows:
        title = str(
            _row_value(row, "current_name")
            or _row_value(row, "original_name")
            or _row_value(row, "new_path")
            or "整理记录"
        )
        status = str(_row_value(row, "status", "unknown") or "unknown").lower()
        status_label = {
            "success": "成功", "failed": "失败", "running": "处理中",
            "pending": "等待中", "interrupted": "已中断", "rolled_back": "已回滚",
            "reverted": "已还原", "partial_failed": "部分失败",
            "revert_failed": "回滚失败", "deleted": "已删除",
        }.get(status, status)
        if status in {"failed", "partial_failed", "revert_failed"}:
            tone = "error"
        elif status in {"success", "rolled_back", "reverted", "deleted"}:
            tone = "success"
        else:
            tone = "neutral"
        path = str(_row_value(row, "new_path") or _row_value(row, "original_path"))
        items.append({
            "title": title,
            "subtitle": path,
            "meta": status_label,
            "url": f"/logs?q={quote_plus(query)}",
            "image_url": "",
            "external": False,
            "provider": "organize",
            "source_label": "整理记录",
            "status": status,
            "status_label": status_label,
            "tone": tone,
            "path": path,
            "created_at": str(_row_value(row, "created_at", "") or ""),
        })
    return _section("logs", "整理日志", f"/logs?q={quote_plus(query)}", items)


def build_global_search(value: str) -> dict:
    query = normalize_search_query(value)
    jobs = [
        ("local", lambda: _local_section(query)),
        ("rss", lambda: _rss_section(query)),
        ("downloads", lambda: _downloads_section(query)),
        ("logs", lambda: _logs_section(query)),
    ]
    if app_config.get_bool("DISCOVERY_ENABLED", False):
        jobs.insert(1, ("discovery", lambda: _discovery_section(query)))

    sections_by_key: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="global-search") as pool:
        futures = {key: pool.submit(loader) for key, loader in jobs}
        for key, _loader in jobs:
            try:
                sections_by_key[key] = futures[key].result()
            except Exception as exc:
                logger.warning("全局搜索来源失败 source=%s type=%s", key, type(exc).__name__)
                title = {
                    "local": "本地媒体",
                    "discovery": "影视探索",
                    "rss": "RSS 订阅",
                    "downloads": "下载任务",
                    "logs": "整理日志",
                }[key]
                target = {
                    "local": f"/media/recent?q={quote_plus(query)}",
                    "discovery": f"/discovery?q={quote_plus(query)}",
                    "rss": "/rss",
                    "downloads": f"/downloads?keyword={quote_plus(query)}",
                    "logs": f"/logs?q={quote_plus(query)}",
                }[key]
                sections_by_key[key] = _section(key, title, target, [], "该来源暂时不可用")

    sections = [sections_by_key[key] for key, _loader in jobs]
    section_map = {section["key"]: section for section in sections}
    media_items = list(section_map.get("local", {}).get("items", []))
    media_items.extend(section_map.get("discovery", {}).get("items", []))
    counts = {
        "media": len(media_items),
        "rss": len(section_map.get("rss", {}).get("items", [])),
        "downloads": len(section_map.get("downloads", {}).get("items", [])),
        "logs": len(section_map.get("logs", {}).get("items", [])),
    }
    return {
        "query": query,
        "sections": sections,
        "section_map": section_map,
        "media_items": media_items,
        "top_match": media_items[0] if media_items else None,
        "counts": counts,
        "total_count": sum(counts.values()),
    }
