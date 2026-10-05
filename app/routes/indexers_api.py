"""多站资源搜索与安全下载分发 API。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app import config
from app.indexers.errors import (
    IndexerError,
    IndexerResultExpired,
    IndexerResultNotFound,
    IndexerValidationError,
)
from app.indexers.config import INDEXER_SITE_ORDER
from app.indexers.models import IndexerMediaSearchRequest, indexer_site_status
from app.indexers.ranking import match_priority
from app.indexers.release import parse_indexer_release_position
from app.indexers.service import IndexerService
from app.indexers.runtime import get_indexer_service
from app.indexers.downloads import (
    DownloadRequestCreationError as _DownloadRequestCreationError,
    InvalidDownloadData as _InvalidDownloadData,
    download_indexer_result,
    download_indexer_result_public,
    resubmit_indexer_download_request,
)
from app.modules.download_dispatcher import public_dispatch_summary
from app.web import api_error, api_response, require_api_login


def _require_enabled() -> None:
    if not config.get_bool("INDEXER_SEARCH_ENABLED"):
        raise HTTPException(status_code=404, detail="not found")


router = APIRouter(prefix="/api/indexers", dependencies=[Depends(_require_enabled)])
_DOWNLOAD_TARGETS = {"qb", "guangya", "both"}
_SITE_ERROR_CATALOG: dict[str, tuple[str, bool]] = {
    "timeout": ("站点响应超时，请稍后重试", True),
    "unavailable": ("站点暂不可用，请稍后重试", True),
    "rate_limited": ("站点请求过于频繁，请稍后再试", True),
    "invalid_response": ("站点返回异常数据，请稍后重试", True),
    "response_too_large": ("站点响应异常，暂无法处理", False),
    "security_error": ("站点连接未通过安全校验", False),
}


def _safe_site_error(error) -> tuple[str, str, bool]:
    code = str(getattr(error, "code", "") or "unavailable")
    message, retryable = _SITE_ERROR_CATALOG.get(
        code, ("站点检索失败，请稍后重试", True)
    )
    return code, message, retryable


def _public_provider_error(error) -> dict[str, str]:
    code, message, _retryable = _safe_site_error(error)
    return {"site_id": str(error.site_id), "code": code, "message": message}


def _public_source_url(service, item) -> str | None:
    value = str(getattr(item, "detail_url", "") or "").strip()
    if not value or service is None:
        return None
    try:
        parsed = urlsplit(value)
        adapter = service.registry.get(item.site_id)
        client = adapter.http_client_for_result(item)
        allowed_hosts = {str(host).rstrip(".").lower() for host in getattr(client, "allowed_hosts", ())}
    except (IndexerError, KeyError, TypeError, ValueError):
        return None
    try:
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port or 443
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or parsed.username or parsed.password:
        return None
    if not host or host not in allowed_hosts or port != 443:
        return None
    return value


def _public_search_item(item, service=None) -> dict[str, Any]:
    payload = item.to_public_dict()
    payload["source_url"] = _public_source_url(service, item)
    payload["match_priority"] = match_priority(item)
    # 稳定身份只供展示/勾选迁移；下载仍必须使用当前result_id并经过确认。
    payload["resource_key"] = hashlib.sha256(repr(IndexerService._result_identity(item)).encode("utf-8")).hexdigest()
    payload.update(parse_indexer_release_position(item.title))
    return payload


def _error_response(exc: IndexerError):
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
        "site_name": adapter.site_name,
        "enabled": site_id in getattr(service, "enabled_site_ids", frozenset(service.registry.enabled_ids())),
        "pagination_supported": adapter.capabilities.pagination_supported,
        "download_kinds": list(adapter.capabilities.download_kinds),
    }


def _search_site_statuses(service, result) -> list[dict[str, Any]]:
    raw_counts = getattr(result, "site_item_counts", None)
    if isinstance(raw_counts, dict) and raw_counts:
        counts = {str(site_id): max(0, int(count)) for site_id, count in raw_counts.items()}
    else:
        counts: dict[str, int] = {}
        for item in result.items:
            counts[item.site_id] = counts.get(item.site_id, 0) + 1
    errors = {error.site_id: error for error in result.errors}
    site_queries = getattr(result, "site_queries", {}) or {}
    site_attempt_counts = getattr(result, "site_attempt_counts", {}) or {}
    visible_counts = getattr(result, "site_visible_counts", {}) or {}
    page_states = getattr(result, "site_page_states", {}) or {}
    enabled_site_ids = getattr(service, "enabled_site_ids", None)
    if enabled_site_ids is None:
        enabled_site_ids = frozenset(service.registry.enabled_ids())
    payload = []
    for site_id in service.registry.ids():
        adapter = service.registry.get(site_id)
        code = None
        retryable = False
        sources = (getattr(result, "source_statuses", {}) or {}).get(site_id, ())
        diagnostics = [{
            "site_id": row.site_id, "site_name": row.site_name, "status": row.status,
            "count": row.count, "cached": bool(result.cached or row.cached), "code": row.code or None,
            "message": (
                ("本轮未完整核验，暂保留缓存结果" if result.cached or row.cached else "部分结果未能取得，已保留成功结果")
                if row.status == "partial" else
                _SITE_ERROR_CATALOG.get(row.code, ("来源检索未完成", True))[0] if row.code else ""
            ),
        } for row in sources]
        status = "disabled" if site_id not in enabled_site_ids else indexer_site_status(result, site_id, counts.get(site_id, 0))
        message = ""
        if status is None:
            continue
        if status == "searching":
            message = "正在补充检索"
        elif status == "partial":
            message, retryable = "本轮未找到匹配资源，部分来源未完成", True
        elif status == "error":
            if site_id in errors:
                code, message, retryable = _safe_site_error(errors[site_id])
            else:
                code, message, retryable = "unavailable", "站点未返回有效状态", True
        if result.site_truncated_counts.get(site_id, 0):
            hint = "达到候选上限，可细化片名、年份或季集后重搜"
            message = f"{message}；{hint}" if message else hint
        page_state = page_states.get(site_id)
        payload.append({
            "site_id": site_id,
            "site_name": adapter.site_name,
            "status": status,
            "count": counts.get(site_id, 0),
            "collected_count": result.site_collected_counts.get(site_id, counts.get(site_id, 0)),
            "truncated_count": result.site_truncated_counts.get(site_id, 0),
            "visible_count": max(0, int(visible_counts.get(site_id, counts.get(site_id, 0)) or 0)),
            "message": message,
            "code": code,
            "retryable": retryable,
            "diagnostics": diagnostics,
            "query": str(site_queries.get(site_id) or ""),
            "attempts": max(0, int(site_attempt_counts.get(site_id, 0) or 0)),
            "pagination_supported": bool(
                page_state.pagination_supported if page_state is not None else adapter.capabilities.pagination_supported
            ),
            "requested_page": int(page_state.requested_page if page_state is not None else result.page),
            "has_more": page_state.has_more if page_state is not None else None,
            "next_page": page_state.next_page if page_state is not None else None,
        })
    return payload


def _search_payload(service, result) -> dict[str, Any]:
    return {
        "query": result.query,
        "page": result.page,
        "items": [_public_search_item(item, service) for item in result.items],
        "sites_attempted": list(result.sites_attempted),
        "sites_succeeded": list(result.sites_succeeded),
        "errors": [_public_provider_error(error) for error in result.errors],
        "site_statuses": _search_site_statuses(service, result),
        "partial": result.partial,
        "truncated": any(result.site_truncated_counts.values()),
        "cached": result.cached,
        "has_more": bool(getattr(result, "has_more", False)),
        "complete": bool(getattr(result, "complete", True)),
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
                yield "event: complete\ndata: " + json.dumps(_search_payload(service, result), ensure_ascii=False) + "\n\n"
                break
            if update in done:
                yield "event: progress\ndata: " + json.dumps(_search_payload(service, update.result()), ensure_ascii=False) + "\n\n"
                update = asyncio.create_task(updates.get())
            elif not done:
                yield ": keepalive\n\n"
    except IndexerError as exc:
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
        for site_id in INDEXER_SITE_ORDER
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
        return _stream_response(request, service, lambda observer: service.search(q, page, site_ids, sort_mode=sort, on_progress=observer))
    try:
        result = await service.search(q, page, site_ids, sort_mode=sort)
    except IndexerError as exc:
        return _error_response(exc)
    return api_response(_search_payload(service, result))


@router.post("/search")
async def search_media(request: Request, payload: Any = Body(...)):
    require_api_login(request)
    try:
        media_request, site_ids = _media_request(payload)
        service = get_indexer_service()
        if "text/event-stream" in request.headers.get("accept", ""):
            return _stream_response(request, service, lambda observer: service.search_media(media_request, site_ids, on_progress=observer))
        result = await service.search_media(media_request, site_ids)
    except IndexerError as exc:
        return _error_response(exc)
    return api_response(_search_payload(service, result))


def _validate_batch_payload(data: Any) -> tuple[list[str], str]:
    if not isinstance(data, dict):
        raise ValueError("批量下载参数必须是 JSON 对象")
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
    if target not in _DOWNLOAD_TARGETS:
        raise ValueError("下载目标仅支持 qb、guangya 或 both")
    return result_ids, target


@router.post("/download")
async def download(request: Request, data: Any = Body(default=None)):
    require_api_login(request)
    if not isinstance(data, dict):
        return api_error("下载参数必须是 JSON 对象", 400)
    result_id = str(data.get("result_id") or "").strip()
    target = str(data.get("target") or "").strip().lower()
    if not result_id or len(result_id) > 128:
        return api_error("资源结果 ID 无效", 400)
    if target not in _DOWNLOAD_TARGETS:
        return api_error("下载目标仅支持 qb、guangya 或 both", 400)

    service = get_indexer_service()
    try:
        item = await download_indexer_result(service, result_id, target)
    except IndexerError as exc:
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
    if target not in _DOWNLOAD_TARGETS:
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
        return await download_indexer_result_public(service, result_id, target)

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
