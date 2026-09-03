"""统一配置管理。"""
from __future__ import annotations

import ctypes
import errno
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from app.env_file import read_env_bytes as _read_env_bytes
from app.defaults import DEFAULT_BOOL_CONFIG_VALUES, DEFAULT_WEB_PORT
from app.runtime_paths import get_runtime_paths

# 程序目录仅供读取；运行数据统一由 RuntimePaths 指定。
PATHS = get_runtime_paths()
BASE_DIR = PATHS.program_dir
DATA_DIR = PATHS.data_dir
# 兼容既有调用方：DB_DIR 过去承载所有可写状态的根目录。
DB_DIR = DATA_DIR
CONFIG_DIR = PATHS.config_dir
ENV_FILE = PATHS.env_file
TEMPLATE_ENV = PATHS.program_dir / ".env.example"

_lock = threading.RLock()
_cache: dict[str, str] | None = None
# 进程启动时已有的非空变量属于部署环境覆盖；应用内部后续写入不能冒充或覆盖它们。
_STARTUP_ENV_OVERRIDES = frozenset(key for key, value in os.environ.items() if value)
_logger = logging.getLogger(__name__)
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LITERAL_MARKER = " # mediaflux-literal"
_LINE_SEPARATORS = frozenset("\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029")
_NON_OWNER_MODE = stat.S_IRWXG | stat.S_IRWXO
_PRIVATE_PERMISSION_FALLBACK_DEVICES: set[int] = set()


class AtomicPublishError(RuntimeError):
    """配置文件无法以安全原子语义发布。"""


class ConcurrentConfigUpdateError(AtomicPublishError):
    """配置文件自快照后已变化，拒绝静默覆盖。"""


class UnsafeConfigFileError(AtomicPublishError):
    """配置路径不是可安全处理的单链接普通文件。"""


class ExternalConfigOverrideError(AtomicPublishError):
    """目标配置由进程启动环境覆盖，禁止运行时写入改变其语义。"""


class CorruptConfigFileError(AtomicPublishError):
    """配置文件内容损坏，必须由用户手动恢复。"""


class ConfigFileTooLargeError(AtomicPublishError):
    """配置文件超过调用方允许的安全读取上限。"""


@dataclass(frozen=True)
class EnvUpdateResult(Mapping[str, str]):
    """配置发布结果；携带运行值以及实际落盘的精确字节快照。"""

    data: dict[str, str]
    payload: bytes

    def __getitem__(self, key: str) -> str:
        return self.data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.data)

    def __len__(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class SensitiveCleanupResult:
    safe: bool
    scrubbed: bool
    removed: bool


def _contains_line_separator(value: str) -> bool:
    return any(character in _LINE_SEPARATORS for character in value)


def _validate_env_item(key: str, value: str) -> None:
    if not _ENV_KEY_RE.fullmatch(key):
        raise ValueError(f"非法配置键: {key!r}")
    if "\0" in value or _contains_line_separator(value):
        raise ValueError(f"配置值不能包含空字符或换行分隔符: {key}")


def _serialize_env(values: Mapping[str, str]) -> bytes:
    lines: list[str] = []
    for raw_key, raw_value in values.items():
        key = str(raw_key)
        value = str(raw_value)
        _validate_env_item(key, value)
        # 应用写入值使用显式 literal 标记；读取时不执行 shell/dotenv 插值。
        escaped = value.replace("'", "\\'")
        lines.append(f"{key}='{escaped}'{_LITERAL_MARKER}")
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def _read_env_file(path: Path | None = None) -> dict[str, str]:
    """解析正式 user.env；不会隐式修改 os.environ。"""
    env_file = Path(path or ENV_FILE)
    try:
        _payload, values = read_env_snapshot(env_file)
    except FileNotFoundError:
        return {}
    except (OSError, AtomicPublishError) as exc:
        _logger.critical("拒绝加载不安全或损坏的配置文件: %s (%s)", env_file, exc)
        return {}
    return values


def _ensure_loaded() -> dict[str, str]:
    global _cache
    if _cache is None:
        with _lock:
            if _cache is None:
                _cache = _read_env_file(ENV_FILE)
    return _cache


def get(key: str, default: str = "") -> str:
    """取配置项：外部环境变量优先，其次 user.env，最后运行时默认值。"""
    val = os.getenv(key)
    if val:
        return val
    loaded = _ensure_loaded()
    if key in loaded:
        return loaded[key]
    if key == "STRM_ROOT" and default == "":
        return str(PATHS.strm_dir)
    return default


def get_many(
    keys: Iterator[str] | list[str] | tuple[str, ...],
    defaults: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """在同一运行时配置快照中读取多个键，避免热更新期间混用新旧值。"""
    normalized = tuple(dict.fromkeys(str(key) for key in keys))
    fallback = {str(key): str(value) for key, value in dict(defaults or {}).items()}
    with _lock:
        # 复用单键读取语义，避免两套优先级规则随时间漂移；外层 RLock
        # 保证由 MediaFlux 发起的热更新不能插入多键读取中间。
        return {key: get(key, fallback.get(key, "")) for key in normalized}


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(get(key, str(default)) or default)
    except ValueError:
        return default


def get_bool(key: str, default: bool | None = None) -> bool:
    """读取布尔配置；已登记键在未显式配置时使用唯一权威默认值。"""
    if default is None:
        default = DEFAULT_BOOL_CONFIG_VALUES.get(key, False)
    v = get(key, str(bool(default))).strip().lower()
    return v in ("1", "true", "yes", "on", "y")


_SYSTEM_ENV_SNAPSHOT = dict(os.environ)


def _is_windows() -> bool:
    return os.name == "nt"


def _windows_system_env() -> dict[str, str]:
    # 统一键名大写，防止 Windows 环境块因大小写重复键名解析崩溃
    env: dict[str, str] = {}
    for source in (_SYSTEM_ENV_SNAPSHOT, os.environ):
        for k, v in source.items():
            if not isinstance(v, str):
                continue
            if "%" in k or "(" in k or ")" in k or "=" in k:
                continue
            env[k.upper()] = v

    system_root = (
        env.get("SYSTEMROOT")
        or env.get("WINDIR")
        or r"C:\Windows"
    )
    system_drive = (
        env.get("SYSTEMDRIVE")
        or (os.path.splitdrive(system_root)[0] if os.path.splitdrive(system_root)[0] else "C:")
    )
    env["SYSTEMROOT"] = system_root
    env["WINDIR"] = system_root
    env["SYSTEMDRIVE"] = system_drive
    env.setdefault("PATHEXT", ".COM;.EXE;.BAT;.CMD;.VBS;.JS;.WS")
    if not env.get("PATH"):
        env["PATH"] = rf"{system_root}\System32;{system_root}"
    if not env.get("COMSPEC"):
        env["COMSPEC"] = rf"{system_root}\System32\cmd.exe"
    temp = env.get("TEMP") or env.get("TMP") or rf"{system_root}\Temp"
    env["TEMP"] = temp
    env["TMP"] = temp
    env.setdefault("ALLUSERSPROFILE", rf"{system_drive}\ProgramData")
    env.setdefault("PROGRAMDATA", rf"{system_drive}\ProgramData")
    return env


def _find_windows_icacls() -> str:
    system_root = (
        os.environ.get("SystemRoot")
        or os.environ.get("SYSTEMROOT")
        or os.environ.get("WINDIR")
        or r"C:\Windows"
    )
    candidate = Path(system_root) / "System32" / "icacls.exe"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("icacls")
    if found:
        return found
    return "icacls"


def _windows_current_user_sid() -> str:
    """读取当前进程令牌用户 SID，确保自定义服务账户可继续读取配置。"""
    from ctypes import wintypes

    token_query = 0x0008
    token_user_class = 1

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [
            ("sid", wintypes.LPVOID),
            ("attributes", wintypes.DWORD),
        ]

    class TokenUser(ctypes.Structure):
        _fields_ = [("user", SidAndAttributes)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
    ):
        raise AtomicPublishError("无法读取 Windows 当前服务账户令牌")
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token, token_user_class, None, 0, ctypes.byref(required)
        )
        if required.value <= 0:
            raise AtomicPublishError("无法读取 Windows 当前服务账户 SID 大小")
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token, token_user_class, buffer, required.value, ctypes.byref(required)
        ):
            raise AtomicPublishError("无法读取 Windows 当前服务账户 SID")
        token_user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(
            token_user.user.sid, ctypes.byref(sid_text)
        ):
            raise AtomicPublishError("无法序列化 Windows 当前服务账户 SID")
        try:
            value = str(sid_text.value or "").strip()
        finally:
            if sid_text:
                kernel32.LocalFree(ctypes.cast(sid_text, wintypes.HLOCAL))
        if not re.fullmatch(r"S-\d+(?:-\d+)+", value, flags=re.IGNORECASE):
            raise AtomicPublishError("Windows 当前服务账户 SID 格式无效")
        return value
    finally:
        kernel32.CloseHandle(token)


def _apply_private_permissions(path: Path) -> None:
    """在发布前收紧 temp 文件权限；Windows ACL 失败必须安全失败。"""
    if not _is_windows():
        descriptor = _open_verified_config_file(path, writable=True)
        try:
            try:
                os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
            except OSError as exc:
                if exc.errno != errno.EPERM:
                    raise
                metadata = os.fstat(descriptor)
                mode = stat.S_IMODE(metadata.st_mode)
                if mode & _NON_OWNER_MODE:
                    raise
                # 部分 NAS/CIFS/FUSE 挂载拒绝 chmod，但仍能正确保留创建时
                # 的私有 mode。按设备仅提示一次，避免每次配置保存打印三遍。
                with _lock:
                    should_log = metadata.st_dev not in _PRIVATE_PERMISSION_FALLBACK_DEVICES
                    _PRIVATE_PERMISSION_FALLBACK_DEVICES.add(metadata.st_dev)
                if should_log:
                    _logger.warning(
                        "配置挂载拒绝 fchmod；已验证现有权限仍为私有模式后继续 "
                        "directory=%s mode=%04o errno=%s",
                        path.parent,
                        mode,
                        exc.errno,
                    )
        finally:
            os.close(descriptor)
        return

    descriptor = _open_verified_config_file(path, writable=True)
    os.close(descriptor)

    current_user_sid = None
    try:
        current_user_sid = _windows_current_user_sid()
    except Exception as exc:
        _logger.warning("获取当前用户 SID 异常，使用基础安全 ACL: %s", exc)

    grants = ["*S-1-5-18:(F)", "*S-1-5-32-544:(F)"]
    if current_user_sid:
        current_grant = f"*{current_user_sid}:(F)"
        if current_grant not in grants:
            grants.append(current_grant)

    icacls_bin = _find_windows_icacls()
    command = [
        icacls_bin,
        str(path),
        "/inheritance:r",
        "/grant:r",
        *grants,
    ]
    result = subprocess.run(
        command,
        check=False,
        env=_windows_system_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        result = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if result.returncode != 0:
        raise AtomicPublishError("无法为配置临时文件设置 Windows 私有 ACL")


_WINDOWS_REPARSE_POINT = 0x400


def _validate_config_stat(path: Path, metadata: os.stat_result) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise UnsafeConfigFileError(f"配置路径是符号链接，拒绝处理: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafeConfigFileError(f"配置路径不是普通文件，拒绝处理: {path}")
    if metadata.st_nlink != 1:
        raise UnsafeConfigFileError(f"配置文件存在多个硬链接，拒绝处理: {path}")
    if os.name == "nt":
        attributes = getattr(metadata, "st_file_attributes", None)
        if attributes is None:
            raise UnsafeConfigFileError(f"Windows 无法可靠验证配置文件属性: {path}")
        if attributes & _WINDOWS_REPARSE_POINT:
            raise UnsafeConfigFileError(f"配置路径是 Windows reparse point，拒绝处理: {path}")


def _open_verified_config_file(path: Path, *, writable: bool = False) -> int:
    """不跟随链接打开配置，并在打开前后验证文件身份与链接数。"""
    target = Path(path)
    try:
        before = os.lstat(target)
    except FileNotFoundError:
        raise
    _validate_config_stat(target, before)

    flags = os.O_WRONLY if writable else os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    elif os.name != "nt":
        raise UnsafeConfigFileError(f"当前平台无法禁止跟随配置符号链接: {target}")

    descriptor = os.open(target, flags)
    try:
        opened = os.fstat(descriptor)
        _validate_config_stat(target, opened)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise UnsafeConfigFileError(f"配置文件在安全检查期间已被替换: {target}")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _read_verified_file_bytes(path: Path, *, max_bytes: int | None = None) -> bytes:
    descriptor = _open_verified_config_file(path)
    with os.fdopen(descriptor, "rb") as source:
        if max_bytes is None:
            return source.read()
        limit = max(0, int(max_bytes))
        if os.fstat(source.fileno()).st_size > limit:
            raise ConfigFileTooLargeError(f"配置文件超过安全读取上限: {path}")
        payload = source.read(limit + 1)
        if len(payload) > limit:
            raise ConfigFileTooLargeError(f"配置文件超过安全读取上限: {path}")
        return payload


def _pending_recovery_backups(target: Path) -> list[Path]:
    pattern = f".{target.name}.recovery.*.bak"
    return sorted(target.parent.glob(pattern), key=lambda item: item.name)


def _recover_missing_env_target(target: Path) -> bool:
    """恢复 capture 后进程异常退出留下的唯一私有备份。"""
    if os.path.lexists(target):
        return False
    backups = _pending_recovery_backups(target)
    if not backups:
        return False
    if len(backups) != 1:
        raise CorruptConfigFileError(
            "发现多个 user.env 恢复备份，无法自动判断最新版本；请手动核验"
        )

    backup = backups[0]
    try:
        payload = _read_verified_file_bytes(backup)
        _read_env_bytes(payload)
    except UnicodeError as exc:
        raise CorruptConfigFileError("user.env 恢复备份不是有效的 UTF-8 配置") from exc
    except (OSError, UnsafeConfigFileError) as exc:
        raise CorruptConfigFileError("user.env 恢复备份无法安全读取") from exc

    try:
        _publish_noreplace(backup, target)
    except FileExistsError:
        return False
    except OSError as exc:
        raise AtomicPublishError("无法恢复异常中断前的 user.env") from exc
    _fsync_directory(target.parent)
    _logger.warning("已自动恢复异常中断前的配置文件: %s", target)
    return True


def read_env_snapshot(
    path: Path, *, max_bytes: int | None = None
) -> tuple[bytes | None, dict[str, str]]:
    """安全读取首次运行快照；拒绝链接、特殊文件和非 UTF-8 内容。"""
    target = Path(path)
    try:
        payload = _read_verified_file_bytes(target, max_bytes=max_bytes)
    except FileNotFoundError:
        if not _recover_missing_env_target(target):
            return None, {}
        payload = _read_verified_file_bytes(target, max_bytes=max_bytes)
    try:
        values = _read_env_bytes(payload)
    except UnicodeError as exc:
        raise CorruptConfigFileError("user.env 不是有效的 UTF-8 配置") from exc
    except ValueError as exc:
        raise CorruptConfigFileError(str(exc)) from exc
    return payload, values


def _scrub_verified_file(path: Path) -> bool:
    """仅清空经验证的单链接普通文件。"""
    try:
        descriptor = _open_verified_config_file(path, writable=True)
    except (OSError, UnsafeConfigFileError):
        return False
    try:
        # 打开后再次确认链接数，避免对已出现额外硬链接的 inode 截断。
        _validate_config_stat(path, os.fstat(descriptor))
        os.ftruncate(descriptor, 0)
        os.fsync(descriptor)
        return True
    except (OSError, UnsafeConfigFileError):
        return False
    finally:
        os.close(descriptor)


def _best_effort_scrub_and_unlink(path: Path) -> SensitiveCleanupResult:
    """仅清理安全普通文件，并返回可诊断结果。"""
    target = Path(path)
    try:
        _open = _open_verified_config_file(target)
    except FileNotFoundError:
        return SensitiveCleanupResult(safe=True, scrubbed=True, removed=True)
    except (OSError, UnsafeConfigFileError) as exc:
        _logger.critical("拒绝清理不安全的敏感配置文件，已保留: %s (%s)", target, exc)
        return SensitiveCleanupResult(safe=False, scrubbed=False, removed=False)
    else:
        os.close(_open)

    scrubbed = _scrub_verified_file(target)
    removed = False
    try:
        # unlink 前再次验证，绝不删除检查期间被替换的路径。
        descriptor = _open_verified_config_file(target)
        os.close(descriptor)
        target.unlink(missing_ok=True)
        removed = True
    except FileNotFoundError:
        removed = True
    except (OSError, UnsafeConfigFileError):
        removed = False
    if not scrubbed and not removed:
        _logger.critical("敏感配置文件既无法清空也无法删除，已保留: %s", target)
    elif not removed:
        _logger.warning("敏感配置文件已清空但无法删除: %s", target)
    return SensitiveCleanupResult(safe=True, scrubbed=scrubbed, removed=removed)


def _create_private_temp(target: Path) -> tuple[Path, int]:
    target.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(32):
        temporary = target.parent / f".{target.name}.{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                stat.S_IRUSR | stat.S_IWUSR,
            )
        except FileExistsError:
            continue
        try:
            _apply_private_permissions(temporary)
        except Exception:
            os.close(descriptor)
            _best_effort_scrub_and_unlink(temporary)
            raise
        return temporary, descriptor
    raise AtomicPublishError("无法创建唯一配置临时文件")


def _write_private_temp(target: Path, payload: bytes) -> Path:
    temporary, descriptor = _create_private_temp(target)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
            try:
                # 发布前再次验证权限；失败时利用仍打开的句柄先清空秘密。
                _apply_private_permissions(temporary)
            except Exception:
                try:
                    output.seek(0)
                    output.truncate(0)
                    output.flush()
                    os.fsync(output.fileno())
                except OSError:
                    pass
                raise
        return temporary
    except Exception:
        _best_effort_scrub_and_unlink(temporary)
        raise


_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = {
    errno.EINVAL,
    errno.ENOTSUP,
    errno.EOPNOTSUPP,
}


def _log_directory_fsync_error(directory: Path, exc: OSError) -> None:
    if exc.errno in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
        return
    _logger.critical(
        "配置已发布，但目录 fsync 失败，需检查存储可靠性: %s (%s)",
        directory,
        exc,
    )


def _fsync_directory(directory: Path) -> str:
    if os.name == "nt":
        return "unsupported"
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        _log_directory_fsync_error(directory, exc)
        return "unsupported" if exc.errno in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS else "error"
    status = "ok"
    try:
        os.fsync(descriptor)
    except OSError as exc:
        _log_directory_fsync_error(directory, exc)
        status = "unsupported" if exc.errno in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS else "error"
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            _log_directory_fsync_error(directory, exc)
            if exc.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
                status = "error"
    return status


def _linux_rename_noreplace(temporary: Path, target: Path) -> None:
    """使用 Linux renameat2(RENAME_NOREPLACE) 原子发布。"""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (AttributeError, OSError) as exc:
        raise AtomicPublishError("当前 Linux 运行库不支持 renameat2 no-replace") from exc
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(temporary),
        -100,
        os.fsencode(target),
        1,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), str(target))
    if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP}:
        raise AtomicPublishError("当前 Linux 文件系统不支持安全 no-replace 配置发布")
    raise OSError(error, os.strerror(error), str(target))


def _windows_rename_noreplace(temporary: Path, target: Path) -> None:
    """使用 Windows MoveFileExW(flags=0) 原子移动且绝不覆盖目标。"""
    if os.name != "nt":  # 仅供跨平台契约测试；真实 Windows 始终走 Win32 API。
        os.rename(temporary, target)
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file = kernel32.MoveFileExW
    move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    move_file.restype = ctypes.c_int
    if move_file(str(temporary), str(target), 0):
        return
    error = ctypes.get_last_error()
    if error in {80, 183}:
        raise FileExistsError(error, "目标配置已存在", str(target))
    raise OSError(error, ctypes.FormatError(error), str(target))


def _publish_noreplace(temporary: Path, target: Path) -> None:
    if _is_windows():
        _windows_rename_noreplace(temporary, target)
        return
    if sys.platform.startswith("linux"):
        _linux_rename_noreplace(temporary, target)
        return
    raise AtomicPublishError("当前平台不支持安全 create-only 配置发布")


def write_env_file(path: Path, values: Mapping[str, str], *, replace: bool) -> bytes:
    """原子发布完整 user.env；create-only 使用平台原生 no-replace。"""
    target = Path(path)
    payload = _serialize_env(values)
    temporary = _write_private_temp(target, payload)
    published = False
    try:
        if replace:
            os.replace(temporary, target)
        else:
            _publish_noreplace(temporary, target)
        published = True
        _fsync_directory(target.parent)
        return payload
    finally:
        if not published:
            _best_effort_scrub_and_unlink(temporary)


def _recovery_backup_path(target: Path) -> Path:
    return target.parent / f".{target.name}.recovery.{secrets.token_hex(16)}.bak"


def _capture_existing_target(target: Path) -> Path:
    """把当前目标原子移到唯一备份；不会覆盖既有备份。"""
    # 在任何 chmod、读取或截断之前拒绝链接、特殊文件和多硬链接。
    descriptor = _open_verified_config_file(target)
    os.close(descriptor)
    for _ in range(32):
        backup = _recovery_backup_path(target)
        try:
            _publish_noreplace(target, backup)
        except FileExistsError:
            continue
        except FileNotFoundError as exc:
            raise ConcurrentConfigUpdateError("配置文件在恢复事务开始前已被移除") from exc
        try:
            descriptor = _open_verified_config_file(backup)
            os.close(descriptor)
            _apply_private_permissions(backup)
        except BaseException:
            _restore_captured_target(backup, target)
            raise
        return backup
    raise AtomicPublishError("无法创建唯一配置恢复备份")


def _restore_captured_target(backup: Path, target: Path) -> bool:
    """仅在目标仍为空时恢复；绝不覆盖竞争者创建的目标。"""
    try:
        _publish_noreplace(backup, target)
    except FileExistsError:
        _logger.critical("竞争者配置已占用目标，保留私有恢复备份: %s", backup)
        return False
    except OSError as exc:
        _logger.critical("恢复原配置失败，保留私有恢复备份: %s (%s)", backup, exc)
        return False
    return True


def _transactional_recovery_update(
    target: Path,
    merged: Mapping[str, str],
    *,
    expected: bytes,
) -> bytes:
    """capture→verify→no-replace publish；任何竞争都不覆盖目标。"""
    payload = _serialize_env(merged)
    temporary = _write_private_temp(target, payload)
    backup: Path | None = None
    published = False
    try:
        backup = _capture_existing_target(target)
        captured = _read_verified_file_bytes(backup)
        if captured != expected:
            restored = _restore_captured_target(backup, target)
            if restored:
                backup = None
            raise ConcurrentConfigUpdateError("配置文件自启动快照后已变化")
        try:
            _publish_noreplace(temporary, target)
        except FileExistsError as exc:
            # 竞争者在捕获后创建了新目标；保留其目标和旧配置备份。
            raise ConcurrentConfigUpdateError("配置文件在恢复发布期间被其他进程创建") from exc
        published = True
        fsync_status = _fsync_directory(target.parent)
        if fsync_status == "error":
            _logger.critical(
                "配置目录持久化状态不可靠，保留私有恢复备份: %s",
                backup,
            )
        else:
            _best_effort_scrub_and_unlink(backup)
        backup = None
        return payload
    except BaseException:
        if backup is not None and backup.exists() and not target.exists():
            if _restore_captured_target(backup, target):
                backup = None
        raise
    finally:
        if not published:
            _best_effort_scrub_and_unlink(temporary)


def update_env_file(
    path: Path,
    updates: Mapping[str, str],
    *,
    expected: bytes | None,
) -> EnvUpdateResult:
    """基于启动快照安全合并配置，绝不以 replace 覆盖竞争者。"""
    target = Path(path)
    with _lock:
        if expected is not None:
            try:
                current = _read_env_bytes(expected)
            except UnicodeError as exc:
                raise CorruptConfigFileError(
                    "user.env 内容损坏；请先备份并移走该文件后再初始化"
                ) from exc
        else:
            current = {}
        merged = dict(current)
        merged.update({str(key): str(value) for key, value in updates.items()})
        if expected is None:
            try:
                payload = write_env_file(target, merged, replace=False)
            except FileExistsError as exc:
                raise ConcurrentConfigUpdateError(
                    "配置文件在初始化期间被其他进程创建"
                ) from exc
        else:
            payload = _transactional_recovery_update(target, merged, expected=expected)
        return EnvUpdateResult(dict(merged), payload)


def _apply_runtime_values(values: Mapping[str, str], *, path: Path) -> None:
    global _cache
    normalized = {str(key): str(value) for key, value in values.items()}
    with _lock:
        for key, value in normalized.items():
            os.environ[key] = value
        if Path(path) == Path(ENV_FILE):
            _cache = dict(normalized)


def has_external_override(key: str) -> bool:
    """判断配置项是否由进程启动环境显式覆盖；不返回覆盖值。"""
    normalized = str(key or "")
    return normalized in _STARTUP_ENV_OVERRIDES and bool(os.environ.get(normalized))


def update_runtime_env_file(
    path: Path,
    updates: Mapping[str, str],
    *,
    expected: bytes | None,
) -> EnvUpdateResult:
    """CAS 发布配置，并只把本次非部署覆盖项应用到当前进程。"""
    global _cache
    normalized = {str(key): str(value) for key, value in updates.items()}
    target = Path(path)

    def publish() -> EnvUpdateResult:
        global _cache
        with _lock:
            if any(has_external_override(key) for key in normalized):
                raise ExternalConfigOverrideError("目标配置由运行环境覆盖")
            result = update_env_file(target, normalized, expected=expected)
            if target == Path(ENV_FILE):
                _cache = dict(result.data)
            for key, value in normalized.items():
                os.environ[key] = value
            return result

    if target != Path(ENV_FILE):
        return publish()
    from app.modules.backup import config_snapshot_guard

    with config_snapshot_guard(PATHS):
        return publish()


def reload_after_restore() -> None:
    """恢复事务替换 user.env 后丢弃旧的进程内文件快照。"""
    global _cache
    with _lock:
        _cache = None


def set_and_save(updates: dict[str, str]) -> None:
    """基于读取快照 CAS 更新当前 user.env，拒绝覆盖并发写入。"""
    normalized = {str(key): str(value) for key, value in updates.items()}
    with _lock:
        externally_managed = sorted(
            key for key in normalized if has_external_override(key)
        )
        if externally_managed:
            raise ExternalConfigOverrideError(
                "目标配置由运行环境覆盖: " + ", ".join(externally_managed[:5])
            )
        expected, _current = read_env_snapshot(ENV_FILE)
        update_runtime_env_file(ENV_FILE, normalized, expected=expected)


def all_items() -> dict[str, str]:
    """返回全部 user.env 实际值。"""
    return dict(_ensure_loaded())


# ===== 常用配置便捷访问 =====
def web_credentials() -> tuple[str, str]:
    """返回实际配置的 Web 凭据；缺失或空值保持 fail-closed。"""
    values = get_many(("ENV_WEB_PASSPORT", "ENV_WEB_PASSWORD"))
    return values["ENV_WEB_PASSPORT"].strip(), values["ENV_WEB_PASSWORD"]


def flask_port() -> int:
    return get_int("WEB_PORT", DEFAULT_WEB_PORT)
