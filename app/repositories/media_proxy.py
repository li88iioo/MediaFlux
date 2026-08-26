"""Emby / Jellyfin 多实例媒体反代的数据访问。"""
from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app.modules.media_proxy_safety import safe_media_name


if TYPE_CHECKING:
    from types import ModuleType


def _database() -> "ModuleType":
    """延迟取得数据库门面，保持测试数据库、时间和路径补丁兼容。"""
    from app import database

    return database


def get_conn():
    return _database().get_conn()


def now() -> str:
    return _database().now()


def resolve_db_path():
    return _database().resolve_db_path()


def add_media_proxy_instance(
    *,
    name: str,
    server_type: str,
    config_source: str = "custom",
    upstream_url: str,
    api_key: str,
    listen_host: str,
    listen_port: int,
    local_root: str = "",
    trust_forwarded_headers: int = 0,
    trusted_proxy_cidrs_json: str = "[]",
    enabled: int = 1,
) -> int:
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO media_proxy_instances("
            "name,server_type,config_source,upstream_url,api_key,listen_host,listen_port,local_root,"
            "trust_forwarded_headers,trusted_proxy_cidrs_json,"
            "enabled,status,last_error,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                name,
                server_type,
                config_source,
                upstream_url,
                api_key,
                listen_host,
                int(listen_port),
                local_root,
                1 if trust_forwarded_headers else 0,
                str(trusted_proxy_cidrs_json or "[]"),
                1 if enabled else 0,
                "stopped",
                "",
                timestamp,
                timestamp,
            ),
        )
        return int(cur.lastrowid)


def list_media_proxy_instances() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM media_proxy_instances ORDER BY id ASC"
        ).fetchall()


def get_media_proxy_instance(instance_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM media_proxy_instances WHERE id=?",
            (int(instance_id),),
        ).fetchone()


def update_media_proxy_instance(instance_id: int, fields: dict) -> bool:
    allowed = {
        "name", "server_type", "config_source", "upstream_url", "api_key", "listen_host",
        "listen_port", "local_root", "trust_forwarded_headers",
        "trusted_proxy_cidrs_json", "enabled", "status", "last_error",
    }
    sets: list[str] = []
    values: list = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        sets.append(f"{key}=?")
        values.append(value)
    if not sets:
        return False
    sets.append("updated_at=?")
    values.extend([now(), int(instance_id)])
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE media_proxy_instances SET {', '.join(sets)} WHERE id=?",
            values,
        )
        return cur.rowcount > 0


def delete_media_proxy_instance(instance_id: int) -> bool:
    normalized_id = int(instance_id)
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM media_proxy_playback_records WHERE instance_id=?",
            (normalized_id,),
        )
        conn.execute(
            "DELETE FROM media_proxy_playback_sessions WHERE instance_id=?",
            (normalized_id,),
        )
        cur = conn.execute(
            "DELETE FROM media_proxy_instances WHERE id=?",
            (normalized_id,),
        )
        return cur.rowcount > 0


def add_media_proxy_binding(
    *,
    instance_id: int,
    media_item_id: str,
    media_source_id: str,
    source_type: str,
    guangya_file_id: str = "",
    local_relative_path: str = "",
    enabled: int = 1,
) -> int:
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO media_proxy_bindings("
            "instance_id,media_item_id,media_source_id,source_type,guangya_file_id,"
            "local_relative_path,enabled,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (
                int(instance_id),
                media_item_id,
                media_source_id,
                source_type,
                guangya_file_id,
                local_relative_path,
                1 if enabled else 0,
                timestamp,
                timestamp,
            ),
        )
        return int(cur.lastrowid)


def create_media_proxy_binding(
    *,
    instance_id: int,
    media_item_id: str,
    media_source_id: str,
    source_type: str,
    guangya_file_id: str = "",
    local_relative_path: str = "",
    enabled: int = 1,
) -> sqlite3.Row:
    """在同一事务内创建并回读绑定，避免并发删除造成响应竞态。"""
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO media_proxy_bindings("
            "instance_id,media_item_id,media_source_id,source_type,guangya_file_id,"
            "local_relative_path,enabled,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (
                int(instance_id),
                media_item_id,
                media_source_id,
                source_type,
                guangya_file_id,
                local_relative_path,
                1 if enabled else 0,
                timestamp,
                timestamp,
            ),
        )
        row = conn.execute(
            "SELECT * FROM media_proxy_bindings WHERE id=?",
            (int(cur.lastrowid),),
        ).fetchone()
        if row is None:  # pragma: no cover - 同事务内 INSERT 后必须可见
            raise RuntimeError("媒体绑定创建后无法回读")
        return row


def list_media_proxy_bindings(instance_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM media_proxy_bindings WHERE instance_id=? ORDER BY id ASC",
            (int(instance_id),),
        ).fetchall()


def get_media_proxy_binding(
    instance_id: int,
    media_item_id: str,
    media_source_id: str = "",
) -> sqlite3.Row | None:
    with get_conn() as conn:
        if media_source_id:
            row = conn.execute(
                "SELECT * FROM media_proxy_bindings WHERE instance_id=? "
                "AND media_item_id=? AND media_source_id=? AND enabled=1",
                (int(instance_id), media_item_id, media_source_id),
            ).fetchone()
            if row:
                return row
        return conn.execute(
            "SELECT * FROM media_proxy_bindings WHERE instance_id=? "
            "AND media_item_id=? AND enabled=1 ORDER BY id LIMIT 1",
            (int(instance_id), media_item_id),
        ).fetchone()


def delete_media_proxy_binding(binding_id: int, instance_id: int | None = None) -> bool:
    with get_conn() as conn:
        if instance_id is None:
            cur = conn.execute(
                "DELETE FROM media_proxy_bindings WHERE id=?",
                (int(binding_id),),
            )
        else:
            cur = conn.execute(
                "DELETE FROM media_proxy_bindings WHERE id=? AND instance_id=?",
                (int(binding_id), int(instance_id)),
            )
        return cur.rowcount > 0


_MEDIA_PROXY_ERROR_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_MEDIA_PROXY_ERROR_SECRET_RE = re.compile(
    r"(?i)(authorization\s*:\s*(?:bearer\s+)?|bearer\s+|"
    r"(?:api[_-]?key|token|access[_-]?token|refresh[_-]?token)\s*[=:]\s*)"
    r"[^\s,;]+"
)
_MEDIA_PROXY_RECORD_SOURCES = {
    "guangya", "upstream", "playback_info", "hls", "websocket", "unknown",
}
_MEDIA_PROXY_RECORD_MAX_ROWS = 10_000
_MEDIA_PROXY_RECORD_MAINTENANCE_INTERVAL = 512
_last_media_proxy_record_prune_key = ""
_media_proxy_record_writes_since_prune = 0
_media_proxy_record_maintenance_lock = threading.RLock()


def _redact_media_proxy_record_error(value: str) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")[:4000]
    text = _MEDIA_PROXY_ERROR_URL_RE.sub("[URL]", text)
    text = _MEDIA_PROXY_ERROR_SECRET_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
    return text[:1000]


def _normalized_record_identity(value: str, limit: int = 255) -> str:
    return str(value or "").strip()[:limit]


def _normalized_record_media_name(value: str, limit: int = 256) -> str:
    return safe_media_name(value, limit=limit)


def _delete_orphan_media_proxy_playback_sessions(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM media_proxy_playback_sessions "
        "WHERE id NOT IN ("
        "SELECT DISTINCT session_id FROM media_proxy_playback_records "
        "WHERE session_id IS NOT NULL"
        ")"
    )


def _prune_media_proxy_playback_records(
    conn: sqlite3.Connection, *, record_write: bool = False
) -> None:
    global _last_media_proxy_record_prune_key
    global _media_proxy_record_writes_since_prune
    with _media_proxy_record_maintenance_lock:
        interval = max(1, int(_MEDIA_PROXY_RECORD_MAINTENANCE_INTERVAL))
        if record_write:
            _media_proxy_record_writes_since_prune += 1
        current_day = datetime.now().strftime("%Y-%m-%d")
        prune_key = f"{resolve_db_path()}:{current_day}"
        interval_due = _media_proxy_record_writes_since_prune >= interval
        if not interval_due and _last_media_proxy_record_prune_key == prune_key:
            return

        # 采用低水位批量裁剪：维护之间最多新增 interval 条，仍保持
        # _MEDIA_PROXY_RECORD_MAX_ROWS 的严格上限，避免每次 302 都扫描 1 万行。
        trim_target = max(
            1,
            int(_MEDIA_PROXY_RECORD_MAX_ROWS) - interval,
        )
        conn.execute(
            "DELETE FROM media_proxy_playback_records "
            "WHERE created_at < datetime('now','-30 days','localtime')"
        )
        conn.execute(
            "DELETE FROM media_proxy_playback_records WHERE id IN ("
            "SELECT id FROM media_proxy_playback_records "
            "ORDER BY id DESC LIMIT -1 OFFSET ?"
            ")",
            (trim_target,),
        )
        _delete_orphan_media_proxy_playback_sessions(conn)
        _last_media_proxy_record_prune_key = prune_key
        _media_proxy_record_writes_since_prune = 0


def _upsert_media_proxy_playback_session(
    conn: sqlite3.Connection,
    *,
    instance_id: int,
    session_key: str,
    media_item_id: str,
    media_source_id: str,
    media_name: str,
    guangya_file_id: str,
    route_class: str,
    source: str,
    status_code: int,
    cache_hit: bool,
    upstream_latency_ms: int,
    total_latency_ms: int,
    failure_stage: str,
    error: str,
    timestamp: str,
) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO media_proxy_playback_sessions("
        "instance_id,session_key,media_item_id,media_source_id,media_name,"
        "guangya_file_id,started_at,last_request_at"
        ") VALUES(?,?,?,?,?,?,?,?)",
        (
            int(instance_id), session_key, media_item_id, media_source_id,
            media_name, guangya_file_id, timestamp, timestamp,
        ),
    )
    success_increment = 1 if 200 <= status_code <= 399 else 0
    error_increment = 1 if status_code == 0 or status_code >= 400 else 0
    cache_hit_increment = 1 if source == "guangya" and cache_hit else 0
    cache_miss_increment = 1 if source == "guangya" and not cache_hit else 0
    conn.execute(
        "UPDATE media_proxy_playback_sessions SET "
        "media_item_id=CASE WHEN ?<>'' THEN ? ELSE media_item_id END,"
        "media_source_id=CASE WHEN ?<>'' THEN ? ELSE media_source_id END,"
        "media_name=CASE WHEN ?<>'' THEN ? ELSE media_name END,"
        "guangya_file_id=CASE WHEN ?<>'' THEN ? ELSE guangya_file_id END,"
        "request_count=request_count+1,success_count=success_count+?,"
        "error_count=error_count+?,cache_hit_count=cache_hit_count+?,"
        "cache_miss_count=cache_miss_count+?,"
        "upstream_latency_ms_total=upstream_latency_ms_total+?,"
        "total_latency_ms_total=total_latency_ms_total+?,"
        "max_total_latency_ms=MAX(max_total_latency_ms,?),"
        "last_route_class=?,last_source=?,last_status_code=?,"
        "last_failure_stage=CASE WHEN ?<>'' THEN ? ELSE last_failure_stage END,"
        "last_error=CASE WHEN ?<>'' THEN ? ELSE last_error END,last_request_at=? "
        "WHERE instance_id=? AND session_key=?",
        (
            media_item_id, media_item_id,
            media_source_id, media_source_id,
            media_name, media_name,
            guangya_file_id, guangya_file_id,
            success_increment, error_increment,
            cache_hit_increment, cache_miss_increment,
            upstream_latency_ms, total_latency_ms, total_latency_ms,
            route_class, source, status_code,
            failure_stage, failure_stage, error, error, timestamp,
            int(instance_id), session_key,
        ),
    )
    row = conn.execute(
        "SELECT id FROM media_proxy_playback_sessions "
        "WHERE instance_id=? AND session_key=?",
        (int(instance_id), session_key),
    ).fetchone()
    if row is None:
        raise sqlite3.DatabaseError("播放会话摘要写入失败")
    return int(row["id"])


def record_media_proxy_playback_attempt(*, instance_id: int, route_class: str,
                                        method: str, status_code: int,
                                        source: str, cache_hit: bool = False,
                                        upstream_latency_ms: int = 0,
                                        total_latency_ms: int = 0,
                                        failure_stage: str = "",
                                        error: str = "",
                                        playback_session_key: str = "",
                                        media_item_id: str = "",
                                        media_source_id: str = "",
                                        guangya_file_id: str = "",
                                        media_name: str = "") -> int:
    route = str(route_class or "unknown").strip()[:64] or "unknown"
    normalized_method = str(method or "GET").strip().upper()[:12] or "GET"
    normalized_source = str(source or "unknown").strip().lower()
    if normalized_source not in _MEDIA_PROXY_RECORD_SOURCES:
        normalized_source = "unknown"
    status = max(0, min(int(status_code or 0), 999))
    upstream_ms = max(0, min(int(upstream_latency_ms or 0), 86_400_000))
    total_ms = max(0, min(int(total_latency_ms or 0), 86_400_000))
    stage = str(failure_stage or "").strip()[:64]
    safe_error = _redact_media_proxy_record_error(error)
    session_key = _normalized_record_identity(playback_session_key, 96)
    item_id = _normalized_record_identity(media_item_id)
    source_id = _normalized_record_identity(media_source_id)
    safe_media_name = _normalized_record_media_name(media_name)
    file_id = _normalized_record_identity(guangya_file_id, 512)
    timestamp = now()
    with get_conn() as conn:
        _prune_media_proxy_playback_records(
            conn, record_write=True
        )
        session_id = None
        if session_key:
            session_id = _upsert_media_proxy_playback_session(
                conn,
                instance_id=int(instance_id),
                session_key=session_key,
                media_item_id=item_id,
                media_source_id=source_id,
                media_name=safe_media_name,
                guangya_file_id=file_id,
                route_class=route,
                source=normalized_source,
                status_code=status,
                cache_hit=bool(cache_hit),
                upstream_latency_ms=upstream_ms,
                total_latency_ms=total_ms,
                failure_stage=stage,
                error=safe_error,
                timestamp=timestamp,
            )
        cursor = conn.execute(
            "INSERT INTO media_proxy_playback_records("
            "instance_id,session_id,route_class,method,status_code,source,cache_hit,"
            "upstream_latency_ms,total_latency_ms,failure_stage,error,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                int(instance_id), session_id, route, normalized_method, status,
                normalized_source, 1 if cache_hit else 0, upstream_ms, total_ms,
                stage, safe_error, timestamp,
            ),
        )
        record_id = int(cursor.lastrowid)
        return record_id


def _playback_filter_clauses(*, instance_id: int | None, status: str,
                             source: str, status_column: str,
                             source_column: str) -> tuple[list[str], list]:
    clauses = ["1=1"]
    params: list = []
    if instance_id is not None:
        clauses.append("instance_id=?")
        params.append(int(instance_id))
    normalized_status = str(status or "").strip().lower()
    if normalized_status:
        if normalized_status == "success":
            clauses.append(f"{status_column} BETWEEN 200 AND 399")
        elif normalized_status == "error":
            clauses.append(f"({status_column}=0 OR {status_column}>=400)")
        elif normalized_status.isdigit():
            clauses.append(f"{status_column}=?")
            params.append(int(normalized_status))
        else:
            raise ValueError("播放记录状态筛选无效")
    normalized_source = str(source or "").strip().lower()
    if normalized_source:
        if normalized_source not in _MEDIA_PROXY_RECORD_SOURCES:
            raise ValueError("播放记录来源筛选无效")
        clauses.append(f"{source_column}=?")
        params.append(normalized_source)
    return clauses, params


def list_media_proxy_playback_records(*, instance_id: int | None = None,
                                      session_id: int | None = None,
                                      unlinked: bool = False,
                                      status: str = "", source: str = "",
                                      page: int = 1, page_size: int = 50) -> dict:
    clauses, params = _playback_filter_clauses(
        instance_id=instance_id,
        status=status,
        source=source,
        status_column="status_code",
        source_column="source",
    )
    if session_id is not None and unlinked:
        raise ValueError("播放记录会话筛选冲突")
    if session_id is not None:
        clauses.append("session_id=?")
        params.append(int(session_id))
    elif unlinked:
        clauses.append("session_id IS NULL")
    normalized_page = max(1, int(page or 1))
    normalized_size = max(1, min(int(page_size or 50), 200))
    where = " AND ".join(clauses)
    with get_conn() as conn:
        _prune_media_proxy_playback_records(conn)
        total = int(conn.execute(
            f"SELECT COUNT(*) AS count FROM media_proxy_playback_records WHERE {where}",
            params,
        ).fetchone()["count"])
        rows = conn.execute(
            "SELECT id,instance_id,session_id,route_class,method,status_code,source,cache_hit,"
            "upstream_latency_ms,total_latency_ms,failure_stage,error,created_at "
            f"FROM media_proxy_playback_records WHERE {where} "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, normalized_size, (normalized_page - 1) * normalized_size],
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        total_ms = max(0, int(item["total_latency_ms"] or 0))
        upstream_ms = max(0, int(item["upstream_latency_ms"] or 0))
        item["internal_latency_ms"] = max(0, total_ms - upstream_ms)
        items.append(item)
    return {
        "items": items,
        "total": total,
        "page": normalized_page,
        "page_size": normalized_size,
    }


def list_media_proxy_playback_sessions(*, instance_id: int | None = None,
                                       status: str = "", source: str = "",
                                       page: int = 1, page_size: int = 20) -> dict:
    clauses, params = _playback_filter_clauses(
        instance_id=instance_id,
        status=status,
        source=source,
        status_column="last_status_code",
        source_column="last_source",
    )
    normalized_page = max(1, int(page or 1))
    normalized_size = max(1, min(int(page_size or 20), 100))
    where = " AND ".join(clauses)
    legacy_clauses, legacy_params = _playback_filter_clauses(
        instance_id=instance_id,
        status=status,
        source=source,
        status_column="status_code",
        source_column="source",
    )
    legacy_clauses.append("session_id IS NULL")
    legacy_where = " AND ".join(legacy_clauses)
    with get_conn() as conn:
        _prune_media_proxy_playback_records(conn)
        total = int(conn.execute(
            f"SELECT COUNT(*) AS count FROM media_proxy_playback_sessions WHERE {where}",
            params,
        ).fetchone()["count"])
        unlinked_total = int(conn.execute(
            f"SELECT COUNT(*) AS count FROM media_proxy_playback_records WHERE {legacy_where}",
            legacy_params,
        ).fetchone()["count"])
        rows = conn.execute(
            "SELECT id,instance_id,media_item_id,media_source_id,media_name,guangya_file_id,"
            "request_count,success_count,error_count,cache_hit_count,cache_miss_count,"
            "upstream_latency_ms_total,total_latency_ms_total,max_total_latency_ms,"
            "last_route_class,last_source,last_status_code,last_failure_stage,last_error,"
            "started_at,last_request_at "
            f"FROM media_proxy_playback_sessions WHERE {where} "
            "ORDER BY last_request_at DESC,id DESC LIMIT ? OFFSET ?",
            [*params, normalized_size, (normalized_page - 1) * normalized_size],
        ).fetchall()
        session_ids = [int(row["id"]) for row in rows]
        stage_metrics = {}
        if session_ids:
            placeholders = ",".join("?" for _ in session_ids)
            metric_rows = conn.execute(
                "SELECT session_id,"
                "SUM(CASE WHEN source='playback_info' THEN 1 ELSE 0 END) "
                "AS playback_info_request_count,"
                "SUM(CASE WHEN source='playback_info' THEN total_latency_ms ELSE 0 END) "
                "AS playback_info_latency_ms_total,"
                "SUM(CASE WHEN route_class='guangya_direct' AND status_code=302 "
                "THEN 1 ELSE 0 END) AS redirect_request_count,"
                "SUM(CASE WHEN route_class='guangya_direct' AND status_code=302 "
                "THEN total_latency_ms ELSE 0 END) AS redirect_latency_ms_total "
                "FROM media_proxy_playback_records "
                f"WHERE session_id IN ({placeholders}) GROUP BY session_id",
                session_ids,
            ).fetchall()
            stage_metrics = {int(row["session_id"]): dict(row) for row in metric_rows}
    items = []
    for row in rows:
        item = dict(row)
        count = max(1, int(item["request_count"] or 0))
        item["average_total_latency_ms"] = int(item["total_latency_ms_total"] or 0) // count
        item["average_upstream_latency_ms"] = int(item["upstream_latency_ms_total"] or 0) // count
        metrics = stage_metrics.get(int(item["id"]), {})
        playback_count = int(metrics.get("playback_info_request_count") or 0)
        redirect_count = int(metrics.get("redirect_request_count") or 0)
        item["playback_info_request_count"] = playback_count
        item["redirect_request_count"] = redirect_count
        item["average_playback_info_latency_ms"] = (
            int(metrics.get("playback_info_latency_ms_total") or 0) // playback_count
            if playback_count else 0
        )
        item["average_redirect_latency_ms"] = (
            int(metrics.get("redirect_latency_ms_total") or 0) // redirect_count
            if redirect_count else 0
        )
        items.append(item)
    return {
        "items": items,
        "total": total,
        "page": normalized_page,
        "page_size": normalized_size,
        "unlinked_total": unlinked_total,
    }


def clear_media_proxy_playback_records(instance_id: int | None = None) -> int:
    with get_conn() as conn:
        if instance_id is None:
            cursor = conn.execute("DELETE FROM media_proxy_playback_records")
            conn.execute("DELETE FROM media_proxy_playback_sessions")
        else:
            cursor = conn.execute(
                "DELETE FROM media_proxy_playback_records WHERE instance_id=?",
                (int(instance_id),),
            )
            conn.execute(
                "DELETE FROM media_proxy_playback_sessions WHERE instance_id=?",
                (int(instance_id),),
            )
        return int(cursor.rowcount)


def get_media_proxy_playback_failure_summary(
    *, hours: int = 24, instance_id: int | None = None
) -> dict:
    """聚合固定时间窗内已记录的反代请求，不返回媒体身份或错误正文。"""
    normalized_hours = int(hours)
    if normalized_hours not in {1, 6, 24, 72}:
        raise ValueError("播放故障摘要仅支持 1、6、24 或 72 小时")
    cutoff = (datetime.now() - timedelta(hours=normalized_hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    clauses = ["created_at>=?"]
    params: list = [cutoff]
    if instance_id is not None:
        clauses.append("instance_id=?")
        params.append(int(instance_id))
    where = " AND ".join(clauses)
    with get_conn() as conn:
        _prune_media_proxy_playback_records(conn)
        totals = conn.execute(
            "SELECT COUNT(*) AS total,"
            "SUM(CASE WHEN status_code=0 OR status_code>=400 THEN 1 ELSE 0 END) AS failed,"
            "SUM(CASE WHEN cache_hit=1 THEN 1 ELSE 0 END) AS cache_hits,"
            "AVG(CASE WHEN total_latency_ms>=0 THEN total_latency_ms END) AS avg_latency "
            f"FROM media_proxy_playback_records WHERE {where}",
            params,
        ).fetchone()
        stages = conn.execute(
            "SELECT CASE WHEN TRIM(failure_stage)='' THEN 'unknown' ELSE failure_stage END AS value,"
            "COUNT(*) AS total FROM media_proxy_playback_records "
            f"WHERE {where} AND (status_code=0 OR status_code>=400) "
            "GROUP BY value ORDER BY total DESC,value LIMIT 8",
            params,
        ).fetchall()
        routes = conn.execute(
            "SELECT route_class AS value,COUNT(*) AS total "
            f"FROM media_proxy_playback_records WHERE {where} "
            "GROUP BY route_class ORDER BY total DESC,value LIMIT 8",
            params,
        ).fetchall()
        last = conn.execute(
            "SELECT MAX(created_at) AS last_seen FROM media_proxy_playback_records "
            f"WHERE {where}", params,
        ).fetchone()
    total = int(totals["total"] or 0) if totals else 0
    failed = int(totals["failed"] or 0) if totals else 0
    return {
        "window_hours": normalized_hours,
        "total_recorded": total,
        "failed": failed,
        "success": max(0, total - failed),
        "cache_hits": int(totals["cache_hits"] or 0) if totals else 0,
        "average_latency_ms": (
            round(float(totals["avg_latency"]), 1)
            if totals is not None and totals["avg_latency"] is not None else None
        ),
        "failure_stages": [
            {"stage": str(row["value"]), "count": int(row["total"])} for row in stages
        ],
        "route_classes": [
            {"route": str(row["value"]), "count": int(row["total"])} for row in routes
        ],
        "last_seen_at": str(last["last_seen"] or "") if last else "",
        "coverage": "recorded_proxy_requests_only",
    }
