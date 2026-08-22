"""下载日志与统一下载请求的数据访问。"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Iterable

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


class DownloadAdmissionBindingError(RuntimeError):
    """媒体订阅下载准入无法在外部提交前绑定到持久化请求。"""


def add_download_log(source: str, title: str = "", path: str = "",
                     rss_item_id: int | None = None, status: str = "submitted",
                     request_id: int | None = None, backend_task_id: str = "",
                     progress: float = 0, error: str = "") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO download_log(source,title,path,status,rss_item_id,request_id,"
            "backend_task_id,progress,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (source, title, path, status, rss_item_id, request_id, backend_task_id,
             max(0.0, min(float(progress or 0), 1.0)), error, now(), now()),
        )
        return cur.lastrowid


def update_download_log(log_id: int, **fields) -> None:
    allowed = {"status", "backend_task_id", "progress", "error", "completed_at", "path", "title"}
    sets, values = [], []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key}=?")
            values.append(value)
    if not sets:
        return
    sets.append("updated_at=?")
    values.extend([now(), log_id])
    with get_conn() as conn:
        conn.execute(f"UPDATE download_log SET {', '.join(sets)} WHERE id=?", values)


def _download_log_filters(source: str | None = None, status: str | None = None,
                          keyword: str = "") -> tuple[str, list]:
    sql = " WHERE 1=1"
    params: list = []
    if source:
        sql += " AND source=?"
        params.append(source)
    if status:
        sql += " AND status=?"
        params.append(status)
    if keyword:
        sql += " AND (title LIKE ? OR path LIKE ? OR backend_task_id LIKE ? OR error LIKE ?)"
        value = f"%{keyword}%"
        params.extend([value, value, value, value])
    return sql, params


def list_download_logs(source: str | None = None, status: str | None = None,
                       keyword: str = "", limit: int = 20,
                       offset: int = 0) -> list[sqlite3.Row]:
    filters, params = _download_log_filters(source, status, keyword)
    sql = "SELECT * FROM download_log" + filters + " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([max(1, int(limit)), max(0, int(offset))])
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def count_download_logs(source: str | None = None, status: str | None = None,
                        keyword: str = "") -> int:
    filters, params = _download_log_filters(source, status, keyword)
    with get_conn() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM download_log" + filters, params
        ).fetchone()[0])


def delete_download_logs(log_ids: list[int]) -> list[int]:
    """删除指定下载日志并返回实际删除的 ID；不操作下载请求、后端任务或文件。"""
    normalized = list(dict.fromkeys(
        int(value) for value in log_ids if int(value) > 0
    ))
    if not normalized:
        return []
    placeholders = ",".join("?" for _ in normalized)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = {
            int(row["id"]) for row in conn.execute(
                f"SELECT id FROM download_log WHERE id IN ({placeholders})",
                normalized,
            ).fetchall()
        }
        if existing:
            delete_ids = [value for value in normalized if value in existing]
            delete_placeholders = ",".join("?" for _ in delete_ids)
            conn.execute(
                f"DELETE FROM download_log WHERE id IN ({delete_placeholders})",
                delete_ids,
            )
    return [value for value in normalized if value in existing]


_DOWNLOAD_ATTENTION_BASE_WHERE = (
    "status IN ('failed','manual_review') OR "
    "qb_status IN ('failed','manual_review') OR gy_status IN ('failed','manual_review') OR "
    "local_import_status='failed' OR organize_started<0 OR "
    "organize_status='failed' OR strm_status='failed' OR "
    "gy_staging_cleanup_status IN ('retained','failed')"
)


_DOWNLOAD_ATTENTION_WHERE = (
    f"({_DOWNLOAD_ATTENTION_BASE_WHERE}) AND COALESCE(attention_cleared_at,'')=''"
)


def _normalized_request_keys(
    request_key: str, alternate_request_keys: Iterable[str] | None,
) -> tuple[str, ...]:
    values = [str(request_key or "").strip()]
    values.extend(str(value or "").strip() for value in (alternate_request_keys or ()))
    normalized = tuple(dict.fromkeys(value for value in values if value))
    if not normalized:
        raise ValueError("下载请求 key 不能为空")
    # 当前调用方只提供同一内容的协议身份；上限避免未来误把不受控集合传入 SQL。
    return normalized[:16]


def _preferred_request_row(rows, primary_key: str):
    return min(
        rows,
        key=lambda row: (
            0 if str(row["request_key"] or "") == primary_key else 1,
            -int(row["id"]),
        ),
    )


def _request_rows_for_keys(
    conn: sqlite3.Connection,
    keys: tuple[str, ...],
    *,
    columns: str = "*",
):
    """读取规范化请求 key 当前所有者。"""
    placeholders = ",".join("?" for _ in keys)
    return conn.execute(
        f"SELECT {columns} FROM download_requests WHERE id IN ("
        f"SELECT request_id FROM download_request_keys "
        f"WHERE request_key IN ({placeholders}))",
        keys,
    ).fetchall()


def _register_request_keys(
    conn: sqlite3.Connection,
    request_id: int,
    keys: tuple[str, ...],
    timestamp: str,
    *,
    replace: bool = False,
) -> None:
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    conn.executemany(
        f"{verb} INTO download_request_keys(request_key,request_id,created_at) "
        "VALUES(?,?,?)",
        ((key, int(request_id), timestamp) for key in keys),
    )


def _bind_media_download_admission_conn(
    conn: sqlite3.Connection,
    admission_id: int | None,
    request_id: int,
    timestamp: str,
) -> None:
    """在调用方事务内建立 admission -> request 的不可缺失关联。"""
    if admission_id is None:
        return
    cur = conn.execute(
        "UPDATE media_download_admissions SET request_id=?,updated_at=? "
        "WHERE id=? AND status='dispatching' AND request_id IS NULL",
        (int(request_id), timestamp, int(admission_id)),
    )
    if cur.rowcount == 1:
        return
    current = conn.execute(
        "SELECT request_id,status FROM media_download_admissions WHERE id=?",
        (int(admission_id),),
    ).fetchone()
    if (
        current is not None
        and int(current["request_id"] or 0) == int(request_id)
        and str(current["status"] or "") in {
            "dispatching", "submitted", "downloading", "processing"
        }
    ):
        return
    raise DownloadAdmissionBindingError("下载准入与请求绑定失败")


def bind_media_download_admission_request(admission_id: int, request_id: int) -> bool:
    """在复用既有请求时，于任何后端副作用前持久化准入关联。"""
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _bind_media_download_admission_conn(conn, admission_id, request_id, timestamp)
    return True


def create_download_request(request_key: str, kind: str, title: str = "",
                            source_value: str = "", torrent_data: bytes | None = None,
                            chat_id: str = "", user_id: str = "", message_id: str = "",
                            origin: str = "telegram", *,
                            supersede_request_id: int | None = None,
                            alternate_request_keys: Iterable[str] | None = None,
                            admission_id: int | None = None) -> tuple[int, bool]:
    """原子创建下载请求，并把等价历史 key 纳入同一防重边界。

    运行中的同源请求继续幂等返回；用户再次显式提交已经完成、失败或取消的普通下载时，
    保留旧请求作为历史尝试，并创建新的 canonical 请求。``manual_review`` 仅允许
    待处理页显式传入 ``supersede_request_id`` 时创建 successor。
    """
    retryable_kinds = {"magnet", "torrent", "ed2k", "http"}
    terminal_statuses = {"completed", "failed", "cancelled"}
    keys = _normalized_request_keys(request_key, alternate_request_keys)
    primary_key = keys[0]
    timestamp = now()
    with get_conn() as conn:
        def finish(request_id: int, created: bool) -> tuple[int, bool]:
            _bind_media_download_admission_conn(
                conn, admission_id, int(request_id), timestamp
            )
            return int(request_id), created

        # 串行化“检查所有等价 key → 归档历史 → 新建 canonical 请求”。
        conn.execute("BEGIN IMMEDIATE")
        rows = _request_rows_for_keys(
            conn,
            keys,
            columns="id,status,kind,request_key",
        )
        if not rows:
            created = conn.execute(
                "INSERT INTO download_requests(request_key,origin,chat_id,user_id,message_id,kind,title,"
                "source_value,torrent_data,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (primary_key, origin, chat_id, user_id, message_id, kind, title, source_value,
                 torrent_data, "pending", timestamp, timestamp),
            )
            request_id = int(created.lastrowid)
            _register_request_keys(conn, request_id, keys, timestamp)
            return finish(request_id, True)

        explicit_row = None
        if supersede_request_id is not None:
            explicit_id = int(supersede_request_id)
            explicit_row = next((row for row in rows if int(row["id"]) == explicit_id), None)

        active_rows = [
            row for row in rows if str(row["status"] or "") not in terminal_statuses
        ]
        explicit_manual_successor = bool(
            explicit_row is not None
            and str(explicit_row["status"] or "") == "manual_review"
        )
        active_blockers = [
            row for row in active_rows
            if not (explicit_manual_successor and int(row["id"]) == int(explicit_row["id"]))
        ]
        if active_blockers:
            existing = _preferred_request_row(active_blockers, primary_key)
            _register_request_keys(
                conn, int(existing["id"]), keys, timestamp, replace=True
            )
            return finish(int(existing["id"]), False)
        if supersede_request_id is not None and not explicit_manual_successor:
            existing = _preferred_request_row(rows, primary_key)
            _register_request_keys(
                conn, int(existing["id"]), keys, timestamp, replace=True
            )
            return finish(int(existing["id"]), False)

        rows_to_archive = [
            row for row in rows
            if str(row["status"] or "") in terminal_statuses
            or (explicit_manual_successor and int(row["id"]) == int(explicit_row["id"]))
        ]
        if not rows_to_archive or any(
            str(row["kind"] or kind) not in retryable_kinds for row in rows_to_archive
        ):
            existing = _preferred_request_row(rows, primary_key)
            _register_request_keys(
                conn, int(existing["id"]), keys, timestamp, replace=True
            )
            return finish(int(existing["id"]), False)

        for index, row in enumerate(rows_to_archive):
            existing_id = int(row["id"])
            existing_key = str(row["request_key"] or "")
            archived_key = (
                f"{existing_key}:history:{existing_id}:"
                f"{datetime.now().timestamp():.6f}:{index}"
            )
            conn.execute(
                "DELETE FROM download_request_keys WHERE request_id=?",
                (existing_id,),
            )
            archived = conn.execute(
                "UPDATE download_requests SET request_key=?,updated_at=? "
                "WHERE id=? AND request_key=?",
                (archived_key, timestamp, existing_id, existing_key),
            )
            if archived.rowcount != 1:
                current = _request_rows_for_keys(
                    conn,
                    keys,
                    columns="id,status,kind,request_key",
                )
                if current:
                    existing = _preferred_request_row(current, primary_key)
                    return finish(int(existing["id"]), False)
                raise RuntimeError("下载请求重试认领失败")

        created = conn.execute(
            "INSERT INTO download_requests(request_key,origin,chat_id,user_id,message_id,kind,title,"
            "source_value,torrent_data,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (primary_key, origin, chat_id, user_id, message_id, kind, title, source_value,
             torrent_data, "pending", timestamp, timestamp),
        )
        request_id = int(created.lastrowid)
        _register_request_keys(conn, request_id, keys, timestamp)
        return finish(request_id, True)


def bind_pending_download_request_owner(
    request_id: int,
    *,
    chat_id: str,
    user_id: str,
) -> sqlite3.Row | None:
    """验证 Telegram pending 请求的规范会话所有者。"""
    safe_chat = str(chat_id or "").strip()
    safe_user = str(user_id or "").strip()
    if not safe_chat or not safe_user:
        return None
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM download_requests "
            "WHERE id=? AND status='pending' AND chat_id=? AND user_id=?",
            (int(request_id), safe_chat, safe_user),
        ).fetchone()


def create_share_transfer_request(
    request_key: str,
    *,
    title: str,
    chat_id: str = "",
    origin: str = "telegram",
) -> tuple[int, bool]:
    """创建分享转存请求；敏感分享 URL、token 与文件 ID 不进入数据库。"""
    return create_download_request(
        request_key,
        "guangya_share",
        title=title,
        source_value="",
        torrent_data=None,
        chat_id=chat_id,
        message_id="",
        origin=origin,
    )


def claim_failed_share_transfer_request(request_id: int) -> bool:
    """显式重试一次明确失败的分享转存；不确定结果禁止重新云写。"""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE download_requests SET status='submitting',gy_status='',error='',"
            "completed_at=NULL,updated_at=? WHERE id=? AND kind='guangya_share' "
            "AND status='failed' AND (SELECT COUNT(*) FROM download_log "
            "WHERE request_id=download_requests.id AND source='guangya_share')=1",
            (now(), int(request_id)),
        )
        return cur.rowcount == 1


def finish_share_transfer_request(
    request_id: int,
    *,
    success: bool,
    target_dir_id: str,
    target_dir_name: str,
    title: str,
    count: int = 0,
    error: str = "",
    failure_status: str = "failed",
    isolated: bool = False,
    staging_parent_dir: str = "",
    staging_name: str = "",
    staging_cleanup_status: str = "",
    staging_cleanup_error: str = "",
) -> None:
    """原子落盘分享转存结果，并接入既有 tracker 所读取的请求状态。"""
    timestamp = now()
    normalized_failure = (
        failure_status if failure_status in {"failed", "manual_review"} else "failed"
    )
    status = "completed" if success else normalized_failure
    gy_status = "completed" if success else normalized_failure
    log_status = "success" if success else "failed"
    safe_error = str(error or "")
    with get_conn() as conn:
        conn.execute(
            "UPDATE download_requests SET targets='guangya',status=?,gy_status=?,"
            "gy_target_dir=?,gy_target_name=?,gy_isolated=?,gy_staging_parent_dir=?,"
            "gy_staging_name=?,gy_staging_cleanup_status=?,gy_staging_cleanup_error=?,"
            "error=?,completed_at=?,updated_at=? WHERE id=?",
            (
                status, gy_status, str(target_dir_id or "0"),
                str(target_dir_name or "根目录"), 1 if isolated else 0,
                str(staging_parent_dir or ""), str(staging_name or ""),
                str(staging_cleanup_status or ""), str(staging_cleanup_error or ""),
                safe_error, timestamp, timestamp, request_id,
            ),
        )
        conn.execute(
            "INSERT INTO download_log(source,title,path,status,request_id,progress,error,"
            "created_at,updated_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "guangya_share", str(title or f"分享转存 {max(0, int(count))} 项"),
                str(target_dir_name or "根目录"), log_status, request_id,
                1.0 if success else 0.0, safe_error, timestamp, timestamp, timestamp,
            ),
        )


def get_download_request(request_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM download_requests WHERE id=?", (request_id,)
        ).fetchone()


def count_download_requests_requiring_attention() -> int:
    """返回需要用户核验的下载及后处理异常请求数。"""
    with get_conn() as conn:
        return int(conn.execute(
            f"SELECT COUNT(*) FROM download_requests WHERE {_DOWNLOAD_ATTENTION_WHERE}"
        ).fetchone()[0])


def list_download_requests_requiring_attention(
    *, limit: int = 50, offset: int = 0,
) -> list[sqlite3.Row]:
    """列出与看板计数口径完全一致的待处理请求。"""
    safe_limit = max(1, min(int(limit or 50), 100))
    safe_offset = max(0, int(offset or 0))
    with get_conn() as conn:
        return conn.execute(
            f"SELECT * FROM download_requests WHERE {_DOWNLOAD_ATTENTION_WHERE} "
            "ORDER BY COALESCE(updated_at,created_at) DESC,id DESC LIMIT ? OFFSET ?",
            (safe_limit, safe_offset),
        ).fetchall()


def clear_download_request_attention(request_id: int) -> str:
    """确认并隐藏一条待处理告警，同时保留原始状态、错误与下载日志。"""
    timestamp = now()
    note = "用户已将本记录移出待处理；原状态、错误与下载日志均保留"
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE download_requests SET attention_cleared_at=?,attention_clear_note=?,updated_at=? "
            f"WHERE id=? AND COALESCE(attention_cleared_at,'')='' AND ({_DOWNLOAD_ATTENTION_BASE_WHERE})",
            (timestamp, note, timestamp, int(request_id)),
        )
        if cur.rowcount == 1:
            return "cleared"
        row = conn.execute(
            "SELECT attention_cleared_at FROM download_requests WHERE id=?",
            (int(request_id),),
        ).fetchone()
        if not row:
            return "not_found"
        if str(row["attention_cleared_at"] or ""):
            return "already_cleared"
        return "not_attention"


def clear_download_request_attentions(request_ids: list[int]) -> dict[str, list[int]]:
    """原子确认多条待处理告警，保留原请求、错误、日志、任务与文件。"""
    normalized = list(dict.fromkeys(
        int(value) for value in request_ids if int(value) > 0
    ))
    result = {
        "cleared": [],
        "already_cleared": [],
        "not_attention": [],
        "not_found": [],
    }
    if not normalized:
        return result
    timestamp = now()
    note = "用户已批量将本记录移出待处理；原状态、错误与下载日志均保留"
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for request_id in normalized:
            cur = conn.execute(
                "UPDATE download_requests SET attention_cleared_at=?,attention_clear_note=?,updated_at=? "
                f"WHERE id=? AND COALESCE(attention_cleared_at,'')='' AND ({_DOWNLOAD_ATTENTION_BASE_WHERE})",
                (timestamp, note, timestamp, request_id),
            )
            if cur.rowcount == 1:
                result["cleared"].append(request_id)
                continue
            row = conn.execute(
                "SELECT attention_cleared_at FROM download_requests WHERE id=?",
                (request_id,),
            ).fetchone()
            if not row:
                result["not_found"].append(request_id)
            elif str(row["attention_cleared_at"] or ""):
                result["already_cleared"].append(request_id)
            else:
                result["not_attention"].append(request_id)
    return result


def mark_download_request_resubmitted(
    request_id: int,
    *,
    successor_request_id: int,
    targets: str,
) -> bool:
    """把旧异常请求标记为已由新的下载请求接管。

    保留旧请求及原错误用于审计，但从待处理口径中移除，避免重新提交后
    旧异常与新请求同时占用两个待处理条目。
    """
    timestamp = now()
    note = f"已重新提交为请求 #{int(successor_request_id)}（目标：{str(targets or '')}）"
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE download_requests SET "
            "status=CASE WHEN status IN ('failed','manual_review') THEN 'resubmitted' ELSE status END,"
            "qb_status=CASE WHEN qb_status IN ('failed','manual_review') THEN 'resubmitted' ELSE qb_status END,"
            "gy_status=CASE WHEN gy_status IN ('failed','manual_review') THEN 'resubmitted' ELSE gy_status END,"
            "local_import_status=CASE WHEN local_import_status='failed' THEN 'resubmitted' ELSE local_import_status END,"
            "organize_started=CASE "
            "WHEN gy_status='completed' AND organize_started<=0 THEN 1 "
            "WHEN organize_started<0 THEN 0 ELSE organize_started END,"
            "organize_status=CASE WHEN organize_status='failed' THEN 'resubmitted' ELSE organize_status END,"
            "strm_status=CASE WHEN strm_status='failed' THEN 'resubmitted' ELSE strm_status END,"
            "error=CASE WHEN COALESCE(error,'')='' THEN ? ELSE substr(error || char(10) || ?,1,1000) END,"
            "updated_at=? WHERE id=?",
            (note, note, timestamp, int(request_id)),
        )
        return cur.rowcount == 1


def get_download_request_status_snapshot(
    request_id: int,
) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
    """在同一只读事务中读取 Agent 状态投影需要的固定白名单列。"""
    with get_conn() as conn:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT targets,status,qb_status,gy_status,organize_started,"
            "local_import_status,created_at,updated_at,completed_at "
            "FROM download_requests WHERE id=?",
            (int(request_id),),
        ).fetchone()
        logs = conn.execute(
            "SELECT source,status,progress,created_at,updated_at,completed_at "
            "FROM download_log WHERE request_id=? AND source IN ('qb','guangya') "
            "ORDER BY id DESC LIMIT 8",
            (int(request_id),),
        ).fetchall()
        return row, logs


def get_download_request_by_request_key(request_key: str):
    """按 canonical key 读取当前活动/最新下载请求。"""
    key = str(request_key or "").strip()
    if not key:
        return None
    with get_conn() as conn:
        rows = _request_rows_for_keys(conn, (key,))
    return rows[0] if rows else None


def get_download_request_by_request_keys(request_keys: Iterable[str]):
    """按同一内容的规范协议身份查找活动请求。"""
    keys = _normalized_request_keys("", request_keys)
    with get_conn() as conn:
        rows = _request_rows_for_keys(conn, keys)
    if not rows:
        return None
    terminal_statuses = {"completed", "failed", "cancelled"}
    active = [row for row in rows if str(row["status"] or "") not in terminal_statuses]
    return _preferred_request_row(active or rows, keys[0])


def claim_download_request_targets(request_id: int, targets: str) -> tuple[str, ...]:
    """原子认领已有请求中尚未提交（或可安全重试）的后端目标。"""
    desired = {"qb", "guangya"} if targets == "both" else {targets}
    if not desired or desired - {"qb", "guangya"}:
        return ()
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id,status,targets,qb_status,gy_status FROM download_requests WHERE id=?",
            (int(request_id),),
        ).fetchone()
        if not row or str(row["status"] or "") in {"pending", "completed", "failed"}:
            return ()
        qb_status = str(row["qb_status"] or "")
        gy_status = str(row["gy_status"] or "")
        active_existing = any(
            status not in {"", "failed"} for status in (qb_status, gy_status)
        )
        if not active_existing:
            return ()
        missing: list[str] = []
        if "qb" in desired and qb_status in {"", "failed"}:
            missing.append("qb")
        if "guangya" in desired and gy_status in {"", "failed"}:
            missing.append("guangya")
        if not missing:
            return ()

        existing_targets = {
            value for value in str(row["targets"] or "").split(",") if value in {"qb", "guangya"}
        }
        if str(row["targets"] or "") == "both":
            existing_targets = {"qb", "guangya"}
        existing_targets.update(missing)
        merged_targets = "both" if existing_targets == {"qb", "guangya"} else next(iter(existing_targets))
        sets = [
            "targets=?", "status='submitting'", "completed_at=NULL", "error=''",
            "attention_cleared_at=NULL", "attention_clear_note=''", "updated_at=?",
        ]
        values: list[object] = [merged_targets, timestamp]
        if "qb" in missing:
            sets.extend([
                "qb_status='submitting'", "qb_task_id=''", "qb_task_missing_since=NULL",
                "qb_content_path=''",
                "local_import_status=''", "local_import_attempts=0", "local_import_error=''",
                "local_import_target=''", "local_import_started_at=NULL", "local_import_completed_at=NULL",
            ])
        if "guangya" in missing:
            sets.extend([
                "gy_status='submitting'", "gy_task_id=''", "gy_task_ids='[]'", "gy_batch_count=0",
                "gy_task_missing_since=NULL", "gy_isolated=0", "gy_staging_parent_dir=''", "gy_staging_name=''",
                "gy_staging_cleanup_status=''", "gy_staging_cleanup_error=''",
                "gy_expected_file_count=0", "gy_settle_observed_file_count=0",
                "gy_settle_attempts=0", "gy_settle_snapshot=''", "gy_settle_stable_count=0",
                "gy_selection_mode=''", "gy_unverified_manifest=0",
                "organize_started=0", "organize_attempts=0", "organize_next_retry_at=NULL",
                "organize_task_id=''", "organize_run_id=NULL", "organize_status=''",
                "organize_error=''", "organize_finished_at=NULL", "strm_run_id=NULL",
                "strm_status=''", "strm_error=''", "strm_finished_at=NULL",
            ])
        conn.execute(
            f"UPDATE download_requests SET {','.join(sets)} WHERE id=?",
            (*values, int(request_id)),
        )
        return tuple(missing)


def claim_download_request(request_id: int, targets: str) -> bool:
    """原子认领待选择请求，防 callback 重放和并发重复提交。"""
    qb_status = "submitting" if targets in {"qb", "both"} else ""
    gy_status = "submitting" if targets in {"guangya", "both"} else ""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE download_requests SET targets=?,status='submitting',"
            "qb_status=?,gy_status=?,qb_task_id='',gy_task_id='',gy_task_ids='[]',gy_batch_count=0,"
            "gy_expected_file_count=0,gy_settle_observed_file_count=0,gy_settle_attempts=0,"
            "gy_settle_snapshot='',gy_settle_stable_count=0,gy_selection_mode='',gy_unverified_manifest=0,"
            "organize_started=0,organize_attempts=0,organize_next_retry_at=NULL,"
            "organize_task_id='',organize_run_id=NULL,organize_status='',organize_error='',organize_finished_at=NULL,"
            "strm_run_id=NULL,strm_status='',strm_error='',strm_finished_at=NULL,"
            "completed_at=NULL,error='',attention_cleared_at=NULL,attention_clear_note='',updated_at=? "
            "WHERE id=? AND status='pending'",
            (targets, qb_status, gy_status, now(), request_id),
        )
        return cur.rowcount > 0


def claim_download_request_organize(request_id: int) -> bool:
    """原子认领光鸭下载后的整理阶段，阻止旧记录或并发跟踪重复启动。"""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE download_requests SET organize_started=1,organize_status='starting',"
            "organize_error='',updated_at=? WHERE id=? "
            "AND targets IN ('guangya','both') AND gy_status='completed' "
            "AND status IN ('submitted','downloading','completed') AND organize_started=0 "
            "AND COALESCE(organize_status,'') NOT IN ('resubmitted','cleared') "
            "AND COALESCE(attention_cleared_at,'')=''",
            (now(), int(request_id)),
        )
        return cur.rowcount == 1


_DOWNLOAD_REQUEST_UPDATE_FIELDS = {
    "targets", "status", "qb_task_id", "gy_task_id", "gy_task_ids", "gy_batch_count",
    "gy_isolated", "gy_staging_parent_dir", "gy_staging_name",
    "gy_staging_cleanup_status", "gy_staging_cleanup_error",
    "gy_expected_file_count", "gy_settle_observed_file_count", "gy_settle_attempts",
    "gy_settle_snapshot", "gy_settle_stable_count", "gy_selection_mode",
    "gy_unverified_manifest", "qb_status", "gy_status",
    "qb_task_missing_since", "gy_task_missing_since",
    "gy_target_dir", "gy_target_name", "organize_started", "organize_attempts",
    "organize_next_retry_at", "organize_task_id",
    "organize_run_id", "organize_status", "organize_error", "organize_finished_at",
    "strm_run_id", "strm_status", "strm_error", "strm_finished_at",
    "qb_content_path", "local_import_status", "local_import_attempts",
    "local_import_error", "local_import_target", "local_import_started_at",
    "local_import_completed_at", "error", "completed_at",
    "title", "source_value", "torrent_data",
}


def _update_download_request_conn(
    conn: sqlite3.Connection,
    request_id: int,
    fields: dict,
    timestamp: str,
) -> bool:
    sets, values = [], []
    for key, value in fields.items():
        if key in _DOWNLOAD_REQUEST_UPDATE_FIELDS:
            sets.append(f"{key}=?")
            values.append(value)
    if not sets:
        return False
    sets.append("updated_at=?")
    values.extend([timestamp, int(request_id)])
    conn.execute(f"UPDATE download_requests SET {', '.join(sets)} WHERE id=?", values)
    return True


def update_download_request(request_id: int, **fields) -> None:
    with get_conn() as conn:
        _update_download_request_conn(conn, request_id, fields, now())


def update_download_request_and_sync_media_admission(request_id: int, **fields) -> int:
    """同事务更新下载请求，并将根状态投影到已绑定的媒体订阅准入。"""
    timestamp = now()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _update_download_request_conn(conn, request_id, fields, timestamp):
            return 0
        from app.repositories.media_subscriptions import (  # 局部导入避免仓储循环加载
            _sync_media_download_admission_for_request_conn,
        )

        return _sync_media_download_admission_for_request_conn(
            conn, int(request_id), timestamp
        )


def link_download_request_to_local_media_task(
    request_id: int, task_id: int, content_path: str
) -> bool:
    """仅把尚未进入终态的下载请求关联到本地媒体任务。"""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE download_requests SET local_import_status='pending',local_import_target=?,"
            "qb_content_path=?,local_import_error='',local_import_completed_at=NULL,updated_at=? "
            "WHERE id=? AND COALESCE(local_import_status,'') IN ('','pending')",
            (f"local-media-task:{int(task_id)}", str(content_path or ""), now(), int(request_id)),
        )
        return cur.rowcount == 1


def mark_download_request_local_media_skipped(request_id: int, content_path: str, error: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE download_requests SET local_import_status='skipped',qb_content_path=?,"
            "local_import_error=?,local_import_completed_at=?,updated_at=? "
            "WHERE id=? AND COALESCE(local_import_status,'') IN ('','pending')",
            (str(content_path or ""), str(error or "")[:1000], now(), now(), int(request_id)),
        )
        return cur.rowcount == 1


def mark_download_request_local_media_failed(
    request_id: int, content_path: str, error: str
) -> bool:
    """仅将未进入终态的本地入库请求标记为配置失败。"""
    timestamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE download_requests SET local_import_status='failed',qb_content_path=?,"
            "local_import_error=?,local_import_started_at="
            "COALESCE(NULLIF(local_import_started_at,''),?),"
            "local_import_completed_at=?,updated_at=? "
            "WHERE id=? AND COALESCE(local_import_status,'') IN ('','pending')",
            (
                str(content_path or ""), str(error or "")[:1000], timestamp,
                timestamp, timestamp, int(request_id),
            ),
        )
        return cur.rowcount == 1


def update_download_request_for_local_media_task(
    task_id: int, status: str, *, error: str = ""
) -> int:
    """把新本地媒体任务终态回写到原下载请求，避免完成任务被重复轮询。"""
    safe_status = str(status or "").strip()
    if safe_status not in {"completed", "requires_manual", "planned", "failed"}:
        raise ValueError("本地媒体回写状态无效")
    timestamp = now()
    completed_at = timestamp if safe_status in {"completed", "requires_manual", "failed"} else None
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE download_requests SET local_import_status=?,local_import_error=?,"
            "local_import_completed_at=?,updated_at=? WHERE local_import_target=? "
            "AND COALESCE(local_import_status,'') IN ('','pending')",
            (safe_status, str(error or ""), completed_at, timestamp, f"local-media-task:{int(task_id)}"),
        )
        return int(cur.rowcount)


def list_active_download_requests(
    limit: int = 100,
    *,
    include_local_import: bool = False,
    after_id: int = 0,
    wrap: bool = False,
) -> list[sqlite3.Row]:
    clauses = [
        "status IN ('submitting','submitted','downloading')",
        "(status!='cancelled' AND ("
        "qb_status IN ('submitted','downloading','outcome_unknown') OR "
        "gy_status IN ('submitted','downloading','outcome_unknown'))) ",
        "(status='completed' AND gy_status='completed' AND organize_started=0 "
        "AND (organize_next_retry_at IS NULL OR organize_next_retry_at='' OR organize_next_retry_at<=datetime('now','localtime')))",
    ]
    if include_local_import:
        clauses.append(
            "(qb_status='completed' AND COALESCE(local_import_status,'') IN ('','pending'))"
        )
    normalized_limit = max(1, int(limit))
    normalized_after = max(0, int(after_id or 0))
    predicate = f"({' OR '.join(clauses)})"
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM download_requests WHERE {predicate} AND id>? "
            "ORDER BY id ASC LIMIT ?",
            (normalized_after, normalized_limit),
        ).fetchall()
        if wrap and normalized_after and len(rows) < normalized_limit:
            rows.extend(conn.execute(
                f"SELECT * FROM download_requests WHERE {predicate} AND id<=? "
                "ORDER BY id ASC LIMIT ?",
                (normalized_after, normalized_limit - len(rows)),
            ).fetchall())
        return rows
