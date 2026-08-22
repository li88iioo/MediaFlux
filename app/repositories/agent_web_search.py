"""Agent 网页搜索每日额度的数据访问。"""
from __future__ import annotations

from datetime import datetime
import json
import re
import time
from typing import Any
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType


def _database() -> "ModuleType":
    """延迟取得数据库门面，保持测试 DB_PATH/configure_database 的唯一状态。"""
    from app import database

    return database


def _validate_agent_web_search_usage_date(value: str) -> str:
    usage_date = str(value or "").strip()
    try:
        parsed = datetime.strptime(usage_date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("usage_date 必须是 YYYY-MM-DD") from exc
    if parsed.strftime("%Y-%m-%d") != usage_date:
        raise ValueError("usage_date 必须是 YYYY-MM-DD")
    return usage_date


def reserve_agent_web_search_credits(
    *, provider: str, usage_date: str, cost: int, daily_limit: int
) -> bool:
    provider_name = str(provider or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", provider_name):
        raise ValueError("provider 无效")
    day = _validate_agent_web_search_usage_date(usage_date)
    if isinstance(cost, bool) or isinstance(daily_limit, bool):
        raise ValueError("搜索额度参数无效")
    unit_cost = int(cost)
    limit = int(daily_limit)
    if unit_cost < 1 or limit < unit_cost:
        return False

    database = _database()
    timestamp = database.now()
    with database.get_conn() as conn:
        # 必须保持为同一 IMMEDIATE 事务，防止并发请求透支每日额度。
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO agent_web_search_daily_usage"
            "(provider,usage_date,credits_used,updated_at) VALUES(?,?,0,?)",
            (provider_name, day, timestamp),
        )
        cursor = conn.execute(
            "UPDATE agent_web_search_daily_usage "
            "SET credits_used=credits_used+?,updated_at=? "
            "WHERE provider=? AND usage_date=? AND credits_used+?<=?",
            (unit_cost, timestamp, provider_name, day, unit_cost, limit),
        )
        return cursor.rowcount == 1


def refund_agent_web_search_credits(
    *, provider: str, usage_date: str, cost: int
) -> bool:
    provider_name = str(provider or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", provider_name):
        raise ValueError("provider 无效")
    day = _validate_agent_web_search_usage_date(usage_date)
    if isinstance(cost, bool) or int(cost) < 1:
        raise ValueError("搜索额度参数无效")
    database = _database()
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE agent_web_search_daily_usage "
            "SET credits_used=MAX(0,credits_used-?),updated_at=? "
            "WHERE provider=? AND usage_date=?",
            (int(cost), database.now(), provider_name, day),
        )
        return cursor.rowcount == 1


def _validate_cache_key(value: str) -> str:
    cache_key = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", cache_key):
        raise ValueError("cache_key 无效")
    return cache_key


def get_agent_web_search_cache(cache_key: str) -> dict[str, Any] | None:
    key = _validate_cache_key(cache_key)
    now_epoch = int(time.time())
    with _database().get_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM agent_web_search_cache "
            "WHERE cache_key=? AND expires_at>?",
            (key, now_epoch),
        ).fetchone()
        if row is None:
            conn.execute(
                "DELETE FROM agent_web_search_cache WHERE cache_key=? AND expires_at<=?",
                (key, now_epoch),
            )
            return None
    try:
        payload = json.loads(str(row["payload"]))
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def set_agent_web_search_cache(
    cache_key: str, payload: dict[str, Any], *, ttl_seconds: int
) -> None:
    key = _validate_cache_key(cache_key)
    if isinstance(ttl_seconds, bool) or not 1 <= int(ttl_seconds) <= 86400:
        raise ValueError("缓存 TTL 无效")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 512 * 1024:
        raise ValueError("缓存内容过大")
    database = _database()
    with database.get_conn() as conn:
        conn.execute(
            "INSERT INTO agent_web_search_cache(cache_key,payload,expires_at,updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET "
            "payload=excluded.payload,expires_at=excluded.expires_at,updated_at=excluded.updated_at",
            (key, encoded, int(time.time()) + int(ttl_seconds), database.now()),
        )
        conn.execute(
            "DELETE FROM agent_web_search_cache WHERE expires_at<=?",
            (int(time.time()),),
        )


def clear_agent_web_search_cache() -> None:
    with _database().get_conn() as conn:
        conn.execute("DELETE FROM agent_web_search_cache")


def get_agent_web_search_daily_usage(*, provider: str, usage_date: str) -> int:
    provider_name = str(provider or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", provider_name):
        raise ValueError("provider 无效")
    day = _validate_agent_web_search_usage_date(usage_date)
    with _database().get_conn() as conn:
        row = conn.execute(
            "SELECT credits_used FROM agent_web_search_daily_usage "
            "WHERE provider=? AND usage_date=?",
            (provider_name, day),
        ).fetchone()
    return int(row["credits_used"]) if row is not None else 0
