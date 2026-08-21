"""本地媒体来源、检查、预览与任务 API。"""
from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from app import database as db
from app.logger import get_logger
from app.modules.local_directory_browser import (
    VIRTUAL_ROOT, assert_browse_root_allowed, browse_local_directories,
)
from app.modules.local_path_mapping import (
    PathMappingError, assert_within, validate_source_target_roots,
)
from app.modules.local_media_candidates import (
    candidate_payload,
    discover_local_media_candidates,
    discover_local_media_directory_candidates,
    move_candidate_to_trash,
)
from app.modules.local_media_models import LOCAL_BUSY_TASK_STATUSES, LOCAL_MEDIA_CATEGORIES
from app.modules.media_server_profiles import list_configured_profiles
from app.modules.local_media_notifications import notify_local_media_task
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
        "smb_user": getattr(source, "smb_user", "") or "",
        "has_smb_pass": bool(getattr(source, "smb_pass", "")),
        "enabled": source.enabled, "stable_seconds": source.stable_seconds,
        "scan_enabled": source.scan_enabled,
        "scan_interval_minutes": source.scan_interval_minutes,
        "media_type": source.media_type, "mode": source.mode,
        "targets": [
            {"id": item.id, "category": item.category, "path": item.path,
             "provider": item.provider, "library_id": item.library_id,
             "library_name": item.library_name}
            for item in targets
        ],
    }


def _task_payload(task) -> dict:
    source = db.get_local_media_source(task.source_id, owner=_OWNER)
    return {
        "id": task.id, "source_id": task.source_id,
        "source_name": source.name if source else "已删除来源",
        "content_name": Path(task.content_path).name,
        "trigger": task.trigger, "status": task.status, "attempts": task.attempts,
        "tmdb_id": getattr(task, "tmdb_id", ""), "media_type": getattr(task, "media_type", ""),
        "season": getattr(task, "season_override", None),
        "episode": getattr(task, "episode_override", None),
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
        from app.modules.windows_smb import explain_windows_network_error
        friendly_msg = explain_windows_network_error(exc)
        logger.warning("本地媒体网络/文件系统异常: %s (type=%s)", friendly_msg, type(exc).__name__)
        return api_error(friendly_msg, 400)
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
        provider = _text(item, "provider", max_length=32).lower()
        library_id = _text(item, "library_id", max_length=256)
        library_name = _text(item, "library_name", max_length=128)
        if provider not in {"", "jellyfin", "emby"}:
            raise ValueError("目标媒体服务器类型无效")
        if provider and not library_name:
            raise ValueError("媒体服务器和媒体库名称必须同时选择")
        if not provider and (library_id or library_name):
            raise ValueError("未选择媒体服务器时不能绑定媒体库")
        normalized.append({
            "category": category,
            "path": _text(item, "path", required=True),
            "provider": provider,
            "library_id": library_id,
            "library_name": library_name,
        })
    return normalized


def _normalize_windows_unc_root(value: str) -> str:
    raw = str(value or "").strip()
    candidate = PureWindowsPath(raw)
    anchor = str(candidate.anchor)
    server_share = [part for part in anchor.strip("\\").split("\\") if part]
    if (
        not raw.startswith("\\\\")
        or not candidate.is_absolute()
        or not anchor.startswith("\\\\")
        or len(server_share) < 2
    ):
        raise ValueError(r"网络共享必须使用 UNC 路径，例如 \\NAS\Media")
    return str(candidate)


def _validate_source_paths(
    local_root: str,
    targets: list[dict[str, str]],
    *,
    smb_user: str = "",
    smb_pass: str = "",
) -> tuple[str, list[dict[str, str]]]:
    from app.modules.windows_smb import (
        ensure_smb_connection, explain_windows_network_error, parse_unc_share_root,
        resolve_drive_or_unc_path,
    )
    resolved_local_root = resolve_drive_or_unc_path(local_root)
    if parse_unc_share_root(resolved_local_root):
        ok, err = ensure_smb_connection(resolved_local_root, smb_user, smb_pass)
        if not ok and err:
            raise ValueError(err)

    source = Path(resolved_local_root).expanduser()
    try:
        if not source.is_absolute() or not source.exists() or not source.is_dir():
            raise ValueError(f"来源目录必须是已存在的绝对目录: {local_root}")
    except OSError as exc:
        raise ValueError(explain_windows_network_error(exc, resolved_local_root)) from exc
    assert_within(source, source)
    target_paths: list[Path] = []
    normalized_targets: list[dict[str, str]] = []
    for item in targets:
        target_str = item["path"]
        resolved_target = resolve_drive_or_unc_path(target_str)
        if parse_unc_share_root(resolved_target):
            ok, err = ensure_smb_connection(resolved_target, smb_user, smb_pass)
            if not ok and err:
                raise ValueError(err)
        target = Path(resolved_target).expanduser()
        try:
            if not target.is_absolute() or not target.exists() or not target.is_dir():
                raise ValueError(f"媒体库目标必须是已存在的绝对目录: {target_str}")
        except OSError as exc:
            raise ValueError(explain_windows_network_error(exc, resolved_target)) from exc
        assert_within(target, target)
        target_paths.append(target)
        normalized_targets.append({**item, "path": resolved_target})
    validate_source_target_roots(source, target_paths)
    return resolved_local_root, normalized_targets


@router.get("/directories")
def list_directories(
    request: Request,
    path: str = VIRTUAL_ROOT,
    source_id: int = 0,
    network_root: str = "",
    smb_user: str = "",
    smb_pass: str = "",
):
    require_api_login(request)
    try:
        allowed_root = None
        if network_root:
            if os.name != "nt":
                return api_error("UNC 网络共享浏览仅支持 Windows 环境", 400)
            normalized_root = _normalize_windows_unc_root(network_root)
            from app.modules.windows_smb import ensure_smb_connection
            ok, err = ensure_smb_connection(normalized_root, smb_user, smb_pass)
            if not ok and err:
                return api_error(err, 400)
            allowed_root = assert_browse_root_allowed(Path(normalized_root))
            if not path or path == VIRTUAL_ROOT:
                path = normalized_root
        elif source_id:
            source = db.get_local_media_source(source_id, owner=_OWNER)
            if source is None:
                return api_error("本地媒体来源不存在", 404)
            from app.modules.windows_smb import ensure_smb_connection, parse_unc_share_root
            effective_user = smb_user or getattr(source, "smb_user", "")
            effective_pass = smb_pass or getattr(source, "smb_pass", "")
            if parse_unc_share_root(source.local_root):
                ok, err = ensure_smb_connection(source.local_root, effective_user, effective_pass)
                if not ok and err:
                    return api_error(err, 400)
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
        return {"provider": normalized, "libraries": client.list_virtual_folders()}
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
        smb_user = _text(payload, "smb_user")
        smb_pass = _text(payload, "smb_pass")
        local_root, targets = _validate_source_paths(local_root, targets, smb_user=smb_user, smb_pass=smb_pass)
        source_id = db.save_local_media_source_bundle(
            name=_text(payload, "name", required=True, max_length=128),
            qb_profile=_text(payload, "qb_profile", max_length=64) or "configured:qb",
            qb_path_prefix=_text(payload, "qb_path_prefix"),
            local_root=local_root,
            smb_user=smb_user,
            smb_pass=smb_pass,
            enabled=_boolean(payload, "enabled", True),
            stable_seconds=_integer(payload, "stable_seconds", 300, 0, 86400),
            scan_enabled=_boolean(payload, "scan_enabled", False),
            scan_interval_minutes=_integer(payload, "scan_interval_minutes", 10, 1, 1440),
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
             "library_id": item.library_id, "library_name": item.library_name}
            for item in db.list_local_library_targets(source_id, owner=_OWNER)
        ]
        effective_targets = targets if targets is not None else existing_targets
        local_root = _text(payload, "local_root", required=True) if "local_root" in payload else source.local_root
        smb_user = _text(payload, "smb_user") if "smb_user" in payload else getattr(source, "smb_user", "")
        if "smb_pass" in payload:
            submitted_pass = _text(payload, "smb_pass")
            if not submitted_pass and getattr(source, "smb_pass", ""):
                smb_pass = source.smb_pass
            else:
                smb_pass = submitted_pass
        else:
            smb_pass = getattr(source, "smb_pass", "")
        local_root, effective_targets = _validate_source_paths(local_root, effective_targets, smb_user=smb_user, smb_pass=smb_pass)
        db.save_local_media_source_bundle(
            source_id=source_id, owner=_OWNER,
            name=_text(payload, "name", required=True, max_length=128) if "name" in payload else source.name,
            qb_profile=_text(payload, "qb_profile", max_length=64) if "qb_profile" in payload else source.qb_profile,
            qb_path_prefix=_text(payload, "qb_path_prefix") if "qb_path_prefix" in payload else source.qb_path_prefix,
            local_root=local_root,
            smb_user=smb_user,
            smb_pass=smb_pass,
            enabled=_boolean(payload, "enabled", source.enabled) if "enabled" in payload else source.enabled,
            stable_seconds=_integer(payload, "stable_seconds", source.stable_seconds, 0, 86400) if "stable_seconds" in payload else source.stable_seconds,
            scan_enabled=_boolean(payload, "scan_enabled", source.scan_enabled) if "scan_enabled" in payload else source.scan_enabled,
            scan_interval_minutes=_integer(payload, "scan_interval_minutes", source.scan_interval_minutes, 1, 1440) if "scan_interval_minutes" in payload else source.scan_interval_minutes,
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
            candidates, error = discover_local_media_candidates(source)
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
        return {
            "task": _task_detail_payload(task),
            "items": [
                _task_item_payload(row)
                for row in db.list_local_media_task_items(task_id, owner=_OWNER)
            ],
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
        media_type = _text(payload, "media_type", max_length=16) or "movie"
        if media_type not in {"movie", "tv"}:
            raise ValueError("媒体类型必须是 movie 或 tv")
        return {"candidates": get_local_media_service().search(query, _text(payload, "year", max_length=4), media_type)}
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
        )
        return {key: value for key, value in result.items() if not key.startswith("_")}
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
        if not db.reset_local_media_task(task_id, owner=_OWNER, **retry_fields):
            return api_error("任务不存在或当前状态不可重试", 409)
        get_local_media_scheduler().reload()
        return {"queued": True, "task_id": task_id}
    except Exception as exc:
        return _safe_error(exc)
