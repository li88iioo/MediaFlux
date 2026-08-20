"""本地媒体来源一级条目的只读发现与可恢复删除。"""
from __future__ import annotations

import os
import stat as stat_module
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.modules.local_path_mapping import PathMappingError, assert_within
from app.modules.local_storage import LocalFilesystemAdapter

LOCAL_MEDIA_TRASH_DIR = ".mediaflux-trash"


def _source_root(source) -> Path:
    root = Path(source.local_root).expanduser().absolute()
    return assert_within(root, root)


def discover_local_media_candidates(source) -> tuple[list[Path], str]:
    """读取来源根目录的一级媒体候选，返回公开错误而不抛出路径细节。"""
    from app.modules.windows_smb import ensure_smb_connection, parse_unc_share_root

    if parse_unc_share_root(source.local_root):
        ok, err = ensure_smb_connection(
            source.local_root,
            getattr(source, "smb_user", ""),
            getattr(source, "smb_pass", ""),
        )
        if not ok:
            return [], str(err or "SMB 来源连接失败")
    try:
        root = _source_root(source)
    except PathMappingError:
        return [], "来源路径不安全"
    if not root.is_dir():
        return [], "来源目录不存在或不可访问"
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return [], "来源目录读取失败"
    candidates: list[Path] = []
    for candidate in entries:
        if candidate.name == LOCAL_MEDIA_TRASH_DIR:
            continue
        try:
            info = candidate.lstat()
            if stat_module.S_ISLNK(info.st_mode):
                continue
            if stat_module.S_ISDIR(info.st_mode) or (
                stat_module.S_ISREG(info.st_mode)
                and LocalFilesystemAdapter.role_for(candidate) == "video"
            ):
                candidates.append(candidate)
        except OSError:
            continue
    return candidates, ""


def candidate_payload(source, candidate: Path, *, organize_ready: bool) -> dict[str, Any]:
    """生成供已登录前端使用的条目快照。"""
    root = _source_root(source)
    selected = assert_within(Path(candidate), root)
    if selected.parent != root:
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
        "kind": kind,
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
    if selected.name == LOCAL_MEDIA_TRASH_DIR:
        raise PathMappingError("禁止操作 MediaFlux 回收区")

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
