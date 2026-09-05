"""只读活动关联：只认持久化请求/运行标识，不凭标题拼接跨域时间线。"""

from __future__ import annotations

from app import database as db

_TABLES = {
    "download": "download_requests",
    "organize": "organize_log",
    "local_media": "local_media_tasks",
}


def search(*, query: str, limit: int = 10) -> list[dict]:
    rows: list[dict] = []
    bounded = max(1, min(int(limit), 20))
    with db.get_conn() as conn:
        for kind, table in _TABLES.items():
            # instr 是文字包含查询，用户的 %/_ 不具有 LIKE 通配语义。
            matches = conn.execute(
                f"SELECT id,title,status,created_at,updated_at FROM {table} "
                "WHERE (?='' OR instr(lower(title),lower(?))>0) "
                "ORDER BY id DESC LIMIT ?",
                (query, query, bounded + 1),
            ).fetchall()
            rows.extend({"kind": kind, **dict(row)} for row in matches)
    rows.sort(
        key=lambda row: (row.get("updated_at") or row["created_at"], row["id"]),
        reverse=True,
    )
    return rows[: bounded + 1]


def snapshot(kind: str, identifier: int) -> dict | None:
    table = _TABLES.get(kind)
    if table is None:
        raise ValueError("活动类型无效")
    with db.get_conn() as conn:
        conn.execute("BEGIN")
        row = conn.execute(
            f"SELECT * FROM {table} WHERE id=?", (identifier,)
        ).fetchone()
        if row is None:
            return None
        result = {"kind": kind, "record": dict(row)}
        if kind == "download":
            result["logs"] = [
                dict(item)
                for item in conn.execute(
                    "SELECT source,status,progress,error,created_at,updated_at,completed_at "
                    "FROM download_log WHERE request_id=? ORDER BY id DESC LIMIT 40",
                    (identifier,),
                )
            ]
            result["runs"] = [
                dict(item)
                for item in conn.execute(
                    "SELECT id,task_name,status,started_at,finished_at,error FROM task_runs "
                    "WHERE id IN (?,?) ORDER BY id",
                    (row["organize_run_id"], row["strm_run_id"]),
                )
            ]
            verification = conn.execute(
                "SELECT status,result,attempts,updated_at FROM agent_download_verifications WHERE request_id=?",
                (identifier,),
            ).fetchone()
            result["verification"] = dict(verification) if verification else None
            # 本地任务归属按 tracker 写入的 qB 身份确定，绝不使用媒体标题猜测。
            result["local_tasks"] = [
                dict(item)
                for item in conn.execute(
                    "SELECT id,title,status,error,updated_at,completed_at FROM local_media_tasks "
                    "WHERE qb_hash=? AND ?!='' ORDER BY id DESC LIMIT 20",
                    (row["qb_task_id"], row["qb_task_id"] or ""),
                )
            ]
        elif kind == "organize":
            result["steps"] = [
                dict(item)
                for item in conn.execute(
                    "SELECT * FROM organize_operation_steps WHERE log_id=? ORDER BY id DESC LIMIT 100",
                    (identifier,),
                )
            ]
        else:
            result["items"] = [
                dict(item)
                for item in conn.execute(
                    "SELECT role,status,error FROM local_media_task_items WHERE task_id=? ORDER BY id LIMIT 101",
                    (identifier,),
                )
            ]
    return result
