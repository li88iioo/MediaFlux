"""Agent 下载复核与通知发件箱的数据访问。"""
from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType


def _database() -> "ModuleType":
    """延迟取得数据库门面，保持测试数据库与连接/时间补丁兼容。"""
    from app import database

    return database


def get_conn():
    return _database().get_conn()


def now() -> str:
    return _database().now()
def enqueue_agent_download_verification(
    request_id: int,
    *,
    title: str,
    tmdb_id: str,
    season: int,
    episode: int,
    as_of: str,
    library_name: str = "",
    owner: str = "",
    chat_id: str = "",
) -> bool:
    """为已确认的缺集下载创建唯一、可恢复的自动复核任务。"""
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO agent_download_verifications("
            "request_id,title,tmdb_id,season,episode,as_of,library_name,owner,chat_id,status,result,attempts,"
            "next_check_at,last_checked_at,created_at,updated_at) "
            "SELECT ?,?,?,?,?,?,?,?,?,'pending','',0,?,NULL,?,? "
            "WHERE EXISTS(SELECT 1 FROM download_requests "
            "WHERE id=? AND (origin='agent' OR origin LIKE 'agent:%') "
            "AND targets IN ('qb','guangya','both') "
            "AND status IN ('submitted','downloading','completed','failed'))",
            (
                int(request_id), str(title), str(tmdb_id), int(season), int(episode),
                str(as_of), str(library_name or "")[:80], str(owner or "")[:512],
                str(chat_id or "")[:32],
                timestamp, timestamp, timestamp, int(request_id),
            ),
        )
        return cur.rowcount == 1


def get_agent_download_verification(request_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT request_id,title,tmdb_id,season,episode,as_of,library_name,owner,chat_id,status,result,attempts,"
            "lease_generation,next_check_at,last_checked_at,created_at,updated_at "
            "FROM agent_download_verifications WHERE request_id=?",
            (int(request_id),),
        ).fetchone()


def claim_due_agent_download_verification(
    *,
    current_time: str | None = None,
    stale_before: str | None = None,
) -> sqlite3.Row | None:
    """原子领取一个到期复核任务，避免并发 worker 重复审计。"""
    timestamp = str(current_time or now())
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if stale_before:
            conn.execute(
                "UPDATE agent_download_verifications SET status='retry_wait',"
                "next_check_at=?,updated_at=? "
                "WHERE status='running' AND updated_at<=?",
                (timestamp, timestamp, str(stale_before)),
            )
        row = conn.execute(
            "SELECT request_id FROM agent_download_verifications "
            "WHERE status IN ('pending','retry_wait') AND next_check_at<=? "
            "ORDER BY next_check_at ASC,request_id ASC LIMIT 1",
            (timestamp,),
        ).fetchone()
        if row is None:
            return None
        request_id = int(row["request_id"])
        cur = conn.execute(
            "UPDATE agent_download_verifications SET status='running',"
            "lease_generation=lease_generation+1,updated_at=? "
            "WHERE request_id=? AND status IN ('pending','retry_wait') AND next_check_at<=?",
            (timestamp, request_id, timestamp),
        )
        if cur.rowcount != 1:
            return None
        return conn.execute(
            "SELECT request_id,title,tmdb_id,season,episode,as_of,library_name,owner,chat_id,status,result,attempts,"
            "lease_generation,next_check_at,last_checked_at,created_at,updated_at "
            "FROM agent_download_verifications WHERE request_id=?",
            (request_id,),
        ).fetchone()


def renew_agent_download_verification_lease(
    request_id: int,
    *,
    expected_lease_generation: int,
    renewed_at: str | None = None,
) -> bool:
    """续期正在运行的复核任务；租约变化后旧 worker 不得继续持有。"""
    timestamp = str(renewed_at or now())
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_download_verifications SET updated_at=? "
            "WHERE request_id=? AND status='running' AND lease_generation=?",
            (
                timestamp,
                int(request_id),
                max(0, int(expected_lease_generation)),
            ),
        )
        return cur.rowcount == 1


def update_agent_download_verification(
    request_id: int,
    *,
    status: str,
    result: str = "",
    attempts: int,
    next_check_at: str,
    expected_lease_generation: int,
    last_checked_at: str | None = None,
) -> bool:
    """更新自动复核状态；持有租约时使用 generation 做条件写入。"""
    if status not in {"pending", "running", "retry_wait", "visible", "attention"}:
        raise ValueError("自动复核状态无效")
    if result not in {"", "visible", "missing", "inconclusive"}:
        raise ValueError("自动复核结果无效")
    safe_attempts = max(0, min(int(attempts), 100))
    values: list[object] = [
        status, status, result, safe_attempts, str(next_check_at or now()),
        last_checked_at, now(), int(request_id),
        max(0, int(expected_lease_generation)),
    ]
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_download_verifications SET status=?,"
            "title=CASE WHEN ? IN ('visible','attention') THEN '' ELSE title END,"
            "result=?,attempts=?,next_check_at=?,last_checked_at=?,updated_at=? "
            "WHERE request_id=? AND lease_generation=? AND status='running'",
            tuple(values),
        )
        return cur.rowcount == 1


def finish_agent_download_verification(
    request_id: int,
    *,
    status: str,
    result: str,
    attempts: int,
    next_check_at: str,
    expected_lease_generation: int,
    payload_json: str,
    last_checked_at: str | None = None,
) -> bool:
    """原子写入复核终态并建立唯一通知，避免终态与通知分裂。"""
    if status not in {"visible", "attention"}:
        raise ValueError("自动复核终态无效")
    if result not in {"", "visible", "missing", "inconclusive"}:
        raise ValueError("自动复核结果无效")
    payload = str(payload_json or "").strip()
    if not payload or len(payload) > 4096:
        raise ValueError("自动复核通知载荷无效")
    timestamp = now()
    due_at = str(next_check_at or timestamp)
    safe_attempts = max(0, min(int(attempts), 100))
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE agent_download_verifications SET status=?,title='',result=?,attempts=?,"
            "next_check_at=?,last_checked_at=?,updated_at=? "
            "WHERE request_id=? AND lease_generation=? AND status='running'",
            (
                status, result, safe_attempts, due_at,
                last_checked_at, timestamp, int(request_id),
                max(0, int(expected_lease_generation)),
            ),
        )
        if cur.rowcount != 1:
            return False
        conn.execute(
            "INSERT OR IGNORE INTO agent_download_verification_notification_outbox("
            "request_id,owner,chat_id,payload_json,status,attempts,lease_generation,"
            "next_attempt_at,last_error_type,sent_at,created_at,updated_at) "
            "SELECT request_id,owner,chat_id,?,'pending',0,0,?,'',NULL,?,? "
            "FROM agent_download_verifications WHERE request_id=?",
            (payload, due_at, timestamp, timestamp, int(request_id)),
        )
        return True


def list_agent_download_verification_notifications() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM agent_download_verification_notification_outbox ORDER BY id"
        ).fetchall()


def claim_due_agent_download_verification_notification(
    *,
    current_time: str | None = None,
    stale_before: str | None = None,
) -> sqlite3.Row | None:
    """原子领取一条下载核验通知；generation 隔离过期发送者。"""
    timestamp = str(current_time or now())
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if stale_before:
            conn.execute(
                "UPDATE agent_download_verification_notification_outbox "
                "SET status='retry_wait',lease_generation=lease_generation+1,"
                "next_attempt_at=?,updated_at=? "
                "WHERE status='sending' AND updated_at<=?",
                (timestamp, timestamp, str(stale_before)),
            )
        row = conn.execute(
            "SELECT id FROM agent_download_verification_notification_outbox "
            "WHERE status IN ('pending','retry_wait') AND next_attempt_at<=? "
            "ORDER BY next_attempt_at,id LIMIT 1",
            (timestamp,),
        ).fetchone()
        if row is None:
            return None
        notification_id = int(row["id"])
        cur = conn.execute(
            "UPDATE agent_download_verification_notification_outbox "
            "SET status='sending',lease_generation=lease_generation+1,updated_at=? "
            "WHERE id=? AND status IN ('pending','retry_wait')",
            (timestamp, notification_id),
        )
        if cur.rowcount != 1:
            return None
        return conn.execute(
            "SELECT * FROM agent_download_verification_notification_outbox WHERE id=?",
            (notification_id,),
        ).fetchone()


def complete_agent_download_verification_notification(
    notification_id: int,
    *,
    expected_lease_generation: int,
    sent_at: str | None = None,
) -> bool:
    timestamp = str(sent_at or now())
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_download_verification_notification_outbox "
            "SET status='sent',payload_json='',sent_at=?,last_error_type='',updated_at=? "
            "WHERE id=? AND status='sending' AND lease_generation=?",
            (
                timestamp, timestamp, int(notification_id),
                max(0, int(expected_lease_generation)),
            ),
        )
        return cur.rowcount == 1


def retry_agent_download_verification_notification(
    notification_id: int,
    *,
    expected_lease_generation: int,
    next_attempt_at: str,
    error_type: str = "",
) -> bool:
    safe_error_type = re.sub(r"[^A-Za-z0-9_.-]", "", str(error_type or ""))[:80]
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_download_verification_notification_outbox "
            "SET status='retry_wait',attempts=MIN(attempts+1,100),"
            "next_attempt_at=?,last_error_type=?,updated_at=? "
            "WHERE id=? AND status='sending' AND lease_generation=?",
            (
                str(next_attempt_at or timestamp), safe_error_type, timestamp,
                int(notification_id), max(0, int(expected_lease_generation)),
            ),
        )
        return cur.rowcount == 1


def discard_agent_download_verification_notification(
    notification_id: int,
    *,
    expected_lease_generation: int,
    error_type: str = "",
) -> bool:
    safe_error_type = re.sub(r"[^A-Za-z0-9_.-]", "", str(error_type or ""))[:80]
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_download_verification_notification_outbox "
            "SET status='discarded',payload_json='',last_error_type=?,updated_at=? "
            "WHERE id=? AND status='sending' AND lease_generation=?",
            (
                safe_error_type, timestamp, int(notification_id),
                max(0, int(expected_lease_generation)),
            ),
        )
        return cur.rowcount == 1


def discard_agent_download_verification_notifications() -> int:
    """通知关闭时丢弃未发送积压，并使正在发送的旧租约失效。"""
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_download_verification_notification_outbox "
            "SET status='discarded',payload_json='',"
            "lease_generation=lease_generation+1,updated_at=? "
            "WHERE status IN ('pending','retry_wait','sending')",
            (timestamp,),
        )
        return max(0, int(cur.rowcount))
