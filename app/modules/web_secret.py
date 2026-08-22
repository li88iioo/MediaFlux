"""Web session 与签名令牌共用的密钥提供器。"""
from __future__ import annotations

import errno
import os
import secrets
import stat
import threading
from pathlib import Path

from app import config
from app.private_files import protect_private_fd
from app.runtime_paths import RuntimePaths, get_runtime_paths


class WebSecretUnavailable(RuntimeError):
    """已初始化的生产安装缺少 Web Secret，或持久化密钥不安全。"""


_lock = threading.RLock()
_process_secrets: dict[RuntimePaths, str] = {}
_FALLBACK_SECRET_NAME = ".web-secret-key"
_MIN_SECRET_LENGTH = 32
_MAX_SECRET_LENGTH = 256


def configured_web_secret() -> str:
    return config.get("WEB_SECRET_KEY", "").strip()


def _is_production() -> bool:
    return config.get("APP_ENV", "development").strip().lower() == "production"


def _fresh_install() -> bool:
    from app.modules.first_run import needs_initialization

    return needs_initialization()


def _validate_secret(value: str) -> str:
    secret = str(value or "").strip()
    if (
        not _MIN_SECRET_LENGTH <= len(secret) <= _MAX_SECRET_LENGTH
        or not secret.isascii()
        or any(ord(char) < 33 or ord(char) > 126 for char in secret)
    ):
        raise WebSecretUnavailable("持久化 Web Secret 格式无效")
    return secret


def _open_flags(base: int) -> int:
    return base | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _read_fallback_secret(path: Path) -> str:
    try:
        if os.name != "posix":
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise WebSecretUnavailable("持久化 Web Secret 不是安全普通文件")
        descriptor = os.open(path, _open_flags(os.O_RDONLY))
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise WebSecretUnavailable("无法安全读取持久化 Web Secret") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not protect_private_fd(descriptor):
            raise WebSecretUnavailable("持久化 Web Secret 文件权限不安全")
        payload = os.read(descriptor, _MAX_SECRET_LENGTH + 2)
        if len(payload) > _MAX_SECRET_LENGTH + 1:
            raise WebSecretUnavailable("持久化 Web Secret 过长")
        try:
            return _validate_secret(payload.decode("ascii").strip())
        except UnicodeDecodeError as exc:
            raise WebSecretUnavailable("持久化 Web Secret 格式无效") from exc
    finally:
        os.close(descriptor)


def _persist_fallback_secret(path: Path, value: str) -> str:
    secret = _validate_secret(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    flags = _open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        descriptor = os.open(temporary, flags, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        raise WebSecretUnavailable("无法创建持久化 Web Secret 临时文件") from exc
    try:
        if os.name == "nt":
            config._apply_private_permissions(temporary)
        elif not protect_private_fd(descriptor):
            raise WebSecretUnavailable("无法收紧持久化 Web Secret 文件权限")
        payload = (secret + "\n").encode("ascii")
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("short write")
            written += count
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    else:
        os.close(descriptor)

    try:
        # Hard-link publication is atomic and no-clobber: losers only observe a
        # fully written inode, never the winner's transient zero-byte file.
        os.link(temporary, path, follow_symlinks=False)
        if os.name == "nt":
            config._apply_private_permissions(path)
    except FileExistsError:
        return _read_fallback_secret(path)
    except OSError as exc:
        unsupported = {
            errno.EPERM, errno.EXDEV, errno.ENOSYS,
            getattr(errno, "EOPNOTSUPP", errno.EPERM),
            getattr(errno, "ENOTSUP", errno.EPERM),
        }
        if exc.errno not in unsupported:
            raise WebSecretUnavailable("无法原子发布持久化 Web Secret") from exc
        try:
            # 某些容器卷/网络盘禁止 hard-link，但仍支持原子的 no-replace rename。
            config._publish_noreplace(temporary, path)
            if os.name == "nt":
                config._apply_private_permissions(path)
        except FileExistsError:
            return _read_fallback_secret(path)
        except (OSError, config.AtomicPublishError) as publish_exc:
            raise WebSecretUnavailable("无法原子发布持久化 Web Secret") from publish_exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return secret


def get_web_secret() -> str:
    """返回外部配置密钥；缺省时使用跨进程稳定的私有回退文件。"""
    configured = configured_web_secret()
    if configured:
        return configured
    if not _fresh_install() and _is_production():
        raise WebSecretUnavailable("生产模式已初始化安装必须配置 WEB_SECRET_KEY")

    paths = get_runtime_paths()
    with _lock:
        cached = _process_secrets.get(paths)
        if cached:
            return cached
        fallback_path = paths.config_dir / _FALLBACK_SECRET_NAME
        persisted = _read_fallback_secret(fallback_path)
        if not persisted:
            persisted = _persist_fallback_secret(
                fallback_path, secrets.token_urlsafe(48)
            )
        _process_secrets[paths] = persisted
        return persisted
