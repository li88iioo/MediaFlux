"""新 Agent Kernel 的薄 Web API；不包含领域意图或业务状态机。"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Annotated, Any

from fastapi import APIRouter, Body, Request
from fastapi.responses import StreamingResponse

from app import config
from app.agent.feature_gate import is_agent_enabled
from app.agent.kernel.bootstrap import get_agent_kernel_runtime
from app.agent.kernel.public_view import public_conversation_messages
from app.agent.kernel.state import SessionBusyError
from app.agent.kernel.transports import (
    EffectEnvelope,
    QueryEnvelope,
    TransportInputError,
)
from app.agent.owner_routes import web_kernel_owner
from app.agent.rate_limit import agent_rate_limiter
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
    if isinstance(exc, TransportInputError):
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
        return api_response(
            {
                "tools": [
                    {
                        "name": tool.name,
                        "domain": tool.domain,
                        "description": tool.description,
                        "effect": tool.effect.value,
                    }
                    for tool in catalog.visible({})
                ],
                "count": len(catalog),
            }
        )
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
        )
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
        runtime = get_agent_kernel_runtime()
        cancelled = await runtime.web.cancel(
            owner=_owner(request),
            session_id=_session_id(data.get("session_id")),
        )
        return api_response(
            {
                "cancelled": cancelled,
                "request_id": _request_id(data.get("request_id")),
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
        status_code = 200 if view.status == "effect_completed" else 409
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
        sessions = await get_agent_kernel_runtime().store.list_sessions(
            owner=_owner(request)
        )
        return api_response({"sessions": sessions})
    except Exception as exc:  # noqa: BLE001 - HTTP fault boundary
        return _error(exc)


@router.get("/sessions/{session_id}")
async def get_session(request: Request, session_id: str):
    require_api_login(request)
    try:
        normalized = _session_id(session_id)
        state = await get_agent_kernel_runtime().store.load(
            owner=_owner(request),
            session_id=normalized,
        )
        messages = public_conversation_messages(state.conversation)
        pending_approval = None
        if state.pending_effect_plan_id:
            events = await get_agent_kernel_runtime().store.list_events(
                owner=_owner(request),
                session_id=normalized,
                limit=200,
            )
            for event in reversed(events):
                if (
                    not isinstance(event, dict)
                    or event.get("type") != "effect.approval_required"
                ):
                    continue
                payload = event.get("payload")
                plan = payload.get("plan") if isinstance(payload, dict) else None
                if (
                    isinstance(plan, dict)
                    and str(plan.get("plan_id") or "") == state.pending_effect_plan_id
                ):
                    pending_approval = {
                        "plan_id": str(plan.get("plan_id") or ""),
                        "tool_name": str(
                            plan.get("tool_name") or payload.get("tool") or ""
                        ),
                        "effect": str(plan.get("effect") or "WRITE"),
                        "preview": plan.get("preview")
                        if isinstance(plan.get("preview"), dict)
                        else {},
                        "result": payload.get("result")
                        if isinstance(payload.get("result"), dict)
                        else {},
                        "confirmation": plan.get("confirmation")
                        if isinstance(plan.get("confirmation"), dict)
                        else {},
                        "expires_at": str(plan.get("expires_at") or ""),
                    }
                    break
        return api_response(
            {
                "session_id": normalized,
                "generation": state.generation,
                "messages": messages,
                "pending_approval": pending_approval,
            }
        )
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
