"""生成本地、脱敏且可复现的 MediaFlux 支持诊断包。"""
from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
import zipfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.defaults import DEFAULT_WEB_PORT
from app.modules.runtime_diagnostics import (
    DiagnosticCheck,
    DiagnosticReport,
    run_diagnostics,
)
from app.private_files import protect_private_file, protect_private_stream
from app.runtime_paths import RuntimePaths
from app.security import redact_config
from app.sensitive_data import is_sensitive_key, redact_sensitive_text
from app.version import BuildInfo


class SupportBundleError(RuntimeError):
    """支持诊断包无法安全生成。"""


_MAX_CONFIG_BYTES = 1024 * 1024
_MAX_LOG_BYTES = 1024 * 1024
_MAX_LOG_LINE_CHARS = 64 * 1024
_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _redact_payload(payload: Any, *, key: str = "") -> Any:
    """递归清理诊断 JSON，避免异常消息或扩展字段绕过脱敏。"""
    if key and payload not in (None, "") and is_sensitive_key(key):
        return "********"
    if isinstance(payload, dict):
        return {
            str(item_key): _redact_payload(item_value, key=str(item_key))
            for item_key, item_value in payload.items()
        }
    if isinstance(payload, (list, tuple)):
        return [_redact_payload(item) for item in payload]
    if isinstance(payload, str):
        return redact_sensitive_text(payload)
    return payload


def _json_bytes(payload: Any) -> bytes:
    safe_payload = _redact_payload(payload)
    rendered = json.dumps(safe_payload, ensure_ascii=False, indent=2, sort_keys=True)
    return f"{rendered}\n".encode()


def _display_path(path: Path) -> str:
    """隐藏常见用户主目录前缀，同时保留足够的排障信息。"""
    value = str(path)
    try:
        home = str(Path.home())
    except RuntimeError:
        return value
    if home and (value == home or value.startswith(home + os.sep)):
        return "~" + value[len(home) :]
    return value


def _has_reparse_point(metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    return bool(attributes & _WINDOWS_REPARSE_POINT)


def _is_link_like(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or _has_reparse_point(metadata)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    left_identity = (left.st_dev, left.st_ino)
    right_identity = (right.st_dev, right.st_ino)
    if os.name == "nt" and (not left.st_ino or not right.st_ino):
        return False
    return left_identity == right_identity


def _absolute_path_without_links(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _ensure_directory_chain(path: Path) -> None:
    """逐级创建缺失目录，并拒绝任一级链接、reparse point 或特殊文件。"""
    absolute = _absolute_path_without_links(path)
    parts = absolute.parts
    if not parts:
        raise SupportBundleError("输出目录无效")
    current = Path(parts[0])
    for part in parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                raise SupportBundleError(
                    f"无法创建支持包输出目录：{type(exc).__name__}"
                ) from exc
            try:
                metadata = current.lstat()
            except OSError as exc:
                raise SupportBundleError(
                    f"无法验证支持包输出目录：{type(exc).__name__}"
                ) from exc
            try:
                os.chmod(current, 0o700)
            except OSError:
                pass
        except OSError as exc:
            raise SupportBundleError(
                f"无法验证支持包输出目录：{type(exc).__name__}"
            ) from exc
        if _is_link_like(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise SupportBundleError("支持包输出目录不能包含链接或特殊文件")


def _validate_directory_chain(path: Path) -> os.stat_result:
    """拒绝输出路径任一已存在的符号链接或 Windows reparse point。"""
    absolute = _absolute_path_without_links(path)
    parts = absolute.parts
    if not parts:
        raise SupportBundleError("输出目录无效")
    current = Path(parts[0])
    for part in parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise SupportBundleError(
                f"无法验证支持包输出目录：{type(exc).__name__}"
            ) from exc
        if _is_link_like(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise SupportBundleError("支持包输出目录不能包含链接或特殊文件")
    try:
        return absolute.lstat()
    except OSError as exc:
        raise SupportBundleError(
            f"无法验证支持包输出目录：{type(exc).__name__}"
        ) from exc


def _open_directory_fd(path: Path) -> int:
    """在 POSIX 上逐级、不跟随链接地打开目录。"""
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise SupportBundleError("当前平台无法安全打开日志目录")
    absolute = _absolute_path_without_links(path)
    parts = absolute.parts
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | os.O_NOFOLLOW
    )
    descriptor = os.open(parts[0], flags)
    try:
        for part in parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _tail_redacted_log(path: Path, limit: int) -> str:
    count = max(1, min(int(limit), 1000))
    if os.name != "posix":
        return "[当前平台为避免目录链接竞态，未收录应用日志]\n"

    directory_fd = -1
    file_fd = -1
    chunks: list[bytes] = []
    start = 0
    try:
        directory_fd = _open_directory_fd(path.parent)
        path_metadata = os.stat(
            path.name, dir_fd=directory_fd, follow_symlinks=False
        )
        if _is_link_like(path_metadata) or not stat.S_ISREG(path_metadata.st_mode):
            return "[应用日志不是普通文件，已跳过]\n"

        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
        )
        file_fd = os.open(path.name, flags, dir_fd=directory_fd)
        opened_metadata = os.fstat(file_fd)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or not _same_identity(path_metadata, opened_metadata)
        ):
            return "[应用日志读取期间发生变化，已跳过]\n"

        start = max(0, int(opened_metadata.st_size) - _MAX_LOG_BYTES)
        os.lseek(file_fd, start, os.SEEK_SET)
        remaining = min(int(opened_metadata.st_size), _MAX_LOG_BYTES)
        while remaining > 0:
            chunk = os.read(file_fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except FileNotFoundError:
        return ""
    except (OSError, SupportBundleError) as exc:
        return f"[无法安全读取应用日志：{type(exc).__name__}]\n"
    finally:
        for descriptor in (file_fd, directory_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    raw = b"".join(chunks)
    truncated = start > 0
    if truncated:
        separator = raw.find(b"\n")
        raw = raw[separator + 1 :] if separator >= 0 else b""
    if b"\x00" in raw:
        return "[应用日志包含二进制内容，已跳过]\n"
    try:
        decoded = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return "[应用日志不是有效 UTF-8，已跳过]\n"

    lines: deque[str] = deque(maxlen=count)
    for raw_line in decoded.splitlines():
        if len(raw_line) > _MAX_LOG_LINE_CHARS:
            lines.append("[超长日志行已跳过]")
        else:
            lines.append(redact_sensitive_text(raw_line))
    prefix = "[日志过长，已截取末尾内容]\n" if truncated else ""
    return prefix + "\n".join(lines) + ("\n" if lines else "")


def _redacted_config(paths: RuntimePaths) -> tuple[dict[str, str], str]:
    try:
        from app.config import read_env_snapshot

        _, values = read_env_snapshot(paths.env_file, max_bytes=_MAX_CONFIG_BYTES)
    except Exception as exc:  # noqa: BLE001 - 配置损坏时仍必须生成最小支持包。
        return {}, type(exc).__name__
    return redact_config(values), ""


def _default_output() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return Path.cwd() / f"mediaflux-support-{stamp}.zip"


def _validate_destination(destination: Path) -> None:
    try:
        metadata = destination.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SupportBundleError(
            f"无法验证支持包输出路径：{type(exc).__name__}"
        ) from exc
    if _is_link_like(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise SupportBundleError("支持包输出路径不能是链接或特殊文件")


def _write_private_archive_posix(destination: Path, files: dict[str, bytes]) -> None:
    directory_fd = -1
    temporary_name = ""
    try:
        directory_fd = _open_directory_fd(destination.parent)
        for _ in range(16):
            candidate = f".{destination.name}.{secrets.token_hex(8)}.tmp"
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                descriptor = os.open(
                    candidate, flags, 0o600, dir_fd=directory_fd
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        else:
            raise SupportBundleError("无法创建唯一的支持包临时文件")

        with os.fdopen(descriptor, "w+b") as temporary:
            if not protect_private_stream(temporary):
                raise SupportBundleError("无法收紧支持包临时文件权限")
            temporary_metadata = os.fstat(temporary.fileno())
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for name, payload in files.items():
                    archive.writestr(name, payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            if not _same_identity(temporary_metadata, os.fstat(temporary.fileno())):
                raise SupportBundleError("支持包临时文件在写入期间发生变化")

        try:
            existing = os.stat(
                destination.name, dir_fd=directory_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            pass
        else:
            if _is_link_like(existing) or not stat.S_ISREG(existing.st_mode):
                raise SupportBundleError("支持包输出路径不能是链接或特殊文件")

        os.replace(
            temporary_name,
            destination.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = ""
        published = os.stat(
            destination.name, dir_fd=directory_fd, follow_symlinks=False
        )
        if (
            _is_link_like(published)
            or not stat.S_ISREG(published.st_mode)
            or not _same_identity(temporary_metadata, published)
        ):
            try:
                os.unlink(destination.name, dir_fd=directory_fd)
            except OSError:
                pass
            raise SupportBundleError("无法安全发布支持包文件")
        os.fsync(directory_fd)
    except (OSError, zipfile.BadZipFile, SupportBundleError) as exc:
        if temporary_name and directory_fd >= 0:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except OSError:
                pass
        if isinstance(exc, SupportBundleError):
            raise
        raise SupportBundleError(str(exc)) from exc
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)


def _write_private_archive_path(destination: Path, files: dict[str, bytes]) -> None:
    """Windows 回退：托盘默认写入当前用户 LOCALAPPDATA 私有目录。"""
    temporary_name = ""
    try:
        parent_metadata = _validate_directory_chain(destination.parent)
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            if not protect_private_stream(temporary):
                raise SupportBundleError("无法收紧支持包临时文件权限")
            temporary_metadata = os.fstat(temporary.fileno())
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for name, payload in files.items():
                    archive.writestr(name, payload)
            temporary.flush()
            os.fsync(temporary.fileno())

        temporary_path = Path(temporary_name)
        closed_metadata = temporary_path.lstat()
        current_parent = destination.parent.lstat()
        if (
            _is_link_like(closed_metadata)
            or not stat.S_ISREG(closed_metadata.st_mode)
            or not _same_identity(temporary_metadata, closed_metadata)
            or _is_link_like(current_parent)
            or not stat.S_ISDIR(current_parent.st_mode)
            or not _same_identity(parent_metadata, current_parent)
        ):
            raise SupportBundleError("支持包路径在发布前发生变化")
        os.replace(temporary_path, destination)
        published = destination.lstat()
        if (
            _is_link_like(published)
            or not stat.S_ISREG(published.st_mode)
            or not _same_identity(temporary_metadata, published)
            or not protect_private_file(destination)
        ):
            destination.unlink(missing_ok=True)
            raise SupportBundleError("无法安全发布支持包文件")
    except (OSError, zipfile.BadZipFile, SupportBundleError) as exc:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(exc, SupportBundleError):
            raise
        raise SupportBundleError(str(exc)) from exc


def _write_private_archive(destination: Path, files: dict[str, bytes]) -> None:
    if os.name == "posix":
        _write_private_archive_posix(destination, files)
    else:
        _write_private_archive_path(destination, files)


def create_support_bundle(
    paths: RuntimePaths,
    *,
    output: Path | None = None,
    host: str = "0.0.0.0",
    port: int = DEFAULT_WEB_PORT,
    runtime_config_check: DiagnosticCheck | None = None,
    log_lines: int = 300,
) -> Path:
    """创建支持包；不包含数据库、token 或原始 user.env。"""
    destination_input = Path(output) if output is not None else _default_output()
    destination = _absolute_path_without_links(destination_input.expanduser())
    _ensure_directory_chain(destination.parent)
    _validate_directory_chain(destination.parent)
    _validate_destination(destination)

    try:
        report = run_diagnostics(paths, host=host, port=port)
    except Exception as exc:  # noqa: BLE001 - 诊断失败不应阻断支持包生成。
        report = DiagnosticReport(
            (
                DiagnosticCheck(
                    "diagnostics_runtime",
                    "error",
                    f"运行诊断失败：{type(exc).__name__}",
                    "请查看 logs.txt 中的本地日志。",
                ),
            )
        )
    if runtime_config_check is not None:
        report = DiagnosticReport((*report.checks, runtime_config_check))

    config_values, config_error = _redacted_config(paths)
    runtime = {
        "paths": {
            "program_dir": _display_path(paths.program_dir),
            "data_dir": _display_path(paths.data_dir),
            "config_dir": _display_path(paths.config_dir),
            "cache_dir": _display_path(paths.cache_dir),
            "log_dir": _display_path(paths.log_dir),
            "strm_dir": _display_path(paths.strm_dir),
            "trash_dir": _display_path(paths.trash_dir),
        },
        "service_endpoint": {"host": host, "port": int(port)},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "privacy": {
            "local_only": True,
            "excluded": ["database", "tokens", "raw user.env", "media file listings"],
        },
    }
    config_payload = {
        "values": config_values,
        "read_error": config_error,
        "redacted": True,
    }
    files = {
        "build-info.json": _json_bytes(BuildInfo.current().as_dict()),
        "runtime.json": _json_bytes(runtime),
        "diagnostics.json": _json_bytes(report.as_dict()),
        "config-redacted.json": _json_bytes(config_payload),
        "logs.txt": _tail_redacted_log(paths.log_dir / "app.log", log_lines).encode(),
    }

    _write_private_archive(destination, files)
    return destination
