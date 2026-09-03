"""冻结副作用计划与一次性确认存储。"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from app.agent.confirmation import ConfirmationStore

from .capabilities import ToolEffect


class EffectPlanError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedEffect:
    preview: Mapping[str, Any]
    snapshot_fingerprint: str
    arguments: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EffectPlan:
    plan_id: str
    owner: str
    session_id: str
    generation: int
    tool_name: str
    effect: ToolEffect
    arguments: Mapping[str, Any]
    snapshot_fingerprint: str
    preview: Mapping[str, Any]
    expires_at: float
    owner_generation: int = 0
    confirmation_contract: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        expires_at = (
            datetime.fromtimestamp(self.expires_at, tz=timezone.utc).isoformat(
                timespec="seconds"
            )
            if self.expires_at >= 946_684_800
            else ""
        )
        result = {
            "plan_id": self.plan_id,
            "tool_name": self.tool_name,
            "effect": self.effect.value,
            "preview": deepcopy(dict(self.preview)),
            "expires_at": expires_at,
        }
        confirmation = self.confirmation_contract.get("audit_contract")
        if isinstance(confirmation, Mapping) and confirmation:
            result["confirmation"] = deepcopy(dict(confirmation))
        return result


class EffectPlanStore(Protocol):
    def freeze(
        self,
        *,
        owner: str,
        session_id: str,
        generation: int,
        tool_name: str,
        effect: ToolEffect,
        arguments: Mapping[str, Any],
        prepared: PreparedEffect,
    ) -> EffectPlan: ...

    def claim(
        self,
        *,
        owner: str,
        session_id: str,
        generation: int,
        plan_id: str,
    ) -> EffectPlan: ...

    def cancel(self, *, owner: str, session_id: str, plan_id: str) -> bool: ...


class ConfirmationEffectPlanStore:
    """在既有一次性 ConfirmationStore 之上提供 Kernel EffectPlan。"""

    CONTRACT_VERSION = 1

    def __init__(
        self,
        store: ConfirmationStore | None = None,
        *,
        record_actions: bool = False,
    ) -> None:
        self.store = store or ConfirmationStore()
        self.record_actions = bool(record_actions)

    @staticmethod
    def _scoped_owner(owner: str, session_id: str) -> str:
        owner_key = str(owner or "").strip()
        session_key = str(session_id or "").strip()
        if not owner_key or not session_key:
            raise EffectPlanError("effect owner and session are required")
        return f"kernel:{owner_key}\x1f{session_key}"

    def freeze(
        self,
        *,
        owner: str,
        session_id: str,
        generation: int,
        tool_name: str,
        effect: ToolEffect,
        arguments: Mapping[str, Any],
        prepared: PreparedEffect,
    ) -> EffectPlan:
        if effect is ToolEffect.READ:
            raise EffectPlanError("READ tools cannot create effect plans")
        scoped_owner = self._scoped_owner(owner, session_id)
        effective_arguments = dict(prepared.arguments or arguments)
        contract = {
            "kernel_effect_version": self.CONTRACT_VERSION,
            "session_id": session_id,
            "generation": int(generation),
            "effect": effect.value,
            "preview": deepcopy(dict(prepared.preview)),
            "metadata": deepcopy(dict(prepared.metadata)),
            "audit_risk": str(prepared.metadata.get("risk") or effect.value),
            "audit_contract": deepcopy(
                dict(prepared.metadata.get("confirmation") or {})
            ),
        }
        ticket = self.store.issue(
            owner=scoped_owner,
            tool_name=tool_name,
            arguments=effective_arguments,
            context_fingerprint=str(prepared.snapshot_fingerprint or ""),
            confirmation_contract=contract,
            replace_active_ticket=True,
        )
        return EffectPlan(
            plan_id=ticket.confirmation_id,
            owner=owner,
            session_id=session_id,
            generation=int(generation),
            tool_name=tool_name,
            effect=effect,
            arguments=deepcopy(ticket.arguments),
            snapshot_fingerprint=ticket.context_fingerprint,
            preview=deepcopy(dict(prepared.preview)),
            expires_at=ticket.expires_at,
            owner_generation=ticket.owner_generation,
            confirmation_contract=deepcopy(ticket.confirmation_contract),
            metadata=deepcopy(dict(prepared.metadata)),
        )

    @staticmethod
    def _restore_plan(
        ticket: Any,
        *,
        owner: str,
        session_id: str,
        generation: int,
    ) -> EffectPlan:
        contract = ticket.confirmation_contract
        try:
            version = int(contract.get("kernel_effect_version"))
            stored_session = str(contract.get("session_id") or "")
            stored_generation = int(contract.get("generation"))
            effect = ToolEffect(str(contract.get("effect") or ""))
            preview = contract.get("preview")
            metadata = contract.get("metadata")
        except (TypeError, ValueError) as exc:
            raise EffectPlanError("effect plan payload is invalid") from exc
        if (
            version != ConfirmationEffectPlanStore.CONTRACT_VERSION
            or stored_session != session_id
        ):
            raise EffectPlanError("effect plan scope is invalid")
        if stored_generation != int(generation):
            raise EffectPlanError("effect plan is stale")
        if not isinstance(preview, dict) or not isinstance(metadata, dict):
            raise EffectPlanError("effect plan payload is invalid")
        return EffectPlan(
            plan_id=ticket.confirmation_id,
            owner=owner,
            session_id=session_id,
            generation=stored_generation,
            tool_name=ticket.tool_name,
            effect=effect,
            arguments=deepcopy(ticket.arguments),
            snapshot_fingerprint=ticket.context_fingerprint,
            preview=deepcopy(preview),
            expires_at=ticket.expires_at,
            owner_generation=ticket.owner_generation,
            confirmation_contract=deepcopy(contract),
            metadata=deepcopy(metadata),
        )

    def claim(
        self,
        *,
        owner: str,
        session_id: str,
        generation: int,
        plan_id: str,
    ) -> EffectPlan:
        scoped_owner = self._scoped_owner(owner, session_id)
        # 先只读校验 generation/contract，再做一次性原子领取。旧回合不能
        # 通过“先领取后失败”撤销同会话更新的有效计划。
        preview_ticket = next(
            (
                ticket
                for ticket in self.store.list_active_tickets(owner=scoped_owner)
                if secrets.compare_digest(ticket.confirmation_id, plan_id)
            ),
            None,
        )
        if preview_ticket is None:
            raise EffectPlanError("effect plan is unavailable")
        self._restore_plan(
            preview_ticket,
            owner=owner,
            session_id=session_id,
            generation=generation,
        )
        ticket = self.store.claim_and_rotate_owner(
            owner=scoped_owner,
            confirmation_id=plan_id,
            record_execution=self.record_actions,
            execution_owner=owner,
        )
        return self._restore_plan(
            ticket,
            owner=owner,
            session_id=session_id,
            generation=generation,
        )

    def cancel(self, *, owner: str, session_id: str, plan_id: str) -> bool:
        return self.store.discard(
            owner=self._scoped_owner(owner, session_id),
            confirmation_id=plan_id,
        )
