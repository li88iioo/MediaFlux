"""可恢复的光鸭单次操作队列数据访问。"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import time
from datetime import datetime
import uuid
from typing import TYPE_CHECKING, Any

from app.modules.web_secret import get_web_secret
from app.repositories.agent_jobs import agent_job_owner_digest

if TYPE_CHECKING:
    from types import ModuleType

_ALLOWED_KINDS = {"agent_directory_scrape", "directory_scrape"}
_TERMINAL_STATUSES = {"completed", "partial", "failed", "cancelled", "manual_review"}
_MAX_JSON_BYTES = 131_072
_MAX_TEXT = 240
_MAX_TERMINAL_HISTORY_PER_OWNER = 64
_MAX_ACTIVE_PER_OWNER = 4
_MAX_ACTIVE_GLOBAL = 128
_DEFAULT_TTL_SECONDS = 3_600
_ALLOWED_RESULT_STATS = {
    "total", "matched", "need_confirm", "moved", "renamed", "rename_failed",
    "metadata_moved", "stopped", "skipped", "conflict", "failed",
    "subtitle_moved", "subtitle_skipped", "replacement_cleanup_failed",
    "empty_dir_cleanup_failed", "source_dir_cleanup_failed", "audit_failures",
}


class OrganizeOperationQueueFullError(RuntimeError):
    """持久队列达到安全容量上限。"""


class OrganizeOperationCancelled(RuntimeError):
    """持久任务收到协作式取消；当前 provider 写入尚未开始。"""

    provider_write_not_started = True


def _database() -> "ModuleType":
    from app import database

    return database


def get_conn():
    return _database().get_conn()


def now() -> str:
    return _database().now()


def organize_operation_owner_digest(owner: str) -> str:
    return agent_job_owner_digest(owner)


def _secret() -> bytes:
    secret = str(get_web_secret() or "")
    if not secret:
        raise ValueError("光鸭操作队列密钥不可用")
    return secret.encode("utf-8")


def _safe_job_id(value: object) -> str:
    job_id = str(value or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ValueError("光鸭操作任务 ID 无效")
    return job_id


def _safe_kind(value: object) -> str:
    kind = str(value or "").strip()
    if kind not in _ALLOWED_KINDS:
        raise ValueError("光鸭操作任务类型无效")
    return kind


def _safe_text(value: object, *, limit: int = _MAX_TEXT) -> str:
    return " ".join(str(value or "").split())[:limit]


def _safe_json(value: object, *, field: str) -> str:
    if isinstance(value, str):
        raw = value
    else:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(raw.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError(f"{field}过大")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field}无效") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field}无效")
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sanitize_organize_operation_result(value: object) -> dict[str, Any]:
    """持久化/公开状态只保留固定聚合计数，丢弃目录与执行标识。"""
    if not isinstance(value, dict) or not isinstance(value.get("stats"), dict):
        return {}
    stats: dict[str, int | float] = {}
    for key, raw in value["stats"].items():
        if str(key) not in _ALLOWED_RESULT_STATS or isinstance(raw, bool):
            continue
        if isinstance(raw, int):
            stats[str(key)] = max(0, raw)
        elif isinstance(raw, float):
            stats[str(key)] = max(0.0, round(raw, 3))
    return {"stats": stats}


def organize_operation_public_ref(job_id: str) -> str:
    safe_id = _safe_job_id(job_id)
    return "GY-" + "-".join(
        safe_id[index:index + 4].upper() for index in range(0, 32, 4)
    )


def organize_operation_job_id_from_public_ref(value: object) -> str:
    reference = str(value or "").strip().upper()
    if not re.fullmatch(r"GY-(?:[0-9A-F]{4}-){7}[0-9A-F]{4}", reference):
        raise ValueError("光鸭操作编号无效")
    return _safe_job_id(reference[3:].replace("-", "").casefold())


def organize_operation_dedupe_digest(owner_digest: str, value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 512:
        raise ValueError("光鸭操作去重键无效")
    return hmac.new(
        _secret(),
        b"mediaflux-organize-operation:v2\0"
        + owner_digest.encode("ascii")
        + b"\0"
        + normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _payload_auth(*, job_id: str, owner_digest: str, job_kind: str, payload_json: str) -> str:
    return hmac.new(
        _secret(),
        b"mediaflux-organize-operation-payload:v1\0"
        + job_id.encode("ascii")
        + b"\0"
        + owner_digest.encode("ascii")
        + b"\0"
        + job_kind.encode("ascii")
        + b"\0"
        + payload_json.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_organize_operation_payload(row: dict[str, Any] | sqlite3.Row) -> bool:
    try:
        expected = _payload_auth(
            job_id=_safe_job_id(row["job_id"]),
            owner_digest=str(row["owner_digest"] or ""),
            job_kind=_safe_kind(row["job_kind"]),
            payload_json=str(row["payload_json"] or "{}"),
        )
        actual = str(row["payload_auth"] or "")
    except (KeyError, TypeError, ValueError, IndexError):
        return False
    return bool(actual) and hmac.compare_digest(actual, expected)


def _expire_pending(conn: sqlite3.Connection, current_epoch: float) -> int:
    timestamp = now()
    cur = conn.execute(
        "UPDATE organize_operation_jobs SET status='cancelled',payload_json='{}',"
        "payload_auth='',error_code='QueueExpired',error='排队确认已过期，请重新预检',"
        "finished_at=COALESCE(finished_at,?),updated_at=? "
        "WHERE status='pending' AND expires_at<=?",
        (timestamp, timestamp, float(current_epoch)),
    )
    conn.execute(
        "DELETE FROM organize_operation_jobs WHERE purged_at IS NOT NULL "
        "AND status<>'running'"
    )
    return max(0, int(cur.rowcount or 0))


def _trim_terminal_history(conn: sqlite3.Connection, owner_digest: str) -> None:
    # manual_review 是结果未知的安全告警，不应被普通成功历史挤掉；由 TTL 清理兜底。
    stale = conn.execute(
        "SELECT job_id FROM organize_operation_jobs WHERE owner_digest=? "
        "AND status IN ('completed','partial','failed','cancelled') "
        "ORDER BY updated_at DESC,job_id DESC LIMIT -1 OFFSET ?",
        (owner_digest, _MAX_TERMINAL_HISTORY_PER_OWNER),
    ).fetchall()
    if stale:
        conn.executemany(
            "DELETE FROM organize_operation_jobs WHERE job_id=?",
            [(str(row["job_id"]),) for row in stale],
        )


def enqueue_organize_operation_job(
    *,
    job_kind: str,
    owner: str,
    operation: str,
    reference: str,
    payload: dict[str, Any],
    dedupe_key: str,
    job_id: str | None = None,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> tuple[sqlite3.Row, bool]:
    """幂等创建持久化操作；返回 ``(row, replayed)``。"""
    safe_kind = _safe_kind(job_kind)
    owner_digest = organize_operation_owner_digest(owner)
    safe_id = _safe_job_id(job_id or uuid.uuid4().hex)
    safe_operation = _safe_text(operation or "操作", limit=80) or "操作"
    safe_reference = _safe_text(reference, limit=240)
    safe_payload = _safe_json(payload, field="光鸭操作任务参数")
    digest = organize_operation_dedupe_digest(owner_digest, dedupe_key)
    auth = _payload_auth(
        job_id=safe_id,
        owner_digest=owner_digest,
        job_kind=safe_kind,
        payload_json=safe_payload,
    )
    lifetime = max(60, min(int(ttl_seconds), 86_400))
    current_epoch = time.time()
    expires_at = current_epoch + lifetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _expire_pending(conn, current_epoch)
        existing = conn.execute(
            "SELECT * FROM organize_operation_jobs WHERE owner_digest=? "
            "AND dedupe_digest=? AND status IN ('pending','running') "
            "ORDER BY created_at,job_id LIMIT 1",
            (owner_digest, digest),
        ).fetchone()
        if existing is not None:
            return existing, True
        owner_active = conn.execute(
            "SELECT COUNT(*) AS total FROM organize_operation_jobs "
            "WHERE owner_digest=? AND status IN ('pending','running')",
            (owner_digest,),
        ).fetchone()
        if int(owner_active["total"] or 0) >= _MAX_ACTIVE_PER_OWNER:
            raise OrganizeOperationQueueFullError("当前会话的光鸭操作排队已满")
        global_active = conn.execute(
            "SELECT COUNT(*) AS total FROM organize_operation_jobs "
            "WHERE status IN ('pending','running')"
        ).fetchone()
        if int(global_active["total"] or 0) >= _MAX_ACTIVE_GLOBAL:
            raise OrganizeOperationQueueFullError("光鸭操作队列已满")
        try:
            conn.execute(
                "INSERT INTO organize_operation_jobs("
                "job_id,job_kind,owner_digest,operation,reference,payload_json,payload_auth,"
                "dedupe_digest,status,lease_generation,result_json,error_code,error,"
                "cancel_requested,expires_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,'pending',0,'{}','','',0,?,?,?)",
                (
                    safe_id, safe_kind, owner_digest, safe_operation, safe_reference,
                    safe_payload, auth, digest, expires_at, timestamp, timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            existing = conn.execute(
                "SELECT * FROM organize_operation_jobs WHERE owner_digest=? "
                "AND dedupe_digest=? AND status IN ('pending','running') "
                "ORDER BY created_at,job_id LIMIT 1",
                (owner_digest, digest),
            ).fetchone()
            if existing is not None:
                return existing, True
            raise
        _trim_terminal_history(conn, owner_digest)
        row = conn.execute(
            "SELECT * FROM organize_operation_jobs WHERE job_id=?", (safe_id,)
        ).fetchone()
        if row is None:
            raise sqlite3.DatabaseError("光鸭操作任务写入后不可见")
        return row, False


def get_organize_operation_job(job_id: str) -> sqlite3.Row | None:
    safe_id = _safe_job_id(job_id)
    try:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM organize_operation_jobs WHERE job_id=?", (safe_id,)
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).casefold():
            return None
        raise


def get_organize_operation_job_for_owner(job_id: str, owner: str) -> sqlite3.Row | None:
    safe_id = _safe_job_id(job_id)
    owner_digest = organize_operation_owner_digest(owner)
    try:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM organize_operation_jobs WHERE job_id=? AND owner_digest=?",
                (safe_id, owner_digest),
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).casefold():
            return None
        raise


def claim_organize_operation_job(job_id: str | None = None) -> sqlite3.Row | None:
    safe_id = _safe_job_id(job_id) if job_id else ""
    timestamp = now()
    current_epoch = time.time()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _expire_pending(conn, current_epoch)
        row = conn.execute(
            "SELECT job_id FROM organize_operation_jobs WHERE status='pending' "
            "AND expires_at>? ORDER BY created_at,job_id LIMIT 1",
            (current_epoch,),
        ).fetchone()
        if row is None:
            return None
        selected = str(row["job_id"])
        if safe_id and selected != safe_id:
            return None
        cur = conn.execute(
            "UPDATE organize_operation_jobs SET status='running',"
            "lease_generation=lease_generation+1,started_at=COALESCE(started_at,?),"
            "updated_at=? WHERE job_id=? AND status='pending' AND expires_at>?",
            (timestamp, timestamp, selected, current_epoch),
        )
        if cur.rowcount != 1:
            return None
        return conn.execute(
            "SELECT * FROM organize_operation_jobs WHERE job_id=?", (selected,)
        ).fetchone()


def is_organize_operation_cancel_requested(
    job_id: str, *, expected_lease_generation: int
) -> bool:
    safe_id = _safe_job_id(job_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT cancel_requested,status,lease_generation FROM organize_operation_jobs "
            "WHERE job_id=?",
            (safe_id,),
        ).fetchone()
    if row is None:
        return True
    return (
        bool(row["cancel_requested"])
        or str(row["status"] or "") != "running"
        or int(row["lease_generation"] or 0) != max(0, int(expected_lease_generation))
    )


def finish_organize_operation_job(
    job_id: str,
    *,
    expected_lease_generation: int,
    status: str,
    result: dict[str, Any] | None = None,
    error_code: str = "",
    error: str = "",
) -> bool:
    safe_id = _safe_job_id(job_id)
    safe_status = str(status or "").strip().lower()
    if safe_status not in _TERMINAL_STATUSES:
        raise ValueError("光鸭操作任务终态无效")
    safe_result = _safe_json(
        sanitize_organize_operation_result(result or {}),
        field="光鸭操作任务结果",
    )
    safe_error_code = re.sub(r"[^A-Za-z0-9_.-]", "", str(error_code or ""))[:80]
    safe_error = _safe_text(error, limit=500)
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT owner_digest,purged_at FROM organize_operation_jobs WHERE job_id=? "
            "AND status='running' AND lease_generation=?",
            (safe_id, max(0, int(expected_lease_generation))),
        ).fetchone()
        if row is None:
            return False
        cur = conn.execute(
            "UPDATE organize_operation_jobs SET status=?,reference='',payload_json='{}',payload_auth='',"
            "result_json=?,error_code=?,error=?,finished_at=?,updated_at=? "
            "WHERE job_id=? AND status='running' AND lease_generation=?",
            (
                safe_status, safe_result, safe_error_code, safe_error, timestamp,
                timestamp, safe_id, max(0, int(expected_lease_generation)),
            ),
        )
        if cur.rowcount != 1:
            return False
        if row["purged_at"] is not None:
            conn.execute("DELETE FROM organize_operation_jobs WHERE job_id=?", (safe_id,))
        else:
            _trim_terminal_history(conn, str(row["owner_digest"] or ""))
        return True


def fail_pending_organize_operation_job(
    job_id: str, *, error_code: str, error: str
) -> bool:
    """线程尚未启动时把 pending 任务原子收束为失败。"""
    safe_id = _safe_job_id(job_id)
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE organize_operation_jobs SET status='failed',reference='',payload_json='{}',"
            "payload_auth='',error_code=?,error=?,finished_at=?,updated_at=? "
            "WHERE job_id=? AND status='pending'",
            (
                re.sub(r"[^A-Za-z0-9_.-]", "", str(error_code or ""))[:80],
                _safe_text(error, limit=500), timestamp, timestamp, safe_id,
            ),
        )
        return cur.rowcount == 1


def count_running_organize_operation_jobs() -> int:
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM organize_operation_jobs WHERE status='running'"
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).casefold():
            return 0
        raise
    return int(row["total"] or 0) if row is not None else 0


def recover_orphaned_organize_operation_jobs() -> int:
    """调用方已持有跨进程整理锁时，收束失去执行者的 running 任务。"""
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cancelled = conn.execute(
            "UPDATE organize_operation_jobs SET status='cancelled',lease_generation=lease_generation+1,"
            "reference='',payload_json='{}',payload_auth='',error_code='PrivacyPurgeCancelled',"
            "error='',finished_at=COALESCE(finished_at,?),updated_at=? "
            "WHERE status='running' AND cancel_requested=1",
            (timestamp, timestamp),
        )
        reviewed = conn.execute(
            "UPDATE organize_operation_jobs SET status='manual_review',"
            "lease_generation=lease_generation+1,reference='',payload_json='{}',payload_auth='',"
            "error_code='WorkerExitedUnknownOutcome',"
            "error=CASE WHEN COALESCE(error,'')='' THEN "
            "'执行进程已退出，远端结果未知；请核对目标目录，勿直接重试' "
            "ELSE error END,finished_at=COALESCE(finished_at,?),updated_at=? "
            "WHERE status='running'",
            (timestamp, timestamp),
        )
        conn.execute(
            "DELETE FROM organize_operation_jobs WHERE purged_at IS NOT NULL "
            "AND status<>'running'"
        )
        return max(0, int(cancelled.rowcount or 0)) + max(0, int(reviewed.rowcount or 0))


def count_pending_organize_operation_jobs() -> int:
    try:
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _expire_pending(conn, time.time())
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM organize_operation_jobs WHERE status='pending'"
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).casefold():
            return 0
        raise
    return int(row["total"] or 0) if row is not None else 0


def list_pending_organize_operation_jobs(*, limit: int = 64) -> list[sqlite3.Row]:
    safe_limit = max(1, min(int(limit), 64))
    try:
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _expire_pending(conn, time.time())
            return conn.execute(
                "SELECT * FROM organize_operation_jobs WHERE status='pending' "
                "ORDER BY created_at,job_id LIMIT ?", (safe_limit,)
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).casefold():
            return []
        raise


def organize_operation_queue_position(job_id: str) -> int:
    safe_id = _safe_job_id(job_id)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _expire_pending(conn, time.time())
        row = conn.execute(
            "SELECT created_at FROM organize_operation_jobs "
            "WHERE job_id=? AND status='pending'", (safe_id,)
        ).fetchone()
        if row is None:
            return 0
        count = conn.execute(
            "SELECT COUNT(*) AS total FROM organize_operation_jobs "
            "WHERE status='pending' AND (created_at<? OR (created_at=? AND job_id<=?))",
            (str(row["created_at"]), str(row["created_at"]), safe_id),
        ).fetchone()
    return int(count["total"] or 0) if count is not None else 0
