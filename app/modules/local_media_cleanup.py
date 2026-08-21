"""本地媒体归档成功后的明确垃圾直删规则。

只删除能够可靠识别的垃圾；未知文件始终保留。调用方必须在媒体目标校验成功，
并且（若来自 qB）qB 任务已用 delete_files=False 移除后才执行。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from app.modules.local_path_mapping import assert_within
from app.modules.local_storage import (
    LocalFileSnapshot,
    LocalFilesystemAdapter,
    is_ignored_local_media_directory,
)


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


def classify_cleanup_items(
    snapshots: Iterable[LocalFileSnapshot],
    *,
    primary_video_count: int = 0,
    sample_max_bytes: int = 300 * 1024 * 1024,
) -> tuple[list[CleanupCandidate], list[LocalFileSnapshot]]:
    """返回（确定垃圾、保留文件），未知文件永远进入保留列表。"""
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
        elif (
            item.role == "video"
            and primary_video_count > 0
            and item.size <= max(1, int(sample_max_bytes))
            and _SAMPLE_RE.search(item.path.stem)
        ):
            candidate = CleanupCandidate(item, "sample", "明确标记的 sample/proof 样片")
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
    """复核快照后直删确定垃圾；按统一规则决定是否清除空目录。"""
    root = Path(allowed_root).expanduser().resolve(strict=False)
    adapter = LocalFilesystemAdapter(root)
    result = CleanupResult()
    parent_dirs: set[Path] = set()
    for candidate in candidates:
        path = assert_within(candidate.snapshot.path, root)
        try:
            adapter.verify_snapshot(candidate.snapshot)
            path.unlink()
            result.deleted.append(str(path))
            parent_dirs.add(path.parent)
        except Exception as exc:
            result.retained.append(str(path))
            result.warnings.append(f"垃圾文件未删除 {path.name}: {exc}")

    if not remove_empty_dirs:
        return result

    selected = Path(selected_path).expanduser().resolve(strict=False) if selected_path else None
    if selected:
        selected = assert_within(selected, root)
        parent_dirs.add(selected if selected.is_dir() else selected.parent)
    for directory in sorted(parent_dirs, key=lambda item: len(item.parts), reverse=True):
        current = directory
        while current != root and root in current.parents:
            try:
                assert_within(current, root)
                if not current.exists() or not current.is_dir():
                    break
                if any(current.iterdir()):
                    break
                current.rmdir()
                result.removed_dirs.append(str(current))
            except OSError:
                break
            current = current.parent
    return result


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
        for path in selected.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            relative_parts = path.relative_to(selected).parts[:-1]
            if any(is_ignored_local_media_directory(part) for part in relative_parts):
                continue
            if len(path.parts) - base_depth > max(1, int(depth_limit)):
                continue
            paths.append(path)
            if len(paths) > max(1, int(item_limit)):
                break
    else:
        return []
    adapter = LocalFilesystemAdapter(root)
    snapshots: list[LocalFileSnapshot] = []
    for path in paths:
        try:
            snapshots.append(adapter.snapshot(path))
        except Exception:
            continue
    non_sample_videos = sum(
        1 for item in snapshots if item.role == "video" and not _SAMPLE_RE.search(item.path.stem)
    )
    cleanup, _ = classify_cleanup_items(snapshots, primary_video_count=non_sample_videos)
    return cleanup
