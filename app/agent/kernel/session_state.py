"""会话状态值、输入守卫与 publication 匹配规则；不依赖存储和续行执行器。"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .conversation_codec import conversation_payload, conversation_rows, json_text
from .conversation_state import history_memory
from .state_types import (
    PublicationLease,
    SelectionInvalidError,
    StateUpdate,
    _ConfirmationClaim,
    _ExecutionCapacity,
    _PendingEffectTransition,
    _PublicationFinished,
    _PublicationUnprotected,
    _ScopedTrustTransition,
)


@dataclass(frozen=True, slots=True)
class CandidateSelectionGuard:
    generation: int
    ref: str
    expires_at: float
    batch_generation: int | None = None

    def check(self, state: SessionState) -> None:
        view = state.metadata.get("ux_candidate_view")
        if (
            state.generation != self.generation
            or self.expires_at <= time.time()
            or not isinstance(view, dict)
            or view.get("ref") != self.ref
            or view.get("generation") != (self.generation if self.batch_generation is None else self.batch_generation)
        ):
            raise SelectionInvalidError()


@dataclass(frozen=True, slots=True)
class PendingEffectGuard:
    generation: int
    plan_id: str
    plan_generation: int
    expires_at: float
    clock: Callable[[], float]

    def check(self, state: SessionState) -> None:
        binding = {"plan_id": self.plan_id, "plan_generation": self.plan_generation}
        current = state.metadata.get("pending_effect_binding")
        if (state.generation != self.generation or state.pending_effect_plan_id != self.plan_id
                or self.expires_at <= self.clock()
                or (current != binding and not (current is None and self.plan_generation == state.generation))):
            raise SelectionInvalidError("待确认计划已失效，请使用当前卡片。")
        state.metadata["pending_effect_binding"] = binding
        if current is None:
            state.metadata["pending_effect_goal"] = deepcopy(next(
                (row for row in reversed(state.conversation) if row.get("role") == "user"), {}
            ))


@dataclass(slots=True)
class SessionState:
    owner: str
    session_id: str
    generation: int = 0
    conversation: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    recent_refs: list[str] = field(default_factory=list)
    ref_kinds: set[str] = field(default_factory=set)
    pending_effect_plan_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    execution_capacity: bool = False
    lifecycle_id: str = field(default_factory=lambda: secrets.token_urlsafe(24))

    def clone(self) -> SessionState:
        return deepcopy(self)

    def to_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "lifecycle_id": self.lifecycle_id,
            "conversation": conversation_payload(deepcopy(self.conversation)),
            "summary": self.summary,
            "recent_refs": list(self.recent_refs[-100:]),
            "ref_kinds": sorted(self.ref_kinds),
            "pending_effect_plan_id": self.pending_effect_plan_id,
            "metadata": deepcopy(self.metadata),
            **({"execution_capacity": 1} if self.execution_capacity else {}),
        }

    @classmethod
    def from_payload(
        cls, *, owner: str, session_id: str, generation: int, payload: Mapping[str, Any]
    ) -> SessionState:
        metadata = payload.get("metadata")
        if "lifecycle_id" in payload:
            lifecycle_id = payload["lifecycle_id"]
            if not isinstance(lifecycle_id, str) or not lifecycle_id.strip() or len(lifecycle_id) > 128:
                raise ValueError("invalid session lifecycle identity")
        else:
            scope = json.dumps([owner, session_id], ensure_ascii=False).encode()
            lifecycle_id = "legacy:" + hashlib.sha256(b"agent-session-lifecycle:v1\0" + scope).hexdigest()
        return cls(
            owner=owner,
            session_id=session_id,
            generation=max(0, generation),
            conversation=conversation_rows(payload.get("conversation", [])),
            summary=str(payload.get("summary") or ""),
            recent_refs=[
                str(item) for item in payload.get("recent_refs", ()) if str(item)
            ][-100:],
            ref_kinds={str(item) for item in payload.get("ref_kinds", ()) if str(item)},
            pending_effect_plan_id=str(payload.get("pending_effect_plan_id") or "")[
                :200
            ],
            metadata=deepcopy(metadata) if isinstance(metadata, dict) else {},
            execution_capacity=bool(payload.get("execution_capacity", 0)),
            lifecycle_id=lifecycle_id,
        )

    def apply(self, updates: Sequence[StateUpdate]) -> None:
        for update in updates:
            if type(update) is _ConfirmationClaim:
                confirmed = deepcopy(dict(update.value))
                previous = self.metadata.get("active_publication") or {}
                marker = {
                    "generation": self.generation, "turn_id": confirmed["turn_id"],
                    "request_id": update.request_id, "status": "running", "protected": True,
                }
                execution_turn_id = (previous.get("execution_turn_id", "")
                                     if previous.get("turn_id") == confirmed["turn_id"] else confirmed["turn_id"])
                if execution_turn_id:
                    marker["execution_turn_id"] = execution_turn_id
                self.metadata["confirmed_publication"] = confirmed
                self.metadata["active_publication"] = marker
                self.pending_effect_plan_id = ""
                self.metadata.pop("pending_effect_binding", None)
                self.metadata.pop("pending_effect_goal", None)
                continue
            if type(update) is _PublicationUnprotected:
                marker = self.metadata.get("active_publication") or {}
                if marker.get("generation") == self.generation:
                    marker.pop("protected", None)
                continue
            if type(update) is _ScopedTrustTransition:
                if update.key != "metadata.scoped_trust" or update.mode != "set":
                    raise ValueError("invalid scoped trust transition")
                if update.value is None:
                    self.metadata.pop("scoped_trust", None)
                else:
                    self.metadata["scoped_trust"] = deepcopy(update.value)
                continue
            if type(update) is _PublicationFinished:
                marker = self.metadata.get("active_publication")
                wait = self.metadata.get("read_wait") or {}
                if (isinstance(marker, dict) and marker.get("generation") == self.generation
                        and marker.get("status") == "running" and wait.get("state") not in {"pending", "claimed", "running"}):
                    marker["status"] = "finished"
                continue
            if type(update) is _PendingEffectTransition:
                if update.mode == "clear_if_equals":
                    if self.pending_effect_plan_id == update.value:
                        if update.cancelled:
                            for row in self.conversation:
                                if row.get("role") == "tool" and row.get("effect_plan_id") == update.value:
                                    row["effect_cancelled"] = True
                                    row["content"] = json_text({"ok": True, "status": "cancelled",
                                        "summary": "此工具此前已成功生成确认卡；这是随后撤销该卡后的状态更新：原确认计划已取消，未执行业务变更。",
                                        "data": {"plan_id": update.value, "preview_created": True, "executed": False}})
                        self.pending_effect_plan_id = ""
                        self.metadata.pop("pending_effect_binding", None)
                        self.metadata.pop("pending_effect_goal", None)
                elif (update.mode == "set" and update.plan_generation == self.generation
                      and update.value and isinstance(update.goal, Mapping)):
                    self.pending_effect_plan_id = str(update.value)
                    self.metadata["pending_effect_binding"] = {"plan_id": update.value, "plan_generation": update.plan_generation}
                    self.metadata["pending_effect_goal"] = deepcopy(dict(update.goal))
                else:
                    raise ValueError("invalid pending effect transition")
                continue
            if type(update) is _ExecutionCapacity:
                self.execution_capacity = bool(update.value)
                continue
            key = (update.key or "").strip()
            if key == "lifecycle_id" or key.startswith("lifecycle_id."):
                raise ValueError("session lifecycle identity is immutable")
            if any(key == "metadata." + field or key.startswith("metadata." + field + ".")
                   for field in ("pending_effect_binding", "pending_effect_goal", "confirmed_publication", "active_publication", "read_wait", "continuation_deliveries", "history_memory_version", "scoped_trust")):
                raise ValueError("pending binding requires a typed transition")
            if key == "metadata" or key == "metadata.task" or key.startswith("metadata.task."):
                raise ValueError("task namespace requires a typed patch")
            if not key:
                continue
            if key == "summary":
                note = str(update.value or "")[:8_000]
                if self.metadata.get("history_memory_version") == 1:
                    memory = history_memory(self)
                    memory["legacy_note"] = note
                    self.summary = json_text(memory)
                else:
                    self.summary = note
            elif key == "pending_effect_plan_id":
                if "pending_effect_binding" in self.metadata:
                    raise ValueError("bound pending ID requires a typed transition")
                if update.mode == "clear_if_equals":
                    # 必须在 store.commit 的锁/事务内比较最新值；旧票据的
                    # 迟到取消或确认结果不能清掉同代次刚发布的新计划。
                    if self.pending_effect_plan_id == str(update.value or ""):
                        self.pending_effect_plan_id = ""
                        self.metadata.pop("pending_effect_binding", None)
                else:
                    self.pending_effect_plan_id = str(update.value or "")[:200]
                    self.metadata.pop("pending_effect_binding", None)
            elif key == "recent_refs":
                values = [str(item) for item in (update.value or []) if str(item)]
                if update.mode == "append":
                    self.recent_refs = (self.recent_refs + values)[-100:]
                else:
                    self.recent_refs = values[-100:]
            elif key == "ref_kinds":
                values = {str(item) for item in (update.value or []) if str(item)}
                self.ref_kinds = (
                    self.ref_kinds | values if update.mode == "append" else values
                )
            elif key == "metadata.ux_candidate_draft" and update.mode == "compare_candidate":
                value = update.value
                current = self.metadata.get("ux_candidate_draft") or {}
                view = self.metadata.get("ux_candidate_view") or {}
                if (
                    view.get("ref") != value["next"]["candidate_ref"]
                    or view.get("expires_at", 0) <= time.time()
                    or (value["expected"] is not None and current.get("handle") != value["expected"])
                ):
                    raise SelectionInvalidError("选择状态已更新，请使用当前消息中的按钮。")
                self.metadata["ux_candidate_draft"] = deepcopy(value["next"])
            elif key.startswith("metadata."):
                field_name = key.partition(".")[2]
                if field_name:
                    if update.mode == "delete":
                        self.metadata.pop(field_name, None)
                    else:
                        self.metadata[field_name] = deepcopy(update.value)


def publication_matches(
    lease: PublicationLease, *, generation: int, confirmed: Any = None,
) -> bool:
    """普通回合按 generation；已认领确认还必须匹配持久化的执行 turn。

    不增加 generation，避免破坏已冻结票据。旧历史没有标记时继续兼容；
    新普通回合增加 generation 后，上一代确认标记自然失效。
    """
    if generation != lease.generation:
        return False
    if not isinstance(confirmed, Mapping) or confirmed.get("generation") != generation:
        return True
    return confirmed.get("turn_id") == lease.turn_id


def publication_commit_matches(
    lease: PublicationLease,
    state: SessionState,
    conversation: Sequence[Mapping[str, Any]] | None,
    updates: Sequence[StateUpdate],
) -> bool:
    """已原子领到当前待确认票据的下一步可接管发布权，旧读回合仍被隔离。"""
    update = updates[0] if conversation is None and len(updates) == 1 else None
    handoff = False
    if type(update) is _ConfirmationClaim:
        handoff = bool(state.pending_effect_plan_id) and update.request_id == lease.request_id and update.value == {
            "generation": lease.generation, "turn_id": lease.turn_id, "plan_id": state.pending_effect_plan_id,
        }
        if not handoff:
            return False
    # 撤销/过期收束只做计划 ID 的原子条件清理，不接管已确认回执的发布权。
    pending_clear = (
        conversation is None and len(updates) == 1
        and updates[0].key == "pending_effect_plan_id"
        and updates[0].mode == "clear_if_equals"
        and (type(updates[0]) is _PendingEffectTransition or "pending_effect_binding" not in state.metadata)
    )
    trust_change = conversation is None and len(updates) == 1 and type(updates[0]) is _ScopedTrustTransition
    confirmed = None if handoff or pending_clear or trust_change or candidate_metadata_only(
        conversation, updates
    ) else state.metadata.get("confirmed_publication")
    return publication_matches(
        lease,
        generation=state.generation,
        confirmed=confirmed,
    )


TRANSIENT_EXTRA_BYTES = 160 * 1024 + len(',"execution_capacity":1')


def state_capacity(payload: Mapping[str, Any], normal: int) -> int:
    phase = payload.get("execution_capacity", 0)
    if type(phase) is not int or phase not in (0, 1):
        raise ValueError("invalid execution capacity phase")
    return normal + TRANSIENT_EXTRA_BYTES if phase else normal


def validate_execution_state(state: SessionState) -> None:
    payload = state.to_payload()
    conversation_rows(payload["conversation"])
    if len(json_text(payload).encode()) > state_capacity(payload, 256 * 1024):
        raise ValueError("Agent Kernel 数据超过持久化上限")


def candidate_metadata_only(
    conversation: Sequence[Mapping[str, Any]] | None, updates: Sequence[StateUpdate],
) -> bool:
    # TG 候选 UI 使用专门的事务内 CAS，不是模型回合的 conversation 发布。
    # 只给这一窄契约例外，不能借 metadata 更新携带回执/计划覆盖。
    return (
        conversation is None and len(updates) == 1
        and updates[0].key == "metadata.ux_candidate_draft"
        and updates[0].mode == "compare_candidate"
    )
