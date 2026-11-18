"""持久媒体库刷新队列。

队列状态复用现有 ``settings_kv`` 的单行 JSON，并在 ``BEGIN IMMEDIATE``
事务内更新。这样无需给并行开发中的正式 schema 迁移增加耦合，同时仍能保证
多线程/多进程入队、领取和确认之间不会丢失变化路径。
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Mapping
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any

from app import database as db
from app.modules.process_lock import CrossProcessLock

if TYPE_CHECKING:
    from app.modules.media_server_profiles import MediaRefreshIntent

_QUEUE_KEY = "media_refresh_queue:v1"
_STATE_VERSION = 1
_MAX_RECENT_TARGETS = 4096
LEGACY_REFRESH_UNVERIFIED = "历史刷新未记录原媒体服务器，无法核验是否完成，请手动刷新"
media_refresh_projection_lock = CrossProcessLock("media-refresh-projection")


def _empty_state() -> dict[str, Any]:
    return {"version": _STATE_VERSION, "groups": {}, "recent": {}, "events": {}, "origins": {}}


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
    state.setdefault("events", {})
    state.setdefault("origins", {})
    # 旧等待/退避状态归一为一次待处理工作；不保留第二套恢复状态机。
    for group in state["groups"].values():
        if not isinstance(group, dict):
            raise RuntimeError("媒体库刷新队列分组无效，已拒绝覆盖")
        waiting = group.pop("waiting_paths", {})
        group["pending_paths"] = _merge_paths(group.get("pending_paths"), waiting)
        if group.get("status") in {"waiting_configuration", "retry_wait"}:
            group["status"] = "queued"
            group["due_at"] = 0
        group.pop("attempts", None)
        group.pop("last_error", None)
    return state


def _write_state(conn, state: dict[str, Any]) -> None:
    payload = json.dumps(
        state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    db.kv_set_conn(conn, _QUEUE_KEY, payload)


def _live_event_tokens(conn, entries: Iterable[dict[str, Any]]) -> set[str]:
    """一次 SQL 批量查原事件，按 outbox 的 (path,allow_emby) 主键定位。"""
    keys = [[entry["path"], int(entry.get("allow_emby", True)), entry["event_token"]] for entry in entries]
    if not keys:
        return set()
    return {str(row["event_token"]) for row in conn.execute(
        "SELECT o.event_token FROM json_each(?) j JOIN strm_refresh_outbox o "
        "ON o.path=json_extract(j.value,'$[0]') AND o.allow_emby=json_extract(j.value,'$[1]') "
        "AND o.event_token=json_extract(j.value,'$[2]')",
        (json.dumps(keys, ensure_ascii=False),),
    )}


def _outbox_references(conn, state: dict[str, Any]) -> set[str]:
    return _live_event_tokens(conn, (
        {"event_token": token, "path": event["path"], "allow_emby": event.get("allow_emby", True)}
        for token, event in state["events"].items() if event.get("source")
    ))


def _collect_results(conn, state: dict[str, Any]) -> bool:
    """队列只保留未完成交接/投影；历史终态已经属于原 task_run。"""
    before = (len(state["events"]), len(state["origins"]))
    referenced = {
        token for group in state["groups"].values()
        for field in ("pending_events", "inflight_events")
        for tokens in group.get(field, {}).values() for token in tokens
    }
    live_outbox = _outbox_references(conn, state)
    busy = referenced | live_outbox
    superseded = False
    for token in state["events"].keys() - busy:
        event = state["events"][token]
        outcomes = event["providers"]
        pending = [provider for provider, value in outcomes.items() if value["status"] == "pending"]
        if event.get("source") and (pending or not outcomes):
            for provider in pending or ["媒体库"]:
                outcomes[provider] = {"status": "failed", "error": "原刷新意图已被该路径的后续变化取代"}
            for key in event["origins"]:
                state["origins"][key]["revision"] += 1
            superseded = True
    terminal = {
        token for token, event in state["events"].items()
        if event["providers"] and all(value["status"] in {"success", "failed", "skipped"} for value in event["providers"].values())
    }
    for key, origin in list(state["origins"].items()):
        tokens = set(origin["events"])
        if ("completion" in origin and origin["revision"] == origin["projected_revision"]
                and tokens <= terminal and not tokens.intersection(busy)):
            state["origins"].pop(key)
    retained = {token for origin in state["origins"].values() for token in origin["events"]}
    for token in terminal - retained - referenced - live_outbox:
        state["events"].pop(token)
    return superseded or before != (len(state["events"]), len(state["origins"]))


def collect_media_refresh_results() -> None:
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = _read_state(conn)
        if _collect_results(conn, state):
            _write_state(conn, state)


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
    provider: str, intent: MediaRefreshIntent,
    allowed_library_ids: tuple[str, ...], binding: dict[str, str],
) -> str:
    payload = json.dumps(
        [provider, intent, allowed_library_ids, binding],
        ensure_ascii=False,
        sort_keys=True,
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
        if expiry <= now_epoch or str(key).partition(":")[0] in {"jellyfin", "emby"}:
            recent.pop(key, None)
            continue
        alive.append((str(key), expiry))
    if len(alive) <= _MAX_RECENT_TARGETS:
        return before - len(recent)
    for key, _expiry in sorted(alive, key=lambda item: item[1])[:-_MAX_RECENT_TARGETS]:
        recent.pop(key, None)
    return before - len(recent)


def _pending_deadlines(group: dict[str, Any]) -> dict[str, float]:
    """旧记录沿用批次 quiet window；延期路径使用持久化的独立截止时间。"""
    try:
        due_at = float(group.get("due_at") or 0)
    except (TypeError, ValueError, OverflowError):
        due_at = 0
    overrides = group.get("path_due_at") or {}
    return {
        path: float(overrides.get(path, due_at))
        for path in _normalized_paths(group.get("pending_paths"))
    }


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
        for path, tokens in group.pop("inflight_events", {}).items():
            pending = group.setdefault("pending_events", {})
            pending[path] = _merge_paths(tokens, pending.get(path))
        group["inflight_paths"] = []
        group["status"] = "queued"
        group["lease_owner"] = ""
        group["lease_until"] = 0
        # 沿用重启立即接管普通 pending 的行为；独立 expiry 不受批次 due_at 影响。
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
    source_entries: Iterable[dict[str, Any]] | None = None,
    intent: MediaRefreshIntent | None = None,
) -> dict[str, Any]:
    """合并一组变化路径；运行中到达的新路径只进入 pending，不覆盖 inflight。"""
    normalized_provider = _normalized_provider(provider)
    normalized_ids = _normalized_library_ids(allowed_library_ids)
    binding = _normalized_binding(library_binding)
    normalized_paths = _normalized_paths(paths)
    if not normalized_paths:
        return {"queued": 0, "group_key": "", "path_count": 0}
    if intent is None:
        from app.modules.media_server_profiles import configured_media_refresh_targets

        intent = configured_media_refresh_targets()[normalized_provider].intent()
    now_value = float(time.time() if now_epoch is None else now_epoch)
    due_at = now_value + max(0.0, float(debounce_seconds or 0.0))
    key = _group_key(normalized_provider, intent, normalized_ids, binding)
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = _read_state(conn)
        entries = list(source_entries or ())
        live_tokens = _live_event_tokens(conn, entries) if source_entries is not None else set()
        events_by_path: dict[str, list[str]] = {}
        path_set = set(normalized_paths)
        touched_origins = set()
        for entry in entries:
            path, token = str(entry["path"]), str(entry["event_token"])
            if path not in path_set:
                continue
            if token not in live_tokens or (entry.get("refresh_targets") or {}).get(normalized_provider) != intent:
                continue
            event = state["events"].setdefault(token, {
                "path": path, "providers": {}, "source": source_entries is not None,
                "allow_emby": bool(entry.get("allow_emby", True)), "origins": [],
            })
            # outbox ACK 失败后的幂等交接不能重放已接受/失败的刷新。
            previous = event["providers"].get(normalized_provider, {})
            if previous.get("status") in {"success", "failed", "skipped"}:
                continue
            if previous.get("status") != "queued":
                touched_origins.update(event.get("origins", []))
                event["providers"][normalized_provider] = {"status": "queued"}
            events_by_path.setdefault(path, []).append(token)
        for origin_key in touched_origins:
            state["origins"][origin_key]["revision"] += 1
        if source_entries is not None:
            normalized_paths = list(events_by_path)
        if not normalized_paths:
            return {"queued": 0, "group_key": key, "path_count": 0}
        # 生产者只追加 pending，绝不依据时间回收 running；消费者可能仍在执行
        # 一个较慢的媒体服务器请求。崩溃恢复只允许持有跨进程消费者锁的一方执行。
        _prune_recent(state, now_value)
        group = state["groups"].get(key)
        if not isinstance(group, dict):
            group: dict[str, Any] = {
                "provider": normalized_provider,
                "intent": intent,
                "allowed_library_ids": list(normalized_ids),
                "library_binding": binding,
                "pending_paths": [],
                "inflight_paths": [],
                "status": "queued",
                "due_at": due_at,
                "lease_owner": "",
                "lease_until": 0,
                "lease_generation": 0,
                "created_at": now_value,
                "updated_at": now_value,
            }
            state["groups"][key] = group
        elif (
            str(group.get("provider") or "") != normalized_provider
            or group.get("intent") != intent
            or _normalized_library_ids(group.get("allowed_library_ids")) != normalized_ids
            or _normalized_binding(group.get("library_binding")) != binding
        ):
            raise RuntimeError("媒体库刷新队列分组冲突")
        before = len(_normalized_paths(group.get("pending_paths")))
        group["pending_paths"] = _merge_paths(
            group.get("pending_paths"), normalized_paths
        )
        pending_events = group.setdefault("pending_events", {})
        inflight_events = group.get("inflight_events", {})
        for path, tokens in events_by_path.items():
            # 同一事件已被消费者领取时，重交接只收尾原 outbox。
            tokens = [token for token in tokens if token not in inflight_events.get(path, [])]
            pending_events[path] = _merge_paths(pending_events.get(path), tokens)
        group["pending_paths"] = [
            path for path in group["pending_paths"]
            if path not in events_by_path or pending_events.get(path)
        ]
        deadlines = group.get("path_due_at") or {}
        for path in normalized_paths:
            if path in deadlines:
                deadlines[path] = max(float(deadlines[path]), due_at)
        group["path_due_at"] = deadlines
        if str(group.get("status") or "") != "running":
            group["status"] = "queued"
        # 新变化共享 quiet window；其它已延期路径的原 expiry 不随之重置。
        group["due_at"] = due_at
        group["updated_at"] = now_value
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
            if status != "queued":
                continue
            deadlines = _pending_deadlines(group)
            if not deadlines:
                continue
            due_at = min(deadlines.values())
            if not force and due_at > now_value:
                continue
            candidates.append((due_at, str(key), group))
        claimed: list[dict[str, Any]] = []
        for _due_at, key, group in sorted(candidates)[:safe_limit]:
            deadlines = _pending_deadlines(group)
            pending = [path for path, due_at in deadlines.items() if force or due_at <= now_value]
            ready = set(pending)
            generation = max(int(state.get("lease_sequence") or 0), int(group.get("lease_generation") or 0)) + 1
            state["lease_sequence"] = generation
            group["pending_paths"] = [path for path in deadlines if path not in ready]
            group["path_due_at"] = {
                path: due_at for path, due_at in (group.get("path_due_at") or {}).items()
                if path not in ready
            }
            group["inflight_paths"] = pending
            pending_events = group.setdefault("pending_events", {})
            group["inflight_events"] = {path: pending_events.pop(path, []) for path in pending}
            group["status"] = "running"
            group["lease_owner"] = safe_owner
            group["lease_until"] = now_value + max(30, (lease_seconds or 300))
            group["lease_generation"] = generation
            group["updated_at"] = now_value
            claimed.append({
                "group_key": key,
                "provider": str(group.get("provider") or ""),
                "intent": group.get("intent"),
                "allowed_library_ids": list(group.get("allowed_library_ids") or []),
                "library_binding": dict(group.get("library_binding") or {}),
                "paths": pending,
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
    refreshed_target_ids: Iterable[object] | None = (),
    deferred_targets: Mapping[str, str] | None = None,
    recent_ttl_seconds: int = 90,
    now_epoch: float | None = None,
    outcome: dict[str, Any] | None = None,
) -> bool:
    """按延期目标原 expiry 结算；保留新变化的 quiet window 与未到期路径。"""
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
        upstream = (group.get("intent") or {}).get("upstream")
        ttl = max(0, (recent_ttl_seconds or 0))
        if ttl and upstream:
            expiry = now_value + ttl
            for item_id in _normalized_paths(refreshed_target_ids):
                state["recent"][f"{upstream}:{item_id}"] = expiry
        deferred = dict(deferred_targets or {})
        inflight = _normalized_paths(group.get("inflight_paths"))
        if not deferred.keys() <= set(inflight):
            raise ValueError("延后刷新路径不属于本轮任务")
        result = outcome or {"ok": True}
        succeeded = set(result.get("succeeded_paths") or ())
        failed = bool(result.get("retryable") or result.get("unmatched_paths") or (
            not result.get("ok") and not deferred
        ))
        inflight_events = group.pop("inflight_events", {})
        touched_origins = set()
        for path in inflight:
            status = "queued" if path in deferred else "success" if path in succeeded or not failed else "failed"
            for token in inflight_events.get(path, []):
                event = state["events"][token]
                touched_origins.update(event.get("origins", []))
                event["providers"][provider] = {
                    "status": status, "finished_at": now_value if status != "queued" else None,
                    "error": str(result.get("fallback") or "刷新失败，请手动刷新媒体库") if status == "failed" else "",
                    "scope": str(result.get("scope") or ""),
                }
        for key in touched_origins:
            state["origins"][key]["revision"] += 1
        pending = _normalized_paths(group.get("pending_paths"))
        pending_deadlines = _pending_deadlines(group)
        deadlines = dict(group.get("path_due_at") or {})
        for path, target in deferred.items():
            expiry = float(state["recent"].get(f"{upstream}:{target}") or now_value)
            deadlines[path] = max(now_value, expiry, pending_deadlines.get(path, 0))
        group["pending_paths"] = _merge_paths((path for path in inflight if path in deferred), pending)
        pending_events = group.setdefault("pending_events", {})
        for path in deferred:
            pending_events[path] = _merge_paths(inflight_events.get(path), pending_events.get(path))
        group["path_due_at"] = deadlines
        if not group["pending_paths"]:
            state["groups"].pop(group_key, None)
        else:
            group.update(inflight_paths=[], status="queued", lease_owner="", lease_until=0, updated_at=now_value)
        _prune_recent(state, now_value)
        _collect_results(conn, state)
        _write_state(conn, state)
    return True


def register_media_refresh_handoff(
    entries: list[dict[str, Any]], *,
    run_id: int | None = None, options: dict[str, Any] | None = None,
    completion: dict[str, Any] | None = None,
) -> None:
    """在交接前冻结原事件/运行；后到的初始化不能覆盖消费者终态。"""
    from app.repositories.strm_request_ownership import refresh_work_key

    with ExitStack() as stack:
        media_refresh_projection_lock.acquire()
        stack.callback(media_refresh_projection_lock.release)
        conn = stack.enter_context(db.get_conn())
        conn.execute("BEGIN IMMEDIATE")
        state = _read_state(conn)
        if run_id is not None:
            origin = state["origins"].setdefault(str(run_id), {
                "run_id": run_id, "events": [], "options": options or {}, "revision": 0, "projected_revision": -1,
            })
            if options is not None and origin["options"] != options:
                origin["options"] = options
                origin["revision"] += 1
            if completion is not None:
                origin["completion"] = completion
                origin["revision"] += 1
                row = conn.execute("SELECT result FROM task_runs WHERE id=?", (run_id,)).fetchone()
                if row is not None:
                    result = json.loads(row["result"] or "{}")
                    result["media_refresh_origin"] = {"options": origin["options"], "completion": completion}
                    conn.execute("UPDATE task_runs SET result=? WHERE id=?", (json.dumps(result, ensure_ascii=False), run_id))
        origin_tokens = {key: set(origin["events"]) for key, origin in state["origins"].items()}
        live_tokens = _live_event_tokens(conn, entries)
        work_keys = [[refresh_work_key(str(entry["path"]), bool(entry.get("allow_emby", True))), entry["event_token"]]
                     for entry in entries if entry["event_token"] in live_tokens]
        owners_by_token: dict[str, set[str]] = {}
        if work_keys:
            rows = conn.execute(
                "SELECT DISTINCT json_extract(j.value,'$[1]') AS token,r.strm_run_id "
                "FROM json_each(?) j CROSS JOIN strm_request_work w "
                "ON w.kind='refresh' AND w.work_key=json_extract(j.value,'$[0]') "
                "CROSS JOIN download_requests r ON r.id=w.request_id AND r.strm_generation=w.generation "
                "AND COALESCE(r.organize_task_id,'')=w.organize_task_id WHERE r.strm_run_id IS NOT NULL",
                (json.dumps(work_keys),),
            )
            for row in rows:
                owners_by_token.setdefault(str(row["token"]), set()).add(str(row["strm_run_id"]))
        for entry in entries:
            token, path = str(entry["event_token"]), str(entry["path"])
            if token not in live_tokens:
                continue
            event = state["events"].setdefault(token, {
                "path": path, "providers": {}, "source": True,
                "allow_emby": bool(entry.get("allow_emby", True)), "origins": [],
            })
            targets = entry.get("refresh_targets")
            outcomes = event["providers"]
            if targets is None:
                # 历史 outbox 无法证明原目标。已排队事件继续由原租约结算。
                for provider in tuple(outcomes) or ("媒体库",):
                    if outcomes.get(provider, {}).get("status", "pending") == "pending":
                        outcomes[provider] = {"status": "failed", "error": LEGACY_REFRESH_UNVERIFIED}
                        for key in event["origins"]:
                            state["origins"][key]["revision"] += 1
            elif targets:
                for provider in targets:
                    outcomes.setdefault(provider, {"status": "pending"})
            else:
                outcomes.setdefault("媒体库", {"status": "skipped"})
            # 恢复 worker 在 ACK 删除归属前读取原运行；绝不借用新的默认 chat。
            origins = set(owners_by_token.get(token, ()))
            if run_id is not None:
                origins.add(str(run_id))
            for key in origins:
                origin = state["origins"].get(key)
                if origin is None:
                    row = conn.execute("SELECT result FROM task_runs WHERE id=?", (int(key),)).fetchone()
                    result = json.loads(row["result"] or "{}") if row else {}
                    archived = result.get("media_refresh_origin")
                    if archived:
                        origin = {"run_id": int(key), "events": [], "revision": 0, "projected_revision": -1,
                                  **archived, "baseline": result.get("media_refresh_results", {}).get("providers", {})}
                        state["origins"][key] = origin
                        origin_tokens[key] = set()
                if origin is not None and token not in origin_tokens[key]:
                    origin["events"].append(token)
                    origin_tokens[key].add(token)
                    event["origins"].append(key)
                    origin["revision"] += 1
        _write_state(conn, state)


def pending_media_refresh_projections() -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        state = _read_state(conn)
        outstanding = _outbox_references(conn, state)
    origins = state["origins"]
    pending = []
    for origin in origins.values():
        if "completion" not in origin or origin["revision"] == origin["projected_revision"]:
            continue
        snapshot = {**origin, "results": {token: state["events"][token] for token in origin["events"]},
                    "outbox_pending": bool(outstanding.intersection(origin["events"]))}
        pending.append(snapshot)
    return pending


def acknowledge_media_refresh_projections(revisions: list[tuple[int, int]]) -> None:
    if not revisions:
        return
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = _read_state(conn)
        for run_id, revision in revisions:
            origin = state["origins"].get(str(run_id))
            if origin is not None and origin["revision"] == revision:
                origin["projected_revision"] = revision
        _collect_results(conn, state)
        _write_state(conn, state)


def _recover_legacy_run_receipts(conn, state: dict[str, Any]) -> bool:
    if state.get("legacy_run_receipts_recovered"):
        return False
    # 旧队列按 provider 合并路径且 ACK 后失去运行关联。不能用当前上游重放，
    # 也不能把旧历史的“已排队”当作已执行或声称原请求一定没有发生。
    rows = conn.execute(
        "SELECT id,result FROM task_runs WHERE task_name='strm_sync' AND status!='running' "
        "AND json_type(CASE WHEN json_valid(result) THEN result ELSE '{}' END,'$.media_refresh')='object'"
    )
    for row in rows:
        run_id = row["id"]
        key = str(run_id)
        result = json.loads(row["result"])
        if key in state["origins"] or "media_refresh_origin" in result or "media_refresh_results" in result:
            continue
        refresh = result["media_refresh"]
        providers = {
            provider: {"status": "failed", "error": LEGACY_REFRESH_UNVERIFIED, "previous_status": status}
            for provider, status in refresh.items()
            if status in ("queued", "pending") or (status in ("failed", False) and result.get("refresh_pending"))
        }
        if not providers:
            continue
        token = f"legacy-run:{run_id}"
        state["events"][token] = {"path": "", "providers": providers, "source": False, "origins": [key]}
        state["origins"][key] = {
            "run_id": run_id, "options": {}, "events": [token], "revision": 0, "projected_revision": -1,
            "baseline": {provider: status for provider, status in refresh.items() if provider not in providers},
        }
    state["legacy_run_receipts_recovered"] = True
    return True


def recover_media_refresh_leases(*, now_epoch: float | None = None) -> int:
    """取得消费者锁后恢复遗留租约与待投影回执。"""
    now_value = float(time.time() if now_epoch is None else now_epoch)
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = _read_state(conn)
        recovered = _recover_running_groups(state, now_value, force=True)
        legacy = _recover_legacy_run_receipts(conn, state)
        for origin in state["origins"].values():
            if "completion" in origin:
                continue
            row = conn.execute("SELECT status,result FROM task_runs WHERE id=?", (origin["run_id"],)).fetchone()
            if row is not None and row["status"] != "running":
                result = json.loads(row["result"] or "{}")
                partial = bool(result.get("strm_partial") or row["status"] in {"failed", "skipped"})
                origin["completion"] = {"strm_status": "部分完成" if partial else "完成", "partial": partial,
                                        "error": "STRM 同步未全部完成，请查看运行记录" if partial else ""}
                recovered += 1
        pruned = _prune_recent(state, now_value)
        if recovered or pruned or legacy:
            _write_state(conn, state)
    return recovered


def recent_media_refresh_target_ids(
    provider: str, *, upstream: str | None = None, now_epoch: float | None = None,
) -> tuple[str, ...]:
    normalized_provider = _normalized_provider(provider)
    if upstream is None:
        from app.modules.media_server_profiles import configured_media_refresh_targets

        upstream = configured_media_refresh_targets()[normalized_provider].intent()["upstream"]
    now_value = float(time.time() if now_epoch is None else now_epoch)
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = _read_state(conn)
        before = len(state["recent"])
        _prune_recent(state, now_value)
        if len(state["recent"]) != before:
            _write_state(conn, state)
        prefix = f"{upstream}:"
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
        if str(group.get("status") or "") != "queued":
            continue
        due_values.extend(_pending_deadlines(group).values())
    if not due_values:
        return None
    return max(0.0, min(due_values) - now_value)


def media_refresh_queue_status(*, now_epoch: float | None = None) -> dict[str, int]:
    now_value = float(time.time() if now_epoch is None else now_epoch)
    with db.get_conn() as conn:
        state = _read_state(conn)
    counts = {"queued": 0, "running": 0, "paths": 0, "recent": 0}
    for group in state["groups"].values():
        if not isinstance(group, dict):
            continue
        status = str(group.get("status") or "")
        if status in {"queued", "running"}:
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
    with db.get_conn() as conn:
        conn.execute("DELETE FROM settings_kv WHERE key=?", (_QUEUE_KEY,))
