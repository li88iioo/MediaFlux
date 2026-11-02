"""识别领域中稳定、无业务决策的 SQLite 访问边界。"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

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
    """只返回成功的人工作品纠偏；普通自动整理不能成为自我学习证据。"""
    with _database().get_conn() as conn:
        return conn.execute(
            "SELECT id,original_name,original_path,season,episode,release_parse_json "
            "FROM organize_log WHERE tmdb_id=? AND media_type='tv' "
            "AND status='success' AND provider IN ('','tmdb') "
            "AND confirmation_actor!='agent' "
            "AND instr(release_parse_json,'\"manual_position\"')>0 ORDER BY id DESC",
            (tmdb_id,),
        ).fetchall()


def delete_tmdb_lock(lock_id: int) -> bool:
    with _database().get_conn() as conn:
        cur = conn.execute("DELETE FROM tmdb_lock WHERE id=?", (lock_id,))
        return cur.rowcount > 0
