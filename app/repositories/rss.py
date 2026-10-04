"""RSS 订阅、条目状态机与诊断的数据访问。"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from typing import Iterable

from app.modules.media_identity import normalize_tmdb_id


def add_rss_subscription(name: str, urls: str, exclude_keywords: str = "",
                         refresh_cron: str = "", parser: str = "mikan",
                         action: str = "subscribe", enabled: int = 1,
                         refresh_interval_minutes: int = 0,
                         download_method: str = "", qb_save_path: str = "",
                         gy_target_dir: str = "", gy_target_dir_name: str = "",
                         *, media_tmdb_id: str = "", media_default_season: int = 1,
                         skip_existing_episodes: int = 0) -> int:
    timestamp = db.now()
    normalized_tmdb_id = normalize_tmdb_id(media_tmdb_id) if media_tmdb_id else ""
    normalized_season = int(media_default_season)
    if not 0 <= normalized_season <= 100:
        raise ValueError("默认季号必须在 0 到 100 之间")
    if skip_existing_episodes and not normalized_tmdb_id:
        raise ValueError("启用媒体库去重前必须填写 TMDB ID")
    with db.get_conn() as conn:
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
    with db.get_conn() as conn:
        return conn.execute(_rss_subscription_select() + " ORDER BY i.id ASC").fetchall()


def list_enabled_rss_subscriptions() -> list[sqlite3.Row]:
    """返回全部启用订阅，供服务端受控批量刷新生成完整快照。"""
    with db.get_conn() as conn:
        return conn.execute(
            _rss_subscription_select() + " WHERE i.enabled=1 ORDER BY i.id ASC"
        ).fetchall()


def list_enabled_rss_subscription_safe_targets() -> list[dict[str, object]]:
    """返回全部启用订阅的公开序号与名称，不读取地址、过滤词或路径。"""
    with db.get_conn() as conn:
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
    with db.get_conn() as conn:
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
    with db.get_conn() as conn:
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
    with db.get_conn() as conn:
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
    timestamp = db.now()
    if connection is not None:
        _update_rss_subscription_in_connection(
            connection, sub_id, fields, timestamp=timestamp
        )
        return
    with db.get_conn() as conn:
        _update_rss_subscription_in_connection(conn, sub_id, fields, timestamp=timestamp)


def delete_rss_subscription(sub_id: int) -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM rss_entries WHERE rss_item_id=?", (sub_id,))
        conn.execute("DELETE FROM rss_items WHERE id=?", (sub_id,))


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
    timestamp = db.now()
    normalized_key = str(media_key or "").strip()
    normalized_reason = str(skip_reason or "").strip()[:160]
    with db.get_conn() as conn:
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


def bind_rss_entry_download(entry_id: int, request_key: str, backend: str) -> None:
    """在副作用前绑定已验证内容身份与目标，重试沿统一请求键自动接续。"""
    if backend not in {"qb", "guangya"} or not request_key:
        raise ValueError("RSS 下载目标或资源身份无效")
    with db.get_conn() as conn:
        changed = conn.execute(
            "UPDATE rss_entries SET download_request_key=?,download_backend=? "
            "WHERE id=? AND status='submitting' AND COALESCE(processed,0)=0",
            (request_key, backend, int(entry_id)),
        ).rowcount
        if not changed:
            raise ValueError("RSS 条目状态已变化，请刷新后重试")
        # 先绑定与受理回写处于同一事务，既有受理与并发受理都不漏投影。
        existing = conn.execute(
            "SELECT request_id FROM download_request_keys WHERE request_key=? UNION "
            "SELECT id FROM download_requests WHERE request_key=? LIMIT 1",
            (request_key, request_key),
        ).fetchone()
        if existing:
            _sync_rss_download_entries_conn(conn, int(existing[0]), db.now())


def _sync_rss_download_entries_conn(conn: sqlite3.Connection, request_id: int, timestamp: str) -> int:
    """只投影选定后端的明确受理；已处理不是文件下载完成，不重写手动标记。"""
    row = conn.execute("SELECT status,qb_status,gy_status FROM download_requests WHERE id=?", (request_id,)).fetchone()
    if row is None or row["status"] in {"cancelled", "resubmitted"}:
        return 0
    accepted = tuple(backend for backend, field in (("qb", "qb_status"), ("guangya", "gy_status"))
                     if row[field] in {"submitted", "downloading", "completed"})
    if not accepted:
        return 0
    placeholders = ",".join("?" for _ in accepted)
    return conn.execute(
        "UPDATE rss_entries SET status='downloaded',processed=1,processed_at=?, "
        "failure_code='',failure_retryable=0,failed_at=NULL WHERE COALESCE(processed,0)=0 "
        "AND status IN ('failed','submitting') AND download_request_key IN ("
        "SELECT request_key FROM download_request_keys WHERE request_id=? UNION "
        "SELECT request_key FROM download_requests WHERE id=?) "
        f"AND download_backend IN ({placeholders})",
        (timestamp, request_id, request_id, *accepted),
    ).rowcount


def _reconcile_rss_download_entries_conn(conn: sqlite3.Connection) -> None:
    """启动时分页补关联旧失败，并只按原始链接/日志/持久种子身份确认结果。"""
    import json
    from app.modules.download_dispatcher import (
        DownloadInput,
        normalize_download_url,
        request_keys,
    )

    after_id = 0
    while True:
        legacy = conn.execute(
            "SELECT id,rss_item_id,title,payload FROM rss_entries WHERE status='failed' "
            "AND COALESCE(processed,0)=0 AND download_request_key='' AND id>? "
            "ORDER BY id ASC LIMIT 500",
            (after_id,),
        ).fetchall()
        if not legacy:
            break
        after_id = int(legacy[-1]["id"])
        for entry in legacy:
            try:
                payload = json.loads(entry["payload"] or "{}")
                if not isinstance(payload, dict):
                    continue
                item = normalize_download_url(str(payload.get("torrent_url") or payload.get("link") or ""))
                previous = conn.execute(
                    "SELECT r.*,l.source AS rss_backend FROM download_log l "
                    "JOIN download_requests r ON r.id=l.request_id "
                    "WHERE l.rss_item_id=? AND l.title=? AND r.source_value=? "
                    "AND l.source IN ('qb','guangya') ORDER BY l.id DESC LIMIT 1",
                    (entry["rss_item_id"], entry["title"], item.source_value),
                ).fetchone()
                if previous is None:
                    continue
                # HTTP 种子已被清掉时不能把可变URL冒充已验证内容；保持旧失败供人工核对。
                if previous["kind"] == "http" and not previous["torrent_data"]:
                    continue
                key = request_keys(DownloadInput(
                    previous["kind"], previous["title"] or "", previous["source_value"] or "",
                    torrent_data=previous["torrent_data"],
                ))[0]
            except (ValueError, TypeError):
                continue
            conn.execute(
                "UPDATE rss_entries SET download_request_key=?,download_backend=? WHERE id=?",
                (key, previous["rss_backend"], entry["id"]),
            )

    # 先完成关联，再投影已有明确受理的后端状态；不按标题或失效 URL 猜测成功。
    requests = conn.execute(
        "SELECT DISTINCT k.request_id FROM rss_entries e JOIN download_request_keys k "
        "ON k.request_key=e.download_request_key WHERE e.status IN ('failed','submitting') "
        "AND COALESCE(e.processed,0)=0"
    ).fetchall()
    for row in requests:
        _sync_rss_download_entries_conn(conn, int(row[0]), db.now())


def list_rss_entries(
    sub_id: int | None = None,
    status: str | None = None,
    keyword: str = "",
    limit: int = 300,
    *,
    order: str = "published_desc",
    include_total: bool = False,
) -> list[sqlite3.Row]:
    filters, params = _rss_entry_filters(sub_id, status, keyword)
    # 同一次查询快照计数，LIMIT 只约束返回行数，不截断业务积压数量。
    total_projection = "COUNT(*) OVER () AS total_count," if include_total else ""
    sql = (f"SELECT {total_projection}e.*, i.name AS sub_name,COALESCE(m.media_key,'') AS media_key,"
           "m.season AS media_season,m.episode AS media_episode,"
           "COALESCE(m.skip_reason,'') AS skip_reason FROM rss_entries e "
           "LEFT JOIN rss_items i ON e.rss_item_id=i.id "
           "LEFT JOIN rss_entry_media m ON m.rss_entry_id=e.id" + filters)
    if order == "received_desc":
        sql += " ORDER BY e.id DESC"
    elif order == "received_asc":
        sql += " ORDER BY e.id ASC"
    elif order in {"published_desc", "unprocessed_first"}:
        # pub_date 由 parser 规范为 SQLite 可解析的 YYYY-MM-DD HH:MM；
        # 不可信或缺失日期回退本地入库时间，并用 id 保证稳定顺序。
        sql += " ORDER BY "
        if order == "unprocessed_first":
            sql += "CASE WHEN COALESCE(e.processed,0)=0 THEN 0 ELSE 1 END,"
        sql += (
            "CASE WHEN strftime('%s',e.pub_date) IS NULL THEN 1 ELSE 0 END,"
            " COALESCE(CAST(strftime('%s',e.pub_date) AS INTEGER),"
            " CAST(strftime('%s',e.created_at) AS INTEGER),0) DESC,e.id DESC"
        )
    else:
        raise ValueError("RSS 条目排序方式无效")
    sql += " LIMIT ?"
    params.append(max(1, int(limit)))
    with db.get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def purge_processed_rss_entries(retention_days: int = 7) -> int:
    """清理超过保留期的已处理 RSS 条目；未处理和失败条目不受影响。"""
    days = max(1, int(retention_days or 7))
    with db.get_conn() as conn:
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
    timestamp = db.now()
    with db.get_conn() as conn:
        cur = conn.execute(
            "UPDATE rss_entries SET status='failed', processed=0, processed_at=NULL, "
            "failure_code='submission_outcome_unknown', failure_retryable=0, "
            "failed_at=COALESCE(NULLIF(submitted_at,''),?) "
            "WHERE status='submitting' AND COALESCE(processed,0)=0 "
            "AND datetime(COALESCE(NULLIF(submitted_at,''),created_at)) "
            "< datetime('now','localtime', ?)",
            (timestamp, f"-{minutes} minutes"),
        )
        return int(cur.rowcount or 0)


def get_rss_entries_by_ids(entry_ids: Iterable[int]) -> dict[int, sqlite3.Row]:
    """在同一读快照中批量取得条目及下载配置；不存在的 ID 不伪造记录。"""
    ids = list(dict.fromkeys(entry_ids))
    if not ids:
        return {}
    result: dict[int, sqlite3.Row] = {}
    with db.get_conn() as conn:
        conn.execute("BEGIN")
        for offset in range(0, len(ids), 500):
            batch = ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                "SELECT e.*, i.name AS sub_name, i.download_method, i.qb_save_path, "
                "i.gy_target_dir, i.gy_target_dir_name FROM rss_entries e "
                "LEFT JOIN rss_items i ON e.rss_item_id=i.id "
                f"WHERE e.id IN ({placeholders})", batch,
            ).fetchall()
            result.update((int(row["id"]), row) for row in rows)
    return result


def get_rss_entry(entry_id: int) -> sqlite3.Row | None:
    return next(iter(get_rss_entries_by_ids((entry_id,)).values()), None)


def get_pending_rss_qb_snapshot(
    default_method: str = "qb",
    limit: int = 21,
) -> list[sqlite3.Row]:
    """返回 Agent 确认绑定所需的 qB 待处理条目快照。

    该函数仅供服务端内部使用；调用方不得向客户端投影 title、payload 或路径。
    """
    safe_limit = max(1, min(100, int(limit or 21)))
    normalized_default = str(default_method or "").strip().lower()
    with db.get_conn() as conn:
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
    with db.get_conn() as conn:
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
        submitted_at = db.now()
        cur = conn.execute(
            f"UPDATE rss_entries SET status='submitting', submitted_at=? "
            f"WHERE id IN ({placeholders}) AND status='pending' AND COALESCE(processed,0)=0",
            [submitted_at, *ids],
        )
        if int(cur.rowcount or 0) != len(expected):
            conn.rollback()
            return []
        return rows


_RSS_QB_RETRY_FAILURE_CODES = (
    "qb_unavailable",
    "qb_rate_limited",
    "qb_server_error",
)
_RSS_GUANGYA_RETRY_FAILURE_CODES = (
    "guangya_manifest_unavailable",
    "guangya_unavailable",
    "guangya_rate_limited",
)
_RSS_SAFE_RETRY_FAILURE_CODES = (
    *_RSS_QB_RETRY_FAILURE_CODES,
    *_RSS_GUANGYA_RETRY_FAILURE_CODES,
)
_RSS_RATE_LIMIT_FAILURE_CODES = ("qb_rate_limited", "guangya_rate_limited")
_RSS_FAILURE_CODES = {
    "invalid_payload",
    "missing_torrent_url",
    "qb_auth_failed",
    "qb_rejected",
    "qb_dedupe_busy",
    "qb_outcome_unknown",
    "submission_outcome_unknown",
    "guangya_auth_failed",
    "guangya_submit_failed",
    "guangya_outcome_unknown",
    "unknown_failure",
    *_RSS_SAFE_RETRY_FAILURE_CODES,
}
_RSS_RETRY_ENTRY_SELECT = (
    "SELECT rss_entries.id,rss_entries.rss_item_id,rss_entries.title,"
    "rss_entries.status,rss_entries.processed,rss_entries.created_at,rss_entries.payload,"
    "rss_entries.failure_code,rss_entries.failure_retryable,rss_entries.retry_count,"
    "rss_entries.failed_at,COALESCE(i.download_method,'') AS download_method,"
    "COALESCE(i.qb_save_path,'') AS qb_save_path,"
    "COALESCE(i.gy_target_dir,'') AS gy_target_dir,"
    "COALESCE(i.gy_target_dir_name,'') AS gy_target_dir_name "
    "FROM rss_entries JOIN rss_items i ON rss_entries.rss_item_id=i.id "
)


def rss_retry_entry_snapshot(row) -> dict[str, object]:
    """把 SQLite 行或确认字典归一为稳定的 RSS 重试快照。"""
    data = dict(row)
    return {
        "id": int(data.get("id") or 0),
        "rss_item_id": int(data.get("rss_item_id") or 0),
        "title": str(data.get("title") or ""),
        "payload": str(data.get("payload") or ""),
        "created_at": str(data.get("created_at") or ""),
        "failure_code": str(data.get("failure_code") or ""),
        "failure_retryable": int(data.get("failure_retryable") or 0),
        "retry_count": int(data.get("retry_count") or 0),
        "failed_at": str(data.get("failed_at") or ""),
        "download_method": str(data.get("download_method") or ""),
        "qb_save_path": str(data.get("qb_save_path") or ""),
        "gy_target_dir": str(data.get("gy_target_dir") or ""),
        "gy_target_dir_name": str(data.get("gy_target_dir_name") or ""),
    }


def _rss_retry_eligibility_sql(default_method: str) -> tuple[str, tuple[str, ...]]:
    """共享失败项安全条件；调用者传入 rss_entries 与 rss_items 的 i 联接。"""
    method = "LOWER(COALESCE(NULLIF(TRIM(i.download_method),''),?))"
    safe_codes = ",".join("?" for _ in _RSS_SAFE_RETRY_FAILURE_CODES)
    qb_codes = ",".join("?" for _ in _RSS_QB_RETRY_FAILURE_CODES)
    gy_codes = ",".join("?" for _ in _RSS_GUANGYA_RETRY_FAILURE_CODES)
    rate_codes = ",".join("?" for _ in _RSS_RATE_LIMIT_FAILURE_CODES)
    condition = (
        "COALESCE(rss_entries.failure_retryable,0)=1 "
        "AND COALESCE(rss_entries.retry_count,0)<5 "
        f"AND rss_entries.failure_code IN ({safe_codes}) "
        f"AND (({method}='qb' AND rss_entries.failure_code IN ({qb_codes})) "
        f"OR ({method}='guangya' AND rss_entries.failure_code IN ({gy_codes}))) "
        f"AND (rss_entries.failure_code NOT IN ({rate_codes}) OR ("
        "NULLIF(rss_entries.failed_at,'') IS NOT NULL AND "
        "datetime(rss_entries.failed_at)<=datetime('now','localtime','-60 seconds')))"
    )
    parameters = (
        *_RSS_SAFE_RETRY_FAILURE_CODES,
        default_method, *_RSS_QB_RETRY_FAILURE_CODES,
        default_method, *_RSS_GUANGYA_RETRY_FAILURE_CODES,
        *_RSS_RATE_LIMIT_FAILURE_CODES,
    )
    return condition, parameters


def get_retryable_failed_rss_snapshot(
    default_method: str = "qb",
    limit: int = 21,
) -> list[sqlite3.Row]:
    """返回按订阅当前下载方式筛选的安全 RSS 失败快照。"""
    safe_limit = max(1, min(100, int(limit or 21)))
    normalized_default = str(default_method or "").strip().lower()
    eligibility, parameters = _rss_retry_eligibility_sql(normalized_default)
    with db.get_conn() as conn:
        query = (
            _RSS_RETRY_ENTRY_SELECT
            + "WHERE rss_entries.status='failed' AND COALESCE(rss_entries.processed,0)=0 "
            + f"AND {eligibility} "
            + "ORDER BY COALESCE(NULLIF(rss_entries.failed_at,''),"
            + "NULLIF(rss_entries.submitted_at,''),rss_entries.created_at) DESC, "
            + "rss_entries.id DESC LIMIT ?"
        )
        return conn.execute(query, (*parameters, safe_limit)).fetchall()


def claim_retryable_failed_rss_entries(
    expected_rows: list[dict],
    default_method: str = "qb",
) -> list[sqlite3.Row]:
    """全有或全无地复核并认领 Agent 已确认的安全 RSS 失败集合。"""
    normalized_default = str(default_method or "").strip().lower()
    expected = [rss_retry_entry_snapshot(raw) for raw in expected_rows]
    ids = [item["id"] for item in expected]
    if not ids or len(ids) > 20 or any(entry_id <= 0 for entry_id in ids):
        return []
    if len(set(ids)) != len(ids):
        return []

    placeholders = ",".join("?" for _ in ids)
    eligibility, parameters = _rss_retry_eligibility_sql(normalized_default)
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        query = (
            _RSS_RETRY_ENTRY_SELECT
            + f"WHERE rss_entries.id IN ({placeholders}) "
            + "AND rss_entries.status='failed' AND COALESCE(rss_entries.processed,0)=0 "
            + f"AND {eligibility} "
            + "ORDER BY COALESCE(NULLIF(rss_entries.failed_at,''),"
            + "NULLIF(rss_entries.submitted_at,''),rss_entries.created_at) DESC, "
            + "rss_entries.id DESC"
        )
        rows = conn.execute(query, (*ids, *parameters)).fetchall()
        if [rss_retry_entry_snapshot(row) for row in rows] != expected:
            conn.rollback()
            return []

        submitted_at = db.now()
        cur = conn.execute(
            f"UPDATE rss_entries SET status='submitting', submitted_at=?, "
            "failure_code='', failure_retryable=0, failed_at=NULL, "
            "retry_count=COALESCE(retry_count,0)+1 "
            f"WHERE id IN ({placeholders}) AND status='failed' AND COALESCE(processed,0)=0",
            [submitted_at, *ids],
        )
        if int(cur.rowcount or 0) != len(expected):
            conn.rollback()
            return []
        return rows


def claim_rss_entry(entry_id: int) -> bool:
    """原子认领待处理项或满足当前后端安全策略的失败项。"""
    from app.config import get as get_config

    default_method = str(get_config("RSS_DOWNLOAD_METHOD", "qb") or "qb").strip().lower()
    eligibility, parameters = _rss_retry_eligibility_sql(default_method)
    with db.get_conn() as conn:
        cur = conn.execute(
            "UPDATE rss_entries SET status='submitting', submitted_at=?, "
            "retry_count=COALESCE(retry_count,0)+CASE WHEN status='failed' THEN 1 ELSE 0 END, "
            "failure_code='', failure_retryable=0, failed_at=NULL WHERE id=? "
            "AND COALESCE(processed,0)=0 AND (status='pending' OR ("
            "status='failed' AND EXISTS ("
            "SELECT 1 FROM rss_items i WHERE i.id=rss_entries.rss_item_id AND "
            f"({eligibility}))))",
            (db.now(), int(entry_id), *parameters),
        )
        return cur.rowcount == 1


def record_rss_entry_failure(
    entry_id: int, failure_code: str, retryable: bool, *, request_id: int = 0,
) -> None:
    """记录稳定失败分类；不保存上游正文、URL 或异常原文。"""
    normalized = str(failure_code or "").strip().lower()
    if normalized not in _RSS_FAILURE_CODES:
        normalized = "unknown_failure"
        retryable = False
    failed_at = db.now()
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        scope, params = "id=?", [int(entry_id)]
        if request_id:
            # 仅当前资源身份的请求可以结束等待者；迟到旧回执不能覆盖后继提交。
            binding = conn.execute(
                "SELECT download_request_key,download_backend FROM rss_entries e WHERE id=? "
                "AND download_request_key IN (SELECT request_key FROM download_request_keys "
                "WHERE request_id=? UNION SELECT request_key FROM download_requests WHERE id=?)",
                (int(entry_id), int(request_id), int(request_id)),
            ).fetchone()
            if binding is None:
                return
            scope += (
                " OR (status='submitting' AND download_backend=? AND download_request_key IN ("
                "SELECT request_key FROM download_request_keys WHERE request_id=? UNION "
                "SELECT request_key FROM download_requests WHERE id=?))"
            )
            params.extend((binding["download_backend"], int(request_id), int(request_id)))
        conn.execute(
            "UPDATE rss_entries SET status='failed', processed=0, processed_at=NULL, "
            "submitted_at=?, failure_code=?, failure_retryable=?, failed_at=? "
            f"WHERE COALESCE(processed,0)=0 AND ({scope})",
            (failed_at, normalized, int(bool(retryable)), failed_at, *params),
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
    stamp = db.now()
    with db.get_conn() as conn:
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
    processed_at = db.now() if processed else None
    allowed_statuses = ("pending", "failed", "skipped") if processed else ("failed", "skipped")
    allowed = ",".join("?" for _ in allowed_statuses)
    with db.get_conn() as conn:
        cur = conn.execute(
            f"UPDATE rss_entries SET processed=?, processed_at=?, status=?, "
            "failure_code='', failure_retryable=0, failed_at=NULL "
            f"WHERE id IN ({placeholders}) AND status IN ({allowed})",
            [1 if processed else 0, processed_at, status, *ids, *allowed_statuses],
        )
        if not processed and cur.rowcount:
            conn.execute(
                f"UPDATE rss_entry_media SET skip_reason='',updated_at=? "
                f"WHERE rss_entry_id IN ({placeholders})",
                [db.now(), *ids],
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
    processed_at = db.now() if processed else None
    with db.get_conn() as conn:
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
            conn.execute(
                f"UPDATE rss_entry_media SET skip_reason='',updated_at=? "
                f"WHERE rss_entry_id IN ({placeholders})",
                [db.now(), *ids],
            )
        return int(cur.rowcount or 0)


def get_rss_manual_review_summary(sub_id: int) -> dict[str, int]:
    """汇总订阅仍未处理的终态失败，供调度告警跨轮次重试与恢复。"""
    with db.get_conn() as conn:
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
    snapshot = current_time or db.now()
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

    with db.get_conn() as conn:
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

    with db.get_conn() as conn:
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
    snapshot = current_time or db.now()
    bounded_hours = max(1, min(24 * 31, int(hours or 24)))
    with db.get_conn() as conn:
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
    snapshot = current_time or db.now()
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
        current_time or db.now(),
        subscription_id=int(subscription_id),
        limit=1,
    )
    return _project_rss_subscription_safe_summary(rows[0]) if rows else None


def list_due_rss_subscriptions(current_time: str | None = None) -> list[sqlite3.Row]:
    current_time = current_time or db.now()
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM rss_items WHERE enabled=1 AND refresh_interval_minutes>0 AND ("
            "last_refreshed_at IS NULL OR last_refreshed_at='' OR datetime(last_refreshed_at) IS NULL OR "
            "datetime(last_refreshed_at, '+' || refresh_interval_minutes || ' minutes') <= datetime(?)"
            ") ORDER BY id",
            (current_time,),
        ).fetchall()


def _recover_after_restart(conn, timestamp: str) -> None:
    """仅收束超过十五分钟的未知提交结果，不自动重投外部请求。"""
    conn.execute(
        "UPDATE rss_entries SET status='failed', processed=0, processed_at=NULL, "
        "failure_code='submission_outcome_unknown', failure_retryable=0, "
        "failed_at=COALESCE(NULLIF(submitted_at,''),?) "
        "WHERE status='submitting' "
        "AND datetime(COALESCE(NULLIF(submitted_at,''),created_at)) "
        "< datetime('now','localtime','-15 minutes')",
        (timestamp,),
    )
    _reconcile_rss_download_entries_conn(conn)


# 在函数定义后绑定门面，兼容 repository-first 导入；运行期始终使用同一连接/时钟所有者。
from app import database as db  # noqa: E402
