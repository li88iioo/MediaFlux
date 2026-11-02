"""识别领域中稳定、无业务决策的 SQLite 访问边界。"""
from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from app.modules.media_identity import confirmed_manual_position

if TYPE_CHECKING:
    from types import ModuleType


def _database() -> "ModuleType":
    """延迟取得数据库门面，避免导入环并保持测试数据库补丁兼容。"""
    from app import database

    return database


def get_tmdb_lock(
    *,
    raw_name: str,
    parent_path: str,
    media_type: str,
    season: int,
) -> dict | None:
    with _database().get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tmdb_lock WHERE raw_name=? AND parent_path=? "
            "AND media_type=? AND season=? AND key_version=1 AND lock_source='manual'",
            (raw_name, parent_path, media_type, season),
        ).fetchone()
    if row is None:
        return None
    return {
        "tmdb_id": row["tmdb_id"],
        "title": row["title"],
        "year": row["year"],
        "media_type": row["media_type"] or "",
        "parent_path": row["parent_path"] or "",
        "season": row["season"],
        "lock_source": row["lock_source"],
        "key_version": row["key_version"],
    }


def upsert_tmdb_lock(
    *,
    raw_name: str,
    parent_path: str,
    tmdb_id: str,
    title: str,
    year: str,
    media_type: str,
    season: int,
    lock_source: str,
) -> None:
    database = _database()
    with database.get_conn() as conn:
        conn.execute(
            "INSERT INTO tmdb_lock("
            "raw_name,parent_path,tmdb_id,title,year,media_type,season,key_version,"
            "lock_source,locked_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(raw_name,parent_path,media_type,season) DO UPDATE SET "
            "tmdb_id=excluded.tmdb_id,title=excluded.title,year=excluded.year,"
            "key_version=excluded.key_version,lock_source=excluded.lock_source,"
            "locked_at=excluded.locked_at",
            (
                raw_name, parent_path, tmdb_id, title, year,
                media_type, season, 1, lock_source, database.now(),
            ),
        )


def list_tmdb_locks(keyword: str = "", limit: int = 200) -> list[sqlite3.Row]:
    sql = "SELECT * FROM tmdb_lock WHERE 1=1"
    params: list[object] = []
    if keyword:
        sql += (
            " AND (raw_name LIKE ? OR parent_path LIKE ? OR title LIKE ? "
            "OR tmdb_id LIKE ?)"
        )
        value = f"%{keyword}%"
        params += [value, value, value, value]
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _database().get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def list_manual_episode_confirmations(tmdb_id: str) -> list[sqlite3.Row]:
    """独立的成功人工编号经验；清理整理日志不会删除学习事实。"""
    with _database().get_conn() as conn:
        return conn.execute(
            "SELECT * FROM manual_episode_confirmations WHERE tmdb_id=? ORDER BY updated_at DESC,log_id DESC",
            (tmdb_id,),
        ).fetchall()


def _save_manual_episode_confirmation(conn: sqlite3.Connection, record: dict) -> None:
    fields = ("source", "file_id", "log_id", "original_name", "original_path", "tmdb_id", "season", "episode", "updated_at")
    conn.execute(
        f"INSERT INTO manual_episode_confirmations({','.join(fields)}) VALUES({','.join('?' for _ in fields)}) "
        "ON CONFLICT(source,file_id) DO UPDATE SET " + ','.join(f"{key}=excluded.{key}" for key in fields[2:]),
        [record[key] for key in fields],
    )


def sync_manual_episode_confirmation(conn: sqlite3.Connection, log_id: int, *, revoke: bool = False) -> None:
    """在业务结果事务内更新经验；暂态、失败及单纯回收不改变已确认事实。"""
    reader = conn.cursor()
    reader.row_factory = sqlite3.Row
    row = reader.execute("SELECT * FROM organize_log WHERE id=?", (log_id,)).fetchone()
    if row is None or not row["source"] or not row["file_id"]:
        return
    key = (row["source"], row["file_id"])
    if revoke:
        conn.execute("DELETE FROM manual_episode_confirmations WHERE source=? AND file_id=?", key)
        return
    if row["confirmation_actor"] == "agent":
        conn.execute(
            "DELETE FROM manual_episode_confirmations WHERE source=? AND file_id=? AND log_id=?",
            (*key, log_id),
        )
        return
    if row["status"] != "success":
        return
    try:
        position = confirmed_manual_position(json.loads(row["release_parse_json"] or "{}"))
    except (TypeError, ValueError):
        position = None
    if (
        row["media_type"] == "tv" and row["provider"] in ("", "tmdb")
        and row["tmdb_id"] and row["original_name"] and position is not None
        and position.get("episode") is not None
        and (position["season"], position["episode"]) == (row["season"], row["episode"])
    ):
        _save_manual_episode_confirmation(conn, {
            "source": row["source"], "file_id": row["file_id"], "log_id": row["id"],
            "original_name": row["original_name"], "original_path": row["original_path"],
            "tmdb_id": row["tmdb_id"], "season": row["season"], "episode": row["episode"],
            "updated_at": row["updated_at"] or row["created_at"],
        })
    else:
        # 人工改成电影或不再有有效编号，不能继续沿用该文件的旧剧集经验。
        conn.execute(
            "DELETE FROM manual_episode_confirmations WHERE source=? AND file_id=? "
            "AND (log_id=? OR ?='reorganize')", (*key, log_id, row["operation_type"]),
        )


def restore_manual_episode_confirmation(conn: sqlite3.Connection, log_id: int, record: dict | None) -> None:
    """跟随可逆业务前像恢复；不复活其它文件的经验。"""
    row = conn.execute("SELECT source,file_id FROM organize_log WHERE id=?", (log_id,)).fetchone()
    if row is None:
        raise LookupError("整理日志不存在")
    if record is not None and (record["source"], record["file_id"]) != tuple(row):
        raise ValueError("人工学习前像与当前文件不一致")
    conn.execute("DELETE FROM manual_episode_confirmations WHERE source=? AND file_id=?", tuple(row))
    if record is not None:
        _save_manual_episode_confirmation(conn, record)


def delete_tmdb_lock(lock_id: int) -> bool:
    with _database().get_conn() as conn:
        cur = conn.execute("DELETE FROM tmdb_lock WHERE id=?", (lock_id,))
        return cur.rowcount > 0
