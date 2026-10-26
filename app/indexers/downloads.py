"""资源站结果解析、持久化与下载分发的唯一权威服务。

Web、Telegram、Agent 与订阅统一复用同一套解析、幂等准入、
异常恢复和分发状态机，避免入口间产生行为差异。
"""
from __future__ import annotations

import asyncio
import threading
import weakref
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any, TypedDict
from urllib.parse import urlsplit

from app import database as db
from app.clients.http import HttpError, HttpInvalidResponse, HttpResponseTooLarge
from app.indexers.config import SENSITIVE_INDEXER_SITE_IDS
from app.indexers.errors import IndexerUnavailable, IndexerValidationError
from app.indexers.models import ResolvedDownload
from app.logger import get_logger
from app.modules.download_dispatcher import (
    DownloadInput,
    create_request,
    dispatch_missing_targets,
    dispatch_request,
    download_resubmit_capabilities,
    normalize_download_url,
    prepare_download_input,
    public_dispatch_summary,
    request_keys,
    request_source_alias_key,
    resubmit_download_request,
    torrent_download_input,
)
from app.repositories.download_requests import (
    download_display_title,
    enrich_download_display_title,
    usable_download_title,
)

logger = get_logger(__name__)

DOWNLOAD_TARGETS = frozenset({"qb", "guangya", "both"})
INDEXER_RESUBMITTABLE_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "manual_review"}
)
_DOWNLOAD_LIMIT = 3
# Semaphore 在发生等待后会强持有事件循环；缓存值也必须弱引用，
# 否则弱 key 被自己的 value 反向保活，反复 asyncio.run 会泄漏已关闭循环。
_DOWNLOAD_LIMITERS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, weakref.ReferenceType[asyncio.Semaphore]
] = (
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
        reference = _DOWNLOAD_LIMITERS.get(loop)
        limiter = reference() if reference is not None else None
        if limiter is None:
            limiter = asyncio.Semaphore(_DOWNLOAD_LIMIT)
            _DOWNLOAD_LIMITERS[loop] = weakref.ref(limiter)
        return limiter


async def _resolve(
    service, result_id: str, *, scope: str = "manual"
) -> tuple[Any, ResolvedDownload]:
    item = service.get_result(result_id, scope=scope)
    resolved = await service.resolve(result_id, scope=scope)
    if not isinstance(resolved, ResolvedDownload):
        raise HttpInvalidResponse("provider returned invalid download")
    return item, resolved


class _DownloadDestination(TypedDict, total=False):
    gy_target_dir: str
    gy_target_name: str


class _AdmissionOptions(TypedDict, total=False):
    admission_id: int


def indexer_download_destination(item: Any, target: str) -> _DownloadDestination:
    """按服务端保存的来源分类路由；预检与提交共享，不从资源标题猜测。"""
    if target not in {"guangya", "both"} or item.site_id not in SENSITIVE_INDEXER_SITE_IDS:
        return {}
    from app.modules.organize_sources import list_nsfw_download_sources

    sources = list_nsfw_download_sources()
    if len(sources) != 1:
        raise IndexerValidationError(
            "sensitive cloud download requires an unambiguous organizing source",
            public_message=(
                "尚未配置启用的 NSFW 整理来源，不能将此资源提交到普通离线目录"
                if not sources else
                "存在多个 NSFW 整理来源，请先明确唯一接收来源，未提交下载"
            ),
        )
    return {"gy_target_dir": sources[0]["id"], "gy_target_name": sources[0]["name"]}


def _torrent_filename(resolved: ResolvedDownload) -> str:
    filename = (resolved.filename or "").strip()
    if filename:
        return PurePosixPath(filename).name or "resource.torrent"
    if isinstance(resolved.value, bytes):
        return "resource.torrent"
    parsed = urlsplit(resolved.value or "")
    return PurePosixPath(parsed.path).name or "resource.torrent"


async def _resolved_download_input(service, stored, resolved: ResolvedDownload):
    try:
        if resolved.kind == "magnet":
            return replace(normalize_download_url(str(resolved.value or "")), display_title=str(stored.title or ""))
        client = service.registry.get(stored.site_id).http_client_for_result(stored)
        if isinstance(resolved.value, bytes):
            max_bytes = int(getattr(client, "max_response_bytes", 2 * 1024 * 1024))
            if len(resolved.value) > max_bytes:
                raise HttpResponseTooLarge(
                    "provider returned oversized torrent bytes"
                )
            return torrent_download_input(_torrent_filename(resolved), resolved.value)

        response = await client.get(resolved.value or "")
        if response.status_code != 200:
            raise IndexerUnavailable("torrent upstream failed")
        content_type = str(response.headers.get("content-type") or "").split(
            ";", 1
        )[0].lower()
        if content_type not in {
            "application/x-bittorrent",
            "application/octet-stream",
        }:
            raise HttpInvalidResponse("torrent content type invalid")
        return torrent_download_input(_torrent_filename(resolved), response.body)
    except ValueError as exc:
        raise InvalidDownloadData() from exc


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


def _duplicate_dispatch_result(existing, target: str) -> dict[str, Any]:
    request_id = int(existing["id"] or 0)
    existing_status = str(existing["status"] or "").strip().lower()
    can_resubmit = False
    if existing_status in INDEXER_RESUBMITTABLE_STATUSES:
        try:
            capability = download_resubmit_capabilities(
                existing,
                allow_completed=True,
            ).get(target) or {}
            can_resubmit = bool(capability.get("enabled"))
        except (IndexError, KeyError, TypeError, ValueError):
            # 兼容迁移期残缺记录；重复保护本身不能因展示能力判断而失效。
            can_resubmit = False
    if can_resubmit:
        error = "已有历史任务"
    elif existing_status in {"pending", "submitting", "submitted", "downloading"}:
        error = "任务正在处理"
    elif existing_status == "manual_review":
        error = "等待核对"
    else:
        error = "该资源已提交"
    return {
        "handled": False,
        "ok": False,
        "request_id": request_id,
        "status": "duplicate",
        "succeeded": [],
        "failed": [],
        "duplicate": True,
        "error": error,
        "existing_status": existing_status,
        "can_resubmit": can_resubmit,
        "resubmit_target": target if can_resubmit else "",
    }


def resubmit_indexer_download_request(
    request_id: int,
    target: str,
) -> dict[str, Any]:
    """按用户明确操作为资源站历史任务创建 successor 请求。"""
    normalized_target = (target or "").strip().lower()
    if normalized_target not in DOWNLOAD_TARGETS:
        return {"ok": False, "error": "下载目标无效"}
    existing = db.get_download_request(request_id)
    if existing is None:
        return {"ok": False, "not_found": True, "error": "下载请求不存在"}
    existing_status = str(existing["status"] or "").strip().lower()
    if existing_status not in INDEXER_RESUBMITTABLE_STATUSES:
        return _duplicate_dispatch_result(existing, normalized_target)
    capability = download_resubmit_capabilities(
        existing,
        allow_completed=True,
    ).get(normalized_target) or {}
    if not capability.get("enabled"):
        return {
            "ok": False,
            "blocked": True,
            "error": str(capability.get("reason") or "当前目标不可重新提交"),
        }
    return resubmit_download_request(
        request_id,
        normalized_target,
        allow_completed=True,
        origin=str(existing["origin"] or "indexer"),
    )


def _persist_and_dispatch(
    item,
    origin: str,
    target: str,
    *,
    chat_id: str = "",
    user_id: str = "",
    message_id: str = "",
    admission_id: int | None = None,
    gy_target_dir: str = "",
    gy_target_name: str = "",
    qb_save_path: str | None = None,
    qb_category: str | None = None,
    qb_runtime_config: dict[str, Any] | None = None,
    qb_task_id_hint: str = "",
    rss_item_id: int | None = None,
    rss_entry_id: int | None = None,
    log_path: str | None = None,
) -> tuple[dict[str, Any], int, dict[str, Any]]:
    dispatch_kwargs: dict[str, Any] = {}
    if gy_target_dir:
        dispatch_kwargs["gy_target_dir"] = gy_target_dir
    if gy_target_name:
        dispatch_kwargs["gy_target_name"] = gy_target_name
    if qb_save_path is not None:
        dispatch_kwargs["qb_save_path"] = qb_save_path
    if qb_category is not None:
        dispatch_kwargs["qb_category"] = qb_category
    if qb_runtime_config is not None:
        dispatch_kwargs["qb_runtime_config"] = qb_runtime_config
    if qb_task_id_hint:
        dispatch_kwargs["qb_task_id_hint"] = qb_task_id_hint
    if rss_item_id is not None:
        dispatch_kwargs["rss_item_id"] = rss_item_id
    if log_path is not None:
        dispatch_kwargs["log_path"] = log_path

    item = prepare_download_input(item, target)
    keys = request_keys(item)
    if rss_entry_id is not None:
        from app.repositories.rss import bind_rss_entry_download
        bind_rss_entry_download(rss_entry_id, keys[0], target)
    existing = db.get_download_request_by_request_key(keys[0])
    if existing is None and len(keys) > 1:
        existing = db.get_download_request_by_request_keys(
            keys, source_alias_key=request_source_alias_key(item),
        )
    if (existing is not None and existing["status"] == "pending"
            and item.kind == existing["kind"] == "http" and item.torrent_data
            and item.source_value == existing["source_value"]):
        from app.repositories.download_requests import bind_verified_torrent_identity

        # 复用历史HTTP请求时也要保留本次已验证的bytes，不能丢失动态URL的MIME语义。
        owner = bind_verified_torrent_identity(
            int(existing["id"]), item.source_value, item.torrent_data, keys, pending_only=True,
            source_alias_key=request_source_alias_key(item),
        )
        existing = db.get_download_request(owner)
    dispatch_target = target
    created: dict[str, Any] | None = None
    request_id = 0
    if existing is not None:
        existing_id = int(existing["id"] or 0)
        existing_status = str(existing["status"] or "")
        if (usable_download_title(item.display_title)
                and not usable_download_title(download_display_title(existing))):
            enrich_download_display_title(existing_id, item.display_title)
        if existing_status == "manual_review":
            if admission_id is not None:
                db.bind_media_download_admission_request(admission_id, existing_id)
            return (
                {"id": existing_id, "created": False},
                existing_id,
                _duplicate_dispatch_result(existing, target),
            )
        if existing_status in {"completed", "failed", "cancelled"}:
            missing = _missing_terminal_targets(existing, target)
            if not missing:
                if admission_id is not None:
                    db.bind_media_download_admission_request(admission_id, existing_id)
                return {
                    "id": existing_id,
                    "created": False,
                }, existing_id, _duplicate_dispatch_result(existing, target)
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
            if (item.kind == existing["kind"] == "http" and item.torrent_data
                    and existing["torrent_data"] is None
                    and item.source_value == existing["source_value"]
                    and target in {"guangya", "both"}):
                # 只补同来源历史 HTTP 请求缺失的 bytes；已持久化内容和其他协议保持原语义。
                dispatch_kwargs["prepared_input"] = item
            appended = dispatch_missing_targets(
                existing_id,
                target,
                **dispatch_kwargs,
            )
            if appended.get("handled"):
                return {"id": existing_id, "created": False}, existing_id, appended
            return {
                "id": existing_id,
                "created": False,
            }, existing_id, _duplicate_dispatch_result(existing, target)
    if created is None:
        admission_options: _AdmissionOptions = {"admission_id": admission_id} if admission_id is not None else {}
        created = create_request(
            item,
            chat_id,
            message_id,
            origin=origin,
            user_id=user_id,
            **admission_options,
        )
        request_id = int(created.get("id") or 0)
        if not request_id:
            raise DownloadRequestCreationError()
    try:
        result = dispatch_request(
            request_id,
            dispatch_target,
            **dispatch_kwargs,
        )
    except Exception as exc:
        # 只将仍持有认领、受理未知的目标转人工核验；已收尾/已取消的
        # 同伴不能因后续目标落盘失败而被覆盖，准入仍沿同一收尾事务同步。
        unknown_error = "下载后端提交结果未知，请先核对下载器，勿直接重复提交"
        for source, prefix in (("qb", "qb"), ("guangya", "gy")):
            if dispatch_target not in {source, "both"}:
                continue
            try:
                db.finalize_download_request_submission(
                    request_id, (source,),
                    **{f"{prefix}_status": "manual_review", "error": f"{source}: {unknown_error}"},
                )
            except Exception as persist_exc:
                logger.error(
                    "下载异常状态持久化失败 request_id=%s type=%s",
                    request_id, type(persist_exc).__name__,
                )
        logger.warning(
            "下载提交结果未知 request_id=%s type=%s",
            request_id, type(exc).__name__,
        )
        try:
            current = db.get_download_request(request_id)
        except Exception as read_exc:
            logger.error(
                "下载异常状态读取失败 request_id=%s type=%s",
                request_id, type(read_exc).__name__,
            )
            current = None
        return created, request_id, {
            "handled": True,
            "ok": False,
            "request_id": request_id,
            "status": str(current["status"] or "manual_review") if current else "manual_review",
            "succeeded": [source for source, prefix in (("qb", "qb"), ("guangya", "gy"))
                          if dispatch_target in {source, "both"} and current is not None
                          and current[f"{prefix}_status"] in {"submitted", "downloading", "completed"}],
            "failed": [],
            "duplicate": False,
            "error": unknown_error,
            "review_required": current is None or any(
                current[f"{prefix}_status"] in {"submitting", "outcome_unknown", "manual_review"}
                for source, prefix in (("qb", "qb"), ("guangya", "gy"))
                if dispatch_target in {source, "both"}
            ),
        }
    return created, request_id, result


def submit_download_input(
    item: DownloadInput,
    target: str,
    *,
    origin: str,
    chat_id: str = "",
    user_id: str = "",
    message_id: str = "",
    admission_id: int | None = None,
    gy_target_dir: str = "",
    gy_target_name: str = "",
    qb_save_path: str | None = None,
    qb_category: str | None = None,
    qb_runtime_config: dict[str, Any] | None = None,
    qb_task_id_hint: str = "",
    rss_item_id: int | None = None,
    rss_entry_id: int | None = None,
    log_path: str | None = None,
) -> dict[str, Any]:
    """持久化并分发一个已规范化下载输入。

    这是非资源站入口（RSS 等）接入统一 ``download_request`` 状态机的
    唯一同步入口。调用方可以读取 ``summary`` 做稳定业务映射；``dispatch``
    仅供同进程内部保留失败分类，不应直接透传给 Web/TG。
    """
    normalized_target = (target or "").strip().lower()
    if normalized_target not in DOWNLOAD_TARGETS:
        raise ValueError("下载目标无效")
    created, request_id, dispatch = _persist_and_dispatch(
        item,
        (origin or "").strip() or "unknown",
        normalized_target,
        chat_id=chat_id,
        user_id=user_id,
        message_id=message_id,
        admission_id=admission_id,
        gy_target_dir=(gy_target_dir or ""),
        gy_target_name=(gy_target_name or ""),
        qb_save_path=qb_save_path,
        qb_category=qb_category,
        qb_runtime_config=qb_runtime_config,
        qb_task_id_hint=(qb_task_id_hint or ""),
        rss_item_id=rss_item_id,
        rss_entry_id=rss_entry_id,
        log_path=log_path,
    )
    return {
        "request_id": request_id,
        "created": bool(created.get("created")),
        "target": normalized_target,
        "summary": public_dispatch_summary(dispatch),
        "dispatch": dispatch,
    }


async def download_indexer_result(
    service,
    result_id: str,
    target: str,
    *,
    chat_id: str = "",
    user_id: str = "",
    message_id: str = "",
    admission_id: int | None = None,
    origin_namespace: str = "indexer",
    scope: str = "manual",
    _public_errors: bool = False,
) -> dict[str, Any]:
    """按服务端调用入口校验后解析并提交资源结果。"""
    normalized_target = (target or "").strip().lower()
    if normalized_target not in DOWNLOAD_TARGETS:
        return _failed_download_result(result_id, normalized_target, "下载目标无效")
    normalized_origin_namespace = (origin_namespace or "").strip().lower()
    if normalized_origin_namespace not in {"indexer", "agent"}:
        raise IndexerValidationError("invalid download origin namespace")

    async with _download_limiter():
        try:
            stored, resolved = await _resolve(service, result_id, scope=scope)
            destination = indexer_download_destination(stored, normalized_target)
            item = await _resolved_download_input(service, stored, resolved)
        except (HttpError, InvalidDownloadData) as exc:
            if not _public_errors:
                raise
            # 仅解析 / 输入转换阶段能证明尚未创建请求或调用下载后端。
            # public 入口后续的兜底异常没有此标记；非 public 入口仍保留原异常契约。
            error = exc.public_message if isinstance(exc, HttpError) else "资源下载数据无效"
            return {
                **_failed_download_result(result_id, normalized_target, error),
                "pre_submission_failure": True,
            }
        admission_options: _AdmissionOptions = {"admission_id": admission_id} if admission_id is not None else {}
        submitted = await asyncio.to_thread(
            submit_download_input,
            item,
            normalized_target,
            origin=f"{normalized_origin_namespace}:{stored.site_id}",
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
            **destination,
            **admission_options,
        )

    request_id = int(submitted["request_id"])
    public = submitted["summary"]
    duplicate = public["duplicate"]
    succeeded = public["succeeded"]
    failed = public["failed"]
    status = public["status"]
    error = public["error"]
    response = {
        "result_id": result_id,
        "ok": status in {"submitted", "partial"},
        "request_id": request_id,
        "created": bool(submitted["created"]),
        "target": normalized_target,
        "status": status,
        "succeeded": succeeded,
        "failed": failed,
        "duplicate": duplicate,
        "error": error,
    }
    if duplicate:
        response.update({
            "existing_status": public.get("existing_status", ""),
            "can_resubmit": bool(public.get("can_resubmit")),
            "resubmit_target": public.get("resubmit_target", ""),
        })
    return response


async def download_indexer_result_public(
    service,
    result_id: str,
    target: str,
    *,
    chat_id: str = "",
    user_id: str = "",
    message_id: str = "",
    admission_id: int | None = None,
    origin_namespace: str = "indexer",
    scope: str = "manual",
) -> dict[str, Any]:
    """返回适合 Web/TG/Agent 展示的稳定错误契约，不泄露上游细节。"""
    try:
        return await download_indexer_result(
            service,
            result_id,
            target,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
            origin_namespace=origin_namespace,
            scope=scope,
            _public_errors=True,
            **({"admission_id": admission_id} if admission_id is not None else {}),
        )
    except HttpError as exc:
        return _failed_download_result(result_id, target, exc.public_message)
    except InvalidDownloadData:
        return _failed_download_result(result_id, target, "资源下载数据无效")
    except DownloadRequestCreationError:
        return _failed_download_result(result_id, target, "下载请求创建失败")
    except Exception:
        return _failed_download_result(result_id, target, "下载处理失败")
