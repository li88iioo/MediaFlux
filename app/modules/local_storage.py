"""受控本地文件系统扫描、快照和校验。"""
from __future__ import annotations

import hashlib
import errno
import os
import shutil
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.modules.local_path_mapping import PathMappingError, assert_within
from app.modules.organize import METADATA_EXTS, VIDEO_EXTS
from app.modules.subtitle_identity import plan_subtitle_companions


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


@dataclass(frozen=True)
class _SiblingMediaFile:
    path: Path
    name: str
    file_id: str


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
        """有界检查路径是否包含可整理视频；不可读与不存在必须显式报错。"""
        start = assert_within(Path(path) if path is not None else self.allowed_root, self.allowed_root)
        relative_parts = start.relative_to(self.allowed_root).parts
        if any(is_ignored_local_media_directory(part) for part in relative_parts):
            return False
        try:
            start_info = start.lstat()
        except FileNotFoundError as exc:
            raise LocalContentChanged(f"扫描路径不存在: {start.name}") from exc
        except OSError as exc:
            raise LocalStorageError(f"扫描路径暂时不可读: {start.name}") from exc
        if stat_module.S_ISLNK(start_info.st_mode):
            return False

        def is_video(candidate: Path) -> bool:
            if candidate.suffix.lower().lstrip(".") not in VIDEO_EXTS:
                return False
            if self.is_temporary(candidate) or candidate.is_symlink():
                return False
            try:
                snapshot = self.snapshot(candidate)
            except (LocalStorageError, OSError):
                return False
            return snapshot.size >= self.min_video_size and snapshot.size > 0

        if stat_module.S_ISREG(start_info.st_mode):
            if self.is_temporary(start):
                return False
            snapshot = self.snapshot(start)
            return (
                snapshot.role == "video"
                and snapshot.size >= self.min_video_size
                and snapshot.size > 0
            )
        if not stat_module.S_ISDIR(start_info.st_mode):
            return False

        walk_errors: list[OSError] = []

        def record_walk_error(exc: OSError) -> None:
            walk_errors.append(exc)

        base_depth = len(start.parts)
        scanned = 0
        for current_root, dirs, files in os.walk(
            start, followlinks=False, onerror=record_walk_error,
        ):
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
        if walk_errors:
            raise LocalStorageError(f"目录暂时不可完整读取: {start.name}") from walk_errors[0]
        return False

    def scan(
        self,
        path: Path | None = None,
        *,
        include_non_media: bool = False,
    ) -> list[LocalFileSnapshot]:
        start = assert_within(Path(path) if path is not None else self.allowed_root, self.allowed_root)
        relative_parts = start.relative_to(self.allowed_root).parts
        if any(is_ignored_local_media_directory(part) for part in relative_parts):
            return []
        if start.is_symlink():
            raise LocalStorageError("禁止扫描符号链接")
        candidates: list[Path] = []
        if start.is_file():
            candidates = self._single_video_candidates(start)
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
            if candidate.is_symlink():
                continue
            if not include_non_media and self.is_temporary(candidate):
                continue
            snapshot = self.snapshot(candidate)
            if not include_non_media and snapshot.size <= 0:
                continue
            if not include_non_media and snapshot.role == "other":
                continue
            if snapshot.role == "video" and snapshot.size < self.min_video_size:
                continue
            snapshots.append(snapshot)
        return snapshots

    def _single_video_candidates(self, video: Path) -> list[Path]:
        """单视频任务只附带能唯一匹配该视频的同级字幕。"""
        if self.role_for(video) != "video":
            return [video]
        try:
            entries = sorted(video.parent.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            return [video]

        videos: list[_SiblingMediaFile] = []
        subtitles: list[_SiblingMediaFile] = []
        for candidate in entries:
            if self.is_temporary(candidate):
                continue
            try:
                info = candidate.lstat()
            except OSError:
                continue
            if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISREG(info.st_mode):
                continue
            if info.st_size <= 0:
                continue
            role = self.role_for(candidate)
            item = _SiblingMediaFile(
                path=candidate,
                name=candidate.name,
                file_id=candidate.as_posix(),
            )
            if role == "video" and info.st_size >= self.min_video_size:
                videos.append(item)
            elif role == "subtitle":
                subtitles.append(item)

        selected_id = video.as_posix()
        subtitle_result = plan_subtitle_companions(videos, subtitles)
        matched = [
            item.file.path
            for item in subtitle_result.plans
            if item.video_file_id == selected_id
        ]
        return [video, *matched]

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
# Linux/Docker 优先使用 renameat2(RENAME_NOREPLACE)；不支持时，普通文件
# 退化为原子硬链接发布。目录没有同等安全的可移植退化方式，宁可保留回收
# 副本并让用户重试，也绝不覆盖并发新建的同名内容。
def move_entry_no_replace_at(
    source_name: str,
    target_name: str,
    *,
    source_dir_fd: int,
    target_dir_fd: int,
    is_directory: bool,
) -> None:
    if os.name == "nt":
        os.rename(
            source_name,
            target_name,
            src_dir_fd=source_dir_fd,
            dst_dir_fd=target_dir_fd,
        )
        return
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is not None:
            renameat2.argtypes = [
                ctypes.c_int, ctypes.c_char_p,
                ctypes.c_int, ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            result = renameat2(
                source_dir_fd,
                os.fsencode(source_name),
                target_dir_fd,
                os.fsencode(target_name),
                1,  # RENAME_NOREPLACE
            )
            if result == 0:
                return
            error_number = ctypes.get_errno()
            if error_number == errno.EEXIST:
                raise FileExistsError(
                    error_number, os.strerror(error_number), target_name,
                )
            if error_number not in {
                errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP,
            }:
                raise OSError(error_number, os.strerror(error_number), target_name)
    except AttributeError:
        pass
    if is_directory:
        raise LocalStorageError("当前文件系统不支持目录的安全无覆盖恢复")
    try:
        os.link(
            source_name,
            target_name,
            src_dir_fd=source_dir_fd,
            dst_dir_fd=target_dir_fd,
            follow_symlinks=False,
        )
    except TypeError:
        os.link(
            source_name,
            target_name,
            src_dir_fd=source_dir_fd,
            dst_dir_fd=target_dir_fd,
        )
    os.unlink(source_name, dir_fd=source_dir_fd)
