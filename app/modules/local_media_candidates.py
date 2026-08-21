"""本地媒体来源一级条目的只读发现与可恢复删除。"""
from __future__ import annotations

import os
import stat as stat_module
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.modules.local_path_mapping import PathMappingError, assert_within
from app.modules.local_storage import (
    LocalFilesystemAdapter,
    LocalStorageError,
    is_ignored_local_media_directory,
)

LOCAL_MEDIA_TRASH_DIR = ".mediaflux-trash"


def _source_root(source) -> Path:
    root = Path(source.local_root).expanduser().absolute()
    return assert_within(root, root)


def discover_local_media_candidates(source) -> tuple[list[Path], str]:
    """读取来源根目录的一级媒体候选，返回公开错误而不抛出路径细节。"""
    candidates, error, _ = discover_local_media_directory_candidates(source)
    return candidates, error


def discover_local_media_directory_candidates(
    source, directory: Path | str | None = None,
) -> tuple[list[Path], str, Path | None]:
    """读取来源内指定目录的直接媒体子项，供登录后的本地条目浏览使用。"""
    from app.modules.windows_smb import ensure_smb_connection, parse_unc_share_root

    if parse_unc_share_root(source.local_root):
        ok, err = ensure_smb_connection(
            source.local_root,
            getattr(source, "smb_user", ""),
            getattr(source, "smb_pass", ""),
        )
        if not ok:
            return [], str(err or "SMB 来源连接失败"), None
    try:
        root = _source_root(source)
        selected = assert_within(Path(directory) if directory else root, root)
        relative_parts = selected.relative_to(root).parts
        if any(is_ignored_local_media_directory(part) for part in relative_parts):
            raise PathMappingError("禁止浏览系统目录、临时目录或 MediaFlux 回收区")
    except PathMappingError:
        return [], "目录路径不安全", None
    if selected.is_symlink() or not selected.is_dir():
        return [], "目录不存在或不可访问", None
    try:
        entries = sorted(selected.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return [], "目录读取失败", None
    adapter = LocalFilesystemAdapter(root)
    candidates: list[Path] = []
    for candidate in entries:
        if is_ignored_local_media_directory(candidate.name):
            continue
        try:
            info = candidate.lstat()
            if stat_module.S_ISLNK(info.st_mode):
                continue
            if stat_module.S_ISDIR(info.st_mode):
                if adapter.contains_video(candidate):
                    candidates.append(candidate)
            elif (
                stat_module.S_ISREG(info.st_mode)
                and adapter.contains_video(candidate)
            ):
                candidates.append(candidate)
        except (LocalStorageError, OSError):
            continue
    return candidates, "", selected


def candidate_payload(
    source, candidate: Path, *, organize_ready: bool, allow_nested: bool = False,
) -> dict[str, Any]:
    """生成供已登录前端使用的条目快照。"""
    root = _source_root(source)
    selected = assert_within(Path(candidate), root)
    relative_parts = selected.relative_to(root).parts
    if any(is_ignored_local_media_directory(part) for part in relative_parts):
        raise PathMappingError("禁止读取系统目录、临时目录或 MediaFlux 回收区")
    if not allow_nested and selected.parent != root:
        raise PathMappingError("仅允许读取来源根目录下的一级条目")
    info = selected.lstat()
    if stat_module.S_ISLNK(info.st_mode):
        raise PathMappingError("禁止读取符号链接")
    if stat_module.S_ISDIR(info.st_mode):
        kind = "directory"
    elif stat_module.S_ISREG(info.st_mode) and LocalFilesystemAdapter.role_for(selected) == "video":
        kind = "video"
    else:
        raise PathMappingError("条目不是可整理的媒体内容")
    return {
        "source_id": int(source.id),
        "source_name": str(source.name),
        "name": selected.name,
        "path": str(selected),
        "relative_path": selected.relative_to(root).as_posix(),
        "kind": kind,
        "deletable": selected.parent == root,
        "size": int(info.st_size) if kind == "video" else None,
        "modified_at": datetime.fromtimestamp(info.st_mtime, tz=timezone.utc).isoformat(),
        "organize_ready": bool(organize_ready),
        "identity": {
            "size": int(info.st_size),
            "mtime_ns": int(info.st_mtime_ns),
            "device": int(info.st_dev),
            "inode": int(info.st_ino),
        },
    }


def move_candidate_to_trash(source, path: Path | str, expected_identity: dict[str, Any]) -> Path:
    """把来源根目录一级条目原子移动到应用回收区，避免直接永久删除。"""
    root = _source_root(source)
    selected = assert_within(Path(path), root)
    if selected == root or selected.parent != root:
        raise PathMappingError("仅允许删除来源根目录下的一级媒体条目")
    if is_ignored_local_media_directory(selected.name):
        raise PathMappingError("禁止操作系统目录、临时目录或 MediaFlux 回收区")

    try:
        info = selected.lstat()
    except FileNotFoundError as exc:
        raise FileNotFoundError("本地媒体条目不存在，请刷新后重试") from exc
    if stat_module.S_ISLNK(info.st_mode):
        raise PathMappingError("禁止删除符号链接")
    is_directory = stat_module.S_ISDIR(info.st_mode)
    is_video = stat_module.S_ISREG(info.st_mode) and LocalFilesystemAdapter.role_for(selected) == "video"
    if not (is_directory or is_video):
        raise PathMappingError("条目不是可删除的媒体内容")

    current_identity = {
        "size": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
    }
    normalized_expected: dict[str, int] = {}
    for key in current_identity:
        value = expected_identity.get(key) if isinstance(expected_identity, dict) else None
        if isinstance(value, bool):
            raise PathMappingError("条目快照无效，请刷新后重试")
        try:
            normalized_expected[key] = int(value)
        except (TypeError, ValueError) as exc:
            raise PathMappingError("条目快照无效，请刷新后重试") from exc
    if normalized_expected != current_identity:
        raise PathMappingError("条目在读取后发生变化，请刷新后重试")

    trash_root = root / LOCAL_MEDIA_TRASH_DIR
    if trash_root.exists() and (trash_root.is_symlink() or not trash_root.is_dir()):
        raise PathMappingError("MediaFlux 回收区路径不安全")
    trash_root.mkdir(mode=0o700, exist_ok=True)
    assert_within(trash_root, root)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = trash_root / f"{timestamp}-{uuid.uuid4().hex[:10]}-{selected.name}"
    os.replace(selected, destination)
    return destination
