"""受控本地文件系统扫描、快照和校验。"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.modules.local_path_mapping import PathMappingError, assert_within
from app.modules.organize import METADATA_EXTS, VIDEO_EXTS


class LocalStorageError(RuntimeError):
    """本地文件系统操作失败。"""


class LocalContentChanged(LocalStorageError):
    """扫描后的源文件内容或身份发生变化。"""


class LocalScanLimitExceeded(LocalStorageError):
    """扫描项目数或目录深度超过安全上限。"""


_TEMP_SUFFIXES = {
    ".part", ".partial", ".tmp", ".temp", ".crdownload", ".download", ".!qb", ".aria2",
}
IGNORED_LOCAL_MEDIA_DIRECTORY_NAMES: frozenset[str] = frozenset(
    {
        ".mediaflux-trash",
        ".appledouble",
        ".temp",
        ".tmp",
        "#recycle",
        "$recycle.bin",
        "@eadir",
        "__macosx",
        "lost+found",
        "system volume information",
        "temp",
        "tmp",
    }
)
_SUBTITLE_SUFFIXES = {"srt", "ass", "ssa", "sub", "idx", "vtt", "sup"}
_IMAGE_SUFFIXES = {"jpg", "jpeg", "png", "webp", "avif"}


def is_ignored_local_media_directory(value: str | Path) -> bool:
    """仅按完整目录名屏蔽系统目录与临时目录，避免误伤包含相同片段的媒体名。"""
    name = value.name if isinstance(value, Path) else str(value)
    return name.strip().casefold() in IGNORED_LOCAL_MEDIA_DIRECTORY_NAMES


@dataclass(frozen=True)
class LocalFileSnapshot:
    path: Path
    relative_path: str
    size: int
    mtime_ns: int
    device: int
    inode: int
    role: str

    @property
    def identity(self) -> tuple[int, int, int, int]:
        return self.size, self.mtime_ns, self.device, self.inode


class LocalFilesystemAdapter:
    def __init__(
        self,
        allowed_root: Path,
        *,
        item_limit: int = 20_000,
        depth_limit: int = 64,
        min_video_size: int = 1,
    ) -> None:
        self.allowed_root = assert_within(Path(allowed_root), Path(allowed_root))
        self.item_limit = max(1, int(item_limit))
        self.depth_limit = max(1, int(depth_limit))
        self.min_video_size = max(0, int(min_video_size))

    @staticmethod
    def role_for(path: Path) -> str:
        ext = path.suffix.lower().lstrip(".")
        if ext in VIDEO_EXTS:
            return "video"
        if ext in _SUBTITLE_SUFFIXES:
            return "subtitle"
        if ext == "nfo":
            return "nfo"
        if ext in _IMAGE_SUFFIXES:
            return "image"
        if ext in METADATA_EXTS:
            return "metadata"
        return "other"

    @staticmethod
    def is_temporary(path: Path) -> bool:
        lower_name = path.name.lower()
        return any(lower_name.endswith(suffix) for suffix in _TEMP_SUFFIXES)

    @staticmethod
    def regular_file_identity(path: Path) -> tuple[int, int, int, int]:
        """基于一次 lstat 返回普通文件身份，避免分离的 symlink/stat/is_file 预检。"""
        candidate = Path(path)
        try:
            info = candidate.lstat()
        except FileNotFoundError as exc:
            raise LocalContentChanged(f"源文件不存在: {candidate.name}") from exc
        if stat_module.S_ISLNK(info.st_mode):
            raise LocalStorageError("禁止扫描符号链接")
        if not stat_module.S_ISREG(info.st_mode):
            raise LocalStorageError("快照目标不是普通文件")
        return (
            int(info.st_size),
            int(info.st_mtime_ns),
            int(info.st_dev),
            int(info.st_ino),
        )

    def snapshot(self, path: Path) -> LocalFileSnapshot:
        candidate = assert_within(Path(path), self.allowed_root)
        size, mtime_ns, device, inode = self.regular_file_identity(candidate)
        try:
            relative = candidate.relative_to(self.allowed_root).as_posix()
        except ValueError as exc:
            raise LocalStorageError("源文件超出允许根目录") from exc
        return LocalFileSnapshot(
            path=candidate,
            relative_path=relative,
            size=size,
            mtime_ns=mtime_ns,
            device=device,
            inode=inode,
            role=self.role_for(candidate),
        )

    def verify_snapshot(self, snapshot: LocalFileSnapshot) -> LocalFileSnapshot:
        current = self.snapshot(snapshot.path)
        if current.relative_path != snapshot.relative_path or current.identity != snapshot.identity:
            raise LocalContentChanged(f"源文件在处理期间发生变化: {snapshot.relative_path}")
        return current

    def contains_video(self, path: Path | None = None) -> bool:
        """有界检查路径是否包含可整理视频，用于目录条目与自动扫描预筛选。"""
        start = assert_within(Path(path) if path is not None else self.allowed_root, self.allowed_root)
        relative_parts = start.relative_to(self.allowed_root).parts
        if any(is_ignored_local_media_directory(part) for part in relative_parts):
            return False
        if start.is_symlink():
            return False
        def is_video(candidate: Path) -> bool:
            if self.is_temporary(candidate) or candidate.is_symlink():
                return False
            try:
                snapshot = self.snapshot(candidate)
            except (LocalStorageError, OSError):
                return False
            return (
                snapshot.role == "video"
                and snapshot.size >= self.min_video_size
                and snapshot.size > 0
            )

        if start.is_file():
            return is_video(start)
        if not start.is_dir():
            return False

        base_depth = len(start.parts)
        scanned = 0
        for current_root, dirs, files in os.walk(start, followlinks=False):
            current = Path(current_root)
            depth = len(current.parts) - base_depth
            if depth > self.depth_limit:
                raise LocalScanLimitExceeded("目录扫描深度超过安全上限")
            dirs[:] = [
                name for name in dirs
                if not is_ignored_local_media_directory(name)
                and not (current / name).is_symlink()
            ]
            for name in files:
                scanned += 1
                if scanned > self.item_limit:
                    raise LocalScanLimitExceeded("目录文件数量超过安全上限")
                if is_video(current / name):
                    return True
        return False

    def scan(self, path: Path | None = None) -> list[LocalFileSnapshot]:
        start = assert_within(Path(path) if path is not None else self.allowed_root, self.allowed_root)
        relative_parts = start.relative_to(self.allowed_root).parts
        if any(is_ignored_local_media_directory(part) for part in relative_parts):
            return []
        if start.is_symlink():
            raise LocalStorageError("禁止扫描符号链接")
        candidates: list[Path] = []
        if start.is_file():
            candidates = [start]
        elif start.is_dir():
            base_depth = len(start.parts)
            for current_root, dirs, files in os.walk(start, followlinks=False):
                current = Path(current_root)
                depth = len(current.parts) - base_depth
                if depth > self.depth_limit:
                    raise LocalScanLimitExceeded("目录扫描深度超过安全上限")
                safe_dirs: list[str] = []
                for name in dirs:
                    child = current / name
                    if is_ignored_local_media_directory(name) or child.is_symlink():
                        continue
                    safe_dirs.append(name)
                dirs[:] = safe_dirs
                for name in files:
                    candidates.append(current / name)
                    if len(candidates) > self.item_limit:
                        raise LocalScanLimitExceeded("目录文件数量超过安全上限")
        else:
            raise LocalContentChanged("扫描路径不存在")

        snapshots: list[LocalFileSnapshot] = []
        for candidate in sorted(candidates, key=lambda item: item.as_posix().casefold()):
            if self.is_temporary(candidate) or candidate.is_symlink():
                continue
            snapshot = self.snapshot(candidate)
            if snapshot.size <= 0:
                continue
            if snapshot.role == "other":
                continue
            if snapshot.role == "video" and snapshot.size < self.min_video_size:
                continue
            snapshots.append(snapshot)
        return snapshots

    @staticmethod
    def same_filesystem(source: Path, target: Path) -> bool:
        source_dev = Path(source).stat().st_dev
        probe = Path(target)
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if not probe.exists():
            raise LocalStorageError("无法确定目标文件系统")
        return source_dev == probe.stat().st_dev

    @staticmethod
    def available_space(path: Path) -> int:
        probe = Path(path)
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if not probe.exists():
            raise LocalStorageError("无法确定目标磁盘可用空间")
        return int(shutil.disk_usage(probe).free)


def snapshot_digest(snapshots: Iterable[LocalFileSnapshot]) -> str:
    digest = hashlib.sha256()
    for item in sorted(snapshots, key=lambda value: value.relative_path):
        digest.update(item.relative_path.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
        digest.update(f"{item.size}:{item.mtime_ns}:{item.device}:{item.inode}:{item.role}".encode())
        digest.update(b"\n")
    return digest.hexdigest()
