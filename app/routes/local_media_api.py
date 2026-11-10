"""本地媒体来源、检查、预览与任务 API。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse

from app import database as db
from app.repositories import local_media as local_media_repo
from app.repositories import organize_history as organize_history_repo
from app.clients.base import close_media_server_client
from app.clients.qbittorrent import close_qbittorrent_client
from app.logger import get_logger
from app.modules.local_directory_browser import VIRTUAL_ROOT, browse_local_directories
from app.modules.local_media_candidates import (
    candidate_payload,
    discover_local_media_directory_candidates,
)
from app.repositories.local_media import can_clear_local_media_task
from app.modules.local_media_notifications import notify_local_media_task
from app.modules.local_media_recognition_summary import (
    merge_recognition_summaries,
    parse_recognition_summary,
    summary_from_task,
)
from app.modules.local_media_scheduler import get_local_media_scheduler
from app.modules.local_media_service import (
    LocalMediaServiceError,
    get_local_media_service,
    payload_boolean,
    payload_text,
)
from app.modules.local_path_mapping import (
    LEGACY_SOURCE_PATH_ERROR,
    PathMappingError,
    assert_within,
)
from app.modules.local_storage import LocalStorageError
from app.modules.media_server_profiles import list_configured_profiles
from app.web import api_error, require_api_login

logger = get_logger(__name__)
router = APIRouter(prefix="/api/local-media")
_OWNER = "admin"


def _integer_value(value: Any, error: str) -> int:
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError(error)
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(error) from exc


def _integer(payload: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    number = _integer_value(payload.get(key, default), f"{key} 必须是整数")
    if not minimum <= number <= maximum:
        raise ValueError(f"{key} 超出允许范围")
    return number


def _optional_integer(
    payload: dict, key: str, *, minimum: int, maximum: int, label: str,
) -> int | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    number = _integer_value(value, f"{label}必须是整数")
    if isinstance(value, str) and str(number) != value.strip():
        raise ValueError(f"{label}必须是整数")
    if not minimum <= number <= maximum:
        raise ValueError(f"{label}必须是 {minimum}-{maximum} 的整数")
    return number


def _candidate_payloads(source, candidates, *, organize_ready: bool, error: str, allow_nested: bool = False):
    items: list[dict] = []
    for candidate in candidates:
        try:
            items.append(candidate_payload(
                source, candidate, organize_ready=organize_ready, allow_nested=allow_nested,
            ))
        except (OSError, PathMappingError):
            error = error or "目录扫描不完整：部分条目在读取期间发生变化"
    return items, error


def _task_display_name(task) -> str:
    content_name = Path(task.content_path).name
    error = str(getattr(task, "error", "") or "")
    missing_episode_marker = "剧集文件缺少集数，不能自动归档:"
    if str(getattr(task, "status", "") or "") == "requires_manual" and missing_episode_marker in error:
        failed_video = error.partition(missing_episode_marker)[2].strip()
        if failed_video:
            return Path(failed_video).name
    return str(getattr(task, "title", "") or "").strip() or content_name


def _task_payload(task, source_name: str, active_confirmations: set[str]) -> dict:
    return {
        "id": task.id, "source_id": task.source_id,
        "source_name": source_name,
        "content_name": Path(task.content_path).name,
        "display_name": _task_display_name(task),
        "title": str(getattr(task, "title", "") or ""),
        "year": str(getattr(task, "year", "") or ""),
        "trigger": task.trigger, "status": task.status, "attempts": task.attempts,
        "tmdb_id": getattr(task, "tmdb_id", ""), "media_type": getattr(task, "media_type", ""),
        "season": getattr(task, "season_override", None),
        "episode": getattr(task, "episode_override", None),
        "numbering_mode": getattr(task, "numbering_mode", "auto") or "auto",
        "clearable": can_clear_local_media_task(task.id, task.status, active_confirmations),
        "error": task.error, "warning": task.warning,
        "confirmation_actor": getattr(task, "confirmation_actor", "") or "",
        "created_at": task.created_at, "updated_at": task.updated_at,
        "completed_at": task.completed_at,
    }


def _task_detail_payload(task) -> dict:
    with db.get_conn() as conn:
        _, active_confirmations, _ = organize_history_repo.active_organize_confirmation_refs(conn)
    source = local_media_repo.get_local_media_source(task.source_id, owner=_OWNER)
    payload = _task_payload(task, source.name if source else "已删除来源", active_confirmations)
    payload.update({
        "content_path": task.content_path,
        "qb_hash": task.qb_hash,
        "snapshot_digest": task.snapshot_digest,
        "rules_snapshot": task.rules_snapshot,
        "title": task.title,
        "year": task.year,
        "version": task.version,
        "stable_since": task.stable_since,
    })
    return payload


def _task_recognition_payload(task, item_rows) -> dict:
    persisted = parse_recognition_summary(
        getattr(task, "recognition_summary", "")
    )
    if persisted:
        return persisted
    selected = summary_from_task(task)
    inferred: dict = {}
    if item_rows and str(getattr(task, "status", "")) == "completed":
        try:
            inferred = get_local_media_service().infer_recognition_summary(
                item_rows
            )
        except Exception as exc:
            logger.debug(
                "本地整理历史识别摘要推断失败 task_id=%s type=%s",
                getattr(task, "id", ""), type(exc).__name__,
            )
    return merge_recognition_summaries(selected, inferred)


def _task_item_payload(row) -> dict:
    return {
        key: row[key]
        for key in (
            "id", "source_path", "target_path", "role", "media_group", "action",
            "size", "status", "error", "created_at", "updated_at",
        )
    }


def _task_step_payload(row) -> dict:
    return {
        key: row[key]
        for key in (
            "id", "step_index", "action", "source_path", "target_path", "status",
            "error", "started_at", "finished_at",
        )
    }


def _safe_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, (
        ValueError, LookupError, LocalMediaServiceError, LocalStorageError,
        FileNotFoundError, PathMappingError,
    )):
        return api_error(str(exc), 400)
    if isinstance(exc, OSError):
        logger.warning("本地媒体文件系统异常 type=%s", type(exc).__name__)
        return api_error("本地目录访问失败，请检查 Docker 卷挂载与容器目录权限", 400)
    logger.error("本地媒体 API 失败 type=%s", type(exc).__name__)
    return api_error("本地媒体请求失败", 500)


@router.get("/directories")
def list_directories(
    request: Request,
    path: str = VIRTUAL_ROOT,
    source_id: int = 0,
):
    require_api_login(request)
    try:
        if (request.query_params.get("network_root") or "").strip():
            return api_error(LEGACY_SOURCE_PATH_ERROR, 400)
        allowed_root = None
        if source_id:
            source = local_media_repo.get_local_media_source(source_id, owner=_OWNER)
            if source is None:
                return api_error("本地媒体来源不存在", 404)
            allowed_root = Path(source.local_root)
            if path == VIRTUAL_ROOT:
                path = source.local_root
        return browse_local_directories(path, allowed_root=allowed_root)
    except Exception as exc:
        return _safe_error(exc)


@router.get("/media-servers")
def list_media_servers(request: Request):
    require_api_login(request)
    servers = [
        {"provider": item.server_type, "label": item.label}
        for item in list_configured_profiles() if item.enabled and item.configured
    ]
    return {"servers": servers}


@router.get("/media-servers/{provider}/libraries")
def list_media_server_libraries(provider: str, request: Request):
    require_api_login(request)
    try:
        normalized = (provider or "").strip().lower()
        if normalized not in {"jellyfin", "emby"}:
            raise ValueError("媒体服务器类型无效")
        profiles = {item.server_type: item for item in list_configured_profiles()}
        profile = profiles.get(normalized)
        if profile is None:
            raise ValueError("媒体服务器类型无效")
        if not profile.enabled or not profile.configured:
            raise ValueError(f"{profile.label} 未启用或未配置")
        if normalized == "jellyfin":
            from app.clients.jellyfin import JellyfinClient
            client = JellyfinClient(profile.url, profile.credential)
        else:
            from app.clients.emby import EmbyClient
            client = EmbyClient(profile.url, profile.credential)
        try:
            return {"provider": normalized, "libraries": client.list_virtual_folders()}
        finally:
            close_media_server_client(client)
    except Exception as exc:
        return _safe_error(exc)


@router.get("/sources")
def list_sources(request: Request):
    require_api_login(request)
    from app.modules.local_media_service import local_sources
    return {"sources": local_sources()}


@router.post("/sources")
def create_source(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    from app.modules.local_media_service import save_local_source_config
    try:
        return save_local_source_config(data or {})["source"]
    except Exception as exc:
        return _safe_error(exc)


@router.put("/sources/{source_id}")
def update_source(source_id: int, request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    from app.modules.local_media_service import save_local_source_config
    if local_media_repo.get_local_media_source(source_id, owner=_OWNER) is None:
        return api_error("本地媒体来源不存在", 404)
    try:
        return save_local_source_config(data or {}, source_id)["source"]
    except Exception as exc:
        return _safe_error(exc)


@router.delete("/sources/{source_id}")
def delete_source(source_id: int, request: Request):
    require_api_login(request)
    from app.modules.local_media_service import delete_local_source_config
    try:
        if local_media_repo.get_local_media_source(source_id, owner=_OWNER) is None:
            return api_error("本地媒体来源不存在", 404)
        return {"deleted": delete_local_source_config(source_id)["deleted"]}
    except Exception as exc:
        return _safe_error(exc)


@router.get("/items")
def list_media_items(request: Request, source_id: int = 0, path: str = ""):
    """列出来源根条目，或安全浏览指定来源内某个目录的直接媒体子项。"""
    require_api_login(request)
    try:
        if path and source_id <= 0:
            raise ValueError("浏览目录时必须指定媒体来源")
        if source_id > 0:
            source = local_media_repo.get_local_media_source(source_id, owner=_OWNER)
            if source is None:
                return api_error("本地媒体来源不存在", 404)
            selected_path = payload_text({"path": path}, "path", max_length=4096) or source.local_root
            candidates, error, current = discover_local_media_directory_candidates(
                source, selected_path,
            )
            if current is None:
                return api_error(error or "目录读取失败", 400)
            targets = local_media_repo.list_local_library_targets(source.id, owner=_OWNER)
            organize_ready = bool(targets) and source.mode != "preview_only"
            serialized, error = _candidate_payloads(
                source, candidates, organize_ready=organize_ready, error=error, allow_nested=True,
            )
            root_candidate = Path(source.local_root).expanduser().absolute()
            root = assert_within(root_candidate, root_candidate)
            relative = current.relative_to(root)
            breadcrumbs = [{"name": source.name, "path": str(root)}]
            cursor = root
            for part in relative.parts:
                cursor /= part
                breadcrumbs.append({"name": part, "path": str(cursor)})
            return {
                "items": serialized,
                "sources": [{
                    "id": source.id,
                    "name": source.name,
                    "count": len(serialized),
                    "error": error,
                }],
                "browse": {
                    "source_id": source.id,
                    "source_name": source.name,
                    "root_path": str(root),
                    "current_path": str(current),
                    "parent_path": "" if current == root else str(current.parent),
                    "breadcrumbs": breadcrumbs,
                },
            }

        items: list[dict] = []
        source_results: list[dict] = []
        bound_sources = {target.source_id for target in local_media_repo.list_local_library_targets(owner=_OWNER)}
        for source in local_media_repo.list_local_media_sources(owner=_OWNER):
            candidates, error, _ = discover_local_media_directory_candidates(source)
            serialized, error = _candidate_payloads(
                source, candidates,
                organize_ready=source.id in bound_sources and source.mode != "preview_only", error=error,
            )
            items.extend(serialized)
            source_results.append({
                "id": source.id, "name": source.name, "count": len(serialized), "error": error,
            })
        items.sort(key=lambda item: (item["source_name"].casefold(), item["name"].casefold()))
        return {"items": items, "sources": source_results, "browse": None}
    except Exception as exc:
        return _safe_error(exc)


@router.post("/items/delete")
def delete_media_item(request: Request, data: dict | None = Body(default=None)):
    """把一级媒体条目移动到来源内的 MediaFlux 回收区。"""
    require_api_login(request)
    payload = data or {}
    try:
        source_id = _integer(payload, "source_id", 0, 1, 2_147_483_647)
        source = local_media_repo.get_local_media_source(source_id, owner=_OWNER)
        if source is None:
            return api_error("本地媒体来源不存在", 404)
        identity = payload.get("identity")
        if not isinstance(identity, dict):
            raise ValueError("条目快照无效，请刷新后重试")
        destination = get_local_media_service().move_media_item_to_trash(
            _OWNER,
            source_id,
            payload_text(payload, "path", required=True),
            identity,
        )
        logger.info("本地媒体条目已移入回收区 source_id=%s name=%s", source_id, destination.name)
        return {"deleted": True, "recoverable": True}
    except Exception as exc:
        return _safe_error(exc)


@router.post("/scan")
def scan_existing_media(request: Request):
    """立即扫描全部本地下载来源，包含配置前已经存在的媒体。"""
    require_api_login(request)
    try:
        scheduler = get_local_media_scheduler()
        was_running = bool(scheduler.status().get("running"))
        result = scheduler.enqueue_manual_scan_candidates(silent=True)
        if result.get("task_ids") and not was_running:
            scheduler.start()
        return result
    except Exception as exc:
        return _safe_error(exc)


@router.get("/tasks")
def list_tasks(request: Request, status: str = "", before_id: int = Query(default=0, ge=0, le=2**63 - 1)):
    require_api_login(request)
    try:
        page = local_media_repo.list_local_media_tasks(owner=_OWNER, status=status, limit=501, before_id=before_id)
        tasks, has_more = page[:500], len(page) > 500
        sources = {source.id: source.name for source in local_media_repo.list_local_media_sources(owner=_OWNER)}
        with db.get_conn() as conn:
            _, active_confirmations, _ = organize_history_repo.active_organize_confirmation_refs(conn)
        return {
            "tasks": [_task_payload(item, sources.get(item.source_id, "已删除来源"), active_confirmations) for item in tasks],
            "has_more": has_more, "next": tasks[-1].id if has_more else None,
        }
    except Exception as exc:
        return _safe_error(exc)


@router.delete("/tasks")
def clear_tasks(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    payload = data or {}
    try:
        if payload_text(payload, "confirm", max_length=16) != "CLEAR":
            raise ValueError("清除本地整理日志需要确认")
        raw_ids = payload.get("ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError("请选择需要清除的本地整理日志")
        if len(raw_ids) > 500:
            raise ValueError("单次最多清除 500 条本地整理日志")
        task_ids = [_integer({"日志 ID": value}, "日志 ID", 0, 1, 2**63 - 1) for value in raw_ids]
        return local_media_repo.delete_local_media_tasks(task_ids, owner=_OWNER)
    except Exception as exc:
        return _safe_error(exc)


@router.get("/tasks/{task_id}")
def task_detail(task_id: int, request: Request):
    require_api_login(request)
    try:
        task = local_media_repo.get_local_media_task(task_id, owner=_OWNER)
        if task is None:
            return api_error("本地整理日志不存在", 404)
        item_rows = local_media_repo.list_local_media_task_items(task_id, owner=_OWNER)
        return {
            "task": _task_detail_payload(task),
            "recognition": _task_recognition_payload(task, item_rows),
            "items": [_task_item_payload(row) for row in item_rows],
            "steps": [
                _task_step_payload(row)
                for row in local_media_repo.list_local_media_operation_steps(task_id, owner=_OWNER)
            ],
        }
    except Exception as exc:
        return _safe_error(exc)


@router.post("/inspect")
def inspect_path(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    payload = data or {}
    try:
        source_id = _integer(payload, "source_id", 0, 1, 2_147_483_647)
        source = local_media_repo.get_local_media_source(source_id, owner=_OWNER)
        if source is None:
            return api_error("本地媒体来源不存在", 404)
        path = payload_text(payload, "path") or source.local_root
        return get_local_media_service().inspect_source(_OWNER, source_id, path)
    except Exception as exc:
        return _safe_error(exc)


@router.post("/search")
def search_media(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    payload = data or {}
    try:
        query = payload_text(payload, "query", required=True, max_length=256)
        media_type = payload_text(payload, "media_type", max_length=16) or "auto"
        if media_type not in {"auto", "movie", "tv"}:
            raise ValueError("媒体类型必须是 auto、movie 或 tv")
        inspection_id = payload_text(payload, "inspection_id", max_length=64)
        return {"candidates": get_local_media_service().search(
            query,
            payload_text(payload, "year", max_length=4),
            media_type,
            owner=_OWNER,
            inspection_id=inspection_id,
        )}
    except Exception as exc:
        return _safe_error(exc)


@router.post("/preview")
def preview_media(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    payload = data or {}
    try:
        result = get_local_media_service().preview(
            _OWNER, payload_text(payload, "inspection_id", required=True, max_length=64),
            payload_text(payload, "tmdb_id", max_length=32), payload_text(payload, "media_type", max_length=16),
            payload.get("overrides") if isinstance(payload.get("overrides"), dict) else None,
            season_override=_optional_integer(
                payload, "season", minimum=0, maximum=99, label="季数",
            ),
            episode_override=_optional_integer(
                payload, "episode", minimum=1, maximum=999, label="集数",
            ),
            numbering_mode=payload_text(payload, "numbering_mode", max_length=32) or "auto",
        )
        return {key: value for key, value in result.items() if not key.startswith("_")}
    except Exception as exc:
        return _safe_error(exc)


@router.post("/external-hints")
def external_hints(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    payload = data or {}
    try:
        return get_local_media_service().external_hints(
            _OWNER,
            payload_text(payload, "inspection_id", required=True, max_length=64),
            payload_text(payload, "query", required=True, max_length=256),
            payload_text(payload, "media_type", max_length=16) or "auto",
        )
    except Exception as exc:
        return _safe_error(exc)


@router.post("/execute")
def execute_media(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    payload = data or {}
    qb_client = None
    notification_task_id: int | None = None
    try:
        if not payload_text(payload, "preview_digest", max_length=144):
            raise LocalMediaServiceError("缺少预览确认，请重新检查并生成预览后确认")
        service = get_local_media_service()
        task_id = service.create_manual_task(
            _OWNER, payload_text(payload, "inspection_id", required=True, max_length=64),
            preview_digest=payload_text(payload, "preview_digest", max_length=144),
        )
        notification_task_id = task_id
        if not local_media_repo.claim_local_media_task(task_id, expected="waiting_stable", owner=_OWNER):
            raise LocalMediaServiceError("任务已被其他操作认领")
        task = local_media_repo.get_local_media_task(task_id, owner=_OWNER)
        if task is None:
            raise LocalMediaServiceError("本地媒体任务不存在")
        if task.qb_hash:
            # 人工重试仍保留qB归属，与调度/通知确认入口一样传入短生命周期客户端。
            try:
                qb_client = get_local_media_scheduler().qb_factory()
                if qb_client is None:
                    raise LocalMediaServiceError("qB 客户端不可用")
            except Exception as exc:
                message = "qB 客户端初始化失败，请检查下载器配置后重试；尚未移动源文件"
                local_media_repo.update_local_media_task(task_id, owner=_OWNER, status="failed", error=message)
                logger.warning("手动整理qB客户端初始化失败 task=%s type=%s", task_id, type(exc).__name__)
                raise LocalMediaServiceError(message) from exc
        result = service.execute_task(_OWNER, task_id, qb_client=qb_client)
        notify_local_media_task(task_id, result, owner=_OWNER)
        return result
    except Exception as exc:
        if notification_task_id is not None:
            notify_local_media_task(notification_task_id, owner=_OWNER, error=str(exc))
        return _safe_error(exc)
    finally:
        close_qbittorrent_client(qb_client)


@router.post("/tasks/{task_id}/inspect")
def inspect_task(task_id: int, request: Request):
    require_api_login(request)
    try:
        return get_local_media_service().inspect_task(_OWNER, task_id)
    except Exception as exc:
        return _safe_error(exc)


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: int, request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    payload = data or {}
    try:
        retry_fields: dict[str, Any] = {}
        if "tmdb_id" in payload:
            retry_fields["tmdb_id"] = payload_text(payload, "tmdb_id", max_length=32)
        if "media_type" in payload:
            retry_fields["media_type"] = payload_text(payload, "media_type", max_length=16)
        if "season" in payload:
            retry_fields["season_override"] = _optional_integer(
                payload, "season", minimum=0, maximum=99, label="季数",
            )
        if "episode" in payload:
            retry_fields["episode_override"] = _optional_integer(
                payload, "episode", minimum=1, maximum=999, label="集数",
            )
        if "numbering_mode" in payload:
            retry_fields["numbering_mode"] = (
                payload_text(payload, "numbering_mode", max_length=32) or "auto"
            )
        confirm_interrupted_write = payload_boolean(
            payload, "confirm_interrupted_write", False,
        )
        task = local_media_repo.get_local_media_task(task_id, owner=_OWNER)
        if (
            task is not None
            and db.is_interrupted_local_media_write_error(task.error)
            and not confirm_interrupted_write
        ):
            return api_error(
                "任务在本地写操作期间中断；请先人工核验文件及 qB 状态，"
                "确认后再显式重试",
                409,
            )
        retry_fields["confirm_interrupted_write"] = confirm_interrupted_write
        if not local_media_repo.reset_local_media_task(task_id, owner=_OWNER, **retry_fields):
            return api_error("任务不存在或当前状态不可重试", 409)
        get_local_media_scheduler().reload()
        return {"queued": True, "task_id": task_id}
    except Exception as exc:
        return _safe_error(exc)
