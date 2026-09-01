"""RSS 订阅、条目状态机与诊断的数据访问。"""
from __future__ import annotations

import re
import secrets
import sqlite3
import unicodedata
from typing import TYPE_CHECKING, Iterable

from app.modules.media_identity import normalize_tmdb_id

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


def add_rss_subscription(name: str, urls: str, exclude_keywords: str = "",
                         refresh_cron: str = "", parser: str = "mikan",
                         action: str = "subscribe", enabled: int = 1,
                         refresh_interval_minutes: int = 0,
                         download_method: str = "", qb_save_path: str = "",
                         gy_target_dir: str = "", gy_target_dir_name: str = "",
                         *, media_tmdb_id: str = "", media_default_season: int = 1,
                         skip_existing_episodes: int = 0) -> int:
    timestamp = now()
    normalized_tmdb_id = normalize_tmdb_id(media_tmdb_id) if media_tmdb_id else ""
    normalized_season = int(media_default_season)
    if not 0 <= normalized_season <= 100:
        raise ValueError("默认季号必须在 0 到 100 之间")
    if skip_existing_episodes and not normalized_tmdb_id:
        raise ValueError("启用媒体库去重前必须填写 TMDB ID")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO rss_items(name,enabled,refresh_cron,refresh_interval_minutes,urls,parser,"
            "exclude_keywords,action,download_method,qb_save_path,gy_target_dir,gy_target_dir_name,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, enabled, refresh_cron, max(0, int(refresh_interval_minutes or 0)), urls, parser,
             exclude_keywords, action, download_method, qb_save_path, gy_target_dir,
             gy_target_dir_name, timestamp, timestamp),
        )
        sub_id = int(cur.lastrowid)
        if normalized_tmdb_id:
            conn.execute(
                "INSERT INTO rss_media_bindings(rss_item_id,tmdb_id,default_season,"
                "skip_existing_episodes,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (sub_id, normalized_tmdb_id, normalized_season,
                 1 if skip_existing_episodes else 0, timestamp, timestamp),
            )
        return sub_id


def _rss_subscription_select() -> str:
    return (
        "SELECT i.*,COALESCE(b.tmdb_id,'') AS media_tmdb_id,"
        "COALESCE(b.default_season,1) AS media_default_season,"
        "COALESCE(b.skip_existing_episodes,0) AS skip_existing_episodes "
        "FROM rss_items i LEFT JOIN rss_media_bindings b ON b.rss_item_id=i.id"
    )


def list_rss_subscriptions() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(_rss_subscription_select() + " ORDER BY i.id ASC").fetchall()


def list_enabled_rss_subscriptions() -> list[sqlite3.Row]:
    """返回全部启用订阅，供服务端受控批量刷新生成完整快照。"""
    with get_conn() as conn:
        return conn.execute(
            _rss_subscription_select() + " WHERE i.enabled=1 ORDER BY i.id ASC"
        ).fetchall()


def list_enabled_rss_subscription_safe_targets() -> list[dict[str, object]]:
    """返回全部启用订阅的公开序号与名称，不读取地址、过滤词或路径。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id,name FROM rss_items WHERE enabled=1 ORDER BY id ASC"
        ).fetchall()
    return [
        {
            "subscription_number": int(row["id"]),
            "name": str(row["name"] or "").strip()[:120],
            "enabled": True,
        }
        for row in rows
    ]


def get_rss_stats() -> dict[str, int]:
    with get_conn() as conn:
        subscriptions = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN enabled=1 AND refresh_interval_minutes>0 THEN 1 ELSE 0 END) AS active "
            "FROM rss_items"
        ).fetchone()
        entries = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN COALESCE(processed,0)=0 AND status='pending' THEN 1 ELSE 0 END) AS pending "
            "FROM rss_entries"
        ).fetchone()
    return {
        "subscription_total": int((subscriptions["total"] if subscriptions else 0) or 0),
        "active_subscriptions": int((subscriptions["active"] if subscriptions else 0) or 0),
        "entry_total": int((entries["total"] if entries else 0) or 0),
        "pending_total": int((entries["pending"] if entries else 0) or 0),
    }


def get_rss_subscription(
    sub_id: int, *, connection: sqlite3.Connection | None = None
) -> sqlite3.Row | None:
    """读取订阅及媒体绑定；传入连接时加入调用方现有事务。"""
    if connection is not None:
        return connection.execute(
            _rss_subscription_select() + " WHERE i.id=?", (sub_id,)
        ).fetchone()
    with get_conn() as conn:
        return get_rss_subscription(sub_id, connection=conn)


def find_rss_subscriptions_by_normalized_name(
    normalized_name: str, *, limit: int = 3
) -> list[sqlite3.Row]:
    """按 NFKC/casefold 后的名称精确匹配；仅返回内部解析所需的 id/name。"""
    target = re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", str(normalized_name or ""))
    ).casefold().strip()
    if not target:
        return []
    safe_limit = max(1, min(int(limit or 1), 10))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id,name FROM rss_items ORDER BY id ASC"
        ).fetchall()
    matches: list[sqlite3.Row] = []
    for row in rows:
        candidate = unicodedata.normalize("NFKC", str(row["name"] or "")).strip(
            " \t\r\n'\"“”‘’《》<>【】[]()（）.。!！?？,，:：;；"
        )
        candidate = re.sub(r"\s+", " ", candidate).casefold().strip()
        if candidate == target:
            matches.append(row)
            if len(matches) >= safe_limit:
                break
    return matches


def _update_rss_subscription_in_connection(
    conn: sqlite3.Connection,
    sub_id: int,
    fields: dict,
    *,
    timestamp: str,
) -> None:
    base_allowed = {
        "name", "enabled", "refresh_cron", "refresh_interval_minutes",
        "last_refreshed_at", "urls", "parser", "exclude_keywords", "action",
        "download_method", "qb_save_path", "gy_target_dir", "gy_target_dir_name",
    }
    binding_keys = {
        "media_tmdb_id", "media_default_season", "skip_existing_episodes",
    }
    sets: list[str] = []
    vals: list[object] = []
    for key, value in fields.items():
        if key in base_allowed:
            sets.append(f"{key}=?")
            vals.append(value)
    if sets:
        sets.append("updated_at=?")
        vals.extend([timestamp, sub_id])
        conn.execute(f"UPDATE rss_items SET {', '.join(sets)} WHERE id=?", vals)

    if binding_keys & fields.keys():
        current = conn.execute(
            "SELECT tmdb_id,default_season,skip_existing_episodes "
            "FROM rss_media_bindings WHERE rss_item_id=?", (sub_id,),
        ).fetchone()
        raw_tmdb_id = str(fields.get(
            "media_tmdb_id", current["tmdb_id"] if current else ""
        ) or "").strip()
        tmdb_id = normalize_tmdb_id(raw_tmdb_id) if raw_tmdb_id else ""
        default_season = int(fields.get(
            "media_default_season", current["default_season"] if current else 1
        ) or 0)
        if not 0 <= default_season <= 100:
            raise ValueError("默认季号必须在 0 到 100 之间")
        skip_existing = 1 if fields.get(
            "skip_existing_episodes",
            current["skip_existing_episodes"] if current else 0,
        ) else 0
        if not tmdb_id:
            conn.execute(
                "DELETE FROM rss_media_bindings WHERE rss_item_id=?", (sub_id,)
            )
        else:
            conn.execute(
                "INSERT INTO rss_media_bindings(rss_item_id,tmdb_id,default_season,"
                "skip_existing_episodes,created_at,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(rss_item_id) DO UPDATE SET tmdb_id=excluded.tmdb_id,"
                "default_season=excluded.default_season,"
                "skip_existing_episodes=excluded.skip_existing_episodes,"
                "updated_at=excluded.updated_at",
                (sub_id, tmdb_id, default_season, skip_existing, timestamp, timestamp),
            )


def update_rss_subscription(
    sub_id: int,
    fields: dict,
    *,
    connection: sqlite3.Connection | None = None,
) -> None:
    """原子更新订阅；传入连接时复用调用方的事务与写锁。"""
    if not fields:
        return
    timestamp = now()
    if connection is not None:
        _update_rss_subscription_in_connection(
            connection, sub_id, fields, timestamp=timestamp
        )
        return
    with get_conn() as conn:
        _update_rss_subscription_in_connection(conn, sub_id, fields, timestamp=timestamp)


def delete_rss_subscription(sub_id: int) -> None:
    with get_conn() as conn:
        for table in ("rss_guangya_download_claims", "rss_qb_download_claims"):
            conn.execute(
                f"DELETE FROM {table} WHERE status!='submitted' "
                "AND first_entry_id IN (SELECT id FROM rss_entries WHERE rss_item_id=?)",
                (sub_id,),
            )
        conn.execute("DELETE FROM rss_entries WHERE rss_item_id=?", (sub_id,))
        conn.execute("DELETE FROM rss_items WHERE id=?", (sub_id,))


def add_rss_entry(sub_id: int, title: str, guid: str, pub_date: str = "",
                  payload: str = "") -> int | None:
    """兼容入口：普通 RSS 条目按 guid 原子去重。"""
    result = add_rss_entry_with_media(
        sub_id, title, guid, pub_date=pub_date, payload=payload
    )
    return int(result["id"]) if result.get("id") is not None else None


def add_rss_entry_with_media(
    sub_id: int,
    title: str,
    guid: str,
    *,
    pub_date: str = "",
    payload: str = "",
    media_key: str = "",
    tmdb_id: str = "",
    season: int | None = None,
    episode: int | None = None,
    skip_reason: str = "",
) -> dict[str, object]:
    """按 guid 与可信 media_key 去重写入，并保留可见跳过原因。"""
    timestamp = now()
    normalized_key = str(media_key or "").strip()
    normalized_reason = str(skip_reason or "").strip()[:160]
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        duplicate_guid = conn.execute(
            "SELECT id FROM rss_entries WHERE rss_item_id=? AND guid=? LIMIT 1",
            (sub_id, guid),
        ).fetchone()
        if duplicate_guid is not None:
            conn.rollback()
            return {"id": None, "status": "duplicate_guid", "skip_reason": ""}
        if normalized_key and not normalized_reason:
            duplicate_media = conn.execute(
                "SELECT e.id FROM rss_entry_media m JOIN rss_entries e ON e.id=m.rss_entry_id "
                "WHERE m.media_key=? AND (e.status IN ('pending','submitting','downloaded') "
                "OR (e.status='failed' AND COALESCE(e.processed,0)=0 "
                "AND e.failure_code IN ('qb_outcome_unknown','guangya_outcome_unknown',"
                "'submission_outcome_unknown'))) ORDER BY e.id DESC LIMIT 1",
                (normalized_key,),
            ).fetchone()
            if duplicate_media is not None:
                normalized_reason = "相同 TMDB 剧集已在 RSS 队列或下载记录中"
        status = "skipped" if normalized_reason else "pending"
        processed = 1 if normalized_reason else 0
        processed_at = timestamp if normalized_reason else None
        cur = conn.execute(
            "INSERT INTO rss_entries(rss_item_id,title,status,processed,processed_at,pub_date,guid,"
            "payload,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (sub_id, title, status, processed, processed_at, pub_date, guid, payload, timestamp),
        )
        entry_id = int(cur.lastrowid)
        if normalized_key or tmdb_id or season is not None or episode is not None or normalized_reason:
            conn.execute(
                "INSERT INTO rss_entry_media(rss_entry_id,media_key,tmdb_id,season,episode,"
                "skip_reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (entry_id, normalized_key, str(tmdb_id or ""), season, episode,
                 normalized_reason, timestamp, timestamp),
            )
        return {"id": entry_id, "status": status, "skip_reason": normalized_reason}


def _rss_entry_filters(sub_id: int | None = None, status: str | None = None,
                       keyword: str = "") -> tuple[str, list]:
    sql = " WHERE 1=1"
    params: list = []
    if sub_id:
        sql += " AND e.rss_item_id=?"
        params.append(sub_id)
    if status:
        sql += " AND e.status=?"
        params.append(status)
    if keyword:
        sql += " AND (e.title LIKE ? OR COALESCE(i.name,'') LIKE ? OR COALESCE(e.guid,'') LIKE ?)"
        value = f"%{keyword}%"
        params.extend([value, value, value])
    return sql, params


def list_rss_entries(
    sub_id: int | None = None,
    status: str | None = None,
    keyword: str = "",
    limit: int = 300,
    *,
    order: str = "published_desc",
) -> list[sqlite3.Row]:
    filters, params = _rss_entry_filters(sub_id, status, keyword)
    sql = ("SELECT e.*, i.name AS sub_name,COALESCE(m.media_key,'') AS media_key,"
           "m.season AS media_season,m.episode AS media_episode,"
           "COALESCE(m.skip_reason,'') AS skip_reason FROM rss_entries e "
           "LEFT JOIN rss_items i ON e.rss_item_id=i.id "
           "LEFT JOIN rss_entry_media m ON m.rss_entry_id=e.id" + filters)
    if order == "received_desc":
        sql += " ORDER BY e.id DESC"
    elif order == "received_asc":
        sql += " ORDER BY e.id ASC"
    elif order == "published_desc":
        # pub_date 由 parser 规范为 SQLite 可解析的 YYYY-MM-DD HH:MM；
        # 不可信或缺失日期回退本地入库时间，并用 id 保证稳定顺序。
        sql += (
            " ORDER BY CASE WHEN strftime('%s',e.pub_date) IS NULL THEN 1 ELSE 0 END,"
            " COALESCE(CAST(strftime('%s',e.pub_date) AS INTEGER),"
            " CAST(strftime('%s',e.created_at) AS INTEGER),0) DESC,e.id DESC"
        )
    else:
        raise ValueError("RSS 条目排序方式无效")
    sql += " LIMIT ?"
    params.append(max(1, int(limit)))
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def purge_processed_rss_entries(retention_days: int = 7) -> int:
    """清理超过保留期的已处理 RSS 条目；未处理和失败条目不受影响。"""
    days = max(1, int(retention_days or 7))
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM rss_entries WHERE COALESCE(processed,0)=1 "
            "AND processed_at IS NOT NULL "
            "AND datetime(processed_at) < datetime('now', ?)",
            (f"-{days} days",),
        )
        return int(cur.rowcount or 0)


def recover_stale_submitting_rss_entries(stale_minutes: int = 15) -> int:
    """将超时且提交结果未知的 RSS 条目转为不可自动重试的人工核对状态。"""
    minutes = max(1, int(stale_minutes or 15))
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE rss_entries SET status='failed', processed=0, processed_at=NULL, "
            "failure_code='submission_outcome_unknown', failure_retryable=0, "
            "failed_at=COALESCE(NULLIF(submitted_at,''),?) "
            "WHERE status='submitting' AND COALESCE(processed,0)=0 "
            "AND datetime(COALESCE(NULLIF(submitted_at,''),created_at)) "
            "< datetime('now','localtime', ?)",
            (timestamp, f"-{minutes} minutes"),
        )
        recovered_entries = int(cur.rowcount or 0)
        # 光鸭 claim 与 entry 由同一 lease 驱动。外部调用超过一小时仍无终态时，
        # 将其转入人工核对；旧调用若稍后返回，仍可凭原 lease 原子落最终结果。
        claim_stale_minutes = max(60, minutes)
        stale_rows = conn.execute(
            "SELECT infohash,first_entry_id FROM rss_guangya_download_claims "
            "WHERE status='submitting' AND (datetime(updated_at) IS NULL OR "
            "datetime(updated_at)<datetime('now','localtime', ?))",
            (f"-{claim_stale_minutes} minutes",),
        ).fetchall()
        for claim in stale_rows:
            conn.execute(
                "UPDATE rss_guangya_download_claims SET status='unknown',updated_at=? "
                "WHERE infohash=? AND status='submitting'",
                (timestamp, str(claim["infohash"])),
            )
            conn.execute(
                "UPDATE rss_entries SET status='failed',processed=0,processed_at=NULL,"
                "failure_code='guangya_outcome_unknown',failure_retryable=0,"
                "failed_at=COALESCE(NULLIF(submitted_at,''),?) WHERE id=?",
                (timestamp, int(claim["first_entry_id"])),
            )
        qb_stale_rows = conn.execute(
            "SELECT infohash,first_entry_id FROM rss_qb_download_claims "
            "WHERE status='submitting' AND (datetime(updated_at) IS NULL OR "
            "datetime(updated_at)<datetime('now','localtime', ?))",
            (f"-{claim_stale_minutes} minutes",),
        ).fetchall()
        for claim in qb_stale_rows:
            conn.execute(
                "UPDATE rss_qb_download_claims SET status='unknown',updated_at=? "
                "WHERE infohash=? AND status='submitting'",
                (timestamp, str(claim["infohash"])),
            )
            conn.execute(
                "UPDATE rss_entries SET status='failed',processed=0,processed_at=NULL,"
                "failure_code='qb_outcome_unknown',failure_retryable=0,"
                "failed_at=COALESCE(NULLIF(submitted_at,''),?) WHERE id=?",
                (timestamp, int(claim["first_entry_id"])),
            )
        return recovered_entries


def get_rss_entry(entry_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT e.*, i.name AS sub_name, i.download_method, i.qb_save_path, "
            "i.gy_target_dir, i.gy_target_dir_name FROM rss_entries e "
            "LEFT JOIN rss_items i ON e.rss_item_id=i.id WHERE e.id=?", (entry_id,)
        ).fetchone()


def get_pending_rss_qb_snapshot(
    default_method: str = "qb",
    limit: int = 21,
) -> list[sqlite3.Row]:
    """返回 Agent 确认绑定所需的 qB 待处理条目快照。

    该函数仅供服务端内部使用；调用方不得向客户端投影 title、payload 或路径。
    """
    safe_limit = max(1, min(100, int(limit or 21)))
    normalized_default = str(default_method or "").strip().lower()
    with get_conn() as conn:
        return conn.execute(
            "SELECT e.id,e.rss_item_id,e.title,e.status,e.processed,e.created_at,e.payload,"
            "COALESCE(i.download_method,'') AS download_method,"
            "COALESCE(i.qb_save_path,'') AS qb_save_path "
            "FROM rss_entries e JOIN rss_items i ON e.rss_item_id=i.id "
            "WHERE e.status='pending' AND COALESCE(e.processed,0)=0 "
            "AND LOWER(COALESCE(NULLIF(TRIM(i.download_method),''),?))='qb' "
            "ORDER BY e.id DESC LIMIT ?",
            (normalized_default, safe_limit),
        ).fetchall()


def claim_pending_rss_qb_entries(
    expected_rows: list[dict],
    default_method: str = "qb",
) -> list[sqlite3.Row]:
    """全有或全无地复核并认领 Agent 已确认的 qB RSS 条目集合。"""
    normalized_default = str(default_method or "").strip().lower()
    expected = []
    seen: set[int] = set()
    for raw in expected_rows:
        entry_id = int(raw.get("id") or 0)
        if entry_id <= 0 or entry_id in seen:
            return []
        seen.add(entry_id)
        expected.append({
            "id": entry_id,
            "rss_item_id": int(raw.get("rss_item_id") or 0),
            "title": str(raw.get("title") or ""),
            "payload": str(raw.get("payload") or ""),
            "created_at": str(raw.get("created_at") or ""),
            "download_method": str(raw.get("download_method") or ""),
            "qb_save_path": str(raw.get("qb_save_path") or ""),
        })
    if not expected or len(expected) > 20:
        return []

    ids = [item["id"] for item in expected]
    placeholders = ",".join("?" for _ in ids)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT e.id,e.rss_item_id,e.title,e.status,e.processed,e.created_at,e.payload,"
            "COALESCE(i.download_method,'') AS download_method,"
            "COALESCE(i.qb_save_path,'') AS qb_save_path "
            "FROM rss_entries e JOIN rss_items i ON e.rss_item_id=i.id "
            f"WHERE e.id IN ({placeholders}) ORDER BY e.id DESC",
            ids,
        ).fetchall()
        eligible_rows = [
            row for row in rows
            if row["status"] == "pending"
            and not bool(row["processed"])
            and (str(row["download_method"] or "").strip().lower() or normalized_default) == "qb"
        ]
        current = [{
            "id": int(row["id"]),
            "rss_item_id": int(row["rss_item_id"]),
            "title": str(row["title"] or ""),
            "payload": str(row["payload"] or ""),
            "created_at": str(row["created_at"] or ""),
            "download_method": str(row["download_method"] or ""),
            "qb_save_path": str(row["qb_save_path"] or ""),
        } for row in eligible_rows]
        if current != expected:
            conn.rollback()
            return []
        submitted_at = now()
        cur = conn.execute(
            f"UPDATE rss_entries SET status='submitting', submitted_at=? "
            f"WHERE id IN ({placeholders}) AND status='pending' AND COALESCE(processed,0)=0",
            [submitted_at, *ids],
        )
        if int(cur.rowcount or 0) != len(expected):
            conn.rollback()
            return []
        return rows


def get_retryable_failed_rss_qb_snapshot(
    default_method: str = "qb",
    limit: int = 21,
) -> list[sqlite3.Row]:
    """返回 Agent 确认绑定所需的可安全重试 qB 失败条目快照。"""
    safe_limit = max(1, min(100, int(limit or 21)))
    normalized_default = str(default_method or "").strip().lower()
    with get_conn() as conn:
        return conn.execute(
            "SELECT e.id,e.rss_item_id,e.title,e.status,e.processed,e.created_at,e.payload,"
            "e.failure_code,e.failure_retryable,e.retry_count,e.failed_at,"
            "COALESCE(i.download_method,'') AS download_method,"
            "COALESCE(i.qb_save_path,'') AS qb_save_path "
            "FROM rss_entries e JOIN rss_items i ON e.rss_item_id=i.id "
            "WHERE e.status='failed' AND COALESCE(e.processed,0)=0 "
            "AND COALESCE(e.failure_retryable,0)=1 "
            "AND COALESCE(e.retry_count,0)<5 "
            "AND (e.failure_code!='qb_rate_limited' OR ("
            "NULLIF(e.failed_at,'') IS NOT NULL AND "
            "datetime(e.failed_at)<=datetime('now','localtime','-60 seconds'))) "
            "AND LOWER(COALESCE(NULLIF(TRIM(i.download_method),''),?))='qb' "
            "ORDER BY COALESCE(NULLIF(e.failed_at,''),NULLIF(e.submitted_at,''),e.created_at) DESC, "
            "e.id DESC LIMIT ?",
            (normalized_default, safe_limit),
        ).fetchall()


def claim_retryable_failed_rss_qb_entries(
    expected_rows: list[dict],
    default_method: str = "qb",
) -> list[sqlite3.Row]:
    """全有或全无地复核并认领 Agent 已确认的可重试 qB 失败集合。"""
    normalized_default = str(default_method or "").strip().lower()
    expected = []
    seen: set[int] = set()
    for raw in expected_rows:
        entry_id = int(raw.get("id") or 0)
        if entry_id <= 0 or entry_id in seen:
            return []
        seen.add(entry_id)
        expected.append({
            "id": entry_id,
            "rss_item_id": int(raw.get("rss_item_id") or 0),
            "title": str(raw.get("title") or ""),
            "payload": str(raw.get("payload") or ""),
            "created_at": str(raw.get("created_at") or ""),
            "failure_code": str(raw.get("failure_code") or ""),
            "failure_retryable": int(raw.get("failure_retryable") or 0),
            "retry_count": int(raw.get("retry_count") or 0),
            "failed_at": str(raw.get("failed_at") or ""),
            "download_method": str(raw.get("download_method") or ""),
            "qb_save_path": str(raw.get("qb_save_path") or ""),
        })
    if not expected or len(expected) > 20:
        return []

    ids = [item["id"] for item in expected]
    placeholders = ",".join("?" for _ in ids)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT e.id,e.rss_item_id,e.title,e.status,e.processed,e.created_at,e.payload,"
            "e.failure_code,e.failure_retryable,e.retry_count,e.failed_at,"
            "COALESCE(i.download_method,'') AS download_method,"
            "COALESCE(i.qb_save_path,'') AS qb_save_path "
            "FROM rss_entries e JOIN rss_items i ON e.rss_item_id=i.id "
            f"WHERE e.id IN ({placeholders}) "
            "AND e.status='failed' AND COALESCE(e.processed,0)=0 "
            "AND COALESCE(e.failure_retryable,0)=1 AND COALESCE(e.retry_count,0)<5 "
            "AND (e.failure_code!='qb_rate_limited' OR ("
            "NULLIF(e.failed_at,'') IS NOT NULL AND "
            "datetime(e.failed_at)<=datetime('now','localtime','-60 seconds'))) "
            "ORDER BY COALESCE(NULLIF(e.failed_at,''),NULLIF(e.submitted_at,''),e.created_at) DESC, "
            "e.id DESC",
            ids,
        ).fetchall()
        eligible_rows = [
            row for row in rows
            if row["status"] == "failed"
            and not bool(row["processed"])
            and bool(row["failure_retryable"])
            and (str(row["download_method"] or "").strip().lower() or normalized_default) == "qb"
        ]
        current = [{
            "id": int(row["id"]),
            "rss_item_id": int(row["rss_item_id"]),
            "title": str(row["title"] or ""),
            "payload": str(row["payload"] or ""),
            "created_at": str(row["created_at"] or ""),
            "failure_code": str(row["failure_code"] or ""),
            "failure_retryable": int(row["failure_retryable"] or 0),
            "retry_count": int(row["retry_count"] or 0),
            "failed_at": str(row["failed_at"] or ""),
            "download_method": str(row["download_method"] or ""),
            "qb_save_path": str(row["qb_save_path"] or ""),
        } for row in eligible_rows]
        if current != expected:
            conn.rollback()
            return []
        submitted_at = now()
        cur = conn.execute(
            f"UPDATE rss_entries SET status='submitting', submitted_at=?, "
            "failure_code='', failure_retryable=0, failed_at=NULL, "
            "retry_count=COALESCE(retry_count,0)+1 "
            f"WHERE id IN ({placeholders}) AND status='failed' "
            "AND COALESCE(processed,0)=0 AND COALESCE(failure_retryable,0)=1 "
            "AND COALESCE(retry_count,0)<5 "
            "AND (failure_code!='qb_rate_limited' OR ("
            "NULLIF(failed_at,'') IS NOT NULL AND "
            "datetime(failed_at)<=datetime('now','localtime','-60 seconds')))",
            [submitted_at, *ids],
        )
        if int(cur.rowcount or 0) != len(expected):
            conn.rollback()
            return []
        return rows


def _normalized_bt_infohash(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", normalized):
        raise ValueError("RSS BT infohash 格式无效")
    return normalized


def _claimable_rss_entry(conn, entry_id: int, *, target_status: str) -> bool:
    stamp = now()
    processed = 1 if target_status == "downloaded" else 0
    processed_at = stamp if processed else None
    cur = conn.execute(
        "UPDATE rss_entries SET status=?,processed=?,processed_at=?,submitted_at=?,"
        "retry_count=COALESCE(retry_count,0)+CASE WHEN status='failed' THEN 1 ELSE 0 END,"
        "failure_code='',failure_retryable=0,failed_at=NULL WHERE id=? "
        "AND COALESCE(processed,0)=0 AND (status='pending' OR "
        "(status='failed' AND COALESCE(failure_retryable,0)=1))",
        (target_status, processed, processed_at, stamp, int(entry_id)),
    )
    return int(cur.rowcount or 0) == 1


def claim_rss_guangya_download(infohash: str, entry_id: int) -> dict[str, str]:
    """在同一事务中认领 RSS 条目与光鸭 infohash。"""
    normalized = _normalized_bt_infohash(infohash)
    token = secrets.token_hex(16)
    stamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM rss_guangya_download_claims WHERE infohash=?",
            (normalized,),
        ).fetchone()
        status = str(row["status"] or "") if row is not None else ""
        if status == "submitting":
            return {"status": "busy", "lease_token": ""}
        if status == "unknown":
            return {"status": "unknown", "lease_token": ""}
        if status == "submitted":
            if not _claimable_rss_entry(conn, entry_id, target_status="downloaded"):
                return {"status": "unavailable", "lease_token": ""}
            return {"status": "submitted", "lease_token": ""}
        if not _claimable_rss_entry(conn, entry_id, target_status="submitting"):
            return {"status": "unavailable", "lease_token": ""}
        conn.execute(
            "INSERT INTO rss_guangya_download_claims("
            "infohash,first_entry_id,lease_token,status,created_at,updated_at"
            ") VALUES(?,?,?,'submitting',?,?)",
            (normalized, int(entry_id), token, stamp, stamp),
        )
        return {"status": "claimed", "lease_token": token}


def finalize_rss_guangya_download(
    infohash: str,
    entry_id: int,
    lease_token: str,
    *,
    outcome: str,
) -> bool:
    """按 lease 原子落盘光鸭 claim 与 RSS 条目终态。"""
    normalized = _normalized_bt_infohash(infohash)
    token = str(lease_token or "").strip()
    if not token or outcome not in {"submitted", "unknown", "failed"}:
        return False
    stamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status,first_entry_id,lease_token FROM rss_guangya_download_claims "
            "WHERE infohash=?",
            (normalized,),
        ).fetchone()
        if (
            row is None
            or int(row["first_entry_id"]) != int(entry_id)
            or str(row["lease_token"] or "") != token
            or str(row["status"] or "") not in {"submitting", "unknown"}
        ):
            return False
        if outcome == "failed":
            conn.execute(
                "DELETE FROM rss_guangya_download_claims WHERE infohash=? "
                "AND first_entry_id=? AND lease_token=?",
                (normalized, int(entry_id), token),
            )
            conn.execute(
                "UPDATE rss_entries SET status='failed',processed=0,processed_at=NULL,"
                "submitted_at=?,failure_code='guangya_submit_failed',"
                "failure_retryable=0,failed_at=? WHERE id=?",
                (stamp, stamp, int(entry_id)),
            )
            return True
        claim_status = "submitted" if outcome == "submitted" else "unknown"
        conn.execute(
            "UPDATE rss_guangya_download_claims SET status=?,updated_at=? "
            "WHERE infohash=? AND first_entry_id=? AND lease_token=?",
            (claim_status, stamp, normalized, int(entry_id), token),
        )
        if outcome == "submitted":
            conn.execute(
                "UPDATE rss_entries SET status='downloaded',processed=1,processed_at=?,"
                "submitted_at=?,failure_code='',failure_retryable=0,failed_at=NULL "
                "WHERE id=?",
                (stamp, stamp, int(entry_id)),
            )
        else:
            conn.execute(
                "UPDATE rss_entries SET status='failed',processed=0,processed_at=NULL,"
                "submitted_at=?,failure_code='guangya_outcome_unknown',"
                "failure_retryable=0,failed_at=? WHERE id=?",
                (stamp, stamp, int(entry_id)),
            )
        return True


def claim_rss_qb_download(
    infohash: str, entry_id: int, *, entry_already_claimed: bool = False
) -> dict[str, str]:
    """在同一事务中认领 qB infohash，并可接管 Agent 已预认领的条目。"""
    normalized = _normalized_bt_infohash(infohash)
    token = secrets.token_hex(16)
    stamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM rss_qb_download_claims WHERE infohash=?",
            (normalized,),
        ).fetchone()
        status = str(row["status"] or "") if row is not None else ""
        if status == "submitting":
            return {"status": "busy", "lease_token": ""}
        if status == "unknown":
            return {"status": "unknown", "lease_token": ""}
        if status == "submitted":
            if entry_already_claimed:
                cur = conn.execute(
                    "UPDATE rss_entries SET status='downloaded',processed=1,processed_at=?,"
                    "submitted_at=?,failure_code='',failure_retryable=0,failed_at=NULL "
                    "WHERE id=? AND status='submitting' AND COALESCE(processed,0)=0",
                    (stamp, stamp, int(entry_id)),
                )
                available = int(cur.rowcount or 0) == 1
            else:
                available = _claimable_rss_entry(
                    conn, entry_id, target_status="downloaded"
                )
            return {
                "status": "submitted" if available else "unavailable",
                "lease_token": "",
            }
        if entry_already_claimed:
            current = conn.execute(
                "SELECT status,processed FROM rss_entries WHERE id=?",
                (int(entry_id),),
            ).fetchone()
            available = bool(
                current is not None
                and str(current["status"] or "") == "submitting"
                and not bool(current["processed"])
            )
        else:
            available = _claimable_rss_entry(
                conn, entry_id, target_status="submitting"
            )
        if not available:
            return {"status": "unavailable", "lease_token": ""}
        conn.execute(
            "INSERT INTO rss_qb_download_claims("
            "infohash,first_entry_id,lease_token,status,created_at,updated_at"
            ") VALUES(?,?,?,'submitting',?,?)",
            (normalized, int(entry_id), token, stamp, stamp),
        )
        return {"status": "claimed", "lease_token": token}


def finalize_rss_qb_download(
    infohash: str,
    entry_id: int,
    lease_token: str,
    *,
    outcome: str,
    failure_code: str = "",
    retryable: bool = False,
) -> bool:
    """按 lease 原子落盘 qB claim 与 RSS 条目终态。"""
    normalized = _normalized_bt_infohash(infohash)
    token = str(lease_token or "").strip()
    if not token or outcome not in {"submitted", "unknown", "failed"}:
        return False
    normalized_failure = str(failure_code or "").strip().lower()
    if normalized_failure not in _RSS_FAILURE_CODES:
        normalized_failure = "qb_rejected"
        retryable = False
    stamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status,first_entry_id,lease_token FROM rss_qb_download_claims "
            "WHERE infohash=?",
            (normalized,),
        ).fetchone()
        if (
            row is None
            or int(row["first_entry_id"]) != int(entry_id)
            or str(row["lease_token"] or "") != token
            or str(row["status"] or "") not in {"submitting", "unknown"}
        ):
            return False
        if outcome == "failed":
            conn.execute(
                "DELETE FROM rss_qb_download_claims WHERE infohash=? "
                "AND first_entry_id=? AND lease_token=?",
                (normalized, int(entry_id), token),
            )
            conn.execute(
                "UPDATE rss_entries SET status='failed',processed=0,processed_at=NULL,"
                "submitted_at=?,failure_code=?,failure_retryable=?,failed_at=? WHERE id=?",
                (
                    stamp, normalized_failure, 1 if retryable else 0, stamp,
                    int(entry_id),
                ),
            )
            return True
        claim_status = "submitted" if outcome == "submitted" else "unknown"
        conn.execute(
            "UPDATE rss_qb_download_claims SET status=?,updated_at=? "
            "WHERE infohash=? AND first_entry_id=? AND lease_token=?",
            (claim_status, stamp, normalized, int(entry_id), token),
        )
        if outcome == "submitted":
            conn.execute(
                "UPDATE rss_entries SET status='downloaded',processed=1,processed_at=?,"
                "submitted_at=?,failure_code='',failure_retryable=0,failed_at=NULL "
                "WHERE id=?",
                (stamp, stamp, int(entry_id)),
            )
        else:
            conn.execute(
                "UPDATE rss_entries SET status='failed',processed=0,processed_at=NULL,"
                "submitted_at=?,failure_code='qb_outcome_unknown',"
                "failure_retryable=0,failed_at=? WHERE id=?",
                (stamp, stamp, int(entry_id)),
            )
        return True


def claim_rss_entry(entry_id: int) -> bool:
    """原子认领条目，防止 Web/自动任务/TG 重复提交同一下载。"""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE rss_entries SET status='submitting', submitted_at=?, "
            "retry_count=COALESCE(retry_count,0)+CASE WHEN status='failed' THEN 1 ELSE 0 END, "
            "failure_code='', failure_retryable=0, failed_at=NULL WHERE id=? "
            "AND COALESCE(processed,0)=0 AND (status='pending' OR "
            "(status='failed' AND COALESCE(failure_retryable,0)=1))",
            (now(), entry_id),
        )
        return cur.rowcount == 1


_RSS_FAILURE_CODES = {
    "invalid_payload",
    "missing_torrent_url",
    "qb_auth_failed",
    "qb_rejected",
    "qb_unavailable",
    "qb_rate_limited",
    "qb_dedupe_busy",
    "qb_server_error",
    "qb_outcome_unknown",
    "submission_outcome_unknown",
    "guangya_submit_failed",
    "guangya_outcome_unknown",
    "unknown_failure",
}


def record_rss_entry_failure(entry_id: int, failure_code: str, retryable: bool) -> None:
    """记录稳定失败分类；不保存上游正文、URL 或异常原文。"""
    normalized = str(failure_code or "").strip().lower()
    if normalized not in _RSS_FAILURE_CODES:
        normalized = "unknown_failure"
        retryable = False
    failed_at = now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE rss_entries SET status='failed', processed=0, processed_at=NULL, "
            "submitted_at=?, failure_code=?, failure_retryable=?, failed_at=? WHERE id=?",
            (failed_at, normalized, 1 if retryable else 0, failed_at, entry_id),
        )


def update_rss_entry_status(entry_id: int, status: str) -> None:
    processed = 1 if status in ("downloaded", "skipped") else 0
    processed_at = now() if processed else None
    submitted_at = now() if status in ("submitting", "downloaded", "failed") else None
    with get_conn() as conn:
        if status == "failed":
            conn.execute(
                "UPDATE rss_entries SET status=?, processed=0, processed_at=NULL, "
                "submitted_at=COALESCE(?, submitted_at), failed_at=COALESCE(failed_at, ?) "
                "WHERE id=?",
                (status, submitted_at, submitted_at, entry_id),
            )
        else:
            conn.execute(
                "UPDATE rss_entries SET status=?, processed=?, processed_at=?, "
                "submitted_at=COALESCE(?, submitted_at), failure_code='', "
                "failure_retryable=0, failed_at=NULL WHERE id=?",
                (status, processed, processed_at, submitted_at, entry_id),
            )


def skip_pending_rss_entries(entry_ids: Iterable[int], reason: str) -> int:
    """按当前过滤规则原子收束历史 pending 条目，并保留可解释原因。"""
    normalized = list(dict.fromkeys(
        int(entry_id) for entry_id in entry_ids if int(entry_id) > 0
    ))
    if not normalized:
        return 0
    message = str(reason or "命中排除关键词").strip()[:160]
    updated = 0
    stamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for offset in range(0, len(normalized), 500):
            batch = normalized[offset:offset + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT id FROM rss_entries WHERE id IN ({placeholders}) "
                "AND status='pending' AND COALESCE(processed,0)=0",
                batch,
            ).fetchall()
            eligible = [int(row["id"]) for row in rows]
            if not eligible:
                continue
            eligible_placeholders = ",".join("?" for _ in eligible)
            cur = conn.execute(
                f"UPDATE rss_entries SET status='skipped',processed=1,processed_at=?,"
                "failure_code='',failure_retryable=0,failed_at=NULL "
                f"WHERE id IN ({eligible_placeholders}) AND status='pending' "
                "AND COALESCE(processed,0)=0",
                (stamp, *eligible),
            )
            updated += int(cur.rowcount or 0)
            conn.executemany(
                "INSERT INTO rss_entry_media(rss_entry_id,skip_reason,created_at,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(rss_entry_id) DO UPDATE SET "
                "skip_reason=excluded.skip_reason,updated_at=excluded.updated_at",
                [(entry_id, message, stamp, stamp) for entry_id in eligible],
            )
    return updated


def update_rss_entries_processed(entry_ids: list[int], processed: bool) -> int:
    ids = list(dict.fromkeys(int(item) for item in entry_ids))
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    status = "skipped" if processed else "pending"
    processed_at = now() if processed else None
    allowed_statuses = ("pending", "failed", "skipped") if processed else ("failed", "skipped")
    allowed = ",".join("?" for _ in allowed_statuses)
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE rss_entries SET processed=?, processed_at=?, status=?, "
            "failure_code='', failure_retryable=0, failed_at=NULL "
            f"WHERE id IN ({placeholders}) AND status IN ({allowed})",
            [1 if processed else 0, processed_at, status, *ids, *allowed_statuses],
        )
        if not processed and cur.rowcount:
            for table in ("rss_guangya_download_claims", "rss_qb_download_claims"):
                conn.execute(
                    f"DELETE FROM {table} WHERE status='unknown' "
                    f"AND first_entry_id IN ({placeholders})",
                    ids,
                )
            conn.execute(
                f"UPDATE rss_entry_media SET skip_reason='',updated_at=? "
                f"WHERE rss_entry_id IN ({placeholders})",
                [now(), *ids],
            )
        return cur.rowcount


def update_rss_entries_processed_snapshot(
    expected_rows: list[dict], processed: bool
) -> int:
    """按确认时冻结的完整状态原子标记 RSS 条目；任一变化则全量拒绝。"""
    if not isinstance(processed, bool) or not isinstance(expected_rows, list):
        return 0
    normalized: list[dict] = []
    seen: set[int] = set()
    for raw in expected_rows:
        if not isinstance(raw, dict):
            return 0
        try:
            entry_id = int(raw.get("id") or 0)
        except (TypeError, ValueError, OverflowError):
            return 0
        if entry_id <= 0 or entry_id in seen:
            return 0
        seen.add(entry_id)
        normalized.append({
            "id": entry_id,
            "status": str(raw.get("status") or ""),
            "processed": bool(raw.get("processed")),
            "created_at": str(raw.get("created_at") or ""),
            "failure_code": str(raw.get("failure_code") or ""),
            "failure_retryable": bool(raw.get("failure_retryable")),
        })
    if not normalized or len(normalized) > 50:
        return 0
    allowed_statuses = (
        {"pending", "failed", "skipped"} if processed else {"failed", "skipped"}
    )
    if any(item["status"] not in allowed_statuses for item in normalized):
        return 0

    ids = [item["id"] for item in normalized]
    placeholders = ",".join("?" for _ in ids)
    target_status = "skipped" if processed else "pending"
    processed_at = now() if processed else None
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id,status,processed,created_at,failure_code,failure_retryable "
            f"FROM rss_entries WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        by_id = {int(row["id"]): row for row in rows}
        current = [{
            "id": item["id"],
            "status": str(by_id[item["id"]]["status"] or "") if item["id"] in by_id else "missing",
            "processed": bool(by_id[item["id"]]["processed"]) if item["id"] in by_id else False,
            "created_at": str(by_id[item["id"]]["created_at"] or "") if item["id"] in by_id else "",
            "failure_code": str(by_id[item["id"]]["failure_code"] or "") if item["id"] in by_id else "",
            "failure_retryable": bool(by_id[item["id"]]["failure_retryable"]) if item["id"] in by_id else False,
        } for item in normalized]
        if current != normalized:
            conn.rollback()
            return 0
        allowed = ",".join("?" for _ in allowed_statuses)
        cur = conn.execute(
            f"UPDATE rss_entries SET processed=?,processed_at=?,status=?,"
            "failure_code='',failure_retryable=0,failed_at=NULL "
            f"WHERE id IN ({placeholders}) AND status IN ({allowed})",
            [
                1 if processed else 0,
                processed_at,
                target_status,
                *ids,
                *sorted(allowed_statuses),
            ],
        )
        if int(cur.rowcount or 0) != len(normalized):
            conn.rollback()
            return 0
        if not processed:
            for table in ("rss_guangya_download_claims", "rss_qb_download_claims"):
                conn.execute(
                    f"DELETE FROM {table} WHERE status='unknown' "
                    f"AND first_entry_id IN ({placeholders})",
                    ids,
                )
            conn.execute(
                f"UPDATE rss_entry_media SET skip_reason='',updated_at=? "
                f"WHERE rss_entry_id IN ({placeholders})",
                [now(), *ids],
            )
        return int(cur.rowcount or 0)


def get_rss_manual_review_summary(sub_id: int) -> dict[str, int]:
    """汇总订阅仍未处理的终态失败，供调度告警跨轮次重试与恢复。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT "
            "SUM(CASE WHEN failure_code IN "
            "('qb_outcome_unknown','guangya_outcome_unknown','submission_outcome_unknown') "
            "THEN 1 ELSE 0 END) AS outcome_unknown_count, "
            "COUNT(*) AS failed_count "
            "FROM rss_entries WHERE rss_item_id=? AND status='failed' "
            "AND COALESCE(processed,0)=0 AND COALESCE(failure_retryable,0)=0",
            (int(sub_id),),
        ).fetchone()
    return {
        "outcome_unknown_count": int(row["outcome_unknown_count"] or 0) if row else 0,
        "failed_count": int(row["failed_count"] or 0) if row else 0,
    }


def get_rss_diagnostic_summary(
    current_time: str | None = None,
    *,
    stale_submitting_minutes: int = 15,
    pending_backlog_hours: int = 24,
    attention_limit: int = 20,
) -> dict:
    """返回 RSS Agent 所需的安全聚合；不读取或返回源 URL、标题、GUID、payload 或路径。"""
    snapshot = current_time or now()
    stale_minutes = max(1, min(24 * 60, int(stale_submitting_minutes or 15)))
    backlog_hours = max(1, min(24 * 365, int(pending_backlog_hours or 24)))
    limit = max(1, min(100, int(attention_limit or 20)))
    stale_modifier = f"-{stale_minutes} minutes"
    backlog_modifier = f"-{backlog_hours} hours"

    pending_valid = "e.status='pending' AND COALESCE(e.processed,0)=0"
    pending_backlog = (
        f"{pending_valid} AND (COALESCE(NULLIF(e.created_at,''),'')='' "
        "OR datetime(e.created_at) IS NULL OR datetime(e.created_at)<=datetime(?,?))"
    )
    submitting_valid = "e.status='submitting' AND COALESCE(e.processed,0)=0"
    submitting_timestamp = "COALESCE(NULLIF(e.submitted_at,''),NULLIF(e.created_at,''),'')"
    stale_submitting = (
        f"{submitting_valid} AND ({submitting_timestamp}='' "
        f"OR datetime({submitting_timestamp}) IS NULL "
        f"OR datetime({submitting_timestamp})<=datetime(?,?))"
    )
    valid_entry = (
        "((e.status IN ('pending','submitting','failed') AND COALESCE(e.processed,0)=0) "
        "OR (e.status IN ('downloaded','skipped') AND COALESCE(e.processed,0)=1))"
    )
    invalid_entry = f"COALESCE(({valid_entry}),0)=0"

    with get_conn() as conn:
        subscription_row = conn.execute(
            "SELECT "
            "COUNT(*) AS total,"
            "SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled,"
            "SUM(CASE WHEN enabled=1 THEN 0 ELSE 1 END) AS disabled,"
            "SUM(CASE WHEN enabled=1 AND refresh_interval_minutes>0 THEN 1 ELSE 0 END) AS scheduled,"
            "SUM(CASE WHEN enabled=1 AND refresh_interval_minutes<=0 THEN 1 ELSE 0 END) AS manual_only,"
            "SUM(CASE WHEN enabled=1 AND COALESCE(last_refreshed_at,'')='' THEN 1 ELSE 0 END) AS never_refreshed,"
            "SUM(CASE WHEN enabled=1 AND COALESCE(last_refreshed_at,'')<>'' "
            "AND datetime(last_refreshed_at) IS NULL THEN 1 ELSE 0 END) AS invalid_last_refreshed_at,"
            "SUM(CASE WHEN enabled=1 AND refresh_interval_minutes>0 AND ("
            "COALESCE(last_refreshed_at,'')='' OR datetime(last_refreshed_at) IS NULL OR "
            "datetime(last_refreshed_at, '+' || refresh_interval_minutes || ' minutes')<=datetime(?)"
            ") THEN 1 ELSE 0 END) AS due_now,"
            "SUM(CASE WHEN enabled=1 AND refresh_interval_minutes<=0 "
            "AND TRIM(COALESCE(refresh_cron,''))<>'' THEN 1 ELSE 0 END) AS cron_not_active "
            "FROM rss_items",
            (snapshot,),
        ).fetchone()
        entry_row = conn.execute(
            "SELECT "
            "COUNT(*) AS total,"
            f"SUM(CASE WHEN {pending_valid} THEN 1 ELSE 0 END) AS pending,"
            f"SUM(CASE WHEN {pending_backlog} THEN 1 ELSE 0 END) AS pending_backlog,"
            f"SUM(CASE WHEN {submitting_valid} THEN 1 ELSE 0 END) AS submitting,"
            f"SUM(CASE WHEN {stale_submitting} THEN 1 ELSE 0 END) AS stale_submitting,"
            "SUM(CASE WHEN e.status='failed' AND COALESCE(e.processed,0)=0 THEN 1 ELSE 0 END) AS failed,"
            "SUM(CASE WHEN e.status='downloaded' AND COALESCE(e.processed,0)=1 THEN 1 ELSE 0 END) AS downloaded,"
            "SUM(CASE WHEN e.status='downloaded' AND COALESCE(e.processed,0)=1 "
            "AND COALESCE(NULLIF(e.processed_at,''),'')<>'' "
            "AND datetime(e.processed_at) IS NOT NULL "
            "AND datetime(e.processed_at)>=datetime(?,'-24 hours') THEN 1 ELSE 0 END) AS downloaded_last_24h,"
            "SUM(CASE WHEN e.status='skipped' AND COALESCE(e.processed,0)=1 THEN 1 ELSE 0 END) AS skipped,"
            f"SUM(CASE WHEN {invalid_entry} THEN 1 ELSE 0 END) AS unknown_or_inconsistent "
            "FROM rss_entries e",
            (snapshot, backlog_modifier, snapshot, stale_modifier, snapshot),
        ).fetchone()

        attention_sql = f"""
            WITH per_subscription AS (
                SELECT
                    i.id AS subscription_id,
                    CASE
                        WHEN COALESCE(i.enabled,0)<>1 THEN 'disabled'
                        WHEN i.refresh_interval_minutes>0
                            AND COALESCE(i.last_refreshed_at,'')<>''
                            AND datetime(i.last_refreshed_at) IS NULL THEN 'scheduled_invalid'
                        WHEN i.refresh_interval_minutes>0 AND (
                            COALESCE(i.last_refreshed_at,'')='' OR
                            datetime(i.last_refreshed_at, '+' || i.refresh_interval_minutes || ' minutes')<=datetime(?)
                        ) THEN 'scheduled_due'
                        WHEN i.refresh_interval_minutes>0 THEN 'scheduled'
                        ELSE 'manual_only'
                    END AS schedule_state,
                    CASE WHEN i.enabled=1 AND i.refresh_interval_minutes<=0
                        AND TRIM(COALESCE(i.refresh_cron,''))<>'' THEN 1 ELSE 0 END AS cron_not_active,
                    CASE WHEN i.enabled=1 AND COALESCE(i.last_refreshed_at,'')<>''
                        AND datetime(i.last_refreshed_at) IS NULL THEN 1 ELSE 0 END AS invalid_last_refreshed_at,
                    SUM(CASE WHEN {pending_backlog} THEN 1 ELSE 0 END) AS pending_backlog,
                    SUM(CASE WHEN {stale_submitting} THEN 1 ELSE 0 END) AS stale_submitting,
                    SUM(CASE WHEN e.status='failed' AND COALESCE(e.processed,0)=0 THEN 1 ELSE 0 END) AS failed_entries,
                    SUM(CASE WHEN e.id IS NOT NULL AND {invalid_entry} THEN 1 ELSE 0 END) AS unknown_or_inconsistent
                FROM rss_items i
                LEFT JOIN rss_entries e ON e.rss_item_id=i.id
                GROUP BY i.id
            )
            SELECT subscription_id,schedule_state,cron_not_active,invalid_last_refreshed_at,
                   pending_backlog,stale_submitting,failed_entries,unknown_or_inconsistent
            FROM per_subscription
            WHERE cron_not_active>0 OR invalid_last_refreshed_at>0 OR pending_backlog>0
                  OR stale_submitting>0 OR failed_entries>0 OR unknown_or_inconsistent>0
            ORDER BY (failed_entries+stale_submitting+unknown_or_inconsistent) DESC,
                     invalid_last_refreshed_at DESC,pending_backlog DESC,
                     cron_not_active DESC,subscription_id ASC
            LIMIT ?
        """
        attention_rows = conn.execute(
            attention_sql,
            (
                snapshot,
                snapshot,
                backlog_modifier,
                snapshot,
                stale_modifier,
                limit + 1,
            ),
        ).fetchall()

    subscriptions = {
        "total": int((subscription_row["total"] if subscription_row else 0) or 0),
        "enabled": int((subscription_row["enabled"] if subscription_row else 0) or 0),
        "disabled": int((subscription_row["disabled"] if subscription_row else 0) or 0),
        "scheduled": int((subscription_row["scheduled"] if subscription_row else 0) or 0),
        "manual_only": int((subscription_row["manual_only"] if subscription_row else 0) or 0),
        "never_refreshed": int((subscription_row["never_refreshed"] if subscription_row else 0) or 0),
        "invalid_last_refreshed_at": int(
            (subscription_row["invalid_last_refreshed_at"] if subscription_row else 0) or 0
        ),
        "due_now": int((subscription_row["due_now"] if subscription_row else 0) or 0),
        "cron_configured_but_not_scheduled": int(
            (subscription_row["cron_not_active"] if subscription_row else 0) or 0
        ),
    }
    pending = int((entry_row["pending"] if entry_row else 0) or 0)
    pending_backlog_count = int((entry_row["pending_backlog"] if entry_row else 0) or 0)
    submitting = int((entry_row["submitting"] if entry_row else 0) or 0)
    stale_submitting_count = int((entry_row["stale_submitting"] if entry_row else 0) or 0)
    downloaded = int((entry_row["downloaded"] if entry_row else 0) or 0)
    downloaded_last_24h = int(
        (entry_row["downloaded_last_24h"] if entry_row else 0) or 0
    )
    skipped = int((entry_row["skipped"] if entry_row else 0) or 0)
    entries = {
        "total": int((entry_row["total"] if entry_row else 0) or 0),
        "pending": pending,
        "pending_recent": max(0, pending - pending_backlog_count),
        "pending_backlog": pending_backlog_count,
        "submitting": submitting,
        "submitting_in_flight": max(0, submitting - stale_submitting_count),
        "stale_submitting": stale_submitting_count,
        "failed": int((entry_row["failed"] if entry_row else 0) or 0),
        "downloaded": downloaded,
        "downloaded_last_24h": downloaded_last_24h,
        "skipped": skipped,
        "terminal": downloaded + skipped,
        "unknown_or_inconsistent": int(
            (entry_row["unknown_or_inconsistent"] if entry_row else 0) or 0
        ),
    }
    projected_attention = [
        {
            "subscription_id": int(row["subscription_id"]),
            "schedule_state": str(row["schedule_state"]),
            "cron_configured_but_not_scheduled": bool(row["cron_not_active"]),
            "invalid_last_refreshed_at": bool(row["invalid_last_refreshed_at"]),
            "entry_counts": {
                "pending_backlog": int(row["pending_backlog"] or 0),
                "stale_submitting": int(row["stale_submitting"] or 0),
                "failed": int(row["failed_entries"] or 0),
                "unknown_or_inconsistent": int(row["unknown_or_inconsistent"] or 0),
            },
        }
        for row in attention_rows[:limit]
    ]
    return {
        "thresholds": {
            "stale_submitting_minutes": stale_minutes,
            "pending_backlog_hours": backlog_hours,
        },
        "subscriptions": subscriptions,
        "entries": entries,
        "attention_subscriptions": projected_attention,
        "attention_truncated": len(attention_rows) > limit,
    }



def _query_rss_subscription_safe_summaries(
    current_time: str,
    *,
    subscription_id: int | None,
    limit: int,
) -> list[sqlite3.Row]:
    """聚合 Agent 公开订阅；名称可展示，但不读取 URL、过滤词、正文或路径。"""
    stale_modifier = "-15 minutes"
    backlog_modifier = "-24 hours"
    pending_valid = "e.status='pending' AND COALESCE(e.processed,0)=0"
    pending_backlog = (
        f"{pending_valid} AND (COALESCE(NULLIF(e.created_at,''),'')='' "
        "OR datetime(e.created_at) IS NULL OR datetime(e.created_at)<=datetime(?,?))"
    )
    submitting_valid = "e.status='submitting' AND COALESCE(e.processed,0)=0"
    submitting_timestamp = "COALESCE(NULLIF(e.submitted_at,''),NULLIF(e.created_at,''),'')"
    stale_submitting = (
        f"{submitting_valid} AND ({submitting_timestamp}='' "
        f"OR datetime({submitting_timestamp}) IS NULL "
        f"OR datetime({submitting_timestamp})<=datetime(?,?))"
    )
    valid_entry = (
        "((e.status IN ('pending','submitting','failed') AND COALESCE(e.processed,0)=0) "
        "OR (e.status IN ('downloaded','skipped') AND COALESCE(e.processed,0)=1))"
    )
    invalid_entry = f"COALESCE(({valid_entry}),0)=0"
    bounded_limit = max(1, min(100, int(limit or 100)))
    selected_where = "WHERE id=?" if subscription_id is not None else ""
    selected_params: list[object] = (
        [int(subscription_id)] if subscription_id is not None else []
    )

    with get_conn() as conn:
        total = (
            1
            if subscription_id is not None
            else int(conn.execute("SELECT COUNT(*) FROM rss_items").fetchone()[0] or 0)
        )
        query = f"""
            WITH selected_items AS (
                SELECT id,name,enabled,refresh_interval_minutes,last_refreshed_at,refresh_cron
                FROM rss_items
                {selected_where}
                ORDER BY id ASC
                LIMIT ?
            )
            SELECT
                i.id AS subscription_id,
                i.name AS subscription_name,
                ? AS subscription_total,
                CASE WHEN COALESCE(i.enabled,0)=1 THEN 1 ELSE 0 END AS enabled,
                CASE
                    WHEN COALESCE(i.enabled,0)<>1 THEN 'disabled'
                    WHEN i.refresh_interval_minutes>0
                        AND COALESCE(i.last_refreshed_at,'')<>''
                        AND datetime(i.last_refreshed_at) IS NULL THEN 'scheduled_invalid'
                    WHEN i.refresh_interval_minutes>0 AND (
                        COALESCE(i.last_refreshed_at,'')='' OR
                        datetime(i.last_refreshed_at, '+' || i.refresh_interval_minutes || ' minutes')<=datetime(?)
                    ) THEN 'scheduled_due'
                    WHEN i.refresh_interval_minutes>0 THEN 'scheduled'
                    ELSE 'manual_only'
                END AS schedule_state,
                MAX(0, COALESCE(i.refresh_interval_minutes,0)) AS refresh_interval_minutes,
                CASE WHEN i.enabled=1 AND COALESCE(i.refresh_interval_minutes,0)<=0
                    AND TRIM(COALESCE(i.refresh_cron,''))<>'' THEN 1 ELSE 0 END AS cron_not_active,
                CASE WHEN i.enabled=1 AND COALESCE(i.last_refreshed_at,'')<>''
                    AND datetime(i.last_refreshed_at) IS NULL THEN 1 ELSE 0 END AS invalid_last_refreshed_at,
                COUNT(e.id) AS entry_total,
                SUM(CASE WHEN {pending_valid} THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN {pending_backlog} THEN 1 ELSE 0 END) AS pending_backlog,
                SUM(CASE WHEN {submitting_valid} THEN 1 ELSE 0 END) AS submitting,
                SUM(CASE WHEN {stale_submitting} THEN 1 ELSE 0 END) AS stale_submitting,
                SUM(CASE WHEN e.status='failed' AND COALESCE(e.processed,0)=0 THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN e.status='downloaded' AND COALESCE(e.processed,0)=1 THEN 1 ELSE 0 END) AS downloaded,
                SUM(CASE WHEN e.status='downloaded' AND COALESCE(e.processed,0)=1
                    AND COALESCE(NULLIF(e.processed_at,''),'')<>''
                    AND datetime(e.processed_at) IS NOT NULL
                    AND datetime(e.processed_at)>=datetime(?,'-24 hours') THEN 1 ELSE 0 END) AS downloaded_last_24h,
                SUM(CASE WHEN e.status='skipped' AND COALESCE(e.processed,0)=1 THEN 1 ELSE 0 END) AS skipped,
                SUM(CASE WHEN e.id IS NOT NULL AND {invalid_entry} THEN 1 ELSE 0 END) AS unknown_or_inconsistent
            FROM selected_items i
            LEFT JOIN rss_entries e ON e.rss_item_id=i.id
            GROUP BY i.id
            ORDER BY i.id ASC
        """
        params = [
            *selected_params,
            bounded_limit,
            total,
            current_time,
            current_time,
            backlog_modifier,
            current_time,
            stale_modifier,
            current_time,
        ]
        return conn.execute(query, params).fetchall()


def _project_rss_subscription_safe_summary(row: sqlite3.Row) -> dict:
    pending = int(row["pending"] or 0)
    pending_backlog = int(row["pending_backlog"] or 0)
    submitting = int(row["submitting"] or 0)
    stale_submitting = int(row["stale_submitting"] or 0)
    failed = int(row["failed"] or 0)
    downloaded = int(row["downloaded"] or 0)
    downloaded_last_24h = int(row["downloaded_last_24h"] or 0)
    skipped = int(row["skipped"] or 0)
    inconsistent = int(row["unknown_or_inconsistent"] or 0)
    cron_not_active = bool(row["cron_not_active"])
    invalid_refreshed = bool(row["invalid_last_refreshed_at"])
    attention_count = (
        pending_backlog
        + stale_submitting
        + failed
        + inconsistent
        + int(cron_not_active)
        + int(invalid_refreshed)
    )
    return {
        "subscription_number": int(row["subscription_id"]),
        "name": str(row["subscription_name"] or "").strip()[:120],
        "enabled": bool(row["enabled"]),
        "schedule_state": str(row["schedule_state"]),
        "refresh_interval_minutes": int(row["refresh_interval_minutes"] or 0),
        "cron_configured_but_not_scheduled": cron_not_active,
        "invalid_last_refreshed_at": invalid_refreshed,
        "attention_count": attention_count,
        "entry_counts": {
            "total": int(row["entry_total"] or 0),
            "pending": pending,
            "pending_recent": max(0, pending - pending_backlog),
            "pending_backlog": pending_backlog,
            "submitting": submitting,
            "submitting_in_flight": max(0, submitting - stale_submitting),
            "stale_submitting": stale_submitting,
            "failed": failed,
            "downloaded": downloaded,
            "downloaded_last_24h": downloaded_last_24h,
            "skipped": skipped,
            "terminal": downloaded + skipped,
            "unknown_or_inconsistent": inconsistent,
        },
    }


def count_rss_downloaded_entries_since(
    current_time: str | None = None,
    *,
    hours: int = 24,
) -> int:
    """统计时间窗内全部订阅的成功下载数，不受摘要展示上限影响。"""
    snapshot = current_time or now()
    bounded_hours = max(1, min(24 * 31, int(hours or 24)))
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM rss_entries "
            "WHERE status='downloaded' AND COALESCE(processed,0)=1 "
            "AND COALESCE(NULLIF(processed_at,''),'')<>'' "
            "AND datetime(processed_at) IS NOT NULL "
            "AND datetime(processed_at)>=datetime(?,?)",
            (snapshot, f"-{bounded_hours} hours"),
        ).fetchone()
    return int((row[0] if row else 0) or 0)


def list_rss_subscription_safe_summaries(
    current_time: str | None = None,
    *,
    limit: int = 100,
) -> dict:
    """返回有界 RSS 订阅摘要；包含名称，不返回地址、过滤词、条目正文或路径。"""
    snapshot = current_time or now()
    bounded_limit = max(1, min(100, int(limit or 100)))
    rows = _query_rss_subscription_safe_summaries(
        snapshot,
        subscription_id=None,
        limit=bounded_limit,
    )
    total = int((rows[0]["subscription_total"] if rows else 0) or 0)
    return {
        "total": total,
        "returned": len(rows),
        "truncated": total > len(rows),
        "items": [_project_rss_subscription_safe_summary(row) for row in rows],
    }


def get_rss_subscription_safe_summary(
    subscription_id: int,
    current_time: str | None = None,
) -> dict | None:
    """按精确 ID 返回单个安全摘要；找不到时返回 None。"""
    rows = _query_rss_subscription_safe_summaries(
        current_time or now(),
        subscription_id=int(subscription_id),
        limit=1,
    )
    return _project_rss_subscription_safe_summary(rows[0]) if rows else None


def list_due_rss_subscriptions(current_time: str | None = None) -> list[sqlite3.Row]:
    current_time = current_time or now()
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM rss_items WHERE enabled=1 AND refresh_interval_minutes>0 AND ("
            "last_refreshed_at IS NULL OR last_refreshed_at='' OR datetime(last_refreshed_at) IS NULL OR "
            "datetime(last_refreshed_at, '+' || refresh_interval_minutes || ' minutes') <= datetime(?)"
            ") ORDER BY id",
            (current_time,),
        ).fetchall()
