"""STRM 索引与变化目标队列的数据访问。"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType


def _database() -> "ModuleType":
    """延迟取得数据库门面，保持测试数据库与连接补丁兼容。"""
    from app import database

    return database


def upsert_strm_index(
    source: str,
    file_id: str,
    etag: str,
    size: int,
    filename: str,
    strm_path: str,
    content_fingerprint: str = "",
    *,
    conflicting_file_ids: list[str] | tuple[str, ...] = (),
) -> str:
    """写入 STRM 索引，并在同一事务内清理冲突文件索引。"""
    database = _database()
    conflicts = [
        item
        for item in dict.fromkeys(
            str(value) for value in conflicting_file_ids if str(value)
        )
        if item != str(file_id)
    ]
    with database.get_conn() as conn:
        previous = conn.execute(
            "SELECT strm_path FROM strm_index WHERE source=? AND file_id=?",
            (source, file_id),
        ).fetchone()
        conn.execute(
            "INSERT INTO strm_index(source,file_id,etag,size,filename,strm_path,"
            "content_fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source,file_id) DO UPDATE SET "
            "etag=excluded.etag,size=excluded.size,filename=excluded.filename,"
            "strm_path=excluded.strm_path,content_fingerprint=excluded.content_fingerprint,"
            "created_at=excluded.created_at",
            (
                source,
                file_id,
                etag,
                size,
                filename,
                strm_path,
                str(content_fingerprint or ""),
                database.now(),
            ),
        )
        if conflicts:
            placeholders = ",".join("?" for _ in conflicts)
            conn.execute(
                f"DELETE FROM strm_index WHERE source=? AND file_id IN ({placeholders})",
                [source, *conflicts],
            )
        return str(previous["strm_path"] or "") if previous else ""


def upsert_strm_index_batch(
    source: str,
    items: list[dict[str, Any]],
) -> None:
    """批量写入 STRM 索引并在同一事务内清理冲突文件索引。

    每个 item 包含:
    - file_id: str
    - etag: str
    - size: int
    - filename: str
    - strm_path: str
    - content_fingerprint: str (可选)
    - conflicting_file_ids: list[str] | tuple[str, ...] (可选)
    """
    if not items:
        return
    database = _database()
    now_str = database.now()
    records = []
    all_conflicts: list[str] = []
    for item in items:
        file_id = str(item["file_id"])
        records.append((
            source,
            file_id,
            str(item.get("etag") or ""),
            int(item.get("size") or 0),
            str(item.get("filename") or ""),
            str(item.get("strm_path") or ""),
            str(item.get("content_fingerprint") or ""),
            now_str,
        ))
        conflicts = [
            str(value) for value in (item.get("conflicting_file_ids") or ())
            if str(value) and str(value) != file_id
        ]
        if conflicts:
            all_conflicts.extend(conflicts)

    with database.get_conn() as conn:
        conn.executemany(
            "INSERT INTO strm_index(source,file_id,etag,size,filename,strm_path,"
            "content_fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source,file_id) DO UPDATE SET "
            "etag=excluded.etag,size=excluded.size,filename=excluded.filename,"
            "strm_path=excluded.strm_path,content_fingerprint=excluded.content_fingerprint,"
            "created_at=excluded.created_at",
            records,
        )
        if all_conflicts:
            unique_conflicts = list(dict.fromkeys(all_conflicts))
            chunk_size = 500
            for i in range(0, len(unique_conflicts), chunk_size):
                chunk = unique_conflicts[i:i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                conn.execute(
                    f"DELETE FROM strm_index WHERE source=? AND file_id IN ({placeholders})",
                    [source, *chunk],
                )


def list_strm_index(source: str = "guangya") -> list[sqlite3.Row]:
    with _database().get_conn() as conn:
        return conn.execute(
            "SELECT * FROM strm_index WHERE source=? ORDER BY id", (source,)
        ).fetchall()


def list_strm_indexes_by_file_id(file_id: str) -> list[sqlite3.Row]:
    """查询所有来源中引用同一远端文件的 STRM 索引。"""
    with _database().get_conn() as conn:
        return conn.execute(
            "SELECT * FROM strm_index WHERE file_id=? ORDER BY id", (str(file_id),)
        ).fetchall()


def list_strm_index_by_prefix(prefix: str) -> list[sqlite3.Row]:
    with _database().get_conn() as conn:
        return conn.execute(
            "SELECT * FROM strm_index WHERE source LIKE ? ORDER BY id",
            (f"{prefix}%",),
        ).fetchall()


def delete_strm_index_ids(source: str, file_ids: list[str]) -> int:
    ids = list(dict.fromkeys(str(item) for item in file_ids if str(item)))
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with _database().get_conn() as conn:
        cur = conn.execute(
            f"DELETE FROM strm_index WHERE source=? AND file_id IN ({placeholders})",
            [source, *ids],
        )
        return cur.rowcount


# ===== STRM 伴随元数据下载队列 =====

DEFAULT_STRM_METADATA_LEASE_SECONDS = 900
DEFAULT_STRM_METADATA_MAX_ATTEMPTS = 6
_METADATA_ACTIVE_STATUSES = ("queued", "running", "retry_wait")


def _metadata_snapshot(item: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(item.get("source_name") or ""),
        str(item.get("parent_id") or ""),
        str(item.get("filename") or ""),
        str(item.get("etag") or ""),
        str(max(0, int(item.get("size") or 0))),
        _normalize_rel_dir(item.get("rel_dir")),
        str(item.get("target_rel_path") or ""),
    )


def _metadata_row_snapshot(row) -> tuple[str, ...]:
    return (
        str(row["source_name"] or ""),
        str(row["parent_id"] or ""),
        str(row["filename"] or ""),
        str(row["etag"] or ""),
        str(max(0, int(row["size"] or 0))),
        _normalize_rel_dir(row["rel_dir"]),
        str(row["target_rel_path"] or ""),
    )


def _sanitize_metadata_error(value: object) -> str:
    from app.logger import redact_sensitive_text

    return redact_sensitive_text(str(value or ""))[:500]


def enqueue_strm_metadata_jobs(
    items: object,
    *,
    provider: str = "guangya",
    max_attempts: int = DEFAULT_STRM_METADATA_MAX_ATTEMPTS,
) -> dict[str, int]:
    """按远端 file_id 幂等登记最新元数据快照，不持久化短效下载直链。"""
    normalized = [dict(item) for item in (items or []) if isinstance(item, dict)]
    result = {"created": 0, "updated": 0, "dirty": 0, "deduped": 0}
    if not normalized:
        return result
    database = _database()
    stamp = database.now()
    safe_max_attempts = max(1, int(max_attempts or DEFAULT_STRM_METADATA_MAX_ATTEMPTS))
    with database.get_conn() as conn:
        for item in normalized:
            source_id = str(item.get("source_id") or "").strip()
            file_id = str(item.get("file_id") or "").strip()
            filename = str(item.get("filename") or "").strip()
            if not source_id or not file_id or not filename:
                continue
            snapshot = _metadata_snapshot(item)
            row = conn.execute(
                "SELECT * FROM strm_metadata_queue WHERE provider=? AND source_id=? AND file_id=?",
                (str(provider or "guangya"), source_id, file_id),
            ).fetchone()
            values = (
                snapshot[0], snapshot[1], snapshot[2], snapshot[3], int(snapshot[4]),
                snapshot[5], snapshot[6], safe_max_attempts, stamp,
            )
            if row is None:
                conn.execute(
                    "INSERT INTO strm_metadata_queue("
                    "provider,source_id,source_name,file_id,parent_id,filename,etag,size,"
                    "rel_dir,target_rel_path,status,max_attempts,next_attempt_at,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,?,?,?)",
                    (
                        str(provider or "guangya"), source_id, snapshot[0], file_id,
                        snapshot[1], snapshot[2], snapshot[3], int(snapshot[4]),
                        snapshot[5], snapshot[6], safe_max_attempts, stamp, stamp, stamp,
                    ),
                )
                result["created"] += 1
                continue

            changed = _metadata_row_snapshot(row) != snapshot
            status = str(row["status"] or "queued")
            if status == "running":
                conn.execute(
                    "UPDATE strm_metadata_queue SET source_name=?,parent_id=?,filename=?,etag=?,"
                    "size=?,rel_dir=?,target_rel_path=?,max_attempts=?,dirty=?,"
                    "revision=revision+?,updated_at=? WHERE id=?",
                    (*values[:8], 1 if changed else int(row["dirty"] or 0), 1 if changed else 0,
                     stamp, int(row["id"])),
                )
                result["dirty" if changed else "deduped"] += 1
                continue
            force = bool(item.get("force"))
            if not changed and status == "failed" and not force:
                result.setdefault("failed", 0)
                result["failed"] += 1
                continue
            if not changed and status in {"completed", "retry_wait", "queued"} and not (
                force and status == "completed"
            ):
                conn.execute(
                    "UPDATE strm_metadata_queue SET source_name=?,parent_id=?,filename=?,etag=?,"
                    "size=?,rel_dir=?,target_rel_path=?,max_attempts=?,updated_at=? WHERE id=?",
                    (*values[:8], stamp, int(row["id"])),
                )
                result["deduped"] += 1
                continue
            conn.execute(
                "UPDATE strm_metadata_queue SET source_name=?,parent_id=?,filename=?,etag=?,"
                "size=?,rel_dir=?,target_rel_path=?,status='queued',dirty=0,revision=revision+1,"
                "attempts=0,max_attempts=?,next_attempt_at=?,last_attempt_at=NULL,completed_at=NULL,"
                "lease_owner='',lease_until=0,last_error_type='',last_error='',updated_at=? WHERE id=?",
                (*values[:8], stamp, stamp, int(row["id"])),
            )
            result["updated"] += 1
    return result


def claim_due_strm_metadata_jobs(
    *,
    owner: str,
    provider: str = "guangya",
    lease_seconds: int = DEFAULT_STRM_METADATA_LEASE_SECONDS,
    limit: int = 1,
) -> list[dict[str, Any]]:
    """原子领取到期元数据任务；lease_generation 隔离迟到 worker。"""
    database = _database()
    stamp = database.now()
    now_epoch = time.time()
    deadline = now_epoch + max(1, int(lease_seconds or 1))
    claimed: list[dict[str, Any]] = []
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM strm_metadata_queue WHERE provider=? AND ("
            "(status IN ('queued','retry_wait') AND next_attempt_at<=?) OR "
            "(status='running' AND lease_until<=?)) "
            "ORDER BY next_attempt_at,id LIMIT ?",
            (str(provider or "guangya"), stamp, now_epoch, max(1, int(limit or 1))),
        ).fetchall()
        for row in rows:
            previous_generation = int(row["lease_generation"] or 0)
            cur = conn.execute(
                "UPDATE strm_metadata_queue SET status='running',dirty=0,lease_owner=?,"
                "lease_until=?,lease_generation=lease_generation+1,last_attempt_at=?,updated_at=? "
                "WHERE id=? AND lease_generation=? AND ("
                "(status IN ('queued','retry_wait') AND next_attempt_at<=?) OR "
                "(status='running' AND lease_until<=?))",
                (
                    str(owner or ""), deadline, stamp, stamp, int(row["id"]),
                    previous_generation, stamp, now_epoch,
                ),
            )
            if cur.rowcount != 1:
                continue
            payload = dict(row)
            payload["status"] = "running"
            payload["dirty"] = 0
            payload["lease_owner"] = str(owner or "")
            payload["lease_until"] = deadline
            payload["lease_generation"] = previous_generation + 1
            claimed.append(payload)
    return claimed


def renew_strm_metadata_job_lease(
    job_id: int,
    *,
    expected_owner: str,
    expected_lease_generation: int,
    lease_seconds: int = DEFAULT_STRM_METADATA_LEASE_SECONDS,
) -> bool:
    """续租当前元数据任务；owner 或代次变化时拒绝迟到 worker。"""
    owner = str(expected_owner or "").strip()
    if not owner:
        return False
    deadline = time.time() + max(1, int(lease_seconds or 1))
    with _database().get_conn() as conn:
        cur = conn.execute(
            "UPDATE strm_metadata_queue SET lease_until=?,updated_at=? "
            "WHERE id=? AND status='running' AND lease_owner=? AND lease_generation=?",
            (
                deadline, _database().now(), int(job_id), owner,
                int(expected_lease_generation),
            ),
        )
        return cur.rowcount == 1


def complete_strm_metadata_job(
    job_id: int,
    *,
    expected_lease_generation: int,
    expected_revision: int,
    expected_owner: str = "",
) -> str:
    """提交成功结果；运行中快照已变化时自动重新排队最新版本。"""
    database = _database()
    stamp = database.now()
    with database.get_conn() as conn:
        # 把快照判定和终态更新放在同一写事务内，避免扫描线程在两条 SQL
        # 之间写入 dirty/revision 后又被迟到的 completed 覆盖。
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status,dirty,revision,lease_owner,lease_generation "
            "FROM strm_metadata_queue WHERE id=?",
            (int(job_id),),
        ).fetchone()
        if (
            row is None or str(row["status"]) != "running"
            or int(row["lease_generation"] or 0) != int(expected_lease_generation)
            or (expected_owner and str(row["lease_owner"] or "") != str(expected_owner))
        ):
            return "stale"
        requeue = bool(row["dirty"]) or int(row["revision"] or 0) != int(expected_revision)
        status = "queued" if requeue else "completed"
        cur = conn.execute(
            "UPDATE strm_metadata_queue SET status=?,dirty=0,attempts=0,next_attempt_at=?,"
            "completed_at=?,lease_owner='',lease_until=0,last_error_type='',last_error='',"
            "updated_at=? WHERE id=? AND status='running' AND lease_generation=?",
            (
                status, stamp, None if requeue else stamp, stamp, int(job_id),
                int(expected_lease_generation),
            ),
        )
        return status if cur.rowcount == 1 else "stale"


def strm_metadata_job_is_current(
    job_id: int, *, expected_lease_generation: int, expected_revision: int,
    expected_owner: str = "",
) -> bool:
    """提交文件前复核任务仍由当前 worker 持有且快照未被更新/取消。"""
    with _database().get_conn() as conn:
        row = conn.execute(
            "SELECT status,dirty,revision,lease_owner,lease_generation "
            "FROM strm_metadata_queue WHERE id=?",
            (int(job_id),),
        ).fetchone()
    return bool(
        row is not None
        and str(row["status"] or "") == "running"
        and not bool(row["dirty"])
        and int(row["revision"] or 0) == int(expected_revision)
        and int(row["lease_generation"] or 0) == int(expected_lease_generation)
        and (not expected_owner or str(row["lease_owner"] or "") == str(expected_owner))
    )


def fail_or_retry_strm_metadata_job(
    job_id: int,
    *,
    expected_lease_generation: int,
    expected_revision: int,
    expected_owner: str = "",
    error_type: str,
    error: object,
    base_backoff_seconds: int = 30,
    max_backoff_seconds: int = 3600,
) -> str:
    """失败后指数退避；快照已变化时优先处理最新版本，不污染其尝试次数。"""
    database = _database()
    stamp = database.now()
    with database.get_conn() as conn:
        # 与 complete 使用相同的原子判定，失败回写也不能吞掉更新后的快照。
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status,dirty,revision,attempts,max_attempts,lease_owner,lease_generation "
            "FROM strm_metadata_queue WHERE id=?",
            (int(job_id),),
        ).fetchone()
        if (
            row is None or str(row["status"]) != "running"
            or int(row["lease_generation"] or 0) != int(expected_lease_generation)
            or (expected_owner and str(row["lease_owner"] or "") != str(expected_owner))
        ):
            return "stale"
        if bool(row["dirty"]) or int(row["revision"] or 0) != int(expected_revision):
            conn.execute(
                "UPDATE strm_metadata_queue SET status='queued',dirty=0,attempts=0,"
                "next_attempt_at=?,lease_owner='',lease_until=0,last_error_type='',last_error='',"
                "updated_at=? WHERE id=? AND status='running' AND lease_generation=?",
                (stamp, stamp, int(job_id), int(expected_lease_generation)),
            )
            return "queued"
        attempts = int(row["attempts"] or 0) + 1
        exhausted = attempts >= max(1, int(row["max_attempts"] or 1))
        status = "failed" if exhausted else "retry_wait"
        delay = 0 if exhausted else min(
            max(1, int(max_backoff_seconds or 1)),
            max(1, int(base_backoff_seconds or 1)) * (2 ** max(0, attempts - 1)),
        )
        next_attempt = stamp if exhausted else _future_stamp(delay)
        cur = conn.execute(
            "UPDATE strm_metadata_queue SET status=?,attempts=?,next_attempt_at=?,"
            "lease_owner='',lease_until=0,last_error_type=?,last_error=?,updated_at=? "
            "WHERE id=? AND status='running' AND lease_generation=?",
            (
                status, attempts, next_attempt, str(error_type or "Error")[:100],
                _sanitize_metadata_error(error), stamp, int(job_id),
                int(expected_lease_generation),
            ),
        )
        return status if cur.rowcount == 1 else "stale"


def recover_stale_strm_metadata_jobs(
    *, provider: str = "guangya", force: bool = False, owner: str = "",
) -> int:
    """恢复上次进程或过期租约遗留的 running 元数据任务。"""
    database = _database()
    stamp = database.now()
    where = "provider=? AND status='running'"
    params: list[object] = [str(provider or "guangya")]
    if force and owner:
        where += " AND lease_owner=?"
        params.append(str(owner))
    elif not force:
        where += " AND lease_until<=?"
        params.append(time.time())
    with database.get_conn() as conn:
        cur = conn.execute(
            "UPDATE strm_metadata_queue SET status='retry_wait',dirty=0,next_attempt_at=?,"
            "lease_owner='',lease_until=0,lease_generation=lease_generation+1,"
            "last_error_type='ProcessInterrupted',last_error='上次进程在元数据下载期间中断',"
            f"updated_at=? WHERE {where}",
            (stamp, stamp, *params),
        )
        return int(cur.rowcount or 0)


def cancel_strm_metadata_job(
    source_id: str, file_id: str, *, provider: str = "guangya", reason: str = "",
) -> bool:
    database = _database()
    stamp = database.now()
    with database.get_conn() as conn:
        cur = conn.execute(
            "UPDATE strm_metadata_queue SET status='cancelled',dirty=0,revision=revision+1,"
            "lease_owner='',lease_until=0,last_error_type='Cancelled',last_error=?,updated_at=? "
            "WHERE provider=? AND source_id=? AND file_id=? AND status<>'cancelled'",
            (
                _sanitize_metadata_error(reason or "远端元数据已删除"), stamp,
                str(provider or "guangya"), str(source_id), str(file_id),
            ),
        )
        return cur.rowcount == 1


def cancel_stale_strm_metadata_jobs(
    source_id: str, valid_file_ids: object, *, provider: str = "guangya",
    reason: str = "完整扫描确认远端元数据已失效",
) -> int:
    valid = {str(item) for item in (valid_file_ids or []) if str(item)}
    database = _database()
    stamp = database.now()
    cancelled = 0
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT id,file_id FROM strm_metadata_queue WHERE provider=? AND source_id=? "
            "AND status<>'cancelled'",
            (str(provider or "guangya"), str(source_id)),
        ).fetchall()
        for row in rows:
            if str(row["file_id"]) in valid:
                continue
            cur = conn.execute(
                "UPDATE strm_metadata_queue SET status='cancelled',dirty=0,revision=revision+1,"
                "lease_owner='',lease_until=0,last_error_type='Cancelled',last_error=?,"
                "updated_at=? WHERE id=? AND status<>'cancelled'",
                (_sanitize_metadata_error(reason), stamp, int(row["id"])),
            )
            cancelled += int(cur.rowcount or 0)
    return cancelled


def cancel_retired_strm_metadata_jobs(
    active_source_ids: object, *, provider: str = "guangya",
) -> int:
    """取消已不在配置来源集中的元数据任务，防止退役清理后被后台复活。"""
    active = {str(item) for item in (active_source_ids or []) if str(item)}
    database = _database()
    stamp = database.now()
    cancelled = 0
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT id,source_id FROM strm_metadata_queue WHERE provider=? "
            "AND status<>'cancelled'",
            (str(provider or "guangya"),),
        ).fetchall()
        for row in rows:
            if str(row["source_id"]) in active:
                continue
            cur = conn.execute(
                "UPDATE strm_metadata_queue SET status='cancelled',dirty=0,"
                "revision=revision+1,lease_owner='',lease_until=0,"
                "last_error_type='Cancelled',last_error='STRM 来源已从配置中移除',"
                "updated_at=? WHERE id=? AND status<>'cancelled'",
                (stamp, int(row["id"])),
            )
            cancelled += int(cur.rowcount or 0)
    return cancelled


def requeue_strm_metadata_jobs(job_ids: object) -> int:
    ids = [int(item) for item in (job_ids or []) if str(item).strip().isdigit()]
    if not ids:
        return 0
    database = _database()
    stamp = database.now()
    updated = 0
    with database.get_conn() as conn:
        for job_id in ids:
            cur = conn.execute(
                "UPDATE strm_metadata_queue SET status='queued',dirty=0,attempts=0,"
                "next_attempt_at=?,completed_at=NULL,lease_owner='',lease_until=0,"
                "last_error_type='',last_error='',updated_at=? WHERE id=? "
                "AND status IN ('failed','retry_wait','cancelled')",
                (stamp, stamp, job_id),
            )
            updated += int(cur.rowcount or 0)
    return updated


def count_strm_metadata_jobs(*, provider: str = "guangya") -> dict[str, int]:
    summary = {
        "queued": 0, "running": 0, "retry_wait": 0,
        "completed": 0, "failed": 0, "cancelled": 0,
    }
    with _database().get_conn() as conn:
        rows = conn.execute(
            "SELECT status,COUNT(*) AS total FROM strm_metadata_queue "
            "WHERE provider=? GROUP BY status",
            (str(provider or "guangya"),),
        ).fetchall()
    for row in rows:
        status = str(row["status"] or "")
        if status in summary:
            summary[status] = int(row["total"] or 0)
    summary["pending"] = summary["queued"] + summary["running"] + summary["retry_wait"]
    summary["total"] = sum(summary[key] for key in (
        "queued", "running", "retry_wait", "completed", "failed", "cancelled"
    ))
    return summary


def list_strm_metadata_queue(
    *, provider: str = "guangya", status: str = "all", limit: int = 200,
) -> list[sqlite3.Row]:
    safe_limit = max(1, min(int(limit or 200), 1000))
    with _database().get_conn() as conn:
        if status == "all":
            return conn.execute(
                "SELECT * FROM strm_metadata_queue WHERE provider=? "
                "ORDER BY updated_at DESC,id DESC LIMIT ?",
                (str(provider or "guangya"), safe_limit),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM strm_metadata_queue WHERE provider=? AND status=? "
            "ORDER BY updated_at DESC,id DESC LIMIT ?",
            (str(provider or "guangya"), str(status), safe_limit),
        ).fetchall()


# ===== STRM 变化目标队列 =====

DEFAULT_STRM_PROVIDER = "guangya"
# 租约用于跨进程恢复：worker 崩溃后租约到期即可被安全重新领取。
DEFAULT_STRM_LEASE_SECONDS = 900
_ACTIVE_STATES = ("queued", "running", "dirty")
_MAX_QUEUED_CHANGES = 5000


def _future_stamp(delay_seconds: float) -> str:
    try:
        delay = max(0.0, float(delay_seconds or 0.0))
    except (TypeError, ValueError, OverflowError):
        delay = 0.0
    return (
        datetime.now() + timedelta(seconds=delay)
    ).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_rel_dir(value: object) -> str:
    return str(value or "").strip().strip("/")


def _change_key(change: dict[str, Any]) -> str:
    """同一目标文件的重复变化幂等合并，避免队列无界增长。"""
    return "\x1f".join((
        str(change.get("source_id") or ""),
        str(change.get("kind") or "video"),
        str(change.get("action") or "upsert"),
        str(change.get("file_id") or ""),
        _normalize_rel_dir(change.get("rel_dir")),
        str(change.get("name") or ""),
    ))


def _load_changes(raw: object) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def merge_strm_changes(*groups: object) -> list[dict[str, Any]]:
    """按变化键去重合并，保持首次出现顺序。"""
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in group or []:
            if not isinstance(item, dict):
                continue
            merged.setdefault(_change_key(item), dict(item))
    return list(merged.values())[:_MAX_QUEUED_CHANGES]


def group_changes_by_target(changes: object) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """把 STRM 变化清单按 (source_id, 目标相对目录) 分组。"""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in changes or []:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("source_id") or "")
        if not source_id:
            continue
        key = (source_id, _normalize_rel_dir(item.get("rel_dir")))
        grouped.setdefault(key, []).append(dict(item))
    return grouped


def enqueue_strm_change_targets(
    changes: object, *, provider: str = DEFAULT_STRM_PROVIDER,
    not_before_seconds: float | None = 0.0,
) -> int:
    """登记变化目标目录；同步进行中的目标转入 dirty，不丢事件。

    ``not_before_seconds`` 用于把整理静默窗口持久化到队列：
    - 数值：从现在起重新设置最早领取时间；
    - ``None``：合并变化但保留已有最早领取时间。
    """
    grouped = group_changes_by_target(changes)
    if not grouped:
        return 0
    database = _database()
    stamp = database.now()
    if not_before_seconds is None:
        next_attempt_at = None
    else:
        try:
            delay = max(0.0, float(not_before_seconds or 0.0))
        except (TypeError, ValueError, OverflowError):
            delay = 0.0
        next_attempt_at = _future_stamp(delay)
    written = 0
    with database.get_conn() as conn:
        for (source_id, rel_dir), items in grouped.items():
            row = conn.execute(
                "SELECT id,state,pending_changes_json,next_attempt_at "
                "FROM strm_change_queue "
                "WHERE provider=? AND source_id=? AND rel_dir=?",
                (provider, source_id, rel_dir),
            ).fetchone()
            payload = json.dumps(
                merge_strm_changes(
                    _load_changes(row["pending_changes_json"]) if row else [], items,
                ),
                ensure_ascii=False,
            )
            if row is None:
                conn.execute(
                    "INSERT INTO strm_change_queue(provider,source_id,rel_dir,state,"
                    "pending_changes_json,created_at,updated_at,next_attempt_at) "
                    "VALUES(?,?,?,'queued',?,?,?,?)",
                    (
                        provider, source_id, rel_dir, payload, stamp, stamp,
                        next_attempt_at or stamp,
                    ),
                )
            elif str(row["state"]) in {"running", "dirty"}:
                if next_attempt_at is None:
                    conn.execute(
                        "UPDATE strm_change_queue SET state='dirty',dirty=1,"
                        "pending_changes_json=?,updated_at=? WHERE id=?",
                        (payload, stamp, int(row["id"])),
                    )
                else:
                    conn.execute(
                        "UPDATE strm_change_queue SET state='dirty',dirty=1,"
                        "pending_changes_json=?,updated_at=?,next_attempt_at=? WHERE id=?",
                        (payload, stamp, next_attempt_at, int(row["id"])),
                    )
            else:
                resolved_next_attempt = (
                    str(row["next_attempt_at"] or stamp)
                    if next_attempt_at is None else next_attempt_at
                )
                conn.execute(
                    "UPDATE strm_change_queue SET state='queued',dirty=0,attempts=0,"
                    "last_error='',pending_changes_json=?,updated_at=?,next_attempt_at=? "
                    "WHERE id=?",
                    (payload, stamp, resolved_next_attempt, int(row["id"])),
                )
            written += 1
    return written


def reschedule_strm_change_targets(
    changes: object, *, not_before_seconds: float,
    provider: str = DEFAULT_STRM_PROVIDER,
) -> int:
    """把同一内存合并批次的全部目标统一到新的最早领取时间。"""
    grouped = group_changes_by_target(changes)
    if not grouped:
        return 0
    database = _database()
    stamp = database.now()
    next_attempt_at = _future_stamp(not_before_seconds)
    updated = 0
    with database.get_conn() as conn:
        for source_id, rel_dir in grouped:
            cur = conn.execute(
                "UPDATE strm_change_queue SET next_attempt_at=?,updated_at=? "
                "WHERE provider=? AND source_id=? AND rel_dir=? "
                "AND state IN ('queued','dirty')",
                (next_attempt_at, stamp, provider, source_id, rel_dir),
            )
            updated += int(cur.rowcount or 0)
    return updated


def claim_strm_change_targets(
    *,
    owner: str,
    lease_seconds: int = DEFAULT_STRM_LEASE_SECONDS,
    limit: int = 200,
    provider: str = DEFAULT_STRM_PROVIDER,
) -> list[dict[str, Any]]:
    """原子领取到期目标；租约代次隔离过期 worker 的迟到结算。"""
    database = _database()
    stamp = database.now()
    now_epoch = time.time()
    deadline = now_epoch + max(1, int(lease_seconds or 1))
    safe_owner = str(owner or "").strip()
    if not safe_owner:
        raise ValueError("STRM 变化目标领取必须提供 owner")
    claimed: list[dict[str, Any]] = []
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM strm_change_queue WHERE provider=? AND ("
            "  (state='queued' AND next_attempt_at<=?)"
            "  OR (state IN ('running','dirty') AND lease_until<=?)"
            ") ORDER BY next_attempt_at, id LIMIT ?",
            (provider, stamp, now_epoch, max(1, int(limit or 1))),
        ).fetchall()
        for row in rows:
            previous_generation = int(row["lease_generation"] or 0)
            inflight = merge_strm_changes(
                _load_changes(row["inflight_changes_json"]),
                _load_changes(row["pending_changes_json"]),
            )
            cur = conn.execute(
                "UPDATE strm_change_queue SET state='running',dirty=0,"
                "pending_changes_json='[]',inflight_changes_json=?,lease_owner=?,"
                "lease_until=?,lease_generation=lease_generation+1,updated_at=? "
                "WHERE id=? AND lease_generation=? AND ("
                "(state='queued' AND next_attempt_at<=?) OR "
                "(state IN ('running','dirty') AND lease_until<=?))",
                (
                    json.dumps(inflight, ensure_ascii=False), safe_owner, deadline, stamp,
                    int(row["id"]), previous_generation, stamp, now_epoch,
                ),
            )
            if cur.rowcount != 1:
                continue
            claimed.append({
                "id": int(row["id"]),
                "provider": str(row["provider"]),
                "source_id": str(row["source_id"]),
                "rel_dir": str(row["rel_dir"]),
                "version": int(row["version"] or 0),
                "attempts": int(row["attempts"] or 0),
                "changes": inflight,
                "lease_owner": safe_owner,
                "lease_until": deadline,
                "lease_generation": previous_generation + 1,
            })
    return claimed


def renew_strm_change_target_leases(
    claimed: object,
    *,
    owner: str,
    lease_seconds: int = DEFAULT_STRM_LEASE_SECONDS,
) -> int:
    """续租仍由当前 owner/代次持有的目标，迟到 worker 无权延长新租约。"""
    safe_owner = str(owner or "").strip()
    rows = [dict(item) for item in (claimed or []) if isinstance(item, dict)]
    if not safe_owner or not rows:
        return 0
    deadline = time.time() + max(1, int(lease_seconds or 1))
    stamp = _database().now()
    renewed = 0
    with _database().get_conn() as conn:
        for item in rows:
            cur = conn.execute(
                "UPDATE strm_change_queue SET lease_until=?,updated_at=? "
                "WHERE id=? AND state IN ('running','dirty') AND lease_owner=? "
                "AND lease_generation=?",
                (
                    deadline, stamp, int(item.get("id") or 0), safe_owner,
                    int(item.get("lease_generation") or 0),
                ),
            )
            renewed += int(cur.rowcount or 0)
    return renewed


def complete_strm_change_target(
    target_id: int, *, expected_owner: str, expected_lease_generation: int,
) -> str:
    """完成一轮同步；只允许当前租约持有者结算，dirty 目标自动重排。"""
    database = _database()
    stamp = database.now()
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT state,pending_changes_json,next_attempt_at FROM strm_change_queue "
            "WHERE id=? AND state IN ('running','dirty') AND lease_owner=? "
            "AND lease_generation=?",
            (int(target_id), str(expected_owner or ""), int(expected_lease_generation)),
        ).fetchone()
        if row is None:
            return "stale"
        requeue = str(row["state"]) == "dirty" or bool(
            _load_changes(row["pending_changes_json"])
        )
        state = "queued" if requeue else "completed"
        next_attempt_at = (
            max(stamp, str(row["next_attempt_at"] or stamp)) if requeue else stamp
        )
        cur = conn.execute(
            "UPDATE strm_change_queue SET state=?,dirty=0,attempts=0,last_error='',"
            "version=version+1,inflight_changes_json='[]',lease_owner='',lease_until=0,"
            "updated_at=?,next_attempt_at=? WHERE id=? AND state IN ('running','dirty') "
            "AND lease_owner=? AND lease_generation=?",
            (
                state, stamp, next_attempt_at, int(target_id), str(expected_owner or ""),
                int(expected_lease_generation),
            ),
        )
        return state if cur.rowcount == 1 else "stale"


def fail_strm_change_target(
    target_id: int,
    *,
    expected_owner: str,
    expected_lease_generation: int,
    error: str = "",
    backoff_seconds: int = 60,
    max_attempts: int = 5,
) -> str:
    """当前租约失败后有界退避；迟到 worker 不得覆盖后来者状态。"""
    database = _database()
    stamp = database.now()
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT attempts,pending_changes_json,inflight_changes_json "
            "FROM strm_change_queue WHERE id=? AND state IN ('running','dirty') "
            "AND lease_owner=? AND lease_generation=?",
            (int(target_id), str(expected_owner or ""), int(expected_lease_generation)),
        ).fetchone()
        if row is None:
            return "stale"
        attempts = int(row["attempts"] or 0) + 1
        pending = merge_strm_changes(
            _load_changes(row["pending_changes_json"]),
            _load_changes(row["inflight_changes_json"]),
        )
        exhausted = attempts >= max(1, int(max_attempts or 1))
        state = "failed" if exhausted else "queued"
        delay = 0 if exhausted else max(1, int(backoff_seconds or 1)) * min(attempts, 6)
        cur = conn.execute(
            "UPDATE strm_change_queue SET state=?,dirty=0,attempts=?,last_error=?,"
            "pending_changes_json=?,inflight_changes_json='[]',lease_owner='',lease_until=0,"
            "updated_at=?,next_attempt_at=? WHERE id=? AND state IN ('running','dirty') "
            "AND lease_owner=? AND lease_generation=?",
            (
                state, attempts, str(error or "")[:300],
                json.dumps(pending, ensure_ascii=False), stamp, _future_stamp(delay),
                int(target_id), str(expected_owner or ""), int(expected_lease_generation),
            ),
        )
        return state if cur.rowcount == 1 else "stale"


def release_strm_change_targets(claimed: object, *, reason: str = "") -> int:
    """协作式停止：仅释放调用方仍持有的租约，不干扰重新领取者。"""
    rows = [dict(item) for item in (claimed or []) if isinstance(item, dict)]
    if not rows:
        return 0
    database = _database()
    stamp = database.now()
    released = 0
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for item in rows:
            target_id = int(item.get("id") or 0)
            owner = str(item.get("lease_owner") or "")
            generation = int(item.get("lease_generation") or 0)
            row = conn.execute(
                "SELECT pending_changes_json,inflight_changes_json "
                "FROM strm_change_queue WHERE id=? AND state IN ('running','dirty') "
                "AND lease_owner=? AND lease_generation=?",
                (target_id, owner, generation),
            ).fetchone()
            if row is None:
                continue
            pending = merge_strm_changes(
                _load_changes(row["pending_changes_json"]),
                _load_changes(row["inflight_changes_json"]),
            )
            cur = conn.execute(
                "UPDATE strm_change_queue SET state='queued',dirty=0,last_error=?,"
                "pending_changes_json=?,inflight_changes_json='[]',lease_owner='',"
                "lease_until=0,updated_at=?,next_attempt_at=? WHERE id=? "
                "AND state IN ('running','dirty') AND lease_owner=? AND lease_generation=?",
                (
                    str(reason or "")[:300], json.dumps(pending, ensure_ascii=False),
                    stamp, stamp, target_id, owner, generation,
                ),
            )
            released += int(cur.rowcount or 0)
    return released


def recover_stale_strm_change_targets(*, provider: str = DEFAULT_STRM_PROVIDER) -> int:
    """恢复租约过期目标，并推进代次使迟到 worker 的结算永久失效。"""
    database = _database()
    stamp = database.now()
    now_epoch = time.time()
    recovered = 0
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id,lease_generation,pending_changes_json,inflight_changes_json "
            "FROM strm_change_queue WHERE provider=? AND state IN ('running','dirty') "
            "AND lease_until<=?",
            (provider, now_epoch),
        ).fetchall()
        for row in rows:
            pending = merge_strm_changes(
                _load_changes(row["pending_changes_json"]),
                _load_changes(row["inflight_changes_json"]),
            )
            cur = conn.execute(
                "UPDATE strm_change_queue SET state='queued',dirty=0,"
                "pending_changes_json=?,inflight_changes_json='[]',lease_owner='',"
                "lease_until=0,lease_generation=lease_generation+1,updated_at=?,"
                "next_attempt_at=? WHERE id=? AND state IN ('running','dirty') "
                "AND lease_generation=? AND lease_until<=?",
                (
                    json.dumps(pending, ensure_ascii=False), stamp, stamp,
                    int(row["id"]), int(row["lease_generation"] or 0), now_epoch,
                ),
            )
            recovered += int(cur.rowcount or 0)
    return recovered

def count_pending_strm_change_targets(*, provider: str = DEFAULT_STRM_PROVIDER) -> int:
    placeholders = ",".join("?" for _ in _ACTIVE_STATES)
    with _database().get_conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS total FROM strm_change_queue "
            f"WHERE provider=? AND state IN ({placeholders})",
            (provider, *_ACTIVE_STATES),
        ).fetchone()
        return int(row["total"] or 0) if row else 0


def count_due_strm_change_targets(*, provider: str = DEFAULT_STRM_PROVIDER) -> int:
    """返回此刻可领取的 queued 目标数，不把租约中或退避中的行算入续跑。"""
    stamp = _database().now()
    with _database().get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM strm_change_queue "
            "WHERE provider=? AND state='queued' AND next_attempt_at<=?",
            (provider, stamp),
        ).fetchone()
        return int(row["total"] or 0) if row else 0


def seconds_until_next_strm_change_target(
    *, provider: str = DEFAULT_STRM_PROVIDER,
) -> float | None:
    """返回下一条变化目标距可领取/可恢复的秒数；无活动目标返回 ``None``。"""
    with _database().get_conn() as conn:
        queued_row = conn.execute(
            "SELECT MIN(next_attempt_at) AS next_attempt_at FROM strm_change_queue "
            "WHERE provider=? AND state='queued'",
            (provider,),
        ).fetchone()
        leased_row = conn.execute(
            "SELECT MIN(lease_until) AS lease_until FROM strm_change_queue "
            "WHERE provider=? AND state IN ('running','dirty')",
            (provider,),
        ).fetchone()
    delays: list[float] = []
    raw = (
        str(queued_row["next_attempt_at"] or "").strip()
        if queued_row else ""
    )
    if raw:
        try:
            due_at = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            delays.append(max(0.0, (due_at - datetime.now()).total_seconds()))
        except ValueError:
            delays.append(0.0)
    lease_until = float(leased_row["lease_until"] or 0.0) if leased_row else 0.0
    if lease_until > 0:
        delays.append(max(0.0, lease_until - time.time()))
    return min(delays) if delays else None


def list_strm_change_queue(
    *, provider: str = DEFAULT_STRM_PROVIDER, limit: int = 100,
) -> list[sqlite3.Row]:
    with _database().get_conn() as conn:
        return conn.execute(
            "SELECT * FROM strm_change_queue WHERE provider=? "
            "ORDER BY updated_at DESC, id DESC LIMIT ?",
            (provider, max(1, int(limit or 1))),
        ).fetchall()
