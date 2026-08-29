"""媒体服务器、STRM 与本地归档路径映射的统一管理 API。"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Request

from app import config
from app import database as db
from app.logger import get_logger
from app.modules.local_directory_browser import VIRTUAL_ROOT, browse_local_directories
from app.modules.local_path_mapping import (
    PathMappingError,
    require_container_absolute_path,
    validate_source_target_roots,
)
from app.modules.media_server_path_mapping import (
    MediaServerPathMapping,
    MediaServerPathMappingError,
    encode_media_server_path_mappings,
    media_server_path_is_within,
    parse_media_server_path_mappings,
)
from app.modules.local_media_models import LOCAL_MEDIA_CATEGORIES
from app.modules.media_server_profiles import MediaServerProfile, list_configured_profiles
from app.modules.strm import (
    STRM_SUBDIR,
    parse_strm_sources,
    plan_strm_sources,
    safe_path_component,
)
from app.web import api_error, require_api_login

logger = get_logger(__name__)
router = APIRouter(prefix="/api/media-libraries")
_OWNER = "admin"
_CATEGORY_LABELS = {
    "default": "默认",
    "movie": "电影",
    "tv": "剧集",
    "anime": "动漫",
    "documentary": "纪录片",
    "variety": "综艺",
    "concert": "演唱会",
    "kids": "儿童节目",
}
_MAPPING_CONFIG = {
    "jellyfin": ("JELLYFIN_PATH_MAPPINGS", "JELLYFIN_ALLOW_GLOBAL_REFRESH_FALLBACK"),
    "emby": ("EMBY_PATH_MAPPINGS", "EMBY_ALLOW_GLOBAL_REFRESH_FALLBACK"),
}


def _client_for(profile: MediaServerProfile):
    if profile.server_type == "jellyfin":
        from app.clients.jellyfin import JellyfinClient

        return JellyfinClient(profile.url, profile.credential)
    if profile.server_type == "emby":
        from app.clients.emby import EmbyClient

        return EmbyClient(profile.url, profile.credential)
    raise ValueError("媒体服务器类型无效")


def _library_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or "").strip(),
        "name": str(item.get("name") or "").strip(),
        "collection_type": str(
            item.get("collection_type") or item.get("CollectionType") or ""
        ).strip().lower(),
        "locations": [
            str(location or "").strip()
            for location in item.get("locations") or []
            if str(location or "").strip()
        ],
    }


def _mapping_payload(provider: str) -> tuple[list[dict[str, str]], str]:
    mapping_key, _fallback_key = _MAPPING_CONFIG[provider]
    try:
        mappings = parse_media_server_path_mappings(config.get(mapping_key, ""))
    except MediaServerPathMappingError as exc:
        return [], str(exc)
    return [
        {"local": item.local_prefix, "server": item.server_prefix}
        for item in mappings
    ], ""


def _probe_profile(profile: MediaServerProfile) -> tuple[str, list[dict[str, Any]], str]:
    if not profile.enabled:
        return profile.server_type, [], "媒体服务器未启用"
    if not profile.configured:
        return profile.server_type, [], "媒体服务器配置不完整"
    try:
        libraries = [_library_payload(item) for item in _client_for(profile).list_virtual_folders()]
        return profile.server_type, libraries, ""
    except Exception as exc:
        logger.warning(
            "媒体库控制中心读取上游失败 provider=%s type=%s",
            profile.server_type,
            type(exc).__name__,
        )
        return profile.server_type, [], "媒体库读取失败，请测试服务器连接"


def _local_sources() -> list[dict[str, Any]]:
    return [
        {
            "id": int(source.id),
            "name": source.name,
            "local_root": source.local_root,
            "enabled": bool(source.enabled),
        }
        for source in db.list_local_media_sources(owner=_OWNER)
    ]


def _local_bindings() -> list[dict[str, Any]]:
    bindings: list[dict[str, Any]] = []
    for source in db.list_local_media_sources(owner=_OWNER):
        for target in db.list_local_library_targets(source.id, owner=_OWNER):
            bindings.append({
                "source_id": int(source.id),
                "source_name": source.name,
                "category": target.category,
                "category_label": _CATEGORY_LABELS.get(target.category, target.category),
                "local_path": target.path,
                "provider": target.provider,
                "library_id": target.library_id,
                "library_name": target.library_name,
                "server_path": str(getattr(target, "server_path", "") or ""),
            })
    return bindings


def _strm_directory_options(
    output_root: str,
    plans: list[dict[str, str]],
) -> list[dict[str, str]]:
    """枚举可用于媒体库映射的 STRM 根目录、来源目录与分类目录。"""
    if not output_root:
        return []

    root_path = Path(output_root)
    options: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_option(path: Path, *, label: str, kind: str, source_id: str = "") -> None:
        text = str(path)
        key = text.rstrip("/\\").casefold()
        if not key or key in seen:
            return
        seen.add(key)
        options.append({
            "id": f"{kind}:{source_id}:{len(options)}",
            "name": label,
            "local_path": text,
            "kind": kind,
            "source_id": source_id,
        })

    add_option(root_path, label="全部 STRM", kind="root")

    source_roots: list[tuple[str, str, Path]] = []
    for item in plans:
        rel_prefix = str(item.get("rel_prefix") or "").strip()
        safe_prefix = safe_path_component(rel_prefix) if rel_prefix else ""
        source_path = root_path / safe_prefix if safe_prefix else root_path
        source_id = str(item.get("id") or "")
        source_name = str(item.get("name") or "STRM 来源")
        source_roots.append((source_id, source_name, source_path))
        if source_path != root_path and source_path.is_dir():
            add_option(
                source_path,
                label=source_name,
                kind="source",
                source_id=source_id,
            )

    scan_roots = source_roots or [("", "", root_path)]
    for source_id, source_name, base_path in scan_roots:
        try:
            children = sorted(
                (item for item in base_path.iterdir() if item.is_dir() and not item.name.startswith(".")),
                key=lambda item: item.name.casefold(),
            )
        except OSError:
            children = []
        for child in children[:128]:
            # 多来源时，输出根的一层目录是来源本身；分类目录应从来源目录下枚举。
            if base_path == root_path and any(child == planned[2] for planned in source_roots if planned[2] != root_path):
                continue
            label = f"{source_name} / {child.name}" if source_name and base_path != root_path else child.name
            add_option(
                child,
                label=label,
                kind="category",
                source_id=source_id,
            )

    return options


def _strm_summary() -> dict[str, Any]:
    root = str(config.get("STRM_ROOT", "") or "").strip()
    sources, error = parse_strm_sources(
        config.get("GY_STRM_SOURCE_DIRS", ""),
        require_nonempty=False,
    )
    plans = plan_strm_sources(sources) if not error else []
    output_root = str(Path(root) / STRM_SUBDIR) if root else ""
    source_payload = []
    for item in plans:
        rel_prefix = str(item.get("rel_prefix") or "").strip()
        safe_prefix = safe_path_component(rel_prefix) if rel_prefix else ""
        source_payload.append({
            "id": item["id"],
            "name": item["name"],
            "local_path": str(Path(output_root) / safe_prefix)
            if output_root and safe_prefix else output_root,
        })
    return {
        "root": root,
        "output_root": output_root,
        "source_error": error,
        "sources": source_payload,
        "directories": _strm_directory_options(output_root, plans),
    }


def _strm_output_root() -> Path:
    """返回当前实例真实的 STRM 输出根，目录浏览不得越过此边界。"""
    root = str(config.get("STRM_ROOT", "") or "").strip()
    if not root:
        raise ValueError("尚未配置 STRM 本地根目录")
    output_root = Path(root).expanduser() / STRM_SUBDIR
    if not output_root.exists():
        raise ValueError("STRM 输出目录不存在，请先完成一次同步")
    if not output_root.is_dir():
        raise ValueError("STRM 输出路径不是目录")
    return output_root


@router.get("/strm-directories")
def list_strm_directories(request: Request, path: str = ""):
    """浏览 STRM 输出目录；允许任意层级，但永远限制在输出根之内。"""
    require_api_login(request)
    try:
        output_root = _strm_output_root()
        requested = str(path or "").strip()
        if not requested or requested == VIRTUAL_ROOT:
            requested = str(output_root)
        return browse_local_directories(requested, allowed_root=output_root)
    except (PathMappingError, ValueError, FileNotFoundError) as exc:
        return api_error(str(exc), 400)
    except OSError as exc:
        logger.warning("STRM 目录浏览失败 type=%s", type(exc).__name__)
        return api_error("无法读取 STRM 目录，请检查 Docker 卷挂载与目录权限", 400)


@router.get("/local-directories")
def list_local_directories(request: Request, path: str = VIRTUAL_ROOT):
    """浏览允许的容器目录，供本地归档目标选择。"""
    require_api_login(request)
    try:
        return browse_local_directories(path)
    except (PathMappingError, ValueError, FileNotFoundError) as exc:
        return api_error(str(exc), 400)
    except OSError as exc:
        logger.warning("本地归档目录浏览失败 type=%s", type(exc).__name__)
        return api_error("无法读取本地目录，请检查 Docker 卷挂载与权限", 400)


@router.get("/overview")
def media_library_overview(request: Request):
    require_api_login(request)
    profiles = list_configured_profiles()
    probes: dict[str, tuple[list[dict[str, Any]], str]] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(2, len(profiles)))) as executor:
        futures = {executor.submit(_probe_profile, profile): profile for profile in profiles}
        for future in as_completed(futures):
            provider, libraries, error = future.result()
            probes[provider] = (libraries, error)

    servers = []
    for profile in profiles:
        mapping_key, fallback_key = _MAPPING_CONFIG[profile.server_type]
        mappings, mapping_error = _mapping_payload(profile.server_type)
        libraries, library_error = probes.get(profile.server_type, ([], "媒体库读取失败"))
        servers.append({
            **profile.public_dict(),
            "mapping_key": mapping_key,
            "fallback_key": fallback_key,
            "mappings": mappings,
            "mapping_error": mapping_error,
            "allow_global_refresh_fallback": config.get_bool(fallback_key, False),
            "libraries": libraries,
            "library_error": library_error,
        })

    bindings = _local_bindings()
    local_sources = _local_sources()
    strm = _strm_summary()
    strm_mapping_count = sum(len(item["mappings"]) for item in servers)
    return {
        "servers": servers,
        "local_sources": local_sources,
        "local_bindings": bindings,
        "strm": strm,
        "summary": {
            "configured_servers": sum(1 for item in servers if item["configured"]),
            "online_servers": sum(
                1 for item in servers
                if item["enabled"] and item["configured"] and not item["library_error"]
            ),
            "libraries": sum(len(item["libraries"]) for item in servers),
            "path_mappings": strm_mapping_count,
            "local_bindings": len(bindings),
            "total_mappings": strm_mapping_count + len(bindings),
            "local_sources": len(local_sources),
            "strm_sources": len(strm["sources"]),
        },
    }


def _required_text(payload: dict[str, Any], key: str, label: str) -> str:
    value = payload.get(key, "")
    if isinstance(value, (dict, list, tuple, bool)):
        raise ValueError(f"{label}格式无效")
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label}不能为空")
    if "\x00" in text or len(text) > 2048:
        raise ValueError(f"{label}无效")
    return text


def _optional_text(payload: dict[str, Any], key: str, *, maximum: int = 2048) -> str:
    value = payload.get(key, "")
    if isinstance(value, (dict, list, tuple, bool)):
        raise ValueError(f"{key}格式无效")
    text = str(value or "").strip()
    if "\x00" in text or len(text) > maximum:
        raise ValueError(f"{key}无效")
    return text


def _validated_local_bindings(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, list):
        raise ValueError("local_bindings 必须是数组")
    sources = {int(item.id): item for item in db.list_local_media_sources(owner=_OWNER)}
    if len(raw) > max(64, len(sources) * len(LOCAL_MEDIA_CATEGORIES)):
        raise ValueError("本地归档映射数量过多")
    normalized: list[dict[str, object]] = []
    seen: set[tuple[int, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("本地归档映射格式无效")
        try:
            source_id = int(item.get("source_id") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("本地归档来源无效") from exc
        source = sources.get(source_id)
        if source is None:
            raise ValueError("本地归档来源不存在")
        category = _required_text(item, "category", "本地归档分类").lower()
        key = (source_id, category)
        if category not in LOCAL_MEDIA_CATEGORIES or key in seen:
            raise ValueError("本地归档分类无效或重复")
        path_text = _required_text(item, "local_path", "本地归档目录")
        target = require_container_absolute_path(path_text, label="本地归档目录")
        if not target.exists() or not target.is_dir() or target.is_symlink():
            raise ValueError(f"本地归档目录必须是已存在的普通目录: {path_text}")
        validate_source_target_roots(Path(source.local_root), [target])
        provider = _optional_text(item, "provider", maximum=32).lower()
        library_id = _optional_text(item, "library_id", maximum=256)
        library_name = _optional_text(item, "library_name", maximum=128)
        server_path = _optional_text(item, "server_path")
        if provider not in {"jellyfin", "emby"}:
            raise ValueError("本地归档必须选择 Jellyfin 或 Emby 媒体库")
        if not library_name:
            raise ValueError("本地归档必须选择媒体库")
        if not server_path:
            raise ValueError("本地归档必须填写媒体服务器可见路径")
        server_path = MediaServerPathMapping(str(target), server_path).server_prefix
        seen.add(key)
        normalized.append({
            "source_id": source_id,
            "category": category,
            "path": str(target),
            "provider": provider,
            "library_id": library_id,
            "library_name": library_name,
            "server_path": server_path,
        })
    return normalized


def _validated_strm_mapping_updates(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError("strm_mappings 必须是对象")
    unknown = sorted(set(raw) - set(_MAPPING_CONFIG))
    if unknown:
        raise ValueError(f"包含无效媒体服务器类型: {', '.join(unknown[:3])}")
    updates: dict[str, str] = {}
    for provider, mappings in raw.items():
        mapping_key, _fallback_key = _MAPPING_CONFIG[provider]
        if config.has_external_override(mapping_key):
            raise ValueError(f"{provider} 路径映射由部署环境管理，不能在页面修改")
        updates[mapping_key] = encode_media_server_path_mappings(
            json.dumps(mappings, ensure_ascii=False)
        )
    return updates


@router.post("/mappings")
def save_media_library_mappings(request: Request, data: dict | None = Body(default=None)):
    """统一保存 STRM 路径映射和本地归档绑定。"""
    require_api_login(request)
    payload = data or {}
    if not isinstance(payload, dict):
        return api_error("映射配置必须是 JSON 对象", 400)
    try:
        local_bindings = _validated_local_bindings(payload.get("local_bindings", []))
        strm_updates = _validated_strm_mapping_updates(payload.get("strm_mappings", {}))
        previous_bindings = [
            {
                "source_id": item["source_id"],
                "category": item["category"],
                "path": item["local_path"],
                "provider": item["provider"],
                "library_id": item["library_id"],
                "library_name": item["library_name"],
                "server_path": item["server_path"],
            }
            for item in _local_bindings()
        ]
        db.replace_local_library_targets(local_bindings, owner=_OWNER)
        try:
            if strm_updates:
                config.set_and_save(strm_updates)
        except Exception:
            db.replace_local_library_targets(previous_bindings, owner=_OWNER)
            raise
        try:
            from app.modules.local_media_scheduler import get_local_media_scheduler
            get_local_media_scheduler().reload()
        except Exception as exc:
            logger.warning(
                "统一媒体库映射保存后调度器重载失败 type=%s",
                type(exc).__name__,
            )
        return {"success": True, "local_bindings": _local_bindings()}
    except (ValueError, LookupError, PathMappingError) as exc:
        return api_error(str(exc), 400)
    except Exception as exc:
        logger.error("统一媒体库映射保存失败 type=%s", type(exc).__name__)
        return api_error("媒体库映射保存失败，请稍后重试", 500)


@router.post("/path-test")
def test_media_library_path(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    payload = data or {}
    try:
        provider = _required_text(payload, "provider", "媒体服务器类型").lower()
        if provider not in _MAPPING_CONFIG:
            raise ValueError("媒体服务器类型无效")
        local_path = _required_text(payload, "local_path", "MediaFlux 路径")
        server_path = _required_text(payload, "server_path", "媒体服务器路径")
        raw_sample_path = payload.get("sample_path")
        sample_path = (
            _required_text(payload, "sample_path", "测试路径")
            if raw_sample_path not in (None, "")
            else local_path
        )
        mapping = MediaServerPathMapping(local_path, server_path)
        mapped_path = mapping.apply(sample_path)
    except ValueError as exc:
        return api_error(str(exc), 400)

    profiles = {item.server_type: item for item in list_configured_profiles()}
    profile = profiles.get(provider)
    if profile is None or not profile.enabled or not profile.configured:
        return api_error("媒体服务器未启用或配置不完整", 400)
    try:
        libraries = [_library_payload(item) for item in _client_for(profile).list_virtual_folders()]
    except Exception as exc:
        logger.warning(
            "媒体库路径测试读取上游失败 provider=%s type=%s",
            provider,
            type(exc).__name__,
        )
        return api_error("媒体库读取失败，请先测试服务器连接", 502)

    matches: list[dict[str, Any]] = []
    for library in libraries:
        direct_locations = [
            location for location in library["locations"]
            if media_server_path_is_within(mapped_path, location)
        ]
        covered_locations = [
            location for location in library["locations"]
            if not media_server_path_is_within(mapped_path, location)
            and media_server_path_is_within(location, mapped_path)
        ]
        if direct_locations or covered_locations:
            matches.append({
                "id": library["id"],
                "name": library["name"],
                "mode": "direct" if direct_locations else "covered",
                "locations": direct_locations or covered_locations,
            })

    return {
        "success": True,
        "provider": provider,
        "local_path": mapping.local_prefix,
        "server_path": mapping.server_prefix,
        "sample_path": sample_path,
        "mapped_path": mapped_path,
        "status": (
            "matched" if any(item["mode"] == "direct" for item in matches)
            else "covered" if matches else "unmatched"
        ),
        "matches": matches,
    }
