"""整理后媒体规格补全队列的数据访问。"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from app import database as db

if TYPE_CHECKING:
    pass


def _future_stamp(seconds: int) -> str:
    return (datetime.now() + timedelta(seconds=max(0, seconds))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _safe_error(value: object, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text[: max(40, limit)]


def _closed_notification_context() -> dict:
    """未知协议不是旧空任务；保留可持久化的拒绝路由标记。"""
    return {
        "version": 1, "untrusted": True, "task_id": "", "chat_id": "",
        "notify_enabled": False, "topic_enabled": False,
        "notification_threads": [], "download_request_ids": [],
    }


def normalize_notification_context(value: object) -> dict:
    """只保留内部合同字段；真正旧空值与未知/损坏非空协议严格区分。"""
    if value is None or value == {}:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return _closed_notification_context()
        if value == {}:
            return {}
    if not isinstance(value, dict) or value.get("version") != 1 or value.get("untrusted"):
        return _closed_notification_context()
    raw_refs = value.get("notification_threads") or []
    ids = value.get("download_request_ids") or []
    if not isinstance(raw_refs, list) or not isinstance(ids, list):
        return _closed_notification_context()
    refs: dict[tuple[str, str, str], dict[str, Any]] = {}
    for ref in raw_refs:
        if not isinstance(ref, dict):
            continue
        topic = str(ref.get("topic") or "")
        identity_key = {"organize": "task_id", "confirmation": "token", "download": "request_id"}.get(topic)
        if not identity_key:
            continue
        identity = str(ref.get(identity_key) or "").strip()
        if not identity or (topic == "download" and (not identity.isdecimal() or int(identity) <= 0)):
            continue
        if str(ref.get("thread_key") or "") != f"{topic}:{identity}":
            continue
        chat = str(ref.get("chat_id") or "").strip()
        key = (topic, identity, chat)
        refs[key] = {
            "topic": topic, "thread_key": f"{topic}:{identity}", identity_key: identity,
            "chat_id": chat,
            # 同一 scope 的重复 ref 不能把已有关闭策略重新打开。
            "topic_enabled": ref.get("topic_enabled", True) is True
            and refs.get(key, {}).get("topic_enabled", True),
        }
    return {
        "version": 1,
        "task_id": str(value.get("task_id") or "").strip(),
        "chat_id": str(value.get("chat_id") or "").strip(),
        "notify_enabled": value.get("notify_enabled", True) is True,
        "topic_enabled": value.get("topic_enabled", True) is True,
        "notification_threads": list(refs.values()),
        "download_request_ids": list(dict.fromkeys(
            int(item) for item in ids if str(item).isdecimal() and int(item) > 0
        )),
    }


def enqueue_organize_probe_completion(
    organize_log_id: int,
    *,
    source_id: str,
    rel_dir: str,
    rules: dict,
    delay_seconds: int = 130,
    max_attempts: int = 2,
    notification_context: dict | None = None,
) -> int:
    """登记整理成功但在线探测未完成的文件；同一日志幂等。"""
    stamp = db.now()
    next_attempt = _future_stamp(max(30, delay_seconds))
    payload = json.dumps(rules if isinstance(rules, dict) else {}, ensure_ascii=False)
    context_json = json.dumps(normalize_notification_context(notification_context), ensure_ascii=False)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO organize_probe_queue(organize_log_id,provider,source_id,rel_dir,"
            "rules_json,notification_context_json,status,attempts,max_attempts,next_attempt_at,lease_owner,lease_until,"
            "last_error_type,last_error,created_at,updated_at) "
            "VALUES(?, 'guangya', ?, ?, ?, ?, 'queued', 0, ?, ?, '', 0, '', '', ?, ?) "
            "ON CONFLICT(organize_log_id) DO UPDATE SET source_id=excluded.source_id,"
            "rel_dir=excluded.rel_dir,rules_json=excluded.rules_json,"
            "max_attempts=excluded.max_attempts,updated_at=excluded.updated_at",
            (
                organize_log_id, (source_id or ""), (rel_dir or ""), payload, context_json,
                max(1, min((max_attempts or 2), 5)), next_attempt, stamp, stamp,
            ),
        )
        row = conn.execute(
            "SELECT id FROM organize_probe_queue WHERE organize_log_id=?",
            (organize_log_id,),
        ).fetchone()
        return int(row["id"]) if row else 0


def claim_due_organize_probe_jobs(
    *, owner: str, lease_seconds: int = 900, limit: int = 1,
) -> list[dict]:
    stamp = db.now()
    lease_until = time.time() + max(30, (lease_seconds or 900))
    safe_limit = max(1, min((limit or 1), 20))
    with db.get_conn() as conn:
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
                ((owner or ""), lease_until, stamp, int(row["id"])),
            )
            if cur.rowcount != 1:
                continue
            current = conn.execute(
                "SELECT * FROM organize_probe_queue WHERE id=?", (int(row["id"]),)
            ).fetchone()
            if current:
                claimed.append(dict(current))
        return claimed


def complete_organize_probe_job(
    job_id: int, *, owner: str, handoff_completed: bool = False,
) -> bool:
    """只有有效执行者确认下游接管后，才清空待交接载荷并完成任务。"""
    stamp = db.now()
    with db.get_conn() as conn:
        # 先获得 writer 再取 lease 校验时间，不能使用等待写锁前的过期快照。
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE organize_probe_queue SET status='completed',lease_owner='',lease_until=0,"
            "last_error_type='',last_error='',completed_at=?,updated_at=?,"
            "pending_strm_changes_json='[]' "
            "WHERE id=? AND status='running' AND lease_owner=? AND lease_until>? "
            "AND (pending_strm_changes_json='[]' OR ?)",
            (stamp, stamp, job_id, (owner or ""), time.time(), handoff_completed),
        )
        return cur.rowcount == 1


def cancel_organize_probe_job(
    job_id: int, *, owner: str = "", reason: object = "",
) -> bool:
    stamp = db.now()
    sql = (
        "UPDATE organize_probe_queue SET status='cancelled',lease_owner='',lease_until=0,"
        "last_error_type='Cancelled',last_error=?,completed_at=?,updated_at=? WHERE id=?"
    )
    params: list[object] = [_safe_error(reason), stamp, stamp, job_id]
    if owner:
        sql += " AND status='running' AND lease_owner=?"
        params.append(owner)
    with db.get_conn() as conn:
        cur = conn.execute(sql, params)
        return cur.rowcount == 1


def release_organize_probe_job(
    job_id: int, *, owner: str, delay_seconds: int = 30, reason: object = "",
) -> bool:
    stamp = db.now()
    with db.get_conn() as conn:
        cur = conn.execute(
            "UPDATE organize_probe_queue SET status='retry_wait',next_attempt_at=?,"
            "lease_owner='',lease_until=0,last_error_type='Busy',last_error=?,updated_at=? "
            "WHERE id=? AND status='running' AND lease_owner=?",
            (
                _future_stamp(max(5, delay_seconds)), _safe_error(reason), stamp,
                job_id, (owner or ""),
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
    stamp = db.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT attempts,max_attempts FROM organize_probe_queue "
            "WHERE id=? AND status='running' AND lease_owner=?",
            (job_id, (owner or "")),
        ).fetchone()
        if not row:
            return "stale"
        attempts = int(row["attempts"] or 0) + 1
        exhausted = attempts >= max(1, int(row["max_attempts"] or 1))
        status = "failed" if exhausted else "retry_wait"
        delay = max(30, (base_backoff_seconds or 600)) * (2 ** max(0, attempts - 1))
        next_attempt = stamp if exhausted else _future_stamp(min(delay, 3600))
        cur = conn.execute(
            "UPDATE organize_probe_queue SET status=?,attempts=?,next_attempt_at=?,"
            "lease_owner='',lease_until=0,last_error_type=?,last_error=?,"
            "completed_at=CASE WHEN ?='failed' THEN ? ELSE completed_at END,updated_at=? "
            "WHERE id=? AND status='running' AND lease_owner=?",
            (
                status, attempts, next_attempt, (error_type or "Error")[:100],
                _safe_error(error), status, stamp, stamp, job_id, (owner or ""),
            ),
        )
        return status if cur.rowcount == 1 else "stale"


def recover_stale_organize_probe_jobs(*, force: bool = False) -> int:
    stamp = db.now()
    where = "status='running'" if force else "status='running' AND lease_until<=?"
    params: list[object] = [] if force else [time.time()]
    with db.get_conn() as conn:
        cur = conn.execute(
            "UPDATE organize_probe_queue SET status='retry_wait',next_attempt_at=?,"
            "lease_owner='',lease_until=0,last_error_type='ProcessInterrupted',"
            "last_error='上次进程在媒体规格补全期间中断',updated_at=? WHERE " + where,
            (stamp, stamp, *params),
        )
        return (cur.rowcount or 0)


def count_organize_probe_jobs() -> dict[str, int]:
    with db.get_conn() as conn:
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
    job_id: int,
    owner: str,
    changes: list[dict],
) -> bool:
    """同事务提交审计和待交接变化；队列调用必须持有未过期 lease。"""
    pending = json.dumps(changes, ensure_ascii=False)
    stamp = db.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT id FROM organize_probe_queue WHERE id=? AND organize_log_id=? "
            "AND status='running' AND lease_owner=? AND lease_until>? "
            "AND pending_strm_changes_json='[]'",
            (job_id, organize_log_id, (owner or ""), time.time()),
        ).fetchone()
        if not owner or job is None:
            return False
        row = conn.execute(
            "SELECT status,legacy_incomplete FROM organize_log WHERE id=?",
            (organize_log_id,),
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
                    stamp, int(item.get("id") or 0), organize_log_id,
                    str(item.get("expected_name") or ""),
                ),
            )
            if cur.rowcount != 1:
                raise RuntimeError("整理成员快照已变化，拒绝提交媒体规格补全")
        cur = conn.execute(
            "UPDATE organize_log SET current_name=?,new_path=?,error='',version=version+1,"
            "updated_at=? WHERE id=? AND status='success' AND legacy_incomplete=0",
            ((current_name or ""), (new_path or ""), stamp, organize_log_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("整理日志快照已变化，拒绝提交媒体规格补全")
        cur = conn.execute(
            "UPDATE organize_probe_queue SET pending_strm_changes_json=?,updated_at=? "
            "WHERE id=? AND organize_log_id=? AND status='running' "
            "AND lease_owner=? AND lease_until>? AND pending_strm_changes_json='[]'",
            (pending, stamp, job_id, organize_log_id, owner, time.time()),
        )
        if cur.rowcount != 1:
            # 此处不能 return False：必须让审计更新一并回滚。
            raise RuntimeError("规格补全任务 lease 已变化，拒绝提交交接")
        return True


def probe_rules_notify_enabled(encoded: object) -> bool:
    """身份快照不能重新打开后来明确关闭的交接通知策略。"""
    try:
        rules = json.loads(encoded or "{}") if isinstance(encoded, str) else encoded
    except (TypeError, ValueError):
        return False
    return isinstance(rules, dict) and rules.get("notify_enabled", True) is True


def get_organize_probe_notification_progress(
    *, topic: str, thread_key: str, chat_id: str, notification_enabled_only: bool = False,
) -> dict[str, int]:
    """只按持久父线程+收件人读进度，不从来源目录推测旧批次。

    STRM 成功接管不代表执行完成；连同既有 changes 的 pending/inflight 载荷
    一起检查，覆盖 probe ACK 与 STRM 结算任意先后顺序。只读，不修改 STRM 表。
    """
    progress = {"total": 0, "pending": 0, "failed": 0, "cancelled": 0, "strm_pending": 0, "strm_failed": 0}
    if not topic or not thread_key or not chat_id:
        return progress

    def matches(value: object) -> bool:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return False
        if not isinstance(value, dict) or value.get("version") != 1:
            return False
        if notification_enabled_only and (
            value.get("notify_enabled") is not True or value.get("topic_enabled") is not True
        ):
            return False
        refs = value.get("notification_threads")
        return isinstance(refs, list) and any(
            isinstance(ref, dict) and ref.get("topic") == topic
            and ref.get("thread_key") == thread_key and ref.get("chat_id") == chat_id
            and (not notification_enabled_only or ref.get("topic_enabled") is True)
            for ref in refs
        )

    with db.get_conn() as conn:
        # 两个队列读取同一 SQLite 快照，不能把两次查询之间的交接误判为排空。
        conn.execute("BEGIN")
        jobs = conn.execute(
            "SELECT status,rules_json,notification_context_json FROM organize_probe_queue "
            "WHERE notification_context_json <> '{}'"
        ).fetchall()
        targets = conn.execute(
            "SELECT state,last_error,pending_changes_json,inflight_changes_json FROM strm_change_queue "
            "WHERE state <> 'completed'"
        ).fetchall()
    for job in jobs:
        if notification_enabled_only and not probe_rules_notify_enabled(job["rules_json"]):
            continue
        if not matches(job["notification_context_json"]):
            continue
        progress["total"] += 1
        status = str(job["status"])
        if status in {"queued", "retry_wait", "running"}:
            progress["pending"] += 1
        elif status in {"failed", "cancelled"}:
            progress[status] += 1
    for target in targets:
        for column in ("pending_changes_json", "inflight_changes_json"):
            try:
                changes = json.loads(target[column] or "[]")
            except (TypeError, ValueError):
                continue
            if isinstance(changes, list):
                matched = sum(
                    1 for change in changes if isinstance(change, dict)
                    and (not notification_enabled_only or change.get("_probe_notify_enabled", True) is True)
                    and matches(change.get("_probe_notification_context"))
                )
                progress["strm_pending"] += matched
                if target["state"] in {"failed", "retry_wait"} or target["last_error"]:
                    # 业务请求的初始 STRM 已完成，也不能抹掉这批后续交接的真实失败。
                    progress["strm_failed"] += matched
    return progress
