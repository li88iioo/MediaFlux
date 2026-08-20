"""项目自有敏感文件的最小权限收紧工具。

只处理显式传入的普通文件；不会修改目录、符号链接、媒体目录或外部挂载。
Windows 的 POSIX mode 位不代表真实 ACL，因此保持安全 no-op，避免误锁当前用户。
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import IO

_PRIVATE_MODE = stat.S_IRUSR | stat.S_IWUSR


def _is_posix() -> bool:
    return os.name == "posix"


def protect_private_fd(fd: int) -> bool:
    """尽力把已打开的普通文件收紧为 0600。"""
    if not _is_posix():
        return True
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            return False
        os.fchmod(fd, _PRIVATE_MODE)
        return stat.S_IMODE(os.fstat(fd).st_mode) == _PRIVATE_MODE
    except (OSError, ValueError):
        return False


def protect_private_stream(stream: IO[str] | IO[bytes]) -> bool:
    """收紧日志等已打开文件流；调用方可在创建与轮转后重复调用。"""
    try:
        return protect_private_fd(stream.fileno())
    except (AttributeError, OSError, ValueError):
        return False


def protect_private_file(path: str | os.PathLike[str]) -> bool:
    """尽力收紧一个既有普通文件；不存在视为无需处理。"""
    if not _is_posix():
        return True
    target = Path(path)
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return False

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except FileNotFoundError:
        # SQLite 关闭最后一个连接时会删除 -wal/-shm，文件可能在 lstat 与
        # open 之间消失。没有文件就没有需要收紧的权限，不是失败。
        return True
    except OSError:
        return False
    try:
        return protect_private_fd(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def protect_sqlite_files(path: str | os.PathLike[str]) -> bool:
    """收紧 SQLite 主文件及可能存在的 WAL/SHM sidecar。"""
    database = Path(path)
    protected = True
    for candidate in (
        database,
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
    ):
        # 不短路：即使某个文件失败，也继续尝试其余 sidecar。
        protected = protect_private_file(candidate) and protected
    return protected
