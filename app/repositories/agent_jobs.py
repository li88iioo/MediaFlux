"""Owner 隔离、可恢复、可取消的 Agent 长任务数据访问。"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from typing import TYPE_CHECKING, Any

from app.agent.errors import AgentToolError
from app.modules.web_secret import get_web_secret

if TYPE_CHECKING:
    from types import ModuleType

_ALLOWED_JOB_TYPES = {"library_episode_audit"}
_ACTIVE_STATUSES = {"pending", "running", "retry_wait"}
_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
_MAX_OWNER_LENGTH = 512
_MAX_JSON_BYTES = 65_536
_MAX_SUMMARY_LENGTH = 240
_MAX_ERROR_CODE_LENGTH = 80
_MAX_HISTORY_PER_OWNER = 50


def _database() -> ModuleType:
    from app import database

    return database


def get_conn():
    return _database().get_conn()


def now() -> str:
    return _database().now()


def agent_job_owner_digest(owner: str) -> str:
    """使用独立 domain separator 派生不可逆 owner 分区键。"""
    normalized = str(owner or "").strip()
    if not normalized or len(normalized) > _MAX_OWNER_LENGTH:
        raise AgentToolError("无法确认当前 Agent 身份", code="identity_required")
    secret = str(get_web_secret() or "")
    if not secret:
        raise AgentToolError("Agent 身份隔离密钥不可用", code="identity_required")
    return hmac.new(
        secret.encode("utf-8"),
        b"mediaflux-agent-durable-job:v1\0" + normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _safe_job_type(value: object) -> str:
    job_type = str(value or "").strip()
    if job_type not in _ALLOWED_JOB_TYPES:
        raise ValueError("Agent 长任务类型无效")
    return job_type


def _safe_job_id(value: object) -> str:
    job_id = str(value or "").strip()
    if not re.fullmatch(r"job_[A-Za-z0-9_-]{16,80}", job_id):
        raise AgentToolError("未找到对应的后台任务", code="job_not_found")
    return job_id


def _safe_dedupe_key(value: object) -> str:
    key = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9:._-]{1,160}", key):
        raise ValueError("Agent 长任务去重键无效")
    return key


def _safe_json(value: object, *, field: str) -> str:
    raw = str(value or "{}")
    if len(raw.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError(f"{field}过大")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field}无效") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field}无效")
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _safe_summary(value: object) -> str:
    return " ".join(str(value or "").split())[:_MAX_SUMMARY_LENGTH]


def _safe_error_code(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "", str(value or ""))[:_MAX_ERROR_CODE_LENGTH]


def _select_job(
    conn: sqlite3.Connection, *, owner_digest: str, job_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM agent_jobs WHERE owner_digest=? AND job_id=?",
        (owner_digest, job_id),
    ).fetchone()


def _trim_owner_history(conn: sqlite3.Connection, *, owner_digest: str) -> None:
    stale = conn.execute(
        "SELECT job_id FROM agent_jobs WHERE owner_digest=? "
        "AND status IN ('succeeded','failed','cancelled') "
        "ORDER BY updated_at DESC,job_id DESC LIMIT -1 OFFSET ?",
        (owner_digest, _MAX_HISTORY_PER_OWNER),
    ).fetchall()
    if stale:
        conn.executemany(
            "DELETE FROM agent_jobs WHERE owner_digest=? AND job_id=?",
            [(owner_digest, str(row["job_id"])) for row in stale],
        )


def create_agent_job(
    *,
    owner: str,
    job_type: str,
    dedupe_key: str,
    input_json: str,
    checkpoint_json: str,
    projection_json: str,
    progress_total: int = 0,
    max_attempts: int = 3,
    job_id: str | None = None,
) -> tuple[sqlite3.Row, bool]:
    """幂等创建 owner 内唯一的活动任务。"""
    owner_digest = agent_job_owner_digest(owner)
    safe_type = _safe_job_type(job_type)
    safe_dedupe = _safe_dedupe_key(dedupe_key)
    safe_input = _safe_json(input_json, field="Agent 长任务输入")
    safe_checkpoint = _safe_json(checkpoint_json, field="Agent 长任务断点")
    safe_projection = _safe_json(projection_json, field="Agent 长任务投影")
    safe_total = max(0, min(int(progress_total), 1_000_000))
    safe_max_attempts = max(1, min(int(max_attempts), 10))
    generated_id = job_id or f"job_{secrets.token_urlsafe(18)}"
    safe_id = _safe_job_id(generated_id)
    timestamp = now()

    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM agent_jobs WHERE owner_digest=? AND job_type=? "
            "AND dedupe_key=? AND status IN ('pending','running','retry_wait') "
            "ORDER BY created_at DESC,job_id DESC LIMIT 1",
            (owner_digest, safe_type, safe_dedupe),
        ).fetchone()
        if existing is not None:
            return existing, False
        try:
            conn.execute(
                "INSERT INTO agent_jobs("
                "job_id,owner_digest,job_type,dedupe_key,status,input_json,"
                "checkpoint_json,projection_json,summary,error_code,attempts,"
                "max_attempts,lease_generation,next_run_at,progress_current,"
                "progress_total,cancel_requested,started_at,finished_at,created_at,updated_at) "
                "VALUES(?,?,?,?,'pending',?,?,?,'','',0,?,0,?,0,?,0,NULL,NULL,?,?)",
                (
                    safe_id,
                    owner_digest,
                    safe_type,
                    safe_dedupe,
                    safe_input,
                    safe_checkpoint,
                    safe_projection,
                    safe_max_attempts,
                    timestamp,
                    safe_total,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            # 并发创建命中活动任务唯一索引时，返回胜出的同 owner 任务。
            existing = conn.execute(
                "SELECT * FROM agent_jobs WHERE owner_digest=? AND job_type=? "
                "AND dedupe_key=? AND status IN ('pending','running','retry_wait') "
                "ORDER BY created_at DESC,job_id DESC LIMIT 1",
                (owner_digest, safe_type, safe_dedupe),
            ).fetchone()
            if existing is None:
                raise
            return existing, False
        _trim_owner_history(conn, owner_digest=owner_digest)
        created = _select_job(conn, owner_digest=owner_digest, job_id=safe_id)
    assert created is not None
    return created, True


def get_agent_job(*, owner: str, job_id: str) -> sqlite3.Row | None:
    owner_digest = agent_job_owner_digest(owner)
    safe_id = _safe_job_id(job_id)
    with get_conn() as conn:
        return _select_job(conn, owner_digest=owner_digest, job_id=safe_id)


def list_agent_jobs(
    *, owner: str, limit: int = 10, job_type: str | None = None
) -> list[sqlite3.Row]:
    owner_digest = agent_job_owner_digest(owner)
    safe_limit = max(1, min(int(limit), 50))
    values: list[Any] = [owner_digest]
    clause = ""
    if job_type is not None:
        clause = " AND job_type=?"
        values.append(_safe_job_type(job_type))
    values.append(safe_limit)
    with get_conn() as conn:
        return list(
            conn.execute(
                "SELECT * FROM agent_jobs WHERE owner_digest=?"
                + clause
                + " ORDER BY updated_at DESC,job_id DESC LIMIT ?",
                tuple(values),
            ).fetchall()
        )


def find_active_agent_job(
    *,
    owner: str,
    job_type: str,
    dedupe_key: str,
) -> sqlite3.Row | None:
    """精确查找 owner 内同类型、同去重键的活动任务。"""
    owner_digest = agent_job_owner_digest(owner)
    safe_type = _safe_job_type(job_type)
    safe_dedupe = _safe_dedupe_key(dedupe_key)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM agent_jobs WHERE owner_digest=? AND job_type=? "
            "AND dedupe_key=? AND status IN ('pending','running','retry_wait') "
            "ORDER BY created_at DESC,job_id DESC LIMIT 1",
            (owner_digest, safe_type, safe_dedupe),
        ).fetchone()


def find_latest_active_agent_job(
    *, owner: str, job_type: str = "library_episode_audit"
) -> sqlite3.Row | None:
    owner_digest = agent_job_owner_digest(owner)
    safe_type = _safe_job_type(job_type)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM agent_jobs WHERE owner_digest=? AND job_type=? "
            "AND status IN ('pending','running','retry_wait') "
            "ORDER BY updated_at DESC,job_id DESC LIMIT 1",
            (owner_digest, safe_type),
        ).fetchone()


def claim_due_agent_job(
    *,
    job_type: str,
    current_time: str | None = None,
    stale_before: str | None = None,
) -> sqlite3.Row | None:
    """原子领取一个到期任务，并回收过期租约。"""
    safe_type = _safe_job_type(job_type)
    timestamp = str(current_time or now())
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if stale_before:
            conn.execute(
                "UPDATE agent_jobs SET "
                "status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'retry_wait' END,"
                "lease_generation=lease_generation+1,next_run_at=?,"
                "error_code=CASE WHEN cancel_requested=1 THEN '' ELSE 'LeaseExpired' END,"
                "finished_at=CASE WHEN cancel_requested=1 THEN ? ELSE finished_at END,"
                "updated_at=? WHERE job_type=? AND status='running' AND updated_at<=?",
                (timestamp, timestamp, timestamp, safe_type, str(stale_before)),
            )
        row = conn.execute(
            "SELECT job_id FROM agent_jobs WHERE job_type=? "
            "AND status IN ('pending','retry_wait') AND cancel_requested=0 "
            "AND next_run_at<=? ORDER BY next_run_at ASC,created_at ASC,job_id ASC LIMIT 1",
            (safe_type, timestamp),
        ).fetchone()
        if row is None:
            return None
        job_id = str(row["job_id"])
        cur = conn.execute(
            "UPDATE agent_jobs SET status='running',lease_generation=lease_generation+1,"
            "started_at=COALESCE(started_at,?),error_code='',updated_at=? "
            "WHERE job_id=? AND status IN ('pending','retry_wait') "
            "AND cancel_requested=0 AND next_run_at<=?",
            (timestamp, timestamp, job_id, timestamp),
        )
        if cur.rowcount != 1:
            return None
        return conn.execute(
            "SELECT * FROM agent_jobs WHERE job_id=?", (job_id,)
        ).fetchone()


def renew_agent_job_lease(
    job_id: str,
    *,
    expected_lease_generation: int,
    renewed_at: str | None = None,
) -> bool:
    """续期仍由当前 generation 持有的运行中租约。"""
    safe_id = _safe_job_id(job_id)
    timestamp = str(renewed_at or now())
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_jobs SET updated_at=? WHERE job_id=? AND status='running' "
            "AND lease_generation=? AND cancel_requested=0",
            (timestamp, safe_id, max(0, int(expected_lease_generation))),
        )
        return cur.rowcount == 1


def is_agent_job_cancel_requested(
    job_id: str, *, expected_lease_generation: int
) -> bool:
    safe_id = _safe_job_id(job_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT cancel_requested FROM agent_jobs WHERE job_id=? "
            "AND status='running' AND lease_generation=?",
            (safe_id, max(0, int(expected_lease_generation))),
        ).fetchone()
    return bool(row and int(row["cancel_requested"] or 0))


def continue_agent_job(
    job_id: str,
    *,
    expected_lease_generation: int,
    checkpoint_json: str,
    projection_json: str,
    progress_current: int,
    progress_total: int,
    next_run_at: str,
    summary: str,
) -> bool:
    safe_id = _safe_job_id(job_id)
    safe_checkpoint = _safe_json(checkpoint_json, field="Agent 长任务断点")
    safe_projection = _safe_json(projection_json, field="Agent 长任务投影")
    current = max(0, min(int(progress_current), 1_000_000))
    total = max(0, min(int(progress_total), 1_000_000))
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_jobs SET status='pending',checkpoint_json=?,projection_json=?,"
            "summary=?,error_code='',attempts=0,next_run_at=?,progress_current=?,"
            "progress_total=?,updated_at=? WHERE job_id=? AND status='running' "
            "AND lease_generation=? AND cancel_requested=0",
            (
                safe_checkpoint,
                safe_projection,
                _safe_summary(summary),
                str(next_run_at or timestamp),
                current,
                total,
                timestamp,
                safe_id,
                max(0, int(expected_lease_generation)),
            ),
        )
        return cur.rowcount == 1


def complete_agent_job(
    job_id: str,
    *,
    expected_lease_generation: int,
    projection_json: str,
    progress_current: int,
    progress_total: int,
    summary: str,
    finished_at: str | None = None,
) -> bool:
    safe_id = _safe_job_id(job_id)
    safe_projection = _safe_json(projection_json, field="Agent 长任务投影")
    timestamp = str(finished_at or now())
    current = max(0, min(int(progress_current), 1_000_000))
    total = max(0, min(int(progress_total), 1_000_000))
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_jobs SET status='succeeded',projection_json=?,summary=?,"
            "error_code='',attempts=0,progress_current=?,progress_total=?,"
            "finished_at=?,updated_at=? WHERE job_id=? AND status='running' "
            "AND lease_generation=? AND cancel_requested=0",
            (
                safe_projection,
                _safe_summary(summary),
                current,
                total,
                timestamp,
                timestamp,
                safe_id,
                max(0, int(expected_lease_generation)),
            ),
        )
        return cur.rowcount == 1


def release_agent_job_lease(
    job_id: str,
    *,
    expected_lease_generation: int,
    next_run_at: str,
    summary: str = "等待 Media Agent 重新开启",
) -> bool:
    """总开关切换时释放运行租约，不消费重试预算或提交旧批次结果。"""
    safe_id = _safe_job_id(job_id)
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_jobs SET status='pending',lease_generation=lease_generation+1,"
            "next_run_at=?,summary=?,error_code='',updated_at=? WHERE job_id=? "
            "AND status='running' AND lease_generation=? AND cancel_requested=0",
            (
                str(next_run_at or timestamp),
                _safe_summary(summary),
                timestamp,
                safe_id,
                max(0, int(expected_lease_generation)),
            ),
        )
        return cur.rowcount == 1


def fail_or_retry_agent_job(
    job_id: str,
    *,
    expected_lease_generation: int,
    attempts: int,
    next_run_at: str,
    error_code: str,
    summary: str,
) -> str:
    """按 max_attempts 决定重试或失败；返回实际状态。"""
    safe_id = _safe_job_id(job_id)
    safe_attempts = max(1, min(int(attempts), 100))
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT max_attempts,cancel_requested FROM agent_jobs WHERE job_id=? "
            "AND status='running' AND lease_generation=?",
            (safe_id, max(0, int(expected_lease_generation))),
        ).fetchone()
        if row is None:
            return "stale"
        if int(row["cancel_requested"] or 0):
            conn.execute(
                "UPDATE agent_jobs SET status='cancelled',summary='任务已取消',"
                "error_code='',finished_at=?,updated_at=? WHERE job_id=? "
                "AND status='running' AND lease_generation=?",
                (timestamp, timestamp, safe_id, max(0, int(expected_lease_generation))),
            )
            return "cancelled"
        terminal = safe_attempts >= max(1, int(row["max_attempts"] or 1))
        status = "failed" if terminal else "retry_wait"
        cur = conn.execute(
            "UPDATE agent_jobs SET status=?,attempts=?,next_run_at=?,summary=?,"
            "error_code=?,finished_at=CASE WHEN ?='failed' THEN ? ELSE NULL END,"
            "updated_at=? WHERE job_id=? AND status='running' AND lease_generation=?",
            (
                status,
                safe_attempts,
                str(next_run_at or timestamp),
                _safe_summary(summary),
                _safe_error_code(error_code),
                status,
                timestamp,
                timestamp,
                safe_id,
                max(0, int(expected_lease_generation)),
            ),
        )
        return status if cur.rowcount == 1 else "stale"


def cancel_agent_job(
    *, owner: str, job_id: str | None = None, job_type: str = "library_episode_audit"
) -> tuple[sqlite3.Row | None, str]:
    """请求 owner 自己的任务取消；未开始任务直接进入终态。"""
    owner_digest = agent_job_owner_digest(owner)
    safe_type = _safe_job_type(job_type)
    safe_id = _safe_job_id(job_id) if job_id else ""
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if safe_id:
            row = _select_job(conn, owner_digest=owner_digest, job_id=safe_id)
        else:
            row = conn.execute(
                "SELECT * FROM agent_jobs WHERE owner_digest=? AND job_type=? "
                "AND status IN ('pending','running','retry_wait') "
                "ORDER BY updated_at DESC,job_id DESC LIMIT 1",
                (owner_digest, safe_type),
            ).fetchone()
        if row is None:
            return None, "not_found"
        status = str(row["status"] or "")
        if status in _TERMINAL_STATUSES:
            return row, "terminal"
        selected_id = str(row["job_id"])
        if status in {"pending", "retry_wait"}:
            conn.execute(
                "UPDATE agent_jobs SET status='cancelled',cancel_requested=1,"
                "summary='任务已取消',error_code='',finished_at=?,updated_at=? "
                "WHERE owner_digest=? AND job_id=? AND status IN ('pending','retry_wait')",
                (timestamp, timestamp, owner_digest, selected_id),
            )
            outcome = "cancelled"
        else:
            conn.execute(
                "UPDATE agent_jobs SET cancel_requested=1,summary='正在安全停止',"
                "updated_at=? WHERE owner_digest=? AND job_id=? AND status='running'",
                (timestamp, owner_digest, selected_id),
            )
            outcome = "requested"
        updated = _select_job(conn, owner_digest=owner_digest, job_id=selected_id)
    return updated, outcome


def finalize_cancelled_agent_job(
    job_id: str, *, expected_lease_generation: int
) -> bool:
    safe_id = _safe_job_id(job_id)
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_jobs SET status='cancelled',summary='任务已取消',"
            "error_code='',finished_at=?,updated_at=? WHERE job_id=? "
            "AND status='running' AND lease_generation=? AND cancel_requested=1",
            (timestamp, timestamp, safe_id, max(0, int(expected_lease_generation))),
        )
        return cur.rowcount == 1
