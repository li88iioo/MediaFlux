"""受控本地文件系统扫描、快照和校验。"""
from __future__ import annotations

import hashlib
import errno
import os
import shutil
import stat as stat_module
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Iterable, Iterator

from app.modules.local_path_mapping import assert_within
from app.modules.organize import METADATA_EXTS, VIDEO_EXTS
from app.modules.subtitle_identity import SubtitleIdentity, plan_subtitle_companions
from app.modules.organize_postprocess import SUBTITLE_EXTS


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
_IMAGE_SUFFIXES = {"jpg", "jpeg", "png", "webp", "avif"}


def is_ignored_local_media_directory(value: str | Path) -> bool:
    """仅按完整目录名屏蔽系统目录与临时目录，避免误伤包含相同片段的媒体名。"""
    name = value.name if isinstance(value, Path) else value
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


class LocalSiblingScanBatch:
    """短批次只共享目录名称索引；文件状态和字幕归属始终现场复核。

    只保留一个目录，重复检查同一视频、目录身份变化或 30 秒到期即换批。
    不缓存 size/inode、匹配结果或媒体快照，零字节文件也进入名称索引，
    避免文件原地写入（不会更新父目录 mtime）后漏掉歧义或字幕。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.clear()

    def clear(self) -> None:
        with self._lock:
            self._scope: tuple | None = None
            self._identity: tuple | None = None
            self._expires_at = 0.0
            self._seen: set[Path] = set()
            self._videos: dict[str, list[Path]] = {}
            self._subtitles: dict[str, list[Path]] = {}

    @staticmethod
    def _directory_identity(directory: Path) -> tuple[int, int, int, int]:
        info = directory.stat()
        return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns

    @staticmethod
    def _stem(path: Path) -> str:
        return path.stem.casefold()

    def candidates(
        self, video: Path, adapter: LocalFilesystemAdapter, *, new_inspection: bool,
    ) -> tuple[list[Path], tuple[int, int, int, int]]:
        with self._lock:
            directory = video.parent
            scope = (adapter.allowed_root, directory, adapter.item_limit)
            identity = self._directory_identity(directory)
            if (
                self._scope != scope or self._identity != identity
                or time.monotonic() >= self._expires_at
                or (new_inspection and video in self._seen)
            ):
                self.clear()
                # 目录快照只有名称，相关候选的类型/大小由下游 lstat 现读。
                for count, candidate in enumerate(directory.iterdir(), 1):
                    if count > adapter.item_limit:
                        raise LocalScanLimitExceeded("目录文件数量超过安全上限")
                    if adapter.is_temporary(candidate):
                        continue
                    role = adapter.role_for(candidate)
                    stem = self._stem(candidate)
                    if role == "video":
                        self._videos.setdefault(stem, []).append(candidate)
                    elif role == "subtitle":
                        media_stem = SubtitleIdentity.parse(candidate.name).media_stem.casefold()
                        for key in {stem, media_stem}:
                            self._subtitles.setdefault(key, []).append(candidate)
                if self._directory_identity(directory) != identity:
                    self.clear()
                    raise LocalContentChanged("字幕目录在扫描期间发生变化，请重新检查")
                self._scope = scope
                self._identity = identity
                self._expires_at = time.monotonic() + 30.0
            if new_inspection:
                self._seen.add(video)
            stem = self._stem(video)
            subtitles = self._subtitles.get(stem, [])
            # exact stem 优先于语言后缀归一化；必须一并纳入这两组视频，
            # 否则 Show.en.mkv 会被错误地当成 Show.mkv 的英文字幕来源。
            video_stems = {stem}
            for subtitle in subtitles:
                video_stems.add(self._stem(subtitle))
                video_stems.add(SubtitleIdentity.parse(subtitle.name).media_stem.casefold())
            candidates = set(subtitles)
            for key in video_stems:
                candidates.update(self._videos.get(key, []))
            return sorted(candidates, key=lambda item: item.name.casefold()), identity


class LocalFilesystemAdapter:
    def __init__(
        self,
        allowed_root: Path,
        *,
        item_limit: int = 20_000,
        depth_limit: int = 64,
        min_video_size: int = 1,
        cache_directory_names: bool = False,
    ) -> None:
        self.allowed_root = assert_within(Path(allowed_root), Path(allowed_root))
        self.item_limit = max(1, item_limit)
        self.depth_limit = max(1, depth_limit)
        self.min_video_size = max(0, min_video_size)
        self._cache_directory_names = cache_directory_names
        self._directory_names: dict | None = None

    @contextmanager
    def directory_name_scope(self) -> Generator[None, None, None]:
        """显式限定一次发现操作的名称缓存；退出（含异常）后不得跨操作复用。

        目录时间戳可能在增删名称后保持不变，不能用作跨操作失效凭据。
        嵌套作用域共享当前索引；文件类型、大小和身份仍由消费者现读。
        """
        if not self._cache_directory_names or self._directory_names is not None:
            yield
            return
        self._directory_names = {}
        try:
            yield
        finally:
            self._directory_names = None

    @staticmethod
    def role_for(path: Path) -> str:
        ext = path.suffix.lower().lstrip(".")
        if ext in VIDEO_EXTS:
            return "video"
        if ext in SUBTITLE_EXTS:
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
            info.st_size,
            info.st_mtime_ns,
            info.st_dev,
            info.st_ino,
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

        return any(is_video(candidate) for candidate in self._walk_files(start, strict=False))

    def directory_entries(self, path: Path) -> tuple[Path, ...]:
        """默认现读名称；仅显式发现作用域内复用，并复核可观察的目录变化。"""
        info = path.lstat()
        identity = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
        cached = self._directory_names.get(path) if self._directory_names is not None else None
        if cached is not None and cached[0] == identity:
            return cached[1]
        entries = tuple(path.iterdir())
        if self._directory_names is not None:
            self._directory_names[path] = (identity, entries)
        return entries

    def _walk_files(self, start: Path, *, strict: bool) -> Iterator[Path]:
        """探测与完整扫描共用目录消费顺序、数量/深度预算与忽略规则。"""
        stack = [(start, 0)]
        scanned = 0
        first_error = None
        while stack:
            current, depth = stack.pop()
            if current.is_symlink():
                continue
            try:
                entries = self.directory_entries(current)
            except OSError as exc:
                if strict:
                    raise LocalStorageError(f"目录暂时不可完整读取: {start.name}") from exc
                first_error = first_error or exc
                continue
            if depth > self.depth_limit:
                raise LocalScanLimitExceeded("目录扫描深度超过安全上限")
            directories = []
            for candidate in entries:
                if candidate.is_dir():
                    if not is_ignored_local_media_directory(candidate.name):
                        directories.append(candidate)
                    continue
                scanned += 1
                if scanned > self.item_limit:
                    raise LocalScanLimitExceeded("目录文件数量超过安全上限")
                yield candidate
            stack.extend((child, depth + 1) for child in reversed(directories))
        if first_error is not None:
            raise LocalStorageError(f"目录暂时不可完整读取: {start.name}") from first_error

    def scan(
        self,
        path: Path | None = None,
        *,
        include_non_media: bool = False,
        sibling_batch: LocalSiblingScanBatch | None = None,
        new_inspection: bool = False,
    ) -> list[LocalFileSnapshot]:
        start = assert_within(Path(path) if path is not None else self.allowed_root, self.allowed_root)
        relative_parts = start.relative_to(self.allowed_root).parts
        if any(is_ignored_local_media_directory(part) for part in relative_parts):
            return []
        if start.is_symlink():
            raise LocalStorageError("禁止扫描符号链接")
        candidates: list[Path] = []
        if start.is_file():
            candidates = self._single_video_candidates(
                start, sibling_batch=sibling_batch, new_inspection=new_inspection,
            )
        elif start.is_dir():
            candidates = list(self._walk_files(start, strict=True))
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

    def _single_video_candidates(
        self, video: Path, *, sibling_batch: LocalSiblingScanBatch | None = None,
        new_inspection: bool = False,
    ) -> list[Path]:
        """单视频任务只附带能唯一匹配该视频的同级字幕。"""
        if self.role_for(video) != "video":
            return [video]
        batch = sibling_batch if sibling_batch is not None else LocalSiblingScanBatch()
        try:
            entries, directory_identity = batch.candidates(
                video, self, new_inspection=new_inspection,
            )
        except OSError as exc:
            batch.clear()
            raise LocalStorageError(f"字幕目录暂时不可完整读取: {video.parent.name}") from exc

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

        if batch._directory_identity(video.parent) != directory_identity:
            batch.clear()
            raise LocalContentChanged("字幕目录在扫描期间发生变化，请重新检查")
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
        return shutil.disk_usage(probe).free


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
    source_name: str | Path,
    target_name: str | Path,
    *,
    source_dir_fd: int | None = None,
    target_dir_fd: int | None = None,
    is_directory: bool = False,
) -> None:
    """唯一的无覆盖移动实现，支持普通路径与已固定父目录的相对路径。"""
    dir_args = {}
    if source_dir_fd is not None:
        dir_args["src_dir_fd"] = source_dir_fd
    if target_dir_fd is not None:
        dir_args["dst_dir_fd"] = target_dir_fd
    if os.name == "nt":
        os.rename(source_name, target_name, **dir_args)
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
                source_dir_fd if source_dir_fd is not None else -100,  # AT_FDCWD
                os.fsencode(source_name),
                target_dir_fd if target_dir_fd is not None else -100,
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
    def identity(name: str | Path, dir_fd: int | None) -> tuple[int, int, int, int]:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    source_identity = identity(source_name, source_dir_fd)
    try:
        os.link(source_name, target_name, follow_symlinks=False, **dir_args)
    except OSError as exc:
        if exc.errno in {errno.EPERM, errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}:
            raise LocalStorageError("目标文件系统不支持安全的无覆盖发布") from exc
        raise
    try:
        os.unlink(source_name, dir_fd=source_dir_fd)
    except Exception:
        # 只有源仍是原副本、目标仍是本次创建的别名时才撤销发布。
        # 若源已被外部移除或替换，目标可能是唯一原始副本，必须保留。
        try:
            if (
                identity(source_name, source_dir_fd) == source_identity
                and identity(target_name, target_dir_fd) == source_identity
            ):
                os.unlink(target_name, dir_fd=target_dir_fd)
        except OSError:
            pass  # 补偿失败不掩盖原始错误，也不冒险删除无法确认的副本。
        raise
