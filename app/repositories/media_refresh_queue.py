"""持久媒体库刷新队列。

队列状态复用现有 ``settings_kv`` 的单行 JSON，并在 ``BEGIN IMMEDIATE``
事务内更新。这样无需给并行开发中的正式 schema 迁移增加耦合，同时仍能保证
多线程/多进程入队、领取和确认之间不会丢失变化路径。
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable
from typing import Any, Literal
from app import database as db

_QUEUE_KEY = "media_refresh_queue:v1"
_STATE_VERSION = 1
_MAX_RECENT_TARGETS = 4096


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
    except (TypeError, ValueError) as exc:
        raise RuntimeError("媒体库刷新队列状态损坏，已拒绝覆盖") from exc
    if not isinstance(state, dict) or int(state.get("version") or 0) != _STATE_VERSION:
        raise RuntimeError("媒体库刷新队列版本无效，已拒绝覆盖")
    if not isinstance(state.get("groups"), dict) or not isinstance(state.get("recent"), dict):
        raise RuntimeError("媒体库刷新队列结构无效，已拒绝覆盖")
    return state


def _write_state(conn, state: dict[str, Any]) -> None:
    payload = json.dumps(
        state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    db.kv_set_conn(conn, _QUEUE_KEY, payload)


def _normalized_provider(value: object) -> str:
    provider = str(value or "").strip().lower()
    if provider not in {"jellyfin", "emby"}:
        raise ValueError("媒体服务器类型无效")
    return provider


def _normalized_library_ids(values: Iterable[object] | None) -> tuple[str, ...]:
    return tuple(sorted(dict.fromkeys(
        str(item or "").strip()
        for item in (values or ())
        if str(item or "").strip()
    )))


def _normalized_paths(values: Iterable[object] | None) -> list[str]:
    return list(dict.fromkeys(
        str(item or "").strip()
        for item in (values or ())
        if str(item or "").strip()
    ))


def _normalized_binding(value: dict[str, str] | None) -> dict[str, str]:
    if not value:
        return {}
    binding = {key: (value.get(key) or "").strip() for key in ("id", "name")}
    if not any(binding.values()):
        raise ValueError("媒体库绑定不能为空")
    return binding


def _group_key(
    provider: str, allowed_library_ids: tuple[str, ...], binding: dict[str, str],
) -> str:
    parts: list[Any] = [provider, list(allowed_library_ids)]
    if binding:
        parts.append(binding)
    payload = json.dumps(
        parts,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


def _merge_paths(*groups: Iterable[object] | None) -> list[str]:
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
    paths: Iterable[object] | None,
    *,
    allowed_library_ids: Iterable[object] | None = (),
    library_binding: dict[str, str] | None = None,
    debounce_seconds: float = 20.0,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """合并一组变化路径；运行中到达的新路径只进入 pending，不覆盖 inflight。"""
    normalized_provider = _normalized_provider(provider)
    normalized_ids = _normalized_library_ids(allowed_library_ids)
    binding = _normalized_binding(library_binding)
    normalized_paths = _normalized_paths(paths)
    if not normalized_paths:
        return {"queued": 0, "group_key": "", "path_count": 0}
    now_value = float(time.time() if now_epoch is None else now_epoch)
    due_at = now_value + max(0.0, float(debounce_seconds or 0.0))
    key = _group_key(normalized_provider, normalized_ids, binding)
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = _read_state(conn)
        # 生产者只追加 pending，绝不依据时间回收 running；消费者可能仍在执行
        # 一个较慢的媒体服务器请求。崩溃恢复只允许持有跨进程消费者锁的一方执行。
        _prune_recent(state, now_value)
        group = state["groups"].get(key)
        if not isinstance(group, dict):
            group: dict[str, Any] = {
                "provider": normalized_provider,
                "allowed_library_ids": list(normalized_ids),
                "library_binding": binding,
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
            or _normalized_binding(group.get("library_binding")) != binding
        ):
            raise RuntimeError("媒体库刷新队列分组冲突")
        before = len(_normalized_paths(group.get("pending_paths")))
        waiting = group.get("waiting_paths", {})
        for path in normalized_paths:
            waiting.pop(path, None)
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
    safe_owner = (owner or "").strip()
    if not safe_owner:
        raise ValueError("媒体库刷新 worker owner 不能为空")
    now_value = float(time.time() if now_epoch is None else now_epoch)
    safe_limit = max(1, min((limit or 1), 64))
    with db.get_conn() as conn:
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
            group["lease_until"] = now_value + max(30, (lease_seconds or 300))
            group["lease_generation"] = generation
            group["updated_at"] = now_value
            claimed.append({
                "group_key": key,
                "provider": str(group.get("provider") or ""),
                "allowed_library_ids": list(group.get("allowed_library_ids") or []),
                "library_binding": dict(group.get("library_binding") or {}),
                "paths": pending,
                "attempts": max(0, int(group.get("attempts") or 0)),
                "lease_generation": generation,
            })
        if recovered or pruned or claimed:
            _write_state(conn, state)
    return claimed


def settle_media_refresh(
    group_key: str,
    *,
    owner: str,
    lease_generation: int,
    outcome: Literal["completed", "deferred", "failed"],
    delay_seconds: int = 0,
    error: object = "",
    refreshed_target_ids: Iterable[object] | None = (),
    acknowledged_paths: Iterable[object] | None = (),
    waiting_paths: Iterable[object] | None = (),
    waiting_reason: str = "",
    configuration_revision: str = "",
    recent_ttl_seconds: int = 90,
    now_epoch: float | None = None,
) -> bool:
    """逐路径结算成功、配置等待与可重试工作；保留租约期间新变化的 quiet window。"""
    if outcome not in {"completed", "deferred", "failed"}:
        raise ValueError("媒体库刷新结算结果无效")
    now_value = float(time.time() if now_epoch is None else now_epoch)
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = _read_state(conn)
        group = state["groups"].get(group_key or "")
        if not isinstance(group, dict):
            return False
        if (
            str(group.get("status") or "") != "running"
            or str(group.get("lease_owner") or "") != (owner or "")
            or int(group.get("lease_generation") or 0) != lease_generation
        ):
            return False
        provider = str(group.get("provider") or "")
        ttl = max(0, (recent_ttl_seconds or 0))
        if ttl:
            expiry = now_value + ttl
            for item_id in _normalized_paths(refreshed_target_ids):
                state["recent"][f"{provider}:{item_id}"] = expiry
        completed = outcome == "completed"
        acknowledged = set(_normalized_paths(acknowledged_paths))
        pending = _normalized_paths(group.get("pending_paths"))
        inflight = _normalized_paths(group.get("inflight_paths"))
        waiting = group.setdefault("waiting_paths", {})
        blocked = set(_normalized_paths(waiting_paths)) & set(inflight) - acknowledged
        for path in blocked - set(pending):
            waiting[path] = {
                "reason": waiting_reason,
                "configuration_revision": configuration_revision,
            }
        remaining = [] if completed else [
            path for path in inflight if path not in acknowledged and path not in blocked
        ]
        group["pending_paths"] = _merge_paths(remaining, pending)
        if not group["pending_paths"] and not waiting:
            state["groups"].pop(group_key, None)
        else:
            if completed:
                group["attempts"] = 0
            else:
                if outcome == "failed":
                    group["attempts"] = max(0, int(group.get("attempts") or 0)) + 1
                # pending 的时间已由新变化入队时确定，旧批次退避不能推迟或提前它。
                # 没有新变化时才按失败/去重窗口重试；目标去重仍由 recent 独立约束。
                if not pending:
                    minimum = 5 if outcome == "failed" else 1
                    group["due_at"] = now_value + max(minimum, (delay_seconds or minimum))
            group.update(
                inflight_paths=[],
                status=("queued" if completed else "retry_wait") if group["pending_paths"] else "waiting_configuration",
                lease_owner="",
                lease_until=0,
                last_error="" if completed else " ".join(str(error or "").split())[:300],
                updated_at=now_value,
            )
        _prune_recent(state, now_value)
        _write_state(conn, state)
    return True


def recover_media_refresh_leases(*, now_epoch: float | None = None) -> int:
    """在取得跨进程消费者锁后，立即恢复前一进程遗留的 running 租约。"""
    now_value = float(time.time() if now_epoch is None else now_epoch)
    with db.get_conn() as conn:
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
    with db.get_conn() as conn:
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
    with db.get_conn() as conn:
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
    with db.get_conn() as conn:
        state = _read_state(conn)
    counts = {"queued": 0, "running": 0, "retry_wait": 0, "waiting_configuration": 0,
              "waiting_paths": 0, "paths": 0, "recent": 0}
    for group in state["groups"].values():
        if not isinstance(group, dict):
            continue
        status = str(group.get("status") or "")
        if status in {"queued", "running", "retry_wait"}:
            counts[status] += 1
        waiting = len(group.get("waiting_paths", {}))
        counts["waiting_configuration"] += bool(waiting)
        counts["waiting_paths"] += waiting
        counts["paths"] += waiting
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


def _waiting_token(group: dict[str, Any]) -> str:
    snapshot = [group.get("created_at"), group.get("waiting_paths", {}), group.get("lease_generation", 0)]
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


def list_waiting_media_refreshes() -> list[dict[str, Any]]:
    """查看需要配置处理的刷新意图，不查询媒体服务器。"""
    with db.get_conn() as conn:
        state = _read_state(conn)
    return [{
        "group_key": key, "provider": group["provider"], "token": _waiting_token(group),
        "path_count": len(group["waiting_paths"]), "paths": list(group["waiting_paths"]),
        "reason": "；".join(dict.fromkeys(item["reason"] for item in group["waiting_paths"].values())),
    } for key, group in state["groups"].items() if group.get("waiting_paths")]


def _resume_waiting(group: dict[str, Any], paths: list[str], now_value: float) -> None:
    if not group.get("pending_paths"):
        group["due_at"] = now_value
    group["pending_paths"] = _merge_paths(group.get("pending_paths"), paths)
    for path in paths:
        group["waiting_paths"].pop(path)
    if group.get("status") != "running":
        group["status"] = "queued"
    group.update(attempts=0, last_error="", updated_at=now_value)


def reconcile_media_refresh_configuration(
    revisions: dict[str, str], *, now_epoch: float | None = None,
) -> int:
    """配置变化只唤醒相关等待项；摘要随意图持久化，重启也不会无故重新轮询。"""
    now_value = float(time.time() if now_epoch is None else now_epoch)
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = _read_state(conn)
        resumed = 0
        for group in state["groups"].values():
            provider = group.get("provider")
            if provider not in revisions:
                continue
            paths = [path for path, item in group.get("waiting_paths", {}).items()
                     if item["configuration_revision"] != revisions[provider]]
            if paths:
                _resume_waiting(group, paths, now_value)
                resumed += len(paths)
        if resumed:
            _write_state(conn, state)
    return resumed


def resolve_waiting_media_refreshes(
    records: list[dict[str, Any]], action: Literal["retry", "clear"], *,
    now_epoch: float | None = None,
) -> dict[str, int]:
    """核对页面快照后重新排队或丢弃所选等待意图；不操作文件或运行中路径。"""
    if action not in {"retry", "clear"} or not records:
        raise ValueError("请选择待处理刷新记录和有效操作")
    now_value = float(time.time() if now_epoch is None else now_epoch)
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = _read_state(conn)
        selected = {}
        for item in records:
            key = item.get("group_key", "")
            group = state["groups"].get(key)
            if not group or not group.get("waiting_paths") or item.get("token") != _waiting_token(group):
                raise ValueError("刷新记录已变化，请更新列表后重新选择")
            raw_paths = item.get("paths", list(group["waiting_paths"]))
            if not isinstance(raw_paths, list) or not raw_paths or any(
                not isinstance(path, str) or path not in group["waiting_paths"] for path in raw_paths
            ):
                raise ValueError("所选刷新路径无效或已变化，请更新列表后重新选择")
            if key in selected:
                raise ValueError("刷新记录不能重复选择")
            selected[key] = (group, list(dict.fromkeys(raw_paths)))
        count = 0
        for key, (group, paths) in selected.items():
            count += len(paths)
            if action == "retry":
                _resume_waiting(group, paths, now_value)
            else:
                for path in paths:
                    group["waiting_paths"].pop(path)
                if not group["waiting_paths"] and not group.get("pending_paths") and not group.get("inflight_paths"):
                    state["groups"].pop(key)
        _write_state(conn, state)
    return {"processed_paths": count, "processed_groups": len(selected)}


def clear_media_refresh_queue() -> None:
    """仅供测试/维护显式清空刷新队列。"""
    with db.get_conn() as conn:
        conn.execute("DELETE FROM settings_kv WHERE key=?", (_QUEUE_KEY,))
