"""FastAPI 页面路由：看板 / 配置 / 各功能页。"""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import config
from app.agent.feature_gate import is_agent_enabled
from app.modules.global_search import build_global_search, normalize_search_query
from app.services import (
    build_automation_summary,
    build_recent_media,
    get_cached_dashboards_or_stubs,
    get_media_server_urls,
)
from app.web import render_template, require_page_login

router = APIRouter()


def _page(request: Request, template: str, active: str, **context):
    redirect = require_page_login(request)
    if redirect:
        return redirect
    context.setdefault("discovery_enabled", config.get_bool("DISCOVERY_ENABLED"))
    return render_template(request, template, active=active, **context)


@router.get("/", name="pages.dashboard")
def dashboard(request: Request):
    redirect = require_page_login(request)
    if redirect:
        return redirect
    dashboards, is_cached = get_cached_dashboards_or_stubs()
    return render_template(
        request,
        "dashboard.html",
        dashboards=dashboards,
        is_cached=is_cached,
        automation=build_automation_summary(),
        server_urls=get_media_server_urls(),
        active="dashboard",
        discovery_enabled=config.get_bool("DISCOVERY_ENABLED"),
    )


@router.get("/agent", name="pages.agent")
def agent(request: Request):
    redirect = require_page_login(request)
    if redirect:
        return redirect
    if not is_agent_enabled():
        return RedirectResponse("/settings?agent_disabled=1#agent", status_code=303)
    return _page(request, "agent.html", "agent")


@router.get("/search", name="pages.global_search")
def global_search(request: Request):
    redirect = require_page_login(request)
    if redirect:
        return redirect
    raw_query = request.query_params.get("q", "")
    result = {"query": "", "sections": []}
    error = ""
    status_code = 200
    if raw_query:
        try:
            query = normalize_search_query(raw_query)
            result = build_global_search(query)
        except ValueError as exc:
            error = str(exc)
            status_code = 400
    return render_template(
        request,
        "global_search.html",
        status_code=status_code,
        active="global_search",
        result=result,
        search_error=error,
        discovery_enabled=config.get_bool("DISCOVERY_ENABLED"),
        resource_results_enabled=config.get_bool(
            "DISCOVERY_RESOURCE_RESULTS_ENABLED",
            True,
        ),
    )


@router.get("/media/recent", name="pages.media_recent")
def media_recent(request: Request):
    redirect = require_page_login(request)
    if redirect:
        return redirect
    selected_server = str(request.query_params.get("server", "all") or "all").strip().lower()
    selected_type = str(request.query_params.get("type", "all") or "all").strip().lower()
    raw_query = str(request.query_params.get("q", "") or "")
    if selected_server not in {"all", "jellyfin", "emby"}:
        raise HTTPException(status_code=400, detail="媒体服务器筛选无效")
    if selected_type not in {"all", "movie", "series", "episode"}:
        raise HTTPException(status_code=400, detail="媒体类型筛选无效")
    query = ""
    if raw_query:
        try:
            query = normalize_search_query(raw_query)
        except ValueError as exc:
            return render_template(
                request, "media_recent.html", status_code=400, active="media_recent",
                sources=[], items=[], selected_server=selected_server,
                selected_type=selected_type, query=raw_query, errors=[],
                search_error=str(exc), has_sources=False, all_sources_failed=False,
                discovery_enabled=config.get_bool("DISCOVERY_ENABLED"),
            )

    sources = build_recent_media()
    filtered_sources = [
        source for source in sources
        if selected_server == "all" or source.get("server_type") == selected_server
    ]
    items = []
    folded_query = query.casefold()
    for source in filtered_sources:
        for item in source.get("items", []):
            item_type = str(getattr(item, "type", "") or "").strip().lower()
            normalized_type = {"tv": "series"}.get(item_type, item_type)
            if selected_type != "all" and normalized_type != selected_type:
                continue
            haystack = " ".join((
                str(getattr(item, "display_name", "") or ""),
                str(getattr(item, "name", "") or ""),
                str(getattr(item, "series_name", "") or ""),
                str(getattr(item, "year", "") or ""),
            )).casefold()
            if folded_query and folded_query not in haystack:
                continue
            items.append({"source": source, "media": item})
    items.sort(
        key=lambda entry: str(getattr(entry["media"], "date_added", "") or ""),
        reverse=True,
    )
    return render_template(
        request,
        "media_recent.html",
        active="media_recent",
        sources=sources,
        items=items,
        selected_server=selected_server,
        selected_type=selected_type,
        query=query,
        errors=[source for source in filtered_sources if source.get("error")],
        search_error="",
        has_sources=bool(sources),
        all_sources_failed=bool(filtered_sources) and all(source.get("error") for source in filtered_sources),
        discovery_enabled=config.get_bool("DISCOVERY_ENABLED"),
    )


@router.get("/guangya", name="pages.guangya")
def guangya(request: Request):
    return _page(request, "guangya.html", "guangya")


def _offline_transfer_defaults() -> dict[str, object]:
    """返回离线转存页首帧配置，避免异步回填时控件和标签横向跳动。"""

    def split_tags(key: str) -> list[str]:
        raw = str(config.get(key, "") or "")
        return [item.strip() for item in re.split(r"[,，\r\n]+", raw) if item.strip()]

    return {
        "offline_initial": {
            "magnet_enabled": config.get_bool("OFFLINE_MAGNET_ENABLED", True),
            "ed2k_enabled": config.get_bool("OFFLINE_ED2K_ENABLED", True),
            "http_enabled": config.get_bool("OFFLINE_HTTP_ENABLED", False),
            "target_dir": str(config.get("OFFLINE_TARGET_DIR", "") or "").strip(),
            "target_dir_name": str(
                config.get("OFFLINE_TARGET_DIR_NAME", "") or ""
            ).strip(),
            "secondary_enabled": config.get_bool("OFFLINE_SECONDARY_ENABLED", False),
            "secondary_dir": str(
                config.get("OFFLINE_SECONDARY_DIR", "") or ""
            ).strip(),
            "secondary_dir_name": str(
                config.get("OFFLINE_SECONDARY_DIR_NAME", "") or ""
            ).strip(),
            "secondary_keywords": split_tags("OFFLINE_SECONDARY_KEYWORDS"),
            "exclude_keywords": split_tags("OFFLINE_EXCLUDE_KEYWORDS"),
            "min_file_mb": max(0, config.get_int("OFFLINE_MIN_FILE_MB", 0)),
            "allowed_exts": split_tags("OFFLINE_ALLOWED_EXTS"),
        }
    }


@router.get("/guangya/offline", name="pages.guangya_offline")
def guangya_offline(request: Request):
    return _page(
        request,
        "guangya_offline.html",
        "guangya_offline",
        **_offline_transfer_defaults(),
    )


@router.get("/guangya/strm", name="pages.guangya_strm")
def guangya_strm(request: Request):
    return _page(request, "guangya_strm.html", "guangya_strm")


@router.get("/guangya/media-proxy", name="pages.media_proxy")
def media_proxy(request: Request):
    return _page(request, "media_proxy.html", "media_proxy")


def _organize_execute_defaults() -> dict[str, object]:
    """返回整理执行页首帧所需目录，避免异步配置加载前闪出错误空态。"""
    raw_sources = str(config.get("GY_ORGANIZE_SOURCE_DIRS", "") or "").strip()
    try:
        decoded = json.loads(raw_sources) if raw_sources else []
    except (TypeError, ValueError):
        decoded = []
    if not isinstance(decoded, list):
        decoded = []

    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(decoded):
        if isinstance(item, str):
            source_id = item.strip()
            source_name = f"源目录{index + 1}"
        elif isinstance(item, dict):
            source_id = str(item.get("id", "") or "").strip()
            source_name = str(item.get("name", "") or "").strip()
        else:
            continue
        if not source_id or source_id == "0" or source_id in seen:
            continue
        seen.add(source_id)
        sources.append({
            "id": source_id,
            "name": source_name or f"源目录{len(sources) + 1}",
        })

    return {
        "organize_initial_sources": sources,
        "organize_initial_target_id": str(
            config.get("GY_ORGANIZE_TARGET_DIR", "") or ""
        ).strip(),
        "organize_initial_target_name": str(
            config.get("GY_ORGANIZE_TARGET_DIR_NAME", "") or ""
        ).strip(),
    }


def _organize_extension_defaults() -> dict[str, list[str]]:
    from app.modules.organize import (
        DEFAULT_ORGANIZE_METADATA_EXTS,
        DEFAULT_ORGANIZE_VIDEO_EXTS,
    )

    def current_extensions(key: str, defaults: tuple[str, ...]) -> list[str]:
        raw = str(config.get(key, "") or "").strip()
        if not raw:
            return list(defaults)
        values: list[str] = []
        for token in re.split(r"[,，\s]+", raw):
            value = token.strip().lower().lstrip(".")
            if value.isalnum() and 1 <= len(value) <= 10 and value not in values:
                values.append(value)
        return values or list(defaults)

    return {
        "organize_video_exts": current_extensions(
            "GY_ORGANIZE_VIDEO_EXTS", DEFAULT_ORGANIZE_VIDEO_EXTS
        ),
        "organize_metadata_exts": current_extensions(
            "GY_ORGANIZE_METADATA_EXTS", DEFAULT_ORGANIZE_METADATA_EXTS
        ),
        "organize_nsfw_enabled": config.get_bool("GY_ORGANIZE_NSFW_ENABLED", False),
    }


@router.get("/organize", name="pages.organize")
def organize(request: Request):
    return _page(
        request,
        "organize.html",
        "organize",
        organize_view="execute",
        **_organize_extension_defaults(),
        **_organize_execute_defaults(),
    )


@router.get("/organize-rules", name="pages.organize_rules")
def organize_rules(request: Request):
    return _page(
        request, "organize.html", "organize_rules", organize_view="rules",
        **_organize_extension_defaults(),
    )


@router.get("/guangya/more", name="pages.guangya_more")
def guangya_more(request: Request, view: str = "share"):
    more_view = "gcid" if view == "gcid" else "share"
    return _page(request, "guangya_more.html", "guangya_more", more_view=more_view)


@router.get("/rss", name="pages.rss")
def rss(request: Request):
    return _page(
        request,
        "rss.html",
        "rss",
        resource_results_enabled=config.get_bool(
            "DISCOVERY_RESOURCE_RESULTS_ENABLED",
            True,
        ),
    )


@router.get("/downloads", name="pages.downloads")
def downloads(request: Request):
    return _page(request, "downloads.html", "downloads")

@router.get("/local-media", name="pages.local_media")
def local_media(request: Request):
    return _page(request, "local_media.html", "local_media")


@router.get("/media-libraries", name="pages.media_libraries")
def media_libraries(request: Request):
    return _page(request, "media_libraries.html", "media_libraries")


@router.get("/discovery", name="pages.discovery")
def discovery(request: Request):
    if not config.get_bool("DISCOVERY_ENABLED"):
        raise HTTPException(status_code=404, detail="not found")
    return _page(
        request,
        "discovery.html",
        "discovery",
        resource_results_enabled=config.get_bool(
            "DISCOVERY_RESOURCE_RESULTS_ENABLED",
            True,
        ),
    )


@router.get("/tools", name="pages.tools", include_in_schema=False)
def tools(request: Request):
    """兼容旧书签；原工具页能力已经归入正式业务页面。"""
    redirect = require_page_login(request)
    if redirect:
        return redirect
    return RedirectResponse("/settings#metadata", status_code=308)


@router.get("/logs", name="pages.logs")
def logs(request: Request):
    return _page(request, "logs.html", "logs")


@router.get("/settings", name="pages.settings")
def settings(request: Request):
    return _page(
        request,
        "settings.html",
        "settings",
        resource_results_enabled=config.get_bool(
            "DISCOVERY_RESOURCE_RESULTS_ENABLED",
            True,
        ),
    )
