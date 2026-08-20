"""资源站结果解析、持久化与下载分发公共服务。

供 Telegram 专用资源站 worker 复用 Web 下载链的同等安全约束：
种子大小、Content-Type、重复请求和部分成功都使用稳定契约。
"""
from __future__ import annotations

import asyncio
import threading
import weakref
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from app.indexers.errors import (
    IndexerError,
    IndexerInvalidResponse,
    IndexerResponseTooLarge,
    IndexerUnavailable,
)
from app.indexers.models import ResolvedDownload
from app import database as db
from app.logger import get_logger
from app.modules.download_dispatcher import (
    create_request,
    dispatch_missing_targets,
    dispatch_request,
    normalize_download_url,
    request_key,
    request_keys,
    torrent_download_input,
)

logger = get_logger(__name__)

DOWNLOAD_TARGETS = frozenset({"qb", "guangya", "both"})
_DOWNLOAD_LIMIT = 3
_DOWNLOAD_LIMITERS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)
_DOWNLOAD_LIMITERS_LOCK = threading.Lock()


class InvalidDownloadData(ValueError):
    """已解析资源无法转换为安全下载输入。"""


class DownloadRequestCreationError(RuntimeError):
    """下载请求未能持久化。"""


def _download_limiter() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    with _DOWNLOAD_LIMITERS_LOCK:
        limiter = _DOWNLOAD_LIMITERS.get(loop)
        if limiter is None:
            limiter = asyncio.Semaphore(_DOWNLOAD_LIMIT)
            _DOWNLOAD_LIMITERS[loop] = limiter
        return limiter


async def _resolve(service, result_id: str) -> tuple[Any, ResolvedDownload]:
    item = service.result_store.get(result_id)
    resolved = await service.resolve(result_id)
    if not isinstance(resolved, ResolvedDownload):
        raise IndexerInvalidResponse("provider returned invalid download")
    return item, resolved


def _torrent_filename(resolved: ResolvedDownload) -> str:
    filename = str(resolved.filename or "").strip()
    if filename:
        return PurePosixPath(filename).name or "resource.torrent"
    if isinstance(resolved.value, bytes):
        return "resource.torrent"
    parsed = urlsplit(str(resolved.value or ""))
    return PurePosixPath(parsed.path).name or "resource.torrent"


async def _resolved_download_input(service, stored, resolved: ResolvedDownload):
    try:
        if resolved.kind == "magnet":
            return normalize_download_url(str(resolved.value or ""))
        if isinstance(resolved.value, bytes):
            adapter = service.registry.get(stored.site_id)
            max_bytes = int(
                getattr(adapter.http, "max_response_bytes", 2 * 1024 * 1024)
            )
            if len(resolved.value) > max_bytes:
                raise IndexerResponseTooLarge(
                    "provider returned oversized torrent bytes"
                )
            return torrent_download_input(_torrent_filename(resolved), resolved.value)

        adapter = service.registry.get(stored.site_id)
        response = await adapter.http.get(str(resolved.value or ""))
        if response.status_code != 200:
            raise IndexerUnavailable("torrent upstream failed")
        content_type = str(response.headers.get("content-type") or "").split(
            ";", 1
        )[0].lower()
        if content_type not in {
            "application/x-bittorrent",
            "application/octet-stream",
        }:
            raise IndexerInvalidResponse("torrent content type invalid")
        return torrent_download_input(_torrent_filename(resolved), response.body)
    except ValueError as exc:
        raise InvalidDownloadData() from exc


def _public_target_results(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    values = set(value)
    return [target for target in ("qb", "guangya") if target in values]


def _failed_download_result(result_id: str, target: str, error: str) -> dict[str, Any]:
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


def _requested_targets(target: str) -> tuple[str, ...]:
    return ("qb", "guangya") if target == "both" else (target,)


def _target_name(targets: tuple[str, ...]) -> str:
    return "both" if set(targets) == {"qb", "guangya"} else targets[0]


def _missing_terminal_targets(existing, target: str) -> tuple[str, ...]:
    # manual_review 表示远端结果未知，只能由待处理页显式创建 successor 请求。
    retryable_statuses = {"", "failed"}
    statuses = {
        "qb": str(existing["qb_status"] or ""),
        "guangya": str(existing["gy_status"] or ""),
    }
    return tuple(name for name in _requested_targets(target) if statuses[name] in retryable_statuses)


def _duplicate_dispatch_result(request_id: int) -> dict[str, Any]:
    return {
        "handled": False,
        "ok": False,
        "request_id": request_id,
        "status": "duplicate",
        "succeeded": [],
        "failed": [],
        "duplicate": True,
        "error": "该目标已提交或正在处理",
    }


def _persist_and_dispatch(
    item,
    origin: str,
    target: str,
    *,
    chat_id: str = "",
    user_id: str = "",
    message_id: str = "",
    admission_id: int | None = None,
) -> tuple[dict[str, Any], int, dict[str, Any]]:
    keys = request_keys(item)
    existing = db.get_download_request_by_request_key(keys[0])
    if existing is None and len(keys) > 1:
        existing = db.get_download_request_by_request_keys(keys[1:])
    dispatch_target = target
    created: dict[str, Any] | None = None
    request_id = 0
    if existing is not None:
        existing_id = int(existing["id"] or 0)
        existing_status = str(existing["status"] or "")
        if existing_status == "manual_review":
            if admission_id is not None:
                db.bind_media_download_admission_request(admission_id, existing_id)
            return (
                {"id": existing_id, "created": False},
                existing_id,
                _duplicate_dispatch_result(existing_id),
            )
        if existing_status in {"completed", "failed"}:
            missing = _missing_terminal_targets(existing, target)
            if not missing:
                if admission_id is not None:
                    db.bind_media_download_admission_request(admission_id, existing_id)
                return {"id": existing_id, "created": False}, existing_id, _duplicate_dispatch_result(existing_id)
            dispatch_target = _target_name(missing)
        elif existing_status == "pending":
            # 请求已持久化但尚未被任何后端认领（常见于创建后进程中断）。
            # 复用原请求并走 dispatch_request 的原子 pending -> submitting CAS，
            # 不能把它误判成已经提交的重复请求。
            if admission_id is not None:
                db.bind_media_download_admission_request(admission_id, existing_id)
            created = {"id": existing_id, "created": False}
            request_id = existing_id
        else:
            if admission_id is not None:
                db.bind_media_download_admission_request(admission_id, existing_id)
            appended = dispatch_missing_targets(existing_id, target)
            if appended.get("handled"):
                return {"id": existing_id, "created": False}, existing_id, appended
            return {"id": existing_id, "created": False}, existing_id, _duplicate_dispatch_result(existing_id)
    if created is None:
        created = create_request(
            item,
            chat_id,
            message_id,
            origin=origin,
            user_id=user_id,
            **({"admission_id": admission_id} if admission_id is not None else {}),
        )
        request_id = int(created.get("id") or 0)
        if not request_id:
            raise DownloadRequestCreationError()
    try:
        result = dispatch_request(request_id, dispatch_target)
    except Exception as exc:
        # 请求已与 admission 原子绑定；此后任何异常都不能再当成可安全重试。
        # 远端可能已经接收任务，保守转入人工核验并持续占用 media_key。
        updates: dict[str, Any] = {
            "status": "manual_review",
            "error": "下载后端提交结果未知，请先核对下载器，勿直接重复提交",
            "completed_at": db.now(),
        }
        if dispatch_target in {"qb", "both"}:
            updates["qb_status"] = "manual_review"
        if dispatch_target in {"guangya", "both"}:
            updates["gy_status"] = "manual_review"
        try:
            db.update_download_request_and_sync_media_admission(request_id, **updates)
        except Exception as persist_exc:
            logger.error(
                "下载异常状态持久化失败 request_id=%s type=%s",
                request_id, type(persist_exc).__name__,
            )
        logger.warning(
            "下载提交结果未知 request_id=%s type=%s",
            request_id, type(exc).__name__,
        )
        return created, request_id, {
            "handled": True,
            "ok": False,
            "request_id": request_id,
            "status": "manual_review",
            "succeeded": [],
            "failed": [],
            "duplicate": False,
            "error": "下载后端提交结果未知，请先核对下载器，勿直接重复提交",
        }
    return created, request_id, result


async def download_indexer_result(
    service,
    result_id: str,
    target: str,
    *,
    chat_id: str = "",
    user_id: str = "",
    message_id: str = "",
    admission_id: int | None = None,
) -> dict[str, Any]:
    """解析一个 opaque 资源站结果并提交到指定目标。"""
    normalized_target = str(target or "").strip().lower()
    if normalized_target not in DOWNLOAD_TARGETS:
        return _failed_download_result(result_id, normalized_target, "下载目标无效")

    async with _download_limiter():
        stored, resolved = await _resolve(service, result_id)
        item = await _resolved_download_input(service, stored, resolved)
        created, request_id, result = await asyncio.to_thread(
            _persist_and_dispatch,
            item,
            f"indexer:{stored.site_id}",
            normalized_target,
            chat_id=str(chat_id),
            user_id=str(user_id),
            message_id=str(message_id),
            **({"admission_id": admission_id} if admission_id is not None else {}),
        )

    duplicate = bool(result.get("duplicate"))
    succeeded = _public_target_results(result.get("succeeded"))
    failed = _public_target_results(result.get("failed"))
    dispatch_status = str(result.get("status") or "")
    if dispatch_status == "manual_review":
        status = "manual_review"
        error = str(result.get("error") or "下载提交结果需要人工核验")
    elif duplicate:
        status = "duplicate"
        error = "该下载请求已提交或正在处理"
    elif succeeded and failed:
        status = "partial"
        error = ""
    elif succeeded:
        status = "submitted"
        error = ""
    else:
        status = "failed"
        error = "下载提交失败"
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


async def download_indexer_result_public(
    service,
    result_id: str,
    target: str,
    *,
    chat_id: str = "",
    user_id: str = "",
    message_id: str = "",
    admission_id: int | None = None,
) -> dict[str, Any]:
    """返回适合 Web/TG 展示的稳定错误契约，不泄露上游细节。"""
    try:
        return await download_indexer_result(
            service,
            result_id,
            target,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
            **({"admission_id": admission_id} if admission_id is not None else {}),
        )
    except IndexerError as exc:
        return _failed_download_result(result_id, target, exc.public_message)
    except InvalidDownloadData:
        return _failed_download_result(result_id, target, "资源下载数据无效")
    except DownloadRequestCreationError:
        return _failed_download_result(result_id, target, "下载请求创建失败")
    except Exception:
        return _failed_download_result(result_id, target, "下载处理失败")
