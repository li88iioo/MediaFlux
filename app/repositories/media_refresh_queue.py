"""持久媒体库刷新队列。

队列状态复用现有 ``settings_kv`` 的单行 JSON，并在 ``BEGIN IMMEDIATE``
事务内更新。这样无需给并行开发中的正式 schema 迁移增加耦合，同时仍能保证
多线程/多进程入队、领取和确认之间不会丢失变化路径。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType

_QUEUE_KEY = "media_refresh_queue:v1"
_STATE_VERSION = 1
_MAX_RECENT_TARGETS = 4096


def _database() -> "ModuleType":
    from app import database

    return database


def _empty_state() -> dict[str, Any]:
    return {"version": _STATE_VERSION, "groups": {}, "recent": {}}


def _read_state(conn) -> dict[str, Any]:
    row = conn.execute(
        "SELECT value FROM settings_kv WHERE key=?", (_QUEUE_KEY,)
    ).fetchone()
    if row is None or not str(row["value"] or "").strip():
        return _empty_state()
    try:
        state = json.loads(str(row["value"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("媒体库刷新队列状态损坏，已拒绝覆盖") from exc
    if not isinstance(state, dict) or int(state.get("version") or 0) != _STATE_VERSION:
        raise RuntimeError("媒体库刷新队列版本无效，已拒绝覆盖")
    if not isinstance(state.get("groups"), dict) or not isinstance(state.get("recent"), dict):
        raise RuntimeError("媒体库刷新队列结构无效，已拒绝覆盖")
    return state


def _write_state(conn, state: dict[str, Any]) -> None:
    database = _database()
    payload = json.dumps(
        state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        "INSERT INTO settings_kv(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (_QUEUE_KEY, payload, database.now()),
    )


def _normalized_provider(value: object) -> str:
    provider = str(value or "").strip().lower()
    if provider not in {"jellyfin", "emby"}:
        raise ValueError("媒体服务器类型无效")
    return provider


def _normalized_library_ids(values: object) -> tuple[str, ...]:
    return tuple(sorted(dict.fromkeys(
        str(item or "").strip()
        for item in (values or ())
        if str(item or "").strip()
    )))


def _normalized_paths(values: object) -> list[str]:
    return list(dict.fromkeys(
        str(item or "").strip()
        for item in (values or ())
        if str(item or "").strip()
    ))


def _group_key(provider: str, allowed_library_ids: tuple[str, ...]) -> str:
    payload = json.dumps(
        [provider, list(allowed_library_ids)],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


def _merge_paths(*groups: object) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for path in group or ():
            text = str(path or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            merged.append(text)
    return merged


def _prune_recent(state: dict[str, Any], now_epoch: float) -> int:
    recent = state["recent"]
    before = len(recent)
    alive: list[tuple[str, float]] = []
    for key, raw_expiry in list(recent.items()):
        try:
            expiry = float(raw_expiry or 0)
        except (TypeError, ValueError, OverflowError):
            expiry = 0
        if expiry <= now_epoch:
            recent.pop(key, None)
            continue
        alive.append((str(key), expiry))
    if len(alive) <= _MAX_RECENT_TARGETS:
        return before - len(recent)
    for key, _expiry in sorted(alive, key=lambda item: item[1])[:-_MAX_RECENT_TARGETS]:
        recent.pop(key, None)
    return before - len(recent)


def _recover_running_groups(
    state: dict[str, Any], now_epoch: float, *, force: bool = False,
) -> int:
    recovered = 0
    for group in state["groups"].values():
        if not isinstance(group, dict) or str(group.get("status") or "") != "running":
            continue
        try:
            lease_until = float(group.get("lease_until") or 0)
        except (TypeError, ValueError, OverflowError):
            lease_until = 0
        if not force and lease_until > now_epoch:
            continue
        try:
            due_at = float(group.get("due_at") or now_epoch)
        except (TypeError, ValueError, OverflowError):
            due_at = now_epoch
        group["pending_paths"] = _merge_paths(
            group.get("inflight_paths"), group.get("pending_paths")
        )
        group["inflight_paths"] = []
        group["status"] = "retry_wait"
        group["lease_owner"] = ""
        group["lease_until"] = 0
        group["due_at"] = min(due_at, now_epoch)
        recovered += 1
    return recovered


def enqueue_media_refresh(
    provider: str,
    paths: object,
    *,
    allowed_library_ids: object = (),
    debounce_seconds: float = 20.0,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """合并一组变化路径；运行中到达的新路径只进入 pending，不覆盖 inflight。"""
    normalized_provider = _normalized_provider(provider)
    normalized_ids = _normalized_library_ids(allowed_library_ids)
    normalized_paths = _normalized_paths(paths)
    if not normalized_paths:
        return {"queued": 0, "group_key": "", "path_count": 0}
    now_value = float(time.time() if now_epoch is None else now_epoch)
    due_at = now_value + max(0.0, float(debounce_seconds or 0.0))
    key = _group_key(normalized_provider, normalized_ids)
    database = _database()
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = _read_state(conn)
        # 生产者只追加 pending，绝不依据时间回收 running；消费者可能仍在执行
        # 一个较慢的媒体服务器请求。崩溃恢复只允许持有跨进程消费者锁的一方执行。
        _prune_recent(state, now_value)
        group = state["groups"].get(key)
        if not isinstance(group, dict):
            group = {
                "provider": normalized_provider,
                "allowed_library_ids": list(normalized_ids),
                "pending_paths": [],
                "inflight_paths": [],
                "status": "queued",
                "due_at": due_at,
                "attempts": 0,
                "lease_owner": "",
                "lease_until": 0,
                "lease_generation": 0,
                "last_error": "",
                "created_at": now_value,
                "updated_at": now_value,
            }
            state["groups"][key] = group
        elif (
            str(group.get("provider") or "") != normalized_provider
            or _normalized_library_ids(group.get("allowed_library_ids")) != normalized_ids
        ):
            raise RuntimeError("媒体库刷新队列分组冲突")
        before = len(_normalized_paths(group.get("pending_paths")))
        group["pending_paths"] = _merge_paths(
            group.get("pending_paths"), normalized_paths
        )
        if str(group.get("status") or "") != "running":
            group["status"] = "queued"
        # quiet-window 语义：每次新入队都把 pending 的执行时间推到最后一次变化后。
        group["due_at"] = due_at
        group["updated_at"] = now_value
        group["last_error"] = ""
        _write_state(conn, state)
    return {
        "queued": max(0, len(group["pending_paths"]) - before),
        "group_key": key,
        "path_count": len(group["pending_paths"]),
        "due_at": due_at,
    }


def claim_due_media_refreshes(
    *,
    owner: str,
    lease_seconds: int = 300,
    limit: int = 16,
    force: bool = False,
    now_epoch: float | None = None,
) -> list[dict[str, Any]]:
    safe_owner = str(owner or "").strip()
    if not safe_owner:
        raise ValueError("媒体库刷新 worker owner 不能为空")
    now_value = float(time.time() if now_epoch is None else now_epoch)
    safe_limit = max(1, min(int(limit or 1), 64))
    database = _database()
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = _read_state(conn)
        recovered = _recover_running_groups(state, now_value)
        pruned = _prune_recent(state, now_value)
        candidates: list[tuple[float, str, dict[str, Any]]] = []
        for key, group in state["groups"].items():
            if not isinstance(group, dict):
                continue
            status = str(group.get("status") or "")
            if status not in {"queued", "retry_wait"}:
                continue
            pending = _normalized_paths(group.get("pending_paths"))
            if not pending:
                continue
            try:
                due_at = float(group.get("due_at") or 0)
            except (TypeError, ValueError, OverflowError):
                due_at = 0
            if not force and due_at > now_value:
                continue
            candidates.append((due_at, str(key), group))
        claimed: list[dict[str, Any]] = []
        for _due_at, key, group in sorted(candidates)[:safe_limit]:
            pending = _normalized_paths(group.get("pending_paths"))
            generation = max(0, int(group.get("lease_generation") or 0)) + 1
            group["pending_paths"] = []
            group["inflight_paths"] = pending
            group["status"] = "running"
            group["lease_owner"] = safe_owner
            group["lease_until"] = now_value + max(30, int(lease_seconds or 300))
            group["lease_generation"] = generation
            group["updated_at"] = now_value
            claimed.append({
                "group_key": key,
                "provider": str(group.get("provider") or ""),
                "allowed_library_ids": list(group.get("allowed_library_ids") or []),
                "paths": pending,
                "attempts": max(0, int(group.get("attempts") or 0)),
                "lease_generation": generation,
            })
        if recovered or pruned or claimed:
            _write_state(conn, state)
    return claimed


def complete_media_refresh(
    group_key: str,
    *,
    owner: str,
    lease_generation: int,
    refreshed_target_ids: object = (),
    recent_ttl_seconds: int = 90,
    now_epoch: float | None = None,
) -> bool:
    now_value = float(time.time() if now_epoch is None else now_epoch)
    database = _database()
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = _read_state(conn)
        group = state["groups"].get(str(group_key or ""))
        if not isinstance(group, dict):
            return False
        if (
            str(group.get("status") or "") != "running"
            or str(group.get("lease_owner") or "") != str(owner or "")
            or int(group.get("lease_generation") or 0) != int(lease_generation)
        ):
            return False
        provider = str(group.get("provider") or "")
        ttl = max(0, int(recent_ttl_seconds or 0))
        if ttl:
            expiry = now_value + ttl
            for item_id in _normalized_paths(refreshed_target_ids):
                state["recent"][f"{provider}:{item_id}"] = expiry
        pending = _normalized_paths(group.get("pending_paths"))
        if pending:
            group["inflight_paths"] = []
            group["status"] = "queued"
            group["attempts"] = 0
            group["lease_owner"] = ""
            group["lease_until"] = 0
            group["last_error"] = ""
            group["updated_at"] = now_value
        else:
            state["groups"].pop(str(group_key), None)
        _prune_recent(state, now_value)
        _write_state(conn, state)
    return True


def fail_media_refresh(
    group_key: str,
    *,
    owner: str,
    lease_generation: int,
    error: object,
    retry_seconds: int,
    refreshed_target_ids: object = (),
    recent_ttl_seconds: int = 90,
    now_epoch: float | None = None,
) -> bool:
    now_value = float(time.time() if now_epoch is None else now_epoch)
    database = _database()
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = _read_state(conn)
        group = state["groups"].get(str(group_key or ""))
        if not isinstance(group, dict):
            return False
        if (
            str(group.get("status") or "") != "running"
            or str(group.get("lease_owner") or "") != str(owner or "")
            or int(group.get("lease_generation") or 0) != int(lease_generation)
        ):
            return False
        provider = str(group.get("provider") or "")
        ttl = max(0, int(recent_ttl_seconds or 0))
        if ttl:
            expiry = now_value + ttl
            for item_id in _normalized_paths(refreshed_target_ids):
                state["recent"][f"{provider}:{item_id}"] = expiry
        attempts = max(0, int(group.get("attempts") or 0)) + 1
        group["pending_paths"] = _merge_paths(
            group.get("inflight_paths"), group.get("pending_paths")
        )
        group["inflight_paths"] = []
        group["status"] = "retry_wait"
        group["attempts"] = attempts
        group["due_at"] = now_value + max(5, int(retry_seconds or 5))
        group["lease_owner"] = ""
        group["lease_until"] = 0
        group["last_error"] = " ".join(str(error or "").split())[:300]
        group["updated_at"] = now_value
        _prune_recent(state, now_value)
        _write_state(conn, state)
    return True


def recover_media_refresh_leases(*, now_epoch: float | None = None) -> int:
    """在取得跨进程消费者锁后，立即恢复前一进程遗留的 running 租约。"""
    now_value = float(time.time() if now_epoch is None else now_epoch)
    database = _database()
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = _read_state(conn)
        recovered = _recover_running_groups(state, now_value, force=True)
        pruned = _prune_recent(state, now_value)
        if recovered or pruned:
            _write_state(conn, state)
    return recovered


def recent_media_refresh_target_ids(
    provider: str, *, now_epoch: float | None = None,
) -> tuple[str, ...]:
    normalized_provider = _normalized_provider(provider)
    now_value = float(time.time() if now_epoch is None else now_epoch)
    database = _database()
    with database.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = _read_state(conn)
        before = len(state["recent"])
        _prune_recent(state, now_value)
        if len(state["recent"]) != before:
            _write_state(conn, state)
        prefix = f"{normalized_provider}:"
        return tuple(sorted(
            key[len(prefix):]
            for key in state["recent"]
            if str(key).startswith(prefix)
        ))


def next_media_refresh_due_in(*, now_epoch: float | None = None) -> float | None:
    now_value = float(time.time() if now_epoch is None else now_epoch)
    database = _database()
    with database.get_conn() as conn:
        state = _read_state(conn)
    due_values: list[float] = []
    for group in state["groups"].values():
        if not isinstance(group, dict):
            continue
        if str(group.get("status") or "") not in {"queued", "retry_wait"}:
            continue
        if not _normalized_paths(group.get("pending_paths")):
            continue
        try:
            due_values.append(float(group.get("due_at") or 0))
        except (TypeError, ValueError, OverflowError):
            due_values.append(0)
    if not due_values:
        return None
    return max(0.0, min(due_values) - now_value)


def media_refresh_queue_status(*, now_epoch: float | None = None) -> dict[str, int]:
    now_value = float(time.time() if now_epoch is None else now_epoch)
    database = _database()
    with database.get_conn() as conn:
        state = _read_state(conn)
    counts = {"queued": 0, "running": 0, "retry_wait": 0, "paths": 0, "recent": 0}
    for group in state["groups"].values():
        if not isinstance(group, dict):
            continue
        status = str(group.get("status") or "")
        if status in counts:
            counts[status] += 1
        counts["paths"] += len(_normalized_paths(group.get("pending_paths")))
        counts["paths"] += len(_normalized_paths(group.get("inflight_paths")))
    recent_count = 0
    for raw_expiry in state["recent"].values():
        try:
            expiry = float(raw_expiry or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if expiry > now_value:
            recent_count += 1
    counts["recent"] = recent_count
    return counts


def clear_media_refresh_queue() -> None:
    """仅供测试/维护显式清空刷新队列。"""
    database = _database()
    with database.get_conn() as conn:
        conn.execute("DELETE FROM settings_kv WHERE key=?", (_QUEUE_KEY,))
