"""光鸭 GCID 清单的导入预览、账号绑定与任务执行。"""
from __future__ import annotations

import json
import re
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable, Generator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from time import time
from typing import Any

from app import database as db
from app import notifier
from app.clients.guangya import GuangYaClient
from app.logger import get_logger
from app.modules.gcid_manifest import GCIDManifest, GCIDManifestFile, normalize_manifest_v2
from app.modules.guangya_gcid import GuangyaGCIDImporter
from app.modules.process_lock import CrossProcessLock

logger = get_logger(__name__)


class PreviewBindingError(ValueError):
    """预览不存在、过期或与当前用户/目标不一致。"""


class ImportCapabilityUnavailable(RuntimeError):
    """当前光鸭登录会话不能用于导入。"""


class ImportTaskNotFound(ValueError):
    """GCID 导入任务不存在。"""


class OperationTokenConflict(ValueError):
    """operation token 已绑定不同操作。"""


@dataclass(frozen=True, slots=True)
class GCIDImportPreview:
    preview_id: str
    owner_id: str
    target_dir_id: str
    target_account_key: str
    manifest: GCIDManifest
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
        manifest: GCIDManifest,
        *,
        target_dir_id: str,
        owner_id: str = "",
        target_account_key: str = "",
    ) -> GCIDImportPreview:
        target = (target_dir_id or "").strip()
        owner = (owner_id or "").strip()
        if not target:
            raise PreviewBindingError("缺少目标目录")
        now = self._clock()
        snapshot = GCIDImportPreview(
            preview_id=secrets.token_urlsafe(18),
            owner_id=owner,
            target_dir_id=target,
            target_account_key=target_account_key,
            manifest=manifest,
            expires_at=now + self.ttl_seconds,
        )
        with self._lock:
            self._purge_expired(now)
            self._items[snapshot.preview_id] = snapshot
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)
        return snapshot

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
            if digest and digest != snapshot.manifest.digest:
                raise PreviewBindingError("清单摘要已变化，请重新预览")
            self._items.move_to_end(key)
            return snapshot


_preview_store = GCIDImportPreviewStore()
_operation_lock = threading.RLock()
_RETRY_REPLAY_KEY = "gcid_import.retry_replays:v1"
_RETRY_REPLAY_LIMIT = 512
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
_LOGIN_REQUIRED = "请先登录光鸭账号，再选择导入目标目录"
_ITEM_ERRORS = {
    "not_matched": "光鸭未命中该文件，无法秒传",
    "name_conflict": "目标位置有同名文件或目录，内容不匹配",
    "invalid_input": "文件路径或校验信息无效，请核对清单",
    "invalid_target": "目标目录无效，请重新选择目录并预览",
    "rate_limited": "光鸭请求过于频繁，请稍后重试",
    "provider_rejected": "光鸭拒绝了本次请求，请检查账号状态和可用空间",
    "cancel_failed": "文件未确认写入，未能取消云端上传任务，请检查目标目录",
    "verify_failed": "未能核实目标文件，请检查后重试",
}


def reset_runtime_state() -> None:
    """清空进程内预览；重试凭据随数据库持久保存，不随运行时重建丢失。"""
    global _preview_store
    with _operation_lock:
        _preview_store = GCIDImportPreviewStore()


def importer_capability() -> dict[str, Any]:
    with closing(GuangYaClient()) as client:
        available = client.logged_in
    return {"available": available, "reason": "" if available else _LOGIN_REQUIRED}


def _account_key(client: GuangYaClient) -> str:
    try:
        return client.share_account_key()
    except Exception as exc:
        logger.warning("GCID 无法核对光鸭账号 type=%s", type(exc).__name__)
        raise ImportCapabilityUnavailable("无法核对光鸭账号，请检查登录状态后重试") from exc


def _preview_account_key(target_dir_id: str) -> str:
    with closing(GuangYaClient()) as client:
        if not client.logged_in:
            return ""
        account_key = _account_key(client)
        if target_dir_id != "0":
            try:
                directory = client.file_info(target_dir_id)
            except Exception as exc:
                raise ImportCapabilityUnavailable("暂时无法核对光鸭目标目录，请重试") from exc
            if directory is None or not directory.is_dir or directory.file_id != target_dir_id:
                raise PreviewBindingError("当前光鸭账号下的目标目录无效，请重新选择")
        return account_key


@contextmanager
def _open_importer() -> Generator[tuple[GuangyaGCIDImporter, str], None, None]:
    with closing(GuangYaClient()) as client:
        if not client.logged_in:
            raise ImportCapabilityUnavailable(_LOGIN_REQUIRED)
        yield GuangyaGCIDImporter(client), _account_key(client)


def _safe_remote_file_id(value: object) -> str:
    return _CONTROL_CHARS_RE.sub("", str(value or "").strip())[:256]


def _tree_for_files(files: tuple[GCIDManifestFile, ...]) -> tuple[list[dict[str, Any]], bool]:
    root: dict[str, Any] = {}
    remaining = 600
    truncated = False
    for item in files:
        *parts, name = item.path.split("/")
        children = root
        for depth, part in enumerate(parts):
            if part not in children:
                # 每层目录嵌套一个 JSON 对象和一组 children，深链也要限制显示。
                if not remaining or depth == 400:
                    truncated = True
                    break
                children[part] = {
                    "name": part, "type": "directory",
                    "file_count": 0, "total_size": 0, "children": {},
                }
                remaining -= 1
            directory = children[part]
            directory["file_count"] += 1
            directory["total_size"] += item.size
            children = directory["children"]
        else:
            if remaining:
                children[name] = {"name": name, "type": "file", "size": item.size}
                remaining -= 1
            else:
                truncated = True

    def finish(node: dict[str, Any]) -> dict[str, Any]:
        if node["type"] == "file":
            return node
        children = [finish(child) for child in node.pop("children").values()]
        children.sort(key=lambda child: (child["type"] == "file", child["name"].casefold()))
        node["children"] = children
        return node

    return [finish(child) for child in root.values()], truncated


def create_preview(manifest: Any, *, target_dir_id: str, owner_id: str) -> dict[str, Any]:
    normalized = normalize_manifest_v2(manifest)
    snapshot = _preview_store.create(
        normalized, target_dir_id=target_dir_id, owner_id=owner_id,
        target_account_key=_preview_account_key(target_dir_id.strip()),
    )
    tree, truncated = _tree_for_files(normalized.files)
    return {
        "preview_id": snapshot.preview_id,
        "manifest_digest": normalized.digest,
        "target_dir_id": snapshot.target_dir_id,
        "file_count": normalized.file_count,
        "missing_md5_count": sum(1 for item in normalized.files if not item.md5),
        "total_size": normalized.total_size,
        "tree": tree,
        "tree_truncated": truncated,
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
    db.kv_set_conn(
        conn, _RETRY_REPLAY_KEY,
        json.dumps(list(replays), ensure_ascii=False, separators=(",", ":")),
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


def _task_lock(task_id: int) -> CrossProcessLock:
    return CrossProcessLock(f"gcid-import-{task_id}")


@contextmanager
def _claim_task_for_run(task_id: int, *, retry_token: str = "") -> Generator[bool, None, None]:
    """先取得执行锁，再提交任务领取及重试凭据，锁保持到执行结束。"""
    condition = (
        "status IN ('failed','partial_success') AND EXISTS "
        "(SELECT 1 FROM gcid_import_items WHERE task_id=gcid_import_tasks.id AND status='failed')"
        if retry_token else "status='previewed'"
    )
    with ExitStack() as execution:
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replays = _read_retry_replays(conn) if retry_token else OrderedDict()
            key = (task_id, retry_token)
            claimed = key not in replays and conn.execute(
                "UPDATE gcid_import_tasks SET status='running',error='',updated_at=? "
                f"WHERE id=? AND {condition}",
                (db.now(), task_id),
            ).rowcount == 1
            if claimed:
                lock = _task_lock(task_id)
                if not lock.acquire(blocking=False):
                    raise ImportCapabilityUnavailable("GCID 导入任务正由另一请求处理，请稍后重试")
                execution.callback(lock.release)
                if retry_token:
                    replays[key] = None
                    _write_retry_replays(conn, replays)
        yield claimed


def _update_item(
    item_id: int, *, status: str, remote_file_id: str = "", error: str = ""
) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE gcid_import_items SET status=?,remote_file_id=?,error=?,updated_at=? "
            "WHERE id=?",
            (status, remote_file_id, error[:1000], db.now(), item_id),
        )


def _failed_samples(task_id: int, limit: int = 3) -> list[dict[str, Any]]:
    return [
        {"id": int(row["id"]), "path": row["path"], "error": row["error"]}
        for row in db.list_gcid_import_items(task_id, "failed", limit=max(0, limit))
    ]


def _count_task_items(task_id: int) -> tuple[int, int, int, int]:
    with db.get_conn() as conn:
        counts = conn.execute(
            "SELECT COUNT(*),COUNT(CASE WHEN status='success' THEN 1 END),"
            "COUNT(CASE WHEN status='failed' THEN 1 END),"
            "COUNT(CASE WHEN status='running' THEN 1 END) FROM gcid_import_items WHERE task_id=?",
            (task_id,),
        ).fetchone()
    return counts[0], counts[1], counts[2], counts[3]


def serialize_task(row, *, failed_samples: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    task_id = row["id"]
    status = row["status"]
    # 持久 running 卡住旧提交。只有确认无执行者，才在回执中显示中断。
    if status == "running":
        lock = _task_lock(task_id)
        if lock.acquire(blocking=False):
            try:
                row = db.get_gcid_import_task(task_id) or row
                status = "interrupted" if row["status"] == "running" else row["status"]
            finally:
                lock.release()
    pending_count = unknown_count = 0
    if row["status"] == "running":
        total, success_count, failed_count, unknown_count = _count_task_items(task_id)
        pending_count = total - success_count - failed_count - unknown_count
    else:
        success_count = row["success_count"]
        failed_count = row["failed_count"]
    return {
        "id": task_id,
        "manifest_digest": row["manifest_digest"],
        "target_dir_id": row["target_dir_id"],
        "status": status,
        "file_count": row["file_count"],
        "total_size": row["total_size"],
        "success_count": success_count,
        "failed_count": failed_count,
        "pending_count": pending_count,
        "unknown_count": unknown_count,
        "error": "导入已中断，请核对原光鸭账号的目标目录" if status == "interrupted" else row["error"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "can_retry": failed_count > 0 and status in {"failed", "partial_success"},
        "failed_samples": _failed_samples(task_id) if failed_samples is None else failed_samples,
    }


def list_tasks(limit: int = 30) -> list[dict[str, Any]]:
    return [serialize_task(row) for row in db.list_gcid_import_tasks(limit)]


def clear_finished_tasks() -> dict[str, int]:
    finished = "status IN ('success','partial_success','failed')"
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # 仅保留提交凭据，防止另一标签页重放旧请求时重新写入云盘。
        conn.execute(
            "INSERT INTO gcid_cleared_operations(operation_token) "
            f"SELECT operation_token FROM gcid_import_tasks WHERE {finished}"
        )
        deleted_ids = {row[0] for row in conn.execute(
            f"DELETE FROM gcid_import_tasks WHERE {finished} RETURNING id"
        )}
        if deleted_ids:
            replays = _read_retry_replays(conn)
            remaining = OrderedDict((key, None) for key in replays if key[0] not in deleted_ids)
            if len(remaining) != len(replays):
                _write_retry_replays(conn, remaining)
        retained = conn.execute("SELECT COUNT(*) FROM gcid_import_tasks").fetchone()[0]
        return {"deleted": len(deleted_ids), "retained": retained}


def _finish_task(task_id: int) -> dict[str, Any]:
    total, success_count, failed_count, _ = _count_task_items(task_id)
    if total and success_count == total:
        status, error = "success", ""
    elif success_count:
        status, error = "partial_success", f"{failed_count} 个文件导入失败"
    else:
        status, error = "failed", "GCID 导入失败"
    # 发布终态前留存通知所需的失败样本，历史随后即可安全清除。
    samples = _failed_samples(task_id)
    row = db.update_gcid_import_task(
        task_id,
        status=status,
        success_count=success_count,
        failed_count=failed_count,
        error=error,
    )
    return serialize_task(row, failed_samples=samples)


def _execute_items(
    task_id: int,
    items: list,
    *,
    importer: GuangyaGCIDImporter,
    target_dir_id: str,
    retry: bool,
) -> dict[str, Any]:
    notifier.notify_gcid_import_started(
        task_id=task_id,
        file_count=len(items),
        total_size=sum(row["size"] for row in items),
        retry=retry,
    )
    for row in items:
        item_id = row["id"]
        _update_item(item_id, status="running")
        outcome = importer.import_file(
            target_dir_id=target_dir_id,
            path=row["path"],
            size=row["size"],
            gcid=row["gcid"],
            md5=row["md5"],
        )
        if outcome.success:
            _update_item(
                item_id,
                status="success",
                remote_file_id=_safe_remote_file_id(outcome.remote_file_id),
            )
        else:
            if outcome.error_code == "not_matched" and not row["md5"]:
                error = "清单缺少 MD5，光鸭仅按 GCID 未命中"
            else:
                error = _ITEM_ERRORS.get(outcome.error_code, "光鸭文件导入失败，请检查登录或稍后重试")
            _update_item(item_id, status="failed", error=error)
    return _finish_task(task_id)


def _validate_operation_token(value: str) -> str:
    token = (value or "").strip()
    if not token or len(token) > 256:
        raise ValueError("operation_token 无效")
    return token


def _require_task(task_id: int):
    row = db.get_gcid_import_task(task_id)
    if row is None:
        raise ImportTaskNotFound("GCID 导入任务不存在")
    return row


def _run_task(
    task_id: int, importer: GuangyaGCIDImporter, target_dir_id: str, *, retry_token: str = "",
) -> tuple[dict[str, Any], bool]:
    with _claim_task_for_run(task_id, retry_token=retry_token) as claimed:
        if not claimed:
            return serialize_task(_require_task(task_id)), True
        retry = bool(retry_token)
        task = _execute_items(
            task_id, db.list_gcid_import_items(task_id, "failed" if retry else "previewed"),
            importer=importer, target_dir_id=target_dir_id, retry=retry,
        )
    notifier.notify_gcid_import_finished(
        task_id=task_id,
        status=task["status"],
        success_count=task["success_count"],
        failed_count=task["failed_count"],
        failed_samples=task["failed_samples"],
        retry=retry,
    )
    return task, False


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
                existing["manifest_digest"], existing["target_account_key"], existing["target_dir_id"],
                existing["file_count"], existing["total_size"],
            ) != (
                snapshot.manifest.digest, snapshot.target_account_key, snapshot.target_dir_id,
                snapshot.manifest.file_count, snapshot.manifest.total_size,
            ):
                raise OperationTokenConflict("operation_token 已绑定其他 GCID 导入任务")
            if existing["status"] != "previewed":
                return serialize_task(existing), True

        if not snapshot.manifest.file_count:
            raise ValueError("清单中没有可导入的文件")
        with _open_importer() as (importer, account_key):
            if snapshot.target_account_key != account_key:
                raise PreviewBindingError("光鸭登录账号已变化，请重新选择目标目录并预览")
            task_id = db.create_gcid_import_task(
                operation_token=token,
                manifest_digest=snapshot.manifest.digest,
                target_dir_id=snapshot.target_dir_id,
                target_account_key=account_key,
                file_count=snapshot.manifest.file_count,
                total_size=snapshot.manifest.total_size,
                items=[item.to_dict() for item in snapshot.manifest.files],
            )
            return _run_task(task_id, importer, snapshot.target_dir_id)


def retry_task(
    task_id: int, *, operation_token: str
) -> tuple[dict[str, Any], bool]:
    token = _validate_operation_token(operation_token)
    with _operation_lock:
        row = _require_task(task_id)
        if row["status"] == "running" or _retry_was_recorded(task_id, token):
            return serialize_task(_require_task(task_id)), True
        if not db.list_gcid_import_items(task_id, "failed", limit=1):
            raise ValueError("任务没有可重试的失败项")
        with _open_importer() as (importer, account_key):
            if not row["target_account_key"]:
                raise PreviewBindingError("旧任务未记录目标账号，请重新预览并导入清单")
            if row["target_account_key"] != account_key:
                raise PreviewBindingError("请切换回该任务的光鸭账号后重试")
            return _run_task(task_id, importer, row["target_dir_id"], retry_token=token)
