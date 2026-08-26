"""Agent 媒体库巡检与通知发件箱的数据访问。"""
from __future__ import annotations

import json
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


def ensure_agent_library_patrol(*, next_run_at: str | None = None) -> sqlite3.Row:
    """幂等创建全库缺集巡检单例。"""
    timestamp = now()
    due_at = str(next_run_at or timestamp)
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agent_library_patrol("
            "patrol_key,status,outcome,attempts,lease_generation,next_run_at,"
            "projection_json,findings_truncated,error_type,created_at,updated_at) "
            "VALUES('default','pending','',0,0,?,'{}',0,'',?,?)",
            (due_at, timestamp, timestamp),
        )
        row = conn.execute(
            "SELECT * FROM agent_library_patrol WHERE patrol_key='default'"
        ).fetchone()
    assert row is not None
    return row


def reschedule_agent_library_patrol(*, next_run_at: str | None = None) -> bool:
    """将非运行中的全库巡检重新排到指定时间，用于配置热加载。"""
    timestamp = now()
    due_at = str(next_run_at or timestamp)
    ensure_agent_library_patrol(next_run_at=due_at)
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_library_patrol SET next_run_at=?,updated_at=? "
            "WHERE patrol_key='default' AND status IN ('pending','retry_wait')",
            (due_at, timestamp),
        )
        return cur.rowcount == 1


def get_agent_library_patrol() -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM agent_library_patrol WHERE patrol_key='default'"
        ).fetchone()


def cancel_agent_library_patrol_lease(
    *,
    next_run_at: str | None = None,
    expected_lease_generation: int | None = None,
) -> bool:
    """禁用巡检时释放运行租约，并递增 generation 使旧 worker 写入失效。"""
    timestamp = now()
    due_at = str(next_run_at or timestamp)
    sql = (
        "UPDATE agent_library_patrol SET status='pending',"
        "lease_generation=lease_generation+1,next_run_at=?,cycle_as_of='',"
        "cycle_cursor_tmdb_id='',cycle_accumulator_json='{}',"
        "cycle_stall_attempts=0,cycle_started_at=NULL,cycle_updated_at=NULL,updated_at=? "
        "WHERE patrol_key='default' AND status='running'"
    )
    params: list[object] = [due_at, timestamp]
    if expected_lease_generation is not None:
        sql += " AND lease_generation=?"
        params.append(max(0, int(expected_lease_generation)))
    with get_conn() as conn:
        cur = conn.execute(sql, tuple(params))
        return cur.rowcount == 1


def claim_due_agent_library_patrol(
    *,
    current_time: str | None = None,
    stale_before: str | None = None,
) -> sqlite3.Row | None:
    """原子领取到期的全库巡检，generation 用于隔离过期 worker。"""
    timestamp = str(current_time or now())
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if stale_before:
            conn.execute(
                "UPDATE agent_library_patrol SET status='retry_wait',"
                "next_run_at=?,updated_at=? "
                "WHERE patrol_key='default' AND status='running' AND updated_at<=?",
                (timestamp, timestamp, str(stale_before)),
            )
        cur = conn.execute(
            "UPDATE agent_library_patrol SET status='running',"
            "lease_generation=lease_generation+1,last_started_at=?,updated_at=? "
            "WHERE patrol_key='default' AND status IN ('pending','retry_wait') "
            "AND next_run_at<=?",
            (timestamp, timestamp, timestamp),
        )
        if cur.rowcount != 1:
            return None
        return conn.execute(
            "SELECT * FROM agent_library_patrol WHERE patrol_key='default'"
        ).fetchone()


def continue_agent_library_patrol(
    *,
    expected_lease_generation: int,
    next_run_at: str,
    cycle_as_of: str,
    cycle_cursor_tmdb_id: str,
    cycle_accumulator_json: str,
    cycle_stall_attempts: int = 0,
    cycle_started_at: str | None = None,
) -> bool:
    """保存可续跑批次进度，并立即释放当前租约供下一批领取。"""
    cursor = str(cycle_cursor_tmdb_id or "").strip()
    if cursor and (
        not cursor.isascii() or not cursor.isdigit() or not 1 <= len(cursor) <= 10
    ):
        raise ValueError("全库巡检游标无效")
    stalls = max(0, min(int(cycle_stall_attempts), 3))
    if not cursor and stalls == 0:
        raise ValueError("全库巡检游标无效")
    raw = str(cycle_accumulator_json or "")
    if not raw or len(raw.encode("utf-8")) > 32_768:
        raise ValueError("全库巡检累计投影无效")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("全库巡检累计投影无效") from exc
    expected_keys = {
        "as_of", "patrol_status", "findings_truncated",
        "checked_series_count", "updates_available_count",
        "missing_episode_count", "inconclusive_count",
        "unmapped_series_count", "options",
    }
    if not isinstance(parsed, dict) or set(parsed) != expected_keys:
        raise ValueError("全库巡检累计投影无效")
    timestamp = now()
    started_at = str(cycle_started_at or timestamp)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT cycle_cursor_tmdb_id FROM agent_library_patrol "
            "WHERE patrol_key='default' AND status='running' AND lease_generation=?",
            (max(0, int(expected_lease_generation)),),
        ).fetchone()
        if current is None:
            return False
        current_cursor = str(current["cycle_cursor_tmdb_id"] or "").strip()
        if stalls:
            if cursor != current_cursor:
                return False
        elif not cursor or int(cursor) <= int(current_cursor or 0):
            return False
        cur = conn.execute(
            "UPDATE agent_library_patrol SET status='pending',outcome='',attempts=0,"
            "next_run_at=?,cycle_as_of=?,cycle_cursor_tmdb_id=?,"
            "cycle_accumulator_json=?,cycle_stall_attempts=?,"
            "cycle_started_at=COALESCE(cycle_started_at,?),"
            "cycle_updated_at=?,error_type='',updated_at=? "
            "WHERE patrol_key='default' AND status='running' AND lease_generation=?",
            (
                str(next_run_at or timestamp),
                str(cycle_as_of or "")[:10],
                str(int(cursor)) if cursor else "",
                raw,
                stalls,
                started_at,
                timestamp,
                timestamp,
                max(0, int(expected_lease_generation)),
            ),
        )
        return cur.rowcount == 1


def retry_agent_library_patrol_cycle(
    *,
    expected_lease_generation: int,
    status: str,
    attempts: int,
    next_run_at: str,
    error_type: str,
) -> bool:
    """批次异常时保留安全累计投影与游标，稍后从断点重试。"""
    if status not in {"pending", "retry_wait"}:
        raise ValueError("全库巡检重试状态无效")
    safe_error_type = re.sub(r"[^A-Za-z0-9_.-]", "", str(error_type or ""))[:80]
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_library_patrol SET status=?,outcome='failed',"
            "attempts=?,next_run_at=?,error_type=?,cycle_updated_at=?,updated_at=? "
            "WHERE patrol_key='default' AND status='running' AND lease_generation=? "
            "AND cycle_as_of<>''",
            (
                status,
                max(0, min(int(attempts), 100)),
                str(next_run_at or timestamp),
                safe_error_type,
                timestamp,
                timestamp,
                max(0, int(expected_lease_generation)),
            ),
        )
        return cur.rowcount == 1


def update_agent_library_patrol(
    *,
    status: str,
    outcome: str,
    attempts: int,
    next_run_at: str,
    expected_lease_generation: int,
    as_of: str = "",
    checked_series_count: int = 0,
    updates_available_count: int = 0,
    missing_episode_count: int = 0,
    inconclusive_count: int = 0,
    unmapped_series_count: int = 0,
    projection_json: str = "{}",
    findings_truncated: bool = False,
    error_type: str = "",
    last_finished_at: str | None = None,
    result_fingerprint: str | None = None,
    notification_payload_json: str = "",
    enqueue_notification: bool = False,
) -> bool:
    """以 lease generation 条件原子写入巡检结果和变化通知 outbox。"""
    if status not in {"pending", "running", "retry_wait"}:
        raise ValueError("全库巡检状态无效")
    if outcome not in {
        "", "updates_available", "up_to_date", "inconclusive",
        "not_configured", "unavailable", "failed",
    }:
        raise ValueError("全库巡检结果无效")
    safe_error_type = re.sub(r"[^A-Za-z0-9_.-]", "", str(error_type or ""))[:80]
    fingerprint = None
    if result_fingerprint is not None:
        candidate = str(result_fingerprint or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", candidate):
            raise ValueError("全库巡检结果指纹无效")
        fingerprint = candidate
    payload = str(notification_payload_json or "")
    if enqueue_notification:
        if fingerprint is None or outcome not in {"updates_available", "up_to_date"}:
            raise ValueError("全库巡检通知参数无效")
        if not payload or len(payload.encode("utf-8")) > 32_768:
            raise ValueError("全库巡检通知载荷无效")

    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT result_revision,result_fingerprint FROM agent_library_patrol "
            "WHERE patrol_key='default' AND status='running' AND lease_generation=?",
            (max(0, int(expected_lease_generation)),),
        ).fetchone()
        if row is None:
            return False
        previous_fingerprint = str(row["result_fingerprint"] or "")
        previous_revision = max(0, int(row["result_revision"] or 0))
        changed = fingerprint is not None and fingerprint != previous_fingerprint
        revision = previous_revision + 1 if changed else previous_revision
        stored_fingerprint = fingerprint if fingerprint is not None else previous_fingerprint
        values: tuple[object, ...] = (
            status,
            outcome,
            revision,
            stored_fingerprint,
            max(0, min(int(attempts), 100)),
            str(next_run_at or timestamp),
            last_finished_at,
            str(as_of or "")[:10],
            max(0, int(checked_series_count)),
            max(0, int(updates_available_count)),
            max(0, int(missing_episode_count)),
            max(0, int(inconclusive_count)),
            max(0, int(unmapped_series_count)),
            str(projection_json or "{}"),
            1 if findings_truncated else 0,
            safe_error_type,
            timestamp,
            max(0, int(expected_lease_generation)),
        )
        cur = conn.execute(
            "UPDATE agent_library_patrol SET status=?,outcome=?,result_revision=?,"
            "result_fingerprint=?,attempts=?,next_run_at=?,last_finished_at=?,as_of=?,"
            "checked_series_count=?,updates_available_count=?,missing_episode_count=?,"
            "inconclusive_count=?,unmapped_series_count=?,projection_json=?,"
            "findings_truncated=?,error_type=?,cycle_as_of='',"
            "cycle_cursor_tmdb_id='',cycle_accumulator_json='{}',"
            "cycle_stall_attempts=0,cycle_started_at=NULL,cycle_updated_at=NULL,updated_at=? "
            "WHERE patrol_key='default' AND status='running' AND lease_generation=?",
            values,
        )
        if cur.rowcount != 1:
            return False
        should_enqueue = bool(
            enqueue_notification
            and changed
            and (previous_fingerprint or outcome == "updates_available")
        )
        if should_enqueue:
            notification_time = str(last_finished_at or timestamp)
            conn.execute(
                "INSERT INTO agent_library_patrol_notification_outbox("
                "patrol_key,result_revision,fingerprint,outcome,payload_json,status,"
                "attempts,lease_generation,next_attempt_at,last_error_type,sent_at,"
                "created_at,updated_at) "
                "VALUES('default',?,?,?,?, 'pending',0,0,?,'',NULL,?,?)",
                (
                    revision, stored_fingerprint, outcome, payload, notification_time,
                    notification_time, notification_time,
                ),
            )
        return True


def list_agent_library_patrol_notifications() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM agent_library_patrol_notification_outbox ORDER BY id"
        ).fetchall()


def claim_due_agent_library_patrol_notification(
    *,
    current_time: str | None = None,
    stale_before: str | None = None,
) -> sqlite3.Row | None:
    """原子领取一条到期通知；generation 隔离过期发送者。"""
    timestamp = str(current_time or now())
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if stale_before:
            conn.execute(
                "UPDATE agent_library_patrol_notification_outbox "
                "SET status='discarded',lease_generation=lease_generation+1,"
                "payload_json='',last_error_type='DeliveryOutcomeUnknown',updated_at=? "
                "WHERE status='sending' AND updated_at<=?",
                (timestamp, str(stale_before)),
            )
        row = conn.execute(
            "SELECT id FROM agent_library_patrol_notification_outbox "
            "WHERE status IN ('pending','retry_wait') AND next_attempt_at<=? "
            "ORDER BY next_attempt_at,id LIMIT 1",
            (timestamp,),
        ).fetchone()
        if row is None:
            return None
        notification_id = int(row["id"])
        cur = conn.execute(
            "UPDATE agent_library_patrol_notification_outbox "
            "SET status='sending',lease_generation=lease_generation+1,updated_at=? "
            "WHERE id=? AND status IN ('pending','retry_wait')",
            (timestamp, notification_id),
        )
        if cur.rowcount != 1:
            return None
        return conn.execute(
            "SELECT * FROM agent_library_patrol_notification_outbox WHERE id=?",
            (notification_id,),
        ).fetchone()


def complete_agent_library_patrol_notification(
    notification_id: int, *, expected_lease_generation: int, sent_at: str | None = None,
) -> bool:
    timestamp = str(sent_at or now())
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_library_patrol_notification_outbox "
            "SET status='sent',payload_json='',sent_at=?,last_error_type='',updated_at=? "
            "WHERE id=? AND status='sending' AND lease_generation=?",
            (timestamp, timestamp, int(notification_id), max(0, int(expected_lease_generation))),
        )
        return cur.rowcount == 1


def release_agent_library_patrol_notification(
    notification_id: int, *, expected_lease_generation: int,
    next_attempt_at: str | None = None,
) -> bool:
    """无损释放尚未发送的通知租约；关闭 Agent 不消耗重试预算。"""
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_library_patrol_notification_outbox "
            "SET status='retry_wait',next_attempt_at=?,last_error_type='',updated_at=? "
            "WHERE id=? AND status='sending' AND lease_generation=?",
            (
                str(next_attempt_at or timestamp), timestamp, int(notification_id),
                max(0, int(expected_lease_generation)),
            ),
        )
        return cur.rowcount == 1


def retry_agent_library_patrol_notification(
    notification_id: int, *, expected_lease_generation: int,
    next_attempt_at: str, error_type: str = "",
) -> bool:
    safe_error_type = re.sub(r"[^A-Za-z0-9_.-]", "", str(error_type or ""))[:80]
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_library_patrol_notification_outbox "
            "SET status='retry_wait',attempts=MIN(attempts+1,100),"
            "next_attempt_at=?,last_error_type=?,updated_at=? "
            "WHERE id=? AND status='sending' AND lease_generation=?",
            (
                str(next_attempt_at or timestamp), safe_error_type, timestamp,
                int(notification_id), max(0, int(expected_lease_generation)),
            ),
        )
        return cur.rowcount == 1


def discard_agent_library_patrol_notification(
    notification_id: int, *, expected_lease_generation: int, error_type: str = "",
) -> bool:
    safe_error_type = re.sub(r"[^A-Za-z0-9_.-]", "", str(error_type or ""))[:80]
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_library_patrol_notification_outbox "
            "SET status='discarded',payload_json='',last_error_type=?,updated_at=? "
            "WHERE id=? AND status='sending' AND lease_generation=?",
            (
                safe_error_type, timestamp, int(notification_id),
                max(0, int(expected_lease_generation)),
            ),
        )
        return cur.rowcount == 1


def discard_agent_library_patrol_notifications() -> int:
    """通知关闭时丢弃未发送积压，并使正在发送的旧租约失效。"""
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE agent_library_patrol_notification_outbox "
            "SET status='discarded',payload_json='',"
            "lease_generation=lease_generation+1,updated_at=? "
            "WHERE status IN ('pending','retry_wait','sending')",
            (timestamp,),
        )
        return max(0, int(cur.rowcount))
