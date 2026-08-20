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


def get_media_external_id(
    provider: str, external_id: str, media_type: str,
) -> sqlite3.Row | None:
    with _database().get_conn() as conn:
        return conn.execute(
            "SELECT * FROM media_external_ids WHERE provider=? AND external_id=? AND media_type=?",
            (str(provider), str(external_id), str(media_type)),
        ).fetchone()


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
            "confidence=excluded.confidence,confirmed=excluded.confirmed,updated_at=excluded.updated_at "
            "WHERE media_external_ids.confirmed=0 OR excluded.confirmed=1",
            (
                str(provider), str(external_id), str(media_type), str(tmdb_id),
                str(title or ""), str(year or ""),
                max(0.0, min(float(confidence or 0), 1.0)),
                1 if confirmed else 0, database.now(),
            ),
        )


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
    with _database().get_conn() as conn:
        return conn.execute(
            "SELECT * FROM media_watchlist ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
