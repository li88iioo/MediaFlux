"""qB 路径到本机路径的跨平台安全映射。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Iterable


class PathMappingError(ValueError):
    """路径无法安全映射。"""


_DRIVE_RE = re.compile(r"^[A-Za-z]:/")


def normalize_qb_path(raw_path: str) -> str:
    """将 POSIX、Windows 和 UNC 路径归一为仅用于比较的斜杠形式。"""
    value = str(raw_path or "").strip()
    if not value:
        raise PathMappingError("路径不能为空")
    if "\x00" in value:
        raise PathMappingError("路径包含 NUL 字符")
    value = value.replace("\\", "/")
    is_unc = value.startswith("//")
    if is_unc:
        value = "//" + re.sub(r"/+", "/", value[2:])
    else:
        value = re.sub(r"/+", "/", value)
    parts: list[str] = []
    prefix = ""
    remainder = value
    if is_unc:
        prefix, remainder = "//", value[2:]
    elif _DRIVE_RE.match(value):
        prefix, remainder = value[:3], value[3:]
    elif value.startswith("/"):
        prefix, remainder = "/", value[1:]
    for part in remainder.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            raise PathMappingError("路径不允许包含 ..")
        parts.append(part)
    if prefix == "//":
        if len(parts) < 2:
            raise PathMappingError("UNC 路径必须包含服务器和共享名")
        result = "//" + "/".join(parts)
    elif prefix:
        result = prefix + "/".join(parts)
    else:
        result = "/".join(parts)
    if result not in {"/", "//"}:
        result = result.rstrip("/")
    return result


def _windows_style(value: str) -> bool:
    return value.startswith("//") or bool(_DRIVE_RE.match(value))


@dataclass(frozen=True)
class PathMapping:
    qb_prefix: str
    local_root: Path

    def __post_init__(self) -> None:
        normalized = normalize_qb_path(self.qb_prefix)
        if normalized in {"", "/", "//"}:
            raise PathMappingError("qB 路径前缀不能是文件系统根目录")
        object.__setattr__(self, "local_root", Path(self.local_root).expanduser())

    @property
    def normalized_prefix(self) -> str:
        return normalize_qb_path(self.qb_prefix)

    @property
    def comparison_prefix(self) -> str:
        value = self.normalized_prefix
        return value.casefold() if _windows_style(value) else value

    def matches(self, value: str) -> bool:
        normalized = normalize_qb_path(value)
        candidate = normalized.casefold() if _windows_style(self.normalized_prefix) else normalized
        prefix = self.comparison_prefix.rstrip("/")
        return candidate == prefix or candidate.startswith(prefix + "/")

    def relative_parts(self, value: str) -> tuple[str, ...]:
        normalized = normalize_qb_path(value)
        if not self.matches(normalized):
            raise PathMappingError("路径不匹配 qB 前缀")
        prefix = self.normalized_prefix.rstrip("/")
        suffix = normalized[len(prefix):].lstrip("/")
        parts = tuple(part for part in suffix.split("/") if part)
        if any(part in {".", ".."} for part in parts):
            raise PathMappingError("路径包含非法相对段")
        return parts


class PathMappingSet:
    def __init__(self, items: Iterable[PathMapping]):
        self.items = tuple(items)
        if not self.items:
            raise PathMappingError("至少需要一条路径映射")
        seen: set[str] = set()
        for item in self.items:
            key = item.comparison_prefix
            if key in seen:
                raise PathMappingError("存在重复或大小写歧义的 qB 路径前缀")
            seen.add(key)

    def map_qb_path(self, raw_path: str) -> Path:
        normalized = normalize_qb_path(raw_path)
        matches = [item for item in self.items if item.matches(normalized)]
        if not matches:
            raise PathMappingError("qB 路径没有匹配的本地来源")
        selected = max(matches, key=lambda item: len(item.comparison_prefix))
        candidate = selected.local_root.joinpath(*selected.relative_parts(normalized))
        return assert_within(candidate, selected.local_root)


def _reject_unsafe_parts(path: PurePath) -> None:
    if any(part == ".." for part in path.parts):
        raise PathMappingError("路径不允许包含 ..")
    if "\x00" in str(path):
        raise PathMappingError("路径包含 NUL 字符")


def _is_symlink_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        if hasattr(path, "is_junction") and path.is_junction():
            return True
    except Exception:
        pass
    return False


def assert_within(path: Path, root: Path, *, reject_symlinks: bool = True) -> Path:
    """解析并确认 path 位于 root 内；默认拒绝路径链上的符号链接。"""
    raw_path = Path(path).expanduser()
    raw_root = Path(root).expanduser()
    _reject_unsafe_parts(raw_path)
    _reject_unsafe_parts(raw_root)
    resolved_root = raw_root.resolve(strict=False)
    resolved_path = raw_path.resolve(strict=False)
    try:
        relative = resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise PathMappingError("路径超出允许根目录") from exc
    if reject_symlinks:
        current = resolved_root
        if raw_root.exists() and _is_symlink_or_junction(raw_root):
            raise PathMappingError("允许根目录不能是符号链接")
        # 从声明根目录逐段检查，防止已存在的中间段经 symlink 逃逸或别名访问。
        declared_current = raw_root.resolve(strict=False)
        for part in relative.parts:
            declared_current = declared_current / part
            if declared_current.exists() and _is_symlink_or_junction(declared_current):
                raise PathMappingError("路径链包含符号链接")
            current = current / part
    return resolved_path


def validate_source_target_roots(source_root: Path, target_roots: Iterable[Path]) -> None:
    """拒绝符号链接根、来源与目标相同或互相递归包含。"""
    declared_source = Path(source_root).expanduser()
    source = assert_within(declared_source, declared_source)
    for target_root in target_roots:
        declared_target = Path(target_root).expanduser()
        target = assert_within(declared_target, declared_target)
        if source == target:
            raise PathMappingError("来源目录与媒体库目标不能相同")
        if source in target.parents or target in source.parents:
            raise PathMappingError("来源目录与媒体库目标不能递归包含")
