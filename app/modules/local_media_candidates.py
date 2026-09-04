"""本地媒体来源中独立媒体条目的只读发现与可恢复删除。"""
from __future__ import annotations

import os
import stat as stat_module
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.modules.local_path_mapping import (
    LEGACY_SOURCE_PATH_ERROR,
    PathMappingError,
    assert_within,
    require_container_absolute_path,
)
from app.modules.local_media_cleanup import is_probable_sample_video
from app.modules.local_storage import (
    LocalFilesystemAdapter,
    LocalStorageError,
    is_ignored_local_media_directory,
    move_entry_no_replace_at,
)

LOCAL_MEDIA_TRASH_DIR = ".mediaflux-trash"

_CANDIDATE_DISCOVERY_DEPTH_LIMIT = 16
_DISCOVERY_INCOMPLETE_PREFIX = "目录扫描不完整："


def _append_discovery_error(errors: list[str], value: object) -> None:
    if isinstance(value, OSError):
        message = f"条目暂时不可读取（{type(value).__name__}）"
    else:
        message = str(value or "").strip() or "部分条目暂时不可读取"
    if message.startswith(_DISCOVERY_INCOMPLETE_PREFIX):
        message = message[len(_DISCOVERY_INCOMPLETE_PREFIX):].strip()
    if message and message not in errors:
        errors.append(message[:240])


def _discovery_error(errors: list[str]) -> str:
    if not errors:
        return ""
    return _DISCOVERY_INCOMPLETE_PREFIX + "；".join(errors[:3])


def _source_root(source) -> Path:
    root = require_container_absolute_path(source.local_root, label="来源目录")
    return assert_within(root, root)


def discover_local_media_candidates(source) -> tuple[list[Path], str]:
    """发现来源内可独立整理的视频单元，避免单集异常阻塞同目录其他媒体。"""
    candidates, error, root = discover_local_media_directory_candidates(source)
    if root is None:
        return [], error

    adapter = LocalFilesystemAdapter(root)
    expanded: list[Path] = []
    visited: set[tuple[int, int]] = set()
    errors: list[str] = []
    if error:
        _append_discovery_error(errors, error)
    for candidate in candidates:
        expanded.extend(
            _expand_media_candidate(
                candidate,
                adapter=adapter,
                depth=0,
                visited=visited,
                errors=errors,
            )
        )

    unique = {str(candidate): candidate for candidate in expanded}
    sample_flags: dict[str, bool] = {}
    parents_with_primary: set[Path] = set()
    for candidate in unique.values():
        try:
            is_sample = is_probable_sample_video(candidate, candidate.lstat().st_size)
        except OSError:
            is_sample = False
        sample_flags[str(candidate)] = is_sample
        if not is_sample:
            parents_with_primary.add(candidate.parent)
    filtered = [
        candidate for candidate in unique.values()
        if not (
            sample_flags.get(str(candidate), False)
            and candidate.parent in parents_with_primary
        )
    ]
    return sorted(
        filtered,
        key=lambda item: item.relative_to(root).as_posix().casefold(),
    ), _discovery_error(errors)


def _direct_media_entries(
    directory: Path,
    *,
    adapter: LocalFilesystemAdapter,
    errors: list[str],
) -> tuple[list[Path], list[Path]]:
    """返回目录中的直接视频与包含视频的直接子目录。"""
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise LocalStorageError(f"目录暂时不可读取: {directory.name}") from exc

    videos: list[Path] = []
    directories: list[Path] = []
    for candidate in entries:
        if is_ignored_local_media_directory(candidate.name):
            continue
        try:
            info = candidate.lstat()
            if stat_module.S_ISLNK(info.st_mode):
                continue
            if stat_module.S_ISDIR(info.st_mode):
                if adapter.contains_video(candidate):
                    directories.append(candidate)
            elif stat_module.S_ISREG(info.st_mode) and adapter.contains_video(candidate):
                videos.append(candidate)
        except (LocalStorageError, OSError) as exc:
            _append_discovery_error(errors, exc)
            continue
    return videos, directories


def _expand_media_candidate(
    candidate: Path,
    *,
    adapter: LocalFilesystemAdapter,
    depth: int,
    visited: set[tuple[int, int]],
    errors: list[str],
) -> list[Path]:
    """把纯容器目录展开为互不阻塞的最小稳定媒体单元。"""
    try:
        info = candidate.lstat()
    except OSError as exc:
        _append_discovery_error(errors, exc)
        return []
    if stat_module.S_ISLNK(info.st_mode):
        return []
    if stat_module.S_ISREG(info.st_mode):
        try:
            return [candidate] if adapter.contains_video(candidate) else []
        except (LocalStorageError, OSError) as exc:
            _append_discovery_error(errors, exc)
            return []
    if not stat_module.S_ISDIR(info.st_mode):
        return []

    identity = (int(info.st_dev), int(info.st_ino))
    if identity in visited:
        return []
    visited.add(identity)
    if depth >= _CANDIDATE_DISCOVERY_DEPTH_LIMIT:
        try:
            return [item.path for item in adapter.scan(candidate) if item.role == "video"]
        except (LocalStorageError, OSError) as exc:
            _append_discovery_error(errors, exc)
            return [candidate]

    try:
        direct_videos, media_directories = _direct_media_entries(
            candidate,
            adapter=adapter,
            errors=errors,
        )
    except LocalStorageError as exc:
        _append_discovery_error(errors, exc)
        # 上层发现已确认该目录包含视频；瞬时读取失败时保留原有整体候选语义，
        # 不应因为进一步拆分失败而把媒体静默漏掉。
        return [candidate]

    # 扫描任务以主视频为边界：无论外层是作品目录、Season 目录还是分类
    # 目录，都继续展开到具体视频。这样待确认只锁住问题单集，其他视频可
    # 独立完成；作品名、年份和 TMDB 标记仍可从 relative_path 的父目录继承。
    expanded: list[Path] = list(direct_videos)
    for child in media_directories:
        expanded.extend(
            _expand_media_candidate(
                child,
                adapter=adapter,
                depth=depth + 1,
                visited=visited,
                errors=errors,
            )
        )
    return expanded


def discover_local_media_directory_candidates(
    source, directory: Path | str | None = None,
) -> tuple[list[Path], str, Path | None]:
    """读取来源内指定目录的直接媒体子项，供登录后的本地条目浏览使用。"""
    try:
        root = _source_root(source)
        selected_input = (
            require_container_absolute_path(directory, label="目录路径")
            if directory else root
        )
        selected = assert_within(selected_input, root)
        relative_parts = selected.relative_to(root).parts
        if any(is_ignored_local_media_directory(part) for part in relative_parts):
            raise PathMappingError("禁止浏览系统目录、临时目录或 MediaFlux 回收区")
    except PathMappingError as exc:
        message = str(exc)
        if message == LEGACY_SOURCE_PATH_ERROR or "Docker 容器内绝对路径" in message:
            return [], message, None
        return [], "目录路径不安全", None
    if selected.is_symlink() or not selected.is_dir():
        return [], "目录不存在或不可访问", None
    try:
        entries = sorted(selected.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return [], "目录读取失败", None
    adapter = LocalFilesystemAdapter(root)
    candidates: list[Path] = []
    errors: list[str] = []
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
        except (LocalStorageError, OSError) as exc:
            _append_discovery_error(errors, exc)
            continue
    return candidates, _discovery_error(errors), selected


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
            # 文件身份是前端回传的透明快照。mtime_ns 与部分 NAS 文件系统的
            # inode/device 会超过 JavaScript Number 的安全整数范围，必须使用
            # 十进制字符串传输，避免浏览器 JSON 往返后被舍入并误报条目变化。
            "size": str(int(info.st_size)),
            "mtime_ns": str(int(info.st_mtime_ns)),
            "device": str(int(info.st_dev)),
            "inode": str(int(info.st_ino)),
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
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination_name = f"{timestamp}-{uuid.uuid4().hex[:10]}-{selected.name}"
    destination = trash_root / destination_name
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    root_fd: int | None = None
    trash_fd: int | None = None
    moved = False
    try:
        root_fd = os.open(root, directory_flags)
        fresh = os.stat(selected.name, dir_fd=root_fd, follow_symlinks=False)
        if stat_module.S_ISLNK(fresh.st_mode):
            raise PathMappingError("禁止删除符号链接")
        fresh_identity = {
            "size": int(fresh.st_size),
            "mtime_ns": int(fresh.st_mtime_ns),
            "device": int(fresh.st_dev),
            "inode": int(fresh.st_ino),
        }
        if fresh_identity != normalized_expected:
            raise PathMappingError("条目在删除前发生变化，请刷新后重试")
        try:
            os.mkdir(LOCAL_MEDIA_TRASH_DIR, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        trash_fd = os.open(LOCAL_MEDIA_TRASH_DIR, directory_flags, dir_fd=root_fd)
        os.replace(
            selected.name,
            destination_name,
            src_dir_fd=root_fd,
            dst_dir_fd=trash_fd,
        )
        moved = True
        published = os.stat(destination_name, dir_fd=trash_fd, follow_symlinks=False)
        published_identity = {
            "size": int(published.st_size),
            "mtime_ns": int(published.st_mtime_ns),
            "device": int(published.st_dev),
            "inode": int(published.st_ino),
        }
        if published_identity != normalized_expected:
            move_entry_no_replace_at(
                destination_name,
                selected.name,
                source_dir_fd=trash_fd,
                target_dir_fd=root_fd,
                is_directory=is_directory,
            )
            moved = False
            raise PathMappingError("条目移动到回收区时发生变化，已恢复原位置")
        return destination
    except (NotImplementedError, TypeError) as exc:
        raise PathMappingError("当前运行环境不支持安全回收操作") from exc
    except Exception:
        if moved and root_fd is not None and trash_fd is not None:
            try:
                move_entry_no_replace_at(
                    destination_name,
                    selected.name,
                    source_dir_fd=trash_fd,
                    target_dir_fd=root_fd,
                    is_directory=is_directory,
                )
            except Exception:
                pass
        raise
    finally:
        if trash_fd is not None:
            os.close(trash_fd)
        if root_fd is not None:
            os.close(root_fd)
