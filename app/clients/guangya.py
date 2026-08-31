"""光鸭云盘客户端（基于 guangyaclient 库实现）。

依赖：pip install guangyaclient  (DDSRem-Dev/guangyaclient, MIT, PyPI 0.0.2)
覆盖能力：短信登录/刷新、文件管理、下载直链、秒传、云下载(离线)、分享转存。

对外保持 GuangYaClient 接口稳定，上层 routes 无需改动。
token 持久化于运行数据目录的 guangya_token.json。
"""
from __future__ import annotations

import hashlib
import logging
import json
import math
import os
import re
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from secrets import token_hex

import httpx
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import monotonic, sleep, time
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from app.config import PATHS
from app.logger import get_logger, log_throttled
from app.modules.process_lock import CrossProcessLock
from app.private_files import protect_private_file

logger = get_logger(__name__)


def close_guangya_client(client: object | None) -> bool:
    """尽力释放短生命周期光鸭 Client，不让清理异常覆盖业务结果。"""
    if client is None:
        return True
    close = getattr(client, "close", None)
    if not callable(close):
        return True
    try:
        closed = close()
    except Exception as exc:
        logger.warning("关闭光鸭 HTTP Client 失败 type=%s", type(exc).__name__)
        return False
    if closed is False:
        logger.warning("关闭光鸭 HTTP Client 失败，已保留句柄供后续重试")
        return False
    return True


def _close_raw_client(raw: object | None) -> bool:
    """释放登录前临时 SDK Client；兼容没有 close 的测试替身。"""
    if raw is None:
        return True
    close = getattr(raw, "close", None)
    if callable(close):
        try:
            closed = close()
        except Exception as exc:
            logger.warning("关闭光鸭 SDK Client 失败 type=%s", type(exc).__name__)
            return False
        if closed is False:
            logger.warning("关闭光鸭 SDK Client 失败，已保留句柄供后续重试")
            return False
    return True


TOKEN_FILE = PATHS.token_file
TOKEN_EXPIRY_MIN = 1_577_836_800  # 2020-01-01T00:00:00Z
TOKEN_EXPIRY_MAX = 4_102_444_800  # 2100-01-01T00:00:00Z
_TOKEN_LOCKS_GUARD = threading.Lock()
_TOKEN_LOCKS: dict[Path, threading.RLock] = {}
_TOKEN_PROCESS_LOCKS: dict[Path, CrossProcessLock] = {}
# 整理与 STRM 链路会创建大量短命客户端，每个实例都会加载一次 token。
# 该指纹只用于抑制重复日志，不参与任何凭据有效性判定。
_LAST_LOGGED_TOKEN_FINGERPRINT = ""


_READ_METRICS_MAX_LATENCY_SAMPLES = 1024
_DEFAULT_DIRECTORY_ITEM_LIMIT = 100_000


@dataclass
class GuangYaReadMetrics:
    """一次光鸭只读扫描的线程安全有界观测器。"""

    requests: int = 0
    pages: int = 0
    retries: int = 0
    rate_limit_retries: int = 0
    failures: int = 0
    _latencies_ms: list[float] = field(default_factory=list, repr=False)
    _latency_cursor: int = field(default=0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_request(self, elapsed_seconds: float, *, failed: bool = False) -> None:
        latency_ms = max(0.0, float(elapsed_seconds)) * 1000
        with self._lock:
            self.requests += 1
            self.failures += int(failed)
            if len(self._latencies_ms) < _READ_METRICS_MAX_LATENCY_SAMPLES:
                self._latencies_ms.append(latency_ms)
            else:
                # 固定容量环形样本保留最近请求，避免大库扫描观测数据自身无界增长。
                self._latencies_ms[self._latency_cursor] = latency_ms
                self._latency_cursor = (
                    self._latency_cursor + 1
                ) % _READ_METRICS_MAX_LATENCY_SAMPLES

    def record_page(self) -> None:
        with self._lock:
            self.pages += 1

    def record_retry(self, status_code: int) -> None:
        with self._lock:
            self.retries += 1
            self.rate_limit_retries += int(status_code == 429)

    @staticmethod
    def _percentile(ordered: list[float], percentile: float) -> float:
        if not ordered:
            return 0.0
        index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile)))
        return round(ordered[index], 1)

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            ordered = sorted(self._latencies_ms)
            return {
                "directory_requests": self.requests,
                "scan_pages": self.pages,
                "read_retries": self.retries,
                "rate_limit_retries": self.rate_limit_retries,
                "read_failures": self.failures,
                "latency_samples": len(ordered),
                "latency_sampled": int(self.requests > len(ordered)),
                "request_p50_ms": self._percentile(ordered, 0.50),
                "request_p95_ms": self._percentile(ordered, 0.95),
                "request_p99_ms": self._percentile(ordered, 0.99),
            }


def _log_token_loaded(fingerprint: str) -> None:
    """token 内容真正变化时才记 INFO，避免实时日志被同一凭据刷屏。"""
    global _LAST_LOGGED_TOKEN_FINGERPRINT
    current = str(fingerprint or "")
    if current and current == _LAST_LOGGED_TOKEN_FINGERPRINT:
        logger.debug("光鸭 token 已加载（内容未变化）")
        return
    _LAST_LOGGED_TOKEN_FINGERPRINT = current
    logger.info("光鸭 token 已加载")


def _canonical_token_path(path: Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _shared_token_lock(path: Path) -> threading.RLock:
    canonical = _canonical_token_path(path)
    with _TOKEN_LOCKS_GUARD:
        lock = _TOKEN_LOCKS.get(canonical)
        if lock is None:
            lock = threading.RLock()
            _TOKEN_LOCKS[canonical] = lock
        return lock


def _shared_token_process_lock(path: Path) -> CrossProcessLock:
    canonical = _canonical_token_path(path)
    with _TOKEN_LOCKS_GUARD:
        lock = _TOKEN_PROCESS_LOCKS.get(canonical)
        if lock is None:
            lock = CrossProcessLock("guangya-credentials", directory=canonical.parent)
            _TOKEN_PROCESS_LOCKS[canonical] = lock
        return lock


def _token_generation_file(path: Path) -> Path:
    canonical = _canonical_token_path(path)
    return canonical.with_name(f".{canonical.name}.generation")


def _token_file_fingerprint(path: Path) -> str:
    canonical = _canonical_token_path(path)
    try:
        payload = canonical.read_bytes()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise RuntimeError("无法读取光鸭凭据文件") from exc
    return hashlib.sha256(payload).hexdigest()


def _read_token_generation(path: Path) -> int:
    generation_file = _token_generation_file(path)
    try:
        raw = generation_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise RuntimeError("无法读取光鸭凭据状态") from exc
    try:
        generation = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("光鸭凭据状态已损坏") from exc
    if generation < 0:
        raise RuntimeError("光鸭凭据状态已损坏")
    return generation


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temp_file = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        if not protect_private_file(temp_file):
            raise PermissionError("无法收紧光鸭凭据状态文件权限")
        temp_file.replace(path)
    finally:
        try:
            temp_file.unlink()
        except FileNotFoundError:
            pass


def _token_generation(path: Path) -> int:
    return _read_token_generation(_canonical_token_path(path))


def _advance_token_generation(path: Path) -> int:
    """在调用方持有 token 跨进程锁时递增并持久化凭据世代。"""
    canonical = _canonical_token_path(path)
    generation = _read_token_generation(canonical) + 1
    _atomic_write_text(_token_generation_file(canonical), str(generation))
    return generation


@contextmanager
def _acquire_process_lock(lock: CrossProcessLock):
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


class _ValidationRefreshBlocked(RuntimeError):
    """只读校验期间 SDK 尝试刷新凭证。"""


OFFLINE_CREATE_TASK_URL = "https://api.guangyapan.com/nd.bizcloudcollection.s/v1/create_task"
OFFLINE_FILE_INDEX_KEYS = (
    "fileIndex", "file_index", "fileIdx", "file_idx", "selectIndex", "select_index",
)
OFFLINE_FILE_TREE_KEYS = {"subfiles", "subFiles", "files", "fileList", "file_list"}
OFFLINE_FILE_NAME_KEYS = (
    "name", "fileName", "filename", "file_name", "displayName", "title", "path",
)
OFFLINE_FILE_SIZE_KEYS = ("size", "fileSize", "file_size", "bytes", "length")
OFFLINE_EXCLUDED_INDEX_KEYS = (
    "excludeIndices", "exclude_indices", "excludedIndexes", "excluded_indexes",
)

# guangyaclient 懒加载（未安装时降级，避免阻断其他模块启动）
_RawClient = None


def _load_raw():
    global _RawClient
    if _RawClient is None:
        try:
            from guangyaclient import GuangyaClient as _C
            _RawClient = _C
        except ImportError as e:
            logger.error("未安装 guangyaclient，光鸭功能不可用 type=%s", type(e).__name__)
            raise
    return _RawClient


class GuangYaWriteRejected(RuntimeError):
    """Provider 以 HTTP 成功响应拒绝了云端写操作。"""

    def __init__(self, operation: str, *, code: str = "", message: str = "") -> None:
        self.operation = str(operation or "write")
        self.code = str(code or "").strip()[:40]
        self.public_message = str(message or "").strip()[:160]
        detail = f" code={self.code}" if self.code else ""
        super().__init__(f"光鸭写操作被拒绝 operation={self.operation}{detail}")


def _validate_write_response(response, *, operation: str) -> None:
    """识别光鸭在 HTTP 200 中返回的业务失败。

    公开 SDK 的部分版本在成功时返回 ``None``，部分版本返回
    ``{"msg": "success"}``。因此这里只拒绝能够确定为失败的结构，
    最终成功仍须由调用方读取远端快照验证。
    """
    if response is None or not isinstance(response, dict):
        return
    payloads = [response]
    data = response.get("data")
    if isinstance(data, dict):
        payloads.append(data)

    success_codes = {"0", "200", "success", "ok"}
    success_messages = {"success", "ok", "成功"}
    codes: list[str] = []
    messages: list[str] = []
    success_flags: list[bool] = []
    for payload in payloads:
        raw_code = payload.get("code")
        normalized_code = str(raw_code).strip() if raw_code not in (None, "") else ""
        if normalized_code:
            codes.append(normalized_code)
        raw_message = payload.get("msg") or payload.get("message")
        normalized_message = (
            str(raw_message).strip() if raw_message not in (None, "") else ""
        )
        if normalized_message:
            messages.append(normalized_message)
        if isinstance(payload.get("success"), bool):
            success_flags.append(bool(payload.get("success")))

    failure_code = next(
        (code for code in codes if code.casefold() not in success_codes), "",
    )
    failure_message = next(
        (message for message in messages if message.casefold() not in success_messages),
        messages[0] if messages else "",
    )
    if failure_code:
        raise GuangYaWriteRejected(
            operation, code=failure_code, message=failure_message,
        )
    if any(flag is False for flag in success_flags):
        raise GuangYaWriteRejected(
            operation, code=codes[0] if codes else "", message=failure_message,
        )
    has_explicit_success = bool(codes) or any(success_flags)
    if messages and not has_explicit_success and all(
        message.casefold() not in success_messages for message in messages
    ):
        raise GuangYaWriteRejected(
            operation, code="", message=failure_message,
        )


@dataclass
class GuangYaFile:
    file_id: str
    name: str
    is_dir: bool
    size: int = 0
    etag: str = ""
    parent_id: str = "0"
    created_at: int = 0
    updated_at: int = 0
    mime_type: str = ""
    extension: str = ""


def _to_int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _to_file(raw: dict, parent_id: str = "0") -> GuangYaFile:
    """把光鸭原始文件 dict 转成统一结构。容错取字段。"""
    # resType: 1=文件, 2=文件夹（guangya 约定）
    res_type = raw.get("resType") or raw.get("type")
    is_dir = res_type in (2, "2", "folder", "dir")
    if "isDir" in raw:
        is_dir = bool(raw["isDir"])
    return GuangYaFile(
        file_id=str(raw.get("fileId") or raw.get("resID") or raw.get("file_id") or raw.get("id") or ""),
        name=str(raw.get("fileName") or raw.get("resName") or raw.get("name") or ""),
        is_dir=is_dir,
        size=int(raw.get("size") or raw.get("fileSize") or 0),
        etag=str(raw.get("etag") or raw.get("gcid") or ""),
        parent_id=str(raw.get("parentId") or raw.get("parentID") or parent_id),
        created_at=max(0, _to_int(_first_value(
            raw, ("ctime", "createTime", "createdAt", "created_at")
        ))),
        updated_at=max(0, _to_int(_first_value(
            raw, ("utime", "updateTime", "updatedAt", "updated_at", "mtime")
        ))),
        mime_type=str(raw.get("mineType") or raw.get("mimeType") or raw.get("mime_type") or ""),
        extension=str(raw.get("ext") or raw.get("extension") or "").lstrip(".").lower(),
    )


def _detail_file_payload(response) -> dict | None:
    """从文件详情接口的直返或 data.fileInfo 包装中提取真实文件对象。"""
    if not isinstance(response, dict):
        return None
    candidates = [response]
    data = response.get("data")
    if isinstance(data, dict):
        candidates.extend((data.get("fileInfo"), data.get("file_info"), data))
    candidates.extend((response.get("fileInfo"), response.get("file_info")))
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        file_id = candidate.get("fileId") or candidate.get("resID") or candidate.get("file_id") or candidate.get("id")
        if str(file_id or "").strip():
            return candidate
    return None


def _first_value(raw: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return None


def _offline_file_index(raw: dict) -> int | None:
    value = _first_value(raw, OFFLINE_FILE_INDEX_KEYS)
    if isinstance(value, bool):
        return None
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    return index if index >= 0 else None


def _offline_file_name(raw: dict) -> str:
    value = _first_value(raw, OFFLINE_FILE_NAME_KEYS)
    return str(value or "").strip()


def _offline_file_size(raw: dict) -> int:
    return max(0, _to_int(_first_value(raw, OFFLINE_FILE_SIZE_KEYS)))


def _offline_file_excluded(raw: dict) -> bool:
    for key in ("excluded", "isExcluded", "is_excluded", "disabled"):
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    return False


def _offline_is_file(raw: dict, index: int | None = None) -> bool:
    item_type = str(
        raw.get("type") or raw.get("fileType") or raw.get("kind") or raw.get("resType") or ""
    ).strip().lower()
    if item_type in ("folder", "dir", "directory", "2"):
        return False
    if not _offline_file_name(raw):
        return False
    return index is not None or _offline_file_index(raw) is not None


def _bt_positional_file_indexes(parent: dict, key: str, items: list) -> dict[int, int]:
    """兼容光鸭 BT 解析响应省略 ``fileIndex: 0``。

    光鸭的 Go 响应会在首个文件索引为零时偶发省略该字段。只有能证明当前
    ``subfiles`` 是 BT 根清单、其余显式索引与数组位置完全一致，且唯一缺失项
    正好位于位置 0 时才补回索引；其他树形响应继续要求显式索引，避免猜测。
    """
    if str(key).lower() != "subfiles" or not items:
        return {}
    is_bt_manifest = any(
        marker in parent
        for marker in ("infoHash", "info_hash", "torrentHash", "torrent_hash", "subfilesNum")
    )
    if not is_bt_manifest:
        return {}
    explicit: list[tuple[int, int]] = []
    missing: list[int] = []
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            return {}
        index = _offline_file_index(item)
        if index is None:
            missing.append(position)
        else:
            explicit.append((position, index))
    if missing != [0] or any(position != index for position, index in explicit):
        return {}
    return {0: 0}


def _excluded_indexes(raw: dict) -> set[int]:
    result: set[int] = set()
    for key in OFFLINE_EXCLUDED_INDEX_KEYS:
        values = raw.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            try:
                result.add(int(value))
            except (TypeError, ValueError):
                continue
    return result


def _collect_offline_files(value, output: list[dict], inherited_excluded: set[int]) -> None:
    if not isinstance(value, dict):
        return
    local_excluded = inherited_excluded | _excluded_indexes(value)
    for key, child in value.items():
        if key in OFFLINE_FILE_TREE_KEYS and isinstance(child, list):
            positional_indexes = _bt_positional_file_indexes(value, key, child)
            for position, item in enumerate(child):
                if not isinstance(item, dict):
                    continue
                index = _offline_file_index(item)
                if index is None:
                    index = positional_indexes.get(position)
                if _offline_is_file(item, index):
                    output.append({
                        "index": index,
                        "name": _offline_file_name(item),
                        "size": _offline_file_size(item),
                        "excluded": index in local_excluded or _offline_file_excluded(item),
                    })
                _collect_offline_files(item, output, local_excluded)
        elif isinstance(child, dict):
            _collect_offline_files(child, output, local_excluded)


def _offline_task_ids(payload: dict) -> list[str]:
    """兼容不同光鸭响应形状提取离线任务 ID。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    values = [
        data.get("taskId"), data.get("task_id"), data.get("id"),
        payload.get("taskId"), payload.get("task_id"), payload.get("id"),
    ]
    task_ids: list[str] = []
    for value in values:
        if value in (None, ""):
            continue
        if isinstance(value, (list, tuple)):
            task_ids.extend(str(item) for item in value if item not in (None, ""))
        else:
            task_ids.append(str(value))
    return list(dict.fromkeys(task_ids))


def _offline_create_error(payload: dict) -> str:
    def error_text(value) -> str:
        if value is None or value is False or value == "":
            return ""
        if isinstance(value, (list, tuple)):
            return "; ".join(filter(None, (error_text(item) for item in value)))
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, default=str) if value else ""
        return str(value).strip()

    def failure_message(fallback: str) -> str:
        message = str(payload.get("message") or payload.get("msg") or "").strip()
        return message if message and message.lower() != "success" else fallback

    def normalized_flag(value) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            if value == 0:
                return False
            if value == 1:
                return True
            return None
        text = str(value).strip().lower()
        if text in {"0", "false", "failed", "failure", "fail", "error", "errored"}:
            return False
        if text in {"1", "true", "success", "succeeded", "ok", "completed", "complete"}:
            return True
        return None

    for key in ("error", "errors"):
        detail = error_text(payload.get(key))
        if detail:
            return detail

    explicit_success = False
    code = payload.get("code")
    if code is not None:
        try:
            if int(code) not in (0, 200):
                return failure_message(f"接口返回 code={code}")
            explicit_success = True
        except (TypeError, ValueError):
            return failure_message(f"接口返回 code={code}")
    for key in ("success", "state"):
        if key not in payload:
            continue
        flag = normalized_flag(payload[key])
        if flag is False:
            return failure_message("接口返回失败")
        if flag is True:
            explicit_success = True

    message = str(payload.get("message") or payload.get("msg") or "").strip()
    if message.lower() == "success":
        explicit_success = True

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    task_id = next((
        value
        for source in (data, payload)
        for key in ("taskId", "taskID", "task_id", "id")
        if (value := source.get(key)) is not None
        and not isinstance(value, bool)
        and str(value).strip().lower() not in {"", "0", "none", "null"}
    ), None)
    if task_id is not None:
        explicit_success = True

    if explicit_success:
        return ""
    return message or "接口返回结果不明确"


class GuangYaClient:
    """光鸭云盘客户端。"""

    display_name = "光鸭云盘"

    def __init__(self, token_file: Path = TOKEN_FILE):
        self.token_file = _canonical_token_path(Path(token_file))
        self._token_lock = _shared_token_lock(self.token_file)
        self._token_process_lock = _shared_token_process_lock(self.token_file)
        # 保留旧内部属性名，所有实例现在按真实 token 路径共享同一把锁。
        self._refresh_lock = self._token_lock
        self._request_context = threading.local()
        self._request_policy_lock = threading.RLock()
        self._read_metrics_lock = threading.Lock()
        self._read_metrics: GuangYaReadMetrics | None = None
        self._raw = None
        self._retired_raws: dict[int, object] = {}
        self._last_persisted_token = ""
        self._token_generation = 0
        self._credential_fingerprint = ""
        self._load_token()

    # ===== 底层 client 与 token 持久化 =====
    @staticmethod
    def _expiry_timestamp(value) -> Optional[float]:
        if value in (None, ""):
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            parsed = float(value)
            return parsed if math.isfinite(parsed) else None
        text = str(value).strip()
        try:
            parsed = float(text)
            return parsed if math.isfinite(parsed) else None
        except ValueError:
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None

    @staticmethod
    def _public_expiry_timestamp(value) -> Optional[float]:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        parsed = float(value)
        if not math.isfinite(parsed) or not TOKEN_EXPIRY_MIN <= parsed <= TOKEN_EXPIRY_MAX:
            return None
        return int(parsed) if parsed.is_integer() else parsed

    @staticmethod
    def _credential_snapshot(raw) -> tuple[str, str, Optional[float]]:
        return (
            str(getattr(raw, "token", "") or ""),
            str(getattr(raw, "refresh_token_value", "") or ""),
            GuangYaClient._expiry_timestamp(getattr(raw, "token_expires_at", None)),
        )

    @staticmethod
    def _set_authorization(raw, access_token: str) -> None:
        client = getattr(raw, "_client", None)
        headers = getattr(client, "headers", None)
        if headers is not None:
            headers["authorization"] = f"Bearer {access_token}"

    @classmethod
    def _restore_credential_snapshot(
        cls, raw, snapshot: tuple[str, str, Optional[float]]
    ) -> None:
        access, refresh, expires_at = snapshot
        raw.token = access
        raw.refresh_token_value = refresh
        raw.token_expires_at = expires_at
        cls._set_authorization(raw, access)

    @classmethod
    def _refresh_payload(cls, result) -> dict:
        if not isinstance(result, dict):
            return {}
        token_keys = {
            "access_token", "accessToken", "refresh_token", "refreshToken",
            "expires_at", "expiresAt", "expires_in", "expiresIn",
        }
        if token_keys.intersection(result) or isinstance(result.get("token"), str):
            return result
        for key in ("data", "result", "payload", "tokens"):
            nested = result.get(key)
            payload = cls._refresh_payload(nested)
            if payload:
                return payload
        return {}

    @classmethod
    def _apply_refresh_payload(cls, raw, result) -> dict:
        payload = cls._refresh_payload(result)
        access = payload.get("access_token") or payload.get("accessToken")
        if not access and isinstance(payload.get("token"), str):
            access = payload.get("token")
        refresh = payload.get("refresh_token") or payload.get("refreshToken")
        expires_at = payload.get("expires_at")
        if expires_at in (None, ""):
            expires_at = payload.get("expiresAt")
        parsed_expiry = cls._expiry_timestamp(expires_at)
        if parsed_expiry is None:
            expires_in = payload.get("expires_in")
            if expires_in in (None, ""):
                expires_in = payload.get("expiresIn")
            try:
                duration = float(expires_in)
            except (TypeError, ValueError):
                duration = 0
            if math.isfinite(duration) and duration > 0:
                parsed_expiry = time() + duration
        if access:
            raw.token = str(access)
            cls._set_authorization(raw, str(access))
        if refresh:
            raw.refresh_token_value = str(refresh)
        if parsed_expiry is not None:
            raw.token_expires_at = parsed_expiry
        return payload

    @classmethod
    def _refresh_succeeded(cls, before, after, result) -> bool:
        # 刷新的唯一成功凭据是 access token 真实轮换。refresh token 或
        # expires_at 单独变化无法证明旧 access token 已恢复可用，不能持久化，
        # 也不能向 API 虚报 valid=true。
        if not after[0] or after[0] == before[0]:
            return False
        payload = cls._refresh_payload(result)
        returned_access = payload.get("access_token") or payload.get("accessToken")
        if not returned_access and isinstance(payload.get("token"), str):
            returned_access = payload.get("token")
        # SDK 可原地更新后返回 None；一旦返回了 access token，则必须与
        # 实例最终状态一致，避免接受不一致的响应/内存组合。
        return not returned_access or str(returned_access) == after[0]

    def _load_token(self) -> None:
        # 与 refresh/login/clear 保持同一锁顺序，避免新实例清理掉另一
        # 进程正在原子替换的临时文件，也保证 generation 与 token 同快照读取。
        with self._token_lock:
            with _acquire_process_lock(self._token_process_lock):
                self._cleanup_token_temp_files()
                self._token_generation = _token_generation(self.token_file)
                if not self.token_file.exists():
                    self._credential_fingerprint = ""
                    return
                try:
                    if not protect_private_file(self.token_file):
                        raise PermissionError("光鸭 token 文件权限不安全")
                    self._credential_fingerprint = _token_file_fingerprint(self.token_file)
                    token_text = self.token_file.read_text(encoding="utf-8")
                    data = json.loads(token_text)
                    access = data.get("access_token", "")
                    refresh = data.get("refresh_token", "")
                    device_id = data.get("device_id", "")
                    expires_at = self._expiry_timestamp(data.get("expires_at"))
                    if access or refresh:
                        Raw = _load_raw()
                        self._raw = Raw(
                            access_token=access or None,
                            refresh_token=refresh or None,
                            device_id=device_id or None,
                        )
                        if expires_at:
                            self._raw.token_expires_at = expires_at
                        self._install_refresh_hook()
                        self._last_persisted_token = str(access or "")
                        _log_token_loaded(self._credential_fingerprint)
                except Exception as e:
                    logger.warning("加载光鸭 token 失败 type=%s", type(e).__name__)

    def _cleanup_token_temp_files(self) -> None:
        pattern = f".{self.token_file.name}.*.tmp"
        generation_file = _token_generation_file(self.token_file)
        generation_pattern = f".{generation_file.name}.*.tmp"
        candidates = {
            *self.token_file.parent.glob(pattern),
            *generation_file.parent.glob(generation_pattern),
        }
        for path in candidates:
            try:
                if path.is_file() and path.parent == self.token_file.parent:
                    path.unlink()
            except OSError:
                logger.warning("清理光鸭 token 临时文件失败: %s", path.name)

    def _credentials_current(self) -> bool:
        token_file = getattr(self, "token_file", None)
        if token_file is None:
            # 兼容只封装 SDK raw client、未经过 __init__ 的轻量实例；
            # 这类实例没有持久化 token 路径，也就不存在跨实例吊销范围。
            return True
        current_generation = _token_generation(token_file)
        instance_generation = getattr(self, "_token_generation", current_generation)
        current_fingerprint = _token_file_fingerprint(token_file)
        instance_fingerprint = getattr(
            self, "_credential_fingerprint", current_fingerprint
        )
        self._token_generation = instance_generation
        self._credential_fingerprint = instance_fingerprint
        return (
            instance_generation == current_generation
            and instance_fingerprint == current_fingerprint
        )

    def _close_or_retire_raw_locked(self, raw: object | None) -> bool:
        """关闭被替换的 SDK Client；失败时保留句柄供 close() 重试。"""
        if raw is None:
            return True
        retired = getattr(self, "_retired_raws", None)
        if retired is None:
            retired = self._retired_raws = {}
        identity = id(raw)
        if _close_raw_client(raw):
            retired.pop(identity, None)
            return True
        retired[identity] = raw
        return False

    def _discard_current_raw_locked(self) -> bool:
        raw = self._raw
        self._raw = None
        return self._close_or_retire_raw_locked(raw)

    def _retry_retired_raws_locked(self) -> bool:
        all_closed = True
        retired = getattr(self, "_retired_raws", None)
        if retired is None:
            retired = self._retired_raws = {}
        for identity, raw in list(retired.items()):
            if _close_raw_client(raw):
                retired.pop(identity, None)
            else:
                all_closed = False
        return all_closed

    def _invalidate_if_stale(self) -> bool:
        lock = getattr(self, "_token_lock", None)
        if lock is None:
            if self._credentials_current():
                return False
            self._discard_current_raw_locked()
            self._last_persisted_token = ""
            return True
        with lock:
            if self._credentials_current():
                return False
            self._discard_current_raw_locked()
            self._last_persisted_token = ""
            return True

    def _token_payload(self) -> dict:
        expires_at = getattr(self._raw, "token_expires_at", None)
        return {
            "access_token": getattr(self._raw, "token", "") or "",
            "refresh_token": getattr(self._raw, "refresh_token_value", "") or "",
            "device_id": getattr(self._raw, "device_id", "") or "",
            "expires_at": expires_at if expires_at is not None else "",
        }

    @staticmethod
    def _mask_token(value: str) -> str:
        token = str(value or "")
        if not token:
            return ""
        return "••••" if len(token) <= 4 else f"••••{token[-4:]}"

    def token_status(self, *, valid: Optional[bool] = None) -> dict:
        """返回可公开展示的凭证元数据，永不包含完整 token。"""
        self._invalidate_if_stale()
        raw = self._raw
        access = str(getattr(raw, "token", "") or "") if raw else ""
        refresh = str(getattr(raw, "refresh_token_value", "") or "") if raw else ""
        raw_expiry = getattr(raw, "token_expires_at", None) if raw else None
        expires_at = self._public_expiry_timestamp(raw_expiry)
        expiry_known = raw_expiry in (None, "") or expires_at is not None
        locally_valid = bool(access) and expiry_known and (expires_at is None or time() < expires_at)
        return {
            "has_access_token": bool(access),
            "has_refresh_token": bool(refresh),
            "expires_at": expires_at,
            "valid": locally_valid if valid is None else bool(valid),
            "access_token_masked": self._mask_token(access),
            "refresh_token_masked": self._mask_token(refresh),
        }

    def _write_token_locked(self) -> None:
        """在进程内锁和跨进程凭据锁均已持有时原子写入 token。"""
        if not self._raw:
            return
        if not self._credentials_current():
            self._discard_current_raw_locked()
            raise RuntimeError("光鸭登录凭证已撤销，请重新登录")
        data = self._token_payload()
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{self.token_file.name}.",
            suffix=".tmp",
            dir=self.token_file.parent,
        )
        temp_file = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, default=str, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            if not protect_private_file(temp_file):
                raise PermissionError("无法收紧光鸭 token 临时文件权限")
            temp_file.replace(self.token_file)
        finally:
            try:
                temp_file.unlink()
            except FileNotFoundError:
                pass
        self._last_persisted_token = str(data["access_token"] or "")
        self._credential_fingerprint = _token_file_fingerprint(self.token_file)
        logger.info("光鸭 token 已持久化")

    def _save_token(self) -> None:
        with self._token_lock:
            with _acquire_process_lock(self._token_process_lock):
                self._write_token_locked()

    def _advance_credentials_after_rotation(self) -> None:
        """在跨进程凭据锁内持久递增世代，使旧客户端立即失效。"""
        self._token_generation = _advance_token_generation(self.token_file)

    def _install_refresh_hook(self) -> None:
        """让 SDK 的到期刷新和 401 重试在成功后同步持久化。"""
        raw = self._raw
        if not raw or getattr(raw, "_mediaflux_refresh_hook", False):
            return
        original_refresh = raw.refresh_token

        def refresh_and_persist(refresh_token=None):
            with self._token_lock:
                with _acquire_process_lock(self._token_process_lock):
                    if getattr(raw, "_mediaflux_refresh_blocked", False):
                        raise _ValidationRefreshBlocked("SDK 在只读校验期间请求刷新")
                    # 即使 SDK 因 token 尚未到期而准备走快速返回，也必须先复核
                    # 持久化凭据世代与磁盘文件，阻止其他进程已撤销的实例复活。
                    if not self._credentials_current() or not self.token_file.exists():
                        raw._mediaflux_last_refresh_ok = False
                        raw._mediaflux_last_refresh_persisted = False
                        raise RuntimeError("光鸭登录凭证已变化，请重新加载")
                    current_expiry = self._expiry_timestamp(
                        getattr(raw, "token_expires_at", None)
                    )
                    if (
                        refresh_token is None
                        and not getattr(
                            getattr(self, "_request_context", None), "force_refresh", False
                        )
                        and current_expiry
                        and time() < current_expiry - 60
                    ):
                        raw._mediaflux_last_refresh_ok = True
                        raw._mediaflux_last_refresh_persisted = False
                        return {"access_token": getattr(raw, "token", "")}
                    before = self._credential_snapshot(raw)
                    raw._mediaflux_last_refresh_ok = False
                    raw._mediaflux_last_refresh_persisted = False
                    try:
                        result = original_refresh(refresh_token)
                        self._apply_refresh_payload(raw, result)
                        after = self._credential_snapshot(raw)
                        success = self._refresh_succeeded(before, after, result)
                        if not success:
                            self._restore_credential_snapshot(raw, before)
                            return result
                        # Access token 轮换不代表登录身份被替换。保持登录世代不变，
                        # 让同一客户端上的长任务可继续；其他已加载客户端仍会因
                        # token 文件指纹变化而立即失效。
                        self._write_token_locked()
                        raw._mediaflux_last_refresh_ok = True
                        raw._mediaflux_last_refresh_persisted = True
                        return result
                    except Exception:
                        self._restore_credential_snapshot(raw, before)
                        raise

        raw.refresh_token = refresh_and_persist
        raw._mediaflux_refresh_hook = True
        self._install_request_retry_policy(raw)

    def _install_request_retry_policy(self, raw) -> None:
        """禁止 SDK 在任意 HTTP 请求内部自动刷新并重放。

        光鸭 SDK 的业务接口当前统一使用 POST，不能按 HTTP method 判断
        是否幂等。明确的业务只读操作由 ``_call_read`` 在外层完成一次
        有界刷新/重试；移动、删除、创建等写操作始终只发送一次。
        """
        if getattr(raw, "_mediaflux_request_policy", False):
            return
        original_request = getattr(raw, "request", None)
        if not callable(original_request):
            return

        def request_with_policy(url, method="GET", **request_kwargs):
            # SDK 会在 401 后根据 refresh token 自动重放原请求。因为业务
            # 读写都可能是 POST，直接走 SDK 持有的 httpx.Client 发出一次
            # 请求，避免修改共享 refresh_token_value，也避免网络等待期间
            # 占用全局凭据锁。httpx.Client 支持跨线程复用连接池。
            transport = getattr(raw, "_client", None)
            transport_request = getattr(transport, "request", None)
            if callable(transport_request):
                headers = {
                    "traceparent": f"00-{token_hex(16)}-{token_hex(8)}-01",
                }
                supplied_headers = request_kwargs.pop("headers", None)
                if supplied_headers:
                    headers.update(supplied_headers)
                response = transport_request(
                    method, url, headers=headers, **request_kwargs
                )
                response.raise_for_status()
                return response

            # 测试替身或未来 SDK 没有暴露底层 transport 时，退回实例级锁
            # 保护的旧策略。这里只会串行同一个 raw 实例，不再阻塞共享同一
            # token 文件的其它真实请求。
            request_policy_lock = getattr(self, "_request_policy_lock", None)
            if request_policy_lock is None:
                request_policy_lock = threading.RLock()
                self._request_policy_lock = request_policy_lock
            with request_policy_lock:
                refresh_token = getattr(raw, "refresh_token_value", None)
                raw.refresh_token_value = None
                try:
                    return original_request(url, method, **request_kwargs)
                finally:
                    raw.refresh_token_value = refresh_token

        raw.request = request_with_policy
        raw._mediaflux_request_policy = True

    def begin_read_metrics(self) -> GuangYaReadMetrics:
        collector = GuangYaReadMetrics()
        with self._read_metrics_lock:
            self._read_metrics = collector
        return collector

    def end_read_metrics(self, collector: GuangYaReadMetrics) -> dict[str, int | float]:
        with self._read_metrics_lock:
            if self._read_metrics is collector:
                self._read_metrics = None
        return collector.snapshot()

    def _active_read_metrics(self) -> GuangYaReadMetrics | None:
        lock = getattr(self, "_read_metrics_lock", None)
        if lock is None:
            return None
        with lock:
            return getattr(self, "_read_metrics", None)

    def _refresh_after_unauthorized(self, observed_access_token: str) -> dict:
        """合并并发 401：已有线程完成刷新时直接复用新凭据。"""
        with self._token_lock:
            raw = getattr(self, "_raw", None)
            current_access = str(getattr(raw, "token", "") or "")
            if (
                observed_access_token
                and current_access
                and current_access != observed_access_token
            ):
                return self.token_status(valid=True)
            return self.refresh_now()

    @staticmethod
    def _exception_status_code(exc: Exception) -> int:
        response = getattr(exc, "response", None)
        try:
            return int(getattr(response, "status_code", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _read_retryable(cls, exc: Exception) -> bool:
        current: BaseException | None = exc
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            status_code = cls._exception_status_code(current)
            if status_code in {401, 408, 425, 429, 500, 502, 503, 504}:
                return True
            if isinstance(current, httpx.TransportError):
                return True
            name = type(current).__name__.lower()
            if any(token in name for token in ("timeout", "connect", "network", "transport")):
                return True
            current = current.__cause__ or current.__context__
        return False

    @staticmethod
    def _is_timeout_error(exc: BaseException) -> bool:
        current: BaseException | None = exc
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            if isinstance(current, (TimeoutError, httpx.TimeoutException)):
                return True
            current = current.__cause__ or current.__context__
        return False

    def _call_read(self, operation: str, callback, *, deadline: float | None = None):
        """只对明确的只读操作执行一次有界重试。

        ``deadline`` 仅用于可降级的媒体探测读取。它禁止 401 路径触发无法
        继承该截止时间的 token 刷新，并把瞬时错误退避限制在剩余预算内。
        """
        for attempt in range(2):
            if deadline is not None and monotonic() >= deadline:
                raise httpx.TimeoutException(f"{operation} exceeded its deadline")
            try:
                observed_access_token = str(
                    getattr(getattr(self, "_raw", None), "token", "") or ""
                )
                started = monotonic()
                try:
                    result = callback()
                except Exception:
                    metrics = self._active_read_metrics()
                    if metrics is not None:
                        metrics.record_request(monotonic() - started, failed=True)
                    raise
                metrics = self._active_read_metrics()
                if metrics is not None:
                    metrics.record_request(monotonic() - started)
                return result
            except Exception as exc:
                if attempt or not self._read_retryable(exc):
                    raise
                status_code = self._exception_status_code(exc)
                metrics = self._active_read_metrics()
                if metrics is not None:
                    metrics.record_retry(status_code)
                log_throttled(
                    logger,
                    logging.WARNING,
                    f"guangya-read-retry:{operation}:{status_code or 'network'}:{type(exc).__name__}",
                    "光鸭只读请求瞬时失败，准备重试 operation=%s status=%s type=%s",
                    operation,
                    status_code or "network",
                    type(exc).__name__,
                )
                if status_code == 401:
                    if deadline is not None:
                        # 媒体探测属于可降级读取；不要让无截止时间的凭据刷新
                        # 突破整个整理任务的墙钟预算。
                        raise
                    self._refresh_after_unauthorized(observed_access_token)
                else:
                    delay = 0.15
                    if deadline is not None:
                        delay = min(delay, max(0.0, deadline - monotonic()))
                        if delay <= 0:
                            raise httpx.TimeoutException(
                                f"{operation} exceeded its deadline"
                            ) from exc
                    sleep(delay)
        raise RuntimeError("光鸭只读请求重试状态异常")

    def _ensure_fresh_token(self) -> None:
        if self._invalidate_if_stale():
            raise RuntimeError("光鸭登录凭证已撤销，请重新登录")
        raw = self._raw
        if not raw:
            return
        expires_at = self._expiry_timestamp(getattr(raw, "token_expires_at", None))
        if expires_at and time() >= expires_at - 60:
            raw.refresh_token()
        elif str(getattr(raw, "token", "") or "") != self._last_persisted_token:
            self._save_token()

    @property
    def raw(self):
        if self._invalidate_if_stale():
            raise RuntimeError("光鸭登录凭证已撤销，请重新登录")
        if self._raw is None:
            raise RuntimeError("光鸭未登录，请先完成短信登录")
        self._ensure_fresh_token()
        return self._raw

    @property
    def logged_in(self) -> bool:
        self._invalidate_if_stale()
        return self._raw is not None

    @property
    def credential_generation(self) -> int:
        """返回跨进程持久凭据世代，用于绑定短期确认票据。"""
        self._invalidate_if_stale()
        token_file = getattr(self, "token_file", None)
        if token_file is None:
            return 0
        return _token_generation(token_file)

    # ===== 登录凭证 =====
    def login_init(self, phone: str) -> dict:
        """初始化短信登录，返回可能需要的 captcha 信息。"""
        Raw = _load_raw()
        tmp = Raw()
        try:
            res = tmp.login_sms_init(phone)
            return res if isinstance(res, dict) else {"raw": str(res)}
        finally:
            _close_raw_client(tmp)

    def send_sms(self, phone: str, captcha_token: str = "") -> dict:
        """发送短信验证码。需先 login_init 拿到 captcha_token。
        发送验证码是登录前操作，用未登录的新实例（self.raw 要求已登录）。"""
        Raw = _load_raw()
        tmp = Raw()
        try:
            res = tmp.login_sms_send(phone, captcha_token)
            return res if isinstance(res, dict) else {"raw": str(res)}
        finally:
            _close_raw_client(tmp)

    def login(self, phone: str, code: str, verification_id: str = "",
              captcha_token: str = "") -> bool:
        """验证码登录。需先 send_sms 拿到 verification_id。

        实测流程：verify(verification_id, code) → 拿 verification_token
        → signin(code, verification_token, "+86 "+phone, captcha_token)。
        signin 的 username 必须用 "+86 "+phone 格式，纯号码会 captcha_invalid。
        """
        Raw = _load_raw()
        tmp = Raw()
        installed = False
        previous = None
        try:
            username = phone if phone.startswith("+") else f"+86 {phone}"
            if verification_id:
                v = tmp.login_sms_verify(
                    verification_id=verification_id, verification_code=code
                )
                vtoken = ""
                if isinstance(v, dict):
                    vtoken = v.get("verification_token") or v.get("token") or ""
                if not vtoken:
                    raise RuntimeError(f"verify 未返回 verification_token: {v}")
                tmp.login_sms_signin(
                    verification_code=code,
                    verification_token=vtoken,
                    username=username,
                    captcha_token=captcha_token,
                )
            else:
                tmp.login_sms(username, get_code=lambda: code)
            with self._token_lock:
                with _acquire_process_lock(self._token_process_lock):
                    # 登录是一次凭据替换：先同步当前持久世代与指纹，再原子写入并递增。
                    self._token_generation = _token_generation(self.token_file)
                    self._credential_fingerprint = _token_file_fingerprint(self.token_file)
                    previous = self._raw
                    self._raw = tmp
                    installed = True
                    self._install_refresh_hook()
                    self._write_token_locked()
                    self._advance_credentials_after_rotation()
        finally:
            if installed:
                if previous is not None and previous is not tmp:
                    with self._token_lock:
                        self._close_or_retire_raw_locked(previous)
            else:
                _close_raw_client(tmp)
        logger.info("光鸭登录成功 phone=%s****%s", phone[:3], phone[-4:])
        return True

    def refresh_now(self) -> dict:
        """显式刷新凭证，兼容 SDK 原地更新、空返回和嵌套 token payload。"""
        with self._token_lock:
            # 必须在共享锁内复核凭据世代。否则另一个实例可能在本实例
            # 等锁期间清除/轮换 token，本实例随后又用旧 refresh token
            # 把已撤销凭据写回磁盘。
            token_exists = self.token_file.exists()
            ephemeral_credentials = (
                not token_exists
                and self._raw is not None
                and not self._last_persisted_token
                and int(getattr(self, "_token_generation", 0) or 0) == 0
                and not str(getattr(self, "_credential_fingerprint", "") or "")
            )
            if (not self._credentials_current() or not token_exists) and not ephemeral_credentials:
                self._discard_current_raw_locked()
                self._last_persisted_token = ""
                if not token_exists:
                    raise RuntimeError("光鸭登录凭证已撤销，请重新登录")
                self._token_generation = _token_generation(self.token_file)
                self._load_token()
                if self._raw is None:
                    raise RuntimeError("光鸭登录凭证已撤销，请重新登录")
                return self.token_status(valid=True)

            raw = self._raw
            if raw is None:
                raise RuntimeError("光鸭未登录，请先完成短信登录")

            # 多实例共享同一 token 文件。前一个实例可能已在等待锁期间
            # 完成 token 轮换；若磁盘 access token 已不同，直接加载最新
            # 凭证，禁止用旧 refresh token 再次刷新造成轮换竞态。
            try:
                disk = json.loads(self.token_file.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, ValueError, TypeError):
                disk = {}
            disk_access = str(disk.get("access_token") or "")
            memory_access = str(getattr(raw, "token", "") or "")
            if disk_access and disk_access != memory_access:
                self._load_token()
                return self.token_status(valid=True)

            refresh_token = str(getattr(raw, "refresh_token_value", "") or "")
            if not refresh_token:
                raise RuntimeError("当前没有可用的 refresh token")
            before = self._credential_snapshot(raw)
            raw._mediaflux_last_refresh_ok = False
            raw._mediaflux_last_refresh_persisted = False
            result = raw.refresh_token(refresh_token)
            after = self._credential_snapshot(raw)
            success = bool(getattr(raw, "_mediaflux_last_refresh_ok", False))
            if not success:
                self._apply_refresh_payload(raw, result)
                after = self._credential_snapshot(raw)
                success = self._refresh_succeeded(before, after, result)
            if not success:
                self._restore_credential_snapshot(raw, before)
                raise RuntimeError("光鸭刷新未返回新的 access token")
            if not getattr(raw, "_mediaflux_last_refresh_persisted", False):
                self._save_token()
            return self.token_status(valid=True)

    def refresh(self) -> bool:
        """兼容旧调用：显式刷新成功返回 True。"""
        try:
            self.refresh_now()
            return True
        except Exception as e:
            logger.error("光鸭 token 刷新失败 type=%s", type(e).__name__)
            return False

    def validate(self) -> bool:
        """执行只读校验；不主动刷新，也阻止 SDK 在到期或 401 时刷新。"""
        if self._invalidate_if_stale():
            return False
        raw = self._raw
        if raw is None:
            return False
        with self._token_lock:
            expires_at = self._expiry_timestamp(getattr(raw, "token_expires_at", None))
            if expires_at and time() >= expires_at - 60:
                return False
            previous_blocked = bool(getattr(raw, "_mediaflux_refresh_blocked", False))
            raw._mediaflux_refresh_blocked = True
            try:
                raw.fs_files(parent_id=None, page=0, page_size=1)
                return True
            except _ValidationRefreshBlocked:
                logger.warning("光鸭只读校验检测到 SDK 刷新请求，已阻止并判定无效")
                return False
            except Exception:
                return False
            finally:
                raw._mediaflux_refresh_blocked = previous_blocked

    def clear_tokens(self) -> dict:
        """在跨进程锁内撤销世代并清除内存与磁盘凭证。"""
        with self._token_lock:
            with _acquire_process_lock(self._token_process_lock):
                self._token_generation = _advance_token_generation(self.token_file)
                self._discard_current_raw_locked()
                self._cleanup_token_temp_files()
                try:
                    self.token_file.unlink()
                except FileNotFoundError:
                    pass
                self._last_persisted_token = ""
                self._credential_fingerprint = ""
                logger.info("光鸭登录已清除")
            return self.token_status(valid=False)

    # ===== 目录浏览 =====
    def iter_dir(
        self,
        parent_id: str = "0",
        *,
        should_stop: Callable[[], bool] | None = None,
        max_items: int | None = None,
    ) -> Iterator[GuangYaFile]:
        """逐页迭代目录，并由调用方预算限制条目数而非固定页数。"""
        page_size = 200
        normalized_parent = parent_id if parent_id != "0" else None
        seen_ids: set[str] = set()
        yielded = 0
        page = 0
        # 保留历史 500 页（约 10 万项）熔断作为所有普通目录读取的默认边界；
        # STRM 等已审计调用方可通过 max_items 传入更严格的本轮预算。
        item_limit = (
            _DEFAULT_DIRECTORY_ITEM_LIMIT
            if max_items is None else max(1, int(max_items))
        )
        while True:
            if should_stop and should_stop():
                return
            res = self._call_read(
                "list_dir",
                lambda: self.raw.fs_files(
                    parent_id=normalized_parent,
                    page=page,
                    page_size=page_size,
                ),
            )
            metrics = self._active_read_metrics()
            if metrics is not None:
                metrics.record_page()
            items = self._extract_list(res)
            new_count = 0
            for raw_item in items:
                item = _to_file(raw_item, parent_id)
                if item.file_id and item.file_id in seen_ids:
                    continue
                if item_limit is not None and yielded >= item_limit:
                    raise RuntimeError(
                        f"光鸭目录项目超过调用方安全上限 {item_limit}，已停止读取"
                    )
                if item.file_id:
                    seen_ids.add(item.file_id)
                yield item
                yielded += 1
                new_count += 1
            if len(items) < page_size:
                return
            if new_count == 0:
                raise RuntimeError("光鸭目录分页未推进，已停止读取以避免返回不完整目录")
            page += 1

    def list_dir(self, parent_id: str = "0") -> list[GuangYaFile]:
        """完整读取目录全部分页，适合需要完整快照的调用方。"""
        return list(self.iter_dir(parent_id))

    def close(self) -> bool:
        """幂等释放 SDK 底层 httpx 连接池。"""
        with self._token_lock:
            all_closed = self._retry_retired_raws_locked()
            raw = self._raw
            if raw is not None:
                if _close_raw_client(raw):
                    if self._raw is raw:
                        self._raw = None
                else:
                    all_closed = False
            return all_closed and not getattr(self, "_retired_raws", {})

    def create_dir(self, name: str, parent_id: str = "0") -> str:
        res = self.raw.fs_create_dir(
            dir_name=name,
            parent_id=None if parent_id == "0" else parent_id,
            fail_if_name_exist=True,
        )
        _validate_write_response(res, operation="create_dir")
        fid = ""
        if isinstance(res, dict):
            data = res.get("data") or {}
            if isinstance(data, dict):
                fid = str(data.get("fileId") or "")
            if not fid:
                fid = str(res.get("fileId") or res.get("resID") or res.get("file_id") or res.get("id") or "")
        return fid

    def file_info(self, file_id: str) -> Optional[GuangYaFile]:
        res = self._call_read("file_info", lambda: self.raw.fs_detail(file_id))
        payload = _detail_file_payload(res)
        if not payload:
            logger.debug("光鸭文件详情缺少有效 fileInfo file=%s", file_id)
            return None
        return _to_file(payload)

    def move(self, file_ids: list[str], parent_id: str) -> bool:
        response = self.raw.fs_move(file_ids, None if parent_id == "0" else parent_id)
        _validate_write_response(response, operation="move")
        return True

    def rename(self, file_id: str, new_name: str) -> bool:
        response = self.raw.fs_rename(file_id, new_name)
        _validate_write_response(response, operation="rename")
        return True

    def delete(self, file_ids: list[str]) -> bool:
        """删除明确指定的云端对象。上层必须先完成权限、快照和确认校验。"""
        ids = list(dict.fromkeys(str(item).strip() for item in file_ids if str(item).strip()))
        if not ids:
            return True
        response = self.raw.fs_delete(ids)
        _validate_write_response(response, operation="delete")
        return True

    @property
    def supports_atomic_empty_directory_delete(self) -> bool:
        """Provider 是否提供带版本前置条件的原子空目录删除。"""
        raw = self._raw
        return bool(raw and callable(getattr(raw, "fs_delete_empty", None)))

    @property
    def supports_guarded_empty_directory_delete(self) -> bool:
        """是否可在版本与双重空目录复核后安全移入光鸭回收站。

        新版 Provider 优先使用原子 ``fs_delete_empty``；旧版公开 SDK 仅有
        ``fs_delete`` 时，仍可在同一客户端锁内执行详情、空目录、详情、空目录
        四次复核后删除。该回退不会把来源根或非空目录交给宽泛删除接口。
        """
        raw = self._raw
        return bool(
            raw
            and (
                callable(getattr(raw, "fs_delete_empty", None))
                or callable(getattr(raw, "fs_delete", None))
            )
        )

    @staticmethod
    def _assert_empty_directory_snapshot(
        current: GuangYaFile | None,
        *,
        expected_etag: str,
        expected_updated_at: int,
    ) -> None:
        if current is None or not current.is_dir:
            raise RuntimeError("目录状态已变化，已保留")
        if expected_etag and str(current.etag or "") != expected_etag:
            raise RuntimeError("目录版本已变化，已保留")
        if expected_updated_at and int(current.updated_at or 0) != expected_updated_at:
            raise RuntimeError("目录更新时间已变化，已保留")

    def delete_empty_directory(
        self,
        file_id: str,
        *,
        expected_etag: str = "",
        expected_updated_at: int = 0,
    ) -> bool:
        """复核空目录身份后移入回收站，优先使用 Provider 原子接口。"""
        normalized_id = str(file_id or "").strip()
        expected_etag = str(expected_etag or "").strip()
        try:
            expected_updated_at = max(0, int(expected_updated_at or 0))
        except (TypeError, ValueError):
            expected_updated_at = 0
        if not normalized_id:
            raise RuntimeError("空目录标识无效")
        if not expected_etag and not expected_updated_at:
            raise RuntimeError("目录缺少可验证的版本信息，已保留")

        with self._token_lock:
            if self._invalidate_if_stale():
                raise RuntimeError("光鸭登录凭证已撤销，请重新登录")
            self._ensure_fresh_token()
            raw = self._raw
            if raw is None:
                raise RuntimeError("光鸭未登录，请先完成短信登录")
            conditional_delete = getattr(raw, "fs_delete_empty", None)
            fallback_delete = getattr(raw, "fs_delete", None)
            if not callable(conditional_delete) and not callable(fallback_delete):
                raise RuntimeError("当前光鸭 Provider 不支持空目录删除，已保留")

            current = self.file_info(normalized_id)
            self._assert_empty_directory_snapshot(
                current,
                expected_etag=expected_etag,
                expected_updated_at=expected_updated_at,
            )
            if self.list_dir(normalized_id):
                raise RuntimeError("目录已包含内容，已保留")

            if callable(conditional_delete):
                result = conditional_delete(
                    normalized_id,
                    expected_etag=expected_etag or None,
                    expected_updated_at=expected_updated_at or None,
                )
                failure_prefix = "光鸭原子空目录删除失败"
            else:
                # 旧版 SDK 无条件删除前再次复核版本与内容，尽量缩短竞态窗口。
                latest = self.file_info(normalized_id)
                self._assert_empty_directory_snapshot(
                    latest,
                    expected_etag=expected_etag,
                    expected_updated_at=expected_updated_at,
                )
                if self.list_dir(normalized_id):
                    raise RuntimeError("目录已包含内容，已保留")
                result = fallback_delete([normalized_id])
                failure_prefix = "光鸭空目录回收站删除失败"

            if result is False:
                raise RuntimeError(f"{failure_prefix}，目录已保留")
            if isinstance(result, dict):
                error = _offline_create_error(result)
                if error:
                    raise RuntimeError(f"{failure_prefix}：{error}")
        return True

    # ===== 链接转存 =====
    def inspect_share(self, share_url: str, page_size: int = 200) -> dict:
        """解析分享链接并返回可选择的顶层文件，不执行转存。"""
        inspected, access_token = self._read_share_files(
            share_url,
            page_size=page_size,
            max_pages=1,
        )
        return {**inspected, "access_token": access_token}

    def list_share_files(self, share_url: str, page_size: int = 100,
                         max_pages: int = 10) -> dict:
        """只读分页读取分享顶层文件，结果不包含任何访问令牌。"""
        inspected, _access_token = self._read_share_files(
            share_url,
            page_size=page_size,
            max_pages=max_pages,
        )
        return inspected

    def _read_share_files(self, share_url: str, page_size: int,
                          max_pages: int) -> tuple[dict, str]:
        """建立临时分享会话并读取文件；令牌仅返回给内部转存预览。"""
        share_id, code = self._parse_share(share_url)
        if not share_id:
            raise ValueError("无法识别光鸭分享 ID")
        page_size = max(1, min(int(page_size or 100), 200))
        max_pages = max(1, min(int(max_pages or 10), 50))
        Raw = _load_raw()
        token_resp = Raw.share_access_token(share_id, code)
        access_token = self._extract_share_token(token_resp)
        if not access_token:
            raise RuntimeError(self._extract_message(token_resp) or "分享链接无效或提取码错误")
        files: list[dict] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            response = Raw.share_files_list(
                access_token,
                page=page,
                page_size=page_size,
            )
            raw_files = self._extract_list(response)
            if not raw_files:
                break
            for raw in raw_files:
                item = self._to_share_file(raw)
                if item["id"] and item["id"] not in seen:
                    seen.add(item["id"])
                    files.append(item)
            if len(raw_files) < page_size:
                break
        return ({
            "share_id": share_id,
            "code_required": bool(code),
            "files": files,
            "count": len(files),
        }, access_token)

    def restore_share(self, access_token: str, file_ids: list[str],
                      target_dir_id: str = "0") -> dict:
        """把已解析分享中的指定文件转存到目标目录。"""
        selected = list(dict.fromkeys(str(item).strip() for item in file_ids if str(item).strip()))
        if not selected:
            return {"success": False, "error": "至少选择一个文件", "count": 0}
        result = self.raw.share_restore(
            access_token=access_token,
            file_ids=selected,
            parent_id=target_dir_id if target_dir_id != "0" else "",
        )
        if not isinstance(result, dict):
            return {
                "success": False,
                "retry_safe": False,
                "error": "光鸭转存结果不明确",
                "count": 0,
            }
        message = self._extract_message(result)
        code = result.get("code")
        success = (
            result.get("success") is True
            or str(result.get("msg", "")).lower() == "success"
            or code in (0, 200, "0", "200")
        )
        if success:
            logger.info(f"光鸭转存成功: {len(selected)} 项 → {target_dir_id}")
            return {"success": True, "retry_safe": False, "count": len(selected)}
        explicit_failure = result.get("success") is False or code is not None
        return {
            "success": False,
            "retry_safe": explicit_failure,
            "error": message or (
                "光鸭转存失败" if explicit_failure else "光鸭转存结果不明确"
            ),
            "count": 0,
        }

    def transfer_share(self, share_url: str, target_dir_id: str) -> dict:
        """兼容旧调用：解析分享并转存全部顶层文件。"""
        inspected = self.inspect_share(share_url)
        return self.restore_share(
            inspected["access_token"],
            [item["id"] for item in inspected["files"]],
            target_dir_id,
        )

    # ===== 秒传 JSON =====
    def generate_gcid_json(self, source_dir_id: str, source_name: str = "") -> dict:
        """递归导出带版本和完整性校验的 GCID 清单。"""
        from app.modules.gcid_manifest import export_manifest

        return export_manifest(self, source_dir_id, source_name=source_name)

    def import_gcid_json(self, gcid_data: dict, target_dir_id: str = "0") -> dict:
        """校验 GCID 清单并报告导入能力，不伪造云端写入。"""
        from app.modules.gcid_manifest import validate_manifest

        result = validate_manifest(gcid_data)
        return {
            **result,
            "target_dir_id": str(target_dir_id or "0"),
            "executed": False,
        }

    # ===== 离线下载 =====
    def add_offline_task(self, url: str, target_dir_id: str = "0",
                         task_type: str = "magnet") -> dict:
        response = self.raw.cloud_create_task(
            url=url,
            parent_id=None if target_dir_id == "0" else str(target_dir_id),
        )
        payload = response.json() if callable(getattr(response, "json", None)) else response
        payload = payload if isinstance(payload, dict) else {"raw": str(payload)}
        error = _offline_create_error(payload)
        task_ids = _offline_task_ids(payload)
        if error:
            return {
                "ok": False, "task_ids": task_ids, "batch_count": 0,
                "error": error, "response": payload,
            }
        logger.info("光鸭离线任务已创建 type=%s target=%s", task_type, target_dir_id)
        return {
            "ok": True, "task_ids": task_ids, "batch_count": 1,
            "error": "", "response": payload,
        }

    def add_offline_selection(self, url: str, target_dir_id: str,
                              file_indexes: list[int]) -> dict:
        """按光鸭接口上限分批创建带选集的离线任务。"""
        indexes: list[int] = []
        seen: set[int] = set()
        for value in file_indexes:
            if isinstance(value, bool):
                raise ValueError("文件索引必须为非负整数")
            try:
                index = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("文件索引必须为非负整数") from exc
            if index < 0:
                raise ValueError("文件索引必须为非负整数")
            if index not in seen:
                seen.add(index)
                indexes.append(index)
        if not indexes:
            raise ValueError("至少选择一个文件")

        task_ids: list[str] = []
        responses: list[dict] = []
        completed_indexes: list[int] = []
        parent_id = "" if str(target_dir_id or "0") == "0" else str(target_dir_id)
        batch_count = (len(indexes) + 499) // 500
        for offset in range(0, len(indexes), 500):
            batch_indexes = indexes[offset:offset + 500]
            batch_number = offset // 500 + 1
            payload: dict = {}
            outcome_unknown = False
            try:
                response = self.raw.request(
                    OFFLINE_CREATE_TASK_URL,
                    method="POST",
                    json={
                        "fileIndexes": batch_indexes,
                        "url": url,
                        "parentId": parent_id,
                    },
                )
                payload = response.json() if callable(getattr(response, "json", None)) else response
                payload = payload if isinstance(payload, dict) else {"raw": str(payload)}
                error = _offline_create_error(payload)
            except Exception as exc:
                error = str(exc) or exc.__class__.__name__
                outcome_unknown = True
            if error:
                return {
                    "ok": False,
                    "partial_success": bool(completed_indexes),
                    "outcome_unknown": outcome_unknown,
                    "completed_batches": batch_number - 1,
                    "task_ids": task_ids,
                    "completed_indexes": completed_indexes,
                    "remaining_indexes": indexes[offset:],
                    "failed_batch": batch_number,
                    "error": error,
                    "selected_count": len(indexes),
                    "batch_count": batch_count,
                    "responses": responses,
                }
            responses.append(payload)
            completed_indexes.extend(batch_indexes)
            task_ids.extend(_offline_task_ids(payload))
        tracking_complete = len(task_ids) == batch_count
        # 单批旧版接口可能只返回成功状态而不返回任务 ID，仍可由 URL/标题
        # 兼容跟踪；多批任务缺 ID 则无法证明全部批次完成，必须阻止自动整理。
        if not tracking_complete and batch_count > 1:
            logger.error(
                "光鸭选集任务已创建但任务 ID 不完整: files=%s batches=%s ids=%s",
                len(indexes), batch_count, len(task_ids),
            )
            return {
                "ok": False,
                "partial_success": True,
                "tracking_incomplete": True,
                "completed_batches": batch_count,
                "task_ids": task_ids,
                "completed_indexes": completed_indexes,
                "remaining_indexes": [],
                "failed_batch": None,
                "error": "光鸭任务已创建，但返回的任务 ID 不完整，已停止自动整理，请人工核验",
                "selected_count": len(indexes),
                "batch_count": batch_count,
                "responses": responses,
            }
        logger.info(f"光鸭选集任务已创建: {len(indexes)} 个文件 / {len(responses)} 批")
        return {
            "ok": True,
            "tracking_incomplete": False,
            "partial_success": False,
            "completed_batches": batch_count,
            "task_ids": task_ids,
            "completed_indexes": completed_indexes,
            "remaining_indexes": [],
            "failed_batch": None,
            "error": "",
            "selected_count": len(indexes),
            "batch_count": batch_count,
            "responses": responses,
        }

    def list_offline_tasks(self) -> list[dict]:
        """完整读取并归一化光鸭离线任务。

        光鸭 SDK 的默认状态集合不含 ``2``，且默认只返回第一页；MediaFlux
        显式读取 0-4 全状态并分页去重，避免已离线完成任务长期显示“下载中”。
        """
        page_size = 50
        max_pages = 200
        status_filter = [0, 1, 2, 3, 4]
        tasks: list[dict] = []
        seen_ids: set[str] = set()
        seen_pages: set[tuple[str, ...]] = set()
        for page in range(max_pages):
            res = self._call_read(
                "list_offline_tasks",
                lambda page=page: self.raw.cloud_task_list(
                    page=page, page_size=page_size, status=status_filter,
                ),
            )
            items = self._extract_list(res)
            page_keys: list[str] = []
            new_count = 0
            for position, item in enumerate(items):
                normalized = self._to_offline_task(item)
                identity = str(normalized.get("id") or "").strip()
                # 极少数上游响应缺 taskId；仍保留项目，但以页内稳定摘要防止
                # 同一错误页无限循环。
                key = identity or f"anonymous:{page}:{position}:{normalized.get('name', '')}"
                page_keys.append(identity or f"{position}:{normalized.get('name', '')}")
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                tasks.append(normalized)
                new_count += 1
            signature = tuple(page_keys)
            if len(items) < page_size:
                break
            if not items or signature in seen_pages or new_count == 0:
                logger.warning("光鸭离线任务分页未推进，已停止读取 page=%s", page)
                break
            seen_pages.add(signature)
        else:
            logger.warning("光鸭离线任务超过安全分页上限 pages=%s", max_pages)
        return tasks

    @staticmethod
    def _to_offline_task(raw: dict) -> dict:
        """兼容光鸭不同版本的任务字段命名。"""
        if not isinstance(raw, dict):
            return {"id": "", "name": str(raw), "raw": raw}
        task_id = raw.get("taskId") or raw.get("taskID") or raw.get("id") or raw.get("resId") or ""
        name = (raw.get("name") or raw.get("taskName") or raw.get("fileName")
                or raw.get("title") or raw.get("url") or "未命名任务")
        status = raw.get("status") if raw.get("status") is not None else raw.get("state")
        normalized_status = str(status).strip().lower()
        completed = status in {1, 2, 3} or normalized_status in {
            "1", "2", "3", "completed", "complete", "success", "succeeded", "finished", "done",
        }
        failed = status in {4, -1} or normalized_status in {
            "4", "-1", "failed", "error", "cancelled", "canceled", "invalid",
        }
        progress = raw.get("progress")
        if progress is None:
            progress = raw.get("process") if raw.get("process") is not None else raw.get("percent", 0)
        try:
            progress = float(progress or 0)
            if progress > 1:
                progress /= 100
        except (TypeError, ValueError):
            progress = 0.0
        size = raw.get("size") or raw.get("totalSize") or raw.get("fileSize") or 0
        downloaded = raw.get("downloaded") or raw.get("completedSize") or raw.get("doneSize") or 0
        speed = raw.get("speed") or raw.get("downloadSpeed") or raw.get("dlspeed") or 0
        size = _to_int(size)
        downloaded = _to_int(downloaded)
        if completed:
            progress = 1.0
            if not downloaded:
                downloaded = size
        return {
            "id": str(task_id),
            "name": str(name),
            "status": status if status is not None else "unknown",
            "status_label": "已完成" if completed else "失败" if failed else "等待中" if normalized_status == "0" else "下载中",
            "status_kind": "done" if completed else "failed" if failed else "running",
            "progress": max(0.0, min(progress, 1.0)),
            "size": size,
            "downloaded": downloaded,
            "speed": _to_int(speed),
            "target_dir": str(raw.get("parentId") or raw.get("parentID") or raw.get("savePath") or ""),
            "created_at": str(raw.get("createTime") or raw.get("createdAt") or raw.get("time") or ""),
            "raw": raw,
        }

    def resolve_url(self, url: str) -> dict:
        """解析 HTTP/磁力/ed2k 链接元数据，不创建离线任务。"""
        res = self._call_read(
            "resolve_url", lambda: self.raw.cloud_resolve_url(url)
        )
        return res if isinstance(res, dict) else {"raw": str(res)}

    def resolve_torrent(self, torrent_data: bytes) -> dict:
        """上传并解析 BT 种子元数据，不创建离线任务。"""
        if not isinstance(torrent_data, bytes) or not torrent_data:
            raise ValueError("种子文件为空")
        res = self._call_read(
            "resolve_torrent",
            lambda: self.raw.cloud_resolve_torrent(torrent_data),
        )
        return res if isinstance(res, dict) else {"raw": str(res)}

    @staticmethod
    def normalize_offline_files(response: dict) -> list[dict]:
        """把不同 resolve_res 响应归一化为 index/name/size/excluded。"""
        output: list[dict] = []
        _collect_offline_files(response, output, set())
        result: list[dict] = []
        seen: set[int] = set()
        for item in output:
            index = item["index"]
            if index in seen:
                raise ValueError(f"解析结果包含重复文件索引: {index}")
            seen.add(index)
            result.append(item)
        return result

    # ===== 直链（STRM 302 反代用）=====
    def get_download_url(
        self,
        file_id: str,
        *,
        timeout: float | None = None,
        raise_timeout: bool = False,
    ) -> Optional[str]:
        # 实测返回 {"msg":"success","data":{"signedURL":"https://...","urlDuration":21600,...}}
        deadline = None
        if timeout is not None:
            deadline = monotonic() + max(0.001, float(timeout))

        def fetch():
            if deadline is None:
                return self.raw.download_url(file_id)
            if self._invalidate_if_stale():
                raise RuntimeError("光鸭登录凭证已撤销，请重新登录")
            raw = self._raw
            if raw is None:
                raise RuntimeError("光鸭未登录，请先完成短信登录")
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise httpx.TimeoutException("get_download_url exceeded its deadline")
            return raw.request(
                "https://api.guangyapan.com/nd.bizuserres.s/v1/get_res_download_url",
                method="POST",
                json={"fileId": file_id},
                timeout=max(0.001, remaining),
            ).json()

        try:
            res = self._call_read(
                "get_download_url", fetch, deadline=deadline
            )
            if isinstance(res, dict):
                data = res.get("data") or {}
                if isinstance(data, dict):
                    return data.get("signedURL") or data.get("download_url") or data.get("url") or ""
                return res.get("signedURL") or res.get("download_url") or res.get("url") or ""
            return str(res) if res else None
        except Exception as exc:
            log_throttled(
                logger,
                logging.ERROR,
                f"guangya-download-url:{type(exc).__name__}",
                "光鸭获取直链失败 type=%s",
                type(exc).__name__,
            )
            if raise_timeout and self._is_timeout_error(exc):
                raise TimeoutError("光鸭获取媒体直链超时") from exc
            return None

    # ===== 工具 =====
    @staticmethod
    def _extract_share_token(response) -> str:
        if isinstance(response, str):
            return response
        if not isinstance(response, dict):
            return ""
        data = response.get("data")
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            return str(data.get("accessToken") or data.get("access_token") or data.get("token") or "")
        return str(response.get("accessToken") or response.get("access_token") or response.get("token") or "")

    @staticmethod
    def _extract_message(response) -> str:
        if not isinstance(response, dict):
            return ""
        data = response.get("data")
        detail = data.get("message") if isinstance(data, dict) else ""
        return str(response.get("message") or response.get("msg") or response.get("error") or detail or "")

    @staticmethod
    def _to_share_file(raw: dict) -> dict:
        if not isinstance(raw, dict):
            return {"id": "", "name": str(raw), "is_dir": False, "size": 0}
        file_id = raw.get("fileId") or raw.get("resID") or raw.get("file_id") or raw.get("id") or ""
        name = raw.get("fileName") or raw.get("resName") or raw.get("name") or "未命名"
        resource_type = raw.get("resType") if raw.get("resType") is not None else raw.get("fileType")
        is_dir = resource_type in (2, "2", "folder", "dir") or bool(raw.get("isDir"))
        size = raw.get("fileSize") or raw.get("size") or raw.get("resSize") or 0
        return {"id": str(file_id), "name": str(name), "is_dir": is_dir, "size": _to_int(size)}

    @staticmethod
    def _extract_list(res) -> list[dict]:
        """从光鸭返回中稳健提取文件列表。"""
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            for key in ("file_list", "fileList", "files", "data", "list", "res_list"):
                v = res.get(key)
                if isinstance(v, list):
                    return v
                if isinstance(v, dict):
                    inner = v.get("file_list") or v.get("list") or v.get("files")
                    if isinstance(inner, list):
                        return inner
        return []

    @staticmethod
    def _parse_share(share_url: str) -> tuple[str, str]:
        """从分享链接解析 share_id 与提取码。"""
        parsed = urlparse(share_url)
        qs = parse_qs(parsed.query)
        code = qs.get("code", [""])[0] or qs.get("pwd", [""])[0] or qs.get("p", [""])[0]
        # 路径中取 share_id（如 /s/xxxxx 或 /share/xxxxx）
        path = unquote(parsed.path)
        m = re.search(
            r"/(?:s|share)/([A-Za-z0-9][A-Za-z0-9_-]*)(?:/|$)",
            path,
            re.IGNORECASE,
        )
        share_id = m.group(1) if m else path.strip("/").split("/")[-1]
        return share_id, code
