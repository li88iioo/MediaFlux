"""新 Agent Kernel 的薄 Web API；不包含领域意图或业务状态机。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
from typing import Annotated, Any

from fastapi import APIRouter, Body, Request
from fastapi.responses import StreamingResponse

from app import config
from app.agent.feature_gate import is_agent_enabled
from app.agent.kernel.bootstrap import get_agent_kernel_runtime
from app.agent.kernel.state import SelectionInvalidError, SessionBusyError
from app.agent.kernel.transports import (
    EffectEnvelope,
    QueryEnvelope,
    TransportInputError,
)
from app.agent.kernel.ux_display import next_actions_view, session_display_patch
from app.agent.kernel.ux_selection import current_candidate_view, normalize_selection
from app.agent.owner_routes import web_kernel_owner
from app.agent.public_view import public_conversation_messages
from app.agent.rate_limit import agent_rate_limiter
from app.agent.workspace_next_actions import summarize_workspace_next_actions
from app.modules.web_secret import get_web_secret
from app.web import api_error, api_response, require_api_login

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_REQUEST_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,160}$")


class AgentRateLimitError(TransportInputError):
    pass


def _require_enabled() -> None:
    if not is_agent_enabled():
        raise TransportInputError("Media Agent 当前未启用")


def _session_id(value: Any) -> str:
    normalized = str(value or "").strip()
    if not _SESSION_RE.fullmatch(normalized):
        raise TransportInputError("session_id 无效")
    return normalized


def _request_id(value: Any) -> str:
    normalized = str(value or "").strip()
    if normalized and not _REQUEST_RE.fullmatch(normalized):
        raise TransportInputError("request_id 无效")
    return normalized


_ACTIVE_PHASE_DETAILS = {
    "turn.started": "正在启动本轮任务",
    "capabilities.selected": "正在准备可用能力",
    "model.started": "正在思考",
    "model.delta": "正在整理回复",
    "model.tool_call": "正在规划下一步",
    "tool.started": "正在执行工具",
    "tool.progress": "正在处理任务",
    "tool.completed": "正在整理工具结果",
    "tool.failed": "正在处理工具异常",
    "effect.preview_started": "正在准备操作预览",
    "effect.approval_required": "正在准备确认信息",
    "effect.completed": "正在整理操作结果",
    "effect.failed": "正在整理操作结果",
    "turn.completed": "正在完成本轮任务",
    "turn.failed": "正在结束本轮任务",
    "turn.cancelled": "正在停止本轮任务",
}
_TERMINAL_TURNS = {
    "turn.completed": ("completed", "本轮已完成"),
    "turn.failed": ("failed", "本轮未能完成"),
    "turn.cancelled": ("cancelled", "本轮已取消"),
}
_KNOWN_TURN_EVENTS = frozenset(_ACTIVE_PHASE_DETAILS)


def _event_type(event: Any) -> str:
    return str(event.get("type") or "") if isinstance(event, dict) else ""


def _matches_turn(event: Any, turn: dict[str, Any]) -> bool:
    return bool(
        isinstance(event, dict)
        and event.get("turn_id") == turn.get("turn_id")
        and event.get("request_id") == turn.get("request_id")
    )


def _active_turn_view(
    activity: Any, events: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if not isinstance(activity, dict) or activity.get("status") not in {"running", "cancelling"}:
        return None
    turn = {
        "request_id": activity.get("request_id", ""),
        "turn_id": activity.get("turn_id", ""),
        "generation": activity.get("generation"),
        "protected": bool(activity.get("protected")),
        "status": activity["status"],
    }
    latest_type = next(
        (_event_type(event) for event in reversed(events) if _matches_turn(event, turn)),
        "",
    )
    turn["detail"] = (
        "正在停止本轮任务"
        if turn["status"] == "cancelling"
        else _ACTIVE_PHASE_DETAILS.get(latest_type, "正在处理本轮任务")
    )
    return turn


def _last_turn_view(
    events: list[dict[str, Any]], active_turn: dict[str, Any] | None
) -> dict[str, str] | None:
    if not events:
        return None
    event = events[-1]
    if not isinstance(event, dict):
        return None
    event_type = _event_type(event)
    request_id = event.get("request_id")
    turn_id = event.get("turn_id")
    if not isinstance(request_id, str) or not isinstance(turn_id, str):
        return None
    terminal = _TERMINAL_TURNS.get(event_type)
    if terminal is not None:
        status, message = terminal
    elif event_type in _KNOWN_TURN_EVENTS:
        if active_turn is not None and _matches_turn(event, active_turn):
            return None
        # 长回复的started事件可能已超出有界尾部；不能因此把未确认终态当完成。
        status, message = "interrupted", "本轮执行状态未确认，请核对已保存的结果；不会自动重放。"
    else:
        return None
    return {
        "request_id": request_id,
        "turn_id": turn_id,
        "status": status,
        "message": message,
    }


def _draft_scope(owner: str) -> str:
    secret = str(get_web_secret() or "")
    if not secret:
        raise RuntimeError("draft scope secret unavailable")
    return hmac.new(
        secret.encode(), b"mediaflux-agent-draft:v1\0" + owner.encode(), hashlib.sha256,
    ).hexdigest()


def _owner(request: Request) -> str:
    """把已登录 Web principal 绑定到稳定 owner，而不是临时 CSRF 会话。"""
    del request  # 鉴权由每个入口的 require_api_login 统一完成。
    username, _password = config.web_credentials()
    return web_kernel_owner(username)


def _check_rate_limit(
    request: Request,
    scope: str,
    *,
    limit: int,
    cost: int = 1,
) -> None:
    owner_digest = hashlib.sha256(
        b"mediaflux-agent-kernel-http-rate:v1\0" + _owner(request).encode()
    ).hexdigest()[:32]
    if not agent_rate_limiter.allow(
        f"webk:{owner_digest}:{scope}",
        limit=limit,
        window_seconds=60,
        cost=cost,
    ):
        raise AgentRateLimitError("Agent 请求过于频繁，请稍后重试")


def _error(exc: Exception):
    if isinstance(exc, AgentRateLimitError):
        return api_error(str(exc), 429)
    if isinstance(exc, (TransportInputError, SelectionInvalidError)):
        return api_error(str(exc), 400)
    if isinstance(exc, SessionBusyError):
        return api_response(
            {
                "error": "已确认写操作正在执行，当前会话暂不能重置或删除",
                "code": "effect_in_progress",
            },
            409,
        )
    logger.warning("Agent Kernel API 失败 type=%s", type(exc).__name__)
    return api_error("Media Agent 暂时不可用", 500)


@router.get("/capabilities")
async def capabilities(request: Request):
    require_api_login(request)
    try:
        _require_enabled()
        catalog = get_agent_kernel_runtime().session.catalog
        tools = [
            {"name": tool.name, "domain": tool.domain,
             "description": tool.description, "effect": tool.effect.value}
            for tool in catalog.visible({})
        ]
        return api_response({"tools": tools, "count": len(tools)})
    except Exception as exc:  # noqa: BLE001 - HTTP fault boundary
        return _error(exc)


@router.get("/next-actions")
async def next_actions(request: Request):
    require_api_login(request)
    try:
        _require_enabled()
        _check_rate_limit(request, "next-actions", limit=30)
        try:
            result = await asyncio.to_thread(summarize_workspace_next_actions, {})
            payload = next_actions_view(result)
        except Exception as exc:  # noqa: BLE001 - 本地快照不可用时不影响首页
            logger.warning("Agent 下一步快照不可用 type=%s", type(exc).__name__)
            payload = {"actions": [], "snapshot_status": "unavailable"}
        response = api_response(payload)
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except Exception as exc:  # noqa: BLE001 - HTTP fault boundary
        return _error(exc)


@router.get("/metrics")
async def metrics(request: Request):
    require_api_login(request)
    try:
        return api_response(get_agent_kernel_runtime().metrics.snapshot())
    except Exception as exc:  # noqa: BLE001 - HTTP fault boundary
        return _error(exc)


@router.post("/query")
async def query(request: Request, data: Annotated[Any, Body()] = None):
    require_api_login(request)
    if not isinstance(data, dict) or not set(data).issubset(
        {
            "message",
            "session_id",
            "stream",
            "request_id",
            "selection",
        }
    ):
        return api_error("请求字段无效", 400)
    try:
        _require_enabled()
        _check_rate_limit(request, "query", limit=20)
        message = data.get("message")
        if not isinstance(message, str):
            raise TransportInputError("message 必须是字符串")
        envelope = QueryEnvelope(
            owner=_owner(request),
            session_id=_session_id(data.get("session_id")),
            message=message,
            request_id=_request_id(data.get("request_id")),
            channel="web",
            selection=normalize_selection(data["selection"]) if "selection" in data else None,
        )
        envelope.to_agent_input()
        transport = get_agent_kernel_runtime().web
        if data.get("stream", True) is not False:
            return StreamingResponse(
                transport.query(envelope),
                media_type="application/x-ndjson",
                headers={
                    "Cache-Control": "no-store",
                    "X-Accel-Buffering": "no",
                },
            )
        return api_response((await transport.query_view(envelope)).to_dict())
    except Exception as exc:  # noqa: BLE001 - HTTP fault boundary
        return _error(exc)


@router.post("/query/cancel")
async def cancel_query(request: Request, data: Annotated[Any, Body()] = None):
    require_api_login(request)
    if not isinstance(data, dict) or not set(data).issubset(
        {"session_id", "request_id"}
    ):
        return api_error("请求字段无效", 400)
    try:
        request_id = _request_id(data.get("request_id"))
        if not request_id:
            raise TransportInputError("停止任务必须提供 request_id，不能通配取消当前会话")
        runtime = get_agent_kernel_runtime()
        cancelled = await runtime.web.cancel(
            owner=_owner(request),
            session_id=_session_id(data.get("session_id")),
            request_id=request_id,
        )
        return api_response(
            {
                "cancelled": cancelled,
                "request_id": request_id,
            }
        )
    except Exception as exc:  # noqa: BLE001 - HTTP fault boundary
        return _error(exc)


@router.post("/actions/confirm")
async def confirm_action(request: Request, data: Annotated[Any, Body()] = None):
    require_api_login(request)
    if not isinstance(data, dict) or not set(data).issubset(
        {
            "plan_id",
            "session_id",
            "request_id",
            "stream",
        }
    ):
        return api_error("请求字段无效", 400)
    try:
        _require_enabled()
        _check_rate_limit(request, "confirm", limit=12)
        envelope = EffectEnvelope(
            owner=_owner(request),
            session_id=_session_id(data.get("session_id")),
            plan_id=str(data.get("plan_id") or ""),
            request_id=_request_id(data.get("request_id")),
            channel="web",
        )
        transport = get_agent_kernel_runtime().web
        if data.get("stream") is True:
            return StreamingResponse(
                transport.confirm(envelope),
                media_type="application/x-ndjson",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
        view = await transport.confirm_view(envelope)
        status_code = 200 if view.status in {"effect_completed", "success", "partial", "approval_required"} else 409
        return api_response(view.to_dict(), status_code)
    except Exception as exc:  # noqa: BLE001 - HTTP fault boundary
        return _error(exc)


@router.post("/actions/confirm/discard")
async def discard_action(request: Request, data: Annotated[Any, Body()] = None):
    require_api_login(request)
    if not isinstance(data, dict) or not set(data).issubset(
        {
            "plan_id",
            "session_id",
            "request_id",
        }
    ):
        return api_error("请求字段无效", 400)
    try:
        _check_rate_limit(request, "discard", limit=20)
        envelope = EffectEnvelope(
            owner=_owner(request),
            session_id=_session_id(data.get("session_id")),
            plan_id=str(data.get("plan_id") or ""),
            request_id=_request_id(data.get("request_id")),
            channel="web",
        )
        discarded = await get_agent_kernel_runtime().web.cancel_effect(envelope)
        return api_response({"discarded": discarded})
    except Exception as exc:  # noqa: BLE001 - HTTP fault boundary
        return _error(exc)


@router.post("/session/reset")
async def reset_session(request: Request, data: Annotated[Any, Body()] = None):
    require_api_login(request)
    if not isinstance(data, dict) or set(data) != {"session_id"}:
        return api_error("请求字段无效", 400)
    try:
        owner = _owner(request)
        session_id = _session_id(data.get("session_id"))
        runtime = get_agent_kernel_runtime()
        state = await runtime.lifecycle.reset(owner=owner, session_id=session_id)
        return api_response({"session_id": session_id, "generation": state.generation})
    except Exception as exc:  # noqa: BLE001 - HTTP fault boundary
        return _error(exc)


@router.get("/sessions")
async def list_sessions(request: Request):
    require_api_login(request)
    try:
        owner = _owner(request)
        sessions = await get_agent_kernel_runtime().store.list_sessions(owner=owner)
        draft_scope = _draft_scope(owner)
        response = api_response({
            "sessions": sessions, "draft_scope": draft_scope,
            "scope": "recent_sessions",
        })
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except Exception as exc:  # noqa: BLE001 - HTTP fault boundary
        return _error(exc)


@router.patch("/sessions/{session_id}")
async def patch_session(request: Request, session_id: str, data: Annotated[Any, Body()] = None):
    require_api_login(request)
    # 写入口沿用主应用 SecurityMiddleware 的登录与 X-CSRF-Token 校验。
    try:
        _require_enabled()
        _check_rate_limit(request, "session-display", limit=30)
        normalized = _session_id(session_id)
        try:
            patch = session_display_patch(data)
        except ValueError as exc:
            raise TransportInputError(str(exc)) from exc
        summary = await get_agent_kernel_runtime().store.patch_session_display(
            owner=_owner(request), session_id=normalized, patch=patch,
        )
        if summary is None:
            return api_error("会话不存在", 404)
        return api_response({"session": summary})
    except Exception as exc:  # noqa: BLE001 - HTTP fault boundary
        return _error(exc)


@router.get("/sessions/{session_id}")
async def get_session(request: Request, session_id: str):
    require_api_login(request)
    try:
        normalized = _session_id(session_id)
        observed_request = _request_id(request.headers.get("X-Agent-Request-Id"))
        runtime = get_agent_kernel_runtime()
        owner = _owner(request)
        activity_before = await runtime.web.activity(owner=owner, session_id=normalized)
        # 终态事件在会话结果提交之后发布：先读事件再读状态，避免
        # 空→活动→空的快速轮次返回“新完成事件 + 旧对话”。
        events = await runtime.store.list_events(
            owner=owner, session_id=normalized, limit=64,
        )
        state = await runtime.store.load(owner=owner, session_id=normalized)
        activity_after = await runtime.web.activity(owner=owner, session_id=normalized)
        if activity_before != activity_after:
            events = await runtime.store.list_events(
                owner=owner, session_id=normalized, limit=64,
            )
            state = await runtime.store.load(owner=owner, session_id=normalized)

        active_turn = _active_turn_view(activity_after, events)
        candidate_view = await current_candidate_view(
            state=state, store=runtime.store,
        )
        messages = public_conversation_messages(state.conversation, candidate_view=candidate_view)
        pending_approval = None
        pending_plan_id = state.pending_effect_plan_id
        if pending_plan_id and not (active_turn and active_turn["protected"]):
            plan = await asyncio.to_thread(
                runtime.lifecycle.effect_store.get_active_plan,
                owner=owner,
                session_id=normalized,
                generation=state.generation,
                plan_id=pending_plan_id,
            )
            if plan is not None:
                pending_approval = plan.public_approval_dict()
        response = api_response(
            {
                "session_id": normalized,
                "draft_scope": _draft_scope(owner),
                "generation": state.generation,
                "messages": messages,
                "pending_approval": pending_approval,
                "candidate_view": candidate_view,
                "active_turn": active_turn,
                "last_turn": _last_turn_view(
                    [event for event in events if not observed_request or event.get("request_id") == observed_request],
                    active_turn,
                ),
            }
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except Exception as exc:  # noqa: BLE001 - HTTP fault boundary
        return _error(exc)


@router.delete("/sessions/{session_id}")
async def delete_session(request: Request, session_id: str):
    require_api_login(request)
    try:
        owner = _owner(request)
        normalized = _session_id(session_id)
        runtime = get_agent_kernel_runtime()
        deleted = await runtime.lifecycle.delete(owner=owner, session_id=normalized)
        return api_response({"deleted": deleted, "session_id": normalized})
    except Exception as exc:  # noqa: BLE001 - HTTP fault boundary
        return _error(exc)
