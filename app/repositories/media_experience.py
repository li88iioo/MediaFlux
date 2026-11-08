"""媒体消费偏好、订阅通知规则、通知 outbox 与今日内容摘要。"""
from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from app import database as db

_DEFAULT_PREFERENCES = {
    "preferred_server": "any",
    "preferred_download_target": "guangya",
    "preferred_resolution": "any",
    "minimum_resolution": "any",
    "preferred_hdr": "any",
    "preferred_codecs": [],
    "preferred_subtitles": [],
    "preferred_audio_languages": [],
    "preferred_release_groups": [],
    "excluded_keywords": [],
    "max_episode_size_gb": 0,
    "preferred_genres": [],
    "excluded_genres": [],
    "min_rating": 0,
    "exclude_played": True,
    "memory_notes": [],
}
_PROFILE_KEYS = tuple(key for key in _DEFAULT_PREFERENCES if key not in {
    "preferred_server", "preferred_download_target",
})
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
    return deepcopy(_DEFAULT_PREFERENCES)


def get_media_preferences(owner_digest: str) -> dict[str, Any]:
    try:
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT * "
                "FROM agent_media_preferences WHERE owner_digest=?",
                (owner_digest,),
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).casefold():
            raise
        # 兼容启动迁移尚未执行或精简测试库：未建表等同于没有显式偏好。
        row = None
    return _preferences_from_row(row)


def _preferences_from_row(row: sqlite3.Row | None) -> dict[str, Any]:
    """投影已读取的确切版本；不得在事务结束后另开连接回读。"""
    if row is None:
        return {**default_media_preferences(), "revision": 0, "revision_token": "", "explicit": False}
    profile = default_media_preferences()
    try:
        saved = json.loads(dict(row).get("profile_json", "{}"))
    except (TypeError, ValueError):
        saved = {}
    if isinstance(saved, dict):
        profile.update({key: saved[key] for key in _PROFILE_KEYS if key in saved})
    return {
        **profile,
        "preferred_server": str(row["preferred_server"]),
        "preferred_download_target": str(row["preferred_download_target"]),
        "revision": int(row["revision"]),
        "revision_token": str(dict(row).get("revision_token") or ""),
        "explicit": True,
    }


def merge_media_preferences(current: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """字段级替换与保留共用同一规则；不会把软偏好扩展为硬筛选。"""
    return {key: updates.get(key, current[key]) for key in _DEFAULT_PREFERENCES}


def set_media_preferences(
    owner_digest: str, *, expected_revision: int, updates: dict[str, Any],
    expected_revision_token: str | None = None,
) -> dict[str, Any] | None:
    stamp = db.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM agent_media_preferences WHERE owner_digest=?",
            (owner_digest,),
        ).fetchone()
        current = _preferences_from_row(row)
        if int(current["revision"]) != expected_revision:
            return None
        if expected_revision_token is not None and not secrets.compare_digest(
            str(current["revision_token"]), expected_revision_token
        ):
            return None
        merged = merge_media_preferences(current, updates)
        profile_json = json.dumps(
            {key: merged[key] for key in _PROFILE_KEYS}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        # 整数 revision 在删除再创建后会重用；每次写入再分配私有 nonce 阻止旧快照 ABA。
        revision_token = secrets.token_hex(16)
        if row is None:
            conn.execute(
                "INSERT INTO agent_media_preferences("
                "owner_digest,preferred_server,preferred_download_target,profile_json,revision_token,"
                "revision,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?)",
                (
                    owner_digest, merged["preferred_server"],
                    merged["preferred_download_target"], profile_json, revision_token, stamp, stamp,
                ),
            )
        else:
            conn.execute(
                "UPDATE agent_media_preferences SET preferred_server=?,"
                "preferred_download_target=?,profile_json=?,revision_token=?,revision=revision+1,updated_at=? "
                "WHERE owner_digest=? AND revision=?",
                (
                    merged["preferred_server"], merged["preferred_download_target"],
                    profile_json, revision_token, stamp, owner_digest, expected_revision,
                ),
            )
        committed = _preferences_from_row(conn.execute(
            "SELECT * FROM agent_media_preferences WHERE owner_digest=?",
            (owner_digest,),
        ).fetchone())
    # 离开 with 后本次事务已成功提交；直接返回锁内读取的自身版本，避免把后来写入 C 当作 B。
    return committed


def clear_media_preferences(
    owner_digest: str, *, expected_revision: int, expected_revision_token: str | None = None,
) -> bool:
    if expected_revision <= 0:
        return False
    where_token = " AND revision_token=?" if expected_revision_token is not None else ""
    parameters: tuple[Any, ...] = (owner_digest, expected_revision)
    if expected_revision_token is not None:
        parameters += (expected_revision_token,)
    with db.get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM agent_media_preferences WHERE owner_digest=? AND revision=?" + where_token,
            parameters,
        )
    return bool(cur.rowcount)


def _read_notification_rule(conn, subscription_id: int) -> dict[str, Any] | None:
    """同一查询投影订阅与规则；写回执也在提交前复用这一快照。"""
    row = conn.execute(
        "SELECT s.id,s.title,s.revision AS subscription_revision,"
        "s.enabled AS subscription_enabled,s.status AS subscription_status,"
        "r.enabled,r.notify_on_missing,r.notify_on_satisfied,r.notify_on_error,"
        "r.revision AS rule_revision FROM media_subscriptions AS s "
        "LEFT JOIN media_subscription_notification_rules AS r ON r.subscription_id=s.id "
        "WHERE s.id=? AND s.deleted_at IS NULL",
        (subscription_id,),
    ).fetchone()
    if row is None:
        return None
    explicit = row["rule_revision"] is not None
    rule = {key: _bool(row[key]) for key in _DEFAULT_RULE} if explicit else dict(_DEFAULT_RULE)
    return {
        "subscription_number": int(row["id"]),
        "title": str(row["title"]),
        "subscription_revision": int(row["subscription_revision"]),
        "subscription_enabled": _bool(row["subscription_enabled"]),
        "subscription_status": str(row["subscription_status"]),
        **rule,
        "revision": int(row["rule_revision"]) if explicit else 0,
        "explicit": explicit,
    }


def get_notification_rule(subscription_id: int) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        return _read_notification_rule(conn, subscription_id)


def _read_rule_for_update(
    conn, subscription_id: int, *, expected_rule_revision: int,
    expected_subscription_revision: int, expected_rule: dict[str, Any] | None,
) -> dict[str, Any] | None:
    current = _read_notification_rule(conn, subscription_id)
    if (
        current is None
        or current["subscription_revision"] != expected_subscription_revision
        or current["revision"] != expected_rule_revision
        # reset 后整数版本可复用；确认链路同时核对实际预检的完整业务状态。
        or (expected_rule is not None and current != expected_rule)
    ):
        return None
    return current


def set_notification_rule(
    subscription_id: int,
    *,
    expected_rule_revision: int,
    expected_subscription_revision: int,
    updates: dict[str, bool],
    expected_rule: dict[str, Any] | None = None,
    check_cancelled: Callable[[], None] = lambda: None,
) -> dict[str, Any] | None:
    stamp = db.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = _read_rule_for_update(
            conn, subscription_id, expected_rule_revision=expected_rule_revision,
            expected_subscription_revision=expected_subscription_revision, expected_rule=expected_rule,
        )
        if current is None:
            return None
        check_cancelled()
        merged = {key: bool(updates.get(key, current[key])) for key in _DEFAULT_RULE}
        if not current["explicit"]:
            conn.execute(
                "INSERT INTO media_subscription_notification_rules("
                "subscription_id,enabled,notify_on_missing,notify_on_satisfied,"
                "notify_on_error,revision,created_at,updated_at) "
                "VALUES(?,?,?,?,?,1,?,?)",
                (
                    subscription_id, int(merged["enabled"]),
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
                    stamp, subscription_id,
                    expected_rule_revision,
                ),
            )
            if cur.rowcount != 1:
                return None
        committed = _read_notification_rule(conn, subscription_id)
    return committed


def reset_notification_rule(
    subscription_id: int, *, expected_rule_revision: int, expected_subscription_revision: int,
    expected_rule: dict[str, Any] | None = None,
    check_cancelled: Callable[[], None] = lambda: None,
) -> bool:
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = _read_rule_for_update(
            conn, subscription_id, expected_rule_revision=expected_rule_revision,
            expected_subscription_revision=expected_subscription_revision, expected_rule=expected_rule,
        )
        if current is None or not current["explicit"]:
            return False
        check_cancelled()
        cur = conn.execute(
            "DELETE FROM media_subscription_notification_rules "
            "WHERE subscription_id=? AND revision=?",
            (subscription_id, expected_rule_revision),
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
            (stamp, max(1, min(limit, 50))),
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
            except (TypeError, ValueError):
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
            (stamp, stamp, notification_id, lease_generation),
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
            "WHERE id=?", (notification_id,),
        ).fetchone()
        if row is None or str(row["status"]) != "sending" or int(row["lease_generation"]) != lease_generation:
            return "stale"
        attempts = int(row["attempts"] or 0) + 1
        exhausted = attempts >= _MAX_ATTEMPTS
        status = "failed" if exhausted else "retry_wait"
        requested_delay = max(0, min((retry_after_seconds or 0), 86_400))
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
                next_attempt, stamp, notification_id, lease_generation,
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
        return (cur.rowcount or 0)


def list_notification_outbox(*, limit: int = 50) -> list[sqlite3.Row]:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM media_subscription_notification_outbox "
            "ORDER BY id DESC LIMIT ?", (max(1, min(limit, 100)),),
        ).fetchall()


def today_content_summary() -> dict[str, Any]:
    local_now = datetime.now().astimezone()
    day = local_now.strftime("%Y-%m-%d")
    next_day = (local_now.date() + timedelta(days=1)).isoformat()
    # 全量计数与有界标题分离；这些 SQL 片段均为固定业务来源，不接收外部输入。
    sources = (
        ("subscription_runs", "media_subscription_runs r JOIN media_subscriptions s ON s.id=r.subscription_id",
         "r.finished_at", "s.title", {
             "missing": "missing", "satisfied": "satisfied", "failed": "failed",
             "inconclusive": "attention", "cancelled": "cancelled",
         }),
        ("local_media_tasks", "local_media_tasks r", "r.completed_at", "r.title", {
            "completed": "completed", "failed": "failed", "requires_manual": "attention",
        }),
        ("rss_entries", "rss_entries r",
         "COALESCE(NULLIF(r.processed_at,''),NULLIF(r.submitted_at,''),r.created_at)", "r.title", {
             "downloaded": "downloaded", "skipped": "skipped", "failed": "failed", "pending": "pending",
         }),
        ("downloads", "download_log r",
         "COALESCE(NULLIF(r.completed_at,''),NULLIF(r.updated_at,''),r.created_at)", "r.title", {
             "success": "success", "failed": "failed", "submitted": "submitted",
         }),
    )
    totals: dict[str, dict[str, int]] = {}
    titles: list[str] = []
    with db.get_conn() as conn:
        # 四类事件与标题必须属于同一时点，不能把后续提交混入半份摘要。
        conn.execute("BEGIN")
        for key, table, timestamp, title, categories in sources:
            where = f"{timestamp}>=? AND {timestamp}<?"
            case = "CASE r.status " + " ".join("WHEN ? THEN ?" for _ in categories) + " ELSE 'processing' END"
            parameters = [value for pair in categories.items() for value in pair]
            rows = conn.execute(
                f"SELECT {case} AS category,COUNT(*) AS count FROM {table} "
                f"WHERE {where} GROUP BY category",
                (*parameters, day, next_day),
            ).fetchall()
            totals[key] = {str(row["category"]): int(row["count"]) for row in rows}
            if len(titles) >= 8:
                continue
            # 展示仍取各来源最近50条；按需迭代并保留 Python strip/去重语义。
            samples = conn.execute(
                f"SELECT {title} AS title FROM {table} WHERE {where} ORDER BY r.id DESC LIMIT 50",
                (day, next_day),
            )
            for row in samples:
                candidate = str(row["title"] or "").strip()
                if candidate and candidate not in titles:
                    titles.append(candidate)
                if len(titles) >= 8:
                    break
            samples.close()
    return {
        "local_date": day,
        "timezone": str(local_now.tzinfo or "local"),
        "as_of": local_now.isoformat(timespec="seconds"),
        **totals,
        "content_titles": titles,
        "event_count": sum(sum(counts.values()) for counts in totals.values()),
    }
