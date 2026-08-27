"""单次 Media Agent 回合的进程内状态模型。

状态只用于协调能力召回、工具观察、进度展示和响应契约；不会持久化用户原文、
工具参数或原始结果，也不会改变任何工具权限。
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
import threading
from time import monotonic
from typing import Any, Iterable, Mapping

from app.agent.capability_retrieval import MediaIntentProfile, infer_media_intent


@dataclass(frozen=True, slots=True)
class AgentTurnObservation:
    tool_name: str
    state: str
    ok: bool | None = None


@dataclass(slots=True)
class AgentTurn:
    request_id: str
    owner_bound: bool
    intent: MediaIntentProfile
    started_at: float = field(default_factory=monotonic)
    phase: str = "created"
    phase_history: list[str] = field(default_factory=lambda: ["created"])
    capability_names: tuple[str, ...] = ()
    observations: dict[str, AgentTurnObservation] = field(default_factory=dict)
    response_contract: dict[str, str] = field(default_factory=dict)
    completed: bool = False
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def transition(self, phase: object) -> None:
        normalized = str(phase or "").strip()[:40]
        if not normalized:
            return
        with self._lock:
            self.phase = normalized
            if not self.phase_history or self.phase_history[-1] != normalized:
                self.phase_history.append(normalized)
                if len(self.phase_history) > 32:
                    del self.phase_history[:-32]

    def set_capabilities(self, names: Iterable[object]) -> None:
        safe = tuple(dict.fromkeys(
            str(name or "").strip()[:120]
            for name in names
            if str(name or "").strip()
        ))[:20]
        with self._lock:
            self.capability_names = safe

    def observe(self, tool_name: object, *, state: str, ok: bool | None = None) -> None:
        name = str(tool_name or "").strip()[:120]
        normalized_state = str(state or "").strip()[:32]
        if not name or not normalized_state:
            return
        with self._lock:
            self.observations[name] = AgentTurnObservation(
                tool_name=name,
                state=normalized_state,
                ok=ok if isinstance(ok, bool) else None,
            )

    def set_response_contract(self, contract: Mapping[str, Any]) -> None:
        safe = {
            key: str(contract.get(key) or "").strip()[:40]
            for key in ("task_kind", "presentation", "resource_candidates")
        }
        if not all(safe.values()):
            return
        with self._lock:
            self.response_contract = safe
            self.completed = True
            self.transition("completed")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "request_id": self.request_id,
                "owner_bound": self.owner_bound,
                "intent": {
                    "domains": list(self.intent.domains),
                    "preferred_sources": list(self.intent.preferred_sources),
                    "forbidden_sources": list(self.intent.forbidden_sources),
                    "presentation_hint": self.intent.presentation_hint,
                },
                "phase": self.phase,
                "phase_history": list(self.phase_history),
                "capability_names": list(self.capability_names),
                "observations": [
                    {
                        "tool_name": item.tool_name,
                        "state": item.state,
                        "ok": item.ok,
                    }
                    for item in self.observations.values()
                ],
                "response_contract": dict(self.response_contract),
                "completed": self.completed,
                "elapsed_ms": max(0, int((monotonic() - self.started_at) * 1000)),
            }


_ACTIVE_AGENT_TURN: ContextVar[AgentTurn | None] = ContextVar(
    "mediaflux_active_agent_turn", default=None
)


def begin_agent_turn(*, message: str, owner: str = "", request_id: str = "") -> Token[AgentTurn | None]:
    turn = AgentTurn(
        request_id=str(request_id or "").strip()[:128],
        owner_bound=bool(str(owner or "").strip()),
        intent=infer_media_intent(message),
    )
    return _ACTIVE_AGENT_TURN.set(turn)


def reset_agent_turn(token: Token[AgentTurn | None]) -> None:
    _ACTIVE_AGENT_TURN.reset(token)


def active_agent_turn() -> AgentTurn | None:
    return _ACTIVE_AGENT_TURN.get()


def transition_agent_turn(phase: object) -> None:
    turn = active_agent_turn()
    if turn is not None:
        turn.transition(phase)


def record_agent_capabilities(names: Iterable[object]) -> None:
    turn = active_agent_turn()
    if turn is not None:
        turn.set_capabilities(names)
        turn.transition("capabilities_ready")


def observe_agent_tool(tool_name: object, *, state: str, ok: bool | None = None) -> None:
    turn = active_agent_turn()
    if turn is not None:
        turn.observe(tool_name, state=state, ok=ok)


def record_agent_response_contract(contract: Mapping[str, Any]) -> None:
    turn = active_agent_turn()
    if turn is not None:
        turn.set_response_contract(contract)
