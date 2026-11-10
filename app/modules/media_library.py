"""指定媒体服务器的库读取和精准刷新；使用真实库 ID，共用现有客户端。"""
from __future__ import annotations

import math
import re
import unicodedata
from contextlib import contextmanager
from collections.abc import Callable
from types import EllipsisType
from typing import Any

from app.clients.base import normalize_explicit_media_user_id
from app.modules.library_quality import LibraryQualityError
from app.modules.media_server_profiles import list_configured_profiles


class MediaLibraryError(ValueError):
    def __init__(self, message: str, *, code: str = "precondition_failed", execution_started: bool = False):
        super().__init__(message)
        self.code = code
        self.execution_started = execution_started


def _profile(server):
    if server not in {"auto", "jellyfin", "emby"}:
        raise MediaLibraryError("server 必须是 auto/jellyfin/emby", code="invalid_arguments")
    profiles = [p for p in list_configured_profiles() if p.enabled and p.configured
                and (server == "auto" or p.server_type == server)]
    if not profiles:
        raise MediaLibraryError("媒体服务器尚未启用或配置不完整", code="provider_not_configured")
    if len(profiles) != 1:
        raise MediaLibraryError("有多个可用媒体服务器，请指定 jellyfin 或 emby")
    return profiles[0]


@contextmanager
def _client(server):
    profile = _profile(server)
    try:
        with profile.create_client() as client:
            yield profile, client
    except (MediaLibraryError, LibraryQualityError):
        raise
    except InterruptedError as exc:
        raise MediaLibraryError("媒体库查询已停止", code="cancelled") from exc
    except TimeoutError as exc:
        raise MediaLibraryError("媒体服务器请求超时", code="upstream_timeout") from exc
    except Exception as exc:
        # 上游错误不是空列表；凭据和请求 URL 不进入对话。
        raise MediaLibraryError("媒体服务器当前不可用", code="provider_unavailable") from exc


def _folders(client):
    return [{"id": str(f["id"]), "name": str(f.get("name") or ""),
             "collection_type": str(f.get("collection_type") or "mixed")}
            for f in client.list_virtual_folders() if str(f.get("id") or "").strip()]


def _library(client, library_id, library_name=""):
    if not isinstance(library_id, str) or not isinstance(library_name, str) or (library_id and library_name):
        raise MediaLibraryError("library_id 与 library_name 只能指定一个字符串", code="invalid_arguments")
    if not library_id and not library_name:
        raise MediaLibraryError("必须指定媒体库", code="invalid_arguments")
    matches = [f for f in _folders(client) if (f["id"] == library_id if library_id
               else f["name"].strip().casefold() == library_name.strip().casefold())]
    if len(matches) != 1:
        raise MediaLibraryError("指定媒体库不存在或名称不唯一；本次未查询或刷新全库")
    return matches[0]


def libraries(server="auto") -> dict:
    with _client(server) as (profile, client):
        folders = _folders(client)
        return {"server": profile.server_type, "server_label": profile.label,
                "libraries": folders, "count": len(folders)}


def recent(server="auto", *, limit=20, library_id="", library_name="", media_type="all") -> dict:
    if type(limit) is not int or not 1 <= limit <= 200 or media_type not in {"all", "movie", "tv", "episode"}:
        raise MediaLibraryError("limit 必须为 1–200 的整数；media_type 必须为 all/movie/tv/episode", code="invalid_arguments")
    if not isinstance(library_id, str) or not isinstance(library_name, str):
        raise MediaLibraryError("媒体库标识和名称必须是字符串", code="invalid_arguments")
    from app.modules.media_links import media_open_url_resolver
    from app.services import _library_search_item

    with _client(server) as (profile, client):
        folder = _library(client, library_id, library_name) if library_id or library_name else None
        query: dict[str, Any] = {"limit": limit}
        if folder:
            query["parent_id"] = folder["id"]
        if media_type != "all":
            query["media_type"] = media_type
        items = client.recent_media(**query)
        resolve = media_open_url_resolver(server_type=profile.server_type, server_url=profile.url)
        rows = [{**_library_search_item(item), "open_url": resolve(item.web_url or item.series_web_url)} for item in items]
        scope: dict[str, Any] = {"kind": "library" if folder else "all", "media_type": media_type}
        if folder:
            scope["library"] = folder
        return {"server": profile.server_type, "server_label": profile.label, "scope": scope,
                "scope_note": f"仅查询媒体库：{folder['name']}" if folder else "查询全部可访问媒体库；未按媒体库排除动漫",
                "count": len(rows), "limit": limit, "max_scanned_items": 2000,
                "scope_note_sampling": "最近入库最多扫描2000个条目后按作品去重；此列表不代表全库库存或总数",
                "granularity": "episode" if media_type == "episode" else "work", "items": rows}


def playback_user(profile, client) -> tuple[str, str]:
    configured = (profile.user_id or "").strip()
    try:
        return (normalize_explicit_media_user_id(configured or client._user_id()),
                "配置用户" if configured else "服务器默认用户")
    except (ValueError, RuntimeError) as exc:
        raise MediaLibraryError("媒体服务器没有可用于观看数据的用户", code="provider_user_required") from exc


def playback_scope(profile, user_id, selection) -> dict:
    return {"server": profile.server_type, "server_label": profile.label,
            "user_selection": selection,
            "scope": {"kind": "user", "profile": profile.source, "user_id": user_id}}


def _watching(server, limit, *, history: bool) -> dict:
    maximum = 50 if history else 20
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise MediaLibraryError(f"limit 必须为 1–{maximum} 的整数", code="invalid_arguments")
    from collections import Counter

    from app.modules.media_links import media_open_url_resolver
    from app.services import _library_search_item

    server = str(server or "auto").strip().casefold()
    with _client(server) as (profile, client):
        user_id, selection = playback_user(profile, client)
        if history:
            items = client.enrich_media_genres(user_id, client.recently_played(user_id, limit=limit))
        else:
            items = client.continue_watching(user_id, limit=limit)
        resolve = media_open_url_resolver(server_type=profile.server_type, server_url=profile.url)
        rows = [{**_library_search_item(item), "open_url": resolve(item.web_url or item.series_web_url)}
                for item in items]
        result = {**playback_scope(profile, user_id, selection), "status": "success" if rows else "empty",
                  "history_kind": "最近播放" if history else "继续观看", "items": rows,
                  "count": len(rows), "returned": len(rows), "limit": limit,
                  "total": None, "has_more": None, "complete": None, "partial": False,
                  "scope_note": "当前用户的有界观看记录；客户端不提供总数或续页，返回数不代表全部记录数"}
        if history:
            result["preference_signals"] = {
                "recent_titles": [name for name, _ in Counter(item.display_name.strip() for item in items if item.display_name.strip()).most_common()],
                "top_genres": [name for name, _ in Counter(g.strip() for item in items for g in item.genres if g.strip()).most_common()],
                "media_types": [name for name, _ in Counter(item.type.strip().casefold() for item in items if item.type.strip()).most_common()],
            }
        return result


def history(server="auto", *, limit=8) -> dict:
    """真实播放事件/DatePlayed；有界客户端未提供总数，不能用返回数推算完整历史。"""
    return _watching(server, limit, history=True)


def resume(server="auto", *, limit=8) -> dict:
    """当前配置用户或默认用户的继续观看列表；不作为播放历史。"""
    return _watching(server, limit, history=False)


def visible_text(value: object, *, limit: int) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        return ""
    return " ".join(normalized.split())[:limit]


def _term_list(value: object, *, name: str, maximum: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise MediaLibraryError(f"{name} 必须是最多 {maximum} 项的数组", code="invalid_arguments")
    result: list[str] = []
    for raw in value:
        term = visible_text(raw, limit=80)
        alternatives = [item.strip() for item in term.split("|")]
        if not term or any(not item for item in alternatives):
            raise MediaLibraryError(f"{name} 中包含无效匹配词", code="invalid_arguments")
        if term not in result:
            result.append(term)
    return result


def recommend(server="auto", *, media_type="any", must_match: list[str] | EllipsisType = ...,
              prefer: list[str] | EllipsisType = ..., exclude: list[str] | EllipsisType = ...,
              min_rating: float | EllipsisType = ..., exclude_played: bool | EllipsisType = ...,
              limit=8, preferences: dict | None = None, cancelled: Callable[[], bool] | None = None) -> dict:
    """一次读取用户历史与分页库存，再执行确定性排名；只在选择结果时限制条数。"""
    from app.modules.media_links import media_open_url_resolver
    from app.modules.media_recommendation import rank_local_recommendations

    preferences = preferences or {}
    prefer = preferences.get("preferred_genres", []) if prefer is Ellipsis else prefer
    exclude = preferences.get("excluded_genres", []) if exclude is Ellipsis else exclude
    min_rating = preferences.get("min_rating", 0) if min_rating is Ellipsis else min_rating
    exclude_played = preferences.get("exclude_played", True) if exclude_played is Ellipsis else exclude_played
    server = str(server or "auto").strip().casefold()
    if server not in {"auto", "jellyfin", "emby"}:
        raise MediaLibraryError("server 仅支持 auto、jellyfin 或 emby", code="invalid_arguments")
    media_type = str(media_type or "any").strip().casefold()
    if media_type not in {"any", "movie", "tv"}:
        raise MediaLibraryError("media_type 仅支持 any、movie 或 tv", code="invalid_arguments")
    if isinstance(min_rating, bool) or not isinstance(min_rating, (int, float)):
        raise MediaLibraryError("min_rating 必须是数字", code="invalid_arguments")
    if not 0 <= min_rating <= 10 or not math.isfinite(min_rating):
        raise MediaLibraryError("min_rating 必须在 0 到 10 之间", code="invalid_arguments")
    if not isinstance(exclude_played, bool):
        raise MediaLibraryError("exclude_played 必须是布尔值", code="invalid_arguments")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise MediaLibraryError("limit 必须是 1 到 20 的整数", code="invalid_arguments")
    must_match = _term_list([] if must_match is Ellipsis else must_match, name="must_match", maximum=6)
    prefer = _term_list(prefer, name="prefer", maximum=12)
    exclude = _term_list(exclude, name="exclude", maximum=8)
    normalized = {
        "media_type": media_type,
        "must_match": must_match,
        "prefer": prefer,
        "exclude": exclude,
        "min_rating": float(min_rating),
        "exclude_played": exclude_played,
        "limit": limit,
    }

    if server == "auto" and preferences.get("preferred_server", "any") != "any":
        server = preferences["preferred_server"]
    if cancelled and cancelled():
        raise MediaLibraryError("媒体库查询已停止", code="cancelled")
    with _client(server) as (profile, client):
        user_id, selection = playback_user(profile, client)
        played = client.enrich_media_genres(user_id, client.recently_played(user_id, limit=50))
        inventory = client.list_recommendation_candidates(user_id, media_type=media_type,
                                                         max_items=5_000, page_size=200, cancelled=cancelled)
        result = rank_local_recommendations(inventory.candidates, played, must_match=must_match,
                                           prefer=prefer, exclude=exclude, min_rating=float(min_rating),
                                           exclude_played=exclude_played, limit=limit)
        resolve = media_open_url_resolver(server_type=profile.server_type, server_url=profile.url)
        for item in result["items"]:
            item["open_url"] = resolve(client.media_web_url(item["id"]))
        partial = inventory.truncated or inventory.total is None
        result.update(playback_scope(profile, user_id, selection))
        result["scope"].update(media_type=media_type, max_items=5_000, page_size=200)
        result["history"].update(total=None, complete=None, limit=50)
        result.update(status="partial" if partial else ("success" if result["count"] else "empty"),
                      partial=partial, inventory_total=inventory.total, inventory_truncated=inventory.truncated,
                      inventory_total_exact=not partial,
                      total=None if partial else result["matched_count"],
                      criteria=normalized, limit=limit, returned=result["count"],
                      matched_count_scope="scanned_candidates",
                      scope_note="排名与排除计数仅覆盖本次读取的库存；库存截断时inventory_total可能只是下界；观看历史最多50条，不代表完整历史")
        return result


def quality(server, library_id, *, start_index=0, limit=20, min_resolution=1080, subtitle_language="chinese") -> dict:
    from app.modules.library_quality import inspect_library_quality

    with _client(server) as (profile, client):
        folder = _library(client, library_id)
        try:
            user_id = normalize_explicit_media_user_id(profile.user_id or client._user_id())
        except (ValueError, RuntimeError) as exc:
            raise MediaLibraryError("媒体服务器没有可用于检查的用户", code="provider_user_required") from exc
        result = inspect_library_quality(client, user_id, library_id=folder["id"], start_index=start_index,
                                       limit=limit, min_resolution=min_resolution, subtitle_language=subtitle_language,
                                       server_label=profile.label, source=f"{profile.server_type}_api")
        result["data"].update(server=profile.server_type, library=folder)
        return result


def _id(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise MediaLibraryError("必须提供真实媒体、列表或成员 ID", code="invalid_arguments")
    return value


def _item(client, user_id, item_id):
    row = client._request(f"/Users/{_id(user_id)}/Items/{_id(item_id)}")
    if not isinstance(row, dict) or row.get("Id") != item_id or not row.get("Name"):
        raise MediaLibraryError("媒体条目已不存在或返回无效", code="confirmation_stale")
    return row



def series_episodes(server, series_id, *, max_episodes=2000, include_specials=False) -> dict:
    """真实剧集 ID 的完整客户端库存；保留未知总数、合并集展开和特别篇规则。"""
    _id(series_id)
    if type(max_episodes) is not int or not 1 <= max_episodes <= 2000 or type(include_specials) is not bool:
        raise MediaLibraryError("max_episodes 必须为1–2000整数；include_specials 必须为布尔值", code="invalid_arguments")
    with _client(server) as (profile, client):
        inventory = client.list_series_episode_inventory(series_id, max_episodes=max_episodes, include_specials=include_specials)
        complete = not inventory.truncated and not inventory.ignored_unknown and inventory.total is not None
        return {"ok": True, "status": "success" if complete else "partial", "data": {
            "server": profile.server_type, "series_id": series_id,
            "episodes": [{"season": season, "episode": episode} for season, episode in inventory.episodes],
            "count": len(inventory.episodes), "total": inventory.total, "truncated": inventory.truncated,
            "inventory_complete": complete, "max_episodes": max_episodes, "include_specials": include_specials,
            "ignored_specials": inventory.ignored_specials, "ignored_unknown": inventory.ignored_unknown}}


def _refresh_target(profile, client, library_id, item_id) -> tuple[str, dict[str, Any]]:
    if bool(library_id) == bool(item_id):
        raise MediaLibraryError("必须指定一个真实 library_id 或 item_id；不支持全库刷新", code="invalid_arguments")
    _id(library_id or item_id)
    if library_id:
        return "library", _library(client, library_id)
    user_id, _ = playback_user(profile, client)
    row = _item(client, user_id, item_id)
    if row.get("Type") not in {"Movie", "Series", "Season", "Episode"}:
        raise MediaLibraryError("仅支持真实电影、剧集、季或单集；不支持全库/目录刷新", code="invalid_arguments")
    return "item", {"id": item_id, "name": row["Name"], "type": row["Type"],
                    "year": row.get("ProductionYear"), "tmdb_id": client._provider_tmdb_id(row.get("ProviderIds"))}


def prepare_refresh(server, library_id="", *, item_id="", mode="scan", replace_images=False,
                    regenerate_trickplay=False) -> dict:
    from app.clients.base import media_refresh_parameters

    options = dict(mode=mode, replace_images=replace_images, regenerate_trickplay=regenerate_trickplay)
    with _client(server) as (profile, client):
        try:
            media_refresh_parameters(**options, server=profile.server_type)
        except ValueError as exc:
            raise MediaLibraryError(str(exc), code="invalid_arguments") from exc
        scope, target = _refresh_target(profile, client, library_id, item_id)
        return {"snapshot": {"profile_revision": profile.fingerprint(), scope: target, "options": options},
                "server": profile.server_type, scope: target, "scope": scope, **options}


def refresh(server, library_id="", *, item_id="", mode="scan", replace_images=False,
            regenerate_trickplay=False, expected_snapshot, check_cancelled=None) -> dict:
    started = False
    try:
        with _client(server) as (profile, client):
            scope, target = _refresh_target(profile, client, library_id, item_id)
            options = dict(mode=mode, replace_images=replace_images, regenerate_trickplay=regenerate_trickplay)
            if {"profile_revision": profile.fingerprint(), scope: target, "options": options} != expected_snapshot:
                raise MediaLibraryError("服务器配置或目标已变化，请重新预检", code="confirmation_stale")
            if check_cancelled:
                check_cancelled()
            started = True
            accepted = client.refresh_library(target["id"], **options)
            if not accepted:
                raise MediaLibraryError("媒体服务器未确认接受刷新请求", code="outcome_unknown")
            result = {"ok": True, "status": "accepted", "summary": "媒体服务器已接受指定目标刷新，尚未完成",
                      "data": {"accepted": True, "server": profile.server_type, scope + "_id": target["id"],
                               "scope": scope, "target": target, **options,
                               "verification": "accepted", "global_refresh": False}}
        return result
    except Exception as exc:
        if started:
            raise MediaLibraryError("刷新结果待核对，请勿重复提交", code="outcome_unknown", execution_started=True) from exc
        raise
