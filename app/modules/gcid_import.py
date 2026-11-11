"""GCID v2 导入预览、私有 importer 注入边界与任务编排。"""
from __future__ import annotations

import json
import re
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from time import time
from typing import Any, Callable, Mapping, Protocol

from app import database as db
from app import notifier
from app.modules.gcid_manifest import GCIDManifest, GCIDManifestFile, normalize_manifest_v2
from app.logger import get_logger

logger = get_logger(__name__)


class PreviewBindingError(ValueError):
    """预览不存在、过期或与当前用户/目标不一致。"""


class ImportCapabilityUnavailable(RuntimeError):
    """生产环境没有已验证的光鸭私有 GCID 导入实现。"""


class ImportTaskNotFound(ValueError):
    """GCID 导入任务不存在。"""


class OperationTokenConflict(ValueError):
    """operation token 已绑定不同操作。"""


class PrivateGCIDImporter(Protocol):
    """Task 7 获得真实样本后实现的最小私有导入协议。"""

    available: bool
    unavailable_reason: str

    def import_file(
        self, *, target_dir_id: str, path: str, size: int, gcid: str
    ) -> object:
        """导入一个清单文件；不得返回值之外泄露私有响应。"""


@dataclass(frozen=True, slots=True)
class GCIDImportItemResult:
    success: bool
    remote_file_id: str = ""
    error_code: str = ""


@dataclass(frozen=True, slots=True)
class GCIDImportPreview:
    preview_id: str
    owner_id: str
    target_dir_id: str
    manifest_digest: str
    manifest: GCIDManifest
    created_at: float
    expires_at: float


class GCIDImportPreviewStore:
    """进程内有界预览快照；不保存用户上传的原始 JSON。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = 1800,
        max_entries: int = 128,
        clock: Callable[[], float] = time,
    ) -> None:
        self.ttl_seconds = max(60, min(ttl_seconds, 7200))
        self.max_entries = max(1, min(max_entries, 1024))
        self._clock = clock
        self._lock = threading.RLock()
        self._items: OrderedDict[str, GCIDImportPreview] = OrderedDict()

    def _purge_expired(self, now: float) -> None:
        expired = [key for key, value in self._items.items() if value.expires_at <= now]
        for key in expired:
            self._items.pop(key, None)

    def create(
        self,
        manifest: GCIDManifest | dict,
        *,
        target_dir_id: str,
        owner_id: str = "",
    ) -> str:
        normalized = manifest if isinstance(manifest, GCIDManifest) else normalize_manifest_v2(manifest)
        target = (target_dir_id or "").strip()
        owner = (owner_id or "").strip()
        if not target:
            raise PreviewBindingError("缺少目标目录")
        now = self._clock()
        preview_id = secrets.token_urlsafe(18)
        snapshot = GCIDImportPreview(
            preview_id=preview_id,
            owner_id=owner,
            target_dir_id=target,
            manifest_digest=normalized.digest,
            manifest=normalized,
            created_at=now,
            expires_at=now + self.ttl_seconds,
        )
        with self._lock:
            self._purge_expired(now)
            self._items[preview_id] = snapshot
            self._items.move_to_end(preview_id)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)
        return preview_id

    def consume(
        self,
        preview_id: str,
        *,
        target_dir_id: str,
        owner_id: str = "",
        manifest_digest: str = "",
    ) -> GCIDImportPreview:
        key = (preview_id or "").strip()
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            snapshot = self._items.get(key)
            if snapshot is None:
                raise PreviewBindingError("预览不存在或已过期")
            if snapshot.target_dir_id != (target_dir_id or "").strip():
                raise PreviewBindingError("目标目录已变化，请重新预览")
            if snapshot.owner_id != (owner_id or "").strip():
                raise PreviewBindingError("预览不属于当前用户")
            digest = (manifest_digest or "").strip().lower()
            if digest and digest != snapshot.manifest_digest:
                raise PreviewBindingError("清单摘要已变化，请重新预览")
            self._items.move_to_end(key)
            return snapshot


_preview_store = GCIDImportPreviewStore()
_operation_lock = threading.RLock()
_RETRY_REPLAY_KEY = "gcid_import.retry_replays:v1"
_RETRY_REPLAY_LIMIT = 512
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
_GENERIC_ITEM_ERROR = "私有 GCID 导入失败"
_CAPABILITY_REASON = "光鸭私有 GCID 导入能力不可用：尚未配置经真实请求样本验证的 importer"


def reset_runtime_state() -> None:
    """清空进程内预览；重试凭据随数据库持久保存，不随运行时重建丢失。"""
    global _preview_store
    with _operation_lock:
        _preview_store = GCIDImportPreviewStore()


def get_private_importer() -> PrivateGCIDImporter | None:
    """生产默认 fail closed；Task 7 只能在拿到真实样本后注入实现。"""
    return None


def importer_capability(importer: PrivateGCIDImporter | None = None) -> dict[str, Any]:
    candidate = get_private_importer() if importer is None else importer
    available = bool(
        candidate is not None
        and getattr(candidate, "available", True)
        and callable(getattr(candidate, "import_file", None))
    )
    reason = ""
    if not available:
        reason = str(getattr(candidate, "unavailable_reason", "") or _CAPABILITY_REASON)
    return {"available": available, "reason": reason}


def require_private_importer() -> PrivateGCIDImporter:
    importer = get_private_importer()
    capability = importer_capability(importer)
    if not capability["available"] or importer is None:
        raise ImportCapabilityUnavailable(capability["reason"])
    return importer


def _safe_remote_file_id(value: object) -> str:
    return _CONTROL_CHARS_RE.sub("", str(value or "").strip())[:256]


def _result_from_private(value: object) -> GCIDImportItemResult:
    if isinstance(value, GCIDImportItemResult):
        return value
    if isinstance(value, Mapping):
        return GCIDImportItemResult(
            success=value.get("success") is True,
            remote_file_id=_safe_remote_file_id(value.get("remote_file_id")),
            error_code=str(value.get("error_code") or "")[:80],
        )
    return GCIDImportItemResult(
        success=getattr(value, "success", False) is True,
        remote_file_id=_safe_remote_file_id(getattr(value, "remote_file_id", "")),
        error_code=str(getattr(value, "error_code", "") or "")[:80],
    )


def _tree_for_files(files: tuple[GCIDManifestFile, ...]) -> list[dict[str, Any]]:
    root: dict[str, Any] = {"children": {}}
    for item in files:
        parts = item.path.split("/")
        cursor = root
        for part in parts[:-1]:
            children = cursor["children"]
            cursor = children.setdefault(part, {
                "name": part,
                "type": "directory",
                "file_count": 0,
                "total_size": 0,
                "children": {},
            })
            cursor["file_count"] += 1
            cursor["total_size"] += item.size
        cursor["children"][parts[-1]] = {
            "name": parts[-1],
            "type": "file",
            "size": item.size,
        }

    def finish(node: dict[str, Any]) -> dict[str, Any]:
        if node["type"] == "file":
            return node
        children = [finish(child) for child in node.pop("children").values()]
        children.sort(key=lambda child: (child["type"] == "file", child["name"].casefold()))
        node["children"] = children
        return node

    return [finish(child) for child in root["children"].values()]


def create_preview(manifest: dict, *, target_dir_id: str, owner_id: str) -> dict[str, Any]:
    normalized = normalize_manifest_v2(manifest)
    preview_id = _preview_store.create(
        normalized, target_dir_id=target_dir_id, owner_id=owner_id
    )
    snapshot = _preview_store.consume(
        preview_id,
        target_dir_id=target_dir_id,
        owner_id=owner_id,
        manifest_digest=normalized.digest,
    )
    return {
        "preview_id": preview_id,
        "manifest_digest": normalized.digest,
        "target_dir_id": snapshot.target_dir_id,
        "file_count": normalized.file_count,
        "total_size": normalized.total_size,
        "tree": _tree_for_files(normalized.files),
        "expires_at": snapshot.expires_at,
    }


def _task_by_operation_token(operation_token: str):
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM gcid_import_tasks WHERE operation_token=?",
            (operation_token,),
        ).fetchone()


def _read_retry_replays(conn) -> OrderedDict[tuple[int, str], None]:
    row = conn.execute(
        "SELECT value FROM settings_kv WHERE key=?", (_RETRY_REPLAY_KEY,)
    ).fetchone()
    if row is None:
        return OrderedDict()
    try:
        values = json.loads(row["value"])
        if not isinstance(values, list) or len(values) > _RETRY_REPLAY_LIMIT:
            raise ValueError("invalid replay list")
        if any(
            not isinstance(item, list)
            or len(item) != 2
            or type(item[0]) is not int
            or item[0] <= 0
            or not isinstance(item[1], str)
            or not item[1]
            or len(item[1]) > 256
            for item in values
        ):
            raise ValueError("invalid replay entry")
        return OrderedDict(((item[0], item[1]), None) for item in values)
    except (TypeError, ValueError) as exc:
        raise ValueError("GCID 重试凭据损坏，已拒绝覆盖，请先恢复历史数据") from exc


def _write_retry_replays(conn, replays: OrderedDict[tuple[int, str], None]) -> None:
    while len(replays) > _RETRY_REPLAY_LIMIT:
        replays.popitem(last=False)
    conn.execute(
        "INSERT INTO settings_kv(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (
            _RETRY_REPLAY_KEY,
            json.dumps(list(replays), ensure_ascii=False, separators=(",", ":")),
            db.now(),
        ),
    )


def _retry_was_recorded(task_id: int, token: str) -> bool:
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        replays = _read_retry_replays(conn)
        key = (task_id, token)
        if key not in replays:
            return False
        replays.move_to_end(key)
        _write_retry_replays(conn, replays)
        return True


def _claim_task_for_run(task_id: int, *, retry_token: str = "") -> bool:
    """初次执行与重试共享任务 CAS；重试凭据必须先于私有写入持久化。"""
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        replays = _read_retry_replays(conn) if retry_token else OrderedDict()
        key = (task_id, retry_token)
        if retry_token and key in replays:
            return False
        condition = (
            "status IN ('failed','partial_success') AND EXISTS "
            "(SELECT 1 FROM gcid_import_items WHERE task_id=gcid_import_tasks.id AND status='failed')"
            if retry_token else "status='previewed'"
        )
        cursor = conn.execute(
            "UPDATE gcid_import_tasks SET status='running',updated_at=? "
            f"WHERE id=? AND {condition}",
            (db.now(), task_id),
        )
        if cursor.rowcount != 1:
            return False
        if retry_token:
            replays[key] = None
            _write_retry_replays(conn, replays)
        return True


def _update_item(
    item_id: int, *, status: str, remote_file_id: str = "", error: str = ""
) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE gcid_import_items SET status=?,remote_file_id=?,error=?,updated_at=? "
            "WHERE id=?",
            (status, remote_file_id, error[:1000], db.now(), item_id),
        )


def _set_items_running(item_ids: list[int]) -> None:
    if not item_ids:
        return
    placeholders = ",".join("?" for _ in item_ids)
    with db.get_conn() as conn:
        conn.execute(
            f"UPDATE gcid_import_items SET status='running',error='',updated_at=? "
            f"WHERE id IN ({placeholders})",
            (db.now(), *item_ids),
        )


def _failed_samples(task_id: int, limit: int = 3) -> list[dict[str, Any]]:
    return [
        {"id": int(row["id"]), "path": row["path"], "error": row["error"]}
        for row in db.list_gcid_import_items(task_id, "failed", limit=max(0, limit))
    ]


def serialize_task(row) -> dict[str, Any]:
    task_id = int(row["id"])
    return {
        "id": task_id,
        "manifest_digest": row["manifest_digest"],
        "target_dir_id": row["target_dir_id"],
        "status": row["status"],
        "file_count": int(row["file_count"] or 0),
        "total_size": int(row["total_size"] or 0),
        "success_count": int(row["success_count"] or 0),
        "failed_count": int(row["failed_count"] or 0),
        "error": row["error"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "can_retry": (
            int(row["failed_count"] or 0) > 0
            and row["status"] in {"failed", "partial_success"}
        ),
        "failed_samples": _failed_samples(task_id),
    }


def list_tasks(limit: int = 30) -> list[dict[str, Any]]:
    return [serialize_task(row) for row in db.list_gcid_import_tasks(limit)]


def _finish_task(task_id: int) -> dict[str, Any]:
    with db.get_conn() as conn:
        counts = conn.execute(
            "SELECT COUNT(*) AS total,SUM(status='success') AS succeeded,"
            "SUM(status='failed') AS failed FROM gcid_import_items WHERE task_id=?",
            (task_id,),
        ).fetchone()
    success_count = int(counts["succeeded"] or 0)
    failed_count = int(counts["failed"] or 0)
    if counts["total"] and success_count == counts["total"]:
        status = "success"
        error = ""
    elif success_count:
        status = "partial_success"
        error = f"{failed_count} 个文件导入失败"
    else:
        status = "failed"
        error = "GCID 导入失败"
    db.update_gcid_import_task(
        task_id,
        status=status,
        success_count=success_count,
        failed_count=failed_count,
        error=error,
    )
    row = db.get_gcid_import_task(task_id)
    if row is None:
        raise ImportTaskNotFound("GCID 导入任务不存在")
    return serialize_task(row)


def _execute_items(
    task_id: int,
    items: list,
    *,
    importer: PrivateGCIDImporter,
    target_dir_id: str,
    retry: bool,
) -> dict[str, Any]:
    _set_items_running([int(row["id"]) for row in items])
    db.update_gcid_import_task(task_id, status="running", error="")
    notifier.notify_gcid_import_started(
        task_id=task_id,
        file_count=len(items),
        total_size=sum(int(row["size"] or 0) for row in items),
        retry=retry,
    )
    for row in items:
        try:
            outcome = _result_from_private(importer.import_file(
                target_dir_id=target_dir_id,
                path=row["path"],
                size=int(row["size"] or 0),
                gcid=row["gcid"],
            ))
        except Exception as exc:
            logger.warning("GCID 导入单项失败 type=%s", type(exc).__name__)
            outcome = GCIDImportItemResult(False)
        if outcome.success:
            _update_item(
                int(row["id"]),
                status="success",
                remote_file_id=_safe_remote_file_id(outcome.remote_file_id),
            )
        else:
            _update_item(
                int(row["id"]), status="failed", error=_GENERIC_ITEM_ERROR
            )
    task = _finish_task(task_id)
    notifier.notify_gcid_import_finished(
        task_id=task_id,
        status=task["status"],
        success_count=task["success_count"],
        failed_count=task["failed_count"],
        failed_samples=task["failed_samples"],
        retry=retry,
    )
    return task


def _validate_operation_token(value: str) -> str:
    token = (value or "").strip()
    if not token or len(token) > 256:
        raise ValueError("operation_token 无效")
    return token


def run_preview(
    *,
    preview_id: str,
    target_dir_id: str,
    owner_id: str,
    manifest_digest: str,
    operation_token: str,
) -> tuple[dict[str, Any], bool]:
    token = _validate_operation_token(operation_token)
    digest = (manifest_digest or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PreviewBindingError("缺少有效的清单摘要，请重新预览")
    snapshot = _preview_store.consume(
        preview_id,
        target_dir_id=target_dir_id,
        owner_id=owner_id,
        manifest_digest=digest,
    )
    with _operation_lock:
        existing = _task_by_operation_token(token)
        if existing is not None:
            if (
                existing["manifest_digest"] != snapshot.manifest_digest
                or existing["target_dir_id"] != snapshot.target_dir_id
                or int(existing["file_count"] or 0) != snapshot.manifest.file_count
                or int(existing["total_size"] or 0) != snapshot.manifest.total_size
            ):
                raise OperationTokenConflict("operation_token 已绑定其他 GCID 导入任务")
            return serialize_task(existing), True

        importer = require_private_importer()
        task_id = db.create_gcid_import_task(
            operation_token=token,
            manifest_digest=snapshot.manifest_digest,
            target_dir_id=snapshot.target_dir_id,
            file_count=snapshot.manifest.file_count,
            total_size=snapshot.manifest.total_size,
        )
        if not _claim_task_for_run(task_id):
            row = db.get_gcid_import_task(task_id)
            if row is None:
                raise ImportTaskNotFound("GCID 导入任务不存在")
            return serialize_task(row), True
        db.replace_gcid_import_items(task_id, [
            {
                "path": item.path,
                "size": item.size,
                "gcid": item.gcid,
                "status": "previewed",
            }
            for item in snapshot.manifest.files
        ])
        task = _execute_items(
            task_id,
            db.list_gcid_import_items(task_id),
            importer=importer,
            target_dir_id=snapshot.target_dir_id,
            retry=False,
        )
        return task, False


def retry_task(
    task_id: int, *, operation_token: str
) -> tuple[dict[str, Any], bool]:
    token = _validate_operation_token(operation_token)
    normalized_task_id = task_id
    with _operation_lock:
        row = db.get_gcid_import_task(normalized_task_id)
        if row is None:
            raise ImportTaskNotFound("GCID 导入任务不存在")
        if _retry_was_recorded(normalized_task_id, token) or row["status"] == "running":
            current = db.get_gcid_import_task(normalized_task_id)
            if current is None:
                raise ImportTaskNotFound("GCID 导入任务不存在")
            return serialize_task(current), True
        if not db.list_gcid_import_items(normalized_task_id, "failed", limit=1):
            raise ValueError("任务没有可重试的失败项")
        importer = require_private_importer()
        if not _claim_task_for_run(normalized_task_id, retry_token=token):
            current = db.get_gcid_import_task(normalized_task_id)
            if current is None:
                raise ImportTaskNotFound("GCID 导入任务不存在")
            return serialize_task(current), True
        # 领取后重读，不能使用另一个执行者完成前的失败项快照。
        failed = db.list_gcid_import_items(normalized_task_id, "failed")
        task = _execute_items(
            normalized_task_id,
            failed,
            importer=importer,
            target_dir_id=row["target_dir_id"],
            retry=True,
        )
        return task, False
