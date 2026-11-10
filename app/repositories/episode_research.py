"""季集研究证据缓存；只保存证据，不替代 service 对当前元数据的重新核验。"""
from __future__ import annotations

import json
import math
import re
import time
from typing import TYPE_CHECKING, Any
from app import database as db

if TYPE_CHECKING:
    pass


_MAX_ENTRIES = 512
_MAX_PAYLOAD_BYTES = 256 * 1024
_MAX_JSON_DEPTH = 64
_MAX_POLICY_VERSION = 2**63 - 1
_STATUSES = frozenset({"verified", "proposal", "negative"})
_CACHE_KEY = re.compile(r"[0-9a-fA-F]{64}")


def _validate_key(value: object) -> str:
    if type(value) is not str or _CACHE_KEY.fullmatch(value) is None:
        raise ValueError("cache_key 必须是 64 位十六进制字符串")
    return value.lower()


def _validate_policy_version(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_POLICY_VERSION:
        raise ValueError("policy_version 必须是 SQLite 范围内的正整数")
    return value


def _validate_status(value: object) -> str:
    if type(value) is not str or value not in _STATUSES:
        raise ValueError("status 必须是 verified/proposal/negative")
    return value


def _validate_json_value(value: Any) -> None:
    """拒绝隐式类型转换，限制深度和展开节点数；不定义业务 proposal schema。"""
    visited = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal visited
        visited += 1
        # 每个节点在 JSON 中至少占一个字节；也限制共享子树展开的 CPU 开销。
        if depth > _MAX_JSON_DEPTH or visited > _MAX_PAYLOAD_BYTES:
            raise ValueError("payload 嵌套过深、过大或包含循环引用")
        kind = type(item)
        if kind is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("payload 的对象键必须是字符串")
                key.encode("utf-8", errors="strict")
                visit(child, depth + 1)
        elif kind is list:
            for child in item:
                visit(child, depth + 1)
        elif kind is str:
            item.encode("utf-8", errors="strict")
        elif kind is float:
            if not math.isfinite(item):
                raise ValueError("payload 不允许非有限数")
        elif kind not in (int, bool, type(None)):
            raise ValueError("payload 包含非 JSON 类型")

    visit(value, 0)


def _encode_payload(payload: object) -> str:
    if type(payload) is not dict:
        raise ValueError("payload 必须是 JSON object")
    try:
        _validate_json_value(payload)
        encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        chunks = []
        byte_count = 0
        for chunk in encoder.iterencode(payload):
            byte_count += len(chunk.encode("utf-8", errors="strict"))
            if byte_count > _MAX_PAYLOAD_BYTES:
                raise ValueError("payload 不得超过 256 KiB UTF-8 JSON")
            chunks.append(chunk)
        return "".join(chunks)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        # 错误不回显 payload，避免证据或其它调用方输入进入日志。
        raise ValueError("payload 必须是有效、有界的 UTF-8 JSON object") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("payload 包含重复对象键")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("payload 不允许非有限数")


def _decode_payload(raw: object) -> dict[str, Any]:
    if type(raw) is not bytes or len(raw) > _MAX_PAYLOAD_BYTES:
        raise ValueError("缓存 payload 类型或大小无效")
    # 在可控边界解码，坏 UTF-8 TEXT 不得在 sqlite3.fetch 时变成未捕获异常。
    encoded = raw.decode("utf-8", errors="strict")
    payload = json.loads(encoded, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    if type(payload) is not dict:
        raise ValueError("缓存 payload 不是 JSON object")
    _validate_json_value(payload)
    return payload


def _valid_timestamp(value: object) -> bool:
    return isinstance(value, (int, float)) and type(value) in (int, float) and math.isfinite(value) and value > 0


def get_episode_research_cache(
    cache_key: str, *, policy_version: int = 1,
) -> dict[str, Any] | None:
    """只读有效缓存；过期、策略版本不匹配或损坏记录一律视为 miss。"""
    key = _validate_key(cache_key)
    version = _validate_policy_version(policy_version)
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT status,CAST(payload AS BLOB) AS payload,expires_at,updated_at,policy_version "
            "FROM episode_research_cache WHERE cache_key=? AND policy_version=? AND expires_at>? "
            "AND typeof(policy_version)='integer' AND status IN ('verified','proposal','negative') "
            "AND typeof(expires_at) IN ('integer','real') "
            "AND typeof(updated_at) IN ('integer','real') "
            "AND typeof(payload)='text' AND length(CAST(payload AS BLOB))<=?",
            (key, version, time.time(), _MAX_PAYLOAD_BYTES),
        ).fetchone()
    if row is None:
        return None
    try:
        status = _validate_status(row["status"])
        stored_version = _validate_policy_version(row["policy_version"])
        if (stored_version != version or not _valid_timestamp(row["expires_at"])
                or not _valid_timestamp(row["updated_at"])
                or row["expires_at"] <= row["updated_at"] or row["expires_at"] <= time.time()):
            return None
        payload = _decode_payload(row["payload"])
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    return {
        "status": status, "payload": payload,
        "expires_at": row["expires_at"], "policy_version": stored_version,
    }


def put_episode_research_cache(
    cache_key: str,
    payload: dict[str, Any],
    *,
    status: str,
    ttl_seconds: int,
    policy_version: int = 1,
) -> None:
    """同一写事务清过期、更新记录、逐出最旧；任何失败均整体回滚。"""
    key = _validate_key(cache_key)
    version = _validate_policy_version(policy_version)
    status = _validate_status(status)
    if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 2_592_000:
        raise ValueError("ttl_seconds 必须是 60..2592000 的整数")
    encoded = _encode_payload(payload)
    with db.get_conn() as conn:
        # 先获得跨连接写锁再取时间，避免并发 count/逐出越限或等待缩短 TTL。
        conn.execute("BEGIN IMMEDIATE")
        timestamp = time.time()
        conn.execute("DELETE FROM episode_research_cache WHERE expires_at<=?", (timestamp,))
        conn.execute(
            "INSERT INTO episode_research_cache"
            "(cache_key,policy_version,status,payload,expires_at,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET "
            "policy_version=excluded.policy_version,status=excluded.status,payload=excluded.payload,"
            "expires_at=excluded.expires_at,updated_at=excluded.updated_at",
            (key, version, status, encoded, timestamp + ttl_seconds, timestamp),
        )
        count = conn.execute("SELECT COUNT(*) FROM episode_research_cache").fetchone()[0]
        if count > _MAX_ENTRIES:
            conn.execute(
                "DELETE FROM episode_research_cache WHERE cache_key IN ("
                "SELECT cache_key FROM episode_research_cache "
                "ORDER BY updated_at,cache_key LIMIT ?)",
                (count - _MAX_ENTRIES,),
            )


def invalidate_episode_research_cache(cache_key: str) -> None:
    """幂等删除单个证据缓存，不清理其它研究或网页缓存。"""
    key = _validate_key(cache_key)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM episode_research_cache WHERE cache_key=?", (key,))
