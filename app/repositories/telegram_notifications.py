"""统一 Telegram 主动通知仓储；schema 仅由 database.init_db 初始化和迁移。"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

_MAX_ATTEMPTS = 7
_LEASE_SECONDS = 120
_STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
AGENT_CONTINUATION_EVENT_PREFIX = "tg:event:agent:agent-continuation:"
# 发布端已有的 SHA256 identity + ':'；既有分段记录也使用同一顺序键，无需迁移。
_CONTINUATION_GROUP_LENGTH = len(AGENT_CONTINUATION_EVENT_PREFIX) + 65


def _future_stamp(delay_seconds: int, *, base_stamp: str = "") -> str:
    try:
        base = datetime.strptime((base_stamp or ""), _STAMP_FORMAT)
    except (TypeError, ValueError):
        base = datetime.now()
    return (base + timedelta(seconds=(delay_seconds or 0))).strftime(
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
    expected_confirmation_delivery: tuple[int, int] | None = None,
    expected_revision: int | None = None,
    expected_download_state: tuple[dict[str, Any], dict[str, Any] | None] | None = None,
) -> dict[str, Any] | None:
    """登记一次性事件或更新消息线程。

    ``replace=False`` 是一次性幂等事件；已有 key 不会重发。
    ``replace=True`` 会递增 revision，并在同一 Telegram 消息上更新。
    ``expected_revision=0`` 要求线程尚不存在，首次插入也遵守同一 CAS。
    """
    key = (event_key or "").strip()
    payload = (event_json or "").strip()
    if not key or not payload:
        return None
    database = db
    stamp = database.now()
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # 源回执与目标线程必须在同一写事务内校验/更新；只在移交前读取
        # 租约会留下终态已发布、旧接纳随后覆写的窗口。
        if expected_confirmation_delivery is not None and conn.execute(
            "SELECT 1 FROM organize_confirmation_delivery_outbox "
            "WHERE id=? AND lease_generation=? AND status='sending' "
            "AND ?='confirmation:'||confirmation_token",
            (*expected_confirmation_delivery, thread_key),
        ).fetchone() is None:
            return None
        row = conn.execute(
            "SELECT id,status,revision,delivered_revision,message_id,event_json,"
            "thread_key,topic,importance,chat_id FROM "
            "telegram_notification_outbox WHERE event_key=?",
            (key,),
        ).fetchone()
        if expected_revision is not None and (row["revision"] if row is not None else 0) != expected_revision:
            return None
        if expected_download_state is not None:
            request, verification = expected_download_state
            current = conn.execute(
                "SELECT * FROM download_requests WHERE id=?", (request["id"],),
            ).fetchone()
            if current is None or dict(current) != request:
                return None
            current_verification = conn.execute(
                "SELECT * FROM agent_download_verifications WHERE request_id=?", (request["id"],),
            ).fetchone()
            if verification is None:
                if current_verification is not None:
                    return None
            elif current_verification is None or any(
                current_verification[key] != value for key, value in verification.items()
            ):
                return None
        if row is None:
            conn.execute(
                "INSERT INTO telegram_notification_outbox("
                "event_key,thread_key,topic,importance,chat_id,event_json,message_id,"
                "revision,delivered_revision,status,attempts,lease_generation,"
                "next_attempt_at,last_error,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,1,0,'pending',0,0,?,'',?,?)",
                (
                    key,
                    (thread_key or ""),
                    (topic or "system"),
                    (importance or "result"),
                    (chat_id or ""),
                    payload,
                    (preferred_message_id or 0) or None,
                    stamp,
                    stamp,
                    stamp,
                ),
            )
        elif replace:
            existing_message_id = int(row["message_id"] or 0)
            message_id = existing_message_id or (preferred_message_id or 0)
            if str(row["event_json"] or "") == payload:
                status = str(row["status"] or "")
                revive = status in {"failed", "suppressed"} or (
                    status == "outcome_unknown" and message_id > 0
                )
                conn.execute(
                    "UPDATE telegram_notification_outbox SET thread_key=?,topic=?,"
                    "importance=?,chat_id=?,message_id=?,"
                    "status=CASE WHEN ? THEN 'pending' ELSE status END,"
                    "attempts=CASE WHEN ? THEN 0 ELSE attempts END,"
                    "next_attempt_at=CASE WHEN ? THEN ? ELSE next_attempt_at END,"
                    "last_error=CASE WHEN ? THEN '' ELSE last_error END,updated_at=? "
                    "WHERE event_key=?",
                    (
                        (thread_key or ""),
                        (topic or "system"),
                        (importance or "result"),
                        (chat_id or ""),
                        message_id or None,
                        int(revive),
                        int(revive),
                        int(revive),
                        stamp,
                        int(revive),
                        stamp,
                        key,
                    ),
                )
                return dict(conn.execute(
                    "SELECT * FROM telegram_notification_outbox WHERE event_key=?", (key,)
                ).fetchone())
            # SQLite 同一 UPDATE 的 CASE 读取旧列值。使用合并后的已知消息身份，
            # 让回调确认收到的候选卡安全转入编辑，而不是仍困在未知首次发送态。
            keep_unknown = str(row["status"] or "") == "outcome_unknown" and message_id <= 0
            conn.execute(
                "UPDATE telegram_notification_outbox SET thread_key=?,topic=?,importance=?,"
                "chat_id=?,event_json=?,message_id=?,revision=revision+1,"
                "status=CASE "
                "WHEN status='sending' THEN 'sending' "
                "WHEN ? "
                "THEN 'outcome_unknown' ELSE 'pending' END,"
                "attempts=CASE "
                "WHEN status='sending' OR "
                "? "
                "THEN attempts ELSE 0 END,"
                "next_attempt_at=?,"
                "last_error=CASE "
                "WHEN ? "
                "THEN last_error ELSE '' END,updated_at=? WHERE event_key=?",
                (
                    (thread_key or ""),
                    (topic or "system"),
                    (importance or "result"),
                    (chat_id or ""),
                    payload,
                    message_id or None,
                    int(keep_unknown),
                    int(keep_unknown),
                    stamp,
                    int(keep_unknown),
                    stamp,
                    key,
                ),
            )
        return dict(conn.execute(
            "SELECT * FROM telegram_notification_outbox WHERE event_key=?", (key,)
        ).fetchone())


def get_notification(event_key: str) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM telegram_notification_outbox WHERE event_key=?",
            ((event_key or ""),),
        ).fetchone()
        return dict(row) if row is not None else None


def list_recovered_organize_refresh_notifications(*, limit: int = 100) -> list[dict[str, Any]]:
    """仅选本消息全部刷新路径已交接的父通知；待恢复依据仍在原 outbox。"""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM telegram_notification_outbox AS n WHERE topic='organize' "
            "AND json_valid(event_json) "
            "AND json_array_length(event_json,'$.lifecycle.refresh.entries')>0 "
            "AND NOT EXISTS (SELECT 1 FROM strm_refresh_outbox AS r "
            "JOIN json_each(n.event_json,'$.lifecycle.refresh.entries') AS p "
            "ON r.path=json_extract(p.value,'$[0]') "
            "WHERE r.allow_emby=json_extract(p.value,'$[1]')) "
            "ORDER BY id LIMIT ?", (max(1, min(limit, 1000)),),
        ).fetchall()
    return [dict(row) for row in rows]


def invalidate_pending_download_notifications_conn(
    conn: sqlite3.Connection, request_ids: list[int], *, timestamp: str,
) -> set[int]:
    """同业务撤销事务阻止未投递的旧快照；实际发送仍只归通知中心。

    按批查询和失效，不对每个请求重新全表查询；sending/unknown
    不能认定尚未发出，因此不重置其投递身份或自动重放。
    """
    threads = {f"download:{request_id}": request_id for request_id in request_ids}
    if not threads:
        return set()
    placeholders = ",".join("?" for _ in threads)
    rows = conn.execute(
        "SELECT thread_key FROM telegram_notification_outbox WHERE topic='download' "
        f"AND thread_key IN ({placeholders})", tuple(threads),
    ).fetchall()
    conn.execute(
        "UPDATE telegram_notification_outbox SET status='suppressed',"
        "last_error='DownloadStateChanged',updated_at=? "
        "WHERE topic='download' AND status IN ('pending','retry_wait') "
        f"AND thread_key IN ({placeholders})", (timestamp, *threads),
    )
    return {threads[str(row["thread_key"])] for row in rows}


def claim_due_notifications(
    *, limit: int = 20, event_key: str = "",
) -> list[dict[str, Any]]:
    database = db
    stamp = database.now()
    claimed: list[dict[str, Any]] = []
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        stale_before = _future_stamp(-_LEASE_SECONDS, base_stamp=stamp)
        # 首次发送没有远端 message_id，租约过期只代表本地失去结果，不能
        # 据此认定 Telegram 未收到并盲目重发。先将这类记录隔离为结果未知；
        # 已知 message_id 的线程编辑仍可安全重领并原位收敛。
        conn.execute(
            "UPDATE telegram_notification_outbox SET status='outcome_unknown',"
            "lease_generation=lease_generation+1,last_error='DeliveryOutcomeUnknown',"
            "updated_at=? WHERE status='sending' AND updated_at<=? "
            "AND COALESCE(message_id,0)=0",
            (stamp, stale_before),
        )
        clauses = [
            "((status IN ('pending','retry_wait') AND next_attempt_at<=?) "
            "OR (status='sending' AND updated_at<=? "
            "AND COALESCE(message_id,0)>0))"
        ]
        # 每次领取只开放已确认送达的完整前缀；不能让部分事实缺失的确认卡先发出。
        # 关联查询按 event_key 唯一索引限定到同一回复，不阻塞其他回复/普通通知。
        group_length = _CONTINUATION_GROUP_LENGTH
        clauses.append(f"""(candidate.event_key NOT LIKE ? OR (
            SELECT COUNT(CASE WHEN prior.status='sent' THEN 1 END)
            FROM telegram_notification_outbox AS prior
            WHERE prior.event_key >= substr(candidate.event_key,1,{group_length})
              AND prior.event_key < substr(candidate.event_key,1,{group_length}) || ':'
              AND CAST(substr(prior.event_key,{group_length + 1}) AS INTEGER)
                  < CAST(substr(candidate.event_key,{group_length + 1}) AS INTEGER)
        ) = CAST(substr(candidate.event_key,{group_length + 1}) AS INTEGER))""")
        params: list[object] = [stamp, stale_before, AGENT_CONTINUATION_EVENT_PREFIX + "%"]
        if event_key:
            clauses.append("event_key=?")
            params.append(event_key)
        params.append(max(1, min((limit or 1), 100)))
        rows = conn.execute(
            "SELECT candidate.* FROM telegram_notification_outbox AS candidate WHERE "
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
    database = db
    stamp = database.now()
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT revision FROM telegram_notification_outbox WHERE id=? "
            "AND status='sending' AND lease_generation=?",
            (notification_id, lease_generation),
        ).fetchone()
        if row is None:
            return False
        latest_revision = int(row["revision"] or 0)
        status = "sent" if latest_revision == claimed_revision else "pending"
        cur = conn.execute(
            "UPDATE telegram_notification_outbox SET status=?,delivered_revision=?,"
            "message_id=COALESCE(NULLIF(?,0),message_id),attempts=0,last_error='',"
            "next_attempt_at=?,sent_at=?,updated_at=? WHERE id=? AND status='sending' "
            "AND lease_generation=?",
            (
                status,
                claimed_revision,
                (message_id or 0),
                stamp,
                stamp,
                stamp,
                notification_id,
                lease_generation,
            ),
        )
        return bool(cur.rowcount)


def retry_notification(
    notification_id: int,
    *,
    lease_generation: int,
    claimed_revision: int,
    error: str,
    retry_after_seconds: int = 0,
    clear_message_id: bool = False,
    outcome_unknown: bool = False,
    message_id: int = 0,
) -> str:
    """统一失败收敛：已知消息可幂等编辑，未知首次发送隔离，旧租约不写入。"""
    database = db
    stamp = database.now()
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT attempts,revision,message_id FROM telegram_notification_outbox WHERE id=? "
            "AND status='sending' AND lease_generation=?",
            (notification_id, lease_generation),
        ).fetchone()
        if row is None:
            return "stale"
        known_message_id = 0 if clear_message_id else int(row["message_id"] or message_id or 0)
        attempts = int(row["attempts"] or 0)
        next_attempt = stamp
        if outcome_unknown and not known_message_id:
            # 包括编辑被明确拒绝后转为新发送的未知结果；不能拿旧 ID 盲目重放。
            status = "outcome_unknown"
        elif int(row["revision"] or 0) != claimed_revision:
            status = "pending"
            attempts = 0
            error = "PriorRevisionRetry:" + (error or "DeliveryFailed")
        else:
            attempts += 1
            status = "failed" if attempts >= _MAX_ATTEMPTS else "retry_wait"
            if status == "retry_wait":
                delay = max(
                    (retry_after_seconds or 0),
                    min(3600, 15 * (2 ** min(attempts - 1, 7))),
                )
                next_attempt = _future_stamp(delay, base_stamp=stamp)
        conn.execute(
            "UPDATE telegram_notification_outbox SET status=?,attempts=?,"
            "message_id=?,last_error=?,next_attempt_at=?,updated_at=? "
            "WHERE id=? AND status='sending' AND lease_generation=?",
            (
                status,
                attempts,
                known_message_id or None,
                (error or "DeliveryFailed")[:300],
                next_attempt,
                stamp,
                notification_id,
                lease_generation,
            ),
        )
        return status


def fail_notification(
    notification_id: int,
    *,
    lease_generation: int,
    claimed_revision: int,
    error: str,
    clear_message_id: bool = False,
) -> bool:
    """记录明确拒绝；迟到 worker 不得把更新后的 revision 一并判死。"""
    database = db
    stamp = database.now()
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT revision FROM telegram_notification_outbox WHERE id=? "
            "AND status='sending' AND lease_generation=?",
            (notification_id, lease_generation),
        ).fetchone()
        if row is None:
            return False
        latest_revision = int(row["revision"] or 0)
        if latest_revision != claimed_revision:
            cur = conn.execute(
                "UPDATE telegram_notification_outbox SET status='pending',attempts=0,"
                "message_id=CASE WHEN ? THEN NULL ELSE message_id END,"
                "last_error=?,next_attempt_at=?,updated_at=? "
                "WHERE id=? AND status='sending' AND lease_generation=?",
                (
                    int(clear_message_id),
                    ("PriorRevisionRejected:" + (error or "TelegramRequestRejected"))[:300],
                    stamp,
                    stamp,
                    notification_id,
                    lease_generation,
                ),
            )
            return bool(cur.rowcount)
        cur = conn.execute(
            "UPDATE telegram_notification_outbox SET status='failed',"
            "attempts=attempts+1,message_id=CASE WHEN ? THEN NULL ELSE message_id END,"
            "last_error=?,next_attempt_at=?,updated_at=? "
            "WHERE id=? AND status='sending' AND lease_generation=?",
            (
                int(clear_message_id),
                (error or "TelegramRequestRejected")[:300],
                stamp,
                stamp,
                notification_id,
                lease_generation,
            ),
        )
        return bool(cur.rowcount)


def suppress_notification(
    notification_id: int,
    *,
    lease_generation: int,
    claimed_revision: int,
    reason: str = "NotificationPolicyDisabled",
) -> bool:
    database = db
    stamp = database.now()
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT revision FROM telegram_notification_outbox WHERE id=? "
            "AND status='sending' AND lease_generation=?",
            (notification_id, lease_generation),
        ).fetchone()
        if row is None:
            return False
        latest_revision = int(row["revision"] or 0)
        if latest_revision != claimed_revision:
            cur = conn.execute(
                "UPDATE telegram_notification_outbox SET status='pending',attempts=0,"
                "last_error=?,next_attempt_at=?,updated_at=? "
                "WHERE id=? AND status='sending' AND lease_generation=?",
                (
                    ("PriorRevisionSuppressed:" + (reason or "NotificationPolicyDisabled"))[:300],
                    stamp,
                    stamp,
                    notification_id,
                    lease_generation,
                ),
            )
            return bool(cur.rowcount)
        cur = conn.execute(
            "UPDATE telegram_notification_outbox SET status='suppressed',"
            "delivered_revision=?,last_error=?,updated_at=? "
            "WHERE id=? AND status='sending' AND lease_generation=?",
            (
                claimed_revision,
                (reason or "NotificationPolicyDisabled")[:300],
                stamp,
                notification_id,
                lease_generation,
            ),
        )
        return bool(cur.rowcount)


def _continuation_group(event_key: str) -> str:
    group = event_key[:_CONTINUATION_GROUP_LENGTH]
    digest = group[len(AGENT_CONTINUATION_EVENT_PREFIX):-1]
    if (not event_key.startswith(AGENT_CONTINUATION_EVENT_PREFIX) or len(digest) != 64
            or group[-1:] != ":" or any(c not in "0123456789abcdef" for c in digest)):
        raise ValueError("invalid Agent continuation group")
    return group


def get_agent_continuation_group(event_key: str) -> list[dict[str, Any]]:
    group = _continuation_group(event_key)
    with db.get_conn() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM telegram_notification_outbox WHERE event_key>=? AND event_key<? ORDER BY id",
            (group, group + ":"),
        )]


def bind_agent_effect_delivery(
    notification_id: int, *, lease_generation: int, claimed_revision: int,
    expected_event_json: str, event_json: str,
) -> dict[str, Any] | None:
    """旧回执只补身份；精确领取者才能原位替换仍未变化的载荷。"""
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            "UPDATE telegram_notification_outbox SET event_json=? "
            "WHERE id=? AND status='sending' AND lease_generation=? AND revision=? AND event_json=?",
            (event_json, notification_id, lease_generation, claimed_revision, expected_event_json),
        )
        if updated.rowcount != 1:
            return None
        return dict(conn.execute(
            "SELECT * FROM telegram_notification_outbox WHERE id=?", (notification_id,),
        ).fetchone())


def bind_agent_continuation_group(
    notification_id: int, *, lease_generation: int, claimed_revision: int,
    parts: Mapping[int, tuple[int, str, str]],
) -> dict[str, Any] | None:
    """旧组只补持久身份；保持事件键、发送状态、远端回执和 revision 不变。"""
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT * FROM telegram_notification_outbox WHERE id=? AND status='sending' "
            "AND lease_generation=? AND revision=?",
            (notification_id, lease_generation, claimed_revision),
        ).fetchone()
        if current is None:
            return None
        group = _continuation_group(current["event_key"])
        rows = conn.execute(
            "SELECT id,revision,event_json FROM telegram_notification_outbox WHERE event_key>=? AND event_key<?",
            (group, group + ":"),
        ).fetchall()
        if {row["id"] for row in rows} != set(parts) or any(
            (row["revision"], row["event_json"]) != parts[row["id"]][:2] for row in rows
        ):
            return None
        conn.executemany(
            "UPDATE telegram_notification_outbox SET event_json=? WHERE id=?",
            [(part[2], notification_id) for notification_id, part in parts.items()],
        )
        return dict(conn.execute(
            "SELECT * FROM telegram_notification_outbox WHERE id=?", (notification_id,),
        ).fetchone())


def suppress_agent_continuation_group(
    notification_id: int, *, lease_generation: int, claimed_revision: int, reason: str,
) -> bool:
    """精确领取者终结整组未发段；陈旧 lease/revision 不能误伤其它分段。"""
    stamp = db.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT event_key FROM telegram_notification_outbox WHERE id=? AND status='sending' "
            "AND lease_generation=? AND revision=?",
            (notification_id, lease_generation, claimed_revision),
        ).fetchone()
        if current is None:
            return False
        group = _continuation_group(current["event_key"])
        conn.execute(
            "UPDATE telegram_notification_outbox SET status='suppressed',delivered_revision=revision,"
            "last_error=?,updated_at=? WHERE id=? OR (event_key>=? AND event_key<? "
            "AND status IN ('pending','retry_wait'))",
            ((reason or "InvalidAgentContinuationDelivery")[:300], stamp, notification_id, group, group + ":"),
        )
        return True


def _recover_deliveries(conn: sqlite3.Connection, stamp: str) -> int:
    """启动和数据库重开共用恢复规则；保留预算，不重放没有回执的新发送。"""
    cur = conn.execute(
        "UPDATE telegram_notification_outbox SET status=CASE "
        "WHEN COALESCE(message_id,0)=0 THEN 'outcome_unknown' "
        "WHEN attempts>=? THEN 'failed' ELSE 'retry_wait' END,"
        "lease_generation=lease_generation+1,next_attempt_at=?,"
        "last_error=CASE WHEN COALESCE(message_id,0)>0 "
        "THEN 'ProcessInterrupted' ELSE 'DeliveryOutcomeUnknown' END,updated_at=? "
        "WHERE status='sending' OR (status='outcome_unknown' AND COALESCE(message_id,0)>0)",
        (_MAX_ATTEMPTS, stamp, stamp),
    )
    return (cur.rowcount or 0)


def recover_notifications() -> int:
    with db.get_conn() as conn:
        return _recover_deliveries(conn, db.now())


def purge_notifications(*, retention_days: int = 30, limit: int = 1000) -> int:
    """清理旧终态；尚有待投递分段的回复保留前缀回执，避免清理破坏顺序证据。"""
    database = db
    normal_cutoff = _future_stamp(-max(1, retention_days) * 86400)
    diagnostic_cutoff = _future_stamp(-max(90, retention_days) * 86400)
    group_length = _CONTINUATION_GROUP_LENGTH
    with database.get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM telegram_notification_outbox WHERE id IN ("
            "SELECT candidate.id FROM telegram_notification_outbox AS candidate WHERE "
            "((status IN ('sent','suppressed') AND updated_at<?) OR "
            "(status IN ('failed','outcome_unknown') AND updated_at<?)) "
            "AND (NOT json_valid(event_json) OR "
            "COALESCE(json_array_length(event_json,'$.lifecycle.refresh.entries'),0)=0) "
            f"""AND (candidate.event_key NOT LIKE ? OR (
                SELECT COUNT(CASE WHEN pending.status IN ('pending','retry_wait','sending') THEN 1 END)
                FROM telegram_notification_outbox AS pending
                WHERE pending.event_key >= substr(candidate.event_key,1,{group_length})
                  AND pending.event_key < substr(candidate.event_key,1,{group_length}) || ':'
            ) = 0) """
            "ORDER BY updated_at,id LIMIT ?)",
            (normal_cutoff, diagnostic_cutoff, AGENT_CONTINUATION_EVENT_PREFIX + "%",
             max(1, min(limit, 10000))),
        )
        return (cur.rowcount or 0)


def pending_notification_count() -> int:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM telegram_notification_outbox "
            "WHERE status IN ('pending','retry_wait','sending')"
        ).fetchone()
        return int(row["total"] or 0)


def _recover_after_restart(conn, timestamp: str) -> None:
    """恢复可确定更新的消息；发送结果未知的新消息不得自动重放。"""
    conn.execute(
        "UPDATE organize_confirmation_delivery_outbox "
        "SET status='retry_wait',lease_generation=lease_generation+1,"
        "next_attempt_at=?,updated_at=? WHERE status='sending'",
        (timestamp, timestamp),
    )
    _recover_deliveries(conn, timestamp)
    for table in ("agent_download_verification_notification_outbox", "agent_library_patrol_notification_outbox"):
        conn.execute(
            f"UPDATE {table} SET status='discarded',lease_generation=lease_generation+1,"
            "payload_json='',last_error_type='DeliveryOutcomeUnknown',updated_at=? "
            "WHERE status='sending'", (timestamp,),
        )


def _enqueue_confirmation_delivery(
    conn: sqlite3.Connection,
    *,
    token: str,
    event_json: str,
    chat_id: str,
    message_id: int | None,
    timestamp: str,
) -> None:
    conn.execute(
        "INSERT INTO organize_confirmation_delivery_outbox("
        "confirmation_token,event_json,chat_id,message_id,status,attempts,"
        "lease_generation,next_attempt_at,last_error,sent_at,created_at,updated_at"
        ") VALUES(?,?,?,?,'pending',0,0,?,'',NULL,?,?) "
        "ON CONFLICT(confirmation_token) DO UPDATE SET "
        "event_json=excluded.event_json,chat_id=excluded.chat_id,"
        "message_id=excluded.message_id,status='pending',attempts=0,"
        "lease_generation=organize_confirmation_delivery_outbox.lease_generation+1,"
        "next_attempt_at=excluded.next_attempt_at,last_error='',sent_at=NULL,"
        "updated_at=excluded.updated_at",
        (
            (token or ""),
            (event_json or "{}"),
            (chat_id or ""),
            message_id if message_id else None,
            timestamp,
            timestamp,
            timestamp,
        ),
    )


# 在函数定义后绑定门面，兼容 repository-first 导入；运行期始终使用同一连接/时钟所有者。
from app import database as db  # noqa: E402
