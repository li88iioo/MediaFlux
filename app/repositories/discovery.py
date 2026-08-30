"""媒体探索缓存、跨来源映射与收藏的数据访问。"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType


def _database() -> "ModuleType":
    # 延迟导入避免 app.database 在加载兼容导出时形成循环依赖；同时保证
    # DB_PATH/configure_database 的唯一状态仍由兼容门面持有。
    from app import database

    return database


def get_discovery_cache(cache_key: str) -> sqlite3.Row | None:
    with _database().get_conn() as conn:
        return conn.execute(
            "SELECT * FROM discovery_cache WHERE cache_key=?", (str(cache_key),)
        ).fetchone()


def upsert_discovery_cache(
    cache_key: str,
    provider: str,
    payload: str,
    fetched_at: str,
    expires_at: str,
    stale_until: str,
    last_error: str = "",
    status: str = "success",
) -> None:
    normalized_status = "error" if status == "error" else "success"
    with _database().get_conn() as conn:
        conn.execute(
            "INSERT INTO discovery_cache(cache_key,provider,payload,fetched_at,expires_at,stale_until,last_error,status) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET "
            "provider=excluded.provider,payload=excluded.payload,fetched_at=excluded.fetched_at,"
            "expires_at=excluded.expires_at,stale_until=excluded.stale_until,"
            "last_error=excluded.last_error,status=excluded.status",
            (
                str(cache_key), str(provider), str(payload), fetched_at, expires_at,
                stale_until, str(last_error or "")[:500], normalized_status,
            ),
        )


def update_discovery_cache_error(cache_key: str, last_error: str) -> None:
    with _database().get_conn() as conn:
        conn.execute(
            "UPDATE discovery_cache SET last_error=? WHERE cache_key=?",
            (str(last_error or "")[:500], str(cache_key)),
        )


def purge_discovery_cache(
    expired_before: str,
    *,
    max_rows: int = 10_000,
    batch_size: int = 5_000,
) -> int:
    """分批清理过期和最旧探索缓存，避免持久缓存无界增长。"""
    row_limit = max(1, int(max_rows))
    delete_limit = max(1, int(batch_size))
    deleted = 0
    with _database().get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM discovery_cache WHERE cache_key IN ("
            "SELECT cache_key FROM discovery_cache WHERE stale_until < ? "
            "ORDER BY stale_until, cache_key LIMIT ?"
            ")",
            (str(expired_before), delete_limit),
        )
        deleted += max(0, int(cursor.rowcount or 0))
        total = int(conn.execute(
            "SELECT COUNT(*) FROM discovery_cache"
        ).fetchone()[0])
        overflow = max(0, total - row_limit)
        if overflow:
            cursor = conn.execute(
                "DELETE FROM discovery_cache WHERE cache_key IN ("
                "SELECT cache_key FROM discovery_cache "
                "ORDER BY fetched_at, cache_key LIMIT ?"
                ")",
                (min(overflow, delete_limit),),
            )
            deleted += max(0, int(cursor.rowcount or 0))
    return deleted


def get_media_external_id(
    provider: str, external_id: str, media_type: str,
) -> sqlite3.Row | None:
    with _database().get_conn() as conn:
        return conn.execute(
            "SELECT * FROM media_external_ids WHERE provider=? AND external_id=? AND media_type=?",
            (str(provider), str(external_id), str(media_type)),
        ).fetchone()


def list_media_external_ids(
    identities: list[tuple[str, str, str]],
) -> dict[tuple[str, str, str], sqlite3.Row]:
    """在单个连接内批量读取跨来源映射，避免收藏列表逐条建连。"""
    keys = list(dict.fromkeys(
        (str(provider), str(external_id), str(media_type))
        for provider, external_id, media_type in identities
        if str(provider) and str(external_id) and str(media_type)
    ))
    if not keys:
        return {}
    result: dict[tuple[str, str, str], sqlite3.Row] = {}
    # SQLite 常见变量上限为 999；每个 identity 使用三个绑定参数。
    with _database().get_conn() as conn:
        for offset in range(0, len(keys), 300):
            chunk = keys[offset:offset + 300]
            clauses = " OR ".join(
                "(provider=? AND external_id=? AND media_type=?)"
                for _ in chunk
            )
            params = tuple(value for key in chunk for value in key)
            rows = conn.execute(
                "SELECT * FROM media_external_ids WHERE " + clauses,
                params,
            ).fetchall()
            result.update({
                (
                    str(row["provider"]),
                    str(row["external_id"]),
                    str(row["media_type"]),
                ): row
                for row in rows
            })
    return result


def upsert_media_external_id(
    provider: str,
    external_id: str,
    media_type: str,
    tmdb_id: str,
    title: str = "",
    year: str = "",
    confidence: float = 0,
    confirmed: bool = False,
) -> None:
    database = _database()
    with database.get_conn() as conn:
        conn.execute(
            "INSERT INTO media_external_ids(provider,external_id,media_type,tmdb_id,title,year,confidence,confirmed,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(provider,external_id,media_type) DO UPDATE SET "
            "tmdb_id=excluded.tmdb_id,title=excluded.title,year=excluded.year,"
            "confidence=excluded.confidence,confirmed=excluded.confirmed,"
            "version=media_external_ids.version+1,updated_at=excluded.updated_at "
            "WHERE media_external_ids.confirmed=0 OR excluded.confirmed=1",
            (
                str(provider), str(external_id), str(media_type), str(tmdb_id),
                str(title or ""), str(year or ""),
                max(0.0, min(float(confidence or 0), 1.0)),
                1 if confirmed else 0, database.now(),
            ),
        )


def confirm_media_external_id_if_unchanged(
    provider: str,
    external_id: str,
    media_type: str,
    tmdb_id: str,
    title: str,
    year: str,
    expected: dict | None,
) -> bool:
    """仅当来源映射仍与确认预检快照一致时写入 confirmed 映射。"""
    database = _database()
    identity = (str(provider), str(external_id), str(media_type))
    values = (
        str(tmdb_id),
        str(title or ""),
        str(year or ""),
        1.0,
        1,
        database.now(),
    )
    with database.get_conn() as conn:
        if expected is None:
            cur = conn.execute(
                "INSERT INTO media_external_ids("
                "provider,external_id,media_type,tmdb_id,title,year,confidence,confirmed,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(provider,external_id,media_type) DO NOTHING",
                (*identity, *values),
            )
        else:
            cur = conn.execute(
                "UPDATE media_external_ids SET tmdb_id=?,title=?,year=?,confidence=?,"
                "confirmed=?,updated_at=?,version=version+1 "
                "WHERE provider=? AND external_id=? AND media_type=? AND version=? "
                "AND COALESCE(tmdb_id,'')=? AND COALESCE(confirmed,0)=?",
                (
                    *values,
                    *identity,
                    max(0, int(expected.get("version") or 0)),
                    str(expected.get("tmdb_id") or ""),
                    1 if bool(expected.get("confirmed")) else 0,
                ),
            )
        return int(cur.rowcount or 0) == 1


def _watchlist_key(provider: str, external_id: str, media_type: str) -> str:
    return f"{str(provider).lower()}:{str(media_type).lower()}:{str(external_id)}"


def list_media_watchlist_keys(identities: list[tuple[str, str, str]]) -> set[str]:
    normalized = [(str(p), str(e), str(m)) for p, e, m in identities]
    if not normalized:
        return set()
    clauses = " OR ".join(
        "(provider=? AND external_id=? AND media_type=?)" for _ in normalized
    )
    params = [value for identity in normalized for value in identity]
    with _database().get_conn() as conn:
        rows = conn.execute(
            f"SELECT provider,external_id,media_type FROM media_watchlist WHERE {clauses}",
            params,
        ).fetchall()
    return {
        _watchlist_key(row["provider"], row["external_id"], row["media_type"])
        for row in rows
    }


def add_media_watchlist(
    provider: str,
    external_id: str,
    media_type: str,
    title: str = "",
    year: str = "",
    poster_key: str = "",
) -> None:
    database = _database()
    with database.get_conn() as conn:
        conn.execute(
            "INSERT INTO media_watchlist(provider,external_id,media_type,title,year,poster_key,created_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(provider,external_id,media_type) DO UPDATE SET "
            "title=excluded.title,year=excluded.year,poster_key=excluded.poster_key",
            (
                str(provider), str(external_id), str(media_type), str(title or ""),
                str(year or ""), str(poster_key or ""), database.now(),
            ),
        )


def get_media_watchlist(
    provider: str, external_id: str, media_type: str
) -> sqlite3.Row | None:
    with _database().get_conn() as conn:
        return conn.execute(
            "SELECT * FROM media_watchlist WHERE provider=? AND external_id=? AND media_type=?",
            (str(provider), str(external_id), str(media_type)),
        ).fetchone()


def get_media_watchlist_by_id(watchlist_id: int) -> sqlite3.Row | None:
    """读取单管理员媒体工作区的共享收藏；会话 owner 不是权限主体。"""
    with _database().get_conn() as conn:
        return conn.execute(
            "SELECT * FROM media_watchlist WHERE id=?",
            (int(watchlist_id),),
        ).fetchone()


def delete_media_watchlist(provider: str, external_id: str, media_type: str) -> bool:
    with _database().get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM media_watchlist WHERE provider=? AND external_id=? AND media_type=?",
            (str(provider), str(external_id), str(media_type)),
        )
        return bool(cur.rowcount)


def list_media_watchlist(limit: int = 500) -> list[sqlite3.Row]:
    """列出单管理员媒体工作区的共享收藏。"""
    with _database().get_conn() as conn:
        return conn.execute(
            "SELECT * FROM media_watchlist ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
