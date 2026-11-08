"""媒体服务器可见路径映射。

MediaFlux 运行在容器/Linux 路径空间中，而 Jellyfin/Emby 可能通过另一套
POSIX、Windows 盘符或 UNC 路径访问同一目录。本模块提供显式、最长前缀优先
的路径映射，刷新使用正向映射，明确删除使用严格反向解析。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, TypedDict

from app import config
from app.logger import get_logger
from app.modules.local_path_mapping import (
    normalize_qb_path,
    normalized_path_parts,
    path_uses_casefold,
)

logger = get_logger(__name__)

_DRIVE_RE = re.compile(r"^[A-Za-z]:/")
MAPPING_CONFIG = {
    "jellyfin": (
        "JELLYFIN_PATH_MAPPINGS",
        "JELLYFIN_ALLOW_GLOBAL_REFRESH_FALLBACK",
    ),
    "emby": (
        "EMBY_PATH_MAPPINGS",
        "EMBY_ALLOW_GLOBAL_REFRESH_FALLBACK",
    ),
}


class MediaServerPathMappingError(ValueError):
    """媒体服务器路径映射配置无效。"""


def _is_absolute(value: str) -> bool:
    return value.startswith("/") or value.startswith("//") or bool(_DRIVE_RE.match(value))


def _comparison_key(value: str) -> str:
    return value.casefold() if path_uses_casefold(value) else value


def normalize_media_server_path(raw_path: object) -> str:
    """按路径风格规范化，保留 POSIX 大小写和 Windows 驱动器根。"""
    normalized = normalize_qb_path(str(raw_path or ""))
    if re.fullmatch(r"[A-Za-z]:", normalized):
        return f"{normalized}/"
    return normalized


def media_server_path_key(raw_path: object) -> str:
    """返回用于比较的路径键；Windows/UNC 不区分大小写。"""
    normalized = normalize_media_server_path(raw_path)
    return _comparison_key(normalized)


def media_server_path_is_within(candidate: object, ancestor: object) -> bool:
    """判断路径是否等于或位于祖先下，且不会跨越目录片段。"""
    try:
        candidate_key = media_server_path_key(candidate)
        ancestor_key = media_server_path_key(ancestor)
    except Exception:
        return False
    if candidate_key == ancestor_key:
        return True
    if ancestor_key == "/":
        return candidate_key.startswith("/") and not candidate_key.startswith("//")
    if _DRIVE_RE.fullmatch(ancestor_key):
        return candidate_key.startswith(ancestor_key)
    return candidate_key.startswith(f"{ancestor_key}/")


def _alias_value(entry: dict, keys: tuple[str, ...], *, label: str) -> str:
    values = [str(entry.get(key) or "").strip() for key in keys]
    values = [value for value in values if value]
    if not values:
        return ""
    try:
        normalized = [normalize_media_server_path(value) for value in values]
    except Exception as exc:
        raise MediaServerPathMappingError(str(exc)) from exc
    comparison = {_comparison_key(value) for value in normalized}
    if len(comparison) > 1:
        raise MediaServerPathMappingError(f"{label}存在冲突字段")
    return values[0]


@dataclass(frozen=True)
class MediaServerPathMapping:
    """一条 MediaFlux 本地路径到媒体服务器可见路径的映射。"""

    local_prefix: str
    server_prefix: str

    def __post_init__(self) -> None:
        try:
            local = normalize_media_server_path(self.local_prefix)
            server = normalize_media_server_path(self.server_prefix)
        except Exception as exc:
            raise MediaServerPathMappingError(str(exc)) from exc
        if not _is_absolute(local):
            raise MediaServerPathMappingError("本地路径必须是绝对路径")
        if not _is_absolute(server):
            raise MediaServerPathMappingError("媒体服务器路径必须是绝对路径或 UNC 路径")
        if local in {"/", "//"} or server in {"/", "//"}:
            raise MediaServerPathMappingError("路径映射不能使用文件系统根目录")
        object.__setattr__(
            self, "local_prefix",
            local if _DRIVE_RE.fullmatch(local) else local.rstrip("/"),
        )
        object.__setattr__(
            self, "server_prefix",
            server if _DRIVE_RE.fullmatch(server) else server.rstrip("/"),
        )

    def relative_parts(self, raw_path: str) -> tuple[str, ...]:
        path = normalize_media_server_path(raw_path)
        prefix_anchor, prefix_parts = normalized_path_parts(self.local_prefix)
        path_anchor, path_parts = normalized_path_parts(path)
        normalize = str.casefold if path_uses_casefold(self.local_prefix) else (lambda value: value)
        if normalize(path_anchor) != normalize(prefix_anchor):
            raise MediaServerPathMappingError("路径未命中当前映射")
        if len(path_parts) < len(prefix_parts):
            raise MediaServerPathMappingError("路径未命中当前映射")
        if tuple(map(normalize, path_parts[:len(prefix_parts)])) != tuple(
            map(normalize, prefix_parts)
        ):
            raise MediaServerPathMappingError("路径未命中当前映射")
        return path_parts[len(prefix_parts):]

    def matches(self, raw_path: str) -> bool:
        try:
            self.relative_parts(raw_path)
        except Exception:
            return False
        return True

    def apply(self, raw_path: str) -> str:
        suffix_parts = self.relative_parts(raw_path)
        suffix = "/".join(suffix_parts)
        if not suffix:
            return self.server_prefix
        separator = "" if self.server_prefix.endswith("/") else "/"
        return f"{self.server_prefix}{separator}{suffix}"


def parse_media_server_path_mappings(raw: object) -> tuple[MediaServerPathMapping, ...]:
    """解析 JSON 映射；支持对象、二元数组和 ``local/server`` 对象数组。"""
    text = str(raw or "").strip()
    if not text:
        return ()
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise MediaServerPathMappingError("路径映射必须是有效 JSON") from exc

    entries: Iterable[object]
    if isinstance(payload, dict):
        entries = [
            {"local": local, "server": server}
            for local, server in payload.items()
        ]
    elif isinstance(payload, list):
        entries = payload
    else:
        raise MediaServerPathMappingError("路径映射必须是 JSON 对象或数组")

    mappings: list[MediaServerPathMapping] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        local = server = ""
        if isinstance(entry, dict):
            local = _alias_value(entry, ("local", "source"), label=f"第 {index} 条本地路径")
            server = _alias_value(
                entry,
                ("server", "remote", "target"),
                label=f"第 {index} 条服务器路径",
            )
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            local, server = (str(entry[0] or "").strip(), str(entry[1] or "").strip())
        if not local or not server:
            raise MediaServerPathMappingError(f"第 {index} 条路径映射缺少本地路径或服务器路径")
        mapping = MediaServerPathMapping(local, server)
        key = _comparison_key(mapping.local_prefix)
        if key in seen:
            raise MediaServerPathMappingError("存在重复或大小写歧义的本地路径前缀")
        seen.add(key)
        mappings.append(mapping)
    return tuple(sorted(mappings, key=lambda item: len(item.local_prefix), reverse=True))


def encode_media_server_path_mappings(raw: object) -> str:
    """校验并编码为稳定、紧凑的 JSON，便于保存到单行 user.env。"""
    mappings = parse_media_server_path_mappings(raw)
    return json.dumps(
        [
            {"local": item.local_prefix, "server": item.server_prefix}
            for item in mappings
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ) if mappings else ""


@contextmanager
def mapping_write_guard() -> Generator[None, None, None]:
    """Web 组合保存与单条 CRUD 统一先取配置锁，再取映射锁。"""
    from app.modules.process_lock import CrossProcessLock

    with config.runtime_config_read_guard():
        lock = CrossProcessLock("media-library-mappings")
        if not lock.acquire():
            raise RuntimeError("媒体库映射正在由其他请求保存")
        try:
            yield
        finally:
            lock.release()


def mapping_updates(raw: object) -> dict[str, str]:
    """Web 批量保存与单条 CRUD 共用校验、部署覆盖和规范编码。"""
    if not isinstance(raw, dict):
        raise MediaServerPathMappingError("strm_mappings 必须是对象")
    if set(raw) - set(MAPPING_CONFIG):
        raise MediaServerPathMappingError("包含无效媒体服务器类型")
    updates = {}
    for server, mappings in raw.items():
        key = MAPPING_CONFIG[server][0]
        if config.has_external_override(key):
            raise MediaServerPathMappingError(f"{server} 路径映射由部署环境管理，不能修改")
        updates[key] = encode_media_server_path_mappings(json.dumps(mappings, ensure_ascii=False))
    return updates


def _mapping_state(server: str) -> tuple[bytes | None, list[dict[str, str]]]:
    if server not in MAPPING_CONFIG:
        raise MediaServerPathMappingError("server 仅支持 jellyfin 或 emby")
    key = MAPPING_CONFIG[server][0]
    raw, values = config.read_env_snapshot(config.ENV_FILE)
    value = config.get(key, "") if config.has_external_override(key) else values.get(key, "")
    items = [{"local": item.local_prefix, "server": item.server_prefix}
             for item in parse_media_server_path_mappings(value)]
    return raw, items


def list_path_mappings(server: str) -> dict[str, Any]:
    """完整读取配置路径对，不探测上游或枚举媒体。"""
    with config.runtime_config_read_guard():
        _, items = _mapping_state(server)
        return {"server": server, "items": items, "count": len(items),
                "managed_by_environment": config.has_external_override(MAPPING_CONFIG[server][0])}


class MappingSnapshotChanged(MediaServerPathMappingError):
    """确认快照或文件 CAS 已失效，未提交映射。"""


def prepare_path_mapping_change(operation: str, server: str, local_path: str, *,
                                server_path: str | None = None, new_local_path: str | None = None) -> dict[str, Any]:
    """以服务器和自然路径键定位，不依赖列表顺序；快照只含路径事实和文件摘要。"""
    with config.runtime_config_read_guard():
        raw, before = _mapping_state(server)
        modified = [dict(item) for item in before]
        if operation == "create":
            if server_path is None:
                raise MediaServerPathMappingError("新增映射必须提供服务器路径")
            modified.append({"local": local_path, "server": server_path})
        elif operation in {"update", "delete"}:
            key = media_server_path_key(local_path)
            selected = next((item for item in modified if media_server_path_key(item["local"]) == key), None)
            if selected is None:
                raise MediaServerPathMappingError("路径映射不存在")
            if operation == "delete":
                modified.remove(selected)
            else:
                if server_path is None and new_local_path is None:
                    raise MediaServerPathMappingError("至少提供一个需要修改的映射路径")
                if new_local_path is not None:
                    selected["local"] = new_local_path
                if server_path is not None:
                    selected["server"] = server_path
        else:
            raise MediaServerPathMappingError("路径映射操作无效")
        updates = mapping_updates({server: modified})
        encoded = updates[MAPPING_CONFIG[server][0]]
        snapshot = {"operation": operation, "server": server, "before": before,
                    "after": json.loads(encoded or "[]"), "file_sha256": hashlib.sha256(raw or b"").hexdigest()}
        fingerprint = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return {**snapshot, "fingerprint": fingerprint}


def apply_path_mapping_change(operation: str, server: str, local_path: str, *, expected_fingerprint: str,
                              server_path: str | None = None, new_local_path: str | None = None,
                              check_cancelled: Callable[[], None] | None = None) -> dict[str, Any]:
    """在共享锁中重算确认事实，并交给原配置发布器做文件 CAS 与运行态发布。"""
    with mapping_write_guard():
        raw, _ = config.read_env_snapshot(config.ENV_FILE)
        try:
            prepared = prepare_path_mapping_change(operation, server, local_path,
                server_path=server_path, new_local_path=new_local_path)
        except ValueError as exc:
            raise MappingSnapshotChanged("路径映射已变化，请重新预检") from exc
        if (prepared["fingerprint"] != expected_fingerprint
                or prepared["file_sha256"] != hashlib.sha256(raw or b"").hexdigest()):
            raise MappingSnapshotChanged("路径映射配置已变化，请重新预检")
        updates = mapping_updates({server: prepared["after"]})
        if check_cancelled:
            check_cancelled()
        try:
            config.update_runtime_env_file(config.ENV_FILE, updates, expected=raw)
        except (config.ConcurrentConfigUpdateError, config.ExternalConfigOverrideError) as exc:
            raise MappingSnapshotChanged("路径映射被其它设置更新，请重新预检") from exc
        current = list_path_mappings(server)
        ok = current["items"] == prepared["after"]
        return {"ok": ok, "status": "completed" if ok else "outcome_unknown",
                "summary": "媒体库路径映射已保存并回读核对" if ok else "路径映射保存结果待核验",
                "data": {**current, "operation": operation, "verified": ok}}


def apply_media_server_path_mapping(
    raw_path: str,
    mappings: Iterable[MediaServerPathMapping],
) -> tuple[str, MediaServerPathMapping | None]:
    """按最长本地前缀映射路径；无匹配时返回规范化原路径。"""
    normalized = normalize_media_server_path(raw_path)
    for mapping in sorted(tuple(mappings), key=lambda item: len(item.local_prefix), reverse=True):
        if mapping.matches(normalized):
            return mapping.apply(normalized), mapping
    return normalized, None


def reverse_media_server_path_mapping(
    raw_path: str,
    mappings: Iterable[MediaServerPathMapping],
) -> tuple[str, MediaServerPathMapping]:
    """按最长服务器前缀反查；缺失或同前缀歧义必须拒绝，不能原样降级。"""
    candidates = []
    for mapping in mappings:
        reverse = MediaServerPathMapping(mapping.server_prefix, mapping.local_prefix)
        if reverse.matches(raw_path):
            candidates.append((mapping, reverse.apply(raw_path)))
    if not candidates:
        raise MediaServerPathMappingError("节目路径未命中媒体服务器映射")
    longest = max(len(mapping.server_prefix) for mapping, _ in candidates)
    selected = [(mapping, path) for mapping, path in candidates if len(mapping.server_prefix) == longest]
    if len({path for _, path in selected}) != 1:
        raise MediaServerPathMappingError("节目路径命中多个 MF 目录，无法唯一定位")
    mapping, path = selected[0]
    return path, mapping


class MediaServerRefreshOptions(TypedDict):
    path_mappings: tuple[MediaServerPathMapping, ...]
    allow_global_refresh_fallback: bool


def configured_media_server_refresh_options(server_type: str) -> MediaServerRefreshOptions:
    """读取指定媒体服务器的路径映射和全局刷新降级策略。"""
    normalized = (server_type or "").strip().lower()
    try:
        mapping_key, fallback_key = MAPPING_CONFIG[normalized]
    except KeyError as exc:
        raise ValueError("媒体服务器类型无效") from exc
    try:
        mappings = parse_media_server_path_mappings(config.get(mapping_key, ""))
    except MediaServerPathMappingError as exc:
        # 配置损坏时 fail closed：不带映射继续匹配，并保持禁止全局刷新。
        logger.error("%s 路径映射配置无效，已安全忽略: %s", normalized, exc)
        mappings = ()
        allow_global_refresh_fallback = False
    else:
        allow_global_refresh_fallback = config.get_bool(fallback_key, False)
    return {
        "path_mappings": mappings,
        "allow_global_refresh_fallback": allow_global_refresh_fallback,
    }
