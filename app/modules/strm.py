"""STRM 生成模块。

为光鸭云盘视频文件生成 .strm 指针文件，内容指向本服务 302 反代地址：
    {base_url}/playgy/{file_id}/{etag}/{size}/{filename}
播放器（Emby/Jellyfin）扫描 .strm 即可播放，实际流量走光鸭直链。

能力：全量扫描、增量跳过、无效清理、长路径保护和有界元数据并发。
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import inspect
import json
import os
import re
import shutil
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import (
    FIRST_COMPLETED,
    ThreadPoolExecutor,
    wait,
)
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlencode

import requests
from urllib3.util import Retry

from app import database as db
from app.config import get, get_int
from app.clients.guangya import GuangYaClient, GuangYaFile
from app.logger import get_logger, redact_sensitive_text
from app.modules.process_lock import CrossProcessLock
from app.modules.strm_notifications import append_change, relative_change

logger = get_logger(__name__)


@contextmanager
def _guangya_client_scope(
    client: GuangYaClient | None,
) -> Iterator[GuangYaClient]:
    """仅释放当前调用内部创建的光鸭客户端。"""
    owned_client = client is None
    runtime_client = client or GuangYaClient()
    try:
        yield runtime_client
    finally:
        if owned_client:
            close = getattr(runtime_client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    logger.warning(
                        "关闭 STRM 光鸭客户端失败 type=%s",
                        type(exc).__name__,
                    )


def _iter_client_dir(
    client,
    dir_id: str,
    *,
    should_stop: Callable[[], bool] | None,
    max_items: int,
):
    """兼容旧客户端替身，同时把生产扫描预算下推到分页层。"""
    iter_dir = getattr(client, "iter_dir", None)
    if not callable(iter_dir):
        return iter(client.list_dir(dir_id))
    kwargs = {"should_stop": should_stop}
    try:
        parameters = inspect.signature(iter_dir).parameters.values()
        if any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters) or any(
            item.name == "max_items" for item in parameters
        ):
            kwargs["max_items"] = max(1, int(max_items))
    except (TypeError, ValueError):
        kwargs["max_items"] = max(1, int(max_items))
    return iter_dir(dir_id, **kwargs)

# 全局元数据下载 Session 与连接池，复用 TCP 连接以避免 Windows 端口耗尽
_METADATA_SESSION: requests.Session | None = None
_METADATA_SESSION_LOCK = threading.Lock()
_ORIGINAL_REQUESTS_GET = requests.get


def _get_metadata_session() -> requests.Session:
    global _METADATA_SESSION
    if _METADATA_SESSION is None:
        with _METADATA_SESSION_LOCK:
            if _METADATA_SESSION is None:
                session = requests.Session()
                session.headers.update({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                    "Accept": "*/*",
                    "Connection": "keep-alive",
                })
                retries = Retry(
                    total=3,
                    connect=3,
                    read=2,
                    status_forcelist=[500, 502, 503, 504],
                    raise_on_status=False,
                    backoff_factor=0.3,
                )
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=32,
                    pool_maxsize=32,
                    max_retries=retries,
                )
                session.mount("http://", adapter)
                session.mount("https://", adapter)
                _METADATA_SESSION = session
    return _METADATA_SESSION


def _normalized_absolute(path: Path | str) -> str:
    """按平台安全归一化绝对路径字符串（Windows 下统一小写且解析绝对路径）。"""
    resolved = Path(path).expanduser().resolve(strict=False)
    return os.path.normcase(str(resolved))


def _is_path_within_root(target: Path | str, strm_root: Path | str) -> bool:
    """跨平台安全判断 target 是否位于 strm_root 之内或等于 strm_root。"""
    target_norm = _normalized_absolute(target)
    root_norm = _normalized_absolute(strm_root)
    if target_norm == root_norm:
        return True
    sep = os.sep
    prefix = root_norm if root_norm.endswith(sep) else f"{root_norm}{sep}"
    return target_norm.startswith(prefix)

# 精准媒体库刷新需要本轮真实变化的 STRM 路径；展示用变化记录会脱敏，
# 无法反推真实路径，因此单独有界记录。
_MAX_TRACKED_CHANGED_PATHS = 5000
_MAX_TRACKED_OVERFLOW_DIRS = 256


def _parent_path_text(path: str) -> str:
    parent = Path(path).parent
    if "/" in path and "\\" not in path:
        return parent.as_posix()
    return str(parent)


def _record_changed_path(stats: dict, target: object) -> None:
    paths = stats.setdefault("changed_strm_paths", [])
    if not isinstance(paths, list):
        return
    path = str(target or "")
    if not path:
        return
    if len(paths) >= _MAX_TRACKED_CHANGED_PATHS:
        stats["changed_paths_omitted"] = int(
            stats.get("changed_paths_omitted", 0) or 0
        ) + 1
        overflow_dirs = stats.setdefault("changed_overflow_dirs", [])
        parent = _parent_path_text(path)
        if (
            isinstance(overflow_dirs, list)
            and parent
            and parent not in overflow_dirs
            and len(overflow_dirs) < _MAX_TRACKED_OVERFLOW_DIRS
        ):
            overflow_dirs.append(parent)
        return
    paths.append(path)


def _track_change(stats: dict, action: str, target, strm_root) -> None:
    """记录展示用变化，同时保留真实 STRM 路径供精准刷新使用。"""
    append_change(stats, relative_change(action, target, strm_root))
    if action != "removed_dir":
        _record_changed_path(stats, target)


@dataclass(frozen=True)
class CleanupDecision:
    """失效清理的统一安全判定。

    删除是不可逆操作：只有本轮扫描完整、无 Provider 错误、未取消且快照
    自洽时才允许删除未知旧对象。任一条件不成立时只允许更新已确认对象。
    """

    scan_completed: bool = True
    scan_partial: bool = False
    provider_error: bool = False
    cancelled: bool = False
    snapshot_consistent: bool = True
    reasons: tuple[str, ...] = ()

    @property
    def cleanup_allowed(self) -> bool:
        return bool(
            self.scan_completed
            and not self.scan_partial
            and not self.provider_error
            and not self.cancelled
            and self.snapshot_consistent
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "scan_completed": bool(self.scan_completed),
            "scan_partial": bool(self.scan_partial),
            "provider_error": bool(self.provider_error),
            "cancelled": bool(self.cancelled),
            "snapshot_consistent": bool(self.snapshot_consistent),
            "cleanup_allowed": self.cleanup_allowed,
            "reasons": list(self.reasons),
        }


def evaluate_cleanup_decision(
    stats: dict,
    *,
    scan_errors: int = 0,
    consistency_errors: int = 0,
    cancelled: bool = False,
) -> CleanupDecision:
    """把散落的安全条件收敛成一次可审计判定。"""
    scan_incomplete = bool((stats or {}).get("scan_incomplete"))
    stopped = bool(cancelled or (stats or {}).get("stopped"))
    provider_error = int(scan_errors or 0) > 0
    snapshot_consistent = int(consistency_errors or 0) == 0
    reasons: list[str] = []
    if scan_incomplete:
        reasons.append(
            str((stats or {}).get("scan_limit_reason") or "") or "云端目录扫描不完整"
        )
    if provider_error:
        reasons.append("云盘目录读取失败")
    if not snapshot_consistent:
        reasons.append("本地 STRM 快照与索引不一致")
    if stopped:
        reasons.append("本轮同步已被取消")
    return CleanupDecision(
        scan_completed=not scan_incomplete,
        scan_partial=scan_incomplete,
        provider_error=provider_error,
        cancelled=stopped,
        snapshot_consistent=snapshot_consistent,
        reasons=tuple(reasons),
    )


def apply_cleanup_decision(stats: dict, decision: CleanupDecision) -> bool:
    """把判定写入统计并返回是否允许清理，保证 Web/TG 口径一致。"""
    stats["cleanup_decision"] = decision.as_dict()
    if not decision.cleanup_allowed:
        stats["clean_skipped"] = True
    return decision.cleanup_allowed


def finalize_changed_paths(stats: dict) -> dict:
    """收敛本轮变化路径与其父目录，输出去重后的稳定顺序结果。"""
    paths = [
        str(item) for item in (stats.get("changed_strm_paths") or []) if str(item)
    ]
    unique_paths = list(dict.fromkeys(paths))
    stats["changed_strm_paths"] = unique_paths
    changed_dirs = [
        str(item) for item in (stats.get("changed_overflow_dirs") or []) if str(item)
    ]
    for item in unique_paths:
        changed_dirs.append(_parent_path_text(item))
    stats["changed_dirs"] = list(dict.fromkeys(changed_dirs))
    return stats

DEFAULT_VIDEO_EXTS = {
    "mp4", "mkv", "ts", "rmvb", "avi", "mov", "mpeg", "mpg",
    "wmv", "3gp", "asf", "m4v", "flv", "m2ts", "tp", "f4v", "rm",
}

DEFAULT_METADATA_EXTS = {
    "nfo", "srt", "ass", "ssa", "sub", "idx",
    "jpg", "jpeg", "png", "webp",
}

STRM_SUBDIR = "光鸭云盘"
MAX_PATH_COMPONENT_BYTES = 255
MAX_RELATIVE_PATH_BYTES = 3072
DEFAULT_SCAN_WORKERS = 15
MAX_SCAN_WORKERS = 32
DEFAULT_VERIFY_WORKERS = 8
MAX_VERIFY_WORKERS = 32
DEFAULT_SCAN_MAX_DIRECTORIES = 100_000
DEFAULT_SCAN_MAX_ENTRIES = 2_000_000
DEFAULT_SCAN_MAX_CANDIDATES = 500_000
DEFAULT_SCAN_DEADLINE_SECONDS = 3_600
DEFAULT_METADATA_MAX_FILE_MB = 64
DEFAULT_METADATA_DEADLINE_SECONDS = 300
_HARD_SCAN_MAX_DIRECTORIES = 1_000_000
_HARD_SCAN_MAX_ENTRIES = 10_000_000
_HARD_SCAN_MAX_CANDIDATES = 2_000_000
_HARD_SCAN_DEADLINE_SECONDS = 21_600
_HARD_METADATA_MAX_FILE_MB = 512
_HARD_METADATA_DEADLINE_SECONDS = 1_800
_HASH_LENGTH = 12
_WINDOWS_RESERVED_COMPONENTS = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}"
    for prefix in ("COM", "LPT")
    for number in range(1, 10)
}
STRM_OPERATION_LOCK = CrossProcessLock("strm-operation")


def _bounded_positive_setting(key: str, default: int, hard_max: int) -> int:
    return max(1, min(get_int(key, default), hard_max))


def _scan_limits() -> tuple[int, int, int, int]:
    """返回全量扫描资源预算；默认足够大，只拦截失控目录树。"""
    return (
        _bounded_positive_setting(
            "STRM_SCAN_MAX_DIRECTORIES",
            DEFAULT_SCAN_MAX_DIRECTORIES,
            _HARD_SCAN_MAX_DIRECTORIES,
        ),
        _bounded_positive_setting(
            "STRM_SCAN_MAX_ENTRIES",
            DEFAULT_SCAN_MAX_ENTRIES,
            _HARD_SCAN_MAX_ENTRIES,
        ),
        _bounded_positive_setting(
            "STRM_SCAN_MAX_CANDIDATES",
            DEFAULT_SCAN_MAX_CANDIDATES,
            _HARD_SCAN_MAX_CANDIDATES,
        ),
        _bounded_positive_setting(
            "STRM_SCAN_DEADLINE_SECONDS",
            DEFAULT_SCAN_DEADLINE_SECONDS,
            _HARD_SCAN_DEADLINE_SECONDS,
        ),
    )


def _metadata_file_limit() -> int:
    return _bounded_positive_setting(
        "STRM_METADATA_MAX_FILE_MB",
        DEFAULT_METADATA_MAX_FILE_MB,
        _HARD_METADATA_MAX_FILE_MB,
    ) * 1024 * 1024


def _metadata_download_deadline_seconds() -> int:
    return _bounded_positive_setting(
        "STRM_METADATA_DEADLINE_SECONDS",
        DEFAULT_METADATA_DEADLINE_SECONDS,
        _HARD_METADATA_DEADLINE_SECONDS,
    )


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节安全裁剪，不产生半个多字节字符。"""
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def safe_path_component(
    name: str,
    *,
    extra_suffix: str = "",
    max_bytes: int = MAX_PATH_COMPONENT_BYTES,
) -> str:
    """生成单个安全路径组件。

    只要清洗改变原名，就加入基于原始名称的稳定哈希，避免 ``A\\B`` 与
    ``A_B`` 等名称在清洗后碰撞。超长名称保留扩展名和调用方后缀。
    """
    raw = str(name or "")
    cleaned = "".join(
        "_" if char in '/\\<>:"|?*' or ord(char) < 32 else char
        for char in raw
    ).rstrip(" .")
    if cleaned in {"", ".", ".."}:
        cleaned = "_"
    component_stem = cleaned.split(".", 1)[0].upper()
    if component_stem in _WINDOWS_RESERVED_COMPONENTS:
        cleaned = f"_{cleaned}"
    changed = cleaned != raw

    suffix = Path(cleaned).suffix
    stem = cleaned[:-len(suffix)] if suffix else cleaned
    full = f"{cleaned}{extra_suffix}"
    if not changed and len(full.encode("utf-8")) <= max_bytes:
        return full

    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
    marker = f"~{digest}"
    reserved = len(f"{marker}{suffix}{extra_suffix}".encode("utf-8"))
    stem_budget = max(1, max_bytes - reserved)
    short_stem = _truncate_utf8(stem, stem_budget).rstrip(" .") or "_"
    result = f"{short_stem}{marker}{suffix}{extra_suffix}"
    if len(result.encode("utf-8")) > max_bytes:
        result = _truncate_utf8(result, max_bytes)
    return result


_ADULT_IDENTITY_TAG = re.compile(
    r"\s*[\{\(](?:metatube|clean_title)-[A-Za-z0-9._-]+[\}\)]\s*",
    re.IGNORECASE,
)


def _jellyfin_visible_name(value: str) -> str:
    """移除 Jellyfin 不识别的成人媒体内部身份标记。"""
    cleaned = _ADULT_IDENTITY_TAG.sub(" ", str(value or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return re.sub(r"\s+(?=\.[A-Za-z0-9]{1,8}$)", "", cleaned)


def _safe_rel_parts(rel_dir: str) -> list[str]:
    parts = [part for part in str(rel_dir or "").replace("\\", "/").split("/") if part]
    return [safe_path_component(_jellyfin_visible_name(part)) for part in parts]


def _relative_size(parts: list[str]) -> int:
    return len("/".join(parts).encode("utf-8"))


def _bounded_rel_parts(dir_parts: list[str], filename: str) -> list[str]:
    """限制完整相对路径；超预算时稳定折叠中间目录。"""
    relative = [STRM_SUBDIR, *dir_parts, filename]
    if _relative_size(relative) <= MAX_RELATIVE_PATH_BYTES:
        return relative

    digest_source = "/".join(dir_parts)
    marker = f"~path-{hashlib.sha256(digest_source.encode('utf-8')).hexdigest()[:_HASH_LENGTH]}"
    if len(dir_parts) >= 3:
        compressed_dirs = [dir_parts[0], marker, dir_parts[-1]]
    else:
        compressed_dirs = [marker]
    relative = [STRM_SUBDIR, *compressed_dirs, filename]
    if _relative_size(relative) <= MAX_RELATIVE_PATH_BYTES:
        return relative
    return [STRM_SUBDIR, marker, filename]


def _require_target_within_root(target: Path, strm_root: str) -> Path:
    if not _is_path_within_root(target, strm_root):
        raise ValueError("STRM 输出路径超出配置根目录")
    return target.expanduser().resolve(strict=False)


def _target_path(strm_root: str, rel_dir: str, filename: str) -> Path:
    parts = _bounded_rel_parts(_safe_rel_parts(rel_dir), filename)
    target = Path(strm_root).expanduser().joinpath(*parts)
    return _require_target_within_root(target, strm_root)


def _safe_indexed_path(path_text: object, strm_root: str) -> Path | None:
    """仅接受位于当前 STRM 根目录内的历史索引路径。"""
    value = str(path_text or "").strip()
    if not value:
        return None
    try:
        path = Path(value)
        if not path.is_absolute():
            path = Path(strm_root) / path
        return _require_target_within_root(path, strm_root)
    except (ValueError, RuntimeError, OSError):
        logger.debug("忽略超出当前 STRM 根目录的历史索引路径")
        return None


def _strm_target(file: GuangYaFile, rel_dir: str, strm_root: str) -> Path:
    # 本地 STRM 使用媒体主文件名，而不是 ``.mkv.strm`` 双扩展名。
    # 原始容器类型仍保留在 STRM 内容的 /playgy/.../{filename} URL 中。
    visible_name = _jellyfin_visible_name(file.name)
    suffix = Path(visible_name).suffix
    stem = visible_name[:-len(suffix)] if suffix else visible_name
    return _target_path(
        strm_root,
        rel_dir,
        safe_path_component(stem, extra_suffix=".strm"),
    )


def _metadata_target(file: GuangYaFile, rel_dir: str, strm_root: str) -> Path:
    visible_name = _jellyfin_visible_name(file.name)
    return _target_path(strm_root, rel_dir, safe_path_component(visible_name))


def _metadata_queue_payload(
    file: GuangYaFile,
    rel_dir: str,
    strm_root: str,
    *,
    source_id: str,
    source_name: str,
    force: bool = False,
) -> dict[str, object]:
    """生成不含签名直链的持久化元数据任务快照。"""
    target = _metadata_target(file, rel_dir, strm_root)
    try:
        target_rel_path = str(target.relative_to(Path(strm_root).expanduser()))
    except ValueError:
        target_rel_path = str(target)
    return {
        "source_id": str(source_id),
        "source_name": str(source_name or source_id),
        "file_id": str(file.file_id),
        "parent_id": str(file.parent_id or ""),
        "filename": str(file.name),
        "etag": str(file.etag or ""),
        "size": max(0, int(file.size or 0)),
        "rel_dir": str(rel_dir or ""),
        "target_rel_path": target_rel_path,
        "force": bool(force),
    }


def _temporary_path(target: Path, suffix: str = ".part") -> Path:
    return target.with_name(f".mediaflux-{uuid.uuid4().hex}{suffix}")


def _atomic_write_text(
    target: Path,
    text: str,
    *,
    before_replace: Callable[[Path], None] | None = None,
    on_replaced: Callable[[Path, str], None] | None = None,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = _temporary_path(target)
    try:
        temp.write_text(text, encoding="utf-8")
        prepared_fingerprint = _content_fingerprint(temp)
        if before_replace:
            before_replace(target)
        temp.replace(target)
        if on_replaced:
            on_replaced(target, prepared_fingerprint)
    finally:
        if temp.exists():
            temp.unlink()


def _copy_backup(path: Path) -> Optional[Path]:
    if not path.is_file():
        return None
    backup = _temporary_path(path, ".backup")
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    return backup


def _restore_backup(path: Path, backup: Optional[Path]) -> None:
    if backup is not None:
        if backup.exists():
            backup.replace(path)
        return
    if path.exists():
        path.unlink()


def _discard_backup(backup: Optional[Path]) -> None:
    if not backup:
        return
    try:
        backup.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("清理 STRM 事务备份失败 type=%s", type(exc).__name__)


def _restore_installed_file(
    path: Path,
    backup: Optional[Path],
    installed_fingerprint: str,
    action: str,
) -> bool:
    """仅当路径仍是本次刚写入的内容时回滚，避免覆盖已检测到的外部修改。"""
    if not installed_fingerprint:
        _discard_backup(backup)
        return False
    if not _fingerprint_matches(path, installed_fingerprint):
        _discard_backup(backup)
        logger.warning("%s回滚已跳过：目标在事务期间被外部修改 %s", action, path)
        return False
    _restore_backup(path, backup)
    return True


def _restore_deleted_file(path: Path, backup: Optional[Path], action: str) -> bool:
    """仅在本次确实删除且路径仍为空时恢复，避免覆盖随后创建的文件。"""
    if backup is None:
        return False
    if path.exists():
        _discard_backup(backup)
        logger.warning("%s回滚已跳过：原路径已被外部重新创建 %s", action, path)
        return False
    _restore_backup(path, backup)
    return True


def _delete_owned_file(path: Path, rows, action: str) -> bool:
    """在删除动作的最后一刻复核内容所有权。"""
    if not path.is_file():
        return False
    _require_owned_file(path, rows, action)
    path.unlink()
    return True


def _row_field(row, key: str, default=""):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _content_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _fingerprint_matches(path: Path, fingerprint: object) -> bool:
    value = str(fingerprint or "").strip().lower()
    if len(value) != 71 or not value.startswith("sha256:"):
        return False
    try:
        int(value[7:], 16)
        return path.is_file() and _content_fingerprint(path) == value
    except (OSError, ValueError):
        return False


class _STRMStopped(RuntimeError):
    pass


class _STRMOwnershipError(RuntimeError):
    """本地文件已脱离持久化索引所有权，禁止自动覆盖或删除。"""


def _require_owned_file(path: Path, rows, action: str) -> None:
    """文件存在时必须由至少一条内容指纹一致的索引记录认领。"""
    if not path.is_file():
        return
    if any(
        _fingerprint_matches(path, _row_field(row, "content_fingerprint", ""))
        for row in rows
        if row is not None
    ):
        return
    raise _STRMOwnershipError(
        f"{action}：已停止，本地文件内容与 STRM 索引不一致，请人工核对 {path}"
    )


def _require_file_snapshot(path: Path, fingerprint: str, action: str) -> None:
    """覆盖前复核文件仍是本轮检查到的内容，避免吞掉并发外部写入。"""
    if fingerprint and _fingerprint_matches(path, fingerprint):
        return
    raise _STRMOwnershipError(
        f"{action}：已停止，本地文件在同步期间再次变化，请重新执行同步 {path}"
    )


def _row_snapshot(row) -> dict:
    return {
        "file_id": str(row["file_id"]),
        "etag": str(row["etag"] or ""),
        "size": int(row["size"] or 0),
        "filename": str(row["filename"] or ""),
        "strm_path": str(row["strm_path"] or ""),
        "content_fingerprint": str(_row_field(row, "content_fingerprint", "") or ""),
    }


def _restore_index_row(source_key: str, row: dict) -> None:
    db.upsert_strm_index(
        source_key,
        row["file_id"],
        row["etag"],
        row["size"],
        row["filename"],
        row["strm_path"],
        row.get("content_fingerprint", ""),
    )


def _rollback_index(
    source_key: str,
    new_file_id: str,
    current_row: Optional[dict],
    conflicting_rows: list[dict],
    index_changed: bool,
) -> None:
    if not index_changed:
        return
    try:
        if current_row:
            _restore_index_row(source_key, current_row)
        else:
            db.delete_strm_index_ids(source_key, [new_file_id])
        for row in conflicting_rows:
            _restore_index_row(source_key, row)
    except Exception as exc:
        logger.error("回滚 STRM 索引失败 file=%s type=%s", new_file_id, type(exc).__name__)


def build_play_url(base_url: str, file_id: str, etag: str,
                   size: str | int, filename: str) -> str:
    """构造 302 反代播放地址。"""
    from app.modules.playgy_signing import encode_playgy_path_token, sign_playgy

    safe_etag = etag or "0"
    safe_size = size or 0
    signature = sign_playgy(file_id, safe_etag, safe_size)
    query_params = {"v": "1", "sig": signature}
    if "/" in str(file_id) or "/" in str(safe_etag):
        path_file_id = encode_playgy_path_token(str(file_id))
        path_etag = encode_playgy_path_token(str(safe_etag))
        query_params["enc"] = "b64"
    else:
        path_file_id = str(file_id)
        path_etag = str(safe_etag)
    segments = (path_file_id, path_etag, safe_size, filename)
    encoded_path = "/".join(quote(str(segment), safe="") for segment in segments)
    query = urlencode(query_params)
    return f"{base_url.rstrip('/')}/playgy/{encoded_path}?{query}"


def _read_strm_state(path: Path, expected_url: str) -> tuple[bool, str]:
    """一次读取同时校验播放地址并计算内容指纹。"""
    try:
        payload = path.read_bytes()
    except (OSError, ValueError):
        return False, ""
    if payload != expected_url.encode("utf-8"):
        return False, ""
    return True, f"sha256:{hashlib.sha256(payload).hexdigest()}"


def generate_strm(
    file: GuangYaFile,
    rel_dir: str,
    base_url: str,
    strm_root: str,
    *,
    before_replace: Callable[[Path], None] | None = None,
    on_replaced: Callable[[Path, str], None] | None = None,
) -> Path:
    """为单个光鸭文件原子生成 .strm，返回生成路径。"""
    play_url = build_play_url(base_url, file.file_id, file.etag, file.size, file.name)
    strm_path = _strm_target(file, rel_dir, strm_root)
    _atomic_write_text(
        strm_path, play_url, before_replace=before_replace, on_replaced=on_replaced
    )
    return strm_path


@dataclass(frozen=True)
class PreparedMetadataDownload:
    target: Path
    temp: Path
    fingerprint: str


def prepare_metadata_download(
    file: GuangYaFile,
    rel_dir: str,
    strm_root: str,
    client: Optional[GuangYaClient] = None,
    *,
    download_url: str = "",
    should_stop: Callable[[], bool] | None = None,
) -> PreparedMetadataDownload:
    """只把元数据流式下载到同目录临时文件，不触碰正式目标。"""
    byte_limit = _metadata_file_limit()
    declared_file_size = int(file.size or 0)
    if declared_file_size < 0 or declared_file_size > byte_limit:
        raise ValueError("元数据文件超过下载大小上限")
    deadline_seconds = _metadata_download_deadline_seconds()
    started = time.monotonic()
    target = _metadata_target(file, rel_dir, strm_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = _temporary_path(target)
    requester = (
        requests.get
        if requests.get is not _ORIGINAL_REQUESTS_GET
        else _get_metadata_session().get
    )
    max_retries = 1
    last_exc = None

    for attempt in range(max_retries + 1):
        try:
            if should_stop and should_stop():
                raise _STRMStopped("STRM 元数据后台任务已停止")
            remaining = deadline_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("元数据下载超过总时限")
            url = str(download_url or "")
            if not url or (attempt > 0 and client is not None):
                if client is None:
                    raise RuntimeError("缺少元数据下载客户端")
                url = str(client.get_download_url(file.file_id) or "")
            if not url:
                raise RuntimeError("无法获取元数据下载直链")
            with requester(
                url,
                stream=True,
                timeout=(min(10.0, remaining), min(30.0, remaining)),
            ) as resp:
                resp.raise_for_status()
                raw_length = str(
                    getattr(resp, "headers", {}).get("Content-Length", "") or ""
                ).strip()
                if raw_length:
                    try:
                        content_length = int(raw_length)
                    except ValueError as exc:
                        raise ValueError("元数据响应 Content-Length 无效") from exc
                    if content_length < 0 or content_length > byte_limit:
                        raise ValueError("元数据响应超过下载大小上限")
                downloaded = 0
                with temp.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=256 * 1024):
                        if should_stop and should_stop():
                            raise _STRMStopped("STRM 元数据后台任务已停止")
                        if time.monotonic() - started > deadline_seconds:
                            raise TimeoutError("元数据下载超过总时限")
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > byte_limit:
                            raise ValueError("元数据实际下载内容超过大小上限")
                        fh.write(chunk)
            return PreparedMetadataDownload(
                target=target,
                temp=temp,
                fingerprint=_content_fingerprint(temp),
            )
        except (_STRMStopped, TimeoutError, ValueError):
            if temp.exists():
                temp.unlink(missing_ok=True)
            raise
        except Exception as exc:
            last_exc = exc
            if temp.exists():
                temp.unlink(missing_ok=True)
            if attempt < max_retries:
                time.sleep(1.0)
                continue
            raise last_exc
    raise RuntimeError("元数据下载未产生结果")


def download_metadata(
    file: GuangYaFile,
    rel_dir: str,
    strm_root: str,
    client: Optional[GuangYaClient] = None,
    *,
    download_url: str = "",
    should_stop: Callable[[], bool] | None = None,
    before_replace: Callable[[Path], None] | None = None,
    on_replaced: Callable[[Path, str], None] | None = None,
) -> Path:
    """下载并原子替换单个元数据文件。"""
    prepared = prepare_metadata_download(
        file, rel_dir, strm_root, client,
        download_url=download_url,
        should_stop=should_stop,
    )
    try:
        if before_replace:
            before_replace(prepared.target)
        prepared.temp.replace(prepared.target)
        if on_replaced:
            on_replaced(prepared.target, prepared.fingerprint)
        return prepared.target
    finally:
        if prepared.temp.exists():
            prepared.temp.unlink(missing_ok=True)


def _candidate_sort_key(candidate: tuple[GuangYaFile, str]) -> tuple:
    file, _rel_dir = candidate
    return (-int(file.size or 0), str(file.file_id), str(file.etag), str(file.name))


def _record_candidate(
    winners: dict[str, tuple[GuangYaFile, str, int]],
    target: Path,
    candidate: tuple[GuangYaFile, str],
) -> None:
    """每个目标只保留稳定赢家与重复数，避免全量候选列表驻留。"""
    target_text = str(target)
    current = winners.get(target_text)
    if current is None:
        winners[target_text] = (candidate[0], candidate[1], 0)
        return
    current_candidate = (current[0], current[1])
    winner = candidate if _candidate_sort_key(candidate) < _candidate_sort_key(
        current_candidate
    ) else current_candidate
    winners[target_text] = (winner[0], winner[1], current[2] + 1)


def _build_video_index_maps(existing_rows: list) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    """把视频索引构造成 file_id 与 path 双索引，保留同路径多行语义。"""
    by_id: dict[str, object] = {}
    by_path: dict[str, dict[str, object]] = {}
    for row in existing_rows:
        file_id = str(_row_field(row, "file_id", "") or "")
        path = str(_row_field(row, "strm_path", "") or "")
        if not file_id:
            continue
        by_id[file_id] = row
        by_path.setdefault(path, {})[file_id] = row
    return by_id, by_path


def _current_fingerprint_backfills(
    items: list[dict[str, object]],
    existing_by_id: dict[str, object],
) -> list[dict[str, object]]:
    """只保留仍与最终内存索引一致的指纹补写，避免旧路径覆盖新快照。"""
    current_items = []
    for item in items:
        current = existing_by_id.get(str(item["file_id"]))
        if not current:
            continue
        if (
            str(_row_field(current, "strm_path", "") or "")
            != str(item["strm_path"])
            or str(_row_field(current, "etag", "") or "")
            != str(item["etag"] or "")
            or int(_row_field(current, "size", 0) or 0)
            != int(item["size"] or 0)
        ):
            continue
        current_items.append(item)
    return current_items


def _update_video_index_snapshot(
    existing_by_id: dict[str, object],
    existing_by_path: dict[str, dict[str, object]],
    file: GuangYaFile,
    expected: Path,
    content_fingerprint: str | None = None,
) -> dict[str, object]:
    """安装成功后 O(1) 维护双索引；同目标路径冲突按 bucket 清理。"""
    file_id = str(file.file_id)
    expected_text = str(expected)
    current = existing_by_id.get(file_id)
    if current is not None:
        previous_path = str(_row_field(current, "strm_path", "") or "")
        previous_bucket = existing_by_path.get(previous_path)
        if previous_bucket is not None:
            previous_bucket.pop(file_id, None)
            if not previous_bucket:
                existing_by_path.pop(previous_path, None)
    for conflicting_id in tuple(existing_by_path.get(expected_text, {})):
        if conflicting_id == file_id:
            continue
        existing_by_id.pop(conflicting_id, None)
        existing_by_path[expected_text].pop(conflicting_id, None)
    if not existing_by_path.get(expected_text):
        existing_by_path.pop(expected_text, None)
    snapshot = {
        "file_id": file_id,
        "etag": str(file.etag or ""),
        "size": int(file.size or 0),
        "filename": str(file.name or ""),
        "strm_path": expected_text,
        "content_fingerprint": content_fingerprint or _content_fingerprint(expected),
    }
    existing_by_id[file_id] = snapshot
    existing_by_path.setdefault(expected_text, {})[file_id] = snapshot
    return snapshot


def _install_video_candidate(
    file: GuangYaFile,
    rel_dir: str,
    expected: Path,
    base_url: str,
    strm_root: str,
    source_key: str,
    existing_by_id: dict[str, object],
    existing_by_path: dict[str, dict[str, object]],
) -> tuple[int, str]:
    """事务式安装视频赢家；失败时仅回滚仍由本事务持有的内容。"""
    current_raw = existing_by_id.get(str(file.file_id))
    current = _row_snapshot(current_raw) if current_raw else None
    conflicts = [
        _row_snapshot(row)
        for conflicting_id, row in existing_by_path.get(str(expected), {}).items()
        if conflicting_id != str(file.file_id)
    ]
    previous_path = _safe_indexed_path(
        current["strm_path"] if current else "", strm_root
    )
    target_owners = list(existing_by_path.get(str(expected), {}).values())
    indexed_target = bool(
        current
        and str(current.get("strm_path") or "") == str(expected)
    )
    repair_fingerprint = ""
    if expected.is_file() and indexed_target:
        # 同 file_id、同目标路径的索引足以证明这是 MediaFlux 管理的 STRM。
        # 指纹不一致表示文件被误改，应在完整/增量同步中自动修复；但仍记录
        # 当前实际内容，原子替换前再次复核，避免覆盖同步期间的新外部写入。
        repair_fingerprint = _content_fingerprint(expected)
    else:
        _require_owned_file(expected, target_owners, "覆盖 STRM")
    if previous_path and previous_path != expected:
        _require_owned_file(previous_path, [current], "删除旧 STRM")
    target_backup = _copy_backup(expected)
    previous_backup = (
        _copy_backup(previous_path)
        if previous_path and previous_path != expected else None
    )

    index_changed = False
    state = {"installed_fingerprint": "", "previous_deleted": False}
    try:
        generate_strm(
            file, rel_dir, base_url, strm_root,
            before_replace=(
                (lambda target: _require_file_snapshot(
                    target, repair_fingerprint, "修复已索引 STRM"
                ))
                if repair_fingerprint
                else (lambda target: _require_owned_file(
                    target, target_owners, "覆盖 STRM"
                ))
            ),
            on_replaced=lambda _target, fingerprint: state.__setitem__(
                "installed_fingerprint", fingerprint
            ),
        )
        db.upsert_strm_index(
            source_key, file.file_id, file.etag, file.size, file.name, str(expected),
            state["installed_fingerprint"],
            conflicting_file_ids=tuple(row["file_id"] for row in conflicts),
        )
        index_changed = True
        cleaned = 0
        if previous_path and previous_path != expected:
            state["previous_deleted"] = _delete_owned_file(
                previous_path, [current], "删除旧 STRM"
            )
            cleaned = int(state["previous_deleted"])
        _discard_backup(target_backup)
        _discard_backup(previous_backup)
        return cleaned, str(state["installed_fingerprint"] or "")
    except Exception:
        try:
            _restore_installed_file(
                expected, target_backup, state["installed_fingerprint"], "STRM"
            )
            if state["previous_deleted"] and previous_path:
                _restore_deleted_file(previous_path, previous_backup, "旧 STRM")
            else:
                _discard_backup(previous_backup)
        finally:
            _rollback_index(
                source_key, str(file.file_id), current, conflicts, index_changed
            )
        raise


def _video_generation_is_update(
    file_id: object,
    expected: Path,
    existing_by_id: dict[str, object],
) -> bool:
    """同一远端文件的内容修复或路径迁移都属于更新，而不是新建。"""
    return expected.is_file() or str(file_id) in existing_by_id


def _install_metadata_candidate(
    file: GuangYaFile,
    rel_dir: str,
    expected: Path,
    strm_root: str,
    source_key: str,
    existing_rows: list,
    download_url: str,
    client: Optional[GuangYaClient] = None,
    should_stop: Callable[[], bool] | None = None,
    prepared: PreparedMetadataDownload | None = None,
) -> int:
    """事务式安装元数据；失败时仅回滚仍由本事务持有的内容。"""
    current_raw = next(
        (row for row in existing_rows if str(row["file_id"]) == str(file.file_id)),
        None,
    )
    current = _row_snapshot(current_raw) if current_raw else None
    conflicts = [
        _row_snapshot(row) for row in existing_rows
        if str(row["file_id"]) != str(file.file_id)
        and str(row["strm_path"] or "") == str(expected)
    ]
    previous_path = _safe_indexed_path(
        current["strm_path"] if current else "", strm_root
    )
    target_owners = [
        row for row in existing_rows
        if str(_row_field(row, "strm_path", "") or "") == str(expected)
    ]
    _require_owned_file(expected, target_owners, "覆盖元数据")
    if previous_path and previous_path != expected:
        _require_owned_file(previous_path, [current], "删除旧元数据")
    target_backup = _copy_backup(expected)
    previous_backup = (
        _copy_backup(previous_path)
        if previous_path and previous_path != expected else None
    )

    index_changed = False
    state = {"installed_fingerprint": "", "previous_deleted": False}
    try:
        if prepared is not None:
            if prepared.target != expected:
                raise RuntimeError("元数据临时文件目标与任务不一致")
            _require_owned_file(expected, target_owners, "覆盖元数据")
            prepared.temp.replace(expected)
            state["installed_fingerprint"] = prepared.fingerprint
        else:
            download_metadata(
                file, rel_dir, strm_root, client, download_url=download_url,
                should_stop=should_stop,
                before_replace=lambda target: _require_owned_file(
                    target, target_owners, "覆盖元数据"
                ),
                on_replaced=lambda _target, fingerprint: state.__setitem__(
                    "installed_fingerprint", fingerprint
                ),
            )
        db.upsert_strm_index(
            source_key, file.file_id, file.etag, file.size, file.name, str(expected),
            state["installed_fingerprint"],
            conflicting_file_ids=tuple(row["file_id"] for row in conflicts),
        )
        index_changed = True
        cleaned = 0
        if previous_path and previous_path != expected:
            state["previous_deleted"] = _delete_owned_file(
                previous_path, [current], "删除旧元数据"
            )
            cleaned = int(state["previous_deleted"])
        _discard_backup(target_backup)
        _discard_backup(previous_backup)
        return cleaned
    except Exception:
        if prepared is not None and prepared.temp.exists():
            prepared.temp.unlink(missing_ok=True)
        try:
            _restore_installed_file(
                expected, target_backup, state["installed_fingerprint"], "元数据"
            )
            if state["previous_deleted"] and previous_path:
                _restore_deleted_file(previous_path, previous_backup, "旧元数据")
            else:
                _discard_backup(previous_backup)
        finally:
            _rollback_index(
                source_key, str(file.file_id), current, conflicts, index_changed
            )
        raise


def _prepare_strm_metadata_job_with_client(
    job: dict[str, object],
    strm_root: str,
    *,
    client: GuangYaClient,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """使用已确定所有权的客户端准备一个元数据任务。"""
    runtime_client = client
    file_id = str(job.get("file_id") or "")
    if not file_id:
        raise ValueError("元数据任务缺少 file_id")
    if should_stop and should_stop():
        raise _STRMStopped("元数据后台任务已停止")
    remote = runtime_client.file_info(file_id)
    if remote is None or remote.is_dir:
        raise RuntimeError("远端元数据文件不存在")
    expected_snapshot = GuangYaFile(
        file_id=file_id,
        name=str(job.get("filename") or ""),
        is_dir=False,
        size=max(0, int(job.get("size") or 0)),
        etag=str(job.get("etag") or ""),
        parent_id=str(job.get("parent_id") or ""),
    )
    for field_name in ("name", "etag", "size", "parent_id"):
        if str(getattr(remote, field_name) or "") != str(
            getattr(expected_snapshot, field_name) or ""
        ):
            raise RuntimeError("远端元数据快照已变化，等待下轮扫描更新队列")
    rel_dir = str(job.get("rel_dir") or "")
    url = str(runtime_client.get_download_url(file_id) or "")
    if not url:
        raise RuntimeError("无法获取元数据下载直链")
    prepared = prepare_metadata_download(
        remote, rel_dir, strm_root, runtime_client,
        download_url=url, should_stop=should_stop,
    )
    return {"file": remote, "rel_dir": rel_dir, "prepared": prepared}


def prepare_strm_metadata_job(
    job: dict[str, object],
    strm_root: str,
    *,
    client: GuangYaClient | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """在不持有 STRM 写锁时校验远端快照并下载到临时文件。"""
    with _guangya_client_scope(client) as runtime_client:
        return _prepare_strm_metadata_job_with_client(
            job,
            strm_root,
            client=runtime_client,
            should_stop=should_stop,
        )


def commit_strm_metadata_job(
    job: dict[str, object],
    prepared_job: dict[str, object],
    strm_root: str,
    *,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """在 STRM 写锁内原子安装已下载的元数据并更新索引。"""
    file_id = str(job.get("file_id") or "")
    source_id = str(job.get("source_id") or "")
    remote = prepared_job.get("file")
    prepared = prepared_job.get("prepared")
    if not isinstance(remote, GuangYaFile) or not isinstance(
        prepared, PreparedMetadataDownload
    ):
        raise ValueError("元数据准备结果无效")
    rel_dir = str(prepared_job.get("rel_dir") or job.get("rel_dir") or "")
    expected = _metadata_target(remote, rel_dir, strm_root)
    source_id = str(job.get("source_id") or "")
    metadata_source_key = f"guangya-meta:{source_id}"
    existing_rows = db.list_strm_index(metadata_source_key)
    current = next(
        (row for row in existing_rows if str(row["file_id"]) == file_id), None
    )
    if current and _metadata_state_matches(current, remote, expected):
        db.resolve_strm_failure_for_item(source_id, file_id, "metadata")
        prepared.temp.unlink(missing_ok=True)
        return {
            "status": "skipped", "file_id": file_id,
            "path": str(expected), "cleaned": 0,
        }
    cleaned = _install_metadata_candidate(
        remote, rel_dir, expected, strm_root, metadata_source_key,
        existing_rows, "", should_stop=should_stop, prepared=prepared,
    )
    db.resolve_strm_failure_for_item(source_id, file_id, "metadata")
    return {
        "status": "completed", "file_id": file_id,
        "path": str(expected), "cleaned": int(cleaned or 0),
    }


def _remove_indexed_item(
    source_key: str,
    file_id: str,
    strm_root: str,
    existing_by_id: dict[str, object],
    existing_by_path: dict[str, dict[str, object]],
) -> tuple[int, Path | None]:
    """精准移除单个索引项；共享路径仍有其他 owner 时不删除本地文件。"""
    current_raw = existing_by_id.get(str(file_id))
    if current_raw is None:
        return 0, None
    current = _row_snapshot(current_raw)
    indexed_path = _safe_indexed_path(current["strm_path"], strm_root)
    path_bucket = existing_by_path.get(str(current["strm_path"]), {})
    shared_path = any(item_id != str(file_id) for item_id in path_bucket)
    local_file_exists = bool(
        indexed_path and not shared_path and indexed_path.is_file()
    )
    backup = _copy_backup(indexed_path) if local_file_exists else None
    deleted = False
    try:
        if local_file_exists and indexed_path:
            deleted = _delete_owned_file(
                indexed_path, [current], "删除失效 STRM"
            )
        db.delete_strm_index_ids(source_key, [str(file_id)])
        _discard_backup(backup)
    except Exception:
        if deleted and indexed_path:
            _restore_deleted_file(indexed_path, backup, "失效 STRM")
        else:
            _discard_backup(backup)
        _restore_index_row(source_key, current)
        raise
    existing_by_id.pop(str(file_id), None)
    path_bucket.pop(str(file_id), None)
    if not path_bucket:
        existing_by_path.pop(str(current["strm_path"]), None)
    return (1 if deleted else 0), indexed_path

def _incremental_rel_dir(rel_dir: str, rel_prefix: str = "") -> str:
    raw_parts = [
        part for part in str(rel_dir or "").replace("\\", "/").split("/")
        if part not in {"", "."}
    ]
    if any(part == ".." for part in raw_parts):
        raise ValueError("STRM 精准增量目录包含非法上级路径")
    parts = [safe_path_component(part) for part in raw_parts]
    prefix = str(rel_prefix or "").strip()
    if prefix:
        parts.insert(0, safe_path_component(prefix))
    return str(Path(*parts)) if parts else ""


def _incremental_remote_file(client, change: dict) -> GuangYaFile:
    file_id = str(change.get("file_id") or "").strip()
    if not file_id:
        raise ValueError("STRM 精准增量缺少 file_id")
    current = client.file_info(file_id)
    if current is None or current.is_dir:
        raise RuntimeError(f"STRM 精准增量对象已不存在：{file_id}")
    expected_name = str(change.get("name") or "")
    expected_etag = str(change.get("etag") or "")
    expected_parent = str(change.get("parent_id") or "")
    expected_size = int(change.get("size") or 0)
    if expected_name and str(current.name or "") != expected_name:
        raise RuntimeError(f"STRM 精准增量对象名称已变化：{file_id}")
    if expected_etag and str(current.etag or "") != expected_etag:
        raise RuntimeError(f"STRM 精准增量对象版本已变化：{file_id}")
    if expected_size and int(current.size or 0) != expected_size:
        raise RuntimeError(f"STRM 精准增量对象大小已变化：{file_id}")
    if expected_parent and str(current.parent_id or "") != expected_parent:
        raise RuntimeError(f"STRM 精准增量对象位置已变化：{file_id}")
    return current


def _metadata_state_matches(row, file: GuangYaFile, expected: Path) -> bool:
    return bool(
        str(row["file_id"]) == str(file.file_id)
        and str(row["etag"] or "") == str(file.etag or "")
        and int(row["size"] or 0) == int(file.size or 0)
        and str(row["strm_path"] or "") == str(expected)
        and expected.is_file()
        and (not file.size or expected.stat().st_size == int(file.size))
        and _fingerprint_matches(expected, _row_field(row, "content_fingerprint", ""))
    )


def _append_error_sample(stats: dict, stage: str, subject: object, exc: object) -> None:
    samples = stats.setdefault("error_samples", [])
    if not isinstance(samples, list) or len(samples) >= 3:
        return
    subject_text = str(subject or "").strip() or "未知对象"
    text = redact_sensitive_text(f"{stage} {subject_text}：{exc}")[:300]
    if text and text not in samples:
        samples.append(text)


class _BoundedProgress:
    """按阶段和 10% 桶限流，调用方可以在逐项循环中安全上报。"""

    def __init__(self, callback):
        self.callback = callback
        self._last: dict[str, int] = {}

    def emit(self, stage: str, completed: int, total: int, detail: str) -> None:
        if not self.callback:
            return
        bounded_total = max(1, int(total or 0))
        bounded_completed = max(0, min(int(completed or 0), bounded_total))
        percent = int(bounded_completed * 100 / bounded_total)
        bucket = min(100, (percent // 10) * 10)
        if self._last.get(stage) == bucket:
            return
        self._last[stage] = bucket
        try:
            self.callback(stage, bounded_completed, bounded_total, detail)
        except Exception as exc:
            logger.warning("STRM 进度回调失败 stage=%s type=%s", stage, type(exc).__name__)


def _failure_target_rel_path(target: Path, strm_root: str) -> str:
    try:
        resolved = target.expanduser().resolve(strict=False)
        root = Path(strm_root).expanduser().resolve(strict=False)
        target_norm = _normalized_absolute(resolved)
        root_norm = _normalized_absolute(root)
        if target_norm == root_norm:
            return "."
        sep = os.sep
        prefix = root_norm if root_norm.endswith(sep) else f"{root_norm}{sep}"
        if target_norm.startswith(prefix):
            rel = str(resolved)[len(str(root)):].lstrip("\\/")
            return Path(rel).as_posix()
        return resolved.relative_to(root).as_posix()
    except (ValueError, Exception):
        return target.name


def _record_failure(
    *, source_id: str, source_name: str, file: GuangYaFile, action: str,
    rel_dir: str, target: Path, strm_root: str, error: object,
) -> int:
    return db.record_strm_failure(
        source_id=source_id, source_name=source_name, file_id=str(file.file_id),
        parent_id=str(file.parent_id or ""), filename=file.name, action=action,
        rel_dir=rel_dir, target_rel_path=_failure_target_rel_path(target, strm_root),
        error=error,
    )


def _flush_failure_resolutions(
    stats: dict, source_id: str, pending: dict[str, set[str]], action: str | None = None,
) -> None:
    """短事务批量关闭成功项的失败台账；台账异常不反向破坏已落盘文件。"""
    actions = (action,) if action else tuple(pending)
    for current_action in actions:
        file_ids = pending.get(current_action, set())
        if not file_ids:
            continue
        started = time.monotonic()
        try:
            db.resolve_strm_failures_for_items(
                source_id, tuple(file_ids), current_action,
            )
            stats["failure_resolve_batches"] = int(
                stats.get("failure_resolve_batches", 0) or 0
            ) + 1
        except Exception as exc:
            stats["failure_ledger_failed"] = int(
                stats.get("failure_ledger_failed", 0) or 0
            ) + 1
            logger.warning(
                "批量更新 STRM 失败台账失败 source=%s action=%s type=%s",
                source_id, current_action, type(exc).__name__,
            )
        finally:
            stats["failure_resolve_elapsed_seconds"] = round(
                float(stats.get("failure_resolve_elapsed_seconds", 0.0) or 0.0)
                + time.monotonic() - started,
                3,
            )
            file_ids.clear()


def _sync_strm_incremental_impl(
    source_dir_id: str,
    changes: list[dict],
    base_url: str,
    strm_root: str,
    client: Optional[GuangYaClient] = None,
    video_exts: Optional[set[str]] = None,
    skip_threshold_mb: int = 0,
    rel_prefix: str = "",
    metadata_exts: Optional[set[str]] = None,
    source_name: str = "",
    on_progress=None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """只处理整理成功的最终对象；不做全量扫描、退役来源或失效清理。"""
    client = client or GuangYaClient()
    exts = video_exts or DEFAULT_VIDEO_EXTS
    metadata = metadata_exts or set()
    threshold_bytes = skip_threshold_mb * 1024 * 1024
    display_source_name = str(source_name or rel_prefix or source_dir_id)
    progress = _BoundedProgress(on_progress)
    stats = {
        "total": 0, "generated": 0, "created": 0, "updated": 0,
        "skipped": 0, "failed": 0,
        "duplicates_skipped": 0,
        "metadata_total": 0, "metadata_generated": 0, "metadata_queued": 0,
        "metadata_queue_failed": 0, "metadata_queue_cancelled": 0,
        "metadata_skipped": 0, "metadata_failed": 0,
        "metadata_cleaned": 0, "cleaned": 0, "clean_skipped": False,
        "empty_dirs_cleaned": 0, "directories": 0, "scanned_files": 0,
        "scan_elapsed_seconds": 0.0, "metadata_elapsed_seconds": 0.0,
        "generate_elapsed_seconds": 0.0, "cleanup_elapsed_seconds": 0.0,
        "failure_resolve_batches": 0, "failure_resolve_elapsed_seconds": 0.0,
        "failure_ledger_failed": 0,
        "error_samples": [], "changes": [], "omitted_count": 0,
        "changed_strm_paths": [], "changed_dirs": [], "changed_paths_omitted": 0,
        "stopped": False, "stop_stage": "", "mode": "incremental",
        "fallback_required": False, "fallback_reason": "",
    }
    pending_resolutions = {"generate": set(), "metadata": set()}
    fingerprint_backfills: list[dict[str, object]] = []

    def stop_requested(stage: str) -> bool:
        if should_stop and should_stop():
            stats["stopped"] = True
            stats["stop_stage"] = stage
            stats["clean_skipped"] = True
            return True
        return False

    normalized: dict[tuple[str, str], dict] = {}
    for raw in changes or []:
        if not isinstance(raw, dict):
            stats["fallback_required"] = True
            stats["fallback_reason"] = "整理联动包含无效 STRM 变更项"
            return stats
        if str(raw.get("source_id") or "") != str(source_dir_id):
            stats["fallback_required"] = True
            stats["fallback_reason"] = "整理联动 STRM 来源与配置不一致"
            return stats
        kind = str(raw.get("kind") or "video").lower()
        action = str(raw.get("action") or "upsert").lower()
        file_id = str(raw.get("file_id") or "").strip()
        if kind not in {"video", "metadata"} or action not in {"upsert", "remove"} or not file_id:
            stats["fallback_required"] = True
            stats["fallback_reason"] = "整理联动 STRM 变更字段不完整"
            return stats
        normalized[(kind, file_id)] = {**raw, "kind": kind, "action": action}

    video_key = f"guangya:{source_dir_id}"
    metadata_key = f"guangya-meta:{source_dir_id}"
    video_by_id, video_by_path = _build_video_index_maps(db.list_strm_index(video_key))
    metadata_by_id, metadata_by_path = _build_video_index_maps(
        db.list_strm_index(metadata_key)
    )
    upserts = [item for item in normalized.values() if item["action"] == "upsert"]
    removals = [item for item in normalized.values() if item["action"] == "remove"]
    total_work = len(upserts) + len(removals)
    completed = 0
    generate_started = time.monotonic()
    progress.emit("generate", 0, total_work, "精准更新 STRM")

    for change in upserts:
        if stop_requested("incremental"):
            _flush_failure_resolutions(stats, source_dir_id, pending_resolutions)
            stats["generate_elapsed_seconds"] = round(
                time.monotonic() - generate_started, 3
            )
            return stats
        kind = change["kind"]
        file_id = str(change["file_id"])
        try:
            file = _incremental_remote_file(client, change)
            rel_dir = _incremental_rel_dir(
                str(change.get("rel_dir") or ""), rel_prefix
            )
            ext = file.name.rsplit(".", 1)[-1].lower() if "." in file.name else ""
            if kind == "video":
                stats["total"] += 1
                if ext not in exts:
                    raise RuntimeError("精准增量对象不属于已配置视频类型")
                if threshold_bytes and 0 < file.size < threshold_bytes:
                    stats["skipped"] += 1
                    completed += 1
                    progress.emit("generate", completed, total_work, "精准更新 STRM")
                    continue
                expected = _strm_target(file, rel_dir, strm_root)
                current = video_by_id.get(file_id)
                expected_url = build_play_url(
                    base_url, file.file_id, file.etag, file.size, file.name
                )
                content_matches, content_fingerprint = _read_strm_state(
                    expected, expected_url
                )
                is_current = bool(
                    current
                    and str(_row_field(current, "strm_path", "") or "") == str(expected)
                    and str(_row_field(current, "etag", "") or "") == str(file.etag or "")
                    and int(_row_field(current, "size", 0) or 0) == int(file.size or 0)
                    and content_matches
                )
                if is_current:
                    if str(_row_field(current, "content_fingerprint", "") or "") != content_fingerprint:
                        fingerprint_backfills.append({
                            "file_id": file.file_id,
                            "etag": file.etag,
                            "size": file.size,
                            "filename": file.name,
                            "strm_path": str(expected),
                            "content_fingerprint": content_fingerprint,
                        })
                        _update_video_index_snapshot(
                            video_by_id, video_by_path, file, expected,
                            content_fingerprint,
                        )
                    stats["skipped"] += 1
                else:
                    is_update = _video_generation_is_update(
                        file.file_id, expected, video_by_id
                    )
                    cleaned, installed_fingerprint = _install_video_candidate(
                        file, rel_dir, expected, base_url, strm_root, video_key,
                        video_by_id, video_by_path,
                    )
                    stats["cleaned"] += cleaned
                    _update_video_index_snapshot(
                        video_by_id, video_by_path, file, expected,
                        installed_fingerprint,
                    )
                    stats["generated"] += 1
                    stats["updated" if is_update else "created"] += 1
                    _track_change(stats, "generated", expected, strm_root)
                pending_resolutions["generate"].add(file_id)
            else:
                stats["metadata_total"] += 1
                if ext not in metadata:
                    raise RuntimeError("精准增量对象不属于已配置伴随文件类型")
                expected = _metadata_target(file, rel_dir, strm_root)
                current = metadata_by_id.get(file_id)
                if current and _metadata_state_matches(current, file, expected):
                    stats["metadata_skipped"] += 1
                    pending_resolutions["metadata"].add(file_id)
                else:
                    queued = db.enqueue_strm_metadata_jobs([
                        _metadata_queue_payload(
                            file, rel_dir, strm_root,
                            source_id=source_dir_id,
                            source_name=display_source_name,
                            force=True,
                        )
                    ])
                    if int(queued.get("failed", 0) or 0):
                        stats["metadata_queue_failed"] += 1
                    else:
                        stats["metadata_queued"] += 1
        except Exception as exc:
            label = str(change.get("name") or file_id)
            logger.warning("STRM 精准增量失败 %s: %s", label, exc)
            key = "metadata_failed" if kind == "metadata" else "failed"
            stats[key] += 1
            stats["fallback_required"] = True
            stats["fallback_reason"] = f"{label}: {redact_sensitive_text(str(exc))}"
            _append_error_sample(stats, "精准增量", label, exc)
            break
        completed += 1
        progress.emit("generate", completed, total_work, "精准更新 STRM")

    _flush_failure_resolutions(stats, source_dir_id, pending_resolutions)
    if fingerprint_backfills:
        db.upsert_strm_index_batch(video_key, fingerprint_backfills)

    if not stats["fallback_required"] and not stats["stopped"]:
        for change in removals:
            if stop_requested("incremental-cleanup"):
                stats["generate_elapsed_seconds"] = round(
                    time.monotonic() - generate_started, 3
                )
                return stats
            kind = change["kind"]
            file_id = str(change["file_id"])
            try:
                source_key = metadata_key if kind == "metadata" else video_key
                by_id = metadata_by_id if kind == "metadata" else video_by_id
                by_path = metadata_by_path if kind == "metadata" else video_by_path
                if kind == "metadata":
                    db.cancel_strm_metadata_job(
                        source_dir_id, file_id,
                        reason="精准增量确认远端元数据已删除",
                    )
                cleaned, removed_path = _remove_indexed_item(
                    source_key, file_id, strm_root, by_id, by_path
                )
                if kind == "metadata":
                    stats["metadata_cleaned"] += cleaned
                else:
                    stats["cleaned"] += cleaned
                if removed_path is not None:
                    _track_change(stats, "removed", removed_path, strm_root)
            except Exception as exc:
                logger.warning("STRM 精准清理失败 file=%s: %s", file_id, exc)
                stats["fallback_required"] = True
                stats["fallback_reason"] = f"清理 {file_id} 失败：{redact_sensitive_text(str(exc))}"
                _append_error_sample(stats, "精准清理", file_id, exc)
                break
            completed += 1
            progress.emit("generate", completed, total_work, "精准更新 STRM")

    stats["generate_elapsed_seconds"] = round(
        time.monotonic() - generate_started, 3
    )
    progress.emit("complete", 1, 1, "精准同步完成")
    logger.info(
        "STRM 精准同步完成 dir=%s 视频=%s 生成=%s 元数据=%s 回退=%s",
        source_dir_id, stats["total"], stats["generated"],
        stats["metadata_generated"], stats["fallback_required"],
    )
    return stats


def sync_strm_incremental(
    source_dir_id: str,
    changes: list[dict],
    base_url: str,
    strm_root: str,
    client: Optional[GuangYaClient] = None,
    video_exts: Optional[set[str]] = None,
    skip_threshold_mb: int = 0,
    rel_prefix: str = "",
    metadata_exts: Optional[set[str]] = None,
    source_name: str = "",
    on_progress=None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """执行可信变化的精准同步，并释放本函数创建的光鸭连接池。"""
    with _guangya_client_scope(client) as runtime_client:
        return _sync_strm_incremental_impl(
            source_dir_id=source_dir_id,
            changes=changes,
            base_url=base_url,
            strm_root=strm_root,
            client=runtime_client,
            video_exts=video_exts,
            skip_threshold_mb=skip_threshold_mb,
            rel_prefix=rel_prefix,
            metadata_exts=metadata_exts,
            source_name=source_name,
            on_progress=on_progress,
            should_stop=should_stop,
        )


def _sync_strm_impl(
    source_dir_id: str,
    base_url: str,
    strm_root: str,
    client: Optional[GuangYaClient] = None,
    video_exts: Optional[set[str]] = None,
    skip_threshold_mb: int = 0,
    rel_prefix: str = "",
    metadata_exts: Optional[set[str]] = None,
    clean_invalid: bool = True,
    clean_empty_dirs: bool = True,
    deferred_cleanup_actions: list[Callable[[], None]] | None = None,
    scan_workers: int | None = None,
    verify_workers: int | None = None,
    source_name: str = "",
    on_progress=None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """递归扫描光鸭目录，为视频生成 STRM。"""
    if client is None:
        client = GuangYaClient()
    exts = video_exts or DEFAULT_VIDEO_EXTS
    metadata = metadata_exts or set()
    threshold_bytes = skip_threshold_mb * 1024 * 1024
    scan_worker_count = max(
        1,
        min(
            int(scan_workers or get_int("STRM_SCAN_WORKERS", DEFAULT_SCAN_WORKERS)),
            MAX_SCAN_WORKERS,
        ),
    )
    verify_worker_count = max(
        1,
        min(
            int(
                verify_workers
                or get_int("STRM_VERIFY_WORKERS", DEFAULT_VERIFY_WORKERS)
            ),
            MAX_VERIFY_WORKERS,
        ),
    )
    max_directories, max_entries, max_candidates, scan_deadline_seconds = _scan_limits()

    stats = {
        "total": 0, "generated": 0, "created": 0, "updated": 0,
        "skipped": 0, "failed": 0,
        "duplicates_skipped": 0,
        "metadata_total": 0, "metadata_generated": 0, "metadata_queued": 0,
        "metadata_queue_failed": 0, "metadata_queue_cancelled": 0,
        "metadata_skipped": 0, "metadata_failed": 0, "metadata_cleaned": 0,
        "cleaned": 0, "clean_skipped": False, "empty_dirs_cleaned": 0,
        "directories": 0, "scan_entries": 0, "scanned_files": 0,
        "scan_elapsed_seconds": 0.0,
        "directory_requests": 0, "scan_pages": 0, "read_retries": 0,
        "rate_limit_retries": 0, "read_failures": 0,
        "request_p50_ms": 0.0, "request_p95_ms": 0.0,
        "request_p99_ms": 0.0,
        "scan_workers_configured": scan_worker_count,
        "scan_workers_peak": 0, "scan_queue_peak": 0,
        "verify_workers_configured": verify_worker_count,
        "verified_candidates": 0, "verify_prefiltered": 0,
        "scan_incomplete": False, "scan_limit_reason": "",
        "generate_elapsed_seconds": 0.0, "metadata_elapsed_seconds": 0.0,
        "cleanup_elapsed_seconds": 0.0,
        "failure_resolve_batches": 0, "failure_resolve_elapsed_seconds": 0.0,
        "failure_ledger_failed": 0, "error_samples": [],
        "changes": [], "omitted_count": 0,
        "changed_strm_paths": [], "changed_dirs": [], "changed_paths_omitted": 0,
        "stopped": False, "stop_stage": "",
    }
    source_key = f"guangya:{source_dir_id}"
    metadata_source_key = f"guangya-meta:{source_dir_id}"
    display_source_name = str(source_name or rel_prefix or source_dir_id)
    progress = _BoundedProgress(on_progress)
    seen_ids: set[str] = set()
    metadata_seen_ids: set[str] = set()
    pending_metadata_targets: set[str] = set()
    scan_errors = 0
    consistency_errors = 0
    pending_resolutions = {"generate": set(), "metadata": set()}
    video_candidates: dict[str, tuple[GuangYaFile, str, int]] = {}
    metadata_candidates: dict[str, tuple[GuangYaFile, str, int]] = {}

    prefix = safe_path_component(rel_prefix) if rel_prefix.strip() else ""
    scan_started = time.monotonic()
    scan_deadline = scan_started + scan_deadline_seconds
    scan_abort = threading.Event()
    scan_deadline_hit = threading.Event()
    scan_entry_budget_hit = threading.Event()
    scan_entry_budget_lock = threading.Lock()
    scan_entry_budget_probe_lock = threading.Lock()
    scan_entry_budget_used = 0
    active_scan_workers = 0
    active_scan_workers_lock = threading.Lock()

    def mark_scan_incomplete(reason: str, detail: str) -> None:
        if not stats["scan_incomplete"]:
            stats["scan_limit_reason"] = reason
            _append_error_sample(
                stats,
                "扫描目录",
                source_dir_id,
                RuntimeError(detail),
            )
        stats["scan_incomplete"] = True
        stats["clean_skipped"] = True

    def page_scan_stop_requested() -> bool:
        if scan_abort.is_set() or scan_entry_budget_hit.is_set():
            return True
        if should_stop and should_stop():
            return True
        if time.monotonic() > scan_deadline:
            scan_deadline_hit.set()
            return True
        return False

    def stop_requested(stage: str) -> bool:
        if should_stop and should_stop():
            stats["stopped"] = True
            stats["stop_stage"] = stage
            stats["clean_skipped"] = True
            return True
        return False

    def scan_tree(initial_dir_id: str, initial_parts: tuple[str, ...]) -> None:
        nonlocal scan_errors
        pending_dirs = deque([(initial_dir_id, initial_parts)])
        visited_dir_ids: set[str] = set()

        def list_directory(dir_id: str) -> list[GuangYaFile]:
            nonlocal active_scan_workers, scan_entry_budget_used
            if scan_abort.is_set():
                return []
            with active_scan_workers_lock:
                active_scan_workers += 1
                stats["scan_workers_peak"] = max(
                    int(stats["scan_workers_peak"]), active_scan_workers
                )
            try:
                files = iter(_iter_client_dir(
                    client,
                    dir_id,
                    should_stop=page_scan_stop_requested,
                    max_items=max_entries,
                ))
                collected: list[GuangYaFile] = []
                while True:
                    # 在读取下一个远端条目前原子预留全局配额；空迭代器会
                    # 归还预留。并发 worker 的总读取量因此不会成倍越界。
                    with scan_entry_budget_lock:
                        at_limit = scan_entry_budget_used >= max_entries
                        if not at_limit:
                            scan_entry_budget_used += 1
                    if at_limit:
                        # 只允许一个 worker 做无配额 lookahead，用来区分
                        # “恰好等于上限”与“确实还有更多条目”。最多额外读取
                        # 一个条目，不会随 scan_workers 成倍放大。
                        with scan_entry_budget_probe_lock:
                            if scan_entry_budget_hit.is_set():
                                break
                            try:
                                next(files)
                            except StopIteration:
                                break
                            scan_entry_budget_hit.set()
                        break
                    try:
                        item = next(files)
                    except StopIteration:
                        with scan_entry_budget_lock:
                            scan_entry_budget_used -= 1
                        break
                    except BaseException:
                        with scan_entry_budget_lock:
                            scan_entry_budget_used -= 1
                        raise
                    collected.append(item)
                return collected
            finally:
                with active_scan_workers_lock:
                    active_scan_workers -= 1

        def abort_scan(reason: str = "", detail: str = "") -> None:
            if reason:
                mark_scan_incomplete(reason, detail)
            scan_abort.set()
            pending_dirs.clear()

        with ThreadPoolExecutor(
            max_workers=scan_worker_count,
            thread_name_prefix="strm-dir-scan",
        ) as executor:
            inflight: dict[object, tuple[str, tuple[str, ...]]] = {}
            while (pending_dirs or inflight) and not scan_abort.is_set():
                if stop_requested("scan"):
                    abort_scan()
                    break
                if time.monotonic() > scan_deadline:
                    abort_scan("deadline", "云端目录扫描超过总时限")
                    break

                while pending_dirs and len(inflight) < scan_worker_count:
                    dir_id, rel_parts = pending_dirs.popleft()
                    identity = str(dir_id)
                    if identity in visited_dir_ids:
                        continue
                    if len(visited_dir_ids) >= max_directories:
                        abort_scan("directories", "云端目录数量超过扫描上限")
                        break
                    visited_dir_ids.add(identity)
                    future = executor.submit(list_directory, identity)
                    inflight[future] = (identity, rel_parts)
                stats["scan_queue_peak"] = max(
                    int(stats["scan_queue_peak"]), len(pending_dirs) + len(inflight)
                )
                if scan_abort.is_set() or not inflight:
                    continue

                completed, _pending = wait(
                    tuple(inflight), timeout=0.05, return_when=FIRST_COMPLETED
                )
                if not completed:
                    continue
                for future in completed:
                    dir_id, rel_parts = inflight.pop(future)
                    try:
                        files = future.result()
                    except ValueError:
                        abort_scan()
                        raise
                    except Exception as exc:
                        logger.error("列目录失败 %s: %s", dir_id, exc)
                        stats["failed"] += 1
                        scan_errors += 1
                        if not stats["scan_limit_reason"]:
                            stats["scan_limit_reason"] = "directory_error"
                        stats["scan_incomplete"] = True
                        stats["clean_skipped"] = True
                        _append_error_sample(stats, "扫描目录", dir_id, exc)
                        abort_scan()
                        break

                    if scan_entry_budget_hit.is_set():
                        abort_scan("entries", "云端目录条目超过扫描上限")
                        break

                    if scan_deadline_hit.is_set() or time.monotonic() > scan_deadline:
                        abort_scan("deadline", "云端目录扫描超过总时限")
                        break

                    stats["directories"] += 1

                    child_dirs: list[tuple[str, tuple[str, ...]]] = []
                    for file in files:
                        if stop_requested("scan"):
                            abort_scan()
                            break
                        if time.monotonic() > scan_deadline:
                            abort_scan("deadline", "云端目录扫描超过总时限")
                            break
                        stats["scan_entries"] += 1
                        if stats["scan_entries"] > max_entries:
                            abort_scan("entries", "云端目录条目超过扫描上限")
                            break
                        if file.is_dir:
                            child_dirs.append((
                                str(file.file_id),
                                (*rel_parts, safe_path_component(file.name)),
                            ))
                            continue
                        stats["scanned_files"] += 1
                        rel_dir = str(Path(*rel_parts)) if rel_parts else ""
                        ext = file.name.rsplit(".", 1)[-1].lower() if "." in file.name else ""
                        if ext in metadata:
                            target = _metadata_target(file, rel_dir, strm_root)
                            target_key = str(target)
                            if (
                                target_key not in metadata_candidates
                                and len(video_candidates) + len(metadata_candidates) >= max_candidates
                            ):
                                abort_scan("candidates", "STRM 候选数量超过扫描上限")
                                break
                            stats["metadata_total"] += 1
                            _record_candidate(metadata_candidates, target, (file, rel_dir))
                            continue
                        if ext not in exts:
                            continue
                        if threshold_bytes and 0 < file.size < threshold_bytes:
                            continue
                        target = _strm_target(file, rel_dir, strm_root)
                        target_key = str(target)
                        if (
                            target_key not in video_candidates
                            and len(video_candidates) + len(metadata_candidates) >= max_candidates
                        ):
                            abort_scan("candidates", "STRM 候选数量超过扫描上限")
                            break
                        stats["total"] += 1
                        _record_candidate(video_candidates, target, (file, rel_dir))
                    if not scan_abort.is_set():
                        pending_dirs.extend(child_dirs)

            if scan_abort.is_set():
                for future in inflight:
                    future.cancel()
        if scan_deadline_hit.is_set() and not stats["stopped"]:
            mark_scan_incomplete("deadline", "云端目录扫描超过总时限")
        stats["scan_entries"] = max(
            int(stats["scan_entries"]), int(scan_entry_budget_used)
        )

    progress.emit("scan", 0, 1, "扫描云端目录")
    begin_metrics = getattr(client, "begin_read_metrics", None)
    end_metrics = getattr(client, "end_read_metrics", None)
    read_metrics = begin_metrics() if callable(begin_metrics) else None
    try:
        scan_tree(source_dir_id, (prefix,) if prefix else ())
    finally:
        if read_metrics is not None and callable(end_metrics):
            stats.update(end_metrics(read_metrics))
    progress.emit("scan", 1, 1, "扫描云端目录")
    stats["scan_elapsed_seconds"] = round(time.monotonic() - scan_started, 3)
    if stats["stopped"]:
        return stats
    if scan_errors or stats["scan_incomplete"]:
        stats["clean_skipped"] = True
        logger.warning(
            "STRM 扫描不完整，跳过本轮生成、元数据同步与失效清理 "
            "source=%s errors=%s reason=%s entries=%s directories=%s",
            source_dir_id,
            scan_errors,
            stats["scan_limit_reason"],
            stats["scan_entries"],
            stats["directories"],
        )
        return stats

    existing_rows = db.list_strm_index(source_key)
    existing_by_id, existing_by_path = _build_video_index_maps(existing_rows)
    fingerprint_backfills: list[dict[str, object]] = []
    video_progress_total = len(video_candidates)
    for _file, _rel_dir, duplicate_count in video_candidates.values():
        stats["duplicates_skipped"] += duplicate_count
        stats["skipped"] += duplicate_count
    generate_started = time.monotonic()
    progress.emit("generate", 0, video_progress_total, "生成 STRM")
    sorted_video_targets = sorted(video_candidates)
    with ThreadPoolExecutor(
        max_workers=verify_worker_count,
        thread_name_prefix="strm-verify",
    ) as verify_executor:
        for batch_start in range(0, video_progress_total, verify_worker_count):
            prepared_candidates = []
            for target_text in sorted_video_targets[
                batch_start:batch_start + verify_worker_count
            ]:
                file, rel_dir, _duplicate_count = video_candidates[target_text]
                expected = Path(target_text)
                current = existing_by_id.get(str(file.file_id))
                expected_url = build_play_url(
                    base_url, file.file_id, file.etag, file.size, file.name
                )
                index_matches = bool(
                    current
                    and str(current["strm_path"] or "") == str(expected)
                    and str(current["etag"] or "") == str(file.etag or "")
                    and int(current["size"] or 0) == int(file.size or 0)
                )
                verify_future = (
                    verify_executor.submit(
                        _read_strm_state, expected, expected_url
                    )
                    if index_matches
                    else None
                )
                if verify_future is None:
                    stats["verify_prefiltered"] += 1
                prepared_candidates.append((
                    target_text, file, rel_dir, expected, current,
                    index_matches, verify_future,
                ))

            # 先等待本批所有只读校验结束，再开始任何文件替换，避免同批
            # 路径迁移或冲突清理与仍在执行的读取互相干扰。
            verification_results = {}
            for prepared in prepared_candidates:
                target_text = prepared[0]
                verify_future = prepared[-1]
                if verify_future is None:
                    continue
                verification_results[target_text] = verify_future.result()
                stats["verified_candidates"] += 1

            for batch_offset, prepared in enumerate(prepared_candidates, 1):
                (
                    target_text, file, rel_dir, expected, current,
                    index_matches, verify_future,
                ) = prepared
                video_completed = batch_start + batch_offset
                if stop_requested("generate"):
                    for remaining in prepared_candidates[batch_offset - 1:]:
                        future = remaining[-1]
                        if future is not None:
                            future.cancel()
                    _flush_failure_resolutions(
                        stats, source_dir_id, pending_resolutions, "generate"
                    )
                    stats["generate_elapsed_seconds"] = round(
                        time.monotonic() - generate_started, 3
                    )
                    return stats
                if verify_future is not None:
                    content_matches, content_fingerprint = (
                        verification_results[target_text]
                    )
                else:
                    content_matches, content_fingerprint = False, ""
                # 同一批内更早候选可能更新或清理冲突索引，因此提交判定必须
                # 重新读取内存快照；并发线程只负责文件读取，不拥有业务状态。
                current = existing_by_id.get(str(file.file_id))
                index_matches = bool(
                    current
                    and str(current["strm_path"] or "") == str(expected)
                    and str(current["etag"] or "") == str(file.etag or "")
                    and int(current["size"] or 0) == int(file.size or 0)
                )
                is_current = bool(index_matches and content_matches)
                if is_current:
                    if str(
                        _row_field(current, "content_fingerprint", "") or ""
                    ) != content_fingerprint:
                        fingerprint_backfills.append({
                            "file_id": file.file_id,
                            "etag": file.etag,
                            "size": file.size,
                            "filename": file.name,
                            "strm_path": str(expected),
                            "content_fingerprint": content_fingerprint,
                        })
                        _update_video_index_snapshot(
                            existing_by_id, existing_by_path, file, expected,
                            content_fingerprint,
                        )
                    seen_ids.add(str(file.file_id))
                    stats["skipped"] += 1
                    pending_resolutions["generate"].add(str(file.file_id))
                    progress.emit(
                        "generate", video_completed, video_progress_total,
                        "生成 STRM",
                    )
                    continue
                try:
                    is_update = _video_generation_is_update(
                        file.file_id, expected, existing_by_id
                    )
                    cleaned, installed_fingerprint = _install_video_candidate(
                        file, rel_dir, expected, base_url, strm_root, source_key,
                        existing_by_id, existing_by_path,
                    )
                    stats["cleaned"] += cleaned
                    seen_ids.add(str(file.file_id))
                    stats["generated"] += 1
                    stats["updated" if is_update else "created"] += 1
                    pending_resolutions["generate"].add(str(file.file_id))
                    _track_change(stats, "generated", expected, strm_root)
                    _update_video_index_snapshot(
                        existing_by_id, existing_by_path, file, expected,
                        installed_fingerprint,
                    )
                except Exception as exc:
                    logger.debug("生成 STRM 失败 file=%s type=%s", redact_sensitive_text(file.name)[:160], type(exc).__name__)
                    stats["failed"] += 1
                    consistency_errors += 1
                    _append_error_sample(stats, "生成 STRM", file.name, exc)
                    _record_failure(
                        source_id=source_dir_id,
                        source_name=display_source_name,
                        file=file,
                        action="generate",
                        rel_dir=rel_dir,
                        target=expected,
                        strm_root=strm_root,
                        error=exc,
                    )
                    append_change(
                        stats,
                        relative_change(
                            "failed", expected, strm_root, error=exc
                        ),
                    )
                progress.emit(
                    "generate", video_completed, video_progress_total,
                    "生成 STRM",
                )

    if fingerprint_backfills:
        # 同一远端 file_id 若在本轮后续候选中发生路径迁移，较早缓存的
        # 指纹补写不得覆盖新索引；仅提交仍与最终内存快照一致的项目。
        current_backfills = _current_fingerprint_backfills(
            fingerprint_backfills, existing_by_id
        )
        if current_backfills:
            db.upsert_strm_index_batch(source_key, current_backfills)
    _flush_failure_resolutions(stats, source_dir_id, pending_resolutions, "generate")
    stats["generate_elapsed_seconds"] = round(
        time.monotonic() - generate_started, 3
    )

    metadata_started = time.monotonic()
    metadata_progress_total = len(metadata_candidates)
    metadata_progress_completed = 0
    if metadata:
        progress.emit("metadata", 0, metadata_progress_total, "同步元数据")
        existing_metadata_rows = db.list_strm_index(metadata_source_key)
        metadata_rows_by_id = {
            str(row["file_id"]): row for row in existing_metadata_rows
        }
        planned_metadata_targets = {
            target_text: str(candidate[0].file_id)
            for target_text, candidate in metadata_candidates.items()
        }
        if metadata_candidates:
            progress.emit("metadata", 0, metadata_progress_total, "排队伴随元数据")
            queue_batch: list[dict[str, object]] = []
            queue_files: list[tuple[GuangYaFile, str, Path]] = []

            def flush_metadata_queue_batch() -> None:
                nonlocal consistency_errors, metadata_progress_completed
                if not queue_batch:
                    return
                try:
                    queued = db.enqueue_strm_metadata_jobs(queue_batch)
                    failed_count = int(queued.get("failed", 0) or 0)
                    stats["metadata_queue_failed"] += failed_count
                    stats["metadata_queued"] += max(0, len(queue_batch) - failed_count)
                except Exception as exc:
                    logger.warning("持久化元数据队列失败 count=%s: %s", len(queue_batch), exc)
                    stats["metadata_failed"] += len(queue_batch)
                    consistency_errors += len(queue_batch)
                    for file, rel_dir, expected in queue_files:
                        _append_error_sample(stats, "排队元数据", file.name, exc)
                        _record_failure(
                            source_id=source_dir_id,
                            source_name=display_source_name,
                            file=file,
                            action="metadata",
                            rel_dir=rel_dir,
                            target=expected,
                            strm_root=strm_root,
                            error=exc,
                        )
                finally:
                    metadata_progress_completed += len(queue_batch)
                    progress.emit(
                        "metadata", metadata_progress_completed,
                        metadata_progress_total, "排队伴随元数据",
                    )
                    queue_batch.clear()
                    queue_files.clear()

            for target_text in sorted(metadata_candidates):
                if stop_requested("metadata-queue"):
                    flush_metadata_queue_batch()
                    _flush_failure_resolutions(
                        stats, source_dir_id, pending_resolutions, "metadata"
                    )
                    stats["metadata_elapsed_seconds"] = round(
                        time.monotonic() - metadata_started, 3
                    )
                    return stats
                file, rel_dir, duplicate_count = metadata_candidates[target_text]
                stats["metadata_skipped"] += duplicate_count
                expected = Path(target_text)
                file_id = str(file.file_id)
                current = metadata_rows_by_id.get(file_id)
                metadata_seen_ids.add(file_id)
                previous_path = _safe_indexed_path(
                    current["strm_path"] if current else "", strm_root
                )
                previous_target_owner = (
                    planned_metadata_targets.get(str(previous_path))
                    if previous_path and previous_path != expected
                    else None
                )
                preflight_error: Exception | None = None
                if previous_target_owner and previous_target_owner != file_id:
                    preflight_error = RuntimeError(
                        "检测到元数据路径交叉改名，已为避免覆盖而跳过"
                    )
                else:
                    try:
                        target_owners = [
                            row for row in existing_metadata_rows
                            if str(row["strm_path"] or "") == str(expected)
                        ]
                        _require_owned_file(expected, target_owners, "排队元数据覆盖")
                        if previous_path and previous_path != expected:
                            _require_owned_file(
                                previous_path,
                                [current] if current is not None else [],
                                "排队元数据迁移",
                            )
                    except _STRMOwnershipError as exc:
                        # 明知本地文件已由用户修改时不应继续下载并让后台队列
                        # 永久重试；扫描阶段只做所有权预检，不执行第二套写入。
                        preflight_error = exc
                if preflight_error is not None:
                    stats["metadata_failed"] += 1
                    consistency_errors += 1
                    _append_error_sample(
                        stats, "排队元数据", file.name, preflight_error
                    )
                    _record_failure(
                        source_id=source_dir_id,
                        source_name=display_source_name,
                        file=file,
                        action="metadata",
                        rel_dir=rel_dir,
                        target=expected,
                        strm_root=strm_root,
                        error=preflight_error,
                    )
                    append_change(
                        stats,
                        relative_change(
                            "failed", expected, strm_root, error=preflight_error
                        ),
                    )
                    metadata_progress_completed += 1
                    progress.emit(
                        "metadata", metadata_progress_completed,
                        metadata_progress_total, "排队伴随元数据",
                    )
                    continue
                if current and _metadata_state_matches(current, file, expected):
                    stats["metadata_skipped"] += 1
                    pending_resolutions["metadata"].add(str(file.file_id))
                    metadata_progress_completed += 1
                    progress.emit(
                        "metadata", metadata_progress_completed,
                        metadata_progress_total, "排队伴随元数据",
                    )
                    continue
                queue_batch.append(_metadata_queue_payload(
                    file, rel_dir, strm_root,
                    source_id=source_dir_id,
                    source_name=display_source_name,
                    force=True,
                ))
                queue_files.append((file, rel_dir, expected))
                pending_metadata_targets.add(str(expected))
                if len(queue_batch) >= 500:
                    flush_metadata_queue_batch()
            flush_metadata_queue_batch()
    _flush_failure_resolutions(stats, source_dir_id, pending_resolutions, "metadata")
    stats["metadata_elapsed_seconds"] = round(time.monotonic() - metadata_started, 3)
    if stats["stopped"] or stop_requested("cleanup"):
        return stats

    cleanup_started = time.monotonic()
    if clean_invalid or deferred_cleanup_actions is not None:
        progress.emit(
            "cleanup", 0, 1,
            "等待整轮清理安全门" if deferred_cleanup_actions is not None else "清理失效索引",
        )
        decision = evaluate_cleanup_decision(
            stats,
            scan_errors=scan_errors,
            consistency_errors=consistency_errors,
        )
        if not apply_cleanup_decision(stats, decision):
            logger.warning(
                "STRM 清理安全门拒绝本轮失效清理 source=%s reasons=%s "
                "scan=%s consistency=%s",
                source_dir_id,
                "; ".join(decision.reasons) or "未知",
                scan_errors,
                consistency_errors,
            )
        else:
            def perform_cleanup() -> None:
                action_started = time.monotonic()
                removed_dir_paths: set[str] = set()
                metadata_queue_cleanup_ready = True
                if metadata:
                    try:
                        stats["metadata_queue_cancelled"] += db.cancel_stale_strm_metadata_jobs(
                            source_dir_id, metadata_seen_ids
                        )
                    except Exception as exc:
                        metadata_queue_cleanup_ready = False
                        stats["clean_skipped"] = True
                        _append_error_sample(
                            stats, "清理元数据队列", display_source_name, exc
                        )
                        logger.warning(
                            "取消失效元数据队列失败 source=%s type=%s",
                            source_dir_id, type(exc).__name__,
                        )
                cleanup = clean_invalid_strm(
                    strm_root,
                    source_key=source_key,
                    valid_ids=seen_ids,
                    strm_only=True,
                    clean_empty_dirs=False,
                    should_stop=should_stop,
                )
                stats["clean_skipped"] = bool(stats["clean_skipped"]) or bool(
                    cleanup.get("skipped")
                )
                stats["cleaned"] += cleanup["cleaned"]
                stats["empty_dirs_cleaned"] += cleanup["empty_dirs_cleaned"]
                for removed_path in cleanup.get("removed_paths", []):
                    _track_change(stats, "removed", removed_path, strm_root)
                removed_dir_paths.update(cleanup.get("removed_dir_paths", []))
                if metadata and metadata_queue_cleanup_ready:
                    # 新 file_id 正在后台替换同一路径时，旧索引仍是当前磁盘文件
                    # 的唯一所有者。必须保留到 worker 原子提交新文件；否则本轮
                    # 清理会先删除旧文件，使下载/索引失败失去可回滚内容。
                    metadata_cleanup_ids = set(metadata_seen_ids)
                    if pending_metadata_targets:
                        metadata_cleanup_ids.update(
                            str(row["file_id"])
                            for row in existing_metadata_rows
                            if str(row["strm_path"] or "")
                            in pending_metadata_targets
                        )
                    metadata_cleanup = clean_invalid_strm(
                        strm_root,
                        source_key=metadata_source_key,
                        valid_ids=metadata_cleanup_ids,
                        clean_empty_dirs=False,
                        should_stop=should_stop,
                    )
                    stats["clean_skipped"] = (
                        bool(stats["clean_skipped"])
                        or bool(metadata_cleanup.get("skipped"))
                    )
                    stats["metadata_cleaned"] += metadata_cleanup["cleaned"]
                    stats["empty_dirs_cleaned"] += metadata_cleanup["empty_dirs_cleaned"]
                    for removed_path in metadata_cleanup.get("removed_paths", []):
                        _track_change(stats, "removed", removed_path, strm_root)
                    removed_dir_paths.update(metadata_cleanup.get("removed_dir_paths", []))
                if clean_empty_dirs and not (should_stop and should_stop()):
                    empty_cleanup = clean_empty_strm_dirs(
                        strm_root, should_stop=should_stop
                    )
                    stats["empty_dirs_cleaned"] += int(
                        empty_cleanup.get("empty_dirs_cleaned", 0) or 0
                    )
                    stats["clean_skipped"] = bool(stats["clean_skipped"]) or bool(
                        empty_cleanup.get("stopped")
                    )
                    removed_dir_paths.update(
                        empty_cleanup.get("removed_dir_paths") or []
                    )
                for removed_dir_path in sorted(removed_dir_paths):
                    _track_change(stats, "removed_dir", removed_dir_path, strm_root)
                stats["cleanup_elapsed_seconds"] = round(
                    float(stats.get("cleanup_elapsed_seconds", 0.0) or 0.0)
                    + max(0.0, time.monotonic() - action_started),
                    3,
                )

            if deferred_cleanup_actions is not None:
                deferred_cleanup_actions.append(perform_cleanup)
            else:
                perform_cleanup()
        progress.emit(
            "cleanup", 1, 1,
            "等待整轮清理安全门" if deferred_cleanup_actions is not None else "清理失效索引",
        )
    if deferred_cleanup_actions is None:
        stats["cleanup_elapsed_seconds"] = round(
            max(
                float(stats.get("cleanup_elapsed_seconds", 0.0) or 0.0),
                time.monotonic() - cleanup_started,
            ),
            3,
        )
    progress.emit("complete", 1, 1, "同步完成")
    logger.info(
        f"STRM 同步完成 dir={source_dir_id}: "
        f"视频={stats['total']} 生成={stats['generated']} 跳过={stats['skipped']} 失败={stats['failed']}"
    )
    return stats


def sync_strm(
    source_dir_id: str,
    base_url: str,
    strm_root: str,
    client: Optional[GuangYaClient] = None,
    video_exts: Optional[set[str]] = None,
    skip_threshold_mb: int = 0,
    rel_prefix: str = "",
    metadata_exts: Optional[set[str]] = None,
    clean_invalid: bool = True,
    clean_empty_dirs: bool = True,
    deferred_cleanup_actions: list[Callable[[], None]] | None = None,
    scan_workers: int | None = None,
    verify_workers: int | None = None,
    source_name: str = "",
    on_progress=None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """执行一次全量同步；只关闭本函数创建的光鸭连接池。"""
    with _guangya_client_scope(client) as runtime_client:
        return _sync_strm_impl(
            source_dir_id=source_dir_id,
            base_url=base_url,
            strm_root=strm_root,
            client=runtime_client,
            video_exts=video_exts,
            skip_threshold_mb=skip_threshold_mb,
            rel_prefix=rel_prefix,
            metadata_exts=metadata_exts,
            clean_invalid=clean_invalid,
            clean_empty_dirs=clean_empty_dirs,
            deferred_cleanup_actions=deferred_cleanup_actions,
            scan_workers=scan_workers,
            verify_workers=verify_workers,
            source_name=source_name,
            on_progress=on_progress,
            should_stop=should_stop,
        )


def parse_strm_sources(
    raw,
    *,
    require_nonempty: bool = True,
) -> tuple[list[dict[str, str]], str]:
    items = []
    if raw is not None and (not isinstance(raw, str) or bool(raw.strip())):
        if isinstance(raw, str):
            try:
                items = json.loads(raw)
            except (TypeError, ValueError):
                return [], "GY_STRM_SOURCE_DIRS 不是有效 JSON"
        else:
            items = raw
        if not isinstance(items, list):
            return [], "GY_STRM_SOURCE_DIRS 必须是数组"

    sources: list[dict[str, str]] = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            source_id, name = item.strip(), f"源目录{index + 1}"
        elif isinstance(item, dict):
            source_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip() or f"源目录{index + 1}"
        else:
            return [], "GY_STRM_SOURCE_DIRS 包含无效来源"
        if not source_id or source_id == "0":
            return [], "STRM 来源 ID 不能为空且不能为根目录"
        if all(row["id"] != source_id for row in sources):
            sources.append({"id": source_id, "name": name})
    if require_nonempty and not sources:
        return [], "未配置光鸭 STRM 源目录"
    return sources, ""


def plan_strm_sources(sources: list[dict[str, str]]) -> list[dict[str, str]]:
    name_counts: dict[str, int] = {}
    for source in sources:
        name = str(source["name"])
        name_counts[name] = name_counts.get(name, 0) + 1
    multiple = len(sources) > 1
    planned = []
    for source in sources:
        source_id = str(source["id"])
        name = str(source["name"])
        prefix = name
        if name_counts[name] > 1:
            prefix = f"{name} ({source_id[-6:]})"
        planned.append({
            "id": source_id,
            "name": name,
            "rel_prefix": prefix if multiple else "",
            "source_key": f"guangya:{source_id}",
            "metadata_source_key": f"guangya-meta:{source_id}",
        })
    return planned


def configured_strm_source_plans() -> tuple[list[dict[str, str]], str]:
    sources, error = parse_strm_sources(get("GY_STRM_SOURCE_DIRS", ""))
    return (plan_strm_sources(sources), "") if not error else ([], error)


def _retry_runtime_config() -> tuple[dict, str]:
    sources, source_error = configured_strm_source_plans()
    if source_error:
        return {}, source_error
    root_text = get("STRM_ROOT", "").strip()
    if not root_text:
        return {}, "未配置 STRM 本地根目录"
    base_url = get("GY_STRM_BASE_URL", "").strip()
    if not base_url:
        return {}, "未配置 STRM 播放服务地址"
    root = Path(root_text).expanduser().resolve(strict=False)
    if root.exists() and not root.is_dir():
        return {}, "STRM 本地根目录不是目录"
    return {"sources": sources, "strm_root": str(root), "base_url": base_url}, ""


def capture_strm_retry_runtime_config() -> tuple[dict, str]:
    """冻结一次已解析的 STRM 重试配置，供确认动作在执行时复用。"""
    runtime, error = _retry_runtime_config()
    return deepcopy(runtime), str(error or "")


@dataclass(frozen=True)
class _RetryLookupResult:
    located: dict[str, tuple[GuangYaFile, str, dict[str, str]]]
    directories: int
    entries: int
    scan_incomplete: bool
    scan_limit_reason: str
    stopped: bool


def _locate_retry_files(
    client,
    sources: list[dict[str, str]],
    wanted: set[str],
    *,
    should_stop: Callable[[], bool] | None = None,
) -> _RetryLookupResult:
    """在有界目录树中重新定位失败项，并显式返回扫描完整性。"""
    located: dict[str, tuple[GuangYaFile, str, dict[str, str]]] = {}
    max_directories, max_entries, _max_candidates, deadline_seconds = _scan_limits()
    scan_deadline = time.monotonic() + deadline_seconds
    directories = 0
    entries = 0
    scan_incomplete = False
    scan_limit_reason = ""
    stopped = False

    def mark_incomplete(reason: str) -> None:
        nonlocal scan_incomplete, scan_limit_reason
        scan_incomplete = True
        if not scan_limit_reason:
            scan_limit_reason = reason

    def stop_requested() -> bool:
        nonlocal stopped
        if should_stop is not None and should_stop():
            stopped = True
            return True
        return False

    def deadline_exceeded() -> bool:
        if time.monotonic() >= scan_deadline:
            mark_incomplete("deadline")
            return True
        return False

    abort_scan = False
    for source in sources:
        if abort_scan or wanted <= located.keys():
            break
        try:
            prefix = str(source.get("rel_prefix") or "")
            initial = (safe_path_component(prefix),) if prefix else ()
            stack: list[tuple[str, tuple[str, ...]]] = [(str(source["id"]), initial)]
            visited_dir_ids: set[str] = set()
            while stack and not wanted <= located.keys():
                if stop_requested():
                    abort_scan = True
                    break
                if deadline_exceeded():
                    abort_scan = True
                    break
                if directories >= max_directories:
                    mark_incomplete("directories")
                    abort_scan = True
                    break
                dir_id, rel_parts = stack.pop()
                if dir_id in visited_dir_ids:
                    continue
                visited_dir_ids.add(dir_id)
                directories += 1

                def page_scan_stop_requested() -> bool:
                    if stop_requested():
                        return True
                    if deadline_exceeded():
                        return True
                    if entries >= max_entries:
                        mark_incomplete("entries")
                        return True
                    return False

                items = _iter_client_dir(
                    client,
                    dir_id,
                    should_stop=page_scan_stop_requested,
                    max_items=max_entries - entries,
                )
                child_dirs: list[tuple[str, tuple[str, ...]]] = []
                items = iter(items)
                while not wanted <= located.keys():
                    if stop_requested():
                        abort_scan = True
                        break
                    if deadline_exceeded():
                        abort_scan = True
                        break
                    if entries >= max_entries:
                        mark_incomplete("entries")
                        abort_scan = True
                        break
                    try:
                        item = next(items)
                    except StopIteration:
                        break
                    entries += 1
                    if item.is_dir:
                        child_dirs.append((
                            str(item.file_id),
                            (*rel_parts, safe_path_component(item.name)),
                        ))
                    elif str(item.file_id) in wanted and str(item.file_id) not in located:
                        rel_dir = str(Path(*rel_parts)) if rel_parts else ""
                        located[str(item.file_id)] = (item, rel_dir, source)
                if abort_scan:
                    break
                stack.extend(reversed(child_dirs))
        except Exception as exc:
            mark_incomplete("directory_error")
            logger.warning("重试重新解析来源失败 %s: %s", source["id"], exc)
    if wanted <= located.keys():
        scan_incomplete = False
        scan_limit_reason = ""
    return _RetryLookupResult(
        located=located,
        directories=directories,
        entries=entries,
        scan_incomplete=scan_incomplete,
        scan_limit_reason=scan_limit_reason,
        stopped=stopped,
    )


def _retry_result(trigger_type: str, *, requested: int = 0) -> dict:
    return {
        "ok": True, "requested": int(requested), "matched": 0,
        "resolved": 0, "failed": 0, "missing": 0, "stale": 0,
        "deferred": 0, "attempted": 0, "batches": 0,
        "scan_incomplete": False, "scan_limit_reason": "",
        "scan_directories": 0, "scan_entries": 0,
        "stopped": False, "stop_stage": "",
        "trigger_type": str(trigger_type),
    }


def _process_claimed_strm_failures(
    rows: list,
    *,
    client: GuangYaClient,
    runtime: dict,
    lookup: _RetryLookupResult,
    result: dict,
    progress: _BoundedProgress,
    progress_offset: int,
    progress_total: int,
) -> None:
    """处理已 claim 的失败项；扫描结果可由单选或“全部”重试共享。"""
    located = lookup.located
    base_url = runtime["base_url"]
    strm_root = runtime["strm_root"]
    video_index_maps: dict[str, tuple[dict[str, object], dict[str, dict[str, object]]]] = {}
    metadata_index_maps: dict[str, tuple[dict[str, object], dict[str, dict[str, object]]]] = {}
    for completed, row in enumerate(rows, 1):
        failure_id = int(row["id"])
        try:
            resolved = located.get(str(row["file_id"]))
            if resolved is None:
                if lookup.scan_incomplete or lookup.stopped:
                    deferred_error = (
                        "STRM 重试扫描已停止，尚未确认云端对象状态"
                        if lookup.stopped
                        else "STRM 重试扫描不完整，尚未确认云端对象状态"
                    )
                    if not db.release_strm_failure_retry(
                        failure_id,
                        error=deferred_error,
                        expected_status="retrying",
                    ):
                        raise RuntimeError("STRM deferred 状态已变化，拒绝覆盖")
                    result["deferred"] += 1
                    continue
                stale_error = "stale: 云端对象已不存在或已移出已配置 STRM 来源"
                if not db.mark_strm_failure_stale(
                    failure_id, error=stale_error, expected_status="retrying"
                ):
                    raise RuntimeError("STRM missing 状态已变化，拒绝覆盖")
                result["missing"] += 1
                result["stale"] += 1
                result["failed"] += 1
                continue
            file, rel_dir, source = resolved
            action = str(row["action"] or "")
            target = (
                _strm_target(file, rel_dir, strm_root)
                if action == "generate"
                else _metadata_target(file, rel_dir, strm_root)
            )
            try:
                target = _require_target_within_root(target, strm_root)
                if action == "generate":
                    source_key = source["source_key"]
                    maps = video_index_maps.get(source_key)
                    if maps is None:
                        maps = _build_video_index_maps(db.list_strm_index(source_key))
                        video_index_maps[source_key] = maps
                    existing_by_id, existing_by_path = maps
                    _cleaned, installed_fingerprint = _install_video_candidate(
                        file, rel_dir, target, base_url, strm_root, source_key,
                        existing_by_id, existing_by_path,
                    )
                    _update_video_index_snapshot(
                        existing_by_id, existing_by_path, file, target,
                        installed_fingerprint,
                    )
                elif action == "metadata":
                    url = str(client.get_download_url(file.file_id) or "")
                    if not url:
                        raise RuntimeError("无法获取元数据下载直链")
                    source_key = source["metadata_source_key"]
                    maps = metadata_index_maps.get(source_key)
                    if maps is None:
                        maps = _build_video_index_maps(db.list_strm_index(source_key))
                        metadata_index_maps[source_key] = maps
                    existing_by_id, existing_by_path = maps
                    _install_metadata_candidate(
                        file, rel_dir, target, strm_root, source_key,
                        list(existing_by_id.values()), url,
                    )
                    _update_video_index_snapshot(
                        existing_by_id, existing_by_path, file, target
                    )
                else:
                    raise ValueError("未知 STRM 重试动作")
                if not db.resolve_strm_failure(
                    failure_id, expected_status="retrying"
                ):
                    raise RuntimeError("STRM 失败项状态已变化，拒绝覆盖")
                result["resolved"] += 1
            except Exception as exc:
                if not db.update_strm_failure_retry(
                    failure_id, source_id=source["id"],
                    source_name=source["name"], file=file, rel_dir=rel_dir,
                    target_rel_path=_failure_target_rel_path(target, strm_root),
                    error=exc, expected_status="retrying",
                ):
                    logger.warning(
                        "STRM 重试失败状态已变化 failure=%s", failure_id
                    )
                result["failed"] += 1
        finally:
            progress.emit(
                "retry", progress_offset + completed, progress_total, "重试失败项"
            )


def _retry_strm_failures_locked(
    ids: list[int], trigger_type: str, *, client: Optional[GuangYaClient] = None,
    on_progress=None, runtime_config: Optional[dict] = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    normalized = list(dict.fromkeys(int(item) for item in ids))
    candidates = db.list_strm_failures(status="open", ids=normalized, limit=1000)
    result = _retry_result(trigger_type, requested=len(normalized))
    result["attempted"] = len(normalized)
    result["batches"] = 1 if normalized else 0
    if not candidates:
        return result
    if runtime_config is None:
        runtime, config_error = _retry_runtime_config()
        if config_error:
            return {**result, "ok": False, "error": config_error}
    else:
        runtime = deepcopy(runtime_config)
    rows = db.claim_strm_failures([int(row["id"]) for row in candidates], limit=1000)
    result["matched"] = len(rows)
    if not rows:
        return result
    with _guangya_client_scope(client) as runtime_client:
        lookup = _locate_retry_files(
            runtime_client,
            runtime["sources"],
            {str(row["file_id"]) for row in rows},
            should_stop=should_stop,
        )
        result.update({
            "scan_incomplete": lookup.scan_incomplete,
            "scan_limit_reason": lookup.scan_limit_reason,
            "scan_directories": lookup.directories,
            "scan_entries": lookup.entries,
            "stopped": lookup.stopped,
            "stop_stage": "scan" if lookup.stopped else "",
        })
        progress = _BoundedProgress(on_progress)
        progress.emit("retry", 0, len(rows), "重试失败项")
        _process_claimed_strm_failures(
            rows,
            client=runtime_client,
            runtime=runtime,
            lookup=lookup,
            result=result,
            progress=progress,
            progress_offset=0,
            progress_total=len(rows),
        )
        return result


def retry_strm_failures(
    ids: list[int], trigger_type: str, *, client: Optional[GuangYaClient] = None,
    on_progress=None, runtime_config: Optional[dict] = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    if not STRM_OPERATION_LOCK.acquire(blocking=False):
        return {
            **_retry_result(trigger_type, requested=len(set(ids))),
            "ok": False, "error": "STRM 同步或重试任务正在运行",
        }
    try:
        return _retry_strm_failures_locked(
            ids, trigger_type, client=client, on_progress=on_progress,
            runtime_config=runtime_config, should_stop=should_stop,
        )
    finally:
        STRM_OPERATION_LOCK.release()


def retry_all_strm_failures(
    source_id: str,
    action: str,
    trigger_type: str,
    *,
    client: Optional[GuangYaClient] = None,
    on_progress=None,
    runtime_config: Optional[dict] = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """在一次锁和一次来源扫描内重试当前筛选范围的全部失败项。"""
    if not STRM_OPERATION_LOCK.acquire(blocking=False):
        return {
            **_retry_result(trigger_type),
            "ok": False, "error": "STRM 同步或重试任务正在运行",
        }
    try:
        snapshot: list[tuple[int, str]] = []
        before_id: int | None = None
        while True:
            candidates = db.list_strm_failures(
                status="open",
                source_id=str(source_id or "").strip(),
                action=str(action or ""),
                before_id=before_id,
                limit=1000,
            )
            if not candidates:
                break
            snapshot.extend(
                (int(row["id"]), str(row["file_id"])) for row in candidates
            )
            before_id = min(int(row["id"]) for row in candidates)

        result = _retry_result(trigger_type, requested=len(snapshot))
        result["attempted"] = len(snapshot)
        if not snapshot:
            return result
        if runtime_config is None:
            runtime, config_error = _retry_runtime_config()
            if config_error:
                return {**result, "ok": False, "error": config_error}
        else:
            runtime = deepcopy(runtime_config)
        with _guangya_client_scope(client) as runtime_client:
            lookup = _locate_retry_files(
                runtime_client,
                runtime["sources"],
                {file_id for _, file_id in snapshot},
                should_stop=should_stop,
            )
            result.update({
                "scan_incomplete": lookup.scan_incomplete,
                "scan_limit_reason": lookup.scan_limit_reason,
                "scan_directories": lookup.directories,
                "scan_entries": lookup.entries,
                "stopped": lookup.stopped,
                "stop_stage": "scan" if lookup.stopped else "",
            })
            progress = _BoundedProgress(on_progress)
            total = len(snapshot)
            progress.emit("retry", 0, total, "重试失败项")
            for offset in range(0, total, 1000):
                batch = snapshot[offset:offset + 1000]
                rows = db.claim_strm_failures(
                    [failure_id for failure_id, _ in batch], limit=1000
                )
                result["batches"] += 1
                result["matched"] += len(rows)
                _process_claimed_strm_failures(
                    rows,
                    client=runtime_client,
                    runtime=runtime,
                    lookup=lookup,
                    result=result,
                    progress=progress,
                    progress_offset=offset,
                    progress_total=total,
                )
                progress.emit(
                    "retry",
                    min(offset + len(batch), total),
                    total,
                    "重试失败项",
                )
            return result
    finally:
        STRM_OPERATION_LOCK.release()


def clean_retired_strm_sources(
    active_source_ids: set[str], *,
    should_stop: Callable[[], bool] | None = None,
    active_ids_complete: bool = True,
    clean_empty_dirs: bool = True,
) -> dict:
    """清理已从配置移除的来源；仅删除可证明由 MediaFlux 生成的文件。"""
    active_ids = {str(item) for item in active_source_ids if str(item)}
    if not active_ids_complete:
        # 活跃来源集不完整时无法证明某个来源确已退役，必须整体保留。
        blocked = len(db.list_strm_retired_sources())
        if blocked:
            logger.warning(
                "活跃 STRM 来源集不完整，已保留全部退役来源 count=%s", blocked
            )
        return {
            "sources": blocked, "cleaned": 0, "index_cleaned": 0,
            "empty_dirs_cleaned": 0, "blocked": blocked, "removed_paths": [],
            "removed_dir_paths": [],
            "empty_dir_roots": [],
            "errors": (
                ["活跃来源集不完整，已跳过退役来源清理"] if blocked else []
            ),
            "stopped": False,
        }
    active_namespaces = {
        namespace
        for source_id in active_ids
        for namespace in (f"guangya:{source_id}", f"guangya-meta:{source_id}")
    }
    active_paths = {
        str(row["strm_path"] or "")
        for prefix in ("guangya:", "guangya-meta:")
        for row in db.list_strm_index_by_prefix(prefix)
        if str(row["source"] or "") in active_namespaces and str(row["strm_path"] or "")
    }
    result = {
        "sources": 0, "cleaned": 0, "index_cleaned": 0,
        "empty_dirs_cleaned": 0, "blocked": 0, "removed_paths": [],
        "removed_dir_paths": [], "empty_dir_roots": [],
        "errors": [], "stopped": False,
    }
    for retired in db.list_strm_retired_sources():
        if should_stop and should_stop():
            result["stopped"] = True
            break
        source_id = str(retired["source_id"] or "")
        if source_id in active_ids:
            db.delete_strm_retired_source(source_id)
            continue
        result["sources"] += 1
        root_text = str(retired["strm_root"] or "").strip()
        if not root_text:
            error = "退役来源缺少原 STRM_ROOT，已安全保留"
            db.update_strm_retired_source_error(source_id, error)
            result["blocked"] += 1
            result["errors"].append(f"{source_id}: {error}")
            continue
        owned_root = (Path(root_text).expanduser() / STRM_SUBDIR).resolve(strict=False)
        rows = []
        for namespace in (f"guangya:{source_id}", f"guangya-meta:{source_id}"):
            rows.extend(db.list_strm_index(namespace))
        blocked = 0
        for row in rows:
            if should_stop and should_stop():
                result["stopped"] = True
                blocked += 1
                break
            namespace = str(row["source"] or "")
            file_id = str(row["file_id"] or "")
            path_text = str(row["strm_path"] or "").strip()
            if not path_text:
                db.delete_strm_index_ids(namespace, [file_id])
                result["index_cleaned"] += 1
                continue
            try:
                path = Path(path_text)
                if not path.is_absolute():
                    path = Path(root_text).expanduser() / path
                resolved = path.resolve(strict=False)
                resolved.relative_to(owned_root)
            except (OSError, RuntimeError, ValueError):
                blocked += 1
                result["blocked"] += 1
                result["errors"].append(f"{source_id}: 索引路径越界 {path_text}")
                continue
            if path_text in active_paths or str(resolved) in active_paths:
                db.delete_strm_index_ids(namespace, [file_id])
                result["index_cleaned"] += 1
                continue
            if not resolved.exists():
                db.delete_strm_index_ids(namespace, [file_id])
                result["index_cleaned"] += 1
                continue
            if not resolved.is_file():
                blocked += 1
                result["blocked"] += 1
                result["errors"].append(f"{source_id}: 目标不是普通文件 {path_text}")
                continue
            verified = _fingerprint_matches(
                resolved, _row_field(row, "content_fingerprint", "")
            )
            if not verified:
                blocked += 1
                result["blocked"] += 1
                result["errors"].append(f"{source_id}: 文件所有权无法验证 {path_text}")
                continue
            try:
                _delete_owned_file(resolved, [row], "清理退役 STRM")
            except (OSError, _STRMOwnershipError) as exc:
                blocked += 1
                result["blocked"] += 1
                result["errors"].append(f"{source_id}: 删除失败 {exc}")
                continue
            db.delete_strm_index_ids(namespace, [file_id])
            result["cleaned"] += 1
            result["index_cleaned"] += 1
            result["removed_paths"].append(str(resolved))
        if result["stopped"]:
            db.update_strm_retired_source_error(source_id, "服务停止，退役清理已安全中止")
            break
        if blocked:
            error = f"仍有 {blocked} 个文件无法安全清理"
            db.update_strm_retired_source_error(source_id, error)
            continue
        db.delete_strm_retired_source(source_id)
        if clean_empty_dirs:
            empty_cleanup = clean_empty_strm_dirs(
                root_text, should_stop=should_stop, owned_root=owned_root,
            )
            result["empty_dirs_cleaned"] += int(
                empty_cleanup.get("empty_dirs_cleaned", 0) or 0
            )
            result["removed_dir_paths"].extend(
                list(empty_cleanup.get("removed_dir_paths") or [])
            )
            result["stopped"] = bool(result["stopped"]) or bool(
                empty_cleanup.get("stopped")
            )
        else:
            result["empty_dir_roots"].append(str(owned_root))
    return result


def clean_empty_strm_dirs(
    strm_root: str,
    *,
    should_stop: Callable[[], bool] | None = None,
    owned_root: Path | None = None,
) -> dict:
    """整轮任务结束后自底向上清理一次共享 STRM 空目录。"""
    root = owned_root or (Path(strm_root) / STRM_SUBDIR)
    if not root.exists():
        return {"empty_dirs_cleaned": 0, "removed_dir_paths": [], "stopped": False}
    empty_dirs = 0
    removed_dir_paths: list[str] = []
    stopped = False
    # os.walk(topdown=False) 原生提供后序遍历，避免先物化并排序整个目录树；
    # 百万级目录下内存保持常量级，停止请求也能在每个目录边界生效。
    for current, _dirs, _files in os.walk(root, topdown=False):
        directory = Path(current)
        if directory == root:
            continue
        if should_stop and should_stop():
            stopped = True
            break
        try:
            if not any(directory.iterdir()):
                directory.rmdir()
                empty_dirs += 1
                removed_dir_paths.append(str(directory))
        except OSError:
            continue
    return {
        "empty_dirs_cleaned": empty_dirs,
        "removed_dir_paths": removed_dir_paths,
        "stopped": stopped,
    }


def clean_invalid_strm(
    strm_root: str,
    client: Optional[GuangYaClient] = None,
    *,
    source_key: str = "",
    valid_ids: Optional[set[str]] = None,
    strm_only: bool = False,
    clean_empty_dirs: bool = True,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """基于持久化索引批量清理，不逐文件请求播放直链。"""
    root = Path(strm_root) / STRM_SUBDIR
    if not root.exists():
        return {
            "cleaned": 0, "checked": 0, "empty_dirs_cleaned": 0,
            "skipped": False, "removed_paths": [], "removed_dir_paths": [],
        }
    if not source_key or valid_ids is None:
        logger.warning("STRM 清理缺少完整扫描上下文，已安全跳过")
        return {
            "cleaned": 0, "checked": 0, "empty_dirs_cleaned": 0,
            "skipped": True, "removed_paths": [], "removed_dir_paths": [],
        }

    rows = db.list_strm_index(source_key)
    if strm_only:
        rows = [
            row for row in rows
            if str(row["strm_path"] or "").lower().endswith(".strm")
        ]
    stale = [row for row in rows if row["file_id"] not in valid_ids]
    active_paths = {
        str(row["strm_path"] or "")
        for row in rows
        if row["file_id"] in valid_ids and str(row["strm_path"] or "")
    }

    # 索引属于持久化外部状态，STRM_ROOT 可能在部署后发生变化。任何陈旧
    # 索引只要指向当前光鸭 STRM 根之外，就整批跳过该命名空间的清理，
    # 既不删除文件也不删除索引，避免配置漂移或数据库污染造成越界误删。
    resolved_root = root.resolve(strict=False)
    unsafe_rows = []
    for row in stale:
        path_text = str(row["strm_path"] or "").strip()
        if not path_text:
            continue
        try:
            resolved_path = Path(path_text).resolve(strict=False)
            resolved_path.relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError):
            unsafe_rows.append(row)
    if unsafe_rows:
        logger.error(
            "STRM 清理检测到越界索引，已跳过命名空间 source=%s unsafe=%s",
            source_key, len(unsafe_rows),
        )
        return {
            "cleaned": 0, "checked": len(rows), "empty_dirs_cleaned": 0,
            "skipped": True, "unsafe_paths_count": len(unsafe_rows),
            "removed_paths": [], "removed_dir_paths": [],
        }

    removed_ids: list[str] = []
    removed_paths: list[str] = []
    blocked_paths: list[str] = []
    cleaned = 0
    stopped = False
    for row in stale:
        if should_stop and should_stop():
            stopped = True
            break
        path_text = str(row["strm_path"] or "")
        path = Path(path_text) if path_text else None
        try:
            if path and path_text not in active_paths and path.is_file():
                try:
                    _require_owned_file(path, [row], "清理失效 STRM")
                except _STRMOwnershipError as exc:
                    blocked_paths.append(path_text)
                    logger.debug("STRM 所有权校验阻止清理 type=%s", type(exc).__name__)
                    continue
                _delete_owned_file(path, [row], "清理失效 STRM")
                cleaned += 1
                removed_paths.append(str(path))
            removed_ids.append(row["file_id"])
        except _STRMOwnershipError as exc:
            blocked_paths.append(path_text)
            logger.debug("STRM 所有权校验阻止清理 type=%s", type(exc).__name__)
        except OSError as exc:
            logger.debug("清理无效 STRM 失败 type=%s", type(exc).__name__)
    db.delete_strm_index_ids(source_key, removed_ids)

    empty_dirs = 0
    removed_dir_paths: list[str] = []
    if clean_empty_dirs and not stopped:
        empty_cleanup = clean_empty_strm_dirs(strm_root, should_stop=should_stop)
        empty_dirs = int(empty_cleanup.get("empty_dirs_cleaned", 0) or 0)
        removed_dir_paths = list(empty_cleanup.get("removed_dir_paths") or [])
        stopped = bool(empty_cleanup.get("stopped"))
    logger.info(
        f"STRM 索引清理完成 source={source_key}: 检查={len(rows)} 清理={cleaned} 空目录={empty_dirs}"
    )
    return {
        "cleaned": cleaned,
        "checked": len(rows),
        "empty_dirs_cleaned": empty_dirs,
        "skipped": bool(blocked_paths) or stopped,
        "stopped": stopped,
        "ownership_blocked": len(blocked_paths),
        "blocked_paths": blocked_paths,
        "removed_paths": removed_paths,
        "removed_dir_paths": removed_dir_paths,
    }
