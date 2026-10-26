"""确认写操作的统一后台终态收敛。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from app.agent.models import EFFECT_RECEIPT_RETENTION_SECONDS, ToolContext, ToolResult
from app.agent.public_safety import sanitize_public_text

from . import completion_observers as observers
from .kernel import completion_receipts as receipts

if TYPE_CHECKING:
    from app.agent.kernel.session_state import SessionState
    from app.agent.kernel.state_ports import SessionStateStore

_WAITABLE_STATUSES = frozenset(
    {"accepted", "queued", "running", "in_progress", "retry_wait"}
)
_WAIT_TIMEOUT_SECONDS = 30 * 60
_WAIT_INTERVAL_SECONDS = 1.0
_EFFECT_POLL_SECONDS = 10.0
_EFFECT_WAITS_KEY = "effect_waits"


def _effect_seed(result: Mapping[str, Any], tracker: observers._CompletionTracker | None, tool: str,
                 scope: receipts.EffectCompletionScope) -> dict[str, Any]:
    return {
        "owner": scope.lease.owner, "session_id": scope.lease.session_id,
        "request_id": scope.lease.request_id, "turn_id": scope.lease.turn_id,
        "plan_id": scope.plan_id, "channel": scope.channel,
        "tool": "agent.batch" if (scope.origin or {}).get("batch_plan") else tool,
        **({"tracker": {"kind": tracker.kind, "value": tracker.value}} if tracker else {}),
        "result": deepcopy(dict(result)),
        **({"origin": scope.origin} if scope.origin else {}),
    }


def _effect_wait_registration(
    result: Mapping[str, Any], tracker: observers._CompletionTracker | None, tool: str,
    scope: receipts.EffectCompletionScope, *, existing_only: bool,
) -> Callable[[SessionState], bool]:
    """后台受理与同步失败共用身份、密文和幂等登记；不虚构 tracker。"""
    from .kernel.conversation_state import commit_conversation
    from .kernel.session_state import publication_matches
    from .kernel.state_types import StalePublicationError

    now = time.time()
    seed = _effect_seed(result, tracker, tool, scope)
    cipher = receipts._completion_cipher()
    sealed = cipher.encrypt(json.dumps(
        seed, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
    ).encode()).decode()

    def register(state):
        if not publication_matches(scope.lease, generation=state.generation,
                                   confirmed=state.metadata.get("confirmed_publication")):
            raise StalePublicationError("确认回合已变化，未注册新的结果跟踪")
        if tracker is None:
            if ((state.owner, state.session_id) != (scope.lease.owner, scope.lease.session_id)
                    or state.pending_effect_plan_id or state.metadata.get("confirmed_publication") != {
                        "generation": scope.lease.generation, "turn_id": scope.lease.turn_id, "plan_id": scope.plan_id}):
                raise StalePublicationError("同步失败不属于当前已消费确认")
            if state.metadata.get("stop_requested_generation") == scope.lease.generation:
                return False
        waits = state.metadata.setdefault(_EFFECT_WAITS_KEY, {})
        if scope.plan_id in waits:
            previous = json.loads(cipher.decrypt(waits[scope.plan_id]["sealed"].encode()))
            if tracker is None and previous.get("tracker") is not None:
                return False  # 真实后台任务仍由原 tracker 结算，不能用同步错误覆盖。
            if any(previous.get(key) != seed[key] for key in seed if key != "result"):
                raise StalePublicationError("后台任务跟踪不属于当前确认计划")
            if tracker is None:
                return waits[scope.plan_id].get("final_result") == seed["result"]
            # 补充领域受理明细，不改变已登记的期限、进度或投递状态。
            waits[scope.plan_id]["sealed"] = sealed
        else:
            if existing_only:
                raise StalePublicationError("后台任务缺少原确认跟踪，请查询任务状态，勿重复提交")
            waits[scope.plan_id] = {
                "sealed": sealed, "created_at": now, "expires_at": now + EFFECT_RECEIPT_RETENTION_SECONDS,
                "next_poll_at": now + _EFFECT_POLL_SECONDS, "delivered": False,
                "state": "pending", "last_status": "", "read_errors": 0,
            }
        from app.agent.kernel.continuation import register_effect, release_effect
        row = waits[scope.plan_id]
        register_effect(row, scope, now)
        if tracker is None:
            row.update(state="terminal", last_status=str(result["status"]), final_result=seed["result"],
                       foreground_final=True, next_poll_at=now, receipt_message=receipts._late_receipt(seed, seed["result"]))
            # 回执、原 native tool 结果、可领取状态同事务落盘，冷启动不见旧预览事实。
            commit_conversation(state, state.conversation)
            release_effect(row, now)
        if (state.metadata.get("stop_requested_generation") == scope.lease.generation
                and waits[scope.plan_id].get("continuation")):
            waits[scope.plan_id]["continuation"].update(state="cancelled", terminal_reason="cancelled")
        receipts._next_effect_poll(state)
        return True
    return register


async def register_terminal_failure(
    scope: receipts.EffectCompletionScope, *, tool: str, result: Mapping[str, Any],
) -> bool:
    """登记已消费确认的真实失败，复用 effect 续行；不是新的写授权。"""
    from app.agent.public_view import public_result_state

    origin = scope.origin
    status = str(result.get("status") or "").strip().casefold()
    blocked = {"authorization_denied", "authorization_required", "authorization_failed", "agent_disabled",
               "confirmation_invalid", "confirmation_not_supported",
               "tool_not_found", "cancelled", "stopped"}
    active = _WAITABLE_STATUSES | {"pending", "submitted", "waiting", "stopping"}
    data = result.get("data")
    background = data.get("background_job") if isinstance(data, Mapping) else None
    if (result.get("ok") is not False or not status or status in blocked | active
            or public_result_state(result) != "failed"
            or (isinstance(background, Mapping) and str(background.get("status") or "").casefold() in active)
            or not isinstance(origin, dict)
            or any(key not in origin for key in ("arguments", "expected_task", "admission_fence", "effective_digest",
                                                 "model_policy", "model_limit", "tool_limit", "delivery_route"))
            or not isinstance(origin.get("goal"), dict)
            or origin["goal"].get("role") != "user" or not str(origin["goal"].get("content") or "").strip()):
        return False
    source = origin.get("reobservation_source")
    if (not isinstance(source, Mapping) or (source.get("owner"), source.get("session_id")) != (
            scope.lease.owner, scope.lease.session_id)
            or any(origin.get(key) != expected for key, expected in {
                "owner": scope.lease.owner, "session_id": scope.lease.session_id,
                "origin_generation": scope.lease.generation, "source_turn_id": scope.lease.turn_id,
                "source_request_id": scope.lease.request_id, "channel": scope.channel,
            }.items())):
        return False
    return await scope.store.update_effect_state(
        owner=scope.lease.owner, session_id=scope.lease.session_id,
        change=_effect_wait_registration(result, None, tool, scope, existing_only=False),
    ) is True


def admit_organize_operation(conn, row, replayed: bool, *, tool: str, scope: receipts.EffectCompletionScope) -> None:
    """仅由确认管道注入：队列与会话跟踪必须同一事务受理，不能重领旧无主作业。"""
    from app.repositories.organize_operation_jobs import (
        organize_operation_owner_digest,
        organize_operation_public_ref,
    )

    from .kernel.state_types import StalePublicationError

    if row["owner_digest"] != organize_operation_owner_digest(scope.lease.owner):
        raise StalePublicationError("后台任务不属于当前会话")
    result = ToolResult(True, "accepted", "光鸭后台任务已受理", data={
        "operation_ref": organize_operation_public_ref(str(row["job_id"])),
        "queued": row["status"] == "pending",
    })
    if row["job_kind"] == "agent_guangya_fs_change":
        result.data["operation"] = "filesystem_change"
    tracker = observers._CompletionTracker("guangya_operation", {"operation_ref": result.data["operation_ref"]})
    from app.agent.kernel.projection import DefaultProjector
    registration = _effect_wait_registration(DefaultProjector().project(result).public_content,
                                             tracker, tool, scope, existing_only=replayed)

    def register(state):
        if state.generation != scope.lease.generation or state.metadata.get("confirmed_publication") != {
            "generation": scope.lease.generation, "turn_id": scope.lease.turn_id, "plan_id": scope.plan_id,
        }:
            raise StalePublicationError("原确认已失效，未受理后台任务")
        return registration(state)

    registered = scope.store.update_effect_state_in_transaction(
        conn, owner=scope.lease.owner, session_id=scope.lease.session_id, change=register,
    )
    if registered is not True:
        raise StalePublicationError("原会话已清理，未受理后台任务")


async def _register_effect_wait(result: ToolResult, tracker: observers._CompletionTracker, tool: str,
                                scope: receipts.EffectCompletionScope) -> None:
    from app.agent.kernel.projection import DefaultProjector

    from .kernel.state_types import StalePublicationError

    registered = await scope.store.update_effect_state(
        owner=scope.lease.owner, session_id=scope.lease.session_id,
        change=_effect_wait_registration(DefaultProjector().project(result).public_content,
                                         tracker, tool, scope, existing_only=tracker.kind == "guangya_operation"),
    )
    if registered is not True:
        raise StalePublicationError("原会话已清理，未重建结果跟踪")


async def _record_effect_observation(
    scope: receipts.EffectCompletionScope | None, *, status: str, final: ToolResult | None = None,
    due_now: bool = False,
) -> bool:
    if scope is None:
        return False
    from app.agent.kernel.projection import DefaultProjector

    stop_requested = False

    def update(state):
        nonlocal stop_requested
        row = (state.metadata.get(_EFFECT_WAITS_KEY) or {}).get(scope.plan_id)
        if row is None:
            return False
        row["last_status"] = (status or "unknown")
        row["next_poll_at"] = time.time() + (0 if due_now else _EFFECT_POLL_SECONDS)
        if final is not None:
            from .kernel.batch_progress import observe_batch
            observed = dict(DefaultProjector().project(final).public_content)
            row.update(state="terminal", final_result=observe_batch(row, scope.origin or {}, observed),
                       foreground_final=True)
            from app.agent.kernel.continuation import release_effect
            release_effect(row, time.time())
        receipts._next_effect_poll(state)
        stop_requested = (
            state.generation == scope.lease.generation
            and state.metadata.get("stop_requested_generation") == scope.lease.generation
        )
        return True

    updated = await scope.store.update_effect_state(
        owner=scope.lease.owner, session_id=scope.lease.session_id, change=update
    )
    if updated is not True:
        raise asyncio.CancelledError("原会话结果跟踪已清理")
    return stop_requested


def _enqueue_late_receipt(
    seed: Mapping[str, Any], message: Mapping[str, Any], *, lifecycle_id: str,
) -> bool:
    from app.modules.telegram_notification_center import (
        _effect_delivery_key,
        publish_agent_interaction_reply,
    )
    from app.notifier import NotificationEvent

    if seed["channel"] != "telegram":
        return True
    match = re.fullmatch(r"tg:v1:(-?[0-9]+)\x1f[0-9]+", seed["owner"])
    if match is None:
        # 只保留会话回执，不把无法确定接收人的交互回复发到默认通知群。
        return True
    delivery = {
        "version": 1, "owner": seed["owner"], "session_id": seed["session_id"],
        "delivery_id": seed["plan_id"], "lifecycle_id": lifecycle_id,
        "delivery_route": (seed.get("origin") or {}).get("delivery_route") or {"chat_id": match.group(1)},
    }
    published = publish_agent_interaction_reply(
        _effect_delivery_key(delivery), NotificationEvent(
            title="已确认任务的后续结果", lines=(message["public_content"],),
        ), delivery=delivery, deliver_now=False,
    )
    # 用户关闭 TG Agent 时不排队补发旧交互，但 Web/历史中的任务事实仍保留。
    return bool(published) or published.status == "disabled"


async def poll_effect_receipts(
    store: SessionStateStore, *, cancelled: Callable[[], bool] = lambda: False, limit: int = 16,
) -> int:
    """复用现有调度器读取已提交任务；不调用 execute、不创建新确认或模型回合。"""
    import secrets

    from app.agent.kernel.projection import DefaultProjector
    from app.logger import get_logger

    from .kernel.conversation_state import commit_conversation
    from .kernel.state_types import SessionBusyError

    semaphore = asyncio.Semaphore(4)

    async def follow(record):
        async with semaphore:
            if cancelled():
                return 0
            try:
                seed = json.loads(receipts._completion_cipher().decrypt(record["sealed"].encode()))
                if seed["plan_id"] != record["plan_id"]:
                    return 0
                plan_id, claim_id = seed["plan_id"], secrets.token_hex(12)
                scope = {"owner": seed["owner"], "session_id": seed["session_id"]}

                def claim(state):
                    row = (state.metadata.get(_EFFECT_WAITS_KEY) or {}).get(plan_id)
                    if (not row or row.get("sealed") != record["sealed"] or row.get("delivered")
                            or float(row["next_poll_at"]) > time.time()):
                        return None
                    row.update(claim_id=claim_id, next_poll_at=time.time() + 60)
                    receipts._next_effect_poll(state)
                    return dict(row)

                current = await store.update_effect_state(**scope, change=claim)
                if current is None:
                    return 0
                final = current.get("final_result")
                status = current.get("last_status", "unknown")
                errors = int(current.get("read_errors", 0))
                if final is None and not cancelled():
                    tracker = observers._CompletionTracker(**seed["tracker"])
                    result = receipts._seed_result(seed)
                    try:
                        snapshot, status, task = await asyncio.wait_for(
                            observers._poll(tracker, ToolContext(**scope, request_id=seed["request_id"],
                                                       cancelled=cancelled)), timeout=15,
                        )
                        errors = 0
                        if status in observers._TERMINAL_STATUSES[tracker.kind]:
                            final = dict(DefaultProjector().project(
                                observers._terminal_result(result, snapshot, status, task, tracker),
                            ).public_content)
                    except (LookupError, ValueError):
                        # 失效/被清理的稳定引用不可改查最近任务，也不可无限重试。
                        current["expires_at"] = 0
                    except Exception:  # noqa: BLE001 - 读取失败不改变任务本身状态
                        errors += 1
                    if final is None and time.time() >= float(current["expires_at"]):
                        final = dict(DefaultProjector().project(ToolResult(
                            False, "outcome_unknown", "后台任务结果仍无法核验，自动跟踪已结束",
                            data={"original_summary": result.summary},
                            suggestions=["请核对原任务状态；未确认结果前不要重复提交。"],
                        )).public_content)
                if cancelled():
                    return 0

                def save(state):
                    waits = state.metadata.get(_EFFECT_WAITS_KEY) or {}
                    row = waits.get(plan_id)
                    if not row or row.get("claim_id") != claim_id:
                        return None
                    row.update(last_status=status, read_errors=errors,
                               next_poll_at=time.time() + min(60, _EFFECT_POLL_SECONDS * (errors + 1)))
                    if final is None:
                        receipts._next_effect_poll(state)
                        return None
                    template = row.get("receipt_template")
                    if row.get("foreground_final") and template and template in state.conversation:
                        from .kernel.completion_receipts import effect_active
                        if not effect_active(row):
                            waits.pop(plan_id)
                        receipts._next_effect_poll(state)
                        return None
                    message = template or receipts._late_receipt(seed, final)
                    from .kernel.batch_progress import observe_batch
                    final_result = observe_batch(row, seed.get("origin") or {}, final)
                    if (seed.get("origin") or {}).get("batch_plan"):
                        message = receipts._late_receipt(seed, final_result)
                    row.update(state="terminal", final_result=final_result, receipt_message=message)
                    from app.agent.kernel.continuation import release_effect
                    release_effect(row, time.time())
                    commit_conversation(state, state.conversation)
                    receipts._next_effect_poll(state)
                    return message, state.lifecycle_id

                saved = await store.update_effect_state(**scope, change=save)
                if saved is None or cancelled():
                    return 0
                message, lifecycle_id = saved
                # 先保存会话回执，再交给同一个 outbox；崩溃重试沿用相同幂等键。
                if not await asyncio.to_thread(_enqueue_late_receipt, seed, message, lifecycle_id=lifecycle_id):
                    return 0

                def delivered(state):
                    row = (state.metadata.get(_EFFECT_WAITS_KEY) or {}).get(plan_id)
                    if row and row.get("claim_id") == claim_id:
                        row["delivered"] = True  # 已交付会话/outbox，不等同远端已收到。
                        receipts._next_effect_poll(state)
                await store.update_effect_state(**scope, change=delivered)
                return 1
            except SessionBusyError:
                return 0  # 活跃确认持锁时让路，不抢占用户操作。
            except Exception as exc:  # noqa: BLE001 - 单个损坏跟踪记录不阻塞其它会话
                get_logger(__name__).warning("Agent 后续回执核验暂未完成 type=%s", type(exc).__name__)
                return 0

    return sum(await asyncio.gather(*(follow(row) for row in await store.due_effect_waits(limit=limit))))


def session_effect_trackers(state: SessionState) -> list[dict[str, Any]]:
    """从同一会话快照解密并校验 tracker，不重读会话或修改回执。"""
    owner_key = state.owner.strip()
    session_key = state.session_id.strip()
    if not owner_key or not session_key:
        return []
    waits = state.metadata.get(_EFFECT_WAITS_KEY)
    if not isinstance(waits, Mapping):
        return []

    from cryptography.fernet import InvalidToken

    cipher = receipts._completion_cipher()
    current: list[dict[str, Any]] = []
    for plan_id, record in waits.items():
        if not isinstance(plan_id, str) or not isinstance(record, Mapping):
            continue
        sealed = record.get("sealed")
        if not isinstance(sealed, str) or not sealed:
            continue
        try:
            seed = json.loads(cipher.decrypt(sealed.encode("ascii")).decode("utf-8"))
        except (InvalidToken, UnicodeError, ValueError, TypeError):
            # 损坏/旧密文不能扩大到其它任务。
            continue
        if (
            not isinstance(seed, Mapping)
            or seed.get("owner") != owner_key
            or seed.get("session_id") != session_key
            or seed.get("plan_id") != plan_id
        ):
            continue
        raw_tracker = seed.get("tracker")
        if not isinstance(raw_tracker, Mapping):
            continue
        kind = str(raw_tracker.get("kind") or "").strip()
        value = raw_tracker.get("value")
        if kind not in observers._ACTIVE_STATUSES or not isinstance(value, Mapping):
            continue
        current.append(
            {
                "owner": owner_key,
                "session_id": session_key,
                "plan_id": plan_id,
                "tracker": {"kind": kind, "value": deepcopy(dict(value))},
                "state": str(record.get("state") or ""),
                "last_status": str(record.get("last_status") or "").strip().casefold(),
                "delivered": record.get("delivered") is True,
            }
        )
    return current


async def wait_for_effect_completion(
    result: ToolResult,
    *,
    tool: str,
    context: ToolContext,
    report_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    timeout_seconds: float = _WAIT_TIMEOUT_SECONDS,
    scope: receipts.EffectCompletionScope | None = None,
) -> ToolResult:
    """追踪有稳定句柄的后台写操作；纯提交结果原样交给公开语义层。"""
    status = (result.status or "").strip().casefold()
    tracker = observers._completion_tracker(result)
    if not result.ok or status not in _WAITABLE_STATUSES or tracker is None:
        return result

    if scope is not None:
        await _register_effect_wait(result, tracker, tool, scope)
    active_statuses = observers._ACTIVE_STATUSES[tracker.kind]
    terminal_statuses = observers._TERMINAL_STATUSES[tracker.kind]
    label = sanitize_public_text(result.summary, limit=120) or "后台任务"

    async def report(snapshot: ToolResult, task_status: str) -> None:
        if report_progress is None:
            return
        payload = {
            "phase": "background_job",
            "tool": (tool or "")[:120],
            "status": task_status,
            "summary": sanitize_public_text(snapshot.summary, limit=240)
            or "后台任务状态已更新",
        }
        operation_ref = str(tracker.value.get("operation_ref") or "").strip().upper()
        if operation_ref:
            payload["operation_ref"] = operation_ref
        try:
            await report_progress(payload)
        except Exception:  # noqa: BLE001 - 进度通道故障不应改写业务终态
            return

    def pending_result(
        last_status: str,
        snapshot: ToolResult | None = None,
        task: dict[str, Any] | None = None,
        *,
        timed_out: bool = False,
    ) -> ToolResult:
        job_status = last_status or "unknown"
        data = observers._background_data(
            result,
            snapshot,
            job_status,
            task or {},
            tracker,
            timed_out=timed_out,
        )
        suggestions = list(
            dict.fromkeys(
                [
                    *result.suggestions,
                    "可继续询问刚才的任务状态；确认终态前请勿重复提交。",
                ]
            )
        )
        if scope is not None:
            data["background_job"]["followup_pending"] = True
            return replace(result, ok=True, status="running",
                summary=(f"{label}仍在后台执行，完成后会自动回报" if job_status in active_statuses
                         else f"{label}状态暂未确认，后台将继续核验并回报"),
                data=data, model_data=observers._background_model_data(result, data),
                suggestions=["无需再次确认或重复提交；系统会继续跟踪这个已提交任务。"], error="")
        return replace(
            result,
            ok=False,
            status="outcome_unknown",
            summary=(
                f"{label}仍在运行，等待已达上限，结果尚未确认"
                if timed_out and job_status in active_statuses
                else f"{label}状态暂时未知，结果尚未确认"
            ),
            data=data,
            model_data=observers._background_model_data(result, data),
            suggestions=suggestions,
            error=result.error or "后台任务尚未返回可信终态",
        )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, float(timeout_seconds))
    last_status = ""
    last_snapshot: ToolResult | None = None
    last_task: dict[str, Any] = {}
    failures = 0
    last_saved = loop.time()
    while True:
        if context.cancelled():
            raise asyncio.CancelledError
        try:
            snapshot, task_status, task = await observers._poll(tracker, context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 仅重读状态，不重放写操作。
            failures += 1
            remaining = deadline - loop.time()
            if not isinstance(exc, (LookupError, ValueError)) and failures < 3 and remaining > 0:
                await asyncio.sleep(min(_WAIT_INTERVAL_SECONDS, remaining))
                continue
            await _record_effect_observation(scope, status=last_status, due_now=True)
            return pending_result(last_status, last_snapshot, last_task)

        failures = 0
        last_snapshot, last_task = snapshot, task
        if task_status in terminal_statuses:
            await report(snapshot, task_status)
            final = observers._terminal_result(result, snapshot, task_status, task, tracker)
            await _record_effect_observation(scope, status=task_status, final=final)
            return final
        if task_status not in active_statuses:
            await _record_effect_observation(scope, status=task_status, due_now=True)
            return pending_result(
                last_status
                or ("unknown" if task_status in {"", "empty", "idle"} else task_status),
                snapshot,
                task,
            )

        if task_status != last_status or loop.time() - last_saved >= _EFFECT_POLL_SECONDS:
            stop_requested = await _record_effect_observation(scope, status=task_status)
            last_saved = loop.time()
            if stop_requested:
                # Stop only the foreground wait. The durable tracker stays pending so
                # the already-accepted remote operation can still reach a real receipt.
                return pending_result(task_status, snapshot, task)
        last_status = task_status
        await report(snapshot, task_status)
        # 持久 Agent 作业交给已登记的终态跟踪；前台只保护提交与回执落盘，
        # 不占住同一会话直到全库巡检结束。无持久scope的调用仍等待终态。
        if scope is not None and tracker.kind == "agent_job":
            return pending_result(last_status, snapshot, task)
        remaining = deadline - loop.time()
        if remaining <= 0:
            await _record_effect_observation(scope, status=last_status, due_now=True)
            return pending_result(last_status, snapshot, task, timed_out=True)
        await asyncio.sleep(min(_WAIT_INTERVAL_SECONDS, remaining))
