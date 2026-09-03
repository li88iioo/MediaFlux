"""统一媒体探索 API。"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from app import config
from app.discovery.models import MediaCard, ProviderError
from app.discovery.service import get_discovery_service
from app.discovery.search import get_discovery_search_service
from app.routes.discovery_image import decode_poster_token, encode_poster_token
from app.web import api_error, api_response, require_api_login


def _require_discovery_enabled() -> None:
    if not config.get_bool("DISCOVERY_ENABLED"):
        raise HTTPException(status_code=404, detail="not found")


router = APIRouter(prefix="/api/discovery", dependencies=[Depends(_require_discovery_enabled)])
_BASE_QUERY_KEYS = {"provider", "category", "media_type", "page"}
_FILTER_QUERY_KEYS = {"with_genres", "with_original_language", "sort_by", "sort", "tags", "weekday"}
_PROVIDERS = {"tmdb", "douban", "bangumi"}
_MEDIA_TYPES = {"movie", "tv"}
_PROVIDER_MEDIA_TYPES = {
    "tmdb": {"movie", "tv"},
    "douban": {"movie", "tv"},
    "bangumi": {"tv"},
}
_EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_YEAR_RE = re.compile(r"^(?:18|19|20|21)\d{2}$")
_TMDB_ID_RE = re.compile(r"^\d{1,20}$")


def _provider_error(exc: ProviderError):
    return api_response({"error": exc.safe_message, "code": exc.code, "retry_after": exc.retry_after}, exc.status_code)


def _scalar(data: dict[str, Any], key: str, *, required: bool = False, max_length: int = 300) -> str:
    value = data.get(key)
    if value is None:
        value = ""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError(f"{key} 必须是标量")
    value = str(value).strip()
    if required and not value:
        raise ValueError(f"{key} 不能为空")
    if len(value) > max_length:
        raise ValueError(f"{key} 过长")
    return value


def _identity(provider: str, media_type: str, external_id: str) -> tuple[str, str, str]:
    provider = str(provider or "").strip().lower()
    media_type = str(media_type or "").strip().lower()
    external_id = str(external_id or "").strip()
    if provider not in _PROVIDERS:
        raise ValueError("不支持的数据源")
    if media_type not in _MEDIA_TYPES:
        raise ValueError("媒体类型无效")
    if media_type not in _PROVIDER_MEDIA_TYPES[provider]:
        raise ValueError("数据源与媒体类型组合无效")
    if not _EXTERNAL_ID_RE.fullmatch(external_id):
        raise ValueError("来源 ID 无效")
    if provider in {"tmdb", "bangumi"} and not _TMDB_ID_RE.fullmatch(external_id):
        raise ValueError("来源 ID 无效")
    return provider, media_type, external_id


def _year(value: str) -> str:
    if value and not _YEAR_RE.fullmatch(value):
        raise ValueError("年份无效")
    return value


def _card_payload(card: MediaCard) -> dict[str, Any]:
    payload = card.to_dict()
    poster_key = str(payload.pop("poster_key", "") or "")
    payload.pop("backdrop_key", None)
    payload["poster_url"] = (
        f"/discovery-poster/{card.provider}/{encode_poster_token(card.provider, poster_key)}"
        if poster_key else ""
    )
    return payload


def _page_payload(page) -> dict[str, Any]:
    payload = page.to_dict()
    payload["items"] = [_card_payload(item) for item in page.items]
    return payload


def _watchlist_payload(row: dict[str, Any]) -> dict[str, Any]:
    card = MediaCard(
        provider=str(row.get("provider") or ""), external_id=str(row.get("external_id") or ""),
        media_type=str(row.get("media_type") or ""), title=str(row.get("title") or ""),
        year=str(row.get("year") or ""), poster_key=str(row.get("poster_key") or ""),
        state="watchlisted",
    )
    return _card_payload(card)


@router.get("/sections")
def sections(request: Request):
    require_api_login(request)
    return api_response(get_discovery_service().list_sections())


@router.get("/search")
def search(
    request: Request,
    q: str = Query(default=""),
    page: str = Query(default="1"),
    providers: str = Query(default=""),
):
    require_api_login(request)
    unknown = set(request.query_params.keys()) - {"q", "page", "providers"}
    if unknown:
        return api_error(f"未知查询参数: {', '.join(sorted(unknown))}", 400)
    if len(providers) > 64:
        return api_error("providers 参数过长", 400)
    try:
        page_number = int(page)
        selected = [value.strip().lower() for value in providers.split(",") if value.strip()] or None
        result = get_discovery_search_service().search(q, page_number, selected)
    except ValueError as exc:
        return api_error(str(exc), 400)
    payload = {
        "query": result.query,
        "page": result.page,
        "items": [_card_payload(card) for card in result.items],
        "has_more": result.has_more,
        "providers_attempted": list(result.providers_attempted),
        "providers_succeeded": list(result.providers_succeeded),
        "errors": list(result.errors),
    }
    return api_response(payload)


@router.get("/items")
def items(
    request: Request,
    provider: str = Query(default=""), category: str = Query(default=""),
    media_type: str = Query(default=""), page: str = Query(default="1"),
):
    require_api_login(request)
    unknown = set(request.query_params.keys()) - _BASE_QUERY_KEYS - _FILTER_QUERY_KEYS
    if unknown:
        return api_error(f"包含不支持的查询参数: {', '.join(sorted(unknown))}", 400)
    if any(len(value) > limit for value, limit in ((provider, 32), (category, 64), (media_type, 16))):
        return api_error("查询参数过长", 400)
    try:
        page_number = int(page)
    except (TypeError, ValueError):
        return api_error("页码无效", 400)
    filters = {key: value for key in _FILTER_QUERY_KEYS if (value := request.query_params.get(key)) not in (None, "")}
    try:
        result = get_discovery_service().list_items(provider, category, media_type, page_number, filters)
        return api_response(_page_payload(result))
    except ProviderError as exc:
        return _provider_error(exc)
    except (TypeError, ValueError) as exc:
        return api_error(str(exc), 400)


@router.get("/filters/{provider}/{media_type}")
def filters(request: Request, provider: str, media_type: str):
    require_api_login(request)
    try:
        return api_response(get_discovery_service().list_filters(provider, media_type))
    except ValueError as exc:
        return api_error(str(exc), 400)


@router.get("/detail/{provider}/{media_type}/{external_id}")
def detail(request: Request, provider: str, media_type: str, external_id: str):
    require_api_login(request)
    try:
        provider, media_type, external_id = _identity(provider, media_type, external_id)
        card = get_discovery_service().get_detail(provider, media_type, external_id)
        if card is None:
            return api_error("未找到媒体详情", 404)
        return api_response(_card_payload(card))
    except ProviderError as exc:
        return _provider_error(exc)
    except (TypeError, ValueError) as exc:
        return api_error(str(exc), 400)


@router.post("/map")
async def map_media(request: Request, data: Any = Body(default=None)):
    require_api_login(request)
    if not isinstance(data, dict):
        return api_error("映射参数必须是 JSON 对象", 400)
    allowed = {"provider", "external_id", "media_type", "title", "year", "tmdb_id", "tmdb_title", "tmdb_year"}
    unknown = set(data) - allowed
    if unknown:
        return api_error(f"包含不支持的映射参数: {', '.join(sorted(unknown))}", 400)
    try:
        provider, media_type, external_id = _identity(
            _scalar(data, "provider", required=True, max_length=32),
            _scalar(data, "media_type", required=True, max_length=16),
            _scalar(data, "external_id", required=True, max_length=128),
        )
        title = _scalar(data, "title", required=True, max_length=300)
        year = _year(_scalar(data, "year", max_length=4))
        tmdb_id = _scalar(data, "tmdb_id", max_length=20)
        if tmdb_id and not _TMDB_ID_RE.fullmatch(tmdb_id):
            raise ValueError("TMDB ID 无效")
        tmdb_title = _scalar(data, "tmdb_title", max_length=300)
        tmdb_year = _year(_scalar(data, "tmdb_year", max_length=4))
        result = await get_discovery_service().map_to_tmdb_async(
            provider,
            external_id,
            media_type,
            title,
            year,
            confirmed_tmdb_id=tmdb_id,
            confirmed_title=tmdb_title,
            confirmed_year=tmdb_year,
        )
        return api_response(result)
    except ProviderError as exc:
        return _provider_error(exc)
    except (TypeError, ValueError) as exc:
        return api_error(str(exc), 400)


@router.get("/watchlist")
def watchlist(request: Request):
    require_api_login(request)
    try:
        return api_response([_watchlist_payload(dict(row)) for row in get_discovery_service().list_watchlist()])
    except (TypeError, ValueError) as exc:
        return api_error(str(exc), 500)


@router.post("/watchlist")
def add_watchlist(request: Request, data: Any = Body(default=None)):
    require_api_login(request)
    if not isinstance(data, dict):
        return api_error("收藏参数必须是 JSON 对象", 400)
    allowed = {"provider", "external_id", "media_type", "title", "year", "poster_token"}
    unknown = set(data) - allowed
    if unknown:
        return api_error(f"包含不支持的收藏参数: {', '.join(sorted(unknown))}", 400)
    try:
        provider, media_type, external_id = _identity(
            _scalar(data, "provider", required=True, max_length=32),
            _scalar(data, "media_type", required=True, max_length=16),
            _scalar(data, "external_id", required=True, max_length=128),
        )
        poster_token = _scalar(data, "poster_token", max_length=2048)
        poster_key = decode_poster_token(provider, poster_token) if poster_token else ""
        card = MediaCard(
            provider=provider, external_id=external_id, media_type=media_type,
            title=_scalar(data, "title", required=True, max_length=300),
            year=_year(_scalar(data, "year", max_length=4)), poster_key=poster_key,
        )
        get_discovery_service().add_watchlist(card)
        return api_response({"success": True, "stable_id": card.stable_id})
    except (TypeError, ValueError) as exc:
        return api_error(str(exc), 400)
    except HTTPException as exc:
        return api_error(str(exc.detail or "poster token invalid"), exc.status_code)


@router.delete("/watchlist/{provider}/{media_type}/{external_id}")
def remove_watchlist(request: Request, provider: str, media_type: str, external_id: str):
    require_api_login(request)
    try:
        provider, media_type, external_id = _identity(provider, media_type, external_id)
        removed = get_discovery_service().remove_watchlist(provider, media_type, external_id)
        return api_response({"success": True, "removed": bool(removed)})
    except (TypeError, ValueError) as exc:
        return api_error(str(exc), 400)
