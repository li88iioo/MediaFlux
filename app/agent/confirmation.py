"""服务端一次性确认票据。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import secrets
import threading
import time
from typing import Any, Callable

from app.agent.registry import AgentToolError


@dataclass(frozen=True)
class ConfirmationTicket:
    confirmation_id: str
    owner: str
    tool_name: str
    arguments: dict[str, Any]
    context_fingerprint: str
    expires_at: float
    owner_generation: int = 0
    followup_context: dict[str, Any] = field(default_factory=dict)
    confirmation_contract: dict[str, Any] = field(default_factory=dict)


class ConfirmationStore:
    """线程安全、会话绑定、短期且只能消费一次的确认票据存储。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = 60,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(24))
        self._lock = threading.RLock()
        self._tickets: dict[str, ConfirmationTicket] = {}
        self._owner_generations: dict[str, tuple[int, float]] = {}

    def issue(
        self,
        *,
        owner: str,
        tool_name: str,
        arguments: dict[str, Any],
        context_fingerprint: str = "",
        followup_context: dict[str, Any] | None = None,
        confirmation_contract: dict[str, Any] | None = None,
        expected_owner_generation: int | None = None,
    ) -> ConfirmationTicket:
        owner_key = str(owner or "").strip()
        if not owner_key:
            raise AgentToolError("当前会话无法创建确认请求", code="confirmation_invalid")
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            owner_generation = self._owner_generation_locked(owner_key, now=now, touch=True)
            if (
                expected_owner_generation is not None
                and int(expected_owner_generation) != owner_generation
            ):
                raise AgentToolError(
                    "会话已重置，请重新生成确认请求",
                    code="confirmation_invalid",
                )
            while len(self._tickets) >= self.max_entries:
                oldest_id = min(self._tickets, key=lambda key: self._tickets[key].expires_at)
                self._tickets.pop(oldest_id, None)
            confirmation_id = self._new_unique_id_locked()
            ticket = ConfirmationTicket(
                confirmation_id=confirmation_id,
                owner=owner_key,
                tool_name=str(tool_name or "").strip(),
                arguments=deepcopy(arguments),
                context_fingerprint=str(context_fingerprint or ""),
                expires_at=now + self.ttl_seconds,
                owner_generation=owner_generation,
                followup_context=deepcopy(followup_context or {}),
                confirmation_contract=deepcopy(confirmation_contract or {}),
            )
            self._tickets[confirmation_id] = ticket
            return ticket

    def claim(self, *, owner: str, confirmation_id: str) -> ConfirmationTicket:
        owner_key = str(owner or "").strip()
        ticket_id = str(confirmation_id or "").strip()
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            ticket = self._tickets.get(ticket_id)
            if (
                ticket is None
                or not owner_key
                or not secrets.compare_digest(ticket.owner, owner_key)
                or ticket.owner_generation != self._owner_generation_locked(
                    owner_key, now=now, touch=True
                )
            ):
                raise AgentToolError("确认请求无效或已过期", code="confirmation_invalid")
            self._tickets.pop(ticket_id, None)
            return ConfirmationTicket(
                confirmation_id=ticket.confirmation_id,
                owner=ticket.owner,
                tool_name=ticket.tool_name,
                arguments=deepcopy(ticket.arguments),
                context_fingerprint=ticket.context_fingerprint,
                expires_at=ticket.expires_at,
                owner_generation=ticket.owner_generation,
                followup_context=deepcopy(ticket.followup_context),
                confirmation_contract=deepcopy(ticket.confirmation_contract),
            )

    def discard(self, *, owner: str, confirmation_id: str) -> bool:
        """撤销属于指定会话且尚未消费的确认票据。"""
        owner_key = str(owner or "").strip()
        ticket_id = str(confirmation_id or "").strip()
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            ticket = self._tickets.get(ticket_id)
            if ticket is None or not owner_key or not secrets.compare_digest(ticket.owner, owner_key):
                return False
            self._tickets.pop(ticket_id, None)
            return True

    def rotate_owner(self, *, owner: str) -> tuple[int, int]:
        """推进 owner epoch 并撤销其票据，返回 ``(数量, 新 epoch)``。"""
        owner_key = str(owner or "").strip()
        if not owner_key:
            raise AgentToolError("当前会话无法创建确认请求", code="confirmation_invalid")
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            generation = self._new_owner_generation_locked()
            self._owner_generations[owner_key] = (generation, now)
            ticket_ids = [
                key
                for key, ticket in self._tickets.items()
                if secrets.compare_digest(ticket.owner, owner_key)
            ]
            for key in ticket_ids:
                self._tickets.pop(key, None)
            return len(ticket_ids), generation

    def revoke_owner(self, *, owner: str) -> int:
        """撤销某个会话仍然有效的全部确认票据。"""
        owner_key = str(owner or "").strip()
        if not owner_key:
            return 0
        revoked, _generation = self.rotate_owner(owner=owner_key)
        return revoked

    def owner_generation(self, *, owner: str) -> int:
        owner_key = str(owner or "").strip()
        if not owner_key:
            raise AgentToolError("当前会话无法创建确认请求", code="confirmation_invalid")
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            return self._owner_generation_locked(owner_key, now=now, touch=True)

    def reset(self) -> None:
        with self._lock:
            self._tickets.clear()
            self._owner_generations.clear()

    def _prune_locked(self, now: float) -> None:
        expired = [key for key, ticket in self._tickets.items() if ticket.expires_at <= now]
        for key in expired:
            self._tickets.pop(key, None)
        active_owners = {ticket.owner for ticket in self._tickets.values()}
        generation_cutoff = now - (self.ttl_seconds * 2)
        stale_owners = [
            owner
            for owner, (_generation, touched_at) in self._owner_generations.items()
            if touched_at <= generation_cutoff and owner not in active_owners
        ]
        for owner in stale_owners:
            self._owner_generations.pop(owner, None)
        max_owner_generations = max(32, self.max_entries * 4)
        while len(self._owner_generations) > max_owner_generations:
            removable = [
                owner for owner in self._owner_generations if owner not in active_owners
            ]
            if not removable:
                break
            oldest_owner = min(
                removable,
                key=lambda owner: self._owner_generations[owner][1],
            )
            self._owner_generations.pop(oldest_owner, None)

    def _owner_generation_locked(self, owner: str, *, now: float, touch: bool) -> int:
        current = self._owner_generations.get(owner)
        if current is None:
            generation = self._new_owner_generation_locked()
            touched_at = now
        else:
            generation, touched_at = current
        if touch or current is None:
            self._owner_generations[owner] = (generation, now)
        else:
            self._owner_generations[owner] = (generation, touched_at)
        return generation

    def _new_owner_generation_locked(self) -> int:
        # 使用不可预测且不复用的 epoch；即使旧 tombstone 被清理，慢 prepare
        # 也无法借由 generation 回退为 0 而在 reset 后重新签发票据。
        active_generations = {generation for generation, _ in self._owner_generations.values()}
        for _ in range(8):
            generation = secrets.randbits(63) or 1
            if generation not in active_generations:
                return generation
        raise AgentToolError("暂时无法创建确认请求", code="confirmation_unavailable")

    def _new_unique_id_locked(self) -> str:
        for _ in range(8):
            token = str(self._token_factory() or "").strip()
            if token and token not in self._tickets:
                return token
        raise AgentToolError("暂时无法创建确认请求", code="confirmation_unavailable")
