"""线程与跨进程共用的文件互斥锁。

MediaFlux 的整理与 STRM 写操作可能由 Web、Telegram、定时器或独立
ASGI 进程触发。仅使用 ``threading.Lock`` 无法阻止多个进程同时修改同一
远端目录或 STRM 根目录，因此这里提供与 ``threading.Lock`` 相同的最小
``acquire/release`` 接口，并叠加操作系统文件锁。
"""
from __future__ import annotations

import errno
import os
import threading
from pathlib import Path
from typing import BinaryIO

from app import database as db


class CrossProcessLock:
    """可跨线程释放的进程内 + 文件系统互斥锁。"""

    def __init__(self, name: str, *, directory: str | Path | None = None) -> None:
        safe_name = "".join(
            char if char.isalnum() or char in {"-", "_"} else "-"
            for char in str(name or "operation")
        ).strip("-") or "operation"
        self._name = safe_name
        self._directory = Path(directory).expanduser().resolve() if directory else None
        self._thread_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._handle: BinaryIO | None = None

    @property
    def path(self) -> Path:
        directory = self._directory or db.resolve_db_path().parent
        return directory / f".mediaflux-{self._name}.lock"

    def acquire(self, blocking: bool = True) -> bool:
        if not self._thread_lock.acquire(blocking=blocking):
            return False
        handle: BinaryIO | None = None
        try:
            lock_path = self.path
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a+b")
            self._acquire_file_lock(handle, blocking=blocking)
            with self._state_lock:
                self._handle = handle
            return True
        except (BlockingIOError, PermissionError, OSError) as exc:
            if handle is not None:
                handle.close()
            self._thread_lock.release()
            if self._is_busy_error(exc):
                return False
            raise
        except BaseException:
            # KeyboardInterrupt/SystemExit 也必须释放进程内锁；否则同一实例会永久死锁。
            if handle is not None:
                handle.close()
            self._thread_lock.release()
            raise

    def release(self) -> None:
        with self._state_lock:
            handle = self._handle
            self._handle = None
        if handle is None:
            raise RuntimeError("release unlocked lock")
        try:
            self._release_file_lock(handle)
        finally:
            handle.close()
            self._thread_lock.release()

    @staticmethod
    def _is_busy_error(exc: BaseException) -> bool:
        return getattr(exc, "errno", None) in {
            errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK,
        }

    @staticmethod
    def _acquire_file_lock(handle: BinaryIO, *, blocking: bool) -> None:
        if os.name == "nt":  # pragma: no cover - Windows CI/package runtime
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            msvcrt.locking(handle.fileno(), mode, 1)
            return

        import fcntl

        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        fcntl.flock(handle.fileno(), flags)

    @staticmethod
    def _release_file_lock(handle: BinaryIO) -> None:
        if os.name == "nt":  # pragma: no cover - Windows CI/package runtime
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
