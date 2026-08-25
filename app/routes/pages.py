"""FastAPI 页面路由：看板 / 配置 / 各功能页。"""
from __future__ import annotations

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
    context.setdefault("discovery_enabled", config.get_bool("DISCOVERY_ENABLED", False))
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
        discovery_enabled=config.get_bool("DISCOVERY_ENABLED", False),
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
        discovery_enabled=config.get_bool("DISCOVERY_ENABLED", False),
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
                discovery_enabled=config.get_bool("DISCOVERY_ENABLED", False),
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
        discovery_enabled=config.get_bool("DISCOVERY_ENABLED", False),
    )


@router.get("/guangya", name="pages.guangya")
def guangya(request: Request):
    return _page(request, "guangya.html", "guangya")


@router.get("/guangya/offline", name="pages.guangya_offline")
def guangya_offline(request: Request):
    return _page(request, "guangya_offline.html", "guangya_offline")


@router.get("/guangya/strm", name="pages.guangya_strm")
def guangya_strm(request: Request):
    return _page(request, "guangya_strm.html", "guangya_strm")


@router.get("/guangya/media-proxy", name="pages.media_proxy")
def media_proxy(request: Request):
    return _page(request, "media_proxy.html", "media_proxy")


def _organize_extension_defaults() -> dict[str, list[str]]:
    from app.modules.organize import (
        DEFAULT_ORGANIZE_METADATA_EXTS, DEFAULT_ORGANIZE_VIDEO_EXTS,
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
        request, "organize.html", "organize", organize_view="execute",
        **_organize_extension_defaults(),
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
    if not config.get_bool("DISCOVERY_ENABLED", False):
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


@router.get("/tools", name="pages.tools")
def tools(request: Request):
    return _page(request, "tools.html", "tools")


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
