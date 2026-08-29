"""媒体订阅、候选资源与下载准入仓储。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from types import ModuleType


def _database() -> "ModuleType":
    """延迟取得数据库门面，保持测试数据库与时间补丁兼容。"""
    from app import database

    return database


def get_conn():
    return _database().get_conn()


def now() -> str:
    return _database().now()


def _json(value: Any, fallback: Any) -> str:
    payload = fallback if value is None else value
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _next_check(interval_minutes: int, *, base: str | None = None) -> str:
    if base:
        try:
            start = datetime.strptime(base, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            start = datetime.now()
    else:
        start = datetime.now()
    return (start + timedelta(minutes=max(5, min(int(interval_minutes or 60), 10080)))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def add_media_subscription(
    *,
    provider: str,
    external_id: str,
    tmdb_id: str,
    media_type: str,
    title: str,
    original_title: str = "",
    year: str = "",
    poster_key: str = "",
    enabled: bool = True,
    monitor_mode: str = "missing",
    seasons: Iterable[int] | None = None,
    include_specials: bool = False,
    action: str = "confirm",
    download_target: str = "guangya",
    sites: Iterable[str] | None = None,
    check_interval_minutes: int = 4320,
) -> int:
    stamp = now()
    interval = max(5, min(int(check_interval_minutes or 4320), 10080))
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO media_subscriptions("
            "provider,external_id,tmdb_id,media_type,title,original_title,year,poster_key,"
            "enabled,monitor_mode,seasons_json,include_specials,action,download_target,sites_json,"
            "check_interval_minutes,next_check_at,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                provider, external_id, tmdb_id, media_type, title, original_title, year, poster_key,
                int(bool(enabled)), monitor_mode, _json(list(seasons or []), []),
                int(bool(include_specials)), action, download_target, _json(list(sites or []), []),
                interval, stamp, stamp, stamp,
            ),
        )
        return int(cur.lastrowid)


def upsert_media_subscription(
    *,
    provider: str,
    external_id: str,
    tmdb_id: str,
    media_type: str,
    title: str,
    original_title: str = "",
    year: str = "",
    poster_key: str = "",
    enabled: bool = True,
    monitor_mode: str = "missing",
    seasons: Iterable[int] | None = None,
    include_specials: bool = False,
    action: str = "confirm",
    download_target: str = "guangya",
    sites: Iterable[str] | None = None,
    check_interval_minutes: int = 4320,
) -> tuple[int, bool]:
    """原子创建或恢复同一 TMDB 身份的订阅。"""
    stamp = now()
    interval = max(5, min(int(check_interval_minutes or 4320), 10080))
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id,revision FROM media_subscriptions WHERE tmdb_id=? AND media_type=?",
            (str(tmdb_id), str(media_type)),
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO media_subscriptions("
                "provider,external_id,tmdb_id,media_type,title,original_title,year,poster_key,"
                "enabled,monitor_mode,seasons_json,include_specials,action,download_target,sites_json,"
                "check_interval_minutes,next_check_at,status,revision,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    provider, external_id, tmdb_id, media_type, title, original_title, year, poster_key,
                    int(bool(enabled)), monitor_mode, _json(list(seasons or []), []),
                    int(bool(include_specials)), action, download_target, _json(list(sites or []), []),
                    interval, stamp, "new" if enabled else "paused", 1, stamp, stamp,
                ),
            )
            return int(cur.lastrowid), True

        subscription_id = int(row["id"])
        revision = int(row["revision"] or 1) + 1
        conn.execute(
            "UPDATE media_subscriptions SET provider=?,external_id=?,title=?,original_title=?,year=?,"
            "poster_key=?,enabled=?,monitor_mode=?,seasons_json=?,include_specials=?,action=?,"
            "download_target=?,sites_json=?,check_interval_minutes=?,next_check_at=?,status=?,"
            "last_error='',revision=?,deleted_at=NULL,updated_at=? WHERE id=?",
            (
                provider, external_id, title, original_title, year, poster_key, int(bool(enabled)),
                monitor_mode, _json(list(seasons or []), []), int(bool(include_specials)), action,
                download_target, _json(list(sites or []), []), interval, stamp,
                "new" if enabled else "paused", revision, stamp, subscription_id,
            ),
        )
        conn.execute(
            "UPDATE media_subscription_candidates SET status='expired',updated_at=? "
            "WHERE subscription_id=? AND status='available'",
            (stamp, subscription_id),
        )
        conn.execute(
            "UPDATE media_download_admissions SET status='cancelled',error=?,completed_at=?,updated_at=? "
            "WHERE subscription_id=? AND status='claimed'",
            ("订阅配置已变更", stamp, stamp, subscription_id),
        )
        conn.execute(
            "UPDATE media_subscription_runs SET status='cancelled',summary=?,error=?,finished_at=? "
            "WHERE subscription_id=? AND status='running'",
            ("订阅配置已变更，旧检查已取消", "订阅配置已变更", stamp, subscription_id),
        )
        return subscription_id, False


def get_media_subscription(subscription_id: int, *, include_deleted: bool = False) -> sqlite3.Row | None:
    deleted_clause = "" if include_deleted else " AND deleted_at IS NULL"
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM media_subscriptions WHERE id=?" + deleted_clause,
            (int(subscription_id),),
        ).fetchone()


def get_media_subscription_by_identity(tmdb_id: str, media_type: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM media_subscriptions WHERE tmdb_id=? AND media_type=? AND deleted_at IS NULL",
            (str(tmdb_id), str(media_type)),
        ).fetchone()


def list_media_subscriptions(
    *, status: str = "", enabled: bool | None = None, limit: int = 200, offset: int = 0
) -> list[sqlite3.Row]:
    clauses: list[str] = ["deleted_at IS NULL"]
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(str(status))
    if enabled is not None:
        clauses.append("enabled=?")
        params.append(int(bool(enabled)))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend((max(1, min(int(limit or 200), 500)), max(0, int(offset or 0))))
    with get_conn() as conn:
        return conn.execute(
            f"SELECT * FROM media_subscriptions{where} ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()


def list_media_subscription_workflows(
    subscription_ids: Iterable[int],
) -> dict[int, dict[str, int | None]]:
    """批量汇总候选与最新下载准入，供订阅列表展示真实工作流阶段。"""
    ids = sorted({int(value) for value in subscription_ids if int(value) > 0})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    result: dict[int, dict[str, int | None]] = {
        subscription_id: {
            "available_candidate_count": 0,
            "submitted_candidate_count": 0,
            "max_relevance_score": None,
            "submitted_count": 0,
            "downloading_count": 0,
            "processing_count": 0,
            "manual_review_count": 0,
            "failed_count": 0,
        }
        for subscription_id in ids
    }
    with get_conn() as conn:
        candidate_rows = conn.execute(
            "SELECT subscription_id,"
            "SUM(CASE WHEN status='available' "
            "AND datetime(expires_at)>datetime('now','localtime') THEN 1 ELSE 0 END) "
            "AS available_candidate_count,"
            "SUM(CASE WHEN status='submitted' THEN 1 ELSE 0 END) AS submitted_candidate_count,"
            "MAX(CASE WHEN status='available' "
            "AND datetime(expires_at)>datetime('now','localtime') "
            "THEN relevance_score ELSE NULL END) AS max_relevance_score "
            "FROM media_subscription_candidates "
            "WHERE (status='submitted' OR (status='available' "
            "AND datetime(expires_at)>datetime('now','localtime'))) "
            f"AND subscription_id IN ({placeholders}) GROUP BY subscription_id",
            ids,
        ).fetchall()
        admission_rows = conn.execute(
            "SELECT a.subscription_id,a.status AS admission_status,"
            "r.status AS request_status,r.qb_status,r.gy_status,r.organize_status,"
            "r.local_import_status,r.strm_status "
            "FROM media_download_admissions a "
            "LEFT JOIN download_requests r ON r.id=a.request_id "
            f"WHERE a.subscription_id IN ({placeholders}) AND a.id=("
            "SELECT a2.id FROM media_download_admissions a2 "
            "WHERE a2.subscription_id=a.subscription_id AND a2.media_key=a.media_key "
            "ORDER BY a2.id DESC LIMIT 1)",
            ids,
        ).fetchall()
    for row in candidate_rows:
        workflow = result[int(row["subscription_id"])]
        workflow["available_candidate_count"] = int(row["available_candidate_count"] or 0)
        workflow["submitted_candidate_count"] = int(row["submitted_candidate_count"] or 0)
        score = row["max_relevance_score"]
        workflow["max_relevance_score"] = int(score) if score is not None else None
    for row in admission_rows:
        workflow = result[int(row["subscription_id"])]
        admission_status = str(row["admission_status"] or "")
        request_status = str(row["request_status"] or "")
        # 准入已进入终态就代表这一集不再有进行中的工作。下载请求行可能仍
        # 停留在 submitted（例如网盘侧完成后没有回写），若继续按请求状态
        # 判定，已入库的订阅会永远显示「已推送，等待下载」。
        if admission_status in {"completed", "released", "cancelled"}:
            continue
        post_statuses = {
            str(row["organize_status"] or ""),
            str(row["local_import_status"] or ""),
            str(row["strm_status"] or ""),
        }
        backend_statuses = {str(row["qb_status"] or ""), str(row["gy_status"] or "")}
        if request_status == "manual_review" or "requires_manual" in post_statuses:
            workflow["manual_review_count"] += 1
        elif (
            admission_status == "failed"
            or request_status == "failed"
            or "failed" in post_statuses
        ):
            workflow["failed_count"] += 1
        elif admission_status == "processing" or request_status == "completed":
            workflow["processing_count"] += 1
        elif (
            admission_status == "downloading"
            or request_status == "downloading"
            or bool(backend_statuses & {"downloading", "completed", "outcome_unknown"})
        ):
            workflow["downloading_count"] += 1
        elif admission_status in {"claimed", "dispatching", "submitted"} or request_status in {
            "pending", "submitting", "submitted"
        }:
            workflow["submitted_count"] += 1
    return result


def count_media_subscriptions(*, status: str = "", enabled: bool | None = None) -> int:
    clauses: list[str] = ["deleted_at IS NULL"]
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(str(status))
    if enabled is not None:
        clauses.append("enabled=?")
        params.append(int(bool(enabled)))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS total FROM media_subscriptions{where}", params).fetchone()
        return int(row["total"] or 0)


def get_media_subscription_stats() -> dict[str, int]:
    """一次聚合订阅与当前可用候选统计，避免看板 N+1 明细查询。"""
    expire_media_subscription_candidates()
    with get_conn() as conn:
        subscriptions = conn.execute(
            "SELECT COUNT(*) AS media_total,"
            "SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS media_active,"
            "SUM(CASE WHEN status='missing' THEN 1 ELSE 0 END) AS media_missing,"
            "SUM(CASE WHEN status IN ('inconclusive','error') THEN 1 ELSE 0 END) "
            "AS media_inconclusive FROM media_subscriptions WHERE deleted_at IS NULL"
        ).fetchone()
        candidates = conn.execute(
            "SELECT COUNT(*) AS candidate_total FROM media_subscription_candidates c "
            "JOIN media_subscriptions s ON s.id=c.subscription_id "
            "WHERE c.status='available' AND s.deleted_at IS NULL"
        ).fetchone()
    return {
        "media_total": int(subscriptions["media_total"] or 0),
        "media_active": int(subscriptions["media_active"] or 0),
        "media_missing": int(subscriptions["media_missing"] or 0),
        "media_inconclusive": int(subscriptions["media_inconclusive"] or 0),
        "candidate_total": int(candidates["candidate_total"] or 0),
    }


def update_media_subscription(subscription_id: int, **fields: Any) -> bool:
    allowed = {
        "provider", "external_id", "title", "original_title", "year", "poster_key",
        "enabled", "monitor_mode", "seasons_json", "include_specials", "action",
        "download_target", "sites_json", "check_interval_minutes", "last_checked_at",
        "next_check_at", "status", "expected_count", "local_count", "missing_count",
        "missing_json", "result_json", "last_error",
    }
    assignments: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        assignments.append(f"{key}=?")
        params.append(value)
    if not assignments:
        return False
    assignments.append("updated_at=?")
    params.extend((now(), int(subscription_id)))
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE media_subscriptions SET {','.join(assignments)} WHERE id=? AND deleted_at IS NULL", params
        )
        return bool(cur.rowcount)


def update_media_subscription_config(subscription_id: int, **fields: Any) -> bool:
    """更新用户配置并失效旧检查、候选和尚未派发的下载准入。"""
    allowed = {
        "enabled", "monitor_mode", "seasons_json", "include_specials", "action",
        "download_target", "sites_json", "check_interval_minutes", "next_check_at",
        "status", "last_error",
    }
    assignments: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key in allowed:
            assignments.append(f"{key}=?")
            params.append(value)
    if not assignments:
        return False
    stamp = now()
    assignments.extend(("revision=revision+1", "updated_at=?"))
    params.extend((stamp, int(subscription_id)))
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            f"UPDATE media_subscriptions SET {','.join(assignments)} "
            "WHERE id=? AND deleted_at IS NULL",
            params,
        )
        if cur.rowcount != 1:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE media_subscription_candidates SET status='expired',updated_at=? "
            "WHERE subscription_id=? AND status='available'",
            (stamp, int(subscription_id)),
        )
        conn.execute(
            "UPDATE media_download_admissions SET status='cancelled',error=?,completed_at=?,updated_at=? "
            "WHERE subscription_id=? AND status='claimed'",
            ("订阅配置已变更", stamp, stamp, int(subscription_id)),
        )
        conn.execute(
            "UPDATE media_subscription_runs SET status='cancelled',summary=?,error=?,finished_at=? "
            "WHERE subscription_id=? AND status='running'",
            ("订阅配置已变更，旧检查已取消", "订阅配置已变更", stamp, int(subscription_id)),
        )
        return True


def schedule_media_subscription(subscription_id: int, interval_minutes: int) -> bool:
    return update_media_subscription(
        subscription_id,
        next_check_at=_next_check(interval_minutes),
    )


def delete_media_subscription(subscription_id: int) -> bool:
    """软删除订阅，保留已经发生的下载审计和运行记录。"""
    stamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE media_subscriptions SET enabled=0,status='paused',deleted_at=?,revision=revision+1,"
            "updated_at=? WHERE id=? AND deleted_at IS NULL",
            (stamp, stamp, int(subscription_id)),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE media_subscription_candidates SET status='expired',updated_at=? "
            "WHERE subscription_id=? AND status='available'",
            (stamp, int(subscription_id)),
        )
        conn.execute(
            "UPDATE media_download_admissions SET status='cancelled',error=?,completed_at=?,updated_at=? "
            "WHERE subscription_id=? AND status='claimed'",
            ("订阅已删除", stamp, stamp, int(subscription_id)),
        )
        conn.execute(
            "UPDATE media_subscription_runs SET status='cancelled',summary=?,error=?,finished_at=? "
            "WHERE subscription_id=? AND status='running'",
            ("订阅已删除，检查已取消", "订阅已删除", stamp, int(subscription_id)),
        )
        return bool(cur.rowcount)


def list_due_media_subscriptions(limit: int = 20) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM media_subscriptions WHERE deleted_at IS NULL AND enabled=1 "
            "AND status!='checking' AND (next_check_at IS NULL OR next_check_at='' "
            "OR datetime(next_check_at)<=datetime('now','localtime')) "
            "ORDER BY COALESCE(next_check_at,created_at),id LIMIT ?",
            (max(1, min(int(limit or 20), 100)),),
        ).fetchall()


def claim_media_subscription_check(subscription_id: int) -> bool:
    stamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE media_subscriptions SET status='checking',updated_at=? "
            "WHERE id=? AND enabled=1 AND status!='checking'",
            (stamp, int(subscription_id)),
        )
        return cur.rowcount == 1


def claim_media_subscription_check_run(
    subscription_id: int, trigger_type: str = "manual"
) -> int | None:
    """原子认领一次检查并创建运行记录，避免 checking 无运行记录。"""
    trigger = str(trigger_type or "manual").strip().lower()
    if trigger not in {"manual", "scheduler", "watchlist", "retry"}:
        trigger = "manual"
    stamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        subscription = conn.execute(
            "SELECT revision FROM media_subscriptions WHERE id=? AND deleted_at IS NULL "
            "AND enabled=1 AND status!='checking'",
            (int(subscription_id),),
        ).fetchone()
        if subscription is None:
            conn.rollback()
            return None
        revision = int(subscription["revision"] or 1)
        cur = conn.execute(
            "UPDATE media_subscriptions SET status='checking',updated_at=? "
            "WHERE id=? AND deleted_at IS NULL AND enabled=1 AND status!='checking' AND revision=?",
            (stamp, int(subscription_id), revision),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return None
        run = conn.execute(
            "INSERT INTO media_subscription_runs("
            "subscription_id,trigger_type,subscription_revision,status,started_at"
            ") VALUES(?,?,?,'running',?)",
            (int(subscription_id), trigger, revision, stamp),
        )
        return int(run.lastrowid)


def media_subscription_check_is_active(
    subscription_id: int,
    subscription_revision: int | None = None,
    *,
    run_id: int | None = None,
) -> bool:
    """确认检查租约仍归当前 run，并顺带刷新 stale recovery 心跳。"""
    clauses = "id=? AND deleted_at IS NULL AND enabled=1 AND status='checking'"
    params: list[Any] = [now(), int(subscription_id)]
    if subscription_revision is not None:
        clauses += " AND revision=?"
        params.append(int(subscription_revision))
    if run_id is not None:
        clauses += (
            " AND EXISTS (SELECT 1 FROM media_subscription_runs r "
            "WHERE r.id=? AND r.subscription_id=media_subscriptions.id "
            "AND r.subscription_revision=media_subscriptions.revision "
            "AND r.status='running')"
        )
        params.append(int(run_id))
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE media_subscriptions SET updated_at=? WHERE {clauses}",
            params,
        )
        return cur.rowcount == 1


def recover_stale_media_subscription_checks(*, stale_minutes: int = 30) -> int:
    """恢复因进程退出而遗留的 checking 状态，避免订阅永久停摆。"""
    minutes = max(5, min(int(stale_minutes or 30), 1440))
    stamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        stale_rows = conn.execute(
            "SELECT id FROM media_subscriptions WHERE deleted_at IS NULL AND status='checking' "
            "AND datetime(updated_at)<=datetime('now','localtime',?)",
            (f"-{minutes} minutes",),
        ).fetchall()
        stale_ids = [int(row["id"]) for row in stale_rows]
        if not stale_ids:
            conn.rollback()
            return 0
        placeholders = ",".join("?" for _ in stale_ids)
        cur = conn.execute(
            "UPDATE media_subscriptions SET status='error',last_error=?,next_check_at=?,updated_at=? "
            f"WHERE id IN ({placeholders}) AND status='checking'",
            (
                "上次检查在进程退出前中断，已自动恢复",
                stamp,
                stamp,
                *stale_ids,
            ),
        )
        conn.execute(
            "UPDATE media_subscription_runs SET status='failed',summary=?,error=?,finished_at=? "
            f"WHERE subscription_id IN ({placeholders}) AND status='running'",
            (
                "上次检查在进程退出前中断，已自动恢复",
                "检查因进程退出而中断",
                stamp,
                *stale_ids,
            ),
        )
        return int(cur.rowcount or 0)


def add_media_subscription_run(subscription_id: int, trigger_type: str = "manual") -> int:
    with get_conn() as conn:
        subscription = conn.execute(
            "SELECT revision FROM media_subscriptions WHERE id=? AND deleted_at IS NULL",
            (int(subscription_id),),
        ).fetchone()
        if subscription is None:
            raise ValueError("媒体订阅不存在")
        cur = conn.execute(
            "INSERT INTO media_subscription_runs("
            "subscription_id,trigger_type,subscription_revision,status,started_at"
            ") VALUES(?,?,?,'running',?)",
            (
                int(subscription_id),
                str(trigger_type),
                int(subscription["revision"] or 1),
                now(),
            ),
        )
        return int(cur.lastrowid)


def finish_media_subscription_run(
    run_id: int, *, status: str, summary: str = "", payload: Any = None, error: str = ""
) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE media_subscription_runs SET status=?,summary=?,payload_json=?,error=?,finished_at=? "
            "WHERE id=? AND status='running'",
            (str(status), str(summary), _json(payload, {}), str(error), now(), int(run_id)),
        )
        return cur.rowcount == 1


def _enqueue_run_notification(
    conn: Any,
    *,
    subscription_id: int,
    run_id: int,
    subscription_revision: int,
    event_type: str,
    payload: dict[str, Any],
    stamp: str,
) -> bool:
    flag = {
        "missing": "notify_on_missing",
        "satisfied": "notify_on_satisfied",
        "error": "notify_on_error",
        "inconclusive": "notify_on_error",
    }.get(str(event_type))
    if flag is None:
        return False
    rule = conn.execute(
        f"SELECT enabled,{flag} AS event_enabled "
        "FROM media_subscription_notification_rules WHERE subscription_id=?",
        (int(subscription_id),),
    ).fetchone()
    if rule is None or not int(rule["enabled"] or 0) or not int(rule["event_enabled"] or 0):
        return False
    event_key = f"media-subscription-run:{int(run_id)}:{event_type}"
    encoded = _json(payload, {})
    cur = conn.execute(
        "INSERT INTO media_subscription_notification_outbox("
        "event_key,subscription_id,subscription_revision,run_id,event_type,payload_json,"
        "status,attempts,lease_generation,next_attempt_at,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,'pending',0,0,?,?,?) "
        "ON CONFLICT(event_key) DO NOTHING",
        (
            event_key, int(subscription_id), int(subscription_revision), int(run_id),
            str(event_type), encoded, stamp, stamp, stamp,
        ),
    )
    return cur.rowcount == 1


def finalize_media_subscription_check(
    subscription_id: int,
    run_id: int,
    *,
    status: str,
    run_status: str,
    summary: str,
    payload: Any,
    interval_minutes: int,
    expected_count: int,
    local_count: int,
    missing_count: int,
    missing_json: str,
    result_json: str,
    subscription_revision: int,
) -> bool:
    """仅在订阅仍处于启用 checking 状态时提交检查结果。"""
    stamp = now()
    next_check = _next_check(interval_minutes, base=stamp)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE media_subscriptions SET status=?,expected_count=?,local_count=?,missing_count=?,"
            "missing_json=?,result_json=?,last_checked_at=?,next_check_at=?,last_error='',updated_at=? "
            "WHERE id=? AND deleted_at IS NULL AND enabled=1 AND status='checking' "
            "AND revision=? AND EXISTS ("
            "SELECT 1 FROM media_subscription_runs r WHERE r.id=? "
            "AND r.subscription_id=media_subscriptions.id "
            "AND r.subscription_revision=media_subscriptions.revision AND r.status='running')",
            (
                str(status), int(expected_count), int(local_count), int(missing_count),
                str(missing_json), str(result_json), stamp, next_check, stamp,
                int(subscription_id), int(subscription_revision), int(run_id),
            ),
        )
        committed = cur.rowcount == 1
        final_status = str(run_status) if committed else "cancelled"
        final_summary = str(summary) if committed else "订阅在检查期间被暂停或配置已变更"
        final_error = "" if committed else final_summary
        conn.execute(
            "UPDATE media_subscription_runs SET status=?,summary=?,payload_json=?,error=?,finished_at=? "
            "WHERE id=? AND status='running'",
            (final_status, final_summary, _json(payload, {}), final_error, stamp, int(run_id)),
        )
        if committed and final_status in {"missing", "satisfied", "inconclusive"}:
            subscription = conn.execute(
                "SELECT title,action FROM media_subscriptions WHERE id=?",
                (int(subscription_id),),
            ).fetchone()
            result_payload = payload if isinstance(payload, dict) else {}
            _enqueue_run_notification(
                conn,
                subscription_id=subscription_id,
                run_id=run_id,
                subscription_revision=subscription_revision,
                event_type=final_status,
                payload={
                    "subscription_number": int(subscription_id),
                    "title": str(subscription["title"] or "") if subscription else "",
                    "status": final_status,
                    "expected_count": int(expected_count),
                    "local_count": int(local_count),
                    "missing_count": int(missing_count),
                    "summary": str(summary or "")[:300],
                    "action": str(
                        result_payload.get("action")
                        or (subscription["action"] if subscription else "notify")
                        or "notify"
                    ),
                    "candidate_count": max(
                        0, int(result_payload.get("candidate_count") or 0)
                    ),
                    "auto_submitted": max(
                        0, int(result_payload.get("auto_submitted") or 0)
                    ),
                },
                stamp=stamp,
            )
        return committed


def fail_media_subscription_check(
    subscription_id: int, run_id: int, *, interval_minutes: int, error: str,
    subscription_revision: int,
) -> bool:
    """结束失败检查，但不覆盖用户在检查期间设置的 paused 状态。"""
    stamp = now()
    next_check = _next_check(interval_minutes, base=stamp)
    message = str(error or "媒体订阅巡检失败")[:500]
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE media_subscriptions SET status='error',last_error=?,last_checked_at=?,"
            "next_check_at=?,updated_at=? WHERE id=? AND deleted_at IS NULL AND enabled=1 "
            "AND status='checking' AND revision=? AND EXISTS ("
            "SELECT 1 FROM media_subscription_runs r WHERE r.id=? "
            "AND r.subscription_id=media_subscriptions.id "
            "AND r.subscription_revision=media_subscriptions.revision AND r.status='running')",
            (
                message, stamp, next_check, stamp, int(subscription_id),
                int(subscription_revision), int(run_id),
            ),
        )
        committed = cur.rowcount == 1
        run_status = "failed" if committed else "cancelled"
        run_message = message if committed else "订阅在检查期间被暂停或配置已变更"
        conn.execute(
            "UPDATE media_subscription_runs SET status=?,summary=?,error=?,finished_at=? "
            "WHERE id=? AND status='running'",
            (run_status, run_message, run_message, stamp, int(run_id)),
        )
        if committed:
            subscription = conn.execute(
                "SELECT title FROM media_subscriptions WHERE id=?",
                (int(subscription_id),),
            ).fetchone()
            _enqueue_run_notification(
                conn,
                subscription_id=subscription_id,
                run_id=run_id,
                subscription_revision=subscription_revision,
                event_type="error",
                payload={
                    "subscription_number": int(subscription_id),
                    "title": str(subscription["title"] or "") if subscription else "",
                    "status": "error",
                },
                stamp=stamp,
            )
        return committed


def cancel_media_subscription_run(
    run_id: int,
    *,
    subscription_id: int,
    subscription_revision: int,
    reason: str,
) -> bool:
    """原子取消当前检查，并释放仍归该 run 持有的 checking 租约。"""
    message = str(reason or "订阅检查已取消")[:500]
    stamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE media_subscriptions SET status='new',last_error='',next_check_at=?,updated_at=? "
            "WHERE id=? AND deleted_at IS NULL AND enabled=1 AND status='checking' "
            "AND revision=? AND EXISTS ("
            "SELECT 1 FROM media_subscription_runs r WHERE r.id=? "
            "AND r.subscription_id=media_subscriptions.id "
            "AND r.subscription_revision=media_subscriptions.revision AND r.status='running')",
            (
                stamp,
                stamp,
                int(subscription_id),
                int(subscription_revision),
                int(run_id),
            ),
        )
        cur = conn.execute(
            "UPDATE media_subscription_runs SET status='cancelled',summary=?,error=?,finished_at=? "
            "WHERE id=? AND subscription_id=? AND subscription_revision=? AND status='running'",
            (
                message,
                message,
                stamp,
                int(run_id),
                int(subscription_id),
                int(subscription_revision),
            ),
        )
        return cur.rowcount == 1


def list_media_subscription_runs(
    *, subscription_id: int | None = None, limit: int = 100, offset: int = 0
) -> list[sqlite3.Row]:
    where = " WHERE r.subscription_id=?" if subscription_id else ""
    params: list[Any] = [int(subscription_id)] if subscription_id else []
    params.extend((max(1, min(int(limit or 100), 300)), max(0, int(offset or 0))))
    with get_conn() as conn:
        return conn.execute(
            "SELECT r.*,s.title,s.media_type,s.tmdb_id FROM media_subscription_runs r "
            "JOIN media_subscriptions s ON s.id=r.subscription_id"
            f"{where} ORDER BY r.id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()


def expire_media_subscription_candidates(subscription_id: int | None = None) -> int:
    params: list[Any] = [now()]
    clause = ""
    if subscription_id is not None:
        clause = " AND subscription_id=?"
        params.append(int(subscription_id))
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE media_subscription_candidates SET status='expired',updated_at=? "
            "WHERE status='available' AND datetime(expires_at)<=datetime('now','localtime')" + clause,
            params,
        )
        return int(cur.rowcount or 0)


def replace_media_subscription_candidates(
    subscription_id: int,
    media_key: str,
    *,
    season: int | None,
    episode: int | None,
    candidates: Iterable[dict[str, Any]],
    expires_at: str,
) -> list[int]:
    stamp = now()
    ids: list[int] = []
    with get_conn() as conn:
        conn.execute(
            "UPDATE media_subscription_candidates SET status='expired',updated_at=? "
            "WHERE subscription_id=? AND media_key=? AND status='available'",
            (stamp, int(subscription_id), str(media_key)),
        )
        for item in candidates:
            result_id = str(item.get("result_id") or "")
            conn.execute(
                "INSERT INTO media_subscription_candidates("
                "subscription_id,media_key,season,episode,result_id,site_id,site_name,title,"
                "size_text,size_bytes,seeders,published_at,relevance_score,download_state,"
                "match_reasons_json,status,expires_at,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'available',?,?,?) "
                "ON CONFLICT(subscription_id,media_key,result_id) DO UPDATE SET "
                "season=excluded.season,episode=excluded.episode,site_id=excluded.site_id,"
                "site_name=excluded.site_name,title=excluded.title,size_text=excluded.size_text,"
                "size_bytes=excluded.size_bytes,seeders=excluded.seeders,"
                "published_at=excluded.published_at,relevance_score=excluded.relevance_score,"
                "download_state=excluded.download_state,match_reasons_json=excluded.match_reasons_json,"
                "status=CASE WHEN media_subscription_candidates.status IN ('submitted','dismissed') "
                "THEN media_subscription_candidates.status ELSE 'available' END,"
                "expires_at=excluded.expires_at,updated_at=excluded.updated_at",
                (
                    int(subscription_id), str(media_key), season, episode,
                    result_id, str(item.get("site_id") or ""),
                    str(item.get("site_name") or ""), str(item.get("title") or ""),
                    str(item.get("size_text") or ""), item.get("size_bytes"), item.get("seeders"),
                    str(item.get("published_at") or "") or None, item.get("relevance_score"),
                    str(item.get("download_state") or "unavailable"),
                    _json(item.get("match_reasons") or [], []), expires_at, stamp, stamp,
                ),
            )
            row = conn.execute(
                "SELECT id FROM media_subscription_candidates "
                "WHERE subscription_id=? AND media_key=? AND result_id=?",
                (int(subscription_id), str(media_key), result_id),
            ).fetchone()
            if row is not None:
                ids.append(int(row["id"]))
    return ids


def list_media_subscription_candidates_by_ids(
    candidate_ids: Iterable[int],
) -> list[sqlite3.Row]:
    """批量读取候选当前状态，保持自动选择与刷新结果的身份对应。"""
    normalized = list(dict.fromkeys(
        int(candidate_id) for candidate_id in candidate_ids if int(candidate_id) > 0
    ))[:200]
    if not normalized:
        return []
    placeholders = ",".join("?" for _ in normalized)
    with get_conn() as conn:
        return conn.execute(
            "SELECT id,result_id,status FROM media_subscription_candidates "
            f"WHERE id IN ({placeholders})",
            normalized,
        ).fetchall()


def get_media_subscription_candidate(candidate_id: int) -> sqlite3.Row | None:
    expire_media_subscription_candidates()
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM media_subscription_candidates WHERE id=?", (int(candidate_id),)
        ).fetchone()


def list_media_subscription_candidates(
    subscription_id: int, *, status: str = "available", limit: int = 200
) -> list[sqlite3.Row]:
    expire_media_subscription_candidates(subscription_id)
    params: list[Any] = [int(subscription_id)]
    clause = ""
    if status:
        clause = " AND status=?"
        params.append(str(status))
    params.append(max(1, min(int(limit or 200), 500)))
    with get_conn() as conn:
        return conn.execute(
            "SELECT c.*,a.id AS delivery_admission_id,a.status AS delivery_status,"
            "a.error AS delivery_error,a.request_id AS delivery_request_id,"
            "r.status AS delivery_request_status,r.qb_status AS delivery_qb_status,"
            "r.gy_status AS delivery_gy_status,r.organize_status AS delivery_organize_status,"
            "r.local_import_status AS delivery_local_import_status,"
            "r.strm_status AS delivery_strm_status,r.error AS delivery_request_error "
            "FROM media_subscription_candidates c "
            "LEFT JOIN media_download_admissions a ON a.id=("
            "SELECT a2.id FROM media_download_admissions a2 "
            "WHERE a2.candidate_id=c.id ORDER BY a2.id DESC LIMIT 1) "
            "LEFT JOIN download_requests r ON r.id=COALESCE(a.request_id,c.request_id) "
            "WHERE c.subscription_id=?" + clause.replace("status", "c.status") +
            " ORDER BY c.media_key,c.relevance_score DESC,c.seeders DESC,c.id DESC LIMIT ?",
            params,
        ).fetchall()


def update_media_subscription_candidate(candidate_id: int, **fields: Any) -> bool:
    allowed = {"status", "request_id", "expires_at"}
    assignments: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key in allowed:
            assignments.append(f"{key}=?")
            params.append(value)
    if not assignments:
        return False
    assignments.append("updated_at=?")
    params.extend((now(), int(candidate_id)))
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE media_subscription_candidates SET {','.join(assignments)} WHERE id=?", params
        )
        return bool(cur.rowcount)


def list_active_media_download_admissions(subscription_id: int | None = None) -> list[sqlite3.Row]:
    params: list[Any] = []
    clause = ""
    if subscription_id is not None:
        clause = " AND subscription_id=?"
        params.append(int(subscription_id))
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM media_download_admissions WHERE status IN "
            "('claimed','dispatching','submitted','downloading','processing')" + clause + " ORDER BY id",
            params,
        ).fetchall()


def _sync_media_download_admission_for_request_conn(
    conn: sqlite3.Connection,
    request_id: int,
    stamp: str,
) -> int:
    """使用调用方连接投影请求状态，供 tracker 的原子写路径复用。"""
    try:
        request = conn.execute(
            "SELECT status,error,organize_started,organize_status,organize_error,"
            "strm_status,strm_error,local_import_status,local_import_error "
            "FROM download_requests WHERE id=?", (int(request_id),)
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return 0
        raise
    if request is None:
        return 0
    request_status = str(request["status"] or "")
    status = ""
    completed_at = None
    error = ""
    if request_status == "failed":
        status = "failed"
        completed_at = stamp
        error = str(request["error"] or "下载请求失败")[:500]
    elif request_status == "completed":
        failed_stage = next((
            (label, str(request[error_key] or ""))
            for state_key, error_key, label in (
                ("organize_status", "organize_error", "自动整理"),
                ("local_import_status", "local_import_error", "本地入库"),
                ("strm_status", "strm_error", "STRM 联动"),
            )
            if str(request[state_key] or "") == "failed"
        ), None)
        if failed_stage is None and int(request["organize_started"] or 0) < 0:
            failed_stage = ("自动整理", str(request["organize_error"] or ""))
        if failed_stage is not None:
            label, detail = failed_stage
            status = "failed"
            completed_at = stamp
            error = f"下载后处理失败（{label}）：{detail or '请在下载记录中重试'}"[:500]
        else:
            status = "processing"
    elif request_status in {"submitted", "downloading"}:
        status = request_status
    elif request_status == "manual_review":
        # 结果未知时继续占用 media_key，防止用户重复提交造成双任务。
        status = "processing"
        error = str(request["error"] or "下载任务需要人工核验")[:500]
    else:
        return 0
    cur = conn.execute(
        "UPDATE media_download_admissions SET status=?,error=?,completed_at=?,updated_at=? "
        "WHERE request_id=? AND status IN "
        "('claimed','dispatching','submitted','downloading','processing')",
        (status, error, completed_at, stamp, int(request_id)),
    )
    return int(cur.rowcount or 0)


def sync_media_download_admission_for_request(request_id: int) -> int:
    """将下载请求的当前根状态即时投影到仍活跃的媒体订阅准入。"""
    database = _database()
    stamp = database.now()
    with database.get_conn() as conn:
        return _sync_media_download_admission_for_request_conn(
            conn, int(request_id), stamp
        )


def reconcile_startup_media_download_admissions(
    *, stale_seconds: int = 900
) -> tuple[int, int]:
    """启动时恢复崩溃窗口中的准入锁，并投影已绑定请求的真实状态。

    ``pending`` 请求尚未领取任何下载后端，可安全释放对应准入；下一次提交会
    复用该请求。进入 ``submitting`` 后的请求由数据库启动迁移改为
    ``manual_review``，这里会继续保持媒体锁，避免重复后端任务。
    """
    database = _database()
    stamp = database.now()
    # 保留参数兼容旧调用；启动恢复只处理尚未提交到任何后端的状态，
    # 不需要等待 stale 窗口，否则刚创建 pending 请求后重启会永久占住 media_key。
    _ = stale_seconds
    projected = 0
    released = 0
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            request_rows = conn.execute(
                "SELECT DISTINCT request_id FROM media_download_admissions "
                "WHERE request_id IS NOT NULL AND status IN "
                "('claimed','dispatching','submitted','downloading','processing')"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return 0, 0
            raise
        for row in request_rows:
            projected += _sync_media_download_admission_for_request_conn(
                conn, int(row["request_id"]), stamp
            )

        stale = conn.execute(
            "UPDATE media_download_admissions SET status='released',error=?,"
            "completed_at=?,updated_at=? WHERE status IN ('claimed','dispatching') "
            "AND (request_id IS NULL OR EXISTS ("
            "SELECT 1 FROM download_requests r WHERE r.id=media_download_admissions.request_id "
            "AND r.status='pending'))",
            (
                "启动恢复：下载尚未提交到后端，可安全重试",
                stamp,
                stamp,
            ),
        )
        released = int(stale.rowcount or 0)
    return projected, released


def reconcile_media_download_admissions(
    subscription_id: int,
    local_keys: Iterable[str],
    *,
    expected_revision: int | None = None,
) -> int:
    """单连接批量读取并以状态/revision 守卫同步下载准入状态。"""
    keys = {str(value) for value in local_keys if str(value)}
    database = _database()
    stamp = database.now()
    updated = 0
    with database.get_conn() as conn:
        revision_clause = ""
        query_params: list[Any] = [int(subscription_id)]
        if expected_revision is not None:
            revision_clause = " AND s.revision=?"
            query_params.append(int(expected_revision))
        rows = conn.execute(
            "SELECT a.id,a.media_key,a.request_id,a.status AS admission_status,"
            "r.id AS request_exists,r.status AS request_status,r.error AS request_error,"
            "r.organize_started,r.organize_status,r.organize_error,"
            "r.strm_status,r.strm_error,r.local_import_status,r.local_import_error "
            "FROM media_download_admissions a "
            "JOIN media_subscriptions s ON s.id=a.subscription_id "
            "LEFT JOIN download_requests r ON r.id=a.request_id "
            "WHERE a.subscription_id=? AND s.deleted_at IS NULL" + revision_clause + " "
            "AND a.status IN "
            "('claimed','dispatching','submitted','downloading','processing') "
            "ORDER BY a.id",
            query_params,
        ).fetchall()
        updates: list[tuple[Any, ...]] = []
        for row in rows:
            admission_id = int(row["id"])
            media_key = str(row["media_key"] or "")
            status = ""
            error = ""
            completed_at = None
            if media_key in keys:
                status = "completed"
                completed_at = stamp
            else:
                request_id = int(row["request_id"] or 0)
                if not request_id:
                    continue
                if row["request_exists"] is None:
                    status = "failed"
                    error = "下载请求不存在"
                else:
                    request_status = str(row["request_status"] or "")
                    if request_status == "failed":
                        status = "failed"
                        error = str(row["request_error"] or "下载请求失败")[:500]
                    elif request_status == "completed":
                        failed_stage = next((
                            (label, str(row[error_key] or ""))
                            for state_key, error_key, label in (
                                ("organize_status", "organize_error", "自动整理"),
                                ("local_import_status", "local_import_error", "本地入库"),
                                ("strm_status", "strm_error", "STRM 联动"),
                            )
                            if str(row[state_key] or "") == "failed"
                        ), None)
                        if failed_stage is None and int(row["organize_started"] or 0) < 0:
                            failed_stage = ("自动整理", str(row["organize_error"] or ""))
                        if failed_stage is not None:
                            label, detail = failed_stage
                            status = "failed"
                            completed_at = stamp
                            error = (
                                f"下载后处理失败（{label}）：{detail or '请在下载记录中重试'}"
                            )[:500]
                        else:
                            status = "processing"
                    elif request_status in {"submitted", "downloading"}:
                        status = request_status
                    else:
                        continue
            updates.append((
                status,
                error,
                completed_at,
                stamp,
                admission_id,
                str(row["admission_status"] or ""),
                int(subscription_id),
                int(expected_revision) if expected_revision is not None else None,
                int(expected_revision) if expected_revision is not None else None,
            ))
        if updates:
            before = conn.total_changes
            conn.executemany(
                "UPDATE media_download_admissions "
                "SET status=?,error=?,completed_at=?,updated_at=? "
                "WHERE id=? AND status=? AND EXISTS ("
                "SELECT 1 FROM media_subscriptions s "
                "WHERE s.id=? AND s.deleted_at IS NULL "
                "AND (? IS NULL OR s.revision=?)"
                ")",
                updates,
            )
            updated = conn.total_changes - before
    return updated


def claim_media_download_admission(
    *,
    media_key: str,
    tmdb_id: str,
    media_type: str,
    subscription_id: int,
    candidate_id: int,
    season: int | None,
    episode: int | None,
    subscription_revision: int,
    require_active_check: bool = False,
    check_run_id: int | None = None,
) -> int | None:
    stamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        status_clause = ""
        active_params: list[Any] = [
            int(subscription_id), int(subscription_revision), int(candidate_id)
        ]
        if require_active_check:
            status_clause = " AND s.status='checking'"
            if check_run_id is not None:
                status_clause += (
                    " AND EXISTS (SELECT 1 FROM media_subscription_runs r WHERE r.id=? "
                    "AND r.subscription_id=s.id AND r.subscription_revision=s.revision "
                    "AND r.status='running')"
                )
                active_params.append(int(check_run_id))
        active = conn.execute(
            "SELECT 1 FROM media_subscriptions s JOIN media_subscription_candidates c "
            "ON c.subscription_id=s.id WHERE s.id=? AND s.deleted_at IS NULL AND s.enabled=1 "
            "AND s.revision=? AND c.id=? AND c.status='available'" + status_clause,
            active_params,
        ).fetchone()
        if active is None:
            conn.rollback()
            return 0
        duplicate = conn.execute(
            "SELECT 1 FROM media_download_admissions WHERE media_key=? AND status IN "
            "('claimed','dispatching','submitted','downloading','processing')",
            (str(media_key),),
        ).fetchone()
        if duplicate is not None:
            conn.rollback()
            return None
        cur = conn.execute(
            "INSERT INTO media_download_admissions("
            "media_key,tmdb_id,media_type,season,episode,subscription_id,subscription_revision,"
            "candidate_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'claimed',?,?)",
            (
                str(media_key), str(tmdb_id), str(media_type), season, episode,
                int(subscription_id), int(subscription_revision), int(candidate_id), stamp, stamp,
            ),
        )
        return int(cur.lastrowid)


def begin_media_download_dispatch(
    admission_id: int,
    *,
    subscription_id: int,
    subscription_revision: int,
    require_active_check: bool = False,
    check_run_id: int | None = None,
) -> bool:
    """在外部副作用前原子领取下载；领取后该次提交不可再撤销。"""
    status_clause = ""
    stamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        active_params: list[Any] = [int(subscription_id), int(subscription_revision)]
        if require_active_check:
            status_clause = " AND status='checking'"
            if check_run_id is not None:
                status_clause += (
                    " AND EXISTS (SELECT 1 FROM media_subscription_runs r WHERE r.id=? "
                    "AND r.subscription_id=media_subscriptions.id "
                    "AND r.subscription_revision=media_subscriptions.revision "
                    "AND r.status='running')"
                )
                active_params.append(int(check_run_id))
        active = conn.execute(
            "SELECT 1 FROM media_subscriptions WHERE id=? AND deleted_at IS NULL AND enabled=1 "
            "AND revision=?" + status_clause,
            active_params,
        ).fetchone()
        if active is None:
            conn.execute(
                "UPDATE media_download_admissions SET status='cancelled',error=?,completed_at=?,updated_at=? "
                "WHERE id=? AND status='claimed'",
                ("订阅已暂停、删除或配置已变更", stamp, stamp, int(admission_id)),
            )
            return False
        cur = conn.execute(
            "UPDATE media_download_admissions SET status='dispatching',updated_at=? "
            "WHERE id=? AND subscription_id=? AND subscription_revision=? AND status='claimed'",
            (stamp, int(admission_id), int(subscription_id), int(subscription_revision)),
        )
        return cur.rowcount == 1


def update_media_download_admission(
    admission_id: int,
    *,
    expected_statuses: Iterable[str] | None = None,
    **fields: Any,
) -> bool:
    """更新准入；调用方可用 expected_statuses 防止迟到结果覆盖终态。"""
    allowed = {"status", "candidate_id", "request_id", "error", "completed_at"}
    assignments: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key in allowed:
            assignments.append(f"{key}=?")
            params.append(value)
    if not assignments:
        return False
    assignments.append("updated_at=?")
    params.append(now())
    where = "id=?"
    params.append(int(admission_id))
    normalized_statuses = tuple(dict.fromkeys(
        str(value or "").strip() for value in (expected_statuses or ())
        if str(value or "").strip()
    ))
    if normalized_statuses:
        placeholders = ",".join("?" for _ in normalized_statuses)
        where += f" AND status IN ({placeholders})"
        params.extend(normalized_statuses)
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE media_download_admissions SET {','.join(assignments)} WHERE {where}",
            params,
        )
        return bool(cur.rowcount)


def fail_unbound_media_download_admission(admission_id: int, error: str) -> bool:
    """仅释放尚未绑定请求的准入；一旦有关联请求便保守保持防重锁。"""
    stamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE media_download_admissions SET status='failed',error=?,"
            "completed_at=?,updated_at=? WHERE id=? AND request_id IS NULL "
            "AND status IN ('claimed','dispatching')",
            (str(error or "")[:500], stamp, stamp, int(admission_id)),
        )
        return bool(cur.rowcount)


def complete_media_download_admissions(media_keys: Iterable[str]) -> int:
    keys = [str(value) for value in dict.fromkeys(media_keys) if str(value)]
    if not keys:
        return 0
    placeholders = ",".join("?" for _ in keys)
    stamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE media_download_admissions SET status='completed',completed_at=?,updated_at=? "
            f"WHERE media_key IN ({placeholders}) AND status IN "
            "('claimed','dispatching','submitted','downloading','processing')",
            [stamp, stamp, *keys],
        )
        return int(cur.rowcount or 0)
