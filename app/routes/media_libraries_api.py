"""媒体服务器、库位置与路径映射的统一只读/诊断 API。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Request

from app import config
from app import database as db
from app.logger import get_logger
from app.modules.local_directory_browser import VIRTUAL_ROOT, browse_local_directories
from app.modules.local_path_mapping import PathMappingError
from app.modules.media_server_path_mapping import (
    MediaServerPathMapping,
    MediaServerPathMappingError,
    media_server_path_is_within,
    parse_media_server_path_mappings,
)
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
    strm = _strm_summary()
    return {
        "servers": servers,
        "local_bindings": bindings,
        "strm": strm,
        "summary": {
            "configured_servers": sum(1 for item in servers if item["configured"]),
            "online_servers": sum(
                1 for item in servers
                if item["enabled"] and item["configured"] and not item["library_error"]
            ),
            "libraries": sum(len(item["libraries"]) for item in servers),
            "path_mappings": sum(len(item["mappings"]) for item in servers),
            "local_bindings": len(bindings),
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
