"""完成回执的加密、恢复、历史交付与确认范围；不观察业务任务或启动续行。"""
from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.agent.models import Evidence, ToolResult
from app.agent.public_view import _CONFIRMED_RESULT_MARKER, format_public_result, public_result_state

from .conversation_state import commit_conversation
from .events import AgentEvent, AgentEventType, EventFactory
from .model import ModelMessage
from .projection import _model_safe
from .session_state import SessionState
from .state_ports import SessionStateStore
from .state_types import PublicationLease

EFFECT_PENDING = {"waiting", "pending", "claimed", "running"}
_EFFECT_WAITS_KEY = "effect_waits"
_EFFECT_NEXT_POLL_KEY = "effect_next_poll_at"


@dataclass(frozen=True)
class EffectCompletionScope:
    """确认管道注入的原会话范围，不来自模型参数。"""

    store: SessionStateStore
    lease: PublicationLease
    plan_id: str
    channel: str = "api"
    origin: dict[str, Any] | None = None


def effect_active(row):
    record = row.get("continuation") or {}
    return record.get("state") in EFFECT_PENDING


def _completion_cipher() -> Fernet:
    from app.modules.web_secret import get_web_secret

    secret = (get_web_secret() or "")
    if not secret:
        raise ValueError("Agent 回执持久化密钥不可用")
    key = hashlib.sha256(b"mediaflux-agent-effect-wait:v1\0" + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _next_effect_poll(state: SessionState) -> None:
    waits = state.metadata.get(_EFFECT_WAITS_KEY) or {}
    due = [float(row["next_poll_at"]) for row in waits.values() if not row.get("delivered")]
    if due:
        state.metadata[_EFFECT_NEXT_POLL_KEY] = min(due)
    else:
        state.metadata.pop(_EFFECT_NEXT_POLL_KEY, None)


def _seed_result(seed: Mapping[str, Any]) -> ToolResult:
    value = seed["result"]
    return ToolResult(
        bool(value["ok"]), str(value["status"]), str(value["summary"]),
        data=dict(value.get("data") or {}),
        evidence=[Evidence(**item) for item in value.get("evidence") or []],
        suggestions=list(value.get("suggestions") or []), error=str(value.get("error") or ""),
    )


def _late_receipt(seed: Mapping[str, Any], public_result: Mapping[str, Any]) -> dict[str, Any]:
    message = ModelMessage(
        role="assistant", tool_name=seed["tool"], effect_plan_id=seed["plan_id"],
        completion_receipt_id=seed["plan_id"],
        content=_CONFIRMED_RESULT_MARKER + "\n" + json.dumps(public_result, ensure_ascii=False),
    ).to_dict()
    message["public_content"] = format_public_result(public_result)
    return message


def _confirmation_result(value: object) -> Mapping[str, Any] | None:
    if (isinstance(value, Mapping) and type(value.get("ok")) is bool
            and isinstance(value.get("status"), str) and value["status"].strip()):
        return value
    return None


def _conversation_receipt(state: SessionState, plan_id: str) -> Mapping[str, Any] | None:
    prefix = _CONFIRMED_RESULT_MARKER + "\n"
    for message in reversed(state.conversation):
        content = message.get("content")
        if (message.get("role") != "assistant" or not message.get("tool_name")
                or message.get("completion_receipt_id") != plan_id or message.get("effect_plan_id") != plan_id
                or not isinstance(content, str) or not content.startswith(prefix)):
            continue
        try:
            return _confirmation_result(json.loads(content.removeprefix(prefix)))
        except ValueError:
            continue
    return None


async def replay_confirmation_receipt(
    state: SessionState, *, plan_id: str, request_id: str,
    read_events: Callable[..., Awaitable[list[dict[str, Any]]]] | None,
) -> tuple[AgentEvent, ...]:
    """用原确认的持久证据重投公开回执，不进入确认执行或后台观察。"""
    waits = state.metadata.get(_EFFECT_WAITS_KEY)
    row = waits.get(plan_id) if isinstance(waits, Mapping) else None
    turn_id = tool = ""
    result = None
    sealed = row.get("sealed") if isinstance(row, Mapping) else None
    if isinstance(sealed, str) and sealed:
        try:
            seed = json.loads(_completion_cipher().decrypt(sealed.encode("ascii")))
        except (InvalidToken, ValueError):
            seed = None  # 密文不可用时仍可采用原回合的签名事件。
        if isinstance(seed, dict) and isinstance(row, Mapping) and (
            seed.get("owner"), seed.get("session_id"), seed.get("plan_id"), seed.get("request_id"),
        ) == (state.owner, state.session_id, plan_id, request_id):
            turn_id, tool = seed.get("turn_id"), seed.get("tool")
            result = _confirmation_result(row.get("final_result") if row.get("state") == "terminal" else seed.get("result"))

    if result is None and read_events is not None:
        events = await read_events(owner=state.owner, session_id=state.session_id, request_id=request_id, limit=500)
        for event in reversed(events):
            if (event.get("session_id"), event.get("request_id")) != (state.session_id, request_id):
                continue
            payload = event.get("payload")
            if not isinstance(payload, Mapping) or payload.get("plan_id") != plan_id:
                continue
            kind = event.get("type")
            if kind in {AgentEventType.EFFECT_COMPLETED, AgentEventType.EFFECT_FAILED} and result is None:
                if (result := _confirmation_result(payload.get("result"))) is not None:
                    turn_id, tool = event.get("turn_id"), payload.get("tool")
            elif kind == AgentEventType.TOOL_STARTED and payload.get("kind") == "confirmed_effect":
                # 认领事件绑定原请求，也为已并入会话的后台回执补齐请求身份。
                turn_id, tool = event.get("turn_id"), payload.get("tool")
                break
        if turn_id and (result is None or public_result_state(result) in {"pending", "submitted"}):
            result = _conversation_receipt(state, plan_id) or result

    if not isinstance(turn_id, str) or not turn_id or not isinstance(tool, str) or not tool or result is None:
        return ()
    public = _model_safe(result)
    answer = format_public_result(public)
    failed = public["ok"] is False
    factory = EventFactory(session_id=state.session_id, turn_id=turn_id, request_id=request_id)
    error = {"code": public["status"], "message": public.get("error") or public.get("summary")} if failed else {}
    receipt = factory.create(AgentEventType.EFFECT_FAILED if failed else AgentEventType.EFFECT_COMPLETED,
        {"plan_id": plan_id, "tool": tool, "result": public, "receipt": answer, **error})
    if public["status"] in {"stopped", "cancelled"}:
        terminal = factory.create(AgentEventType.TURN_CANCELLED, {"reason": answer, "turn_status": "cancelled"})
    elif failed:
        terminal = factory.create(AgentEventType.TURN_FAILED, {**error, "turn_status": "failed"})
    else:
        status = {"pending": "waiting", "submitted": "waiting", "warning": "partial"}.get(public_result_state(public), "effect_completed")
        terminal = factory.create(AgentEventType.TURN_COMPLETED,
            {"status": status, "turn_status": status, "answer": answer, "finish_reason": "confirmation_receipt"})
    return receipt, terminal


async def remember_effect_receipt(store, *, owner, session_id, plan_id, message):
    """先保存投影后的完整回执模板，再写会话，崩溃后仍能补回opaque refs等投影。"""
    def remember(state):
        row = (state.metadata.get(_EFFECT_WAITS_KEY) or {}).get(plan_id)
        if row and row.get("state") == "terminal":
            row["receipt_template"] = dict(message)
            if row.get("receipt_message") is not None:
                row["receipt_message"] = dict(message)
                commit_conversation(state, state.conversation)
            return True
        return False
    return await store.update_effect_state(owner=owner, session_id=session_id, change=remember)


async def acknowledge_effect_receipt(store, *, owner, session_id, plan_id):
    """仅在前台终态会话已落盘后清理等待记录；超时的占位回执不能ACK。"""
    def acknowledge(state):
        waits = state.metadata.get(_EFFECT_WAITS_KEY) or {}
        row = waits.get(plan_id)
        if row and row.get("state") == "terminal" and row.get("foreground_final"):
            if effect_active(row):
                row["delivered"] = True
            else:
                waits.pop(plan_id)
            _next_effect_poll(state)
            return True
        return False
    return await store.update_effect_state(owner=owner, session_id=session_id, change=acknowledge)
