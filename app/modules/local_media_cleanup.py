"""本地媒体归档成功后的明确垃圾直删规则。

只删除能够可靠识别的垃圾；未知文件始终保留。调用方必须在媒体目标校验成功，
并且（若来自 qB）qB 任务已用 delete_files=False 移除后才执行。
"""
from __future__ import annotations

import errno
import os
import re
import stat as stat_module
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from app.modules.local_path_mapping import PathMappingError, assert_within
from app.modules.local_storage import (
    LocalFileSnapshot,
    LocalFilesystemAdapter,
    LocalScanLimitExceeded,
    is_ignored_local_media_directory,
    move_entry_no_replace_at,
)

_CLEANUP_QUARANTINE_DIR = ".mediaflux-trash"


@dataclass(frozen=True)
class CleanupCandidate:
    snapshot: LocalFileSnapshot
    reason_code: str
    reason: str


@dataclass
class CleanupResult:
    deleted: list[str] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)
    removed_dirs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_DIRECT_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
_DIRECT_SUFFIXES = {".url", ".website", ".part", ".partial", ".tmp", ".temp", ".aria2", ".!qb"}
_AD_TEXT_RE = re.compile(
    r"(?i)(readme|广告|说明|声明|最新网址|下载必看|本站|发布页|公众号|微信|高清影视之家)"
)
_SAMPLE_RE = re.compile(r"(?i)(?:^|[._\-\s])(sample|proof)(?:$|[._\-\s])")


def is_probable_sample_video(
    path: Path,
    size: int,
    *,
    sample_max_bytes: int = 300 * 1024 * 1024,
) -> bool:
    """判断文件是否为需保留、但不应自动拆成独立任务的 sample/proof。"""
    return bool(
        int(size or 0) <= max(1, int(sample_max_bytes))
        and _SAMPLE_RE.search(Path(path).stem)
    )


def classify_cleanup_items(
    snapshots: Iterable[LocalFileSnapshot],
    *,
    primary_video_count: int = 0,
    sample_max_bytes: int = 300 * 1024 * 1024,
) -> tuple[list[CleanupCandidate], list[LocalFileSnapshot]]:
    """返回（确定垃圾、保留文件），未知文件永远进入保留列表。"""
    del primary_video_count  # 兼容旧调用；保守清理不根据视频数量扩大删除范围。
    cleanup: list[CleanupCandidate] = []
    retained: list[LocalFileSnapshot] = []
    for item in snapshots:
        name = item.path.name
        lower = name.casefold()
        suffix = item.path.suffix.casefold()
        candidate: CleanupCandidate | None = None
        if item.size == 0:
            candidate = CleanupCandidate(item, "empty", "0 字节空文件")
        elif lower in _DIRECT_NAMES:
            candidate = CleanupCandidate(item, "system-junk", "系统生成的无用文件")
        elif any(lower.endswith(value) for value in _DIRECT_SUFFIXES):
            candidate = CleanupCandidate(item, "temporary", "下载器或浏览器临时残留")
        elif suffix in {".url", ".website"}:
            candidate = CleanupCandidate(item, "shortcut", "下载站网址快捷方式")
        elif suffix in {".txt", ".html", ".htm"} and _AD_TEXT_RE.search(item.path.stem):
            candidate = CleanupCandidate(item, "site-note", "下载站说明或广告文件")
        if candidate is None:
            retained.append(item)
        else:
            cleanup.append(candidate)
    return cleanup, retained


def delete_cleanup_items(
    candidates: Iterable[CleanupCandidate],
    *,
    allowed_root: Path,
    selected_path: Path | None = None,
    remove_empty_dirs: bool = True,
) -> CleanupResult:
    """复核快照后隔离并删除确定垃圾；源与回收区都固定到目录句柄。"""
    root = Path(allowed_root).expanduser().resolve(strict=False)
    adapter = LocalFilesystemAdapter(root)
    result = CleanupResult()
    parent_dirs: set[Path] = set()
    quarantine_root = root / _CLEANUP_QUARANTINE_DIR
    quarantine_dir_fd: int | None = None
    quarantine_run_fd: int | None = None
    quarantine_run_name = f"cleanup-{uuid.uuid4().hex}"
    quarantine_display_root = quarantine_root / quarantine_run_name
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    root_fd: int | None = None

    def open_source_parent(relative_parent: Path) -> int:
        """从已固定根目录逐段打开父目录，拒绝中途符号链接替换。"""
        if root_fd is None:
            raise RuntimeError("本地安全清理目录句柄不可用")
        current_fd = os.dup(root_fd)
        try:
            for part in relative_parent.parts:
                if part in {"", "."}:
                    continue
                next_fd = os.open(part, directory_flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except Exception:
            os.close(current_fd)
            raise

    def ensure_quarantine() -> int:
        nonlocal quarantine_dir_fd, quarantine_run_fd
        if quarantine_run_fd is not None:
            return quarantine_run_fd
        if root_fd is None:
            raise RuntimeError("本地安全清理目录句柄不可用")
        try:
            os.mkdir(_CLEANUP_QUARANTINE_DIR, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        quarantine_dir_fd = os.open(
            _CLEANUP_QUARANTINE_DIR,
            directory_flags,
            dir_fd=root_fd,
        )
        try:
            os.mkdir(quarantine_run_name, mode=0o700, dir_fd=quarantine_dir_fd)
            quarantine_run_fd = os.open(
                quarantine_run_name,
                directory_flags,
                dir_fd=quarantine_dir_fd,
            )
        except Exception:
            os.close(quarantine_dir_fd)
            quarantine_dir_fd = None
            raise
        return quarantine_run_fd

    try:
        root_fd = os.open(root, directory_flags)
    except (NotImplementedError, OSError) as exc:
        result.retained.extend(str(candidate.snapshot.path) for candidate in candidates)
        result.warnings.append(f"当前运行环境不支持安全垃圾清理: {exc}")
        return result

    try:
        for candidate in candidates:
            path = assert_within(candidate.snapshot.path, root)
            relative_path = path.relative_to(root)
            quarantine_name = f"{uuid.uuid4().hex}-{path.name}"
            moved = False
            source_parent_fd: int | None = None
            try:
                # 保留适配器的业务快照校验，再在固定目录句柄上做最终身份复核。
                adapter.verify_snapshot(candidate.snapshot)
                source_parent_fd = open_source_parent(relative_path.parent)
                info = os.stat(path.name, dir_fd=source_parent_fd, follow_symlinks=False)
                source_identity = (
                    int(info.st_size),
                    int(info.st_mtime_ns),
                    int(info.st_dev),
                    int(info.st_ino),
                ) if stat_module.S_ISREG(info.st_mode) else None
                if source_identity != candidate.snapshot.identity:
                    raise RuntimeError("垃圾文件在删除前被替换，已保留并停止清理")
                run_fd = ensure_quarantine()
                os.replace(
                    path.name,
                    quarantine_name,
                    src_dir_fd=source_parent_fd,
                    dst_dir_fd=run_fd,
                )
                moved = True
                info = os.stat(quarantine_name, dir_fd=run_fd, follow_symlinks=False)
                moved_identity = (
                    int(info.st_size),
                    int(info.st_mtime_ns),
                    int(info.st_dev),
                    int(info.st_ino),
                ) if stat_module.S_ISREG(info.st_mode) else None
                if moved_identity != candidate.snapshot.identity:
                    move_entry_no_replace_at(
                        quarantine_name,
                        path.name,
                        source_dir_fd=run_fd,
                        target_dir_fd=source_parent_fd,
                        is_directory=False,
                    )
                    moved = False
                    raise RuntimeError("垃圾文件在删除前被替换，已保留并停止清理")
                os.unlink(quarantine_name, dir_fd=run_fd)
                moved = False
                result.deleted.append(str(path))
                parent_dirs.add(path.parent)
            except Exception as exc:
                if moved:
                    result.retained.append(str(quarantine_display_root / quarantine_name))
                else:
                    result.retained.append(str(path))
                result.warnings.append(f"垃圾文件未删除 {path.name}: {exc}")
            finally:
                if source_parent_fd is not None:
                    os.close(source_parent_fd)
    finally:
        if quarantine_run_fd is not None:
            os.close(quarantine_run_fd)
        if quarantine_dir_fd is not None:
            try:
                os.rmdir(quarantine_run_name, dir_fd=quarantine_dir_fd)
            except OSError:
                pass
            os.close(quarantine_dir_fd)

    try:
        if not remove_empty_dirs:
            return result

        selected = Path(selected_path).expanduser().resolve(strict=False) if selected_path else None
        if selected:
            try:
                selected = assert_within(selected, root)
                relative_selected = selected.relative_to(root)
                selected_fd = open_source_parent(relative_selected)
            except (OSError, PathMappingError):
                parent_dirs.add(selected.parent)
            else:
                os.close(selected_fd)
                parent_dirs.add(selected)

        warned_path_change = False
        for directory in sorted(parent_dirs, key=lambda item: len(item.parts), reverse=True):
            try:
                relative = assert_within(directory, root).relative_to(root)
            except PathMappingError as exc:
                if not warned_path_change:
                    result.warnings.append(f"来源目录已变化，跳过空目录清理: {exc}")
                    warned_path_change = True
                continue
            while relative.parts:
                parent_fd: int | None = None
                try:
                    parent_fd = open_source_parent(Path(*relative.parts[:-1]))
                    os.rmdir(relative.parts[-1], dir_fd=parent_fd)
                    removed = root.joinpath(*relative.parts)
                    result.removed_dirs.append(str(removed))
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    if getattr(exc, "errno", None) not in {errno.ENOTEMPTY, errno.EEXIST}:
                        if not warned_path_change:
                            result.warnings.append(
                                f"来源目录已变化，跳过空目录清理: {type(exc).__name__}"
                            )
                            warned_path_change = True
                    break
                finally:
                    if parent_fd is not None:
                        os.close(parent_fd)
                relative = Path(*relative.parts[:-1])
        return result
    finally:
        if root_fd is not None:
            os.close(root_fd)


def cleanup_candidates_from_snapshots(
    snapshots: Iterable[LocalFileSnapshot],
) -> list[CleanupCandidate]:
    """复用既有扫描快照，只选择可确定删除的非媒体垃圾。"""
    items = list(snapshots)
    cleanup, _ = classify_cleanup_items(items)
    return cleanup


def probable_sample_video_paths(
    snapshots: Iterable[LocalFileSnapshot],
    *,
    sample_max_bytes: int = 300 * 1024 * 1024,
) -> set[Path]:
    """返回需保留但不自动归档的 sample/proof 视频路径。"""
    items = list(snapshots)
    primary_count = sum(
        1 for item in items
        if item.role == "video" and not _SAMPLE_RE.search(item.path.stem)
    )
    if primary_count <= 0:
        return set()
    return {
        item.path
        for item in items
        if item.role == "video"
        and is_probable_sample_video(
            item.path, item.size, sample_max_bytes=sample_max_bytes
        )
    }


def discover_cleanup_candidates(
    allowed_root: Path,
    selected_path: Path,
    *,
    item_limit: int = 20_000,
    depth_limit: int = 64,
) -> list[CleanupCandidate]:
    """在选择目录中发现明确垃圾；选择单文件时不扩展清理到兄弟文件。"""
    root = Path(allowed_root).expanduser().resolve(strict=False)
    selected = assert_within(Path(selected_path), root)
    relative_selected = selected.relative_to(root)
    selected_directory_parts = (
        relative_selected.parts if selected.is_dir() else relative_selected.parts[:-1]
    )
    if any(is_ignored_local_media_directory(part) for part in selected_directory_parts):
        return []
    if selected.is_file():
        paths = [selected]
    elif selected.is_dir():
        paths = []
        base_depth = len(selected.parts)
        visited = 0
        max_depth = max(1, int(depth_limit))
        max_items = max(1, int(item_limit))
        for current_root, dirs, files in os.walk(selected, followlinks=False):
            current = Path(current_root)
            depth = len(current.parts) - base_depth
            if depth >= max_depth:
                dirs[:] = []
            else:
                dirs[:] = [
                    name for name in dirs
                    if not is_ignored_local_media_directory(name)
                    and not (current / name).is_symlink()
                ]
            visited += len(dirs) + len(files)
            if visited > max_items:
                raise LocalScanLimitExceeded("垃圾清理扫描条目数量超过安全上限")
            for name in files:
                path = current / name
                if path.is_symlink():
                    continue
                paths.append(path)
    else:
        return []
    adapter = LocalFilesystemAdapter(root)
    snapshots: list[LocalFileSnapshot] = []
    for path in paths:
        try:
            snapshots.append(adapter.snapshot(path))
        except Exception:
            continue
    return cleanup_candidates_from_snapshots(snapshots)
