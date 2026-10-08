"""会话范围的 Agent 回合与后台任务协作停止。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from app.agent.effect_completion import current_session_effect_trackers
from app.agent.kernel.session_guard import session_scope_guard
from app.agent.kernel.state import SessionBusyError

_AGENT_JOB_TYPE = "library_episode_audit"
_AGENT_JOB_ACTIVE = frozenset({"pending", "running", "retry_wait"})
_AGENT_JOB_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
_ORGANIZE_ACTIVE = frozenset({"queued", "running", "stopping"})
_ORGANIZE_TERMINAL = frozenset(
    {"completed", "partial", "failed", "cancelled", "manual_review", "stopped"}
)
logger = logging.getLogger(__name__)

_TRACKER_TERMINAL = {
    "guangya_operation": _ORGANIZE_TERMINAL,
    "guangya_task": frozenset({"completed", "failed"}),
    "guangya_organize_task": _ORGANIZE_TERMINAL,
    "local_media_task": frozenset({"completed", "failed", "manual_review"}),
    "local_media_scan": frozenset(
        {"completed", "partial", "failed", "requires_manual"}
    ),
    "agent_job": frozenset(
        {"updates_available", "up_to_date", "inconclusive", "cancelled", "failed", "succeeded"}
    ),
    "strm_run": frozenset({"completed", "partial", "failed"}),
    "library_patrol": frozenset(
        {"updates_available", "up_to_date", "inconclusive", "not_configured", "unavailable", "failed"}
    ),
}


def _value(row: Any, key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = default
    return default if value is None else value


def _is_active_tracker(item: Mapping[str, Any]) -> bool:
    if item.get("state") == "terminal" or item.get("delivered") is True:
        return False
    tracker = item.get("tracker")
    if not isinstance(tracker, Mapping):
        return False
    kind = str(tracker.get("kind") or "")
    status = str(item.get("last_status") or "").strip().casefold()
    return status not in _TRACKER_TERMINAL.get(
        kind, frozenset({"completed", "partial", "failed", "cancelled", "stopped"})
    )


def _cancel_organize_operation(owner: str, operation_ref: str) -> str:
    """按公开稳定编号和 owner CAS 取消持久整理操作。"""
    from app.modules.organize_tasks import get_organize_manager
    from app.repositories.organize_operation_jobs import (
        get_organize_operation_job_for_owner,
        organize_operation_job_id_from_public_ref,
        organize_operation_public_ref,
        request_cancel_organize_operation_job,
    )

    reference = str(operation_ref or "").strip().upper()
    try:
        job_id = organize_operation_job_id_from_public_ref(reference)
        row = get_organize_operation_job_for_owner(job_id, owner)
    except (TypeError, ValueError):
        return "uncancellable"
    if (
        row is None
        or str(_value(row, "job_id", "")) != job_id
        or organize_operation_public_ref(job_id) != reference
    ):
        return "uncancellable"

    updated, outcome = request_cancel_organize_operation_job(
        job_id, owner=owner,
        expected_lease_generation=int(_value(row, "lease_generation", 0) or 0),
    )
    if updated is None:
        return "uncancellable"
    if outcome == "uncancellable":
        return "uncancellable"
    if outcome == "terminal":
        return "already_terminal"
    if outcome == "stale":
        status = str(_value(updated, "status", "")).strip().casefold()
        return "already_terminal" if status in _ORGANIZE_TERMINAL else "uncancellable"
    if outcome not in {"requested", "cancelled"}:
        return "uncancellable"

    manager = get_organize_manager()
    wakeup = getattr(manager, "_operation_queue_wakeup", None)
    if wakeup is not None:
        wakeup.set()
    return "cancel_requested" if outcome == "requested" else "cancelled"


def _cancel_agent_job(owner: str, task_id: str) -> str:
    """按 owner + 固定 job_id 请求取消，不触碰队列中的其它任务。"""
    from app.modules.agent_jobs_scheduler import get_agent_jobs_scheduler
    from app.repositories.agent_jobs import cancel_agent_job, get_agent_job

    try:
        row = get_agent_job(owner=owner, job_id=task_id)
    except (TypeError, ValueError):
        return "uncancellable"
    if row is None or str(_value(row, "job_id", "")) != task_id:
        return "uncancellable"
    if str(_value(row, "job_type", "")) != _AGENT_JOB_TYPE:
        return "uncancellable"
    status = str(_value(row, "status", "")).strip().casefold()
    if status in _AGENT_JOB_TERMINAL:
        return "already_terminal"
    if status not in _AGENT_JOB_ACTIVE:
        return "uncancellable"

    updated, outcome = cancel_agent_job(
        owner=owner, job_id=task_id, job_type=_AGENT_JOB_TYPE
    )
    if updated is None or str(_value(updated, "job_id", "")) != task_id:
        return "uncancellable"
    if outcome == "terminal":
        return "already_terminal"
    if outcome not in {"requested", "cancelled"}:
        return "uncancellable"
    try:
        get_agent_jobs_scheduler().wake()
    except Exception as exc:  # noqa: BLE001 - 取消标记已落盘，唤醒失败不改写结果
        logger.warning("Agent 后台任务取消已落盘但唤醒失败 type=%s", type(exc).__name__)
    return "cancel_requested" if outcome == "requested" else "cancelled"


def _cancel_organize_task(task_id: str) -> str:
    """复用整理管理器的 task-id CAS 与原子写阶段保护。"""
    from app.modules.organize_tasks import get_organize_manager

    manager = get_organize_manager()
    snapshot = manager.task_status()
    if str(snapshot.get("id") or "").strip() != task_id:
        previous = manager.task_result(task_id)
        previous_status = str((previous or {}).get("status") or "").strip().casefold()
        return "already_terminal" if previous_status in _ORGANIZE_TERMINAL else "uncancellable"

    status = str(snapshot.get("status") or "").strip().casefold()
    if status in _ORGANIZE_TERMINAL:
        return "already_terminal"
    if status not in _ORGANIZE_ACTIVE:
        return "uncancellable"
    if snapshot.get("stoppable") is False:
        return "critical_pending"

    result = manager.stop(expected_task_id=task_id)
    if result.get("ok") is True:
        return "cancel_requested"
    if "不可中断" in str(result.get("error") or ""):
        return "critical_pending"
    return "uncancellable"


async def _cancel_background_trackers(
    owner: str, session_id: str, store: Any
) -> list[dict[str, str]]:
    trackers = await current_session_effect_trackers(
        store, owner=owner, session_id=session_id
    )
    results: list[dict[str, str]] = []
    for item in trackers:
        if not _is_active_tracker(item):
            continue
        tracker = item["tracker"]
        kind = str(tracker.get("kind") or "")
        value = tracker.get("value")
        value = value if isinstance(value, Mapping) else {}
        task_id = str(value.get("job_id" if kind == "agent_job" else "task_id") or "").strip()
        if kind == "agent_job" and task_id:
            outcome = await asyncio.to_thread(_cancel_agent_job, owner, task_id)
        elif kind == "guangya_organize_task" and task_id:
            outcome = await asyncio.to_thread(_cancel_organize_task, task_id)
        elif kind == "guangya_operation" and value.get("operation_ref"):
            outcome = await asyncio.to_thread(
                _cancel_organize_operation, owner, str(value["operation_ref"])
            )
        else:
            # 云端已受理任务及没有 owner/session 级取消 API 的任务仍由原 tracker 跟踪。
            outcome = "uncancellable"
        results.append(
            {
                "plan_id": str(item.get("plan_id") or ""),
                "kind": kind,
                "status": outcome,
            }
        )
    return results


async def _request_stop_generation(
    store: Any, *, owner: str, session_id: str, generation: int,
) -> bool:
    """仅在捕获的回合仍拥有当前状态代次时写停止栅栏。"""
    expected_generation = max(0, int(generation))
    if not expected_generation:
        return False

    def mark(state: Any) -> bool:
        if int(state.generation) != expected_generation:
            return False
        state.metadata["stop_requested_generation"] = expected_generation
        return True

    value = await store.update_effect_state(
        owner=owner, session_id=session_id, change=mark
    )
    return value is True


def _same_turn(active: Any, *, generation: int, request_id: str) -> bool:
    return bool(
        isinstance(active, Mapping)
        and int(active.get("generation") or 0) == generation
        and str(active.get("request_id") or "") == request_id
    )


async def _stop_captured_turn(
    session: Any,
    store: Any,
    *,
    owner: str,
    session_id: str,
    active: Mapping[str, Any],
) -> tuple[str, int]:
    """Fence and cancel only the coordinator turn observed by ``describe``."""
    try:
        generation = int(active.get("generation") or 0)
    except (TypeError, ValueError):
        generation = 0
    request_id = str(active.get("request_id") or "").strip()
    if generation <= 0 or not request_id:
        return "stop_unconfirmed", 0

    marked = await _request_stop_generation(
        store, owner=owner, session_id=session_id, generation=generation
    )
    stop_generation = generation if marked else 0
    was_cancelling = active.get("status") == "cancelling"

    # request_id is an identity fence: if a later generation wins between the
    # snapshot and this call, its turn is not cancelled.
    accepted = await session.cancel(
        owner=owner, session_id=session_id, request_id=request_id
    )
    current = await session.coordinator.describe(owner=owner, session_id=session_id)
    if current is None:
        return "not_running", stop_generation
    if not _same_turn(current, generation=generation, request_id=request_id):
        return "superseded", stop_generation
    if current.get("protected") is True:
        return "critical_pending", stop_generation

    # If cancel observed the protected state just before unprotect, retry against
    # the same request after revalidation; never fall through to a newer request.
    if not accepted and current.get("status") != "cancelling":
        accepted = await session.cancel(
            owner=owner, session_id=session_id, request_id=request_id
        )
        current = await session.coordinator.describe(owner=owner, session_id=session_id)
        if current is None:
            return "not_running", stop_generation
        if not _same_turn(current, generation=generation, request_id=request_id):
            return "superseded", stop_generation
        if current.get("protected") is True:
            return "critical_pending", stop_generation

    if current.get("status") == "cancelling":
        return ("already_cancelling" if was_cancelling else "stop_requested"), stop_generation
    if accepted:
        return "stop_requested", stop_generation
    return "stop_unconfirmed", stop_generation


def _response(
    *,
    model_turn: str,
    confirmation: str,
    background_tasks: list[dict[str, str]],
    stop_requested_generation: int = 0,
) -> dict[str, Any]:
    critical = model_turn == "critical_pending" or any(
        item["status"] == "critical_pending" for item in background_tasks
    )
    uncancellable = any(item["status"] == "uncancellable" for item in background_tasks)
    target_unresolved = model_turn in {"superseded", "stop_unconfirmed"}
    stopping = model_turn in {"stop_requested", "already_cancelling"} or any(
        item["status"] == "cancel_requested" for item in background_tasks
    )
    stopped = not critical and not uncancellable and not stopping and not target_unresolved
    status = (
        "critical_pending"
        if critical
        else "partially_stopping"
        if target_unresolved and (stopping or uncancellable)
        else "superseded"
        if model_turn == "superseded"
        else "stop_unconfirmed"
        if model_turn == "stop_unconfirmed"
        else "partially_stopping"
        if stopping and uncancellable
        else "stopping"
        if stopping
        else "partial"
        if uncancellable
        else "stopped"
        if stopped and (confirmation != "none" or any(
            item["status"] in {"cancelled", "already_terminal"}
            for item in background_tasks
        ))
        else "already_stopped"
    )
    return {
        "ok": True,
        "status": status,
        "stopped": stopped,
        "model_turn": model_turn,
        "confirmation": confirmation,
        "background_tasks": background_tasks,
        "uncancellable": [item for item in background_tasks if item["status"] == "uncancellable"],
        "stop_requested_generation": stop_requested_generation,
    }

async def stop_agent_session(runtime: Any, owner: str, session_id: str) -> dict[str, Any]:
    """协作停止单个 owner/session，不停止服务、全局队列或其它会话。"""
    owner_key = str(owner or "").strip()
    session_key = str(session_id or "").strip()
    if not owner_key or not session_key:
        raise ValueError("owner and session_id are required")

    session = runtime.session
    store = getattr(runtime, "store", session.state_store)
    model_turn = "not_running"
    confirmation = "none"
    stop_requested_generation = 0

    active = await session.coordinator.describe(owner=owner_key, session_id=session_key)
    if active and active.get("protected") is True:
        model_turn, stop_requested_generation = await _stop_captured_turn(
            session, store, owner=owner_key, session_id=session_key, active=active
        )
        confirmation = "unchanged"
    else:
        try:
            with session_scope_guard(owner_key, session_key, kind="effect"):
                async with session._start_lock:
                    active = await session.coordinator.describe(
                        owner=owner_key, session_id=session_key
                    )
                    if active and active.get("protected") is True:
                        model_turn, stop_requested_generation = await _stop_captured_turn(
                            session, store, owner=owner_key,
                            session_id=session_key, active=active,
                        )
                        confirmation = "unchanged"
                    else:
                        if active:
                            model_turn, stop_requested_generation = await _stop_captured_turn(
                                session, store, owner=owner_key,
                                session_id=session_key, active=active,
                            )

                        if model_turn != "superseded":
                            state = await store.load(owner=owner_key, session_id=session_key)
                            plan_id = str(state.pending_effect_plan_id or "").strip()
                            if plan_id:
                                revoked = await session.cancel_effect(
                                    owner=owner_key,
                                    session_id=session_key,
                                    plan_id=plan_id,
                                )
                                confirmation = "revoked" if revoked else "cleared"
        except SessionBusyError:
            # The session scope may be held by a confirm or a short state mutation.
            # Signal only the observed request and leave its card untouched.
            async with session._start_lock:
                active = await session.coordinator.describe(
                    owner=owner_key, session_id=session_key
                )
                if active is None:
                    model_turn = "critical_pending"
                    confirmation = "unchanged"
                else:
                    model_turn, stop_requested_generation = await _stop_captured_turn(
                        session, store, owner=owner_key,
                        session_id=session_key, active=active,
                    )
                    if model_turn == "critical_pending":
                        confirmation = "unchanged"
                    elif model_turn != "superseded":
                        state = await store.load(owner=owner_key, session_id=session_key)
                        confirmation = "pending" if state.pending_effect_plan_id else "none"

    background_tasks = await _cancel_background_trackers(
        owner_key, session_key, store
    )
    return _response(
        model_turn=model_turn,
        confirmation=confirmation,
        background_tasks=background_tasks,
        stop_requested_generation=stop_requested_generation,
    )
