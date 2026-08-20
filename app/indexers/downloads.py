"""Indexer 搜索结果的安全解析与下载分发服务。"""
from __future__ import annotations

import asyncio
import threading
import weakref
from pathlib import PurePosixPath
from typing import Any, Callable
from urllib.parse import urlsplit

from app.indexers.errors import (
    IndexerError,
    IndexerInvalidResponse,
    IndexerResponseTooLarge,
    IndexerUnavailable,
    IndexerValidationError,
)
from app.indexers.models import ResolvedDownload
from app.modules.download_dispatcher import (
    create_request,
    dispatch_request,
    normalize_download_url,
    torrent_download_input,
)

_DOWNLOAD_TARGETS = frozenset({"qb", "guangya", "both"})
_DOWNLOAD_LIMIT = 3
_DOWNLOAD_LIMITERS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()
_DOWNLOAD_LIMITERS_LOCK = threading.Lock()


class InvalidDownloadData(ValueError):
    """已解析资源无法转换为安全下载输入。"""


class DownloadRequestCreationError(RuntimeError):
    """下载请求未能持久化。"""


def _torrent_filename(resolved: ResolvedDownload) -> str:
    filename = str(resolved.filename or "").strip()
    if filename:
        return PurePosixPath(filename).name or "resource.torrent"
    if isinstance(resolved.value, bytes):
        return "resource.torrent"
    parsed = urlsplit(str(resolved.value or ""))
    return PurePosixPath(parsed.path).name or "resource.torrent"


async def _resolved_download_input(
    service,
    stored,
    resolved: ResolvedDownload,
    *,
    normalize,
    torrent_input,
):
    try:
        if resolved.kind == "magnet":
            return normalize(str(resolved.value or ""))
        if isinstance(resolved.value, bytes):
            adapter = service.registry.get(stored.site_id)
            max_bytes = int(getattr(adapter.http, "max_response_bytes", 2 * 1024 * 1024))
            if len(resolved.value) > max_bytes:
                raise IndexerResponseTooLarge("provider returned oversized torrent bytes")
            return torrent_input(_torrent_filename(resolved), resolved.value)

        adapter = service.registry.get(stored.site_id)
        response = await adapter.http.get(str(resolved.value or ""))
        if response.status_code != 200:
            raise IndexerUnavailable("torrent upstream failed")
        content_type = (
            str(response.headers.get("content-type") or "")
            .split(";", 1)[0]
            .lower()
        )
        if content_type not in {"application/x-bittorrent", "application/octet-stream"}:
            raise IndexerInvalidResponse("torrent content type invalid")
        return torrent_input(_torrent_filename(resolved), response.body)
    except ValueError as exc:
        raise InvalidDownloadData() from exc


def _public_target_results(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    values = set(value)
    return [target for target in ("qb", "guangya") if target in values]


def failed_download_result(result_id: str, target: str, error: str) -> dict[str, Any]:
    return {
        "result_id": result_id,
        "ok": False,
        "request_id": 0,
        "created": False,
        "target": target,
        "status": "failed",
        "succeeded": [],
        "failed": [],
        "duplicate": False,
        "error": error,
    }


def _download_limiter() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    with _DOWNLOAD_LIMITERS_LOCK:
        limiter = _DOWNLOAD_LIMITERS.get(loop)
        if limiter is None:
            limiter = asyncio.Semaphore(_DOWNLOAD_LIMIT)
            _DOWNLOAD_LIMITERS[loop] = limiter
        return limiter


def _persist_and_dispatch(
    item,
    origin: str,
    target: str,
    *,
    create,
    dispatch,
) -> tuple[dict[str, Any], int, dict[str, Any]]:
    created = create(item, "", "", origin=origin)
    request_id = int(created.get("id") or 0)
    if not request_id:
        raise DownloadRequestCreationError()
    return created, request_id, dispatch(request_id, target)


async def download_result(
    service,
    result_id: str,
    target: str,
    *,
    origin_namespace: str = "indexer",
    normalize: Callable = normalize_download_url,
    torrent_input: Callable = torrent_download_input,
    create: Callable = create_request,
    dispatch: Callable = dispatch_request,
) -> dict[str, Any]:
    """由 opaque result_id 恢复、解析并分发一个资源。"""
    normalized_target = str(target or "").strip().lower()
    if normalized_target not in _DOWNLOAD_TARGETS:
        raise IndexerValidationError("invalid download target")
    normalized_origin_namespace = str(origin_namespace or "").strip().lower()
    if normalized_origin_namespace not in {"indexer", "agent"}:
        raise IndexerValidationError("invalid download origin namespace")

    async with _download_limiter():
        stored = service.result_store.get(result_id)
        resolved = await service.resolve(result_id)
        if not isinstance(resolved, ResolvedDownload):
            raise IndexerInvalidResponse("provider returned invalid download")
        item = await _resolved_download_input(
            service,
            stored,
            resolved,
            normalize=normalize,
            torrent_input=torrent_input,
        )
        created, request_id, result = await asyncio.to_thread(
            _persist_and_dispatch,
            item,
            f"{normalized_origin_namespace}:{stored.site_id}",
            normalized_target,
            create=create,
            dispatch=dispatch,
        )

    duplicate = bool(result.get("duplicate"))
    succeeded = _public_target_results(result.get("succeeded"))
    failed = _public_target_results(result.get("failed"))
    if duplicate:
        status, error = "duplicate", "该下载请求已提交或正在处理"
    elif succeeded and failed:
        status, error = "partial", ""
    elif succeeded:
        status, error = "submitted", ""
    else:
        status, error = "failed", "下载提交失败"
    return {
        "result_id": result_id,
        "ok": status in {"submitted", "partial"},
        "request_id": request_id,
        "created": bool(created.get("created")),
        "target": normalized_target,
        "status": status,
        "succeeded": succeeded,
        "failed": failed,
        "duplicate": duplicate,
        "error": error,
    }


async def download_result_public(
    service,
    result_id: str,
    target: str,
    **dependencies,
) -> dict[str, Any]:
    """批量/API 场景的固定错误映射，禁止透传上游异常。"""
    try:
        return await download_result(service, result_id, target, **dependencies)
    except IndexerError as exc:
        return failed_download_result(result_id, target, exc.public_message)
    except InvalidDownloadData:
        return failed_download_result(result_id, target, "资源下载数据无效")
    except DownloadRequestCreationError:
        return failed_download_result(result_id, target, "下载请求创建失败")
    except Exception:
        return failed_download_result(result_id, target, "下载处理失败")
