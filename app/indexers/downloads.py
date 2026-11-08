"""资源站结果解析、持久化与下载分发的唯一权威服务。

Web、Telegram、Agent 与订阅统一复用同一套解析、幂等准入、
异常恢复和分发状态机，避免入口间产生行为差异。
"""
from __future__ import annotations

import asyncio
import threading
import weakref
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any, TypedDict
from urllib.parse import urlsplit

from app import config, database as db
from app.clients.guangya import GuangYaClient, close_guangya_client
from app.clients.http import HttpError, HttpInvalidResponse, HttpResponseTooLarge
from app.indexers.config import AIPAN_URL, DYGANG_URL, YS5266_URL, SENSITIVE_INDEXER_SITE_IDS
from app.indexers.errors import IndexerResultExpired, IndexerUnavailable, IndexerValidationError
from app.indexers.models import ResolvedDownload
from app.logger import get_logger
from app.modules import episode_audit
from app.modules.media_identity import normalize_episode_verification
from app.modules.download_dispatcher import (
    DownloadInput,
    create_request,
    dispatch_missing_targets,
    dispatch_request,
    download_resubmit_capabilities,
    normalize_download_url,
    prepare_download_input,
    public_dispatch_summary,
    request_key,
    request_keys,
    request_source_alias_key,
    resubmit_download_request,
    route_download_url,
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
_SPLIT_SITE_HOSTS = {
    host for base in (AIPAN_URL, DYGANG_URL, YS5266_URL)
    for hostname in (urlsplit(base).hostname or "",)
    for host in (hostname, hostname.removeprefix("www."))
}
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


class DownloadSnapshotChanged(IndexerValidationError):
    code = "confirmation_stale"
    default_public_message = "资源或下载配置已变化，请重新预检"


def prepare_download_verifications(
    service, result_ids: list[str], targets: list[dict], *, scope: str = "manual",
) -> dict[str, list[dict]]:
    """只读核验选中资源的缺集目标；按作品/季共享审计，不接受客户端缺集结论。"""
    if not isinstance(result_ids, list) or any(not isinstance(item, str) or not item for item in result_ids):
        raise ValueError("result_ids 必须是资源 ID 列表")
    if not isinstance(targets, list):
        raise ValueError("verification targets 必须是列表")
    selected = set(result_ids)
    grouped: dict[tuple, list[tuple[str, dict]]] = {}
    seen: set[str] = set()
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("复核目标必须是对象")
        result_id = target.get("result_id")
        if not isinstance(result_id, str) or result_id not in selected:
            raise ValueError("复核目标必须属于当前选中的资源 ID")
        seen.add(result_id)
        if "target_episode" in target:
            raise ValueError("复核目标使用 episode 指定集号")
        arguments = {key: value for key, value in target.items() if key != "result_id"}
        arguments["target_episode"] = arguments.pop("episode", None)
        arguments = episode_audit.normalize_episode_audit_arguments(arguments)
        if arguments.get("season") is None or arguments.get("target_episode") is None:
            raise ValueError("复核目标必须指定 season 和 episode")
        key = (arguments["tmdb_id"] or arguments["query"], arguments["season"],
               arguments["as_of"], arguments.get("library_name", ""))
        grouped.setdefault(key, []).append((result_id, arguments))
    for result_id in seen:
        service.get_result(result_id, scope=scope)
    prepared: dict[str, list[dict]] = {}
    for group in grouped.values():
        arguments = dict(group[0][1])
        if len({item[1]["target_episode"] for item in group}) > 1:
            arguments.pop("target_episode")
        audit = episode_audit.audit_series_episodes(arguments, refresh=True)
        for result_id, target in group:
            verification = episode_audit.missing_episode_verification(
                audit, season=target["season"], episode=target["target_episode"],
            )
            if verification is None:
                raise ValueError("服务端审计未确认指定季集缺失，不能登记自动复核")
            bucket = prepared.setdefault(result_id, [])
            if verification not in bucket:
                bucket.append(verification)
    return prepared


def enqueue_download_verification(
    receipt: dict, verification: dict | list[dict] | None, *, owner: str,
) -> dict:
    """为真实单项受理回执登记已冻结目标；登记失败不改写下载受理事实。

    verification 来自服务端预检/确认元数据，不能直接传入客户端自报的证据。
    一次请求可以核验多个季集；相同目标幂等，异 owner 不覆盖。
    """
    request_id = receipt.get("request_id") if isinstance(receipt, dict) else None
    result = {"status": "not_eligible", "request_id": request_id}
    proofs = []
    for item in (verification if isinstance(verification, list) else [verification]):
        proof = normalize_episode_verification(item)
        if proof is None:
            return result
        proofs.append(proof)
    if (type(request_id) is not int or not 0 < request_id <= 2_147_483_647
            or receipt.get("target") not in DOWNLOAD_TARGETS
            or receipt.get("status") not in {"submitted", "partial", "failed"}
            or not set(receipt.get("succeeded") or []) & {"qb", "guangya"}
            or not proofs
            or not isinstance(owner, str) or len(owner) > 512):
        return result
    # 复用现有 owner 路由语法，不将普通 owner 回退到全局 Telegram chat。
    from app.agent.owner_routes import parse_telegram_owner_route

    route = parse_telegram_owner_route(owner)
    try:
        request = db.get_download_request(request_id)
        if request is None or request["targets"] != receipt["target"]:
            return result
        status = db.enqueue_agent_download_verification(
            request_id, targets=proofs, owner=owner, chat_id=route.chat_id if route else "",
        )
        return {**result, "status": status}
    except Exception as exc:
        logger.warning("下载已受理，自动复核登记失败 type=%s", type(exc).__name__)
        return {**result, "status": "error", "error": "自动复核登记失败；下载受理结果不变"}


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
    qb_save_path: str
    qb_category: str


class _RequestOptions(TypedDict, total=False):
    admission_id: int
    verification_targets: list[dict]
    verification_owner: str


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


def download_target_readiness(target: str) -> dict[str, bool]:
    readiness = {}
    if target in {"qb", "both"}:
        readiness["qb"] = bool((config.get("QB_URL", "") or "").strip())
    if target in {"guangya", "both"}:
        client = None
        try:
            client = GuangYaClient()
            readiness["guangya"] = client.logged_in
        except Exception:
            readiness["guangya"] = False
        finally:
            close_guangya_client(client)
    return readiness


def preflight_indexer_download(service, result_id: str, target: str, *, scope: str = "manual") -> dict:
    """采集唯一下载事实；不解析远端资源、不提交，也不包含凭据。"""
    if target not in DOWNLOAD_TARGETS:
        raise IndexerValidationError(public_message="下载目标无效")
    item = service.get_result(result_id, scope=scope)
    if item.site_id == "btbtla":
        try:
            detail_host = (urlsplit(item.detail_url or "").hostname or "").lower().rstrip(".")
        except ValueError:
            detail_host = ""
        if detail_host in _SPLIT_SITE_HOSTS:
            raise IndexerResultExpired(public_message="资源站点已拆分，旧资源确认已过期，请重新搜索后确认。")
    if item.download_state not in {"ready", "resolvable"} or not item.download_kinds:
        raise IndexerValidationError(public_message="该资源当前不可下载")
    destination = indexer_download_destination(item, target)
    if target in {"qb", "both"}:
        destination.update(
            qb_save_path=config.get("TG_QB_SAVE_PATH", config.get("RSS_QB_SAVE_PATH", "")) or "",
            qb_category=config.get("TG_QB_CATEGORY", config.get("RSS_QB_CATEGORY", "")) or "",
        )
    routing = {}
    if target in {"guangya", "both"} and not destination.get("gy_target_dir"):
        from app.modules.offline import OfflineRules

        rules = OfflineRules.from_config()
        routing = {name: getattr(rules, name) for name in (
            "target_dir_id", "target_dir_name", "secondary_enabled", "secondary_dir_id", "secondary_dir_name",
        )}
        routing["secondary_keywords"] = list(rules.secondary_keywords)
    return {
        "result_id": result_id, "target": target,
        "site_id": str(item.site_id), "site_name": str(item.site_name), "title": str(item.title),
        "download_state": str(item.download_state), "download_kinds": sorted(item.download_kinds),
        "backends": download_target_readiness(target), "destination": destination, "routing": routing,
    }


def preflight_indexer_download_batch(service, result_ids: list[str], target: str, *, scope: str = "manual") -> dict:
    """按输入顺序冻结整批事实；确认前不解析远端资源、不创建下载请求。"""
    if not result_ids:
        raise IndexerValidationError(public_message="请选择至少一个资源")
    return {"target": target, "resources": [
        preflight_indexer_download(service, result_id, target, scope=scope) for result_id in result_ids
    ]}


def _download_text(value: Any, maximum: int) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _receiving_folders(snapshot: dict) -> list[str]:
    """仅展示已采集的业务目录，不再次读取配置或另算指纹。"""
    destination, routing = snapshot["destination"], snapshot["routing"]
    folders = []
    if snapshot["target"] in {"qb", "both"}:
        name = destination["qb_save_path"].replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        folders.append("qBittorrent：" + (_download_text(name, 100) or "使用下载器默认保存目录"))
    if snapshot["target"] in {"guangya", "both"}:
        name = destination.get("gy_target_name") or routing.get("target_dir_name")
        text = "光鸭：" + (_download_text(name, 100) or "默认接收目录") + " / 每任务隔离目录"
        if routing.get("secondary_enabled") and routing.get("secondary_dir_id") not in {"", "0"}:
            text += "；匹配二次分流规则时改用「" + (_download_text(routing["secondary_dir_name"], 100) or "二次分流目录") + "」"
        folders.append(text)
    return folders


def preview_indexer_download_batch(service, result_ids: list[str], target: str, *, scope: str = "manual") -> tuple[dict, dict]:
    """公开预览与冻结事实共用一次采集；不解析资源、不创建下载请求。"""
    if not config.get_bool("INDEXER_SEARCH_ENABLED"):
        return {"ok": False, "status": "disabled", "summary": "资源检索功能已关闭", "data": {}, "error": "资源检索功能未启用。"}, {}
    try:
        snapshot = preflight_indexer_download_batch(service, result_ids, target, scope=scope)
        snapshots = snapshot["resources"]
    except HttpError as exc:
        return {"ok": False, "status": exc.code, "summary": exc.public_message, "data": {}, "error": exc.public_message}, {}
    readiness = snapshots[0]["backends"]
    unavailable = [name for name, ready in readiness.items() if not ready]
    if unavailable:
        labels = {"qb": "qBittorrent", "guangya": "光鸭"}
        return {"ok": False, "status": "not_configured", "summary": f"{'、'.join(labels[name] for name in unavailable)} 尚未配置或登录",
                "data": {"target": target, "backends": readiness}, "error": "所选下载目标尚未就绪。"}, {}
    resources = [{
        "result_id": item["result_id"], "site_id": _download_text(item["site_id"], 32),
        "site_name": _download_text(item["site_name"], 80), "title": _download_text(item["title"], 300),
        "download_state": item["download_state"],
        "download_kinds": [kind for kind in item["download_kinds"] if kind in {"magnet", "torrent"}],
        "receiving_folder": _download_text(item["destination"].get("gy_target_name", ""), 100),
    } for item in snapshots]
    data = {
        "count": len(resources), "resources": resources,
        "target": target, "backends": readiness,
        "receiving_folders": list(dict.fromkeys(folder for item in snapshots for folder in _receiving_folders(item))),
        "effects": ["服务端解析短期资源结果", "每项独立创建幂等下载请求", "向所选下载后端提交任务；部分失败不回滚已成功项"],
    }
    return {"ok": True, "status": "confirmation_required", "summary": f"确认后将提交 {len(resources)} 个资源到所选下载目标",
            "data": data, "error": ""}, snapshot


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


def _existing_download_request(item: DownloadInput, keys: tuple[str, ...]):
    """分组与提交共享仓库的内容身份/HTTP来源别名匹配规则。"""
    existing = db.get_download_request_by_request_key(keys[0])
    if existing is None and len(keys) > 1:
        existing = db.get_download_request_by_request_keys(
            keys, source_alias_key=request_source_alias_key(item),
        )
    return existing


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
    verification_targets: list[dict] | None = None,
    verification_owner: str = "",
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
    existing = _existing_download_request(item, keys)
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
        missing = (_missing_terminal_targets(existing, target)
                   if existing_status in {"completed", "failed", "cancelled"} else ())
        if verification_targets and not missing:
            # pending 恢复、补后端以及纯去重都绑定原请求；绑定先于任何远端提交。
            # 同源请求可能属于其他会话，仓库负责保留原 owner，不能改通知归属。
            registration = db.enqueue_agent_download_verification(
                existing_id, targets=verification_targets, owner=verification_owner, chat_id=chat_id,
            )
            if registration not in {"queued", "existing"} and (
                existing_status == "pending" or
                (existing_status not in INDEXER_RESUBMITTABLE_STATUSES and _missing_terminal_targets(existing, target))
            ):
                raise IndexerValidationError(public_message="复核目标未能登记，未提交下载")
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
        request_options: _RequestOptions = {"admission_id": admission_id} if admission_id is not None else {}
        if verification_targets:
            request_options.update(verification_targets=verification_targets, verification_owner=verification_owner)
        created = create_request(
            item,
            chat_id,
            message_id,
            origin=origin,
            user_id=user_id,
            **request_options,
        )
        request_id = int(created.get("id") or 0)
        if not request_id:
            raise DownloadRequestCreationError()
        if verification_targets and not created["created"] and created["status"] == "pending":
            # 创建竞争被另一个 pending 请求接管时，也须先确认本次意图已绑定。
            registration = db.enqueue_agent_download_verification(
                request_id, targets=verification_targets, owner=verification_owner, chat_id=chat_id,
            )
            if registration not in {"queued", "existing"}:
                raise IndexerValidationError(public_message="复核目标未能登记，未提交下载")
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
    verification_targets: list[dict] | None = None,
    verification_owner: str = "",
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
        verification_targets=verification_targets,
        verification_owner=verification_owner,
    )
    return {
        "request_id": request_id,
        "created": bool(created.get("created")),
        "target": normalized_target,
        "summary": public_dispatch_summary(dispatch),
        "dispatch": dispatch,
    }


def preview_download_url(url: str, target: str) -> tuple[dict, dict]:
    """直接链接的只读预检；共享协议路由、身份与后端就绪事实，不创建请求。"""
    if not isinstance(url, str) or not url.strip() or len(url) > 8192:
        raise ValueError("请提供有效的下载链接（不超过8192字符）")
    if target not in DOWNLOAD_TARGETS:
        raise ValueError("下载目标无效")
    routed = route_download_url(url)
    if routed == "guangya_share":
        raise ValueError("光鸭分享链接需要先解析分享内容，再执行转存")
    if routed == "web":
        raise ValueError("这是普通网页链接，不会创建离线下载任务")
    item = normalize_download_url(url)
    readiness = download_target_readiness(target)
    unavailable = [name for name, ready in readiness.items() if not ready]
    if unavailable:
        labels = {"qb": "qBittorrent", "guangya": "光鸭"}
        raise ValueError("、".join(labels[name] for name in unavailable) + " 尚未配置或登录")
    from app.repositories.download_requests import safe_download_text

    preview = {
        "source_type": "direct_url", "kind": item.kind,
        "title": safe_download_text(item.title), "target": target, "backends": readiness,
        "effects": ["创建幂等下载请求", "提交到所选下载后端", "保留后续整理与 STRM 跟踪"],
    }
    return preview, {"identity": request_key(item), "target": target, "backends": readiness}


def submit_download_url(url: str, target: str, *, origin: str,
                        expected_snapshot: dict | None = None) -> dict:
    """直接链接只准入首次提交；已有活跃请求不会隐式补投失败后端。"""
    preview, snapshot = preview_download_url(url, target)
    if expected_snapshot is not None and snapshot != expected_snapshot:
        raise DownloadSnapshotChanged()
    created = create_request(normalize_download_url(url), "", "", origin=origin)
    request_id = int(created["id"])
    summary = public_dispatch_summary(dispatch_request(request_id, target))
    message = {
        "submitted": "已提交", "partial": "仅部分目标已提交", "duplicate": "未完成提交",
        "manual_review": "受理结果待核对",
    }.get(summary["status"], "未完成提交")
    return {**summary, "request_id": request_id, "created": bool(created["created"]),
            "target": target, "kind": preview["kind"], "source_type": "direct_url",
            "summary": f"下载请求 #{request_id} {message}"}


@dataclass(frozen=True)
class _ResolvedIndexerDownload:
    item: DownloadInput
    snapshot: dict
    destination: dict


async def _resolve_indexer_download(service, result_id: str, target: str, *, scope: str,
                                    expected_snapshot: dict | None) -> _ResolvedIndexerDownload:
    """只读解析成规范下载输入并复验冻结事实；不创建请求或提交后端。"""
    stored, resolved = await _resolve(service, result_id, scope=scope)
    item = await _resolved_download_input(service, stored, resolved)
    # 解析可能等待网络；在远端写入之前复验一次，随后只使用这份目录事实。
    snapshot = preflight_indexer_download(service, result_id, target, scope=scope)
    if expected_snapshot is not None and snapshot != expected_snapshot:
        raise DownloadSnapshotChanged()
    destination = dict(snapshot["destination"])
    if snapshot["routing"]:
        from app.modules.offline import OfflineRules, analyze_offline_url

        rules = replace(OfflineRules.from_config(), **snapshot["routing"])
        decision = analyze_offline_url(item.source_value, item.title, rules, content_type=item.content_type)
        destination.update(gy_target_dir=decision.target_dir_id, gy_target_name=decision.target_dir_name)
    return _ResolvedIndexerDownload(item, snapshot, destination)


async def _submit_indexer_download(service, resolved: _ResolvedIndexerDownload, *,
                                   scope: str, origin_namespace: str, **options) -> dict[str, Any]:
    snapshot = resolved.snapshot
    target = snapshot["target"]
    if preflight_indexer_download(service, snapshot["result_id"], target, scope=scope) != snapshot:
        raise DownloadSnapshotChanged()
    submitted = await asyncio.to_thread(
        submit_download_input, resolved.item, target,
        origin=f"{origin_namespace}:{snapshot['site_id']}", **resolved.destination, **options,
    )
    request_id = int(submitted["request_id"])
    public = submitted["summary"]
    duplicate = public["duplicate"]
    succeeded = public["succeeded"]
    failed = public["failed"]
    status = public["status"]
    error = public["error"]
    response = {
        "result_id": snapshot["result_id"],
        "ok": status in {"submitted", "partial"},
        "request_id": request_id,
        "created": bool(submitted["created"]),
        "target": target,
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


def _attach_download_verification(items: list[dict], owner: str) -> None:
    """在整批提交收束后，每个确切请求只读取一次公开复核状态。"""
    statuses: dict[int, dict] = {}
    for item in items:
        request_id = item.get("request_id")
        if not request_id:
            continue
        if request_id not in statuses:
            try:
                job = db.get_agent_download_verification(request_id)
                if job is not None and job["owner"] == owner:
                    status = {"status": job["status"], "target_count": len(job["targets"])}
                else:
                    status = {"status": "conflict" if job else "not_registered", "target_count": 0}
            except Exception:
                # 展示读取失败不能改写已受理回执；意图已在请求提交前保存。
                status = {"status": "unavailable", "target_count": 0}
            statuses[request_id] = status
        item["library_verification"] = dict(statuses[request_id])


def _indexer_download_error(result_id: str, target: str, exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, HttpError):
        error = exc.public_message
    elif isinstance(exc, InvalidDownloadData):
        error = "资源下载数据无效"
    elif isinstance(exc, DownloadRequestCreationError):
        error = "下载请求创建失败"
    else:
        error = "下载处理失败"
    result = _failed_download_result(result_id, target, error)
    if isinstance(exc, DownloadSnapshotChanged):
        result["pre_submission_failure"] = True
    return result


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
    expected_snapshot: dict | None = None,
    verification_targets: list[dict] | None = None,
    verification_owner: str = "",
    _public_errors: bool = False,
) -> dict[str, Any]:
    """单项入口与整批下载共用只读解析、冻结复验和真实提交。"""
    normalized_target = (target or "").strip().lower()
    if normalized_target not in DOWNLOAD_TARGETS:
        return _failed_download_result(result_id, normalized_target, "下载目标无效")
    namespace = (origin_namespace or "").strip().lower()
    if namespace not in {"indexer", "agent"}:
        raise IndexerValidationError("invalid download origin namespace")
    async with _download_limiter():
        try:
            resolved = await _resolve_indexer_download(service, result_id, normalized_target,
                scope=scope, expected_snapshot=expected_snapshot)
        except (HttpError, InvalidDownloadData) as exc:
            if not _public_errors:
                raise
            return {**_indexer_download_error(result_id, normalized_target, exc), "pre_submission_failure": True}
        response = await _submit_indexer_download(service, resolved, scope=scope, origin_namespace=namespace,
            chat_id=chat_id, user_id=user_id, message_id=message_id,
            **({"admission_id": admission_id} if admission_id is not None else {}),
            verification_targets=verification_targets, verification_owner=verification_owner)
    if verification_targets:
        _attach_download_verification([response], verification_owner)
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
    expected_snapshot: dict | None = None,
    verification_targets: list[dict] | None = None,
    verification_owner: str = "",
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
            expected_snapshot=expected_snapshot,
            verification_targets=verification_targets,
            verification_owner=verification_owner,
            _public_errors=True,
            **({"admission_id": admission_id} if admission_id is not None else {}),
        )
    except Exception as exc:
        return _indexer_download_error(result_id, target, exc)


def _converge_batch_duplicates(items: list[dict[str, Any]]) -> int:
    """仅关联同批、同请求、同目标的唯一实际提交结果，不查询全局任务。

    duplicate/ok/created 仍描述本候选未再次提交；batch_result 描述关联的
    提交结果（submitted 并非下载完成），existing_status 不再保留瞬时快照。
    不从结果推断新的重试授权；有多个实际提交结果时不猜测其先后顺序。
    """
    outcomes = {
        "submitted": "已受理（不代表下载完成）",
        "partial": "部分下载目标成功，其余失败",
        "manual_review": "提交结果待核对，勿直接重复提交",
        "failed": "提交失败",
    }
    completed: dict[tuple[int | None, str | None], list[dict[str, Any]]] = {}
    for item in items:
        request_id = item.get("request_id")
        if (
            isinstance(request_id, int)
            and not isinstance(request_id, bool)
            and request_id > 0
            and item.get("target") in DOWNLOAD_TARGETS
            and not item.get("duplicate")
            and item.get("status") in outcomes
        ):
            completed.setdefault((request_id, item["target"]), []).append(item)

    merged = 0
    for item in items:
        if not item.get("duplicate"):
            continue
        matches = completed.get((item.get("request_id"), item.get("target")), [])
        if len(matches) != 1:
            continue
        final = matches[0]
        item["batch_result"] = {
            key: final.get(key)
            for key in ("result_id", "status", "succeeded", "failed", "error")
        }
        item["existing_status"] = final["status"]
        item["can_resubmit"] = False
        item["resubmit_target"] = ""
        item["error"] = f"已合并到同批下载请求，未新增任务；{outcomes[final['status']]}"
        merged += 1
    return merged


async def submit_indexer_download_batch(
    service, result_ids: list[str], target: str, *, expected_snapshot: dict,
    origin_namespace: str = "indexer", scope: str = "manual",
    verification_targets: dict[str, list[dict]] | None = None,
    verification_owner: str = "",
) -> dict[str, Any]:
    """复验整批冻结事实后逐项提交；复用下载限流与幂等，不回滚或重投。

    返回有序逐项回执和批量计数；accepted 仅代表受理，不代表下载完成。
    取消继续向调用方传播，已经开始的后端提交仍以原下载请求回执为准。
    """
    snapshot = preflight_indexer_download_batch(service, result_ids, target, scope=scope)
    if snapshot != expected_snapshot:
        raise DownloadSnapshotChanged(public_message="资源集合或下载配置已变化，请重新预检")
    if verification_targets and (set(verification_targets) - set(result_ids) or not verification_owner):
        raise IndexerValidationError("复核目标或归属不属于本批下载")
    from app.agent.owner_routes import parse_telegram_owner_route

    route = parse_telegram_owner_route(verification_owner) if verification_targets else None
    namespace = (origin_namespace or "").strip().lower()
    if namespace not in {"indexer", "agent"}:
        raise IndexerValidationError("invalid download origin namespace")

    async def resolve(item):
        async with _download_limiter():
            return await _resolve_indexer_download(service, item["result_id"], target,
                scope=scope, expected_snapshot=item)

    resolved = await asyncio.gather(*(resolve(item) for item in snapshot["resources"]), return_exceptions=True)
    groups: list[tuple[set[str], list[int]]] = []
    for index, value in enumerate(resolved):
        if isinstance(value, BaseException):
            if not isinstance(value, Exception):
                raise value
            continue
        if not verification_targets:
            continue
        canonical_keys = request_keys(value.item)
        keys = set(canonical_keys)
        # URL 只是来源别名，不能把内容已经改变的 HTTP 资源合并为同一个包。
        if len(keys) > 1:
            keys.discard(request_source_alias_key(value.item))
        existing = _existing_download_request(value.item, canonical_keys)
        if existing is not None:
            keys.add(f"request:{existing['id']}")
        members = [index]
        for group in list(groups):
            if keys & group[0]:
                keys.update(group[0])
                members.extend(group[1])
                groups.remove(group)
        groups.append((keys, members))
    goals: dict[int, list[dict]] = {}
    for _keys, members in groups:
        targets: list[dict] = []
        for index in sorted(members):
            for proof in (verification_targets or {}).get(snapshot["resources"][index]["result_id"], []):
                if proof not in targets:
                    targets.append(proof)
        if targets:
            goals.update((index, targets) for index in members)

    async def submit(index, value):
        result_id = snapshot["resources"][index]["result_id"]
        if isinstance(value, BaseException):
            response = _indexer_download_error(result_id, target, value)
            if isinstance(value, (HttpError, InvalidDownloadData)):
                response["pre_submission_failure"] = True
            return response
        async with _download_limiter():
            try:
                return await _submit_indexer_download(service, value, scope=scope, origin_namespace=namespace,
                    verification_targets=goals.get(index), verification_owner=verification_owner,
                    chat_id=route.chat_id if route else "")
            except Exception as exc:
                return _indexer_download_error(result_id, target, exc)

    items = list(await asyncio.gather(*(submit(index, value) for index, value in enumerate(resolved))))
    merged = _converge_batch_duplicates(items)
    _attach_download_verification([item for index, item in enumerate(items) if index in goals], verification_owner)
    counts = Counter(item.get("status") for item in items)
    accepted = counts["submitted"] + counts["partial"]
    review_required = counts["manual_review"]
    unaccepted = len(items) - merged - accepted
    if (
        accepted
        and not counts["failed"]
        and counts["duplicate"] == merged
        and not counts["partial"]
        and not review_required
    ):
        status, summary = "accepted", f"{accepted} 个下载任务已提交"
    elif accepted:
        status, summary = (
            "partial",
            f"批量提交完成：{accepted} 个已受理，{unaccepted} 个未受理",
        )
    elif review_required:
        status, summary = (
            "review_required",
            f"{review_required} 个下载任务提交结果待核对",
        )
    elif counts["duplicate"] and not counts["failed"]:
        status, summary = "conflict", "所选资源均已提交或正在处理中"
    else:
        status, summary = "unavailable", "批量资源提交未成功"
    if counts["partial"]:
        summary += f"；{counts['partial']} 个任务仅部分下载目标成功"
    if merged:
        summary += f"；{merged} 个同批重复候选已合并（未新增任务）"
    return {
        "ok": bool(accepted), "status": status, "summary": summary,
        "target": target, "total": len(items), "succeeded": accepted,
        "review_required": review_required, "failed": counts["failed"],
        "duplicate": counts["duplicate"], "merged": merged, "items": items,
        "error": (
            "部分下载任务提交结果待核对，请先核对下载器，勿直接重复提交。" if review_required else
            "部分下载目标提交失败。" if counts["partial"] else
            "部分或全部资源未被下载后端接受。" if unaccepted else ""
        ),
    }
