"""整理通知的持久化投递队列。

Telegram 临时网络错误不应丢失整理结果通知，也不能让重试变成重复消息。
每条通知拥有稳定幂等键：已成功的事件不会再次发送，未成功的事件在进程
重启后仍可恢复重试。带内联按钮的待确认卡由确认投递队列负责，不在此处。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType

MAX_ATTEMPTS = 6
BASE_BACKOFF_SECONDS = 30
_STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def _database() -> "ModuleType":
    from app import database

    return database


def _future_stamp(delay_seconds: int, *, base_stamp: str = "") -> str:
    """基于数据库时钟计算下次时间，避免本地时间源与 SQLite 记录漂移。"""
    try:
        base = datetime.strptime(str(base_stamp or ""), _STAMP_FORMAT)
    except (TypeError, ValueError):
        base = datetime.now()
    return (
        base + timedelta(seconds=max(0, int(delay_seconds or 0)))
    ).strftime(_STAMP_FORMAT)


def enqueue_organize_notification(
    idempotency_key: str, body: str, *, chat_id: str = "",
) -> bool:
    """登记一条待投递通知；同一幂等键已存在时不重复入队。"""
    key = str(idempotency_key or "").strip()
    text = str(body or "")
    if not key or not text:
        return False
    database = _database()
    stamp = database.now()
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO organize_notification_outbox("
            "idempotency_key,chat_id,body,status,next_attempt_at,created_at,updated_at)"
            " VALUES(?,?,?,'pending',?,?,?) ON CONFLICT(idempotency_key) DO NOTHING",
            (key, str(chat_id or ""), text, stamp, stamp, stamp),
        )
        return bool(cur.rowcount)


def organize_notification_state(idempotency_key: str) -> str:
    """返回该幂等键当前状态；不存在时返回空串。"""
    key = str(idempotency_key or "").strip()
    if not key:
        return ""
    with _database().get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM organize_notification_outbox WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        return str(row["status"]) if row else ""


def claim_due_organize_notifications(*, limit: int = 20) -> list[dict[str, Any]]:
    """原子领取到期通知，并递增租约代数阻止迟到 worker 覆写新结果。"""
    database = _database()
    stamp = database.now()
    claimed: list[dict[str, Any]] = []
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id,idempotency_key,chat_id,body,attempts,status,lease_generation "
            "FROM organize_notification_outbox "
            "WHERE status IN ('pending','retry_wait') AND next_attempt_at<=? "
            "ORDER BY next_attempt_at, id LIMIT ?",
            (stamp, max(1, int(limit or 1))),
        ).fetchall()
        for row in rows:
            old_generation = int(row["lease_generation"] or 0)
            updated = conn.execute(
                "UPDATE organize_notification_outbox SET status='sending',"
                "lease_generation=lease_generation+1,updated_at=? "
                "WHERE id=? AND status=? AND lease_generation=?",
                (stamp, int(row["id"]), str(row["status"]), old_generation),
            )
            if updated.rowcount != 1:
                continue
            claimed.append({
                "id": int(row["id"]),
                "idempotency_key": str(row["idempotency_key"]),
                "chat_id": str(row["chat_id"] or ""),
                "body": str(row["body"] or ""),
                "attempts": int(row["attempts"] or 0),
                "lease_generation": old_generation + 1,
            })
    return claimed


def mark_organize_notification_sent(
    notification_id: int, *, expected_lease_generation: int,
) -> bool:
    database = _database()
    stamp = database.now()
    with database.get_conn() as conn:
        cur = conn.execute(
            "UPDATE organize_notification_outbox SET status='sent',sent_at=?,"
            "last_error='',updated_at=? WHERE id=? AND status='sending' "
            "AND lease_generation=?",
            (
                stamp, stamp, int(notification_id),
                int(expected_lease_generation),
            ),
        )
        return bool(cur.rowcount)


def retry_organize_notification(
    notification_id: int,
    *,
    expected_lease_generation: int,
    error: str = "",
    retry_after_seconds: int = 0,
) -> str:
    """失败后按指数退避重排；Telegram retry_after 可延长等待窗口。"""
    database = _database()
    stamp = database.now()
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT attempts,status,lease_generation FROM organize_notification_outbox "
            "WHERE id=?",
            (int(notification_id),),
        ).fetchone()
        if row is None:
            return ""
        if (
            str(row["status"] or "") != "sending"
            or int(row["lease_generation"] or 0) != int(expected_lease_generation)
        ):
            return "stale"
        attempts = int(row["attempts"] or 0) + 1
        exhausted = attempts >= MAX_ATTEMPTS
        status = "failed" if exhausted else "retry_wait"
        exponential_delay = BASE_BACKOFF_SECONDS * (2 ** min(attempts - 1, 5))
        delay = 0 if exhausted else max(
            exponential_delay, max(0, int(retry_after_seconds or 0)),
        )
        updated = conn.execute(
            "UPDATE organize_notification_outbox SET status=?,attempts=?,last_error=?,"
            "next_attempt_at=?,updated_at=? WHERE id=? AND status='sending' "
            "AND lease_generation=?",
            (
                status, attempts, str(error or "")[:300],
                _future_stamp(delay, base_stamp=stamp), stamp, int(notification_id),
                int(expected_lease_generation),
            ),
        )
        return status if updated.rowcount == 1 else "stale"


def recover_stale_organize_notifications() -> int:
    """启动时回收 sending 租约并递增代数，使旧 worker 的回写失效。"""
    database = _database()
    stamp = database.now()
    with database.get_conn() as conn:
        cur = conn.execute(
            "UPDATE organize_notification_outbox SET status='retry_wait',"
            "lease_generation=lease_generation+1,next_attempt_at=?,updated_at=? "
            "WHERE status='sending'",
            (stamp, stamp),
        )
        return int(cur.rowcount or 0)


def count_pending_organize_notifications() -> int:
    with _database().get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM organize_notification_outbox "
            "WHERE status IN ('pending','sending','retry_wait')"
        ).fetchone()
        return int(row["total"] or 0) if row else 0


def list_organize_notifications(*, limit: int = 50) -> list[sqlite3.Row]:
    with _database().get_conn() as conn:
        return conn.execute(
            "SELECT * FROM organize_notification_outbox "
            "ORDER BY updated_at DESC, id DESC LIMIT ?",
            (max(1, int(limit or 1)),),
        ).fetchall()
