"""MediaFlux 本地备份、校验与原子恢复。"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from app.runtime_paths import RuntimePaths
from app.version import BuildInfo

BACKUP_FORMAT_VERSION = 1
_ALLOWED_PAYLOADS = {
    "database/mediaflux.db",
    "config/user.env",
    "data/guangya_token.json",
}


class BackupError(RuntimeError):
    """备份无法被安全创建、校验或恢复。"""


@dataclass(frozen=True)
class BackupManifest:
    payload: dict[str, Any]

    @property
    def entries(self) -> tuple[dict[str, Any], ...]:
        raw = self.payload.get("entries", [])
        return tuple(item for item in raw if isinstance(item, dict))

    def as_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.payload, ensure_ascii=False))


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _safe_read_regular(path: Path) -> bytes | None:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise BackupError(f"拒绝读取非普通单链接文件：{path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise BackupError(f"备份源在读取期间变为不安全文件：{path}")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise BackupError(f"备份源在读取期间被替换：{path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def _sqlite_backup_bytes(path: Path, source_connection: sqlite3.Connection | None = None) -> bytes | None:
    if source_connection is None and not path.exists():
        return None
    with tempfile.TemporaryDirectory(prefix="mediaflux-sqlite-backup-") as temporary:
        target_path = Path(temporary) / "mediaflux.db"
        owns_source = source_connection is None
        source = source_connection or sqlite3.connect(str(path), timeout=10)
        target = sqlite3.connect(str(target_path))
        try:
            source.backup(target)
            target.commit()
        except sqlite3.Error as exc:
            raise BackupError(f"SQLite 在线备份失败：{exc}") from exc
        finally:
            target.close()
            if owns_source:
                source.close()
        return target_path.read_bytes()


def _database_schema_version(payload: bytes | None) -> int:
    if payload is None:
        return 0
    with tempfile.TemporaryDirectory(prefix="mediaflux-schema-") as temporary:
        path = Path(temporary) / "database.db"
        path.write_bytes(payload)
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()


def _entry(name: str, payload: bytes) -> dict[str, Any]:
    return {"name": name, "size": len(payload), "sha256": _sha256_bytes(payload)}


def _default_backup_path(paths: RuntimePaths, reason: str) -> Path:
    safe_reason = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in reason).strip("-")
    safe_reason = safe_reason[:40] or "manual"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return paths.backup_dir / f"mediaflux-{stamp}-{safe_reason}-{uuid.uuid4().hex[:8]}.zip"


def _create_backup_unlocked(
    paths: RuntimePaths,
    *,
    output: Path | None = None,
    reason: str = "manual",
    source_connection: sqlite3.Connection | None = None,
    include_settings: bool = True,
) -> Path:
    """创建原子 ZIP；使用 SQLite backup API，避免 WAL 不一致。"""
    destination = (Path(output) if output is not None else _default_backup_path(paths, reason)).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)

    payloads: dict[str, bytes] = {}
    database_payload = _sqlite_backup_bytes(paths.database_path, source_connection)
    if database_payload is not None:
        payloads["database/mediaflux.db"] = database_payload
    if include_settings:
        env_payload = _safe_read_regular(paths.env_file)
        if env_payload is not None:
            payloads["config/user.env"] = env_payload
        token_payload = _safe_read_regular(paths.token_file)
        if token_payload is not None:
            payloads["data/guangya_token.json"] = token_payload
    if not payloads:
        raise BackupError("没有可备份的 MediaFlux 数据")

    build = BuildInfo.current()
    manifest = {
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reason": str(reason or "manual")[:100],
        "build": build.as_dict(),
        "database_schema_version": _database_schema_version(database_payload),
        "entries": [_entry(name, payloads[name]) for name in sorted(payloads)],
    }
    manifest_payload = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False
        ) as handle:
            temporary_name = handle.name
        temporary_path = Path(temporary_name)
        try:
            os.chmod(temporary_path, 0o600)
        except OSError:
            pass
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", manifest_payload)
            for name in sorted(payloads):
                archive.writestr(name, payloads[name])
        descriptor = os.open(
            temporary_path,
            os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary_path, destination)
        try:
            os.chmod(destination, 0o600)
        except OSError:
            pass
        _fsync_directory(destination.parent)
    except (OSError, zipfile.BadZipFile) as exc:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise BackupError(str(exc)) from exc
    return destination


def create_backup(
    paths: RuntimePaths,
    *,
    output: Path | None = None,
    reason: str = "manual",
    source_connection: sqlite3.Connection | None = None,
    include_settings: bool = True,
) -> Path:
    """创建原子 ZIP，并与恢复/启动期迁移串行。"""
    # 固定锁序：配置快照 -> 备份/恢复。配置发布只持有前者，恢复在
    # runtime lifecycle 独占期只持有后者，因此不会形成反向锁序。
    with config_snapshot_guard(paths):
        with _backup_operation_guard(paths):
            return _create_backup_unlocked(
                paths,
                output=output,
                reason=reason,
                source_connection=source_connection,
                include_settings=include_settings,
            )


def _safe_archive_name(name: str) -> str:
    pure = PurePosixPath(name)
    if not name or pure.is_absolute() or ".." in pure.parts or "\\" in name:
        raise BackupError(f"备份包含不安全路径：{name}")
    normalized = str(pure)
    if normalized not in _ALLOWED_PAYLOADS and normalized != "manifest.json":
        raise BackupError(f"备份包含未授权条目：{name}")
    return normalized


def _verify_sqlite(payload: bytes) -> None:
    with tempfile.TemporaryDirectory(prefix="mediaflux-backup-verify-") as temporary:
        path = Path(temporary) / "mediaflux.db"
        path.write_bytes(payload)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        except sqlite3.Error as exc:
            raise BackupError(f"备份数据库无法读取：{exc}") from exc
        finally:
            if connection is not None:
                connection.close()
        if integrity != "ok" or foreign:
            raise BackupError("备份数据库完整性校验失败")


def _read_verified_backup(archive_path: Path) -> tuple[BackupManifest, dict[str, bytes]]:
    """在单次归档打开期间完成验证并返回同一批已验证字节。"""
    path = Path(archive_path).expanduser().resolve()
    payloads: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            raw_names = archive.namelist()
            if len(raw_names) != len(set(raw_names)):
                raise BackupError("备份包含重复条目")
            names = {_safe_archive_name(name) for name in raw_names}
            if "manifest.json" not in names:
                raise BackupError("备份缺少 manifest.json")
            try:
                manifest = json.loads(archive.read("manifest.json"))
            except (ValueError, UnicodeError, KeyError) as exc:
                raise BackupError("备份 manifest 无效") from exc
            if not isinstance(manifest, dict) or manifest.get("format_version") != BACKUP_FORMAT_VERSION:
                raise BackupError("不支持的备份格式版本")
            entries = manifest.get("entries")
            if not isinstance(entries, list) or not entries:
                raise BackupError("备份 manifest 没有有效条目")
            expected_names: set[str] = set()
            database_payload: bytes | None = None
            for entry in entries:
                if not isinstance(entry, dict):
                    raise BackupError("备份 manifest 条目无效")
                name = _safe_archive_name(str(entry.get("name") or ""))
                if name == "manifest.json" or name in expected_names:
                    raise BackupError("备份 manifest 条目重复")
                expected_names.add(name)
                payload = archive.read(name)
                if len(payload) != int(entry.get("size", -1)):
                    raise BackupError(f"备份条目大小不匹配：{name}")
                if _sha256_bytes(payload) != str(entry.get("sha256") or ""):
                    raise BackupError(f"备份条目哈希不匹配：{name}")
                payloads[name] = payload
                if name == "database/mediaflux.db":
                    database_payload = payload
            if names != expected_names | {"manifest.json"}:
                raise BackupError("备份内容与 manifest 不一致")
            if database_payload is not None:
                _verify_sqlite(database_payload)
                actual_schema_version = _database_schema_version(database_payload)
                try:
                    manifest_schema_version = int(
                        manifest.get("database_schema_version", -1)
                    )
                except (TypeError, ValueError) as exc:
                    raise BackupError("备份数据库版本字段无效") from exc
                if manifest_schema_version != actual_schema_version:
                    raise BackupError("备份数据库版本与 manifest 不一致")
    except FileNotFoundError as exc:
        raise BackupError(f"备份文件不存在：{path}") from exc
    except zipfile.BadZipFile as exc:
        raise BackupError("备份不是有效 ZIP 文件") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise BackupError("备份 manifest 与归档内容不一致") from exc
    return BackupManifest(manifest), payloads


def verify_backup(archive_path: Path) -> BackupManifest:
    """验证条目白名单、哈希和 SQLite 完整性，不触碰运行数据。"""
    manifest, _payloads = _read_verified_backup(archive_path)
    return manifest


_RESTORE_JOURNAL_VERSION = 1
_RESTORE_JOURNAL_NAME = ".mediaflux-restore.journal.json"
_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = {
    errno.EINVAL,
    errno.ENOTSUP,
    errno.EOPNOTSUPP,
}


def _restore_mapping(paths: RuntimePaths) -> dict[str, Path]:
    return {
        "database/mediaflux.db": paths.database_path,
        "config/user.env": paths.env_file,
        "data/guangya_token.json": paths.token_file,
    }


def _restore_journal_path(paths: RuntimePaths) -> Path:
    return paths.data_dir / _RESTORE_JOURNAL_NAME


def _restore_artifacts(target: Path, transaction_id: str) -> tuple[Path, Path]:
    prefix = f".{target.name}.restore.{transaction_id}"
    return target.parent / f"{prefix}.tmp", target.parent / f"{prefix}.bak"


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":  # pragma: no cover - Windows 不支持目录 fsync
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            return
        raise BackupError(f"无法打开恢复目录进行持久化：{directory}") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            raise BackupError(f"恢复目录持久化失败：{directory}") from exc
    finally:
        os.close(descriptor)


def _regular_file_fingerprint(path: Path) -> dict[str, Any] | None:
    """安全读取普通单链接文件的大小与哈希；不存在时返回 None。"""
    try:
        if path.is_symlink():
            raise BackupError(f"拒绝访问符号链接文件：{path}")
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise BackupError(f"拒绝访问非普通单链接文件：{path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise BackupError(f"恢复文件在读取期间变为不安全文件：{path}")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise BackupError(f"恢复文件在读取期间被替换：{path}")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
        return {"size": size, "sha256": digest.hexdigest()}
    finally:
        os.close(descriptor)


def _fingerprint(payload: bytes) -> dict[str, Any]:
    return {"size": len(payload), "sha256": _sha256_bytes(payload)}


def _same_fingerprint(actual: dict[str, Any] | None, expected: dict[str, Any] | None) -> bool:
    return actual is not None and expected is not None and actual == expected


def _write_private_file(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _fsync_directory(path.parent)


def _validate_transaction_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    try:
        parsed = uuid.UUID(hex=text)
    except (ValueError, AttributeError) as exc:
        raise BackupError("恢复 journal 的事务标识无效") from exc
    if parsed.hex != text:
        raise BackupError("恢复 journal 的事务标识无效")
    return text


def _validate_fingerprint(value: Any, *, allow_none: bool = False) -> dict[str, Any] | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, dict) or set(value) != {"size", "sha256"}:
        raise BackupError("恢复 journal 的文件指纹无效")
    size = value.get("size")
    digest = str(value.get("sha256") or "")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise BackupError("恢复 journal 的文件大小无效")
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest.lower()):
        raise BackupError("恢复 journal 的文件哈希无效")
    return {"size": size, "sha256": digest.lower()}


def _validated_journal(paths: RuntimePaths, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("version") != _RESTORE_JOURNAL_VERSION:
        raise BackupError("恢复 journal 格式无效")
    phase = str(payload.get("phase") or "")
    if phase not in {"prepared", "committed"}:
        raise BackupError("恢复 journal 阶段无效")
    transaction_id = _validate_transaction_id(payload.get("transaction_id"))
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise BackupError("恢复 journal 没有有效条目")
    mapping = _restore_mapping(paths)
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict) or set(raw) != {"name", "had_target", "old", "new"}:
            raise BackupError("恢复 journal 条目无效")
        name = str(raw.get("name") or "")
        if name not in mapping or name in seen:
            raise BackupError("恢复 journal 包含未知或重复条目")
        seen.add(name)
        had_target = raw.get("had_target")
        if not isinstance(had_target, bool):
            raise BackupError("恢复 journal 的旧文件状态无效")
        old = _validate_fingerprint(raw.get("old"), allow_none=not had_target)
        if had_target and old is None:
            raise BackupError("恢复 journal 缺少旧文件指纹")
        if not had_target and raw.get("old") is not None:
            raise BackupError("恢复 journal 的旧文件指纹冲突")
        new = _validate_fingerprint(raw.get("new"))
        entries.append({
            "name": name,
            "had_target": had_target,
            "old": old,
            "new": new,
        })
    return {
        "version": _RESTORE_JOURNAL_VERSION,
        "transaction_id": transaction_id,
        "phase": phase,
        "entries": entries,
    }


def _read_restore_journal(paths: RuntimePaths) -> dict[str, Any] | None:
    path = _restore_journal_path(paths)
    raw = _safe_read_regular(path)
    if raw is None:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise BackupError("恢复 journal 无法解析") from exc
    return _validated_journal(paths, payload)


def _write_restore_journal(paths: RuntimePaths, journal: dict[str, Any]) -> None:
    validated = _validated_journal(paths, journal)
    path = _restore_journal_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    _regular_file_fingerprint(path)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    payload = json.dumps(validated, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        _write_private_file(temporary, payload)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _delete_restore_journal(paths: RuntimePaths) -> None:
    path = _restore_journal_path(paths)
    if _regular_file_fingerprint(path) is None:
        return
    path.unlink()
    _fsync_directory(path.parent)


def _unlink_matching(path: Path, expected: dict[str, Any]) -> None:
    actual = _regular_file_fingerprint(path)
    if actual is None:
        return
    if not _same_fingerprint(actual, expected):
        raise BackupError(f"拒绝删除内容不匹配的恢复临时文件：{path}")
    path.unlink()
    _fsync_directory(path.parent)


def _cleanup_database_sidecars(paths: RuntimePaths) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(paths.database_path) + suffix)
        try:
            info = os.lstat(sidecar)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BackupError(f"拒绝删除不安全的 SQLite 辅助文件：{sidecar}")
        sidecar.unlink()
        _fsync_directory(sidecar.parent)


def _recover_prepared(paths: RuntimePaths, journal: dict[str, Any]) -> None:
    mapping = _restore_mapping(paths)
    transaction_id = journal["transaction_id"]
    for entry in reversed(journal["entries"]):
        target = mapping[entry["name"]]
        temporary, backup = _restore_artifacts(target, transaction_id)
        current = _regular_file_fingerprint(target)
        previous = _regular_file_fingerprint(backup)
        old = entry["old"]
        new = entry["new"]
        if entry["had_target"]:
            if previous is not None:
                if not _same_fingerprint(previous, old):
                    raise BackupError(f"恢复旧文件副本内容不匹配：{backup}")
                if current is None or _same_fingerprint(current, new):
                    os.replace(backup, target)
                    _fsync_directory(target.parent)
                elif _same_fingerprint(current, old):
                    backup.unlink()
                    _fsync_directory(backup.parent)
                else:
                    raise BackupError(f"恢复目标已被外部修改：{target}")
            elif not _same_fingerprint(current, old):
                if _same_fingerprint(current, new):
                    raise BackupError(f"恢复目标旧版本缺失，无法安全回滚：{target}")
                raise BackupError(f"恢复目标状态无法判定：{target}")
        else:
            if previous is not None:
                raise BackupError(f"原本不存在的恢复目标出现旧副本：{backup}")
            if current is not None:
                if not _same_fingerprint(current, new):
                    raise BackupError(f"恢复目标已被外部创建或修改：{target}")
                target.unlink()
                _fsync_directory(target.parent)
        _unlink_matching(temporary, new)
    _delete_restore_journal(paths)


def _recover_committed(paths: RuntimePaths, journal: dict[str, Any]) -> None:
    mapping = _restore_mapping(paths)
    transaction_id = journal["transaction_id"]
    restored_database = False
    for entry in journal["entries"]:
        target = mapping[entry["name"]]
        temporary, backup = _restore_artifacts(target, transaction_id)
        if not _same_fingerprint(_regular_file_fingerprint(target), entry["new"]):
            raise BackupError(f"已提交的恢复目标内容不匹配：{target}")
        _unlink_matching(temporary, entry["new"])
        if entry["had_target"]:
            _unlink_matching(backup, entry["old"])
        elif _regular_file_fingerprint(backup) is not None:
            raise BackupError(f"原本不存在的恢复目标出现旧副本：{backup}")
        restored_database = restored_database or entry["name"] == "database/mediaflux.db"
    if restored_database:
        _cleanup_database_sidecars(paths)
    _delete_restore_journal(paths)


def _recover_pending_restore_unlocked(paths: RuntimePaths) -> bool:
    journal = _read_restore_journal(paths)
    if journal is None:
        return False
    if journal["phase"] == "prepared":
        _recover_prepared(paths, journal)
    else:
        _recover_committed(paths, journal)
    return True


@contextmanager
def _backup_operation_guard(paths: RuntimePaths):
    from app.modules.process_lock import CrossProcessLock

    lock = CrossProcessLock("backup-restore", directory=paths.data_dir)
    if not lock.acquire(blocking=True):  # pragma: no cover - blocking lock returns true or raises
        raise BackupError("另一个备份恢复任务正在运行")
    try:
        yield
    finally:
        lock.release()


@contextmanager
def config_snapshot_guard(paths: RuntimePaths):
    """串行配置发布与包含配置的备份快照。"""
    from app.modules.process_lock import CrossProcessLock

    lock = CrossProcessLock("config-snapshot", directory=paths.data_dir)
    if not lock.acquire(blocking=True):  # pragma: no cover - blocking lock returns true or raises
        raise BackupError("另一个配置保存或备份任务正在运行")
    try:
        yield
    finally:
        lock.release()


_RUNTIME_LIFECYCLE_REGISTRY_LOCK = threading.RLock()
_RUNTIME_LIFECYCLE_REGISTRY: dict[tuple[int, str], dict[str, Any]] = {}


def _runtime_lifecycle_key(paths: RuntimePaths) -> tuple[int, str]:
    return os.getpid(), str(Path(paths.data_dir).expanduser().resolve())


@contextmanager
def runtime_lifecycle_guard(paths: RuntimePaths):
    """独占服务进程生命周期，同一进程内的多个 ASGI 上下文共享租约。"""
    from app.modules.process_lock import CrossProcessLock

    key = _runtime_lifecycle_key(paths)
    with _RUNTIME_LIFECYCLE_REGISTRY_LOCK:
        entry = _RUNTIME_LIFECYCLE_REGISTRY.get(key)
        if entry is None:
            lock = CrossProcessLock("runtime-lifecycle", directory=paths.data_dir)
            if not lock.acquire(blocking=False):
                # 该守卫服务于「启动服务」；恢复备份走
                # _exclusive_runtime_lifecycle_guard，提示语不可混用。
                raise BackupError(
                    "同一数据目录已有 MediaFlux 实例在运行，已拒绝重复启动；"
                    "请先停止正在运行的实例后重试"
                )
            entry = {"lock": lock, "references": 0}
            _RUNTIME_LIFECYCLE_REGISTRY[key] = entry
        entry["references"] = int(entry["references"]) + 1

    release_lock = None
    try:
        yield
    finally:
        with _RUNTIME_LIFECYCLE_REGISTRY_LOCK:
            current = _RUNTIME_LIFECYCLE_REGISTRY.get(key)
            if current is not entry:  # pragma: no cover - internal invariant
                raise RuntimeError("runtime lifecycle registry corrupted")
            current["references"] = int(current["references"]) - 1
            if current["references"] == 0:
                _RUNTIME_LIFECYCLE_REGISTRY.pop(key, None)
                release_lock = current["lock"]
        if release_lock is not None:
            release_lock.release()


@contextmanager
def _exclusive_runtime_lifecycle_guard(paths: RuntimePaths):
    """恢复专用的非重入生命周期锁；即使同进程服务在运行也必须拒绝。"""
    from app.modules.process_lock import CrossProcessLock

    lock = CrossProcessLock("runtime-lifecycle", directory=paths.data_dir)
    if not lock.acquire(blocking=False):
        raise BackupError("MediaFlux 服务正在运行，请先停止服务后再恢复备份")
    try:
        yield
    finally:
        lock.release()


@contextmanager
def _offline_restore_guard(paths: RuntimePaths):
    # 固定顺序：服务生命周期 -> 配置快照 -> 备份恢复，避免恢复 user.env
    # 与配置发布并发，也不与在线备份形成反向锁序。
    with _exclusive_runtime_lifecycle_guard(paths):
        with config_snapshot_guard(paths):
            with _backup_operation_guard(paths):
                yield


def recover_pending_restore(
    paths: RuntimePaths,
    *,
    lifecycle_lock_held: bool = False,
) -> bool:
    """在数据库打开前恢复被中断的多文件恢复事务。"""
    if lifecycle_lock_held:
        with config_snapshot_guard(paths):
            with _backup_operation_guard(paths):
                return _recover_pending_restore_unlocked(paths)
    with _offline_restore_guard(paths):
        return _recover_pending_restore_unlocked(paths)


def _transactional_restore_files(
    paths: RuntimePaths,
    files: dict[str, tuple[Path, bytes]],
) -> None:
    transaction_id = uuid.uuid4().hex
    entries: list[dict[str, Any]] = []
    staged: list[Path] = []
    journal_written = False
    try:
        for name, (target, payload) in files.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            old = _regular_file_fingerprint(target)
            temporary, backup = _restore_artifacts(target, transaction_id)
            if _regular_file_fingerprint(temporary) is not None or _regular_file_fingerprint(backup) is not None:
                raise BackupError(f"恢复事务临时文件已存在：{target}")
            _write_private_file(temporary, payload)
            staged.append(temporary)
            entries.append({
                "name": name,
                "had_target": old is not None,
                "old": old,
                "new": _fingerprint(payload),
            })
        journal = {
            "version": _RESTORE_JOURNAL_VERSION,
            "transaction_id": transaction_id,
            "phase": "prepared",
            "entries": entries,
        }
        _write_restore_journal(paths, journal)
        journal_written = True
        for entry in entries:
            target, _payload = files[entry["name"]]
            temporary, backup = _restore_artifacts(target, transaction_id)
            current = _regular_file_fingerprint(target)
            if entry["had_target"]:
                if not _same_fingerprint(current, entry["old"]):
                    raise BackupError(f"恢复目标在发布前被修改：{target}")
                os.replace(target, backup)
                _fsync_directory(target.parent)
            elif current is not None:
                raise BackupError(f"恢复目标在发布前被创建：{target}")
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        journal["phase"] = "committed"
        _write_restore_journal(paths, journal)
        _recover_committed(paths, journal)
    except Exception as exc:
        if journal_written:
            try:
                _recover_pending_restore_unlocked(paths)
            except Exception as recovery_exc:
                raise BackupError(
                    f"恢复失败且自动回滚未完成：{type(recovery_exc).__name__}"
                ) from exc
        else:
            for temporary in staged:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        if isinstance(exc, BackupError):
            raise
        raise BackupError(f"恢复过程中发生错误：{exc}") from exc


def restore_backup(paths: RuntimePaths, archive_path: Path) -> BackupManifest:
    """完整验证后事务替换数据库、配置与 token。调用方必须先停止服务。"""
    with _offline_restore_guard(paths):
        _recover_pending_restore_unlocked(paths)
        manifest, verified_payloads = _read_verified_backup(archive_path)
        database_payload = verified_payloads.get("database/mediaflux.db")
        if database_payload is None:
            raise BackupError("完整恢复要求备份包含数据库")
        # 不能只信任 manifest；必须以已经完成哈希与完整性校验的数据库
        # payload 为准，避免降级版本恢复成功后把服务留在无法启动的状态。
        from app.database import SCHEMA_VERSION

        backup_schema_version = _database_schema_version(database_payload)
        if backup_schema_version > SCHEMA_VERSION:
            raise BackupError(
                "备份数据库版本 "
                f"{backup_schema_version} 高于当前程序支持的 {SCHEMA_VERSION}，"
                "已拒绝降级恢复"
            )
        mapping = _restore_mapping(paths)
        files = {
            str(entry["name"]): (
                mapping[str(entry["name"])],
                verified_payloads[str(entry["name"])],
            )
            for entry in manifest.entries
        }
        _transactional_restore_files(paths, files)
        return manifest
