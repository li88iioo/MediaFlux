"""受限的 Docker 本地目录浏览器，仅暴露目录名称与规范化路径。"""
from __future__ import annotations

import os
import re
from pathlib import Path

from app.config import get
from app.modules.local_path_mapping import (
    PathMappingError, assert_within, is_windows_or_unc_path,
)

VIRTUAL_ROOT = "__roots__"


def _platform_roots() -> list[Path]:
    """Docker 运行时只浏览容器内的 POSIX 文件系统。"""
    return [Path("/")]


def _configured_roots() -> list[Path]:
    raw = str(get("LOCAL_MEDIA_BROWSE_ROOTS", "") or "").strip()
    if raw:
        parts = [item.strip() for item in re.split(r"[\n,;]+", raw) if item.strip()]
        roots = [Path(item).expanduser() for item in parts]
    else:
        roots = _platform_roots()

    normalized: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            if is_windows_or_unc_path(str(root)) or not root.is_absolute():
                continue
            resolved = assert_within(root, root)
            if not resolved.is_dir():
                continue
        except (OSError, PathMappingError):
            continue
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        normalized.append(resolved)
    return normalized


def _display_name(path: Path) -> str:
    return path.name or str(path)


def _allowed_root(path: Path, roots: list[Path]) -> Path:
    for root in roots:
        try:
            return assert_within(path, root)
        except PathMappingError:
            continue
    raise PathMappingError("路径超出允许浏览范围")


def _directory_access_error(action: str, exc: OSError) -> ValueError:
    detail = str(exc).strip()
    suffix = f"：{detail}" if detail else ""
    return ValueError(f"{action}失败，请确认 Docker 卷挂载与容器目录权限{suffix}")


def browse_local_directories(
    path: str = "",
    *,
    allowed_root: Path | None = None,
    source_id: int = 0,
    owner: str = "admin",
) -> dict:
    """列出一级子目录；指定 allowed_root 时禁止离开该来源根目录。"""
    if source_id:
        from app import database as db

        source = db.get_local_media_source(source_id, owner=owner)
        if source and allowed_root is None:
            allowed_root = Path(source.local_root)

    roots = [assert_within(allowed_root, allowed_root)] if allowed_root is not None else _configured_roots()
    if not roots:
        raise PathMappingError("没有可浏览的本地目录，请检查 Docker 卷挂载和 LOCAL_MEDIA_BROWSE_ROOTS")

    requested = str(path or "").strip()
    if not requested or requested == VIRTUAL_ROOT:
        if allowed_root is not None:
            current = roots[0]
        else:
            return {
                "current": VIRTUAL_ROOT,
                "parent": "",
                "directories": [
                    {"id": str(root), "name": _display_name(root), "path": str(root)}
                    for root in roots
                ],
            }
    else:
        if is_windows_or_unc_path(requested):
            raise PathMappingError(
                "Docker 版本不直接访问 Windows/UNC 路径；请先在宿主机挂载目录并映射为容器路径"
            )
        candidate = Path(requested).expanduser()
        if not candidate.is_absolute():
            raise PathMappingError("目录路径必须是容器内绝对路径")
        current = _allowed_root(candidate, roots)

    try:
        if not current.exists():
            raise FileNotFoundError("目录不存在，请检查 Docker 卷挂载")
        if not current.is_dir():
            raise ValueError("所选路径不是目录")
        if current.is_symlink():
            raise PathMappingError("不允许浏览符号链接目录")
        children = sorted(current.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise _directory_access_error("目录读取", exc) from exc

    directories: list[dict[str, str]] = []
    for child in children:
        try:
            if child.is_symlink() or not child.is_dir():
                continue
            safe_child = _allowed_root(child, roots)
        except (OSError, PathMappingError):
            continue
        directories.append({"id": str(safe_child), "name": child.name, "path": str(safe_child)})

    root = next(root for root in roots if current == root or root in current.parents)
    parent = ""
    if current != root:
        parent = str(current.parent)
    elif allowed_root is None:
        parent = VIRTUAL_ROOT
    return {"current": str(current), "parent": parent, "directories": directories}
