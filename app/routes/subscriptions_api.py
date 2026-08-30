"""统一订阅中心 API：媒体追更与现有 RSS 订阅聚合。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Query, Request

from app import database as db
from app.discovery.models import ProviderError
from app.discovery.service import get_discovery_service
from app.logger import get_logger
from app.modules.media_subscription_scheduler import get_media_subscription_scheduler
from app.modules.media_subscriptions import MediaSubscriptionError, get_media_subscription_service
from app.routes.discovery_image import encode_poster_token
from app.web import api_error, api_response, require_api_login

logger = get_logger(__name__)
router = APIRouter(prefix="/api/subscriptions")
_ALLOWED_LIST_STATUS = {"", "new", "checking", "satisfied", "missing", "inconclusive", "error", "paused"}
_ALLOWED_PROVIDERS = {"tmdb", "douban", "bangumi"}
_ALLOWED_MEDIA_TYPES = {"movie", "tv"}
_WATCHLIST_SUBSCRIPTION_FIELDS = {
    "provider", "external_id", "media_type", "title", "year", "tmdb_id",
    "monitor_mode", "seasons", "include_specials", "action", "download_target",
    "sites", "check_interval_minutes", "enabled",
}


def _service_error(exc: MediaSubscriptionError):
    return api_response({"error": str(exc), "code": exc.code}, exc.status_code)


def _wake_scheduler() -> None:
    try:
        get_media_subscription_scheduler().reload()
    except Exception as exc:
        logger.warning("媒体订阅调度器唤醒失败 type=%s", type(exc).__name__)


def _body(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MediaSubscriptionError("请求参数必须是 JSON 对象")
    return value


def _optional_bool(value: str) -> bool | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise MediaSubscriptionError("enabled 查询参数无效")


def _watchlist_row(
    row: dict[str, Any],
    subscription_map: dict[tuple[str, str], dict[str, Any]],
    external_id_map: dict[tuple[str, str, str], Any],
) -> dict[str, Any]:
    provider = str(row.get("provider") or "")
    external_id = str(row.get("external_id") or "")
    media_type = str(row.get("media_type") or "")
    tmdb_id = external_id if provider == "tmdb" else ""
    if not tmdb_id:
        mapping = external_id_map.get((provider, external_id, media_type))
        if mapping is not None and bool(mapping["confirmed"]):
            tmdb_id = str(mapping["tmdb_id"] or "")
    subscription = subscription_map.get((tmdb_id, media_type)) if tmdb_id else None
    poster_key = str(row.get("poster_key") or "")
    poster_url = (
        f"/discovery-poster/{provider}/{encode_poster_token(provider, poster_key)}"
        if poster_key else ""
    )
    return {
        "provider": provider,
        "external_id": external_id,
        "media_type": media_type,
        "title": str(row.get("title") or ""),
        "year": str(row.get("year") or ""),
        "poster_url": poster_url,
        "created_at": str(row.get("created_at") or ""),
        "subscription": subscription,
    }


@router.get("/stats")
def subscription_stats(request: Request):
    require_api_login(request)
    media = get_media_subscription_service().stats()
    rss = db.get_rss_stats()
    return api_response({
        **media,
        "rss_total": int(rss.get("subscription_total", 0)),
        "rss_active": int(rss.get("active_subscriptions", 0)),
        "rss_pending": int(rss.get("pending_total", 0)),
        "rss_entry_total": int(rss.get("entry_total", 0)),
        "total": int(media.get("media_total", 0)) + int(rss.get("subscription_total", 0)),
    })


@router.get("/media")
def list_media_subscriptions(
    request: Request,
    status: str = Query(default=""),
    enabled: str = Query(default=""),
):
    require_api_login(request)
    try:
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in _ALLOWED_LIST_STATUS:
            raise MediaSubscriptionError("status 查询参数无效")
        return api_response(get_media_subscription_service().list_subscriptions(
            status=normalized_status,
            enabled=_optional_bool(enabled),
        ))
    except MediaSubscriptionError as exc:
        return _service_error(exc)


@router.post("/media")
async def create_media_subscription(request: Request, data: Any = Body(default=None)):
    require_api_login(request)
    try:
        result = await get_media_subscription_service().create_subscription(_body(data))
        _wake_scheduler()
        return api_response(result, 201 if result.get("created") else 200)
    except MediaSubscriptionError as exc:
        return _service_error(exc)


@router.get("/media/{subscription_id}")
def get_media_subscription(subscription_id: int, request: Request):
    require_api_login(request)
    try:
        return api_response(get_media_subscription_service().get_subscription(subscription_id))
    except MediaSubscriptionError as exc:
        return _service_error(exc)


@router.put("/media/{subscription_id}")
def update_media_subscription(subscription_id: int, request: Request, data: Any = Body(default=None)):
    require_api_login(request)
    try:
        result = get_media_subscription_service().update_subscription(subscription_id, _body(data))
        _wake_scheduler()
        return api_response(result)
    except MediaSubscriptionError as exc:
        return _service_error(exc)


@router.delete("/media/{subscription_id}")
def delete_media_subscription(subscription_id: int, request: Request):
    require_api_login(request)
    removed = get_media_subscription_service().delete_subscription(subscription_id)
    if not removed:
        return api_error("媒体订阅不存在", 404)
    _wake_scheduler()
    return api_response({"success": True, "removed": True})


@router.post("/media/{subscription_id}/check")
async def check_media_subscription(subscription_id: int, request: Request):
    require_api_login(request)
    try:
        return api_response(await get_media_subscription_service().check_subscription(
            subscription_id,
            trigger="manual",
        ))
    except MediaSubscriptionError as exc:
        return _service_error(exc)


@router.get("/media/{subscription_id}/candidates")
def list_media_candidates(subscription_id: int, request: Request):
    require_api_login(request)
    try:
        return api_response(get_media_subscription_service().list_candidates(subscription_id))
    except MediaSubscriptionError as exc:
        return _service_error(exc)


@router.post("/media/{subscription_id}/download")
async def download_media_candidate(
    subscription_id: int,
    request: Request,
    data: Any = Body(default=None),
):
    require_api_login(request)
    try:
        payload = _body(data)
        unknown = set(payload) - {"candidate_id", "target"}
        if unknown:
            raise MediaSubscriptionError(f"包含不支持的下载参数：{', '.join(sorted(unknown))}")
        candidate_id = int(payload.get("candidate_id") or 0)
        if candidate_id <= 0:
            raise MediaSubscriptionError("candidate_id 无效")
        candidate = db.get_media_subscription_candidate(candidate_id)
        if candidate is None or int(candidate["subscription_id"]) != int(subscription_id):
            raise MediaSubscriptionError("候选资源不存在", status_code=404, code="not_found")
        result = await get_media_subscription_service().download_candidate(
            candidate_id,
            str(payload.get("target") or ""),
        )
        return api_response(result, 409 if result.get("duplicate") else 200)
    except MediaSubscriptionError as exc:
        return _service_error(exc)
    except (TypeError, ValueError):
        return api_error("candidate_id 无效", 400)


@router.get("/runs")
def list_media_subscription_runs(
    request: Request,
    subscription_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=300),
):
    require_api_login(request)
    return api_response(get_media_subscription_service().list_runs(
        subscription_id=subscription_id,
        limit=limit,
    ))


@router.get("/watchlist")
def list_watchlist(request: Request):
    require_api_login(request)
    subscriptions = get_media_subscription_service().list_subscriptions()
    subscription_map = {
        (str(item.get("tmdb_id") or ""), str(item.get("media_type") or "")): item
        for item in subscriptions
    }
    rows = [dict(row) for row in get_discovery_service().list_watchlist()]
    external_id_map = db.list_media_external_ids([
        (
            str(row.get("provider") or ""),
            str(row.get("external_id") or ""),
            str(row.get("media_type") or ""),
        )
        for row in rows
        if str(row.get("provider") or "") != "tmdb"
    ])
    return api_response([
        _watchlist_row(row, subscription_map, external_id_map)
        for row in rows
    ])


@router.post("/media/from-watchlist")
async def create_from_watchlist(request: Request, data: Any = Body(default=None)):
    require_api_login(request)
    try:
        payload = _body(data)
        unknown = set(payload) - _WATCHLIST_SUBSCRIPTION_FIELDS
        if unknown:
            raise MediaSubscriptionError(
                f"包含不支持的订阅参数：{', '.join(sorted(unknown))}"
            )
        provider = str(payload.get("provider") or "").strip().lower()
        external_id = str(payload.get("external_id") or "").strip()
        media_type = str(payload.get("media_type") or "").strip().lower()
        if provider not in _ALLOWED_PROVIDERS or media_type not in _ALLOWED_MEDIA_TYPES:
            raise MediaSubscriptionError("收藏媒体身份无效")
        if not external_id or len(external_id) > 128 or any(char.isspace() for char in external_id):
            raise MediaSubscriptionError("收藏媒体 ID 无效")

        watchlist_row = db.get_media_watchlist(provider, external_id, media_type)
        watchlist_item = dict(watchlist_row) if watchlist_row is not None else None
        if watchlist_item is None:
            raise MediaSubscriptionError(
                "收藏清单中不存在该媒体", status_code=404, code="not_found"
            )
        title = str(watchlist_item.get("title") or "").strip()[:300]
        year = str(watchlist_item.get("year") or "").strip()[:16]
        if not title:
            raise MediaSubscriptionError("收藏媒体信息不完整")

        confirmed_tmdb_id = str(payload.get("tmdb_id") or "").strip()
        if confirmed_tmdb_id and (
            not confirmed_tmdb_id.isascii()
            or not confirmed_tmdb_id.isdigit()
            or not 1 <= len(confirmed_tmdb_id) <= 10
            or int(confirmed_tmdb_id) <= 0
        ):
            raise MediaSubscriptionError("确认的 TMDB ID 无效")
        try:
            mapping = await get_discovery_service().map_to_tmdb_async(
                provider,
                external_id,
                media_type,
                title,
                year,
                confirmed_tmdb_id=confirmed_tmdb_id,
            )
        except ProviderError as exc:
            return api_response({"error": exc.safe_message, "code": exc.code}, exc.status_code)
        except (TypeError, ValueError) as exc:
            raise MediaSubscriptionError("收藏媒体映射参数无效") from exc
        if not mapping.get("confirmed"):
            return api_response({
                "error": "需要确认 TMDB 媒体后才能创建订阅",
                "code": "mapping_required",
                "candidates": list(mapping.get("candidates") or []),
            }, 409)
        subscription_fields = {
            key: payload[key]
            for key in (
                "monitor_mode", "seasons", "include_specials", "action",
                "download_target", "sites", "check_interval_minutes", "enabled",
            )
            if key in payload
        }
        result = await get_media_subscription_service().create_subscription({
            **subscription_fields,
            "provider": provider,
            "external_id": external_id,
            "media_type": media_type,
            "tmdb_id": str(mapping["tmdb_id"]),
        }, identity_confirmed=True)
        _wake_scheduler()
        return api_response(result, 201 if result.get("created") else 200)
    except MediaSubscriptionError as exc:
        return _service_error(exc)
