"""整理后媒体规格补全队列的数据访问。"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType


def _database() -> "ModuleType":
    from app import database

    return database


def _future_stamp(seconds: int) -> str:
    return (datetime.now() + timedelta(seconds=max(0, int(seconds)))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _safe_error(value: object, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text[: max(40, int(limit))]


def enqueue_organize_probe_completion(
    organize_log_id: int,
    *,
    source_id: str,
    rel_dir: str,
    rules: dict,
    delay_seconds: int = 130,
    max_attempts: int = 2,
) -> int:
    """登记整理成功但在线探测未完成的文件；同一日志幂等。"""
    database = _database()
    stamp = database.now()
    next_attempt = _future_stamp(max(30, int(delay_seconds)))
    payload = json.dumps(rules if isinstance(rules, dict) else {}, ensure_ascii=False)
    with database.get_conn() as conn:
        conn.execute(
            "INSERT INTO organize_probe_queue(organize_log_id,provider,source_id,rel_dir,"
            "rules_json,status,attempts,max_attempts,next_attempt_at,lease_owner,lease_until,"
            "last_error_type,last_error,created_at,updated_at) "
            "VALUES(?, 'guangya', ?, ?, ?, 'queued', 0, ?, ?, '', 0, '', '', ?, ?) "
            "ON CONFLICT(organize_log_id) DO UPDATE SET source_id=excluded.source_id,"
            "rel_dir=excluded.rel_dir,rules_json=excluded.rules_json,"
            "max_attempts=excluded.max_attempts,updated_at=excluded.updated_at",
            (
                int(organize_log_id), str(source_id or ""), str(rel_dir or ""), payload,
                max(1, min(int(max_attempts or 2), 5)), next_attempt, stamp, stamp,
            ),
        )
        row = conn.execute(
            "SELECT id FROM organize_probe_queue WHERE organize_log_id=?",
            (int(organize_log_id),),
        ).fetchone()
        return int(row["id"]) if row else 0


def claim_due_organize_probe_jobs(
    *, owner: str, lease_seconds: int = 900, limit: int = 1,
) -> list[dict]:
    database = _database()
    stamp = database.now()
    lease_until = time.time() + max(30, int(lease_seconds or 900))
    safe_limit = max(1, min(int(limit or 1), 20))
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id FROM organize_probe_queue "
            "WHERE status IN ('queued','retry_wait') AND next_attempt_at<=? "
            "ORDER BY next_attempt_at,id LIMIT ?",
            (stamp, safe_limit),
        ).fetchall()
        claimed: list[dict] = []
        for row in rows:
            cur = conn.execute(
                "UPDATE organize_probe_queue SET status='running',lease_owner=?,lease_until=?,"
                "updated_at=? WHERE id=? AND status IN ('queued','retry_wait')",
                (str(owner or ""), lease_until, stamp, int(row["id"])),
            )
            if cur.rowcount != 1:
                continue
            current = conn.execute(
                "SELECT * FROM organize_probe_queue WHERE id=?", (int(row["id"]),)
            ).fetchone()
            if current:
                claimed.append(dict(current))
        return claimed


def complete_organize_probe_job(job_id: int, *, owner: str) -> bool:
    database = _database()
    stamp = database.now()
    with database.get_conn() as conn:
        cur = conn.execute(
            "UPDATE organize_probe_queue SET status='completed',lease_owner='',lease_until=0,"
            "last_error_type='',last_error='',completed_at=?,updated_at=? "
            "WHERE id=? AND status='running' AND lease_owner=?",
            (stamp, stamp, int(job_id), str(owner or "")),
        )
        return cur.rowcount == 1


def cancel_organize_probe_job(
    job_id: int, *, owner: str = "", reason: object = "",
) -> bool:
    database = _database()
    stamp = database.now()
    sql = (
        "UPDATE organize_probe_queue SET status='cancelled',lease_owner='',lease_until=0,"
        "last_error_type='Cancelled',last_error=?,completed_at=?,updated_at=? WHERE id=?"
    )
    params: list[object] = [_safe_error(reason), stamp, stamp, int(job_id)]
    if owner:
        sql += " AND status='running' AND lease_owner=?"
        params.append(str(owner))
    with database.get_conn() as conn:
        cur = conn.execute(sql, params)
        return cur.rowcount == 1


def release_organize_probe_job(
    job_id: int, *, owner: str, delay_seconds: int = 30, reason: object = "",
) -> bool:
    database = _database()
    stamp = database.now()
    with database.get_conn() as conn:
        cur = conn.execute(
            "UPDATE organize_probe_queue SET status='retry_wait',next_attempt_at=?,"
            "lease_owner='',lease_until=0,last_error_type='Busy',last_error=?,updated_at=? "
            "WHERE id=? AND status='running' AND lease_owner=?",
            (
                _future_stamp(max(5, int(delay_seconds))), _safe_error(reason), stamp,
                int(job_id), str(owner or ""),
            ),
        )
        return cur.rowcount == 1


def fail_or_retry_organize_probe_job(
    job_id: int,
    *,
    owner: str,
    error_type: str,
    error: object,
    base_backoff_seconds: int = 600,
) -> str:
    database = _database()
    stamp = database.now()
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT attempts,max_attempts FROM organize_probe_queue "
            "WHERE id=? AND status='running' AND lease_owner=?",
            (int(job_id), str(owner or "")),
        ).fetchone()
        if not row:
            return "stale"
        attempts = int(row["attempts"] or 0) + 1
        exhausted = attempts >= max(1, int(row["max_attempts"] or 1))
        status = "failed" if exhausted else "retry_wait"
        delay = max(30, int(base_backoff_seconds or 600)) * (2 ** max(0, attempts - 1))
        next_attempt = stamp if exhausted else _future_stamp(min(delay, 3600))
        cur = conn.execute(
            "UPDATE organize_probe_queue SET status=?,attempts=?,next_attempt_at=?,"
            "lease_owner='',lease_until=0,last_error_type=?,last_error=?,"
            "completed_at=CASE WHEN ?='failed' THEN ? ELSE completed_at END,updated_at=? "
            "WHERE id=? AND status='running' AND lease_owner=?",
            (
                status, attempts, next_attempt, str(error_type or "Error")[:100],
                _safe_error(error), status, stamp, stamp, int(job_id), str(owner or ""),
            ),
        )
        return status if cur.rowcount == 1 else "stale"


def recover_stale_organize_probe_jobs(*, force: bool = False) -> int:
    database = _database()
    stamp = database.now()
    where = "status='running'" if force else "status='running' AND lease_until<=?"
    params: list[object] = [] if force else [time.time()]
    with database.get_conn() as conn:
        cur = conn.execute(
            "UPDATE organize_probe_queue SET status='retry_wait',next_attempt_at=?,"
            "lease_owner='',lease_until=0,last_error_type='ProcessInterrupted',"
            "last_error='上次进程在媒体规格补全期间中断',updated_at=? WHERE " + where,
            (stamp, stamp, *params),
        )
        return int(cur.rowcount or 0)


def count_organize_probe_jobs() -> dict[str, int]:
    database = _database()
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT status,COUNT(*) AS count FROM organize_probe_queue GROUP BY status"
        ).fetchall()
    counts = {str(row["status"]): int(row["count"] or 0) for row in rows}
    counts["pending"] = sum(counts.get(key, 0) for key in ("queued", "retry_wait", "running"))
    return counts


def commit_organize_probe_rename(
    organize_log_id: int,
    *,
    current_name: str,
    new_path: str,
    item_updates: list[dict],
) -> bool:
    """在单一事务中提交后台补全后的日志与成员快照。"""
    database = _database()
    stamp = database.now()
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status,legacy_incomplete FROM organize_log WHERE id=?",
            (int(organize_log_id),),
        ).fetchone()
        if (
            row is None or str(row["status"] or "") != "success"
            or bool(row["legacy_incomplete"])
        ):
            return False
        for item in item_updates:
            cur = conn.execute(
                "UPDATE organize_log_items SET current_name=?,target_name=?,status='success',"
                "error='',updated_at=? WHERE id=? AND log_id=? AND current_name=?",
                (
                    str(item.get("current_name") or ""),
                    str(item.get("target_name") or item.get("current_name") or ""),
                    stamp, int(item.get("id") or 0), int(organize_log_id),
                    str(item.get("expected_name") or ""),
                ),
            )
            if cur.rowcount != 1:
                raise RuntimeError("整理成员快照已变化，拒绝提交媒体规格补全")
        cur = conn.execute(
            "UPDATE organize_log SET current_name=?,new_path=?,error='',version=version+1,"
            "updated_at=? WHERE id=? AND status='success' AND legacy_incomplete=0",
            (str(current_name or ""), str(new_path or ""), stamp, int(organize_log_id)),
        )
        if cur.rowcount != 1:
            raise RuntimeError("整理日志快照已变化，拒绝提交媒体规格补全")
        return True
