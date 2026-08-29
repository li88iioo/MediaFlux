"""本地媒体同盘/跨盘安全移动事务。"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat as stat_module
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator, Protocol, Sequence

from app import database as db
from app.modules.local_path_mapping import assert_within
from app.modules.local_storage import (
    LocalContentChanged,
    LocalFileSnapshot,
    LocalFilesystemAdapter,
)
from app.modules.process_lock import CrossProcessLock


class MovePlanLike(Protocol):
    source: LocalFileSnapshot
    target: Path
    role: str
    action: str
    expected_target_identity: tuple[int, int, int, int] | None
    retire_target: Path | None
    expected_retire_identity: tuple[int, int, int, int] | None


class LocalMoveError(RuntimeError):
    """移动事务失败且已尽力回滚。"""


@dataclass(frozen=True)
class MovedItem:
    source: Path
    target: Path
    role: str
    cross_filesystem: bool
    published_identity: tuple[int, int, int, int]


@dataclass
class MoveTransactionResult:
    status: str
    moved: list[MovedItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rollback_errors: list[str] = field(default_factory=list)


# 所有本地媒体写事务共用同一把跨线程/跨进程锁。媒体移动频率低，粗粒度串行
# 比按路径派生多把锁更容易避免来源/目标根有交集时的锁遗漏。
_LOCAL_MEDIA_MOVE_LOCK = CrossProcessLock("local-media-move")


class LocalMoveTransaction:
    def __init__(
        self,
        source_roots: Sequence[Path],
        target_roots: Sequence[Path],
        *,
        task_id: int | None = None,
        owner: str = "admin",
        operation_token: str = "",
        chunk_size: int = 4 * 1024 * 1024,
    ) -> None:
        if not source_roots or not target_roots:
            raise ValueError("移动事务必须配置来源和目标白名单")
        self.source_roots = tuple(
            assert_within(Path(root).expanduser().absolute(), Path(root).expanduser().absolute())
            for root in source_roots
        )
        self.target_roots = tuple(
            assert_within(Path(root).expanduser().absolute(), Path(root).expanduser().absolute())
            for root in target_roots
        )
        self.task_id = task_id
        self.owner = str(owner or "admin")
        self.operation_token = operation_token or uuid.uuid4().hex
        self.chunk_size = max(64 * 1024, int(chunk_size))
        self._moved: list[MovedItem] = []
        self._moved_steps: dict[Path, int] = {}
        self._step_ids: dict[int, int] = {}
        self._replaced_backups: dict[
            Path, tuple[Path, tuple[int, int, int, int], Path]
        ] = {}

    @staticmethod
    def _within_any(path: Path, roots: Sequence[Path]) -> Path:
        errors: list[Exception] = []
        for root in roots:
            try:
                return assert_within(path, root)
            except Exception as exc:
                errors.append(exc)
        raise LocalMoveError("路径不在允许的来源或目标根目录内") from (
            errors[-1] if errors else None
        )

    @contextmanager
    def _pinned_entry(
        self,
        path: Path,
        roots: Sequence[Path],
        *,
        create_parent: bool = False,
    ) -> Iterator[tuple[Path, Path]]:
        """将最终目录项固定到逐段打开的目录句柄，避免父目录被并发替换。"""
        display = self._within_any(path, roots)
        root = next(
            (
                candidate
                for candidate in sorted(roots, key=lambda item: len(item.parts), reverse=True)
                if display == candidate or candidate in display.parents
            ),
            None,
        )
        if root is None:
            raise LocalMoveError("路径不在允许的来源或目标根目录内")
        relative = display.relative_to(root)
        if not relative.parts:
            raise LocalMoveError("移动事务不能直接操作根目录")
        proc_fd_root = Path("/proc/self/fd")
        if os.name != "posix" or not proc_fd_root.is_dir():
            raise LocalMoveError("当前运行环境不支持目录句柄固定，拒绝执行本地移动")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        opened: list[int] = []
        current_fd = os.open(root, flags)
        opened.append(current_fd)
        try:
            for part in relative.parts[:-1]:
                try:
                    next_fd = os.open(part, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create_parent:
                        raise
                    try:
                        os.mkdir(part, mode=0o755, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                    next_fd = os.open(part, flags, dir_fd=current_fd)
                current_fd = next_fd
                opened.append(current_fd)
            yield display, proc_fd_root / str(current_fd) / relative.name
        finally:
            for fd in reversed(opened):
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _unlink_owned_anchored(
        self,
        path: Path,
        expected: tuple[int, int, int, int],
        *,
        roots: Sequence[Path],
        label: str,
    ) -> None:
        with self._pinned_entry(path, roots) as (_display, pinned):
            self._unlink_owned(pinned, expected, label=label)

    @staticmethod
    def _identity(path: Path) -> tuple[int, int, int, int]:
        return LocalFilesystemAdapter.regular_file_identity(Path(path))

    @classmethod
    def _identity_or_none(cls, path: Path) -> tuple[int, int, int, int] | None:
        try:
            return cls._identity(path)
        except LocalContentChanged:
            return None

    @classmethod
    def _require_identity(
        cls,
        path: Path,
        expected: tuple[int, int, int, int],
        *,
        label: str,
    ) -> tuple[int, int, int, int]:
        current = cls._identity(path)
        if current != expected:
            raise LocalMoveError(f"{label}已被外部修改，拒绝继续操作: {path}")
        return current

    @classmethod
    def _unlink_owned(
        cls,
        path: Path,
        expected: tuple[int, int, int, int],
        *,
        label: str,
    ) -> None:
        current = cls._identity_or_none(path)
        if current is None:
            return
        if current != expected:
            raise LocalMoveError(f"{label}已被外部修改，拒绝删除: {path}")
        Path(path).unlink()

    @staticmethod
    def _identity_from_stat(info: os.stat_result) -> tuple[int, int, int, int]:
        return (
            int(info.st_size),
            int(info.st_mtime_ns),
            int(info.st_dev),
            int(info.st_ino),
        )

    @contextmanager
    def _open_verified_source(
        self,
        snapshot: LocalFileSnapshot,
        source_path: Path | None = None,
    ) -> Iterator[tuple[BinaryIO, os.stat_result]]:
        source = Path(source_path) if source_path is not None else self._within_any(
            snapshot.path, self.source_roots
        )
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(source, flags)
        except FileNotFoundError as exc:
            raise LocalMoveError(f"源文件不存在: {source.name}") from exc
        try:
            with os.fdopen(fd, "rb", closefd=False) as stream:
                info = os.fstat(fd)
                if not stat_module.S_ISREG(info.st_mode):
                    raise LocalMoveError(f"源文件不是普通文件: {source.name}")
                if self._identity_from_stat(info) != snapshot.identity:
                    raise LocalMoveError(f"源文件在处理期间发生变化: {snapshot.relative_path}")
                yield stream, info
                after = os.fstat(fd)
                if self._identity_from_stat(after) != snapshot.identity:
                    raise LocalMoveError(f"源文件在处理期间发生变化: {snapshot.relative_path}")
        finally:
            os.close(fd)

    @staticmethod
    def _quick_fingerprint_stream(
        stream: BinaryIO,
        size: int,
        block_size: int = 1024 * 1024,
    ) -> str:
        digest = hashlib.sha256()
        original = stream.tell()
        try:
            stream.seek(0)
            digest.update(stream.read(block_size))
            if size > block_size:
                stream.seek(max(0, size - block_size))
                digest.update(stream.read(block_size))
        finally:
            stream.seek(original)
        digest.update(str(size).encode())
        return digest.hexdigest()

    @classmethod
    def _quick_fingerprint(cls, path: Path, size: int) -> str:
        before = cls._identity(path)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            with os.fdopen(fd, "rb", closefd=False) as stream:
                fingerprint = cls._quick_fingerprint_stream(stream, size)
        finally:
            os.close(fd)
        after = cls._identity(path)
        if before != after:
            raise LocalMoveError(f"文件在读取指纹期间发生变化: {Path(path).name}")
        return fingerprint

    @classmethod
    def _publish_no_replace(
        cls,
        source: Path,
        target: Path,
    ) -> tuple[int, int, int, int]:
        """同文件系统原子发布且绝不覆盖已有目标。"""
        source_identity = cls._identity(source)
        if os.name == "nt":  # Windows rename 在目标存在时失败，不覆盖。
            os.rename(source, target)
            return source_identity

        renameat2 = None
        try:
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = getattr(libc, "renameat2", None)
            if renameat2 is not None:
                renameat2.argtypes = [
                    ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
                ]
                renameat2.restype = ctypes.c_int
                result = renameat2(
                    -100, os.fsencode(source), -100, os.fsencode(target), 1,
                )
                if result == 0:
                    return source_identity
                error_number = ctypes.get_errno()
                if error_number == 17:  # EEXIST
                    raise FileExistsError(error_number, os.strerror(error_number), str(target))
                if error_number not in {22, 38, 95}:  # EINVAL/ENOSYS/EOPNOTSUPP
                    raise OSError(error_number, os.strerror(error_number), str(target))
        except AttributeError:
            renameat2 = None

        # 不支持 renameat2 的 POSIX 文件系统使用“硬链接 + 删除旧目录项”。
        # link 的目标创建是原子且 no-clobber；若文件系统不支持硬链接则保守失败。
        try:
            os.link(source, target, follow_symlinks=False)
        except TypeError:  # 极旧平台没有 follow_symlinks 参数。
            os.link(source, target)
        except OSError as exc:
            if getattr(exc, "errno", None) in {1, 22, 38, 45, 95}:
                raise LocalMoveError(
                    f"目标文件系统不支持安全的无覆盖发布: {target.parent}"
                ) from exc
            raise
        try:
            os.unlink(source)
        except Exception:
            try:
                if cls._identity(target) == source_identity:
                    target.unlink()
            except Exception:
                pass
            raise
        return source_identity

    def _record_step(self, index: int, action: str, source: Path, target: Path) -> None:
        if self.task_id is None:
            return
        step_id = db.add_local_media_operation_step(
            self.task_id,
            self.operation_token,
            index,
            action,
            str(source),
            str(target),
            owner=self.owner,
        )
        db.update_local_media_operation_step(step_id, "running")
        self._step_ids[index] = step_id

    def _finish_step(self, index: int, status: str, error: str = "") -> None:
        step_id = self._step_ids.get(index)
        if step_id is not None:
            db.update_local_media_operation_step(step_id, status, error=error)

    def _verify_target(
        self,
        source_fingerprint: str,
        target: Path,
        expected_size: int,
    ) -> tuple[int, int, int, int]:
        before = self._identity(target)
        if before[0] != expected_size:
            raise LocalMoveError(f"目标文件大小校验失败: {target.name}")
        if self._quick_fingerprint(target, expected_size) != source_fingerprint:
            raise LocalMoveError(f"目标文件指纹校验失败: {target.name}")
        after = self._identity(target)
        if before != after:
            raise LocalMoveError(f"目标文件在校验期间发生变化: {target.name}")
        return after

    def _copy_to_partial(
        self,
        snapshot: LocalFileSnapshot,
        source: Path,
        target: Path,
    ) -> tuple[Path, str]:
        if LocalFilesystemAdapter.available_space(target.parent) < snapshot.size:
            raise LocalMoveError(f"目标磁盘空间不足: {target.parent}")
        partial = target.with_name(f".{target.name}.mediaflux-partial-{self.operation_token[:12]}")
        if partial.exists():
            raise FileExistsError(f"发现未清理的移动临时文件: {partial}")
        try:
            with self._open_verified_source(snapshot, source) as (src, source_info):
                source_fingerprint = self._quick_fingerprint_stream(src, snapshot.size)
                src.seek(0)
                with partial.open("xb") as dst:
                    shutil.copyfileobj(src, dst, length=self.chunk_size)
                    dst.flush()
                    os.fsync(dst.fileno())
                try:
                    os.chmod(partial, stat_module.S_IMODE(source_info.st_mode))
                    if os.utime in getattr(os, "supports_follow_symlinks", set()):
                        os.utime(
                            partial,
                            ns=(int(source_info.st_atime_ns), int(source_info.st_mtime_ns)),
                            follow_symlinks=False,
                        )
                    else:
                        os.utime(
                            partial,
                            ns=(int(source_info.st_atime_ns), int(source_info.st_mtime_ns)),
                        )
                except (OSError, NotImplementedError):
                    # 部分 SMB/NAS/Windows 不支持完整 POSIX 元数据；内容与身份校验仍必须继续。
                    pass
            self._verify_target(source_fingerprint, partial, snapshot.size)
            self._require_identity(source, snapshot.identity, label="源文件")
            return partial, source_fingerprint
        except Exception:
            partial.unlink(missing_ok=True)
            raise

    def _retire_copied_source(
        self,
        snapshot: LocalFileSnapshot,
        source: Path,
    ) -> None:
        try:
            self._require_identity(source, snapshot.identity, label="源文件")
        except LocalMoveError as exc:
            raise LocalMoveError(
                f"源文件在处理期间发生变化: {snapshot.relative_path}"
            ) from exc
        retired = source.with_name(
            f".{source.name}.mediaflux-retire-{self.operation_token[:12]}"
        )
        if retired.exists():
            raise FileExistsError(f"发现未清理的来源退役文件: {retired}")
        os.replace(source, retired)
        try:
            self._require_identity(retired, snapshot.identity, label="来源退役文件")
        except Exception:
            if not source.exists() and retired.exists():
                os.replace(retired, source)
            raise
        self._unlink_owned(retired, snapshot.identity, label="来源退役文件")

    def _backup_replaced_target(
        self,
        retire_display: Path,
        expected_identity: tuple[int, int, int, int],
        *,
        transaction_target: Path,
    ) -> tuple[Path, tuple[int, int, int, int]]:
        """原子退役旧版本，并记录其真实恢复位置供事务回滚。"""
        backup_display = retire_display.with_name(
            f".{retire_display.name}.mediaflux-replaced-{self.operation_token[:12]}"
        )
        with self._pinned_entry(
            retire_display, self.target_roots,
        ) as (_retire_display, retire), self._pinned_entry(
            backup_display, self.target_roots,
        ) as (_backup_display, backup):
            self._require_identity(retire, expected_identity, label="待替换目标")
            if self._identity_or_none(backup) is not None:
                raise FileExistsError(f"发现未清理的替换备份: {backup_display}")
            os.replace(retire, backup)
            # rename 成功后先登记可恢复状态；后续身份读取即使瞬态失败，
            # 外层 rollback 仍能找到隐藏备份并尝试恢复原媒体库路径。
            self._replaced_backups[transaction_target] = (
                backup_display,
                expected_identity,
                retire_display,
            )
            try:
                backup_identity = self._identity(backup)
                if backup_identity != expected_identity:
                    raise LocalMoveError(
                        f"待替换目标在备份时发生变化: {retire_display.name}"
                    )
            except Exception:
                try:
                    self._restore_replaced_backup(
                        transaction_target,
                        backup_display,
                        expected_identity,
                        retire_display,
                    )
                except Exception:
                    # 保留登记记录，交给事务级 rollback 再次恢复；若仍失败，
                    # 会明确返回人工核验错误，而不是静默遗留隐藏文件。
                    pass
                raise
        return backup_display, backup_identity

    def _restore_replaced_backup(
        self,
        transaction_target: Path,
        backup_display: Path,
        backup_identity: tuple[int, int, int, int],
        restore_display: Path,
    ) -> None:
        with self._pinned_entry(
            backup_display, self.target_roots,
        ) as (_backup_display, backup), self._pinned_entry(
            restore_display, self.target_roots,
        ) as (_restore_display, restore):
            self._require_identity(backup, backup_identity, label="替换备份")
            if self._identity_or_none(restore) is not None:
                raise LocalMoveError(f"回滚目标路径已被占用: {restore_display}")
            os.replace(backup, restore)
            self._require_identity(
                restore,
                backup_identity,
                label="恢复后的替换目标",
            )
        self._replaced_backups.pop(transaction_target, None)

    def _move_one(self, plan: MovePlanLike, index: int) -> MovedItem:
        source_display = self._within_any(plan.source.path, self.source_roots)
        target_display = self._within_any(Path(plan.target), self.target_roots)
        plan_action = str(getattr(plan, "action", "move") or "move")
        if plan_action not in {"move", "replace"}:
            raise LocalMoveError(f"不支持的本地移动动作: {plan_action}")
        retire_value = getattr(plan, "retire_target", None)
        retire_display = (
            self._within_any(Path(retire_value), self.target_roots)
            if retire_value is not None else target_display
        )
        replace_same_path = plan_action == "replace" and retire_display == target_display
        with self._pinned_entry(
            source_display, self.source_roots
        ) as (_source_display, source), self._pinned_entry(
            target_display, self.target_roots, create_parent=True
        ) as (_target_display, target):
            self._require_identity(source, plan.source.identity, label="源文件")
            current_target_identity = self._identity_or_none(target)
            if current_target_identity is not None and plan_action != "replace":
                raise FileExistsError(f"本地媒体库已存在目标文件: {target_display}")
            if plan_action == "replace":
                expected_target = (
                    getattr(plan, "expected_target_identity", None)
                    if replace_same_path
                    else getattr(plan, "expected_retire_identity", None)
                )
                if replace_same_path:
                    current_retire_identity = current_target_identity
                else:
                    if current_target_identity is not None:
                        raise LocalMoveError(
                            f"新版本目标在预览后已被占用，请重新生成预览: {target_display.name}"
                        )
                    with self._pinned_entry(
                        retire_display, self.target_roots,
                    ) as (_retire_display, retire):
                        current_retire_identity = self._identity_or_none(retire)
                if current_retire_identity is None:
                    raise LocalMoveError(
                        f"待替换目标已不存在，请重新生成预览: {retire_display.name}"
                    )
                if expected_target is not None and current_retire_identity != expected_target:
                    raise LocalMoveError(
                        f"待替换目标在预览后发生变化，请重新生成预览: {retire_display.name}"
                    )
            same_fs = LocalFilesystemAdapter.same_filesystem(source, target)
            action = "replace" if plan_action == "replace" else (
                "rename" if same_fs else "copy_verify_delete"
            )
            self._record_step(index, action, source_display, target_display)
            with self._open_verified_source(plan.source, source) as (stream, _):
                source_fingerprint = self._quick_fingerprint_stream(stream, plan.source.size)
            self._require_identity(source, plan.source.identity, label="源文件")
            partial: Path | None = None
            target_created = False
            published_identity: tuple[int, int, int, int] | None = None
            moved: MovedItem | None = None
            backup: Path | None = None
            backup_identity: tuple[int, int, int, int] | None = None
            try:
                if plan_action == "replace":
                    backup_display, backup_identity = self._backup_replaced_target(
                        retire_display,
                        current_retire_identity,
                        transaction_target=target_display,
                    )
                    backup = backup_display
                else:
                    if self._identity_or_none(target) is not None:
                        raise FileExistsError(f"本地媒体库已存在目标文件: {target_display}")
                if same_fs:
                    if replace_same_path:
                        os.replace(source, target)
                        published_identity = plan.source.identity
                    else:
                        published_identity = self._publish_no_replace(source, target)
                    target_created = True
                    self._require_identity(target, published_identity, label="事务目标")
                    moved = MovedItem(
                        source=source_display,
                        target=target_display,
                        role=str(plan.role),
                        cross_filesystem=False,
                        published_identity=published_identity,
                    )
                    self._moved.append(moved)
                    self._moved_steps[target_display] = index
                    if published_identity != plan.source.identity:
                        raise LocalMoveError(
                            f"源文件在最终移动前发生变化: {plan.source.relative_path}"
                        )
                    self._verify_target(source_fingerprint, target, plan.source.size)
                else:
                    partial, copied_fingerprint = self._copy_to_partial(
                        plan.source, source, target
                    )
                    if copied_fingerprint != source_fingerprint:
                        raise LocalMoveError(
                            f"源文件在复制准备期间发生变化: {plan.source.relative_path}"
                        )
                    if plan_action != "replace" and self._identity_or_none(target) is not None:
                        raise FileExistsError(f"本地媒体库已存在目标文件: {target_display}")
                    if replace_same_path:
                        published_identity = self._identity(partial)
                        os.replace(partial, target)
                    else:
                        published_identity = self._publish_no_replace(partial, target)
                    partial = None
                    target_created = True
                    self._require_identity(target, published_identity, label="事务目标")
                    self._verify_target(source_fingerprint, target, plan.source.size)
                    self._retire_copied_source(plan.source, source)
                    moved = MovedItem(
                        source=source_display,
                        target=target_display,
                        role=str(plan.role),
                        cross_filesystem=True,
                        published_identity=published_identity,
                    )
                    self._moved.append(moved)
                    self._moved_steps[target_display] = index
                self._finish_step(index, "completed")
                return moved
            except Exception as exc:
                cleanup_errors: list[str] = []
                if partial is not None:
                    try:
                        partial.unlink(missing_ok=True)
                    except Exception as cleanup_exc:
                        cleanup_errors.append(str(cleanup_exc))
                if target_created and moved is None and published_identity is not None:
                    try:
                        self._unlink_owned(target, published_identity, label="事务目标")
                    except Exception as cleanup_exc:
                        cleanup_errors.append(str(cleanup_exc))
                if moved is None and backup is not None and backup_identity is not None:
                    try:
                        self._restore_replaced_backup(
                            target_display,
                            backup,
                            backup_identity,
                            retire_display,
                        )
                    except Exception as cleanup_exc:
                        cleanup_errors.append(str(cleanup_exc))
                error_text = str(exc)
                if cleanup_errors:
                    error_text += "；失败清理需要人工核验: " + " | ".join(cleanup_errors)
                    exc = LocalMoveError(error_text)
                self._finish_step(index, "failed", error_text)
                raise exc

    def _restore_one(self, moved: MovedItem) -> None:
        with self._pinned_entry(
            moved.target, self.target_roots
        ) as (_target_display, target), self._pinned_entry(
            moved.source, self.source_roots, create_parent=True
        ) as (_source_display, source):
            if self._identity_or_none(target) is None:
                if self._identity_or_none(source) is not None:
                    return
                raise LocalMoveError(f"回滚时源和目标均不存在: {moved.source.name}")
            self._require_identity(
                target,
                moved.published_identity,
                label="回滚目标",
            )
            if self._identity_or_none(source) is not None:
                raise FileExistsError(f"回滚源路径已被占用: {moved.source}")
            if LocalFilesystemAdapter.same_filesystem(target, source):
                os.replace(target, source)
                self._require_identity(
                    source,
                    moved.published_identity,
                    label="回滚后的源文件",
                )
                return
            size = moved.published_identity[0]
            fingerprint = self._quick_fingerprint(target, size)
            partial = source.with_name(
                f".{source.name}.mediaflux-rollback-{self.operation_token[:12]}"
            )
            try:
                with target.open("rb") as src, partial.open("xb") as dst:
                    shutil.copyfileobj(src, dst, length=self.chunk_size)
                    dst.flush()
                    os.fsync(dst.fileno())
                self._verify_target(fingerprint, partial, size)
                os.replace(partial, source)
                self._unlink_owned(
                    target,
                    moved.published_identity,
                    label="回滚目标",
                )
            finally:
                partial.unlink(missing_ok=True)

    def rollback(self) -> list[str]:
        errors: list[str] = []
        for moved in reversed(self._moved):
            index = self._moved_steps.get(moved.target)
            try:
                self._restore_one(moved)
                backup_record = self._replaced_backups.get(moved.target)
                if backup_record is not None:
                    backup, backup_identity, restore_target = backup_record
                    self._restore_replaced_backup(
                        moved.target,
                        backup,
                        backup_identity,
                        restore_target,
                    )
                if index is not None:
                    self._finish_step(index, "rolled_back")
            except Exception as exc:
                errors.append(f"{moved.target}: {exc}")
                if index is not None:
                    self._finish_step(index, "failed", f"回滚失败: {exc}")
        # 备份可能在新目标发布前就因身份复核失败而产生，此时尚没有
        # MovedItem。继续恢复所有剩余记录，避免旧媒体只留在隐藏文件中。
        for target, backup_record in list(self._replaced_backups.items()):
            backup, backup_identity, restore_target = backup_record
            try:
                self._restore_replaced_backup(
                    target, backup, backup_identity, restore_target,
                )
            except Exception as exc:
                errors.append(f"{restore_target}: {exc}")
        return errors

    def _execute_locked(self, plans: Sequence[MovePlanLike]) -> MoveTransactionResult:
        targets: set[Path] = set()
        retired_targets: set[Path] = set()
        for plan in plans:
            normalized = Path(plan.target).expanduser().resolve(strict=False)
            if normalized in targets:
                raise LocalMoveError(f"移动计划包含重复目标: {normalized.name}")
            targets.add(normalized)
            retire_value = getattr(plan, "retire_target", None)
            if retire_value is not None:
                retired = Path(retire_value).expanduser().resolve(strict=False)
                if retired in retired_targets:
                    raise LocalMoveError(f"移动计划重复替换同一旧版本: {retired.name}")
                retired_targets.add(retired)
        self._moved = []
        self._moved_steps = {}
        self._replaced_backups = {}
        try:
            for index, plan in enumerate(plans, start=1):
                self._move_one(plan, index)
            warnings: list[str] = []
            for target, backup_record in list(self._replaced_backups.items()):
                backup, backup_identity, _restore_target = backup_record
                try:
                    self._unlink_owned_anchored(
                        backup,
                        backup_identity,
                        roots=self.target_roots,
                        label="替换备份",
                    )
                    self._replaced_backups.pop(target, None)
                except Exception as exc:
                    warnings.append(f"旧版本备份清理失败: {backup.name}: {exc}")
            return MoveTransactionResult(
                status="requires_manual" if warnings else "completed",
                moved=list(self._moved),
                warnings=warnings,
            )
        except Exception as exc:
            rollback_errors = self.rollback()
            message = f"本地媒体移动失败: {exc}"
            if rollback_errors:
                message += "；部分回滚失败，需要人工核验: " + " | ".join(rollback_errors)
            error = LocalMoveError(message)
            setattr(error, "rollback_errors", rollback_errors)
            raise error from exc

    def execute(self, plans: Sequence[MovePlanLike]) -> MoveTransactionResult:
        if not plans:
            raise ValueError("没有可执行的本地媒体移动计划")
        if not _LOCAL_MEDIA_MOVE_LOCK.acquire(blocking=False):
            raise LocalMoveError("已有本地媒体移动正在进行，请稍后重试")
        try:
            return self._execute_locked(plans)
        finally:
            _LOCAL_MEDIA_MOVE_LOCK.release()
