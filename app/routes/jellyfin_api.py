"""Jellyfin 插件的明确删除请求；认证复用 Web 会话及全局 CSRF。"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from app.modules.directory_scrape_errors import (
    DirectoryScrapePublicError,
    public_error_message,
)
from app.modules.jellyfin_delete import DELETE_SCOPE_KINDS, delete_key, execute_delete, prepare_delete
from app.modules.organize_tasks import get_organize_manager
from app.web import require_api_login

router = APIRouter(prefix="/api/jellyfin")


@router.get("/delete-capabilities")
def delete_capabilities(request: Request):
    require_api_login(request)
    return {"scope_version": 1, "kinds": list(DELETE_SCOPE_KINDS)}


@router.post("/delete")
def delete_media(request: Request, data: Annotated[dict | None, Body()] = None):
    require_api_login(request)
    payload = data or {}
    try:
        targets = prepare_delete(payload)
    except DirectoryScrapePublicError as exc:
        return JSONResponse({"error": public_error_message(exc)}, status_code=exc.status_code)
    result = get_organize_manager().start_operation(
        "Jellyfin 联动删除", f"{len(targets)} 个 STRM 文件",
        lambda: execute_delete(payload, targets), queue_if_busy=False,
        dedupe_key=delete_key(targets),
    )
    return JSONResponse(result, status_code=202 if result.get("ok") else 409)
