"""多站资源搜索与安全下载分发 API。"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app import config
from app.clients.http import HttpError
from app.indexers.config import (
    INDEXER_SITE_LABELS,
    MANUAL_INDEXER_SITE_ORDER,
)
from app.indexers.downloads import (
    DownloadRequestCreationError as _DownloadRequestCreationError,
)
from app.indexers.downloads import (
    InvalidDownloadData as _InvalidDownloadData,
)
from app.indexers.downloads import (
    download_indexer_result,
    download_indexer_result_public,
    resubmit_indexer_download_request,
)
from app.indexers.errors import (
    IndexerResultExpired,
    IndexerResultNotFound,
    IndexerValidationError,
)
from app.indexers.models import IndexerMediaSearchRequest
from app.indexers.runtime import get_indexer_service
from app.indexers.service import search_payload
from app.modules.download_dispatcher import public_dispatch_summary
from app.web import api_error, api_response, require_api_login
from app.repositories.download_requests import DOWNLOAD_TARGETS


def _require_enabled() -> None:
    if not config.get_bool("INDEXER_SEARCH_ENABLED"):
        raise HTTPException(status_code=404, detail="not found")


router = APIRouter(prefix="/api/indexers", dependencies=[Depends(_require_enabled)])


def _error_response(exc: HttpError):
    if isinstance(exc, IndexerResultExpired):
        status = 410
    elif isinstance(exc, IndexerResultNotFound):
        status = 404
    elif isinstance(exc, IndexerValidationError):
        status = 400
    elif exc.code == "timeout":
        status = 504
    else:
        status = 502
    return api_response({"error": exc.public_message, "code": exc.code}, status)


def _site_payload(service, site_id: str) -> dict[str, Any]:
    adapter = service.registry.get(site_id)
    return {
        "site_id": adapter.site_id,
        "site_name": INDEXER_SITE_LABELS.get(site_id, adapter.site_name),
        "enabled": site_id in getattr(service, "enabled_site_ids", frozenset(service.registry.enabled_ids())),
        "pagination_supported": adapter.capabilities.pagination_supported,
        "download_kinds": list(adapter.capabilities.download_kinds),
    }


async def _stream_search(request: Request, service, run_search):
    """相同搜索管线的SSE表现形式：队列仅保留最新快照，断开即撤销本消费者。"""
    updates = asyncio.Queue(maxsize=1)

    def receive(result):
        if updates.full():
            updates.get_nowait()
        updates.put_nowait(result)

    task = asyncio.create_task(run_search(receive))
    update = asyncio.create_task(updates.get())
    try:
        while True:
            done, _ = await asyncio.wait((task, update), timeout=10, return_when=asyncio.FIRST_COMPLETED)
            if await request.is_disconnected():
                break
            if task in done:
                result = task.result()
                yield "event: complete\ndata: " + json.dumps(search_payload(service, result), ensure_ascii=False) + "\n\n"
                break
            if update in done:
                yield "event: progress\ndata: " + json.dumps(search_payload(service, update.result()), ensure_ascii=False) + "\n\n"
                update = asyncio.create_task(updates.get())
            elif not done:
                yield ": keepalive\n\n"
    except HttpError as exc:
        yield "event: error\ndata: " + json.dumps({"error": exc.public_message, "code": exc.code}, ensure_ascii=False) + "\n\n"
    finally:
        for pending in (task, update):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, update, return_exceptions=True)


def _stream_response(request, service, run_search):
    return StreamingResponse(_stream_search(request, service, run_search), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


def _media_request(payload: Any) -> tuple[IndexerMediaSearchRequest, list[str] | None]:
    if not isinstance(payload, dict):
        raise IndexerValidationError("request body must be an object")
    allowed = {
        "title",
        "original_title",
        "english_title",
        "aliases",
        "year",
        "media_type",
        "sort_mode",
        "season",
        "episode",
        "page",
        "sites",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise IndexerValidationError(f"unknown request fields: {', '.join(sorted(unknown))}")
    aliases = payload.get("aliases", [])
    sites = payload.get("sites", [])
    if not isinstance(aliases, list):
        raise IndexerValidationError("aliases must be a list")
    if not isinstance(sites, list) or len(sites) > 16:
        raise IndexerValidationError("sites must be a bounded list")
    site_ids: list[str] = []
    for value in sites:
        site_id = str(value or "").strip().lower()
        if not site_id or len(site_id) > 32:
            raise IndexerValidationError("invalid site id")
        if site_id not in site_ids:
            site_ids.append(site_id)
    request = IndexerMediaSearchRequest.create(
        title=payload.get("title", ""),
        original_title=payload.get("original_title", ""),
        english_title=payload.get("english_title", ""),
        aliases=aliases,
        year=payload.get("year"),
        media_type=payload.get("media_type", ""),
        sort_mode=payload.get("sort_mode", "relevance_desc"),
        season=payload.get("season"),
        episode=payload.get("episode"),
        page=payload.get("page", 1),
    )
    return request, site_ids or None


@router.get("/sites")
def sites(request: Request):
    require_api_login(request)
    service = get_indexer_service()
    registered = set(service.registry.ids())
    return api_response([
        _site_payload(service, site_id)
        for site_id in MANUAL_INDEXER_SITE_ORDER
        if site_id in registered
    ])


@router.get("/search")
async def search(
    request: Request,
    q: str = Query(default=""),
    page: int = Query(default=1),
    sites: str = Query(default=""),
    sort: str = Query(default="relevance_desc"),
):
    require_api_login(request)
    unknown = set(request.query_params.keys()) - {"q", "page", "sites", "sort"}
    if unknown:
        return api_error(f"未知查询参数: {', '.join(sorted(unknown))}", 400)
    if len(sites) > 128:
        return api_error("sites 参数过长", 400)
    site_ids = [value.strip().lower() for value in sites.split(",") if value.strip()] or None
    service = get_indexer_service()
    if "text/event-stream" in request.headers.get("accept", ""):
        return _stream_response(request, service, lambda observer: service.search(
            q, page, site_ids, scope="manual", sort_mode=sort, on_progress=observer
        ))
    try:
        result = await service.search(
            q, page, site_ids, scope="manual", sort_mode=sort
        )
    except HttpError as exc:
        return _error_response(exc)
    return api_response(search_payload(service, result))


@router.post("/search")
async def search_media(request: Request, payload: Any = Body(...)):
    require_api_login(request)
    try:
        media_request, site_ids = _media_request(payload)
        service = get_indexer_service()
        if "text/event-stream" in request.headers.get("accept", ""):
            return _stream_response(request, service, lambda observer: service.search_media(
                media_request, site_ids, scope="manual", on_progress=observer
            ))
        result = await service.search_media(
            media_request, site_ids, scope="manual"
        )
    except HttpError as exc:
        return _error_response(exc)
    return api_response(search_payload(service, result))


def _validate_batch_payload(data: Any) -> tuple[list[str], str]:
    if not isinstance(data, dict):
        raise ValueError("批量下载参数必须是 JSON 对象")
    unknown = set(data) - {"result_ids", "target"}
    if unknown:
        raise ValueError(f"未知批量下载参数: {', '.join(sorted(unknown))}")
    raw_result_ids = data.get("result_ids")
    if not isinstance(raw_result_ids, list) or not raw_result_ids:
        raise ValueError("资源结果 ID 列表无效")

    normalized_ids = []
    for result_id in raw_result_ids:
        if not isinstance(result_id, str):
            raise ValueError("资源结果 ID 无效")
        normalized = result_id.strip()
        if not normalized or len(normalized) > 128:
            raise ValueError("资源结果 ID 无效")
        normalized_ids.append(normalized)
    result_ids = list(dict.fromkeys(normalized_ids))
    if len(result_ids) > 50:
        raise ValueError("批量下载最多支持 50 个资源")

    raw_target = data.get("target")
    if not isinstance(raw_target, str):
        raise ValueError("下载目标无效")
    target = raw_target.strip().lower()
    if target not in DOWNLOAD_TARGETS:
        raise ValueError("下载目标仅支持 qb、guangya 或 both")
    return result_ids, target


@router.post("/download")
async def download(request: Request, data: Any = Body(default=None)):
    require_api_login(request)
    if not isinstance(data, dict):
        return api_error("下载参数必须是 JSON 对象", 400)
    unknown = set(data) - {"result_id", "target"}
    if unknown:
        return api_error(f"未知下载参数: {', '.join(sorted(unknown))}", 400)
    result_id = str(data.get("result_id") or "").strip()
    target = str(data.get("target") or "").strip().lower()
    if not result_id or len(result_id) > 128:
        return api_error("资源结果 ID 无效", 400)
    if target not in DOWNLOAD_TARGETS:
        return api_error("下载目标仅支持 qb、guangya 或 both", 400)

    service = get_indexer_service()
    try:
        item = await download_indexer_result(service, result_id, target, scope="manual")
    except HttpError as exc:
        return _error_response(exc)
    except _InvalidDownloadData:
        return api_response({"error": "资源下载数据无效", "code": "invalid_download"}, 400)
    except _DownloadRequestCreationError:
        return api_response({"error": "下载请求创建失败", "code": "request_failed"}, 500)
    except Exception:
        return api_response({"error": "下载处理失败", "code": "download_failed"}, 500)

    status = 200 if item["ok"] else (409 if item["duplicate"] else 502)
    response = {
        "ok": item["ok"],
        "request_id": item["request_id"],
        "created": item["created"],
        "target": target,
        "status": item["status"],
        "succeeded": item["succeeded"],
        "failed": item["failed"],
        "duplicate": item["duplicate"],
        "error": item["error"],
    }
    if item["duplicate"]:
        response.update({
            "existing_status": item.get("existing_status", ""),
            "can_resubmit": bool(item.get("can_resubmit")),
            "resubmit_target": item.get("resubmit_target", ""),
        })
    return JSONResponse(response, status_code=status)


@router.post("/download/resubmit")
async def resubmit_download(request: Request, data: Any = Body(default=None)):
    require_api_login(request)
    if not isinstance(data, dict):
        return api_error("重新提交参数必须是 JSON 对象", 400)
    try:
        request_id = int(data.get("request_id") or 0)
    except (TypeError, ValueError):
        request_id = 0
    target = str(data.get("target") or "").strip().lower()
    if request_id <= 0:
        return api_error("下载请求 ID 无效", 400)
    if target not in DOWNLOAD_TARGETS:
        return api_error("下载目标仅支持 qb、guangya 或 both", 400)

    result = await asyncio.to_thread(
        resubmit_indexer_download_request,
        request_id,
        target,
    )
    if result.get("not_found"):
        return api_error("下载请求不存在", 404)
    if result.get("blocked"):
        return api_error(str(result.get("error") or "当前目标不可重新提交"), 409)
    public = public_dispatch_summary(result)
    response = {
        **public,
        "source_request_id": request_id,
        "request_id": int(result.get("request_id") or request_id),
        "created": bool(result.get("created")),
        "target": target,
    }
    status = 200 if public["ok"] else (409 if public["duplicate"] else 502)
    return JSONResponse(response, status_code=status)


@router.post("/download/batch")
async def batch_download(request: Request, data: Any = Body(default=None)):
    require_api_login(request)
    try:
        result_ids, target = _validate_batch_payload(data)
    except ValueError as exc:
        return api_error(str(exc), 400)

    service = get_indexer_service()

    async def run(result_id: str) -> dict[str, Any]:
        return await download_indexer_result_public(
            service, result_id, target, scope="manual"
        )

    items = await asyncio.gather(*(run(result_id) for result_id in result_ids))
    summary = {
        "total": len(items),
        "succeeded": sum(item["status"] == "submitted" for item in items),
        "partial": sum(item["status"] == "partial" for item in items),
        "review_required": sum(item["status"] == "manual_review" for item in items),
        "failed": sum(item["status"] == "failed" for item in items),
        "duplicate": sum(item["status"] == "duplicate" for item in items),
    }
    return api_response({
        "ok": summary["succeeded"] > 0 or summary["partial"] > 0,
        "summary": summary,
        "items": items,
    })
