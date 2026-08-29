"""本地媒体来源、检查、预览与任务 API。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from app import database as db
from app.logger import get_logger
from app.modules.local_directory_browser import VIRTUAL_ROOT, browse_local_directories
from app.modules.local_path_mapping import (
    LEGACY_SOURCE_PATH_ERROR,
    PathMappingError,
    assert_within,
    is_windows_or_unc_path,
    validate_source_target_roots,
)
from app.modules.local_media_candidates import (
    candidate_payload,
    discover_local_media_candidates,
    discover_local_media_directory_candidates,
    move_candidate_to_trash,
)
from app.modules.local_media_models import LOCAL_BUSY_TASK_STATUSES, LOCAL_MEDIA_CATEGORIES
from app.modules.media_server_path_mapping import MediaServerPathMapping
from app.modules.media_server_profiles import list_configured_profiles
from app.modules.local_media_notifications import notify_local_media_task
from app.modules.local_media_recognition_summary import (
    infer_recognition_summary,
    merge_recognition_summaries,
    parse_recognition_summary,
    summary_from_task,
)
from app.modules.local_media_scheduler import get_local_media_scheduler
from app.modules.local_media_service import LocalMediaServiceError, get_local_media_service
from app.web import api_error, require_api_login

logger = get_logger(__name__)
router = APIRouter(prefix="/api/local-media")
_OWNER = "admin"


def _text(payload: dict, key: str, *, required: bool = False, max_length: int = 2048) -> str:
    value = payload.get(key, "")
    if value is None:
        value = ""
    if isinstance(value, (dict, list, tuple, bool)):
        raise ValueError(f"{key} 必须是字符串")
    text = str(value).strip()
    if "\x00" in text:
        raise ValueError(f"{key} 包含非法字符")
    if required and not text:
        raise ValueError(f"{key} 不能为空")
    if len(text) > max_length:
        raise ValueError(f"{key} 过长")
    return text


def _integer(payload: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{key} 必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 必须是整数") from exc
    if not minimum <= number <= maximum:
        raise ValueError(f"{key} 超出允许范围")
    return number


def _boolean(payload: dict, key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} 必须是布尔值")
    return value


def _optional_integer(
    payload: dict, key: str, *, minimum: int, maximum: int, label: str,
) -> int | None:
    if key not in payload or payload.get(key) in (None, ""):
        return None
    value = payload.get(key)
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是整数") from exc
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{label}必须是整数")
    if isinstance(value, str) and str(number) != value.strip():
        raise ValueError(f"{label}必须是整数")
    if not minimum <= number <= maximum:
        raise ValueError(f"{label}必须是 {minimum}-{maximum} 的整数")
    return number


def _source_payload(source) -> dict:
    targets = db.list_local_library_targets(source.id, owner=_OWNER)
    return {
        "id": source.id, "name": source.name, "qb_profile": source.qb_profile,
        "qb_path_prefix": source.qb_path_prefix, "local_root": source.local_root,
        "enabled": source.enabled,
        # 旧字段保留在响应中兼容历史客户端，但产品链路已取消固定等待与定时扫描。
        "stable_seconds": 0, "scan_enabled": False, "scan_interval_minutes": 10,
        "media_type": source.media_type, "mode": source.mode,
        "targets": [
            {"id": item.id, "category": item.category, "path": item.path,
             "provider": item.provider, "library_id": item.library_id,
             "library_name": item.library_name, "server_path": item.server_path}
            for item in targets
        ],
    }


def _task_display_name(task) -> str:
    content_name = Path(task.content_path).name
    error = str(getattr(task, "error", "") or "")
    missing_episode_marker = "剧集文件缺少集数，不能自动归档:"
    if str(getattr(task, "status", "") or "") == "requires_manual" and missing_episode_marker in error:
        failed_video = error.partition(missing_episode_marker)[2].strip()
        if failed_video:
            return Path(failed_video).name
    return str(getattr(task, "title", "") or "").strip() or content_name


def _task_payload(task) -> dict:
    source = db.get_local_media_source(task.source_id, owner=_OWNER)
    return {
        "id": task.id, "source_id": task.source_id,
        "source_name": source.name if source else "已删除来源",
        "content_name": Path(task.content_path).name,
        "display_name": _task_display_name(task),
        "title": str(getattr(task, "title", "") or ""),
        "year": str(getattr(task, "year", "") or ""),
        "trigger": task.trigger, "status": task.status, "attempts": task.attempts,
        "tmdb_id": getattr(task, "tmdb_id", ""), "media_type": getattr(task, "media_type", ""),
        "season": getattr(task, "season_override", None),
        "episode": getattr(task, "episode_override", None),
        "numbering_mode": getattr(task, "numbering_mode", "auto") or "auto",
        "clearable": task.status not in LOCAL_BUSY_TASK_STATUSES,
        "error": task.error, "warning": task.warning,
        "created_at": task.created_at, "updated_at": task.updated_at,
        "completed_at": task.completed_at,
    }


def _task_detail_payload(task) -> dict:
    payload = _task_payload(task)
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
            inferred = infer_recognition_summary(
                item_rows, scraper=get_local_media_service().scraper,
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
    if isinstance(exc, (ValueError, LookupError, LocalMediaServiceError, FileNotFoundError, PathMappingError)):
        return api_error(str(exc), 400)
    if isinstance(exc, OSError):
        logger.warning("本地媒体文件系统异常 type=%s", type(exc).__name__)
        return api_error("本地目录访问失败，请检查 Docker 卷挂载与容器目录权限", 400)
    logger.error("本地媒体 API 失败 type=%s", type(exc).__name__)
    return api_error("本地媒体请求失败", 500)


def _validated_targets(payload: dict) -> list[dict[str, str]] | None:
    if "targets" not in payload or payload.get("targets") is None:
        return None
    targets = payload["targets"]
    if not isinstance(targets, list) or len(targets) > len(LOCAL_MEDIA_CATEGORIES):
        raise ValueError("targets 必须是分类目标数组")
    normalized: list[dict[str, str]] = []
    submitted: set[str] = set()
    for item in targets:
        if not isinstance(item, dict):
            raise ValueError("目标配置格式错误")
        category = _text(item, "category", required=True, max_length=32).lower()
        if category not in LOCAL_MEDIA_CATEGORIES or category in submitted:
            raise ValueError("目标分类无效或重复")
        submitted.add(category)
        path = _text(item, "path", required=True)
        provider = _text(item, "provider", max_length=32).lower()
        library_id = _text(item, "library_id", max_length=256)
        library_name = _text(item, "library_name", max_length=128)
        server_path = _text(item, "server_path", max_length=2048)
        if provider not in {"", "jellyfin", "emby"}:
            raise ValueError("目标媒体服务器类型无效")
        if provider and not library_name:
            raise ValueError("媒体服务器和媒体库名称必须同时选择")
        if not provider and (library_id or library_name or server_path):
            raise ValueError("未选择媒体服务器时不能绑定媒体库或服务端路径")
        if server_path:
            server_path = MediaServerPathMapping(path, server_path).server_prefix
        normalized.append({
            "category": category,
            "path": path,
            "provider": provider,
            "library_id": library_id,
            "library_name": library_name,
            "server_path": server_path,
        })
    return normalized


def _validated_container_directory(value: str, label: str) -> Path:
    raw = str(value or "").strip()
    if is_windows_or_unc_path(raw):
        raise ValueError(
            f"{label}必须填写 Docker 容器内路径；请先把宿主机或 NAS 目录挂载到容器，"
            "再填写例如 /media/downloads 的路径"
        )
    directory = Path(raw).expanduser()
    try:
        if not directory.is_absolute() or not directory.exists() or not directory.is_dir():
            raise ValueError(f"{label}必须是容器内已存在的绝对目录: {raw}")
        return assert_within(directory, directory)
    except OSError as exc:
        raise ValueError(f"{label}无法访问，请检查 Docker 卷挂载与容器目录权限") from exc


def _validate_source_paths(
    local_root: str,
    targets: list[dict[str, str]],
) -> tuple[str, list[dict[str, str]]]:
    source = _validated_container_directory(local_root, "来源目录")
    target_paths: list[Path] = []
    normalized_targets: list[dict[str, str]] = []
    for item in targets:
        target = _validated_container_directory(item["path"], "媒体库目标")
        target_paths.append(target)
        normalized_targets.append({**item, "path": str(target)})
    validate_source_target_roots(source, target_paths)
    return str(source), normalized_targets


@router.get("/directories")
def list_directories(
    request: Request,
    path: str = VIRTUAL_ROOT,
    source_id: int = 0,
):
    require_api_login(request)
    try:
        if str(request.query_params.get("network_root") or "").strip():
            return api_error(LEGACY_SOURCE_PATH_ERROR, 400)
        allowed_root = None
        if source_id:
            source = db.get_local_media_source(source_id, owner=_OWNER)
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
        normalized = str(provider or "").strip().lower()
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
            try:
                client.close()
            except Exception as exc:
                logger.debug("媒体库列表客户端关闭失败 type=%s", type(exc).__name__)
    except Exception as exc:
        return _safe_error(exc)


@router.get("/sources")
def list_sources(request: Request):
    require_api_login(request)
    return {"sources": [_source_payload(item) for item in db.list_local_media_sources(owner=_OWNER)]}


@router.post("/sources")
def create_source(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    payload = data or {}
    try:
        targets = _validated_targets(payload) or []
        local_root = _text(payload, "local_root", required=True)
        local_root, targets = _validate_source_paths(local_root, targets)
        source_id = db.save_local_media_source_bundle(
            name=_text(payload, "name", required=True, max_length=128),
            qb_profile=_text(payload, "qb_profile", max_length=64) or "configured:qb",
            qb_path_prefix=_text(payload, "qb_path_prefix"),
            local_root=local_root,
            enabled=_boolean(payload, "enabled", True),
            # 保留数据库列以兼容旧数据；新来源不再启用固定等待或目录轮询。
            stable_seconds=0, scan_enabled=False, scan_interval_minutes=10,
            owner=_OWNER, media_type=_text(payload, "media_type", max_length=16) or "auto",
            mode=_text(payload, "mode", max_length=16) or "move", targets=targets,
        )
        get_local_media_scheduler().reload()
        return _source_payload(db.get_local_media_source(source_id, owner=_OWNER))
    except Exception as exc:
        return _safe_error(exc)


@router.put("/sources/{source_id}")
def update_source(source_id: int, request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    payload = data or {}
    try:
        source = db.get_local_media_source(source_id, owner=_OWNER)
        if source is None:
            return api_error("本地媒体来源不存在", 404)
        targets = _validated_targets(payload)
        existing_targets = [
            {"category": item.category, "path": item.path, "provider": item.provider,
             "library_id": item.library_id, "library_name": item.library_name,
             "server_path": item.server_path}
            for item in db.list_local_library_targets(source_id, owner=_OWNER)
        ]
        effective_targets = targets if targets is not None else existing_targets
        local_root = _text(payload, "local_root", required=True) if "local_root" in payload else source.local_root
        local_root, effective_targets = _validate_source_paths(local_root, effective_targets)
        db.save_local_media_source_bundle(
            source_id=source_id, owner=_OWNER,
            name=_text(payload, "name", required=True, max_length=128) if "name" in payload else source.name,
            qb_profile=_text(payload, "qb_profile", max_length=64) if "qb_profile" in payload else source.qb_profile,
            qb_path_prefix=_text(payload, "qb_path_prefix") if "qb_path_prefix" in payload else source.qb_path_prefix,
            local_root=local_root,
            # 旧 schema 列保留兼容，但废弃凭据必须在编辑时清空。
            smb_user="",
            smb_pass="",
            enabled=_boolean(payload, "enabled", source.enabled) if "enabled" in payload else source.enabled,
            # 编辑旧来源时同步归一化已废弃的等待/轮询配置，避免隐藏配置继续生效。
            stable_seconds=0, scan_enabled=False, scan_interval_minutes=10,
            media_type=_text(payload, "media_type", max_length=16) if "media_type" in payload else source.media_type,
            mode=_text(payload, "mode", max_length=16) if "mode" in payload else source.mode,
            targets=effective_targets,
        )
        get_local_media_scheduler().reload()
        return _source_payload(db.get_local_media_source(source_id, owner=_OWNER))
    except Exception as exc:
        return _safe_error(exc)


@router.delete("/sources/{source_id}")
def delete_source(source_id: int, request: Request):
    require_api_login(request)
    try:
        if not db.delete_local_media_source(source_id, owner=_OWNER):
            return api_error("本地媒体来源不存在", 404)
        get_local_media_scheduler().reload()
        return {"deleted": True}
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
            source = db.get_local_media_source(source_id, owner=_OWNER)
            if source is None:
                return api_error("本地媒体来源不存在", 404)
            selected_path = _text({"path": path}, "path", max_length=4096) or source.local_root
            candidates, error, current = discover_local_media_directory_candidates(
                source, selected_path,
            )
            if error or current is None:
                return api_error(error or "目录读取失败", 400)
            targets = db.list_local_library_targets(source.id, owner=_OWNER)
            organize_ready = bool(targets) and source.mode != "preview_only"
            serialized: list[dict] = []
            for candidate in candidates:
                try:
                    serialized.append(candidate_payload(
                        source, candidate, organize_ready=organize_ready, allow_nested=True,
                    ))
                except (FileNotFoundError, OSError, PathMappingError):
                    continue
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
                    "id": source.id, "name": source.name, "count": len(serialized), "error": "",
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
        for source in db.list_local_media_sources(owner=_OWNER):
            targets = db.list_local_library_targets(source.id, owner=_OWNER)
            organize_ready = bool(targets) and source.mode != "preview_only"
            candidates, error, _ = discover_local_media_directory_candidates(source)
            if error:
                source_results.append({
                    "id": source.id, "name": source.name, "count": 0, "error": error,
                })
                continue
            serialized: list[dict] = []
            for candidate in candidates:
                try:
                    serialized.append(
                        candidate_payload(source, candidate, organize_ready=organize_ready)
                    )
                except (FileNotFoundError, OSError, PathMappingError):
                    # 列表读取期间条目可能被下载器改名或移动；跳过瞬时失效项即可。
                    continue
            items.extend(serialized)
            source_results.append({
                "id": source.id, "name": source.name, "count": len(serialized), "error": "",
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
        source = db.get_local_media_source(source_id, owner=_OWNER)
        if source is None:
            return api_error("本地媒体来源不存在", 404)
        identity = payload.get("identity")
        if not isinstance(identity, dict):
            raise ValueError("条目快照无效，请刷新后重试")
        destination = move_candidate_to_trash(
            source,
            _text(payload, "path", required=True),
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
def list_tasks(request: Request, status: str = ""):
    require_api_login(request)
    try:
        tasks = db.list_local_media_tasks(owner=_OWNER, status=status, limit=500)
        return {"tasks": [_task_payload(item) for item in tasks]}
    except Exception as exc:
        return _safe_error(exc)


@router.delete("/tasks")
def clear_tasks(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    payload = data or {}
    try:
        if _text(payload, "confirm", max_length=16) != "CLEAR":
            raise ValueError("清除本地整理日志需要确认")
        raw_ids = payload.get("ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError("请选择需要清除的本地整理日志")
        if len(raw_ids) > 500:
            raise ValueError("单次最多清除 500 条本地整理日志")
        task_ids: list[int] = []
        for value in raw_ids:
            if isinstance(value, bool):
                raise ValueError("日志 ID 必须是正整数")
            try:
                task_id = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("日志 ID 必须是正整数") from exc
            if task_id <= 0:
                raise ValueError("日志 ID 必须是正整数")
            task_ids.append(task_id)
        return db.delete_local_media_tasks(task_ids, owner=_OWNER)
    except Exception as exc:
        return _safe_error(exc)


@router.get("/tasks/{task_id}")
def task_detail(task_id: int, request: Request):
    require_api_login(request)
    try:
        task = db.get_local_media_task(task_id, owner=_OWNER)
        if task is None:
            return api_error("本地整理日志不存在", 404)
        item_rows = db.list_local_media_task_items(task_id, owner=_OWNER)
        return {
            "task": _task_detail_payload(task),
            "recognition": _task_recognition_payload(task, item_rows),
            "items": [_task_item_payload(row) for row in item_rows],
            "steps": [
                _task_step_payload(row)
                for row in db.list_local_media_operation_steps(task_id, owner=_OWNER)
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
        source = db.get_local_media_source(source_id, owner=_OWNER)
        if source is None:
            return api_error("本地媒体来源不存在", 404)
        path = _text(payload, "path") or source.local_root
        return get_local_media_service().inspect_source(_OWNER, source_id, path)
    except Exception as exc:
        return _safe_error(exc)


@router.post("/search")
def search_media(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    payload = data or {}
    try:
        query = _text(payload, "query", required=True, max_length=256)
        media_type = _text(payload, "media_type", max_length=16) or "auto"
        if media_type not in {"auto", "movie", "tv"}:
            raise ValueError("媒体类型必须是 auto、movie 或 tv")
        inspection_id = _text(payload, "inspection_id", max_length=64)
        return {"candidates": get_local_media_service().search(
            query,
            _text(payload, "year", max_length=4),
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
            _OWNER, _text(payload, "inspection_id", required=True, max_length=64),
            _text(payload, "tmdb_id", max_length=32), _text(payload, "media_type", max_length=16),
            payload.get("overrides") if isinstance(payload.get("overrides"), dict) else None,
            season_override=_optional_integer(
                payload, "season", minimum=0, maximum=99, label="季数",
            ),
            episode_override=_optional_integer(
                payload, "episode", minimum=1, maximum=999, label="集数",
            ),
            numbering_mode=_text(payload, "numbering_mode", max_length=32) or "auto",
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
            _text(payload, "inspection_id", required=True, max_length=64),
            _text(payload, "query", required=True, max_length=256),
            _text(payload, "media_type", max_length=16) or "auto",
        )
    except Exception as exc:
        return _safe_error(exc)


@router.post("/execute")
def execute_media(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    payload = data or {}
    try:
        service = get_local_media_service()
        task_id = service.create_manual_task(
            _OWNER, _text(payload, "inspection_id", required=True, max_length=64),
            tmdb_id=_text(payload, "tmdb_id", max_length=32),
            media_type=_text(payload, "media_type", max_length=16),
            rules_snapshot=_text(payload, "rules_snapshot", max_length=20000),
            season_override=_optional_integer(
                payload, "season", minimum=0, maximum=99, label="季数",
            ),
            episode_override=_optional_integer(
                payload, "episode", minimum=1, maximum=999, label="集数",
            ),
            numbering_mode=_text(payload, "numbering_mode", max_length=32) or "auto",
        )
        if not db.claim_local_media_task(task_id, expected="waiting_stable", owner=_OWNER):
            raise LocalMediaServiceError("任务已被其他操作认领")
        result = service.execute_task(_OWNER, task_id)
        notify_local_media_task(task_id, result, owner=_OWNER)
        return result
    except Exception as exc:
        if "task_id" in locals():
            notify_local_media_task(task_id, owner=_OWNER, error=str(exc))
        return _safe_error(exc)


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
        retry_fields: dict[str, object] = {}
        if "tmdb_id" in payload:
            retry_fields["tmdb_id"] = _text(payload, "tmdb_id", max_length=32)
        if "media_type" in payload:
            retry_fields["media_type"] = _text(payload, "media_type", max_length=16)
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
                _text(payload, "numbering_mode", max_length=32) or "auto"
            )
        if not db.reset_local_media_task(task_id, owner=_OWNER, **retry_fields):
            return api_error("任务不存在或当前状态不可重试", 409)
        get_local_media_scheduler().reload()
        return {"queued": True, "task_id": task_id}
    except Exception as exc:
        return _safe_error(exc)
