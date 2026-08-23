"""媒体消费偏好、订阅通知规则、通知 outbox 与今日内容摘要。"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import sqlite3
from typing import Any

from app import database as db

_DEFAULT_PREFERENCES = {
    "preferred_server": "any",
    "preferred_download_target": "guangya",
}
_DEFAULT_RULE = {
    "enabled": False,
    "notify_on_missing": True,
    "notify_on_satisfied": False,
    "notify_on_error": True,
}
_MAX_ATTEMPTS = 6


def _bool(value: Any) -> bool:
    return bool(int(value or 0))


def default_media_preferences() -> dict[str, Any]:
    """返回显式偏好的公开默认值，避免使用伪 owner 读取数据库。"""
    return dict(_DEFAULT_PREFERENCES)


def get_media_preferences(owner_digest: str) -> dict[str, Any]:
    try:
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT preferred_server,preferred_download_target,revision,created_at,updated_at "
                "FROM agent_media_preferences WHERE owner_digest=?",
                (str(owner_digest),),
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).casefold():
            raise
        # 兼容启动迁移尚未执行或精简测试库：未建表等同于没有显式偏好。
        row = None
    if row is None:
        return {**_DEFAULT_PREFERENCES, "revision": 0, "explicit": False}
    return {
        "preferred_server": str(row["preferred_server"]),
        "preferred_download_target": str(row["preferred_download_target"]),
        "revision": int(row["revision"]),
        "explicit": True,
    }


def set_media_preferences(
    owner_digest: str, *, expected_revision: int, updates: dict[str, Any]
) -> dict[str, Any] | None:
    current = get_media_preferences(owner_digest)
    stamp = db.now()
    merged = {key: updates.get(key, current[key]) for key in _DEFAULT_PREFERENCES}
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT revision FROM agent_media_preferences WHERE owner_digest=?",
            (str(owner_digest),),
        ).fetchone()
        actual_revision = int(row["revision"]) if row is not None else 0
        if actual_revision != int(expected_revision):
            return None
        if row is None:
            conn.execute(
                "INSERT INTO agent_media_preferences("
                "owner_digest,preferred_server,preferred_download_target,"
                "revision,created_at,updated_at) VALUES(?,?,?,1,?,?)",
                (
                    str(owner_digest), merged["preferred_server"],
                    merged["preferred_download_target"], stamp, stamp,
                ),
            )
        else:
            conn.execute(
                "UPDATE agent_media_preferences SET preferred_server=?,"
                "preferred_download_target=?,revision=revision+1,updated_at=? "
                "WHERE owner_digest=? AND revision=?",
                (
                    merged["preferred_server"], merged["preferred_download_target"],
                    stamp, str(owner_digest), int(expected_revision),
                ),
            )
    return get_media_preferences(owner_digest)


def clear_media_preferences(owner_digest: str, *, expected_revision: int) -> bool:
    if int(expected_revision) <= 0:
        return False
    with db.get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM agent_media_preferences WHERE owner_digest=? AND revision=?",
            (str(owner_digest), int(expected_revision)),
        )
        return cur.rowcount == 1


def get_notification_rule(subscription_id: int) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        subscription = conn.execute(
            "SELECT id,title,revision,enabled,status FROM media_subscriptions "
            "WHERE id=? AND deleted_at IS NULL",
            (int(subscription_id),),
        ).fetchone()
        if subscription is None:
            return None
        row = conn.execute(
            "SELECT enabled,notify_on_missing,notify_on_satisfied,notify_on_error,"
            "revision FROM media_subscription_notification_rules "
            "WHERE subscription_id=?",
            (int(subscription_id),),
        ).fetchone()
    rule = dict(_DEFAULT_RULE)
    rule_revision = 0
    explicit = row is not None
    if row is not None:
        rule.update({key: _bool(row[key]) for key in _DEFAULT_RULE})
        rule_revision = int(row["revision"])
    return {
        "subscription_number": int(subscription["id"]),
        "title": str(subscription["title"]),
        "subscription_revision": int(subscription["revision"]),
        "subscription_enabled": _bool(subscription["enabled"]),
        "subscription_status": str(subscription["status"]),
        **rule,
        "revision": rule_revision,
        "explicit": explicit,
    }


def set_notification_rule(
    subscription_id: int,
    *,
    expected_rule_revision: int,
    expected_subscription_revision: int,
    updates: dict[str, bool],
) -> dict[str, Any] | None:
    stamp = db.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        subscription = conn.execute(
            "SELECT revision FROM media_subscriptions WHERE id=? AND deleted_at IS NULL",
            (int(subscription_id),),
        ).fetchone()
        if (
            subscription is None
            or int(subscription["revision"]) != int(expected_subscription_revision)
        ):
            return None
        row = conn.execute(
            "SELECT enabled,notify_on_missing,notify_on_satisfied,notify_on_error,"
            "revision FROM media_subscription_notification_rules "
            "WHERE subscription_id=?",
            (int(subscription_id),),
        ).fetchone()
        actual_revision = int(row["revision"]) if row is not None else 0
        if actual_revision != int(expected_rule_revision):
            return None
        current = dict(_DEFAULT_RULE) if row is None else {
            key: _bool(row[key]) for key in _DEFAULT_RULE
        }
        merged = {key: bool(updates.get(key, current[key])) for key in _DEFAULT_RULE}
        if row is None:
            conn.execute(
                "INSERT INTO media_subscription_notification_rules("
                "subscription_id,enabled,notify_on_missing,notify_on_satisfied,"
                "notify_on_error,revision,created_at,updated_at) "
                "VALUES(?,?,?,?,?,1,?,?)",
                (
                    int(subscription_id), int(merged["enabled"]),
                    int(merged["notify_on_missing"]), int(merged["notify_on_satisfied"]),
                    int(merged["notify_on_error"]), stamp, stamp,
                ),
            )
        else:
            cur = conn.execute(
                "UPDATE media_subscription_notification_rules SET enabled=?,"
                "notify_on_missing=?,notify_on_satisfied=?,notify_on_error=?,"
                "revision=revision+1,updated_at=? "
                "WHERE subscription_id=? AND revision=?",
                (
                    int(merged["enabled"]), int(merged["notify_on_missing"]),
                    int(merged["notify_on_satisfied"]), int(merged["notify_on_error"]),
                    stamp, int(subscription_id),
                    int(expected_rule_revision),
                ),
            )
            if cur.rowcount != 1:
                return None
    return get_notification_rule(subscription_id)


def reset_notification_rule(
    subscription_id: int, *, expected_rule_revision: int, expected_subscription_revision: int
) -> bool:
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        subscription = conn.execute(
            "SELECT revision FROM media_subscriptions WHERE id=? AND deleted_at IS NULL",
            (int(subscription_id),),
        ).fetchone()
        if (
            subscription is None
            or int(subscription["revision"]) != int(expected_subscription_revision)
        ):
            return False
        cur = conn.execute(
            "DELETE FROM media_subscription_notification_rules "
            "WHERE subscription_id=? AND revision=?",
            (int(subscription_id), int(expected_rule_revision)),
        )
        return cur.rowcount == 1


def claim_due_notifications(*, limit: int = 20) -> list[dict[str, Any]]:
    stamp = db.now()
    try:
        lease_until = (
            datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S") + timedelta(minutes=2)
        ).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        lease_until = (datetime.now() + timedelta(minutes=2)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    claimed: list[dict[str, Any]] = []
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM media_subscription_notification_outbox "
            "WHERE status IN ('pending','retry_wait') AND next_attempt_at<=? "
            "ORDER BY next_attempt_at,id LIMIT ?",
            (stamp, max(1, min(int(limit), 50))),
        ).fetchall()
        for row in rows:
            generation = int(row["lease_generation"] or 0)
            cur = conn.execute(
                "UPDATE media_subscription_notification_outbox SET status='sending',"
                "lease_generation=lease_generation+1,lease_until=?,updated_at=? "
                "WHERE id=? AND status=? AND lease_generation=?",
                (
                    lease_until, stamp, int(row["id"]),
                    str(row["status"]), generation,
                ),
            )
            if cur.rowcount != 1:
                continue
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            claimed.append({
                "id": int(row["id"]),
                "event_key": str(row["event_key"]),
                "event_type": str(row["event_type"]),
                "payload": payload if isinstance(payload, dict) else {},
                "attempts": int(row["attempts"] or 0),
                "lease_generation": generation + 1,
            })
    return claimed


def mark_notification_sent(notification_id: int, *, lease_generation: int) -> bool:
    stamp = db.now()
    with db.get_conn() as conn:
        cur = conn.execute(
            "UPDATE media_subscription_notification_outbox SET status='sent',sent_at=?,"
            "last_error='',lease_until='',updated_at=? WHERE id=? AND status='sending' "
            "AND lease_generation=?",
            (stamp, stamp, int(notification_id), int(lease_generation)),
        )
        return cur.rowcount == 1


_DELIVERY_ERROR_CODES = {
    "telegram_exception", "telegram_rate_limited", "telegram_unavailable",
}


def _delivery_error_code(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    return normalized if normalized in _DELIVERY_ERROR_CODES else "telegram_unavailable"


def retry_notification(
    notification_id: int, *, lease_generation: int, error: str, retry_after_seconds: int = 0
) -> str:
    stamp = db.now()
    try:
        base = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        base = datetime.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT attempts,status,lease_generation FROM media_subscription_notification_outbox "
            "WHERE id=?", (int(notification_id),),
        ).fetchone()
        if row is None or str(row["status"]) != "sending" or int(row["lease_generation"]) != int(lease_generation):
            return "stale"
        attempts = int(row["attempts"] or 0) + 1
        exhausted = attempts >= _MAX_ATTEMPTS
        status = "failed" if exhausted else "retry_wait"
        requested_delay = max(0, min(int(retry_after_seconds or 0), 86_400))
        delay = 0 if exhausted else max(
            30 * (2 ** min(attempts - 1, 5)), requested_delay
        )
        next_attempt = (base + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            "UPDATE media_subscription_notification_outbox SET status=?,attempts=?,"
            "last_error=?,lease_until='',next_attempt_at=?,updated_at=? "
            "WHERE id=? AND status='sending' "
            "AND lease_generation=?",
            (
                status, attempts, _delivery_error_code(error),
                next_attempt, stamp, int(notification_id), int(lease_generation),
            ),
        )
        return status if cur.rowcount == 1 else "stale"


def recover_notifications() -> int:
    stamp = db.now()
    with db.get_conn() as conn:
        cur = conn.execute(
            "UPDATE media_subscription_notification_outbox SET status='retry_wait',"
            "lease_generation=lease_generation+1,lease_until='',next_attempt_at=?,updated_at=? "
            "WHERE status='sending' AND (lease_until='' OR lease_until<=?)",
            (stamp, stamp, stamp),
        )
        return int(cur.rowcount or 0)


def list_notification_outbox(*, limit: int = 50) -> list[sqlite3.Row]:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM media_subscription_notification_outbox "
            "ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 100)),),
        ).fetchall()


def today_content_summary() -> dict[str, Any]:
    local_now = datetime.now().astimezone()
    day = local_now.strftime("%Y-%m-%d")
    pattern = f"{day}%"
    with db.get_conn() as conn:
        runs = conn.execute(
            "SELECT r.status,s.title FROM media_subscription_runs r "
            "JOIN media_subscriptions s ON s.id=r.subscription_id "
            "WHERE r.finished_at LIKE ? ORDER BY r.id DESC LIMIT 50",
            (pattern,),
        ).fetchall()
        local_tasks = conn.execute(
            "SELECT status,title FROM local_media_tasks WHERE completed_at LIKE ? "
            "ORDER BY id DESC LIMIT 50", (pattern,),
        ).fetchall()
        rss = conn.execute(
            "SELECT status,title FROM rss_entries WHERE COALESCE(processed_at,submitted_at,created_at) "
            "LIKE ? ORDER BY id DESC LIMIT 50", (pattern,),
        ).fetchall()
        downloads = conn.execute(
            "SELECT status,title FROM download_log WHERE COALESCE(completed_at,updated_at,created_at) "
            "LIKE ? ORDER BY id DESC LIMIT 50", (pattern,),
        ).fetchall()
    def counts(rows: list[Any], categories: dict[str, str]) -> dict[str, int]:
        result: dict[str, int] = {}
        for row in rows:
            raw_status = str(row["status"] or "unknown")
            status = categories.get(raw_status, "processing")
            result[status] = result.get(status, 0) + 1
        return result
    titles: list[str] = []
    for rows in (runs, local_tasks, rss, downloads):
        for row in rows:
            title = str(row["title"] or "").strip()
            if title and title not in titles:
                titles.append(title)
            if len(titles) >= 8:
                break
        if len(titles) >= 8:
            break
    return {
        "local_date": day,
        "timezone": str(local_now.tzinfo or "local"),
        "as_of": local_now.isoformat(timespec="seconds"),
        "subscription_runs": counts(list(runs), {
            "missing": "missing", "satisfied": "satisfied",
            "failed": "failed", "inconclusive": "attention",
            "cancelled": "cancelled",
        }),
        "local_media_tasks": counts(list(local_tasks), {
            "completed": "completed", "failed": "failed",
            "requires_manual": "attention",
        }),
        "rss_entries": counts(list(rss), {
            "downloaded": "downloaded", "skipped": "skipped",
            "failed": "failed", "pending": "pending",
        }),
        "downloads": counts(list(downloads), {
            "success": "success", "failed": "failed",
            "submitted": "submitted",
        }),
        "content_titles": titles,
        "event_count": len(runs) + len(local_tasks) + len(rss) + len(downloads),
    }
