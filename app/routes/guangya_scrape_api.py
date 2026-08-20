"""光鸭目录级手动搜索、自动匹配和归档整理 API。"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from app.logger import get_logger
from app.modules.directory_scrape import get_directory_scrape_service
from app.modules.directory_scrape_errors import (
    DirectoryScrapePublicError,
    DirectoryScrapeRequestError,
    public_error_message,
)
from app.modules.organize_tasks import get_organize_manager
from app.web import require_api_login

logger = get_logger(__name__)
router = APIRouter(prefix="/api/guangya/directory-scrape")


def _owner(request: Request) -> str:
    owner = str(request.session.get("csrf_token") or "")
    if not owner:
        raise HTTPException(status_code=401, detail="登录会话无效")
    return owner


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, DirectoryScrapePublicError):
        return JSONResponse(
            {"error": public_error_message(exc)},
            status_code=exc.status_code,
        )
    logger.error(
        "目录刮削 API 失败 type=%s",
        type(exc).__name__,
    )
    return JSONResponse({"error": "目录刮削请求失败"}, status_code=500)


@router.post("/inspect")
def inspect_directory(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    payload = data or {}
    has_directory = "directory_id" in payload
    has_file = "file_id" in payload
    if has_directory == has_file:
        return JSONResponse(
            {"error": "请选择一个需要刮削的目录或视频文件"},
            status_code=400,
        )
    scope_key = "directory_id" if has_directory else "file_id"
    raw_scope_id = payload[scope_key]
    if not isinstance(raw_scope_id, str) or not raw_scope_id.strip():
        return JSONResponse({"error": "刮削目标 ID 无效"}, status_code=400)
    scope_id = raw_scope_id.strip()
    try:
        service = get_directory_scrape_service()
        owner = _owner(request)
        if has_file:
            return service.inspect_file(owner, scope_id)
        return service.inspect(owner, scope_id)
    except Exception as exc:
        return _error(exc)


@router.post("/search")
def search_directory(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    payload = data or {}
    try:
        candidates = get_directory_scrape_service().search(
            _owner(request),
            str(payload.get("inspection_id") or ""),
            str(payload.get("query") or ""),
            str(payload.get("media_type") or "auto"),
            str(payload.get("year") or ""),
        )
        return {"candidates": candidates}
    except Exception as exc:
        return _error(exc)


@router.post("/external-hints")
def external_hints(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    payload = data or {}
    try:
        return get_directory_scrape_service().external_hints(
            _owner(request),
            str(payload.get("inspection_id") or ""),
            str(payload.get("query") or ""),
            str(payload.get("media_type") or "auto"),
        )
    except Exception as exc:
        return _error(exc)


def _optional_int(
    payload: dict,
    key: str,
    minimum: int,
    maximum: int,
) -> int | None:
    if key not in payload or payload[key] in (None, ""):
        return None
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise DirectoryScrapeRequestError(f"{key} 必须是整数")
    if not minimum <= value <= maximum:
        raise DirectoryScrapeRequestError(f"{key} 超出允许范围")
    return value


@router.post("/preview")
def preview_directory(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    payload = data or {}
    try:
        season = _optional_int(payload, "season", 0, 99)
        episode = _optional_int(payload, "episode", 1, 999)
        overrides = {}
        if season is not None:
            overrides["season"] = season
        if episode is not None:
            overrides["episode"] = episode
        preview_kwargs = dict(overrides)
        if "numbering_mode" in payload:
            preview_kwargs["numbering_mode"] = str(
                payload.get("numbering_mode") or "auto"
            ).strip().lower()
        provider = str(payload.get("provider") or "").strip().lower()
        external_id = str(payload.get("external_id") or "").strip()
        if provider and (provider != "tmdb" or external_id):
            preview_kwargs.update(provider=provider, external_id=external_id)
        return get_directory_scrape_service().preview(
            _owner(request),
            str(payload.get("inspection_id") or ""),
            str(payload.get("tmdb_id") or ""),
            str(payload.get("media_type") or "movie"),
            **preview_kwargs,
        )
    except Exception as exc:
        return _error(exc)


@router.post("/run")
def run_directory(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    payload = data or {}
    owner = _owner(request)
    mode = str(payload.get("mode") or "").strip().lower()
    service = get_directory_scrape_service()
    try:
        if mode == "auto":
            result = service.auto_match(
                owner,
                str(payload.get("inspection_id") or ""),
            )
            if result.get("status") == "requires_manual":
                return result
            preview_id = str(result.get("preview_id") or "")
            reference = str(
                (result.get("directory") or {}).get("name")
                or service.preview_reference(owner, preview_id)
            )
        elif mode == "manual":
            preview_id = str(payload.get("preview_id") or "").strip()
            if not preview_id:
                return JSONResponse({"error": "缺少预览 ID"}, status_code=400)
            reference = service.preview_reference(owner, preview_id)
        else:
            return JSONResponse({"error": "执行模式只能是 manual 或 auto"}, status_code=400)

        task = get_organize_manager().start_operation(
            "目录刮削",
            reference,
            lambda: service.execute_preview(owner, preview_id),
            queue_if_busy=mode == "manual",
            dedupe_key=f"directory-scrape:{owner}:{preview_id}",
        )
        if not task.get("ok"):
            return JSONResponse(
                {"error": task.get("error") or "网盘整理任务正在运行"},
                status_code=409,
            )
        return JSONResponse(task, status_code=202)
    except Exception as exc:
        return _error(exc)
