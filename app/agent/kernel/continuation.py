"""签名会话续行：READ额度恢复与已确认后台结果共用领取、预算和执行租约。"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from cryptography.fernet import InvalidToken

from app.agent.errors import _ReadPermitRejected
from app.agent.feature_gate import RuntimeFence, require_runtime_fence

from .batch_progress import observe_batch, restored_effect_tool
from .completion_receipts import EFFECT_PENDING, _completion_cipher, _late_receipt
from .conversation_codec import conversation_payload, json_text
from .conversation_state import commit_conversation
from .session_guard import publication_execution_active
from .session_state import SessionState
from .state_types import PublicationLease, StalePublicationError, StateUpdate
from .task_state import identity
from .tool_calls import PipelineResult

MAX_WAIT_SEGMENTS = 3
MAX_WAIT_SECONDS = 180
EXPIRY_SECONDS = 300
CLAIM_SECONDS = 30
ACTIVE_SECONDS = 60
SCAN_LIMIT = 16
ACTIVE = {"pending", "claimed", "running"}

# 同一对象用于前台、READ等待与effect续行；usage另行报告，不参与退款。
BUDGET_COUNTERS = ("model", "tool", "token")


def active_remaining(budget, now):
    since = budget.get("active_since")
    spent = budget.get("active_spent", 0) + (max(0, now - since) if since is not None else 0)
    limit = budget.get("active_limit")
    return None if limit is None else max(0, limit - spent)


def reserve_budget_values(budget, value, now):
    """先扣后请求，不退款；旧有限上限只能收紧，缺失历史成本不补造。"""
    updated = dict(budget)
    for kind in (*BUDGET_COUNTERS, "active"):
        key = kind + "_limit"
        limits = [v for v in (updated.get(key), value.get(key)) if v is not None]
        updated[key] = min(limits) if limits else None
    # 新预算保留前台max_tool_calls的业务调用口径；旧签名记录不追溯减去发现次数。
    if "discovery_spent" in updated:
        updated["discovery_spent"] += value.get("discovery_spent", 0)
    for kind in BUDGET_COUNTERS:
        key, limit = kind + "_spent", updated[kind + "_limit"]
        amount = value.get(key, 0)
        if type(amount) is not int or amount < 0:
            raise ValueError("invalid budget debit")
        spent = updated.get(key, 0) + amount
        counted = spent - (updated.get("discovery_spent", 0) if kind == "tool" else 0)
        if amount and limit is not None and counted > limit:
            raise ValueError(kind + "_budget_exceeded")
        updated[key] = spent
    since = updated.get("active_since")
    updated["active_spent"] = updated.get("active_spent", 0) + value.get("active_spent", 0) + (max(0, now - since) if since is not None else 0)
    updated["active_since"] = None if value.get("stop") else now
    if not value.get("stop") and active_remaining(updated, now) == 0:
        raise ValueError("active_time_budget_exceeded")
    budget.update(updated)


def exhausted(budget, now):
    return active_remaining(budget, now) == 0 or any(
        budget.get(kind + "_limit") is not None and budget.get(kind + "_spent", 0)
        - (budget.get("discovery_spent", 0) if kind == "tool" else 0) >= budget[kind + "_limit"]
        for kind in BUDGET_COUNTERS)


def record_budget(state, record):
    # 已有task的effect引用原预算，不能复制出可分别消费的两份余额。
    task_id = record.get("budget_task_id")
    if task_id:
        source = state.metadata.get("read_wait") or {}
        if source.get("task_id") != task_id:
            raise ValueError("task budget no longer available")
        return source["budget"]
    return record["budget"]


@dataclass(frozen=True, slots=True)
class _ContinuationAction(StateUpdate):
    """Only the Session executor builds these; never decoded from tool output."""
    binding: Any = None


def digest(rows):
    return hashlib.sha256(json.dumps(json.loads(json_text(conversation_payload(rows))), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _history_anchors(state, call_id, tool):
    for index, row in enumerate(state.conversation):
        if row.get("role") == "assistant" and any(c.get("call_id") == call_id for c in row.get("tool_calls", ())):
            calls = row["tool_calls"]
            group = state.conversation[index:index + 1 + len(calls)]
            if len(group) != len(calls) + 1 or {r.get("tool_call_id") for r in group[1:]} != {c["call_id"] for c in calls}:
                raise ValueError("read wait requires a closed native group")
            user = next((r for r in reversed(state.conversation[:index]) if r.get("role") == "user"), None)
            if user is not None:
                anchor = {"group_digest": digest(group), "user_digest": digest([user]), "call_id": call_id, "tool": tool}
                yield index, anchor, next(c for c in calls if c["call_id"] == call_id)


def anchor_for(state, call_id, tool):
    latest = next((i for i in reversed(range(len(state.conversation)))
                   if state.conversation[i].get("role") == "assistant" and state.conversation[i].get("tool_calls")), None)
    matches = list(_history_anchors(state, call_id, tool))
    current = next((anchor for index, anchor, _call in matches if index == latest), None)
    if current is None or sum(anchor == current for _index, anchor, _call in matches) != 1:
        raise ValueError("read wait history unavailable")
    return current


def history_call(state, record):
    anchor = record["anchor"]
    matches = [call for _index, candidate, call in _history_anchors(state, anchor["call_id"], anchor["tool"])
               if candidate == anchor]
    if len(matches) != 1:
        raise ValueError("read wait history changed")
    return deepcopy(matches[0])


def check_claim(state, lease, now, conn=None, *, publication=False):
    if not lease.continuation_id:
        return
    record = continuation_record(state, lease.continuation_id, lease.continuation_kind)
    if (record.get("wait_id") != lease.continuation_id
            or record.get("claim_id") != lease.continuation_claim_id
            or record.get("resume_turn_id") != lease.turn_id
            or record.get("publication_generation") != state.generation
            or (lease.continuation_kind == "read_quota" and record.get("expected_task") != identity(state.metadata.get("task")))
            or record.get("state") not in ({"claimed", "running", "pending", "terminal"} if publication else {"claimed", "running"})
            or now < record.get("last_checked_at", now)
            or now >= record.get("expires_at", 0)):
        raise StalePublicationError("read wait claim revoked")
    if conn is not None:
        require_runtime_fence(conn, RuntimeFence(**record["admission_fence"]))


def revoke(state, reason):
    record = state.metadata.get("read_wait")
    if isinstance(record, dict) and record.get("state") in ACTIVE:
        record.update(state="cancelled" if reason == "cancelled" else "terminal", terminal_reason=reason)
    for row in (state.metadata.get("effect_waits") or {}).values():
        continuation = row.get("continuation") or {}
        if continuation.get("state") not in EFFECT_PENDING:
            continue
        if reason == "cancelled":
            continuation.update(state="cancelled", terminal_reason=reason)
        elif reason == "superseded" and continuation.get("state") in {"claimed", "running"}:
            continuation.update(state="pending", claim_id="", claim_until=0, resume_turn_id="")


def task_changed(state):
    record = state.metadata.get("read_wait")
    if not isinstance(record, dict):
        return
    task = state.metadata.get("task")
    # 完成业务清单不撤销当前执行租约；由同一终结事件保存最终回答/投递。
    if (not task or record["task_id"] != task["task_id"] or task["status"] == "cancelled"
            or (task["status"] == "complete" and record.get("state") != "running")):
        revoke(state, "task_changed")
    elif record.get("expected_task") and record["expected_task"]["epoch"] != task["epoch"]:
        revoke(state, "task_changed")
    record["expected_task"] = identity(task)


def interrupt_read_wait(state: SessionState, now: float) -> bool:
    """调用方已独占续行 OS 锁；开始过的 READ 只能收束，不能重放。"""
    record = state.metadata.get("read_wait") or {}
    if record.get("state") != "running":
        return False
    record.update(state="terminal", terminal_reason="interrupted", claim_id="",
                  claim_until=0, last_checked_at=max(now, record.get("last_checked_at", now)))
    marker = state.metadata.get("active_publication") or {}
    if (record.get("publication_generation") == state.generation
            and marker.get("generation") == state.generation
            and marker.get("request_id") == record.get("source_request_id")):
        marker["status"] = "finished"
    return True


def _remember_delivery(metadata, record, view, route):
    delivery_id = f'{record["wait_id"]}:{record["resume_turn_id"]}'
    delivery = {
        "kind": "delivery", "delivery_id": delivery_id,
        "owner": record["owner"], "session_id": record["session_id"],
        "delivery_view": deepcopy(view), "delivery_route": deepcopy(route or {}),
        "due_at": record["due_at"], "expires_at": record["expires_at"],
    }
    deliveries = metadata.setdefault("continuation_deliveries", {})
    previous = deliveries.get(delivery_id)
    if previous is not None:
        if any(previous.get(key) != value for key, value in delivery.items() if key != "due_at"):
            raise ValueError("continuation delivery identity changed")
        return
    deliveries[delivery_id] = delivery


def normalize_delivery_payload(payload):
    """旧签名数据只在解码边界搬移；运行时只读取独立的待交接终态。"""
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return
    records = [metadata.get("read_wait") or {}, *((row.get("continuation") or {})
               for row in (metadata.get("effect_waits") or {}).values())]
    for record in records:
        if record.get("delivery_pending") is True:
            if record.get("kind") == "effect":
                state = SessionState.from_payload(
                    owner=record["owner"], session_id=record["session_id"], generation=0, payload=payload,
                )
                route = effect_payload(state, record)["origin"].get("delivery_route")
            else:
                route = record.get("delivery_route")
            _remember_delivery(metadata, record, record["delivery_view"], route)
        for key in ("delivery_pending", "delivery_view", "delivery_state"):
            record.pop(key, None)


def apply_actions(state, lease, updates, now, conn=None):
    for update in updates:
        if type(update) is not _ContinuationAction:
            continue
        value = update.value
        operation = value["operation"]
        if conn is not None:
            require_runtime_fence(conn, update.binding.fence)
        if operation == "finish":
            record = continuation_record(state, lease.continuation_id, lease.continuation_kind)
            reserve_budget_values(record_budget(state, record), {"stop": True}, now)
            record.update(state="terminal", terminal_reason=value["reason"])
            if lease.continuation_kind == "effect":
                row = state.metadata["effect_waits"][record["wait_id"]]
                if row.get("batch_progress"):
                    payload = effect_payload(state, record)
                    final = observe_batch(row, payload["origin"], payload["result"])
                    row.update(final_result=final, receipt_message=_late_receipt(payload["seed"], final))
                    commit_conversation(state, state.conversation)
            if value.get("delivery_view") is not None and record.get("channel") == "telegram":
                deliveries = state.metadata.get("continuation_deliveries") or {}
                for delivery_id in [key for key, item in deliveries.items() if item["expires_at"] <= now]:
                    deliveries.pop(delivery_id)
                route = (effect_payload(state, record)["origin"].get("delivery_route")
                         if lease.continuation_kind == "effect" else record.get("delivery_route"))
                _remember_delivery(state.metadata, record, value["delivery_view"], route)
            marker = state.metadata.get("active_publication") or {}
            if marker.get("generation") == lease.generation and marker.get("request_id") == lease.request_id:
                marker["status"] = "finished"
            continue
        if lease.continuation_kind == "effect" and lease.continuation_id:
            apply_effect_action(state, lease, update, now)
            continue
        task = state.metadata.get("task")
        record = state.metadata.get("read_wait")
        if operation == "seed":
            if not task or (record and record["task_id"] == task["task_id"]):
                raise ValueError("read wait budget cannot be reseeded")
            state.metadata["read_wait"] = {
                "v": 1, "task_id": task["task_id"], "expected_task": identity(task),
                "state": "idle", "terminal_reason": "", "wait_id": "",
                "owner": state.owner, "session_id": state.session_id,
                "budget": deepcopy(value["budget"]),
            }
            continue
        if not record or not task or record["task_id"] != task["task_id"]:
            raise ValueError("trusted task budget unavailable")
        budget = record["budget"]
        if operation == "reserve":
            if value.get("task_id") and value["task_id"] != task["task_id"]:
                raise ValueError("selected task budget changed")
            reserve_budget_values(budget, value, now)
            if lease.continuation_id:
                record["state"] = "running"
                record["last_checked_at"] = now
            continue
        if operation == "park":
            permit = value["permit"]
            if not isinstance(permit, _ReadPermitRejected) or permit.rate_limit["reason"] != "quota":
                raise ValueError("untrusted READ rejection")
            due = permit.rate_limit["retry_at_epoch"]
            if ((type(due) is not float and type(due) is not int) or not math.isfinite(due) or due < now
                    or state.pending_effect_plan_id or task["status"] in {"complete", "cancelled"}
                    or budget["wait_segments"] >= MAX_WAIT_SEGMENTS
                    or exhausted(budget, now)
                    or due - now + budget["wait_seconds_spent"] > MAX_WAIT_SECONDS):
                raise ValueError("read wait budget or eligibility rejected")
            if record["state"] in ACTIVE and not lease.continuation_id:
                raise ValueError("another read wait is active")
            anchor = anchor_for(state, value["call_id"], permit.tool_name)
            from app.agent.owner_routes import normalize_delivery_route
            route = normalize_delivery_route(value["delivery_route"], channel=value["channel"], owner=state.owner)
            first = record.get("first_park_at", now)
            expires = record.get("expires_at", first + EXPIRY_SECONDS)
            if due >= expires:
                raise ValueError("read wait expired")
            record.update(state="pending", terminal_reason="", wait_id=record["wait_id"] if lease.continuation_id else secrets.token_urlsafe(18),
                          anchor=anchor, admission_fence=asdict(update.binding.fence),
                          effective_digest=update.binding.effective_digest,
                          model_policy=value["model_policy"],
                          channel=value["channel"], delivery_route=route, source_request_id=lease.request_id,
                          source_turn_id=record.get("source_turn_id", lease.turn_id), publication_generation=lease.generation,
                          first_park_at=first, parked_at=now, due_at=due, expires_at=expires,
                          last_checked_at=now, claim_id=lease.continuation_claim_id, claim_until=0,
                          resume_turn_id=lease.turn_id if lease.continuation_id else "",
                          expected_task=identity(task))
            reserve_budget_values(budget, {"stop": True}, now)
            budget["wait_segments"] += 1
        else:
            raise ValueError("invalid read wait operation")


def claim(state: SessionState, candidate, now, binding, conn=None):
    if candidate.get("kind") == "effect":
        return claim_effect(state, candidate, now, binding, conn)
    record = state.metadata.get("read_wait") or {}
    same_candidate = all(record.get(key) == candidate.get(key) for key in (
        "wait_id", "publication_generation", "expected_task", "claim_id", "state"))
    if (record.get("wait_id") != candidate.get("wait_id")
            or record.get("publication_generation") != state.generation
            or record.get("expected_task") != identity(state.metadata.get("task"))
            or record.get("state") not in {"pending", "claimed"}
            or (record["state"] == "claimed" and now < record["claim_until"])):
        return None
    # An expired claim may be retried from its original scan only while the
    # same native parked segment has never started. Repark changes the anchor
    # and segment count; claim/reclaim leaves that segment identity intact.
    reclaimable = (record["state"] == "claimed"
                   and candidate.get("state") in {"pending", "claimed"}
                   and all(record.get(key) == candidate.get(key) for key in (
                       "wait_id", "expected_task", "anchor"))
                   and record["budget"]["wait_segments"] == candidate.get("budget", {}).get("wait_segments"))
    if not same_candidate and not reclaimable:
        return None
    confirmed = state.metadata.get("confirmed_publication") or {}
    if state.pending_effect_plan_id or confirmed.get("generation") == state.generation:
        if same_candidate:
            revoke(state, "effect_conflict")
        return None
    if record["admission_fence"] != asdict(binding.fence) or record["effective_digest"] != binding.effective_digest:
        if same_candidate:
            revoke(state, "runtime_changed")
        return None
    if conn is not None:
        require_runtime_fence(conn, binding.fence)
    spent = record["budget"]["wait_seconds_spent"] + max(0, now - record["parked_at"])
    if now < record["last_checked_at"] or now >= record["expires_at"] or spent >= MAX_WAIT_SECONDS:
        if same_candidate:
            revoke(state, "expired")
        return None
    if now < record["due_at"]:
        return None
    try:
        history_call(state, record)
    except ValueError:
        if same_candidate:
            revoke(state, "history_unavailable")
        return None
    state.generation += 1
    record.update(state="claimed", claim_id=secrets.token_urlsafe(18), claim_until=now + CLAIM_SECONDS,
                  resume_turn_id=secrets.token_urlsafe(12), publication_generation=state.generation,
                  parked_at=now, last_checked_at=now)
    record["budget"]["wait_seconds_spent"] = spent
    state.metadata["active_publication"] = {"generation": state.generation,
        "turn_id": record["resume_turn_id"], "request_id": record["source_request_id"], "status": "running"}
    return PublicationLease(state.owner, state.session_id, state.generation, record["resume_turn_id"],
                            record["source_request_id"], record["wait_id"], record["claim_id"])


# Accepted writes are never resubmitted. A frozen batch may finish its approved
# synchronous tail; other continuations can only plan new work with fresh consent.
EFFECT_ACTIVE_SECONDS = 1800


def continuation_record(state, wait_id, kind):
    if kind == "effect":
        row = (state.metadata.get("effect_waits") or {}).get(wait_id) or {}
        return row.get("continuation") or {}
    return state.metadata.get("read_wait") or {}



def effect_origin(state, context, plan):
    binding = context.continuation_binding
    policy = context.continuation_policy
    goal = state.metadata.get("pending_effect_goal")
    if (binding is None or not policy or not isinstance(goal, dict)
            or (context.channel == "telegram" and not context.delivery_route)
            or goal.get("role") != "user" or not str(goal.get("content") or "").strip()):
        return None
    frozen = plan.metadata.get("continuation_budget") or {}
    budget = policy.get("budget") or frozen.get("budget") or {
        "model_limit": policy["model_limit"], "tool_limit": policy["tool_limit"],
        "model_spent": 0, "tool_spent": 0, "wait_segments": 0, "wait_seconds_spent": 0}
    return {"goal": deepcopy(goal), "arguments": dict(plan.arguments),
            "budget": deepcopy(budget), "budget_task_id": frozen.get("task_id", policy.get("budget_task_id", "")),
            **({"reobservation_source": deepcopy(plan.metadata["reobservation_source"])}
               if plan.metadata.get("reobservation_source") is not None else {}),
            "owner": state.owner, "session_id": state.session_id,
            "source_request_id": context.lease.request_id, "source_turn_id": context.lease.turn_id,
            "origin_generation": context.lease.generation, "expected_task": identity(state.metadata.get("task")),
            "admission_fence": asdict(binding.fence), "effective_digest": binding.effective_digest,
            "model_policy": policy["model_policy"], "model_limit": budget["model_limit"],
            "tool_limit": budget["tool_limit"], "channel": context.channel,
            "delivery_route": dict(context.delivery_route)}


def register_effect(row, scope, now):
    origin = scope.origin
    if not origin or "continuation" in row:
        return
    row["continuation"] = {key: deepcopy(origin[key]) for key in (
        "owner", "session_id", "source_request_id", "source_turn_id", "origin_generation",
        "expected_task", "admission_fence", "effective_digest", "model_policy", "channel")}
    row["continuation"].update(kind="effect", wait_id=scope.plan_id, state="waiting", terminal_reason="",
        budget_task_id=origin.get("budget_task_id"),
        due_at=now, expires_at=row["expires_at"], last_checked_at=now,
        publication_generation=scope.lease.generation, claim_id="", claim_until=0, resume_turn_id="",
        **({} if origin.get("budget_task_id") else {
            "budget": deepcopy(origin.get("budget") or {
                "model_limit": origin["model_limit"], "tool_limit": origin["tool_limit"],
                "model_spent": 0, "tool_spent": 0, "wait_segments": 0, "wait_seconds_spent": 0})}))


def release_effect(row, now):
    record = row.get("continuation")
    if record and record["state"] == "waiting" and isinstance(row.get("final_result"), dict):
        record.update(state="pending", due_at=now, last_checked_at=now)


def due_records(metadata, now):
    records = []
    candidates = [metadata.get("read_wait") or {}, *((row.get("continuation") or {})
                  for row in (metadata.get("effect_waits") or {}).values())]
    for record in candidates:
        if record.get("due_at", float("inf")) > now:
            continue
        # READ running 不可重放；effect 的冻结尾部保留既有租约恢复规则。
        resumable = {"pending", "claimed", "running"} if record.get("kind") == "effect" else {"pending", "claimed"}
        if record.get("state") in resumable and record.get("claim_until", 0) <= now:
            records.append(deepcopy(record))
    records.extend(deepcopy(record) for record in (metadata.get("continuation_deliveries") or {}).values()
                   if record["due_at"] <= now)
    return sorted(records, key=lambda r: r["due_at"])


def effect_payload(state, record):
    # The encrypted admission seed, not a chat summary or model-supplied goal.
    row = (state.metadata.get("effect_waits") or {}).get(record["wait_id"])
    if not row or not isinstance(row.get("final_result"), dict):
        raise ValueError("effect completion unavailable")
    seed = json.loads(_completion_cipher().decrypt(row["sealed"].encode()))
    origin = seed.get("origin") or {}
    if (seed.get("owner"), seed.get("session_id"), seed.get("plan_id")) != (
            state.owner, state.session_id, record["wait_id"]):
        raise ValueError("effect continuation scope mismatch")
    if any(record.get(key) != origin.get(key) for key in (
            "owner", "session_id", "source_request_id", "source_turn_id", "origin_generation",
            "expected_task", "admission_fence", "effective_digest", "model_policy", "channel", "budget_task_id")):
        raise ValueError("effect continuation origin changed")
    return {"seed": seed, "origin": origin, "result": deepcopy(row["final_result"])}


def retained_delivery_routes(state):
    """为旧 outbox 归一枚举仍留在签名状态内的可信终态身份；不发送旧记录。"""
    for delivery_id, record in (state.metadata.get("continuation_deliveries") or {}).items():
        if ((record.get("owner"), record.get("session_id")) == (state.owner, state.session_id)
                and record.get("delivery_id") == delivery_id):
            yield delivery_id, record.get("delivery_route")
    records = [state.metadata.get("read_wait") or {}, *((row.get("continuation") or {})
               for row in (state.metadata.get("effect_waits") or {}).values())]
    for record in records:
        if (record.get("state") != "terminal" or record.get("channel") != "telegram"
                or (record.get("owner"), record.get("session_id")) != (state.owner, state.session_id)
                or not record.get("wait_id") or not record.get("resume_turn_id")):
            continue
        try:
            route = (effect_payload(state, record)["origin"].get("delivery_route")
                     if record.get("kind") == "effect" else record.get("delivery_route"))
        except (InvalidToken, KeyError, TypeError, ValueError):
            continue
        yield f'{record["wait_id"]}:{record["resume_turn_id"]}', route


def claim_effect(state, candidate, now, binding, conn=None):
    row = (state.metadata.get("effect_waits") or {}).get(candidate.get("wait_id")) or {}
    record = row.get("continuation") or {}
    if (not record or record.get("state") not in {"pending", "claimed", "running"}
            or any(record.get(key) != candidate.get(key) for key in (
                "wait_id", "claim_id", "state", "publication_generation"))
            or record.get("claim_until", 0) > now or record["due_at"] > now):
        return None
    stopped_generation = state.metadata.get("stop_requested_generation")
    if type(stopped_generation) is int and record["origin_generation"] <= stopped_generation <= state.generation:
        record.update(state="cancelled", terminal_reason="cancelled")
        return None
    if (record["admission_fence"] != asdict(binding.fence) or record["effective_digest"] != binding.effective_digest
            or now < record["last_checked_at"] or now >= record["expires_at"]):
        record.update(state="terminal", terminal_reason="expired_or_runtime_changed")
        return None
    if conn is not None:
        require_runtime_fence(conn, binding.fence)
    if state.pending_effect_plan_id:
        return None  # Retain this goal, never overwrite the user's newer confirmation.
    if publication_execution_active(state):
        return None  # A newer user turn is still working; defer without discarding the old goal.
    task = state.metadata.get("task")
    expected = record.get("expected_task")
    if task and expected and task["task_id"] == expected["task_id"] and (
            task["epoch"] != expected["epoch"] or task["status"] == "cancelled"):
        record.update(state="cancelled", terminal_reason="goal_changed")
        return None
    try:
        payload = effect_payload(state, record)
        record_budget(state, record)
        # Confirmation has its own request ID within the same user generation.
        # Only a newer publication supersedes the frozen tail authorization.
        if payload["origin"].get("batch_plan") and state.generation != record["publication_generation"]:
            record.update(state="cancelled", terminal_reason="new_user_turn")
            return None
    except (InvalidToken, ValueError, KeyError, TypeError):
        record.update(state="cancelled", terminal_reason="origin_unavailable")
        return None
    state.generation += 1
    record.update(state="claimed", claim_id=secrets.token_urlsafe(18), claim_until=now + CLAIM_SECONDS,
        resume_turn_id=secrets.token_urlsafe(12), publication_generation=state.generation, last_checked_at=now)
    state.metadata["active_publication"] = {"generation": state.generation,
        "turn_id": record["resume_turn_id"], "request_id": record["source_request_id"], "status": "running"}
    state.metadata["confirmed_publication"] = {"generation": state.generation,
        "turn_id": record["resume_turn_id"], "plan_id": record["wait_id"]}
    if payload["origin"].get("batch_plan"):
        state.metadata["active_publication"]["protected"] = True
    return PublicationLease(state.owner, state.session_id, state.generation, record["resume_turn_id"],
        record["source_request_id"], record["wait_id"], record["claim_id"], "effect")


def apply_effect_action(state, lease, update, now):
    record = continuation_record(state, lease.continuation_id, lease.continuation_kind)
    value, budget = update.value, record_budget(state, record)
    if value["operation"] == "reserve":
        reserve_budget_values(budget, value, now)
        record.update(state="running", last_checked_at=now, claim_until=now + EFFECT_ACTIVE_SECONDS + CLAIM_SECONDS)
    elif value["operation"] == "park":
        permit = value["permit"]
        if not isinstance(permit, _ReadPermitRejected) or permit.rate_limit["reason"] != "quota":
            raise ValueError("untrusted READ rejection")
        due = permit.rate_limit["retry_at_epoch"]
        if ((type(due) is not float and type(due) is not int) or not math.isfinite(due) or not now <= due < record["expires_at"]
                or state.pending_effect_plan_id or budget["wait_segments"] >= MAX_WAIT_SEGMENTS
                or exhausted(budget, now)
                or budget["wait_seconds_spent"] + due - now > MAX_WAIT_SECONDS):
            raise ValueError("effect continuation wait budget exhausted")
        # Only an acquire refusal is resumable; no started external call is retried here.
        anchor = anchor_for(state, value["call_id"], permit.tool_name)
        reserve_budget_values(budget, {"stop": True}, now)
        budget["wait_segments"] += 1
        budget["wait_seconds_spent"] += due - now
        record.update(state="pending", anchor=anchor, due_at=due, claim_until=0, last_checked_at=now)
    else:
        raise ValueError("unsupported effect continuation transition")


async def adopt_foreground(store, lease, plan_id):
    """完成回执不消费原目标；原前台沿同一续行租约继续，不再另开模型循环。"""
    def adopt(state):
        row = (state.metadata.get("effect_waits") or {}).get(plan_id) or {}
        record = row.get("continuation") or {}
        if not record or record.get("state") not in {"pending", "waiting"}:
            return None
        if state.generation != lease.generation or state.metadata.get("confirmed_publication") != {
                "generation": lease.generation, "turn_id": lease.turn_id, "plan_id": plan_id}:
            return None
        # 前台写入和回执已收束；后台等待不算活跃执行。task预算由同一turn的reserve收尾。
        if not record.get("budget_task_id"):
            reserve_budget_values(record["budget"], {"stop": True}, store._clock())
        if not isinstance(row.get("final_result"), dict):
            return None
        effect_payload(state, record)
        record.update(state="claimed", claim_id=secrets.token_urlsafe(18), claim_until=store._clock() + EFFECT_ACTIVE_SECONDS + CLAIM_SECONDS,
                      resume_turn_id=lease.turn_id, publication_generation=lease.generation)
        return PublicationLease(lease.owner, lease.session_id, lease.generation, lease.turn_id,
                                lease.request_id, plan_id, record["claim_id"], "effect")
    return await store.update_effect_state(owner=lease.owner, session_id=lease.session_id, change=adopt)


def confirmed_result(catalog, state, record):
    from .tool_calls import EffectPlan
    from .projection import ToolOutcome
    payload = effect_payload(state, record)
    seed, origin, result = payload["seed"], payload["origin"], payload["result"]
    tool = restored_effect_tool(catalog, seed["tool"], origin["arguments"])
    batch = origin.get("batch_plan") or {}
    if batch and (tool.name not in {"session.batch", "agent.batch"} or batch["effect"] != tool.effect.value):
        raise ValueError("frozen batch risk changed")
    plan = EffectPlan(seed["plan_id"], state.owner, state.session_id, origin["origin_generation"],
                      tool.name, tool.effect, origin["arguments"], batch.get("snapshot", ""), {}, 0,
                      owner_generation=batch.get("owner_generation", 0),
                      confirmation_contract=deepcopy(batch.get("confirmation_contract", {})),
                      metadata=deepcopy(batch["metadata"]) if batch else
                               ({"reobservation_source": deepcopy(origin["reobservation_source"])}
                                if origin.get("reobservation_source") is not None else {}))
    return PipelineResult(tool, origin["arguments"], ToolOutcome(
        model_content=json.dumps(result, ensure_ascii=False), public_content=result,
    ), effect_plan=plan)


@dataclass(frozen=True, slots=True)
class _DeliveryAcknowledged:
    record: dict
    accepted: bool
    expired: bool
    now: float

    def __call__(self, state):
        record = self.record
        deliveries = state.metadata.get("continuation_deliveries") or {}
        current = deliveries.get(record["delivery_id"])
        if current is None or current != record:
            return
        if self.accepted or self.expired:
            deliveries.pop(record["delivery_id"])
            if not deliveries:
                state.metadata.pop("continuation_deliveries", None)
        else:
            current["due_at"] = self.now + CLAIM_SECONDS


async def deliver_continuation_result(store, candidate, *, prepare_delivery, publish_delivery):
    """通过组合层提供的交付入口重投持久视图，不再调用模型或执行旧写操作。"""

    state = await store.load(owner=candidate["owner"], session_id=candidate["session_id"])
    record = (state.metadata.get("continuation_deliveries") or {}).get(candidate["delivery_id"])
    if record is None or record != candidate:
        return False
    now = store._clock()
    expired = now >= record["expires_at"]
    accepted = False
    if not expired:
        payload = prepare_delivery(
            record["delivery_view"], owner=state.owner, session_id=state.session_id,
            delivery_route=record["delivery_route"], delivery_id=record["delivery_id"],
            lifecycle_id=state.lifecycle_id,
        )
        result = await publish_delivery(payload)
        accepted = result.accepted

    await store.update_effect_state(owner=state.owner, session_id=state.session_id,
        change=_DeliveryAcknowledged(record, accepted, expired, now))
    return accepted
