"""媒体探索缓存、跨来源映射与收藏的数据访问。"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any
from app import database as db

if TYPE_CHECKING:
    pass


def get_discovery_cache(cache_key: str) -> sqlite3.Row | None:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM discovery_cache WHERE cache_key=?", (cache_key,)
        ).fetchone()


def get_discovery_cache_many(cache_keys: list[str]) -> dict[str, sqlite3.Row]:
    """一次批量读取列表页需要的探索缓存行。"""
    keys = list(dict.fromkeys(key for key in cache_keys if key))
    if not keys:
        return {}
    result: dict[str, sqlite3.Row] = {}
    with db.get_conn() as conn:
        for offset in range(0, len(keys), 900):
            chunk = keys[offset:offset + 900]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT * FROM discovery_cache WHERE cache_key IN ({placeholders})",
                chunk,
            ).fetchall()
            result.update({str(row["cache_key"]): row for row in rows})
    return result


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
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO discovery_cache(cache_key,provider,payload,fetched_at,expires_at,stale_until,last_error,status) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET "
            "provider=excluded.provider,payload=excluded.payload,fetched_at=excluded.fetched_at,"
            "expires_at=excluded.expires_at,stale_until=excluded.stale_until,"
            "last_error=excluded.last_error,status=excluded.status",
            (
                cache_key, provider, payload, fetched_at, expires_at,
                stale_until, (last_error or "")[:500], normalized_status,
            ),
        )


def update_discovery_cache_error(cache_key: str, last_error: str) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE discovery_cache SET last_error=? WHERE cache_key=?",
            ((last_error or "")[:500], cache_key),
        )


def purge_discovery_cache(
    expired_before: str,
    *,
    max_rows: int = 10_000,
    batch_size: int = 5_000,
) -> int:
    """分批清理过期和最旧探索缓存，避免持久缓存无界增长。"""
    row_limit = max(1, max_rows)
    delete_limit = max(1, batch_size)
    deleted = 0
    with db.get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM discovery_cache WHERE cache_key IN ("
            "SELECT cache_key FROM discovery_cache WHERE stale_until < ? "
            "ORDER BY stale_until, cache_key LIMIT ?"
            ")",
            (expired_before, delete_limit),
        )
        deleted += max(0, (cursor.rowcount or 0))
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
            deleted += max(0, (cursor.rowcount or 0))
    return deleted


def get_media_external_id(
    provider: str, external_id: str, media_type: str,
) -> sqlite3.Row | None:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM media_external_ids WHERE provider=? AND external_id=? AND media_type=?",
            (provider, external_id, media_type),
        ).fetchone()


def list_media_external_ids(
    identities: list[tuple[str, str, str]],
) -> dict[tuple[str, str, str], sqlite3.Row]:
    """在单个连接内批量读取跨来源映射，避免收藏列表逐条建连。"""
    keys = list(dict.fromkeys(
        (provider, external_id, media_type)
        for provider, external_id, media_type in identities
        if provider and external_id and media_type
    ))
    if not keys:
        return {}
    result: dict[tuple[str, str, str], sqlite3.Row] = {}
    # SQLite 常见变量上限为 999；每个 identity 使用三个绑定参数。
    with db.get_conn() as conn:
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
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO media_external_ids(provider,external_id,media_type,tmdb_id,title,year,confidence,confirmed,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(provider,external_id,media_type) DO UPDATE SET "
            "tmdb_id=excluded.tmdb_id,title=excluded.title,year=excluded.year,"
            "confidence=excluded.confidence,confirmed=excluded.confirmed,"
            "version=media_external_ids.version+1,updated_at=excluded.updated_at "
            "WHERE media_external_ids.confirmed=0 OR excluded.confirmed=1",
            (
                provider, external_id, media_type, tmdb_id,
                (title or ""), (year or ""),
                max(0.0, min(float(confidence or 0), 1.0)),
                1 if confirmed else 0, db.now(),
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
    identity = (provider, external_id, media_type)
    values = (
        tmdb_id,
        (title or ""),
        (year or ""),
        1.0,
        1,
        db.now(),
    )
    with db.get_conn() as conn:
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
        return cur.rowcount == 1


def _watchlist_key(provider: str, external_id: str, media_type: str) -> str:
    return f"{provider.lower()}:{media_type.lower()}:{external_id}"


def list_media_watchlist_keys(identities: list[tuple[str, str, str]]) -> set[str]:
    """先去重再有界分批；一个读事务保持整页收藏状态的一致快照。"""
    normalized = list(dict.fromkeys((p, e, m) for p, e, m in identities))
    if not normalized:
        return set()
    watched: set[str] = set()
    with db.get_conn() as conn:
        conn.execute("BEGIN")
        for offset in range(0, len(normalized), 300):
            chunk = normalized[offset:offset + 300]
            clauses = " OR ".join(
                "(provider=? AND external_id=? AND media_type=?)" for _ in chunk
            )
            params = [value for identity in chunk for value in identity]
            rows = conn.execute(
                f"SELECT provider,external_id,media_type FROM media_watchlist WHERE {clauses}",
                params,
            )
            watched.update(
                _watchlist_key(row["provider"], row["external_id"], row["media_type"])
                for row in rows
            )
    return watched


def add_media_watchlist(
    provider: str,
    external_id: str,
    media_type: str,
    title: str = "",
    year: str = "",
    poster_key: str = "",
    *,
    expected_absent: bool = False,
) -> int | None:
    """普通添加更新元数据；冻结预览只允许首次插入，冲突返回 None。"""
    conflict = "DO NOTHING" if expected_absent else (
        "DO UPDATE SET title=excluded.title,year=excluded.year,poster_key=excluded.poster_key"
    )
    with db.get_conn() as conn:
        row = conn.execute(
            "INSERT INTO media_watchlist(provider,external_id,media_type,title,year,poster_key,created_at) "
            f"VALUES(?,?,?,?,?,?,?) ON CONFLICT(provider,external_id,media_type) {conflict} RETURNING id",
            (
                provider, external_id, media_type, (title or ""),
                (year or ""), (poster_key or ""), db.now(),
            ),
        ).fetchone()
        return int(row["id"]) if row is not None else None


def get_media_watchlist(
    provider: str, external_id: str, media_type: str
) -> sqlite3.Row | None:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM media_watchlist WHERE provider=? AND external_id=? AND media_type=?",
            (provider, external_id, media_type),
        ).fetchone()


def get_media_watchlist_by_id(watchlist_id: int) -> sqlite3.Row | None:
    """读取单管理员媒体工作区的共享收藏；会话 owner 不是权限主体。"""
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM media_watchlist WHERE id=?",
            (watchlist_id,),
        ).fetchone()


def media_watchlist_snapshot(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {"exists": False}
    return {
        "exists": True,
        "watchlist_number": int(row["id"]),
        **{key: str(row[key] or "") for key in (
            "provider", "external_id", "media_type", "title", "year", "poster_key", "created_at",
        )},
    }


def delete_media_watchlist(
    provider: str, external_id: str, media_type: str, *,
    expected_snapshot: dict[str, Any] | None = None,
) -> bool:
    """在同一条 DELETE 中核对完整快照，避免预检后覆盖并发修改。"""
    where = "provider=? AND external_id=? AND media_type=?"
    parameters: tuple[Any, ...] = (provider, external_id, media_type)
    if expected_snapshot is not None:
        if expected_snapshot.get("exists") is not True or parameters != tuple(
            expected_snapshot.get(key) for key in ("provider", "external_id", "media_type")
        ):
            return False
        where += " AND id=? AND COALESCE(title,'')=? AND COALESCE(year,'')=? AND COALESCE(poster_key,'')=? AND COALESCE(created_at,'')=?"
        parameters += tuple(expected_snapshot.get(key) for key in (
            "watchlist_number", "title", "year", "poster_key", "created_at",
        ))
    with db.get_conn() as conn:
        cur = conn.execute(
            f"DELETE FROM media_watchlist WHERE {where}", parameters,
        )
        return bool(cur.rowcount)


def list_media_watchlist(
    limit: int = 500, *, before_number: int | None = None,
) -> list[sqlite3.Row]:
    """按编号倒序读取共享收藏；续页不受新收藏插入或前页删除影响。"""
    where = " WHERE id < ?" if before_number is not None else ""
    parameters = (() if before_number is None else (before_number,)) + (
        max(1, min(limit, 1000)),
    )
    with db.get_conn() as conn:
        return conn.execute(
            f"SELECT * FROM media_watchlist{where} ORDER BY id DESC LIMIT ?",
            parameters,
        ).fetchall()


def get_media_watchlist_statistics() -> list[sqlite3.Row]:
    """单次只读查询计算全库及各类型数量，并取各类型编号最新的三项。"""
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT id,provider,media_type,title,year,total,type_count FROM ("
            "SELECT id,provider,media_type,title,year,"
            "COUNT(*) OVER () AS total,"
            "COUNT(*) OVER (PARTITION BY media_type) AS type_count,"
            "ROW_NUMBER() OVER (PARTITION BY media_type ORDER BY id DESC) AS type_rank "
            "FROM media_watchlist"
            ") WHERE type_rank <= 3 ORDER BY media_type,id DESC"
        ).fetchall()


def count_media_watchlist() -> int:
    with db.get_conn() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM media_watchlist").fetchone()[0])
