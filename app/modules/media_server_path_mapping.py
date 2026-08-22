"""媒体服务器可见路径映射。

MediaFlux 运行在容器/Linux 路径空间中，而 Jellyfin/Emby 可能通过另一套
POSIX、Windows 盘符或 UNC 路径访问同一目录。本模块提供显式、最长前缀优先
的单向映射，避免路径失配时误降级为全局媒体库扫描。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable

from app import config
from app.logger import get_logger
from app.modules.local_path_mapping import normalize_qb_path

logger = get_logger(__name__)

_DRIVE_RE = re.compile(r"^[A-Za-z]:/")
_CONFIG_KEYS = {
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


def _windows_style(value: str) -> bool:
    return value.startswith("//") or bool(_DRIVE_RE.match(value))


def _anchor_and_parts(value: str) -> tuple[str, tuple[str, ...]]:
    if value.startswith("//"):
        anchor, remainder = "//", value[2:]
    elif _DRIVE_RE.match(value):
        anchor, remainder = value[:3], value[3:]
    elif value.startswith("/"):
        anchor, remainder = "/", value[1:]
    else:
        anchor, remainder = "", value
    return anchor, tuple(part for part in remainder.split("/") if part)


def _comparison_key(value: str) -> str:
    return value.casefold() if _windows_style(value) else value


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
        prefix_anchor, prefix_parts = _anchor_and_parts(self.local_prefix)
        path_anchor, path_parts = _anchor_and_parts(path)
        normalize = str.casefold if _windows_style(self.local_prefix) else (lambda value: value)
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


def configured_media_server_refresh_options(server_type: str) -> dict[str, object]:
    """读取指定媒体服务器的路径映射和全局刷新降级策略。"""
    normalized = str(server_type or "").strip().lower()
    try:
        mapping_key, fallback_key = _CONFIG_KEYS[normalized]
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
