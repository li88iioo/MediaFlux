"""安装环境的只读运行时诊断。"""
from __future__ import annotations

import ctypes
import errno
import getpass
import os
import shutil
import socket
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from app.defaults import DEFAULT_WEB_PORT
from app.runtime_paths import RuntimePaths


DiagnosticStatus = Literal["ok", "warning", "error"]
DEFAULT_SERVICE_HOST = "0.0.0.0"
DEFAULT_SERVICE_PORT = DEFAULT_WEB_PORT
_PROBE_PREFIX = ".mediaflux-diagnostic-"
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


@dataclass(frozen=True)
class DiagnosticCheck:
    """单项可序列化诊断结果。"""

    key: str
    status: DiagnosticStatus
    message: str
    suggestion: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("诊断检查键必须是非空字符串")
        if not isinstance(self.status, str) or self.status not in {"ok", "warning", "error"}:
            raise ValueError("诊断状态必须是 ok、warning 或 error")
        if not isinstance(self.message, str) or not isinstance(self.suggestion, str):
            raise TypeError("诊断消息和建议必须是字符串")

    def as_dict(self) -> dict[str, str]:
        """以固定字段顺序返回适合 JSON 的值。"""
        return {
            "key": self.key,
            "status": self.status,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass(frozen=True)
class DiagnosticReport:
    """不可变的、按创建顺序保存的诊断报告。"""

    checks: tuple[DiagnosticCheck, ...]

    def __post_init__(self) -> None:
        checks = tuple(self.checks)
        if any(not isinstance(check, DiagnosticCheck) for check in checks):
            raise TypeError("诊断报告只能包含 DiagnosticCheck")
        if len({check.key for check in checks}) != len(checks):
            raise ValueError("诊断检查键必须唯一")
        object.__setattr__(self, "checks", checks)

    def check(self, key: str) -> DiagnosticCheck:
        """按稳定键取得诊断检查；未知键明确报错。"""
        for diagnostic_check in self.checks:
            if diagnostic_check.key == key:
                return diagnostic_check
        raise KeyError(key)

    def as_dict(self) -> dict[str, list[dict[str, str]]]:
        """以稳定顺序返回结构化报告的副本。"""
        return {"checks": [check.as_dict() for check in self.checks]}


def run_diagnostics(
    paths: RuntimePaths,
    *,
    source_paths: Sequence[Path] = (),
    target_paths: Sequence[Path] = (),
    host: str = DEFAULT_SERVICE_HOST,
    port: int = DEFAULT_SERVICE_PORT,
) -> DiagnosticReport:
    """检查运行目录、媒体路径、端口和可选依赖，不修改系统设置。"""
    return DiagnosticReport(
        (
            _execution_identity_check(),
            _database_check(paths.database_path),
            _aggregate_readability_check(source_paths),
            _aggregate_writability_check(target_paths),
            _port_check(host, port),
            _ffprobe_check(),
            _program_directory_check(paths.program_dir),
        )
    )


def _execution_identity_check() -> DiagnosticCheck:
    """说明诊断仅代表调用它的身份，绝不尝试切换用户。"""
    try:
        username = getpass.getuser()
    except (OSError, KeyError):
        username = "unknown"

    if _is_windows():
        elevated = _windows_elevation_state()
        if elevated is True:
            return DiagnosticCheck(
                "execution_identity",
                "warning",
                f"当前执行身份：{username}（Windows elevated）；结果仅代表提升后的身份。",
                "请以目标服务账户再次运行 doctor，确认实际运行权限。",
            )
        if elevated is False:
            return DiagnosticCheck("execution_identity", "ok", f"当前执行身份：{username}（Windows 非提升）。")
        return DiagnosticCheck(
            "execution_identity",
            "warning",
            f"当前执行身份：{username}（Windows 提升状态未知）。",
            "请以目标服务账户再次运行 doctor，确认实际运行权限。",
        )

    if os.name == "posix":
        uid = os.geteuid()
        if uid == 0:
            return DiagnosticCheck(
                "execution_identity",
                "warning",
                f"当前执行身份：{username}（UID 0/root）；结果仅代表 root。",
                "请以目标服务账户再次运行 doctor，确认实际运行权限。",
            )
        return DiagnosticCheck("execution_identity", "ok", f"当前执行身份：{username}（UID {uid}）。")
    return DiagnosticCheck("execution_identity", "ok", f"当前执行身份：{username}。")


def _is_windows() -> bool:
    return os.name == "nt"


def _windows_elevation_state() -> bool | None:
    """返回当前 Windows token 是否提升；无法可靠判断时返回 None。"""
    if not _is_windows():
        return None
    try:
        from ctypes import wintypes

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
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return None
        try:
            elevation = wintypes.DWORD()
            returned = wintypes.DWORD()
            success = advapi32.GetTokenInformation(
                token,
                20,  # TokenElevation
                ctypes.byref(elevation),
                ctypes.sizeof(elevation),
                ctypes.byref(returned),
            )
            if not success:
                return None
            return bool(elevation.value)
        finally:
            kernel32.CloseHandle(token)
    except (AttributeError, OSError):
        return None


def _database_check(database_path: Path) -> DiagnosticCheck:
    if _uses_dir_fd():
        return _database_check_posix(database_path)
    return _database_check_string(database_path)


def _database_check_posix(database_path: Path) -> DiagnosticCheck:
    parent_fd, parent_path, missing, detail = _open_existing_ancestor(database_path.parent)
    if parent_fd is None or parent_path is None:
        return _database_error(database_path.parent, detail)
    try:
        if missing:
            creatable, detail = _create_probe_at(parent_fd)
            if not creatable:
                return _database_error(database_path.parent, detail)
            return DiagnosticCheck(
                "database_dir",
                "ok",
                f"数据库目录尚未创建但可创建：当前执行身份可在祖先目录 {parent_path} 创建 {database_path.parent}。",
            )

        writable, detail = _write_probe_at(parent_fd)
        if not writable:
            return _database_error(database_path.parent, detail)
        return _database_file_check_at(parent_fd, database_path.name, database_path)
    finally:
        os.close(parent_fd)


def _database_file_check_at(parent_fd: int, name: str, database_path: Path) -> DiagnosticCheck:
    try:
        path_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return DiagnosticCheck(
            "database_dir",
            "ok",
            f"当前执行身份可写数据库目录：{database_path.parent}；数据库文件尚未创建。",
        )
    except OSError as exc:
        return _database_error(database_path.parent, _format_os_error(exc))

    error = _unsafe_or_wrong_kind(path_stat, expected="regular file")
    if error:
        return _database_error(database_path.parent, f"数据库文件 {database_path}：{error}")
    writable, detail = _open_regular_at(parent_fd, name, writable=True)
    if not writable:
        return _database_error(database_path.parent, f"数据库文件 {database_path} 不可安全读写：{detail}")
    return DiagnosticCheck(
        "database_dir",
        "ok",
        f"当前执行身份可写数据库目录：{database_path.parent}，且可安全读写数据库文件。",
    )


def _database_check_string(database_path: Path) -> DiagnosticCheck:
    prepared, detail = _prepare_path(database_path)
    if prepared is None:
        return _database_error(database_path.parent, detail)
    parent = prepared.parent
    parent_stat, missing, detail = _validate_string_components(parent)
    if parent_stat is None:
        ancestor, detail = _nearest_existing_string_directory(parent)
        if ancestor is None:
            return _database_error(parent, detail)
        creatable, detail = _create_probe_in_directory(ancestor)
        if not creatable:
            return _database_error(parent, detail)
        return DiagnosticCheck(
            "database_dir",
            "ok",
            f"数据库目录尚未创建但可创建：当前执行身份可在祖先目录 {ancestor} 创建 {parent}。",
        )
    if missing:
        return _database_error(parent, detail)
    writable, detail = _write_probe_in_directory(parent)
    if not writable:
        return _database_error(parent, detail)
    try:
        database_stat = os.lstat(prepared)
    except FileNotFoundError:
        return DiagnosticCheck("database_dir", "ok", f"当前执行身份可写数据库目录：{parent}；数据库文件尚未创建。")
    except OSError as exc:
        return _database_error(parent, _format_os_error(exc))
    error = _unsafe_or_wrong_kind(database_stat, expected="regular file")
    if error:
        return _database_error(parent, f"数据库文件 {prepared}：{error}")
    writable, detail = _open_regular_path(prepared, writable=True)
    if not writable:
        return _database_error(parent, f"数据库文件 {prepared} 不可安全读写：{detail}")
    return DiagnosticCheck("database_dir", "ok", f"当前执行身份可写数据库目录：{parent}，且可安全读写数据库文件。")


def _database_error(database_dir: Path, detail: str) -> DiagnosticCheck:
    return DiagnosticCheck(
        "database_dir",
        "error",
        f"当前执行身份无法安全使用数据库目录 {database_dir}：{detail}",
        "确认数据目录及数据库文件不是符号链接，并以目标服务账户授予所需读写权限。",
    )


def _aggregate_readability_check(source_paths: Sequence[Path]) -> DiagnosticCheck:
    if not source_paths:
        return DiagnosticCheck(
            "source_readable",
            "warning",
            "当前执行身份未验证 source 路径：未提供 source 参数。",
            "使用 doctor --source PATH 显式验证媒体源路径。",
        )

    failures = []
    for path in source_paths:
        readable, detail = _probe_readable(path)
        if not readable:
            failures.append(_message_with_path("source 路径不可读", path, detail))
    if failures:
        return DiagnosticCheck(
            "source_readable",
            "error",
            "；".join(failures),
            "确认路径不是符号链接，且当前执行身份拥有目录读取和搜索/遍历权限。",
        )
    return DiagnosticCheck("source_readable", "ok", "当前执行身份可读取所有提供的 source 路径。")


def _aggregate_writability_check(target_paths: Sequence[Path]) -> DiagnosticCheck:
    if not target_paths:
        return DiagnosticCheck(
            "library_writable",
            "warning",
            "当前执行身份未验证媒体库路径：未提供 target 参数。",
            "使用 doctor --target PATH 显式验证媒体库写入路径。",
        )

    failures = []
    for path in target_paths:
        writable, detail = _probe_writable(path)
        if not writable:
            failures.append(_message_with_path("媒体库路径不可写", path, detail))
    if failures:
        return DiagnosticCheck(
            "library_writable",
            "error",
            "；".join(failures),
            "确认路径不是符号链接，且当前执行身份拥有写入权限。",
        )
    return DiagnosticCheck("library_writable", "ok", "当前执行身份可写入所有提供的媒体库路径。")


def _probe_readable(path: Path) -> tuple[bool, str]:
    if _uses_dir_fd():
        return _probe_readable_posix(path)
    return _probe_readable_string(path)


def _probe_readable_posix(path: Path) -> tuple[bool, str]:
    parent_fd, name, prepared, detail = _open_parent_fd(path)
    if parent_fd is None or name is None or prepared is None:
        return False, detail
    descriptor: int | None = None
    try:
        path_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        error = _unsafe_or_wrong_kind(path_stat, expected=None)
        if error:
            return False, error
        if stat.S_ISDIR(path_stat.st_mode):
            if not _directory_access(prepared, os.R_OK | os.X_OK):
                return False, "当前执行身份缺少目录读取或搜索/遍历权限"
            descriptor = _open_directory_at(parent_fd, name)
            if descriptor is None:
                return False, "无法安全打开目录"
            with os.scandir(descriptor) as entries:
                next(entries, None)
            return True, ""
        return _open_regular_at(parent_fd, name, writable=False)
    except OSError as exc:
        return False, _format_os_error(exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def _probe_writable(path: Path) -> tuple[bool, str]:
    if _uses_dir_fd():
        return _probe_writable_posix(path)
    return _probe_writable_string(path)


def _probe_writable_posix(path: Path) -> tuple[bool, str]:
    parent_fd, name, _prepared, detail = _open_parent_fd(path)
    if parent_fd is None or name is None:
        return False, detail
    descriptor: int | None = None
    try:
        path_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        error = _unsafe_or_wrong_kind(path_stat, expected=None)
        if error:
            return False, error
        if stat.S_ISDIR(path_stat.st_mode):
            descriptor = _open_directory_at(parent_fd, name)
            if descriptor is None:
                return False, "无法安全打开目录"
            return _write_probe_at(descriptor)
        return _open_regular_at(parent_fd, name, writable=True)
    except OSError as exc:
        return False, _format_os_error(exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def _probe_readable_string(path: Path) -> tuple[bool, str]:
    prepared, detail = _prepare_path(path)
    if prepared is None:
        return False, detail
    path_stat, missing, detail = _validate_string_components(prepared)
    if path_stat is None or missing:
        return False, detail or "路径不存在"
    error = _unsafe_or_wrong_kind(path_stat, expected=None)
    if error:
        return False, error
    if stat.S_ISDIR(path_stat.st_mode):
        if os.name == "posix" and not _directory_access(prepared, os.R_OK | os.X_OK):
            return False, "当前执行身份缺少目录读取或搜索/遍历权限"
        try:
            with os.scandir(prepared) as entries:
                next(entries, None)
        except OSError as exc:
            return False, _format_os_error(exc)
        return True, ""
    return _open_regular_path(prepared, writable=False)


def _probe_writable_string(path: Path) -> tuple[bool, str]:
    prepared, detail = _prepare_path(path)
    if prepared is None:
        return False, detail
    path_stat, missing, detail = _validate_string_components(prepared)
    if path_stat is None or missing:
        return False, detail or "路径不存在"
    error = _unsafe_or_wrong_kind(path_stat, expected=None)
    if error:
        return False, error
    if stat.S_ISDIR(path_stat.st_mode):
        return _write_probe_in_directory(prepared)
    return _open_regular_path(prepared, writable=True)


def _program_directory_check(program_dir: Path) -> DiagnosticCheck:
    writable, detail = _probe_writable(program_dir)
    if writable:
        return DiagnosticCheck(
            "program_dir_writable",
            "warning",
            "当前执行身份可写程序目录；生产安装建议保持程序目录只读。",
            "将程序文件目录设为只读，仅向数据目录授予目标服务账户写入权限。",
        )
    if detail == "路径不存在":
        return DiagnosticCheck("program_dir_writable", "ok", "程序目录不存在，当前执行身份未执行写入探测。")
    return DiagnosticCheck("program_dir_writable", "ok", f"当前执行身份未发现程序目录可写权限：{detail}。")


def _port_check(host: str, port: int) -> DiagnosticCheck:
    if _is_port_available(host, port):
        return DiagnosticCheck("default_service_port", "ok", f"当前执行身份可绑定服务端口 {host}:{port}。")
    return DiagnosticCheck(
        "default_service_port",
        "warning",
        f"当前执行身份无法绑定服务端口 {host}:{port}；该端口可能正由服务使用。",
        "确认该端口是否由正在运行的 MediaFlux 服务使用；否则停止占用进程或配置其他端口。",
    )


def _ffprobe_check() -> DiagnosticCheck:
    configured = str(
        os.environ.get("MEDIAFLUX_FFPROBE")
        or ""
    ).strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file() or shutil.which(configured):
            return DiagnosticCheck(
                "ffprobe", "ok", f"已找到显式配置的 ffprobe：{candidate}。"
            )
        return DiagnosticCheck(
            "ffprobe",
            "warning",
            f"显式配置的 ffprobe 不可用：{candidate}。",
            "修正 MEDIAFLUX_FFPROBE，或移除配置后使用系统 PATH。",
        )
    if shutil.which("ffprobe"):
        return DiagnosticCheck("ffprobe", "ok", "在当前执行身份的 PATH 中已找到 ffprobe。")
    return DiagnosticCheck(
        "ffprobe",
        "warning",
        "在当前执行身份的 PATH 中未找到 ffprobe，媒体信息探测功能可能不可用。",
        "安装 FFmpeg，并确保以目标服务账户运行时 ffprobe 位于 PATH 中。",
    )


def _uses_dir_fd() -> bool:
    return os.name == "posix" and not sys.platform.startswith("cygwin")


def _prepare_path(path: Path) -> tuple[Path | None, str]:
    candidate = Path(path)
    if any(part == ".." for part in candidate.parts):
        return None, "路径包含不允许的 .. 组件"
    if candidate.is_absolute():
        return candidate, ""
    return Path(os.getcwd()) / candidate, ""


def _open_parent_fd(path: Path) -> tuple[int | None, str | None, Path | None, str]:
    prepared, detail = _prepare_path(path)
    if prepared is None:
        return None, None, None, detail
    if not prepared.name:
        return None, None, None, "路径必须包含最终组件"
    parent_fd, parent_path, missing, detail = _open_existing_ancestor(prepared.parent)
    if parent_fd is None or parent_path is None:
        return None, None, None, detail
    if missing:
        os.close(parent_fd)
        return None, None, None, "路径不存在"
    return parent_fd, prepared.name, prepared, ""


def _open_existing_ancestor(path: Path) -> tuple[int | None, Path | None, tuple[str, ...], str]:
    prepared, detail = _prepare_path(path)
    if prepared is None:
        return None, None, (), detail
    parts = prepared.parts[1:]
    descriptor: int | None = None
    try:
        descriptor = os.open(prepared.anchor or "/", _directory_open_flags())
        current = Path(prepared.anchor or "/")
        for index, component in enumerate(parts):
            try:
                component_stat = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return descriptor, current, tuple(parts[index:]), ""
            error = _unsafe_or_wrong_kind(component_stat, expected="directory")
            if error:
                os.close(descriptor)
                return None, None, (), f"祖先路径 {current / component}：{error}"
            child = os.open(component, _directory_open_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            current = current / component
        return descriptor, current, (), ""
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        return None, None, (), _format_os_error(exc)


def _open_directory_at(parent_fd: int, name: str) -> int | None:
    descriptor: int | None = None
    try:
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            descriptor = None
            return None
        return descriptor
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        return None


def _directory_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _regular_open_flags(*, writable: bool) -> int:
    flags = os.O_RDWR if writable else os.O_RDONLY
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _open_regular_at(parent_fd: int, name: str, *, writable: bool) -> tuple[bool, str]:
    descriptor: int | None = None
    try:
        descriptor = os.open(name, _regular_open_flags(writable=writable), dir_fd=parent_fd)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return False, "打开时不是普通文件"
    except OSError as exc:
        return False, _format_os_error(exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return True, ""


def _open_regular_path(path: Path, *, writable: bool) -> tuple[bool, str]:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, _regular_open_flags(writable=writable))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return False, "打开时不是普通文件"
    except OSError as exc:
        return False, _format_os_error(exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return True, ""


def _write_probe_at(directory_fd: int) -> tuple[bool, str]:
    name = f"{_PROBE_PREFIX}{uuid.uuid4().hex}"
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(
            name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        created = True
        os.close(descriptor)
        descriptor = None
        os.unlink(name, dir_fd=directory_fd)
        created = False
    except OSError as exc:
        return False, _format_os_error(exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError:
                pass
    return True, ""


def _create_probe_at(directory_fd: int) -> tuple[bool, str]:
    name = f"{_PROBE_PREFIX}{uuid.uuid4().hex}"
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=directory_fd)
        created = True
        os.rmdir(name, dir_fd=directory_fd)
        created = False
    except OSError as exc:
        return False, _format_os_error(exc)
    finally:
        if created:
            try:
                os.rmdir(name, dir_fd=directory_fd)
            except OSError:
                pass
    return True, ""


def _validate_string_components(path: Path) -> tuple[os.stat_result | None, bool, str]:
    prepared, detail = _prepare_path(path)
    if prepared is None:
        return None, False, detail
    current = Path(prepared.anchor)
    parts = prepared.parts[1:]
    for index, component in enumerate(parts):
        current = current / component
        try:
            path_stat = os.lstat(current)
        except FileNotFoundError:
            return None, True, "路径不存在"
        except OSError as exc:
            return None, False, _format_os_error(exc)
        error = _unsafe_or_wrong_kind(path_stat, expected="directory" if index < len(parts) - 1 else None)
        if error:
            return None, False, f"祖先路径 {current}：{error}"
    try:
        return os.lstat(prepared), False, ""
    except OSError as exc:
        return None, False, _format_os_error(exc)


def _nearest_existing_string_directory(path: Path) -> tuple[Path | None, str]:
    prepared, detail = _prepare_path(path)
    if prepared is None:
        return None, detail
    candidate = prepared
    while True:
        path_stat, missing, detail = _validate_string_components(candidate)
        if path_stat is not None and not missing:
            error = _unsafe_or_wrong_kind(path_stat, expected="directory")
            if error:
                return None, error
            return candidate, ""
        if not missing:
            return None, detail
        parent = candidate.parent
        if parent == candidate:
            return None, "找不到可创建数据库目录的现有祖先目录"
        candidate = parent


def _write_probe_in_directory(path: Path) -> tuple[bool, str]:
    import tempfile

    descriptor: int | None = None
    probe_path: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=_PROBE_PREFIX, dir=str(path))
        probe_path = Path(name)
        os.close(descriptor)
        descriptor = None
        probe_path.unlink()
        probe_path = None
    except OSError as exc:
        return False, _format_os_error(exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if probe_path is not None:
            try:
                probe_path.unlink()
            except OSError:
                pass
    return True, ""


def _create_probe_in_directory(path: Path) -> tuple[bool, str]:
    import tempfile

    probe_path: Path | None = None
    try:
        probe_path = Path(tempfile.mkdtemp(prefix=_PROBE_PREFIX, dir=str(path)))
        probe_path.rmdir()
        probe_path = None
    except OSError as exc:
        return False, _format_os_error(exc)
    finally:
        if probe_path is not None:
            try:
                probe_path.rmdir()
            except OSError:
                pass
    return True, ""


def _unsafe_or_wrong_kind(path_stat: os.stat_result, *, expected: str | None) -> str:
    if stat.S_ISLNK(path_stat.st_mode) or _is_windows_reparse_point(path_stat):
        return "路径是符号链接或 Windows 重解析点，当前执行身份不会跟随探测"
    if stat.S_ISDIR(path_stat.st_mode):
        return "" if expected in (None, "directory") else "不是普通文件"
    if stat.S_ISREG(path_stat.st_mode):
        return "" if expected in (None, "regular file") else "不是目录"
    return "路径是特殊文件，当前执行身份不会打开或探测"


def _is_windows_reparse_point(path_stat: os.stat_result) -> bool:
    attributes = getattr(path_stat, "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT)


def _directory_access(path: Path, mode: int) -> bool:
    """在 POSIX 上优先按有效身份检查，并始终配合已打开 fd 的 scandir 使用。"""
    try:
        return os.access(path, mode, effective_ids=True)
    except (NotImplementedError, TypeError):
        try:
            return os.access(path, mode)
        except OSError:
            return False
    except OSError:
        return False


def _is_port_available(host: str, port: int) -> bool:
    """模拟 asyncio/Uvicorn 的多地址绑定语义，并在返回前关闭所有 socket。"""
    normalized_host = host.strip("[]")
    try:
        addresses = socket.getaddrinfo(
            normalized_host,
            port,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
            flags=socket.AI_PASSIVE,
        )
    except OSError:
        return False

    unique_addresses = []
    seen = set()
    for address in addresses:
        family, socket_type, protocol, _canonical_name, sockaddr = address
        key = (family, socket_type, protocol, sockaddr)
        if key not in seen:
            seen.add(key)
            unique_addresses.append(address)

    listeners = []
    try:
        for family, socket_type, protocol, _canonical_name, sockaddr in unique_addresses:
            if family not in (socket.AF_INET, socket.AF_INET6):
                continue
            try:
                listener = socket.socket(family, socket_type, protocol)
            except OSError:
                continue
            try:
                if _uses_dir_fd():
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
                    listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                listener.bind(sockaddr)
            except OSError as exc:
                listener.close()
                if exc.errno == errno.EADDRNOTAVAIL:
                    continue
                return False
            listeners.append(listener)
        return bool(listeners)
    finally:
        for listener in listeners:
            listener.close()


def _message_with_path(prefix: str, path: Path, detail: str) -> str:
    message = f"当前执行身份的 {prefix}：{path}"
    if detail:
        message = f"{message}（{detail}）"
    return message


def _format_os_error(error: OSError) -> str:
    return error.strerror or str(error)
