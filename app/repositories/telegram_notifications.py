"""统一 Telegram 主动通知 outbox 与可更新消息线程仓储。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType

_MAX_ATTEMPTS = 7
_LEASE_SECONDS = 120
_STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def _database() -> "ModuleType":
    from app import database

    return database


def _ensure_schema(conn) -> None:
    """兼容尚未经过进程启动初始化的 CLI、测试与独立 worker。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS telegram_notification_outbox ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,event_key TEXT NOT NULL UNIQUE,"
        "thread_key TEXT NOT NULL DEFAULT '',topic TEXT NOT NULL DEFAULT 'system',"
        "importance TEXT NOT NULL DEFAULT 'result',chat_id TEXT NOT NULL DEFAULT '',"
        "event_json TEXT NOT NULL,message_id INTEGER,"
        "revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),"
        "delivered_revision INTEGER NOT NULL DEFAULT 0 CHECK(delivered_revision >= 0),"
        "status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN "
        "('pending','sending','retry_wait','sent','failed','outcome_unknown','suppressed')),"
        "attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),"
        "lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),"
        "next_attempt_at TEXT NOT NULL,last_error TEXT NOT NULL DEFAULT '',"
        "sent_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_telegram_notification_outbox_due "
        "ON telegram_notification_outbox(status,next_attempt_at,id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_telegram_notification_outbox_thread "
        "ON telegram_notification_outbox(thread_key,chat_id)"
    )


def _future_stamp(delay_seconds: int, *, base_stamp: str = "") -> str:
    try:
        base = datetime.strptime(str(base_stamp or ""), _STAMP_FORMAT)
    except (TypeError, ValueError):
        base = datetime.now()
    return (base + timedelta(seconds=max(0, int(delay_seconds or 0)))).strftime(
        _STAMP_FORMAT
    )


def upsert_notification(
    event_key: str,
    *,
    thread_key: str = "",
    topic: str,
    importance: str,
    chat_id: str,
    event_json: str,
    preferred_message_id: int = 0,
    replace: bool = False,
) -> dict[str, Any] | None:
    """登记一次性事件或更新消息线程。

    ``replace=False`` 是一次性幂等事件；已有 key 不会重发。
    ``replace=True`` 会递增 revision，并在同一 Telegram 消息上更新。
    """
    key = str(event_key or "").strip()
    payload = str(event_json or "").strip()
    if not key or not payload:
        return None
    database = _database()
    stamp = database.now()
    with database.get_conn() as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id,status,revision,delivered_revision,message_id,event_json FROM "
            "telegram_notification_outbox WHERE event_key=?",
            (key,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO telegram_notification_outbox("
                "event_key,thread_key,topic,importance,chat_id,event_json,message_id,"
                "revision,delivered_revision,status,attempts,lease_generation,"
                "next_attempt_at,last_error,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,1,0,'pending',0,0,?,'',?,?)",
                (
                    key,
                    str(thread_key or ""),
                    str(topic or "system"),
                    str(importance or "result"),
                    str(chat_id or ""),
                    payload,
                    int(preferred_message_id or 0) or None,
                    stamp,
                    stamp,
                    stamp,
                ),
            )
        elif replace:
            existing_message_id = int(row["message_id"] or 0)
            message_id = existing_message_id or int(preferred_message_id or 0)
            if str(row["event_json"] or "") == payload:
                if not existing_message_id and message_id:
                    conn.execute(
                        "UPDATE telegram_notification_outbox SET message_id=?,updated_at=? "
                        "WHERE event_key=?",
                        (message_id, stamp, key),
                    )
                return dict(conn.execute(
                    "SELECT * FROM telegram_notification_outbox WHERE event_key=?", (key,)
                ).fetchone())
            conn.execute(
                "UPDATE telegram_notification_outbox SET thread_key=?,topic=?,importance=?,"
                "chat_id=?,event_json=?,message_id=?,revision=revision+1,"
                "status=CASE WHEN status='sending' THEN 'sending' ELSE 'pending' END,"
                "attempts=CASE WHEN status='sending' THEN attempts ELSE 0 END,"
                "next_attempt_at=?,last_error='',updated_at=? WHERE event_key=?",
                (
                    str(thread_key or ""),
                    str(topic or "system"),
                    str(importance or "result"),
                    str(chat_id or ""),
                    payload,
                    message_id or None,
                    stamp,
                    stamp,
                    key,
                ),
            )
        return dict(conn.execute(
            "SELECT * FROM telegram_notification_outbox WHERE event_key=?", (key,)
        ).fetchone())


def get_notification(event_key: str) -> dict[str, Any] | None:
    with _database().get_conn() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM telegram_notification_outbox WHERE event_key=?",
            (str(event_key or ""),),
        ).fetchone()
        return dict(row) if row is not None else None


def claim_due_notifications(
    *, limit: int = 20, event_key: str = "",
) -> list[dict[str, Any]]:
    database = _database()
    stamp = database.now()
    claimed: list[dict[str, Any]] = []
    with database.get_conn() as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        stale_before = _future_stamp(-_LEASE_SECONDS, base_stamp=stamp)
        clauses = [
            "((status IN ('pending','retry_wait') AND next_attempt_at<=?) "
            "OR (status='sending' AND updated_at<=?))"
        ]
        params: list[object] = [stamp, stale_before]
        if event_key:
            clauses.append("event_key=?")
            params.append(str(event_key))
        params.append(max(1, min(int(limit or 1), 100)))
        rows = conn.execute(
            "SELECT * FROM telegram_notification_outbox WHERE "
            + " AND ".join(clauses)
            + " ORDER BY next_attempt_at,id LIMIT ?",
            tuple(params),
        ).fetchall()
        for row in rows:
            generation = int(row["lease_generation"] or 0)
            updated = conn.execute(
                "UPDATE telegram_notification_outbox SET status='sending',"
                "lease_generation=lease_generation+1,updated_at=? "
                "WHERE id=? AND status=? AND lease_generation=?",
                (stamp, int(row["id"]), str(row["status"]), generation),
            )
            if updated.rowcount != 1:
                continue
            item = dict(row)
            item["lease_generation"] = generation + 1
            claimed.append(item)
    return claimed


def complete_notification(
    notification_id: int,
    *,
    lease_generation: int,
    claimed_revision: int,
    message_id: int = 0,
) -> bool:
    database = _database()
    stamp = database.now()
    with database.get_conn() as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT revision FROM telegram_notification_outbox WHERE id=? "
            "AND status='sending' AND lease_generation=?",
            (int(notification_id), int(lease_generation)),
        ).fetchone()
        if row is None:
            return False
        latest_revision = int(row["revision"] or 0)
        status = "sent" if latest_revision == int(claimed_revision) else "pending"
        cur = conn.execute(
            "UPDATE telegram_notification_outbox SET status=?,delivered_revision=?,"
            "message_id=COALESCE(NULLIF(?,0),message_id),attempts=0,last_error='',"
            "next_attempt_at=?,sent_at=?,updated_at=? WHERE id=? AND status='sending' "
            "AND lease_generation=?",
            (
                status,
                int(claimed_revision),
                int(message_id or 0),
                stamp,
                stamp,
                stamp,
                int(notification_id),
                int(lease_generation),
            ),
        )
        return bool(cur.rowcount)


def retry_notification(
    notification_id: int,
    *,
    lease_generation: int,
    error: str,
    retry_after_seconds: int = 0,
) -> str:
    database = _database()
    stamp = database.now()
    with database.get_conn() as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT attempts FROM telegram_notification_outbox WHERE id=? "
            "AND status='sending' AND lease_generation=?",
            (int(notification_id), int(lease_generation)),
        ).fetchone()
        if row is None:
            return "stale"
        attempts = int(row["attempts"] or 0) + 1
        if attempts >= _MAX_ATTEMPTS:
            status = "failed"
            next_attempt = stamp
        else:
            status = "retry_wait"
            delay = max(
                int(retry_after_seconds or 0),
                min(3600, 15 * (2 ** min(attempts - 1, 7))),
            )
            next_attempt = _future_stamp(delay, base_stamp=stamp)
        conn.execute(
            "UPDATE telegram_notification_outbox SET status=?,attempts=?,last_error=?,"
            "next_attempt_at=?,updated_at=? WHERE id=? AND status='sending' "
            "AND lease_generation=?",
            (
                status,
                attempts,
                str(error or "DeliveryFailed")[:300],
                next_attempt,
                stamp,
                int(notification_id),
                int(lease_generation),
            ),
        )
        return status


def suppress_notification(
    notification_id: int,
    *,
    lease_generation: int,
    reason: str = "NotificationPolicyDisabled",
) -> bool:
    database = _database()
    stamp = database.now()
    with database.get_conn() as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            "UPDATE telegram_notification_outbox SET status='suppressed',"
            "delivered_revision=revision,last_error=?,updated_at=? "
            "WHERE id=? AND status='sending' AND lease_generation=?",
            (str(reason or "NotificationPolicyDisabled")[:300], stamp,
             int(notification_id), int(lease_generation)),
        )
        return bool(cur.rowcount)


def mark_outcome_unknown(
    notification_id: int,
    *,
    lease_generation: int,
    error: str,
    message_id: int = 0,
) -> bool:
    database = _database()
    stamp = database.now()
    with database.get_conn() as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            "UPDATE telegram_notification_outbox SET status='outcome_unknown',"
            "message_id=COALESCE(NULLIF(?,0),message_id),last_error=?,updated_at=? "
            "WHERE id=? AND status='sending' AND lease_generation=?",
            (
                int(message_id or 0),
                str(error or "OutcomeUnknown")[:300],
                stamp,
                int(notification_id),
                int(lease_generation),
            ),
        )
        return bool(cur.rowcount)


def recover_notifications() -> int:
    database = _database()
    stamp = database.now()
    with database.get_conn() as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            "UPDATE telegram_notification_outbox SET status='retry_wait',"
            "lease_generation=lease_generation+1,next_attempt_at=?,updated_at=? "
            "WHERE status='sending'",
            (stamp, stamp),
        )
        return int(cur.rowcount or 0)


def purge_notifications(*, retention_days: int = 30, limit: int = 1000) -> int:
    """清理已送达/已抑制的旧事件；失败和结果未知保留更久便于核对。"""
    database = _database()
    normal_cutoff = _future_stamp(-max(1, int(retention_days)) * 86400)
    diagnostic_cutoff = _future_stamp(-max(90, int(retention_days)) * 86400)
    with database.get_conn() as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            "DELETE FROM telegram_notification_outbox WHERE id IN ("
            "SELECT id FROM telegram_notification_outbox WHERE "
            "(status IN ('sent','suppressed') AND updated_at<?) OR "
            "(status IN ('failed','outcome_unknown') AND updated_at<?) "
            "ORDER BY updated_at,id LIMIT ?)",
            (normal_cutoff, diagnostic_cutoff, max(1, min(int(limit), 10000))),
        )
        return int(cur.rowcount or 0)


def pending_notification_count() -> int:
    with _database().get_conn() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM telegram_notification_outbox "
            "WHERE status IN ('pending','retry_wait','sending')"
        ).fetchone()
        return int(row["total"] or 0)
