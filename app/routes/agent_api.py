"""新 Agent Kernel 的薄 Web API；不包含领域意图或业务状态机。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Body, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app import config
from app.agent.domains.resources.selection import (
    current_candidate_view,
    normalize_selection,
)
from app.agent.errors import AgentToolError, ToolPipelineError
from app.agent.feature_gate import is_agent_enabled
from app.agent.kernel.bootstrap import get_agent_kernel_runtime
from app.agent.kernel.capabilities import ToolEffect
from app.agent.kernel.continuation import ACTIVE as READ_WAIT_ACTIVE
from app.agent.kernel.state_types import SelectionInvalidError, SessionBusyError
from app.agent.kernel.task_state import task_projection
from app.agent.kernel.turn_display import active_turn_view, last_turn_view
from app.agent.kernel.ux_display import next_actions_view, session_display_patch
from app.agent.owner_routes import web_kernel_owner
from app.agent.public_view import public_active_turn_events, public_activities, public_conversation_messages
from app.agent.rate_limit import agent_rate_limiter
from app.agent.transports import (
    EffectEnvelope,
    ExplainEnvelope,
    QueryEnvelope,
    TransportInputError,
)
from app.modules.web_secret import get_web_secret
from app.modules.workspace import next_actions as read_next_actions
from app.modules.workspace import resolve_action_handoff
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


def _draft_scope(owner: str) -> str:
    # 使用本请求已捕获的登录主体，异步读取期间切换账号不改变草稿归属。
    secret = (get_web_secret() or "")
    if not secret:
        raise RuntimeError("draft scope secret unavailable")
    return hmac.new(
        secret.encode(), b"mediaflux-agent-draft:v1\0" + owner.encode(), hashlib.sha256,
    ).hexdigest()


def _owner() -> str:
    """Web 会话只属于登录主体；鉴权由入口 require_api_login 完成。"""
    username, _password = config.web_credentials()
    return web_kernel_owner(username)


def _check_rate_limit(
    scope: str,
    *,
    limit: int,
    cost: int = 1,
) -> None:
    owner_digest = hashlib.sha256(
        b"mediaflux-agent-kernel-http-rate:v1\0" + _owner().encode()
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
    if isinstance(exc, AgentToolError):
        return api_error(exc.safe_message, 409 if exc.code == "precondition_failed" else 400)
    if isinstance(exc, ToolPipelineError):
        return api_response({"error": str(exc), "code": exc.code,
                             "old_plan_invalidated": exc.plan_invalidated}, 409)
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
        catalog = (await run_in_threadpool(get_agent_kernel_runtime)).session.catalog
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
        _check_rate_limit("next-actions", limit=30)
        try:
            result = await asyncio.to_thread(read_next_actions)
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
        return api_response((await run_in_threadpool(get_agent_kernel_runtime)).metrics.snapshot())
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
            "action_key",
        }
    ):
        return api_error("请求字段无效", 400)
    try:
        _require_enabled()
        _check_rate_limit("query", limit=20)
        owner = _owner()
        session_id = _session_id(data.get("session_id"))
        request_id = _request_id(data.get("request_id"))
        message = data.get("message")
        initial_read_tool = ""
        initial_read_arguments = {}
        if "action_key" in data:
            if {"message", "selection"}.intersection(data):
                raise TransportInputError("工作区行动不能与 message 或 selection 混用")
            handoff = await asyncio.to_thread(resolve_action_handoff, {"action_key": data["action_key"]})
            message, initial_read_tool = handoff["label"], handoff["target_tool"]
            initial_read_arguments = handoff["arguments"]
        elif not isinstance(message, str):
            raise TransportInputError("message 必须是字符串")
        envelope = QueryEnvelope(
            owner=owner,
            session_id=session_id,
            message=message,
            request_id=request_id,
            channel="web",
            selection=normalize_selection(data["selection"]) if "selection" in data else None,
            initial_read_tool=initial_read_tool,
            initial_read_arguments=initial_read_arguments,
        )
        envelope.to_agent_input()
        runtime = await run_in_threadpool(get_agent_kernel_runtime)
        if initial_read_tool and (not runtime.session.catalog.has(initial_read_tool)
                or runtime.session.catalog.get(initial_read_tool).effect is not ToolEffect.READ):
            raise AgentToolError("该工作区只读行动当前不可用", code="precondition_failed")
        transport = runtime.web
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
        runtime = await run_in_threadpool(get_agent_kernel_runtime)
        cancelled = await runtime.web.cancel(
            owner=_owner(),
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


@router.post("/session/stop")
async def stop_session(request: Request, data: Annotated[Any, Body()] = None):
    require_api_login(request)
    if not isinstance(data, dict) or set(data) != {"session_id"}:
        return api_error("请求字段无效", 400)
    try:
        from app.agent.task_stop import stop_agent_session

        owner = _owner()
        session_id = _session_id(data.get("session_id"))
        runtime = await run_in_threadpool(get_agent_kernel_runtime)
        return api_response(await stop_agent_session(runtime, owner, session_id))
    except Exception as exc:  # noqa: BLE001 - HTTP fault boundary
        return _error(exc)


@router.post("/actions/explain")
async def explain_action(request: Request, data: Annotated[Any, Body()] = None):
    require_api_login(request)
    if not isinstance(data, dict) or not set(data).issubset({"session_id", "plan_id", "message", "request_id", "stream"}):
        return api_error("请求字段无效", 400)
    try:
        _require_enabled()
        _check_rate_limit("explain", limit=12)
        if not isinstance(data.get("message"), str) or ("stream" in data and type(data["stream"]) is not bool):
            raise TransportInputError("message 无效")
        envelope = ExplainEnvelope(owner=_owner(), session_id=_session_id(data.get("session_id")),
            plan_id=str(data.get("plan_id") or ""), message=data["message"],
            request_id=_request_id(data.get("request_id")), channel="web").normalized()
        transport = (await run_in_threadpool(get_agent_kernel_runtime)).web
        if data.get("stream") is True:
            return StreamingResponse(transport.explain(envelope), media_type="application/x-ndjson",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        view = await transport.explain_view(envelope)
        return api_response(view.to_dict(), 200 if view.status == "success" else 409)
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
        _check_rate_limit("confirm", limit=12)
        envelope = EffectEnvelope(
            owner=_owner(),
            session_id=_session_id(data.get("session_id")),
            plan_id=str(data.get("plan_id") or ""),
            request_id=_request_id(data.get("request_id")),
            channel="web",
        )
        transport = (await run_in_threadpool(get_agent_kernel_runtime)).web
        if data.get("stream") is True:
            return StreamingResponse(
                transport.confirm(envelope),
                media_type="application/x-ndjson",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
        view = await transport.confirm_view(envelope)
        status_code = 200 if view.terminal and view.status not in {"failed", "cancelled"} else 409
        return api_response(view.to_dict(), status_code)
    except Exception as exc:  # noqa: BLE001 - HTTP fault boundary
        return _error(exc)


@router.post("/actions/confirm/edit")
async def edit_confirmation_action(request: Request, data: Annotated[Any, Body()] = None):
    require_api_login(request)
    if (not isinstance(data, dict) or not set(data).issubset({"session_id", "plan_id", "arguments", "request_id"})
            or not isinstance(data.get("arguments"), dict)):
        return api_error("请求字段无效", 400)
    try:
        _require_enabled()
        _check_rate_limit("confirmation_edit", limit=12)
        envelope = EffectEnvelope(owner=_owner(), session_id=_session_id(data.get("session_id")),
                                  plan_id=str(data.get("plan_id") or ""), request_id=_request_id(data.get("request_id")), channel="web").normalized()
        runtime = await run_in_threadpool(get_agent_kernel_runtime)
        result = await runtime.session.edit_confirmation(
            owner=envelope.owner, session_id=envelope.session_id, plan_id=envelope.plan_id,
            arguments=data["arguments"], request_id=envelope.request_id, channel="web")
        return api_response({"plan": result.effect_plan.public_approval_dict() if result.effect_plan else None,
                             "result": dict(result.outcome.public_content), "old_plan_invalidated": True})
    except Exception as exc:
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
        _check_rate_limit("discard", limit=20)
        envelope = EffectEnvelope(
            owner=_owner(),
            session_id=_session_id(data.get("session_id")),
            plan_id=str(data.get("plan_id") or ""),
            request_id=_request_id(data.get("request_id")),
            channel="web",
        )
        runtime = await run_in_threadpool(get_agent_kernel_runtime)
        discarded = await runtime.web.cancel_effect(envelope)
        return api_response({"discarded": discarded})
    except Exception as exc:  # noqa: BLE001 - HTTP fault boundary
        return _error(exc)


@router.post("/actions/trust")
async def scoped_trust_action(request: Request, data: Annotated[Any, Body()] = None):
    require_api_login(request)
    if (not isinstance(data, dict) or not set(data).issubset(
            {"session_id", "request_id", "operation", "plan_id", "tool_name", "scope", "ttl_seconds", "max_actions"})
            or data.get("operation") not in {"grant", "revoke"}):
        return api_error("请求字段无效", 400)
    try:
        _require_enabled()
        _check_rate_limit("scoped_trust", limit=12)
        selected = {key: data[key] for key in ("plan_id", "tool_name", "scope", "ttl_seconds", "max_actions") if key in data}
        runtime = await run_in_threadpool(get_agent_kernel_runtime)
        result = await runtime.session.set_scoped_trust(
            owner=_owner(), session_id=_session_id(data.get("session_id")),
            request_id=_request_id(data.get("request_id")), operation=data["operation"], **selected)
        return api_response(result)
    except Exception as exc:
        return _error(exc)


@router.post("/session/reset")
async def reset_session(request: Request, data: Annotated[Any, Body()] = None):
    require_api_login(request)
    if not isinstance(data, dict) or set(data) != {"session_id"}:
        return api_error("请求字段无效", 400)
    try:
        owner = _owner()
        session_id = _session_id(data.get("session_id"))
        runtime = await run_in_threadpool(get_agent_kernel_runtime)
        state = await runtime.lifecycle.reset(owner=owner, session_id=session_id)
        return api_response({"session_id": session_id, "generation": state.generation})
    except Exception as exc:  # noqa: BLE001 - HTTP fault boundary
        return _error(exc)


@router.get("/sessions")
async def list_sessions(request: Request):
    require_api_login(request)
    try:
        owner = _owner()
        draft_scope = _draft_scope(owner)
        runtime = await run_in_threadpool(get_agent_kernel_runtime)
        sessions = await runtime.store.list_sessions(owner=owner)
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
        _check_rate_limit("session-display", limit=30)
        normalized = _session_id(session_id)
        try:
            patch = session_display_patch(data)
        except ValueError as exc:
            raise TransportInputError(str(exc)) from exc
        runtime = await run_in_threadpool(get_agent_kernel_runtime)
        summary = await runtime.store.patch_session_display(
            owner=_owner(), session_id=normalized, patch=patch,
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
        runtime = await run_in_threadpool(get_agent_kernel_runtime)
        owner = _owner()
        draft_scope = _draft_scope(owner)
        activity_before = await runtime.web.activity(owner=owner, session_id=normalized)
        # 终态事件在会话结果提交之后发布：先读事件再读状态，避免
        # 空→活动→空的快速轮次返回“新完成事件 + 旧对话”。
        events = await runtime.store.list_events(
            owner=owner, session_id=normalized, limit=500 if activity_before else 64,
        )
        turn_events = (await runtime.store.list_events(owner=owner, session_id=normalized,
            request_id=activity_before["request_id"], turn_id=activity_before["turn_id"])) if activity_before else []
        state = await runtime.store.load(owner=owner, session_id=normalized)
        activity_after = await runtime.web.activity(owner=owner, session_id=normalized)
        if activity_before != activity_after:
            events = await runtime.store.list_events(
                owner=owner, session_id=normalized, limit=500 if activity_after else 64,
            )
            turn_events = (await runtime.store.list_events(owner=owner, session_id=normalized,
                request_id=activity_after["request_id"], turn_id=activity_after["turn_id"])) if activity_after else []
            state = await runtime.store.load(owner=owner, session_id=normalized)

        active_turn = active_turn_view(activity_after, events)
        if active_turn:
            active_turn["events"] = public_active_turn_events(turn_events, active_turn)
        candidate_view = await current_candidate_view(
            state=state, store=runtime.store,
        )
        messages = public_conversation_messages(state.conversation, candidate_view=candidate_view)
        effect_waits = getattr(state, "metadata", {}).get("effect_waits", {})
        wait_records = effect_waits.values() if isinstance(effect_waits, Mapping) else ()
        from app.agent.kernel.completion_receipts import EFFECT_PENDING
        pending_effect_count = sum(
            (not wait.get("delivered") and not wait.get("receipt_message"))
            or (wait.get("continuation") or {}).get("state") in EFFECT_PENDING
            for wait in wait_records
        )
        # 沿用公开计数字段；READ 等待与已确认后台操作共用结果观察器。
        pending_effect_count += (getattr(state, "metadata", {}).get("read_wait") or {}).get("state") in READ_WAIT_ACTIVE
        pending_approval = None
        pending_plan_id = state.pending_effect_plan_id
        if pending_plan_id and not (active_turn and active_turn["protected"]):
            plan = await runtime.session.pipeline.get_pending_plan(owner=owner, session_id=normalized)
            if plan is not None:
                pending_approval = plan.public_approval_dict()
                preview = next((row for row in reversed(state.conversation)
                                if row.get("role") == "tool" and row.get("effect_plan_id") == plan.plan_id), {})
                activities = public_activities(preview.get("activities"))
                for activity in activities:
                    if activity.get("method") == plan.tool_name and activity.get("status") == "running":
                        activity["status"] = "approval_required"
                if activities:
                    pending_approval["activities"] = activities
        last_turn = last_turn_view(
            [event for event in events if not observed_request or event.get("request_id") == observed_request],
            active_turn,
        )
        terminal_payload = next((event.get("payload", {}) for event in reversed(events)
                                 if last_turn and event.get("turn_id") == last_turn["turn_id"]), {})
        if not active_turn and last_turn and last_turn["status"] == "waiting":
            # 等待提示属于公开响应，不写入原任务的模型历史；恢复沿用同一文本投影。
            answer = public_conversation_messages([{"role": "assistant", "content": terminal_payload.get("answer", "")}])
            if answer and (not messages or messages[-1] != answer[-1]):
                messages.extend(answer)
        response = api_response(
            {
                "session_id": normalized,
                "draft_scope": draft_scope,
                "generation": state.generation,
                "task": task_projection(getattr(state, "metadata", {}).get("task")),
                "turn_status": active_turn["status"] if active_turn else terminal_payload.get("turn_status", terminal_payload.get("status", (last_turn or {}).get("status"))),
                "messages": messages,
                "pending_approval": pending_approval,
                "scoped_trust": runtime.session.pipeline.scoped_trust.view(state),
                "candidate_view": candidate_view,
                "active_turn": active_turn,
                "pending_effect_count": pending_effect_count,
                "last_turn": last_turn,
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
        owner = _owner()
        normalized = _session_id(session_id)
        runtime = await run_in_threadpool(get_agent_kernel_runtime)
        deleted = await runtime.lifecycle.delete(owner=owner, session_id=normalized)
        return api_response({"deleted": deleted, "session_id": normalized})
    except Exception as exc:  # noqa: BLE001 - HTTP fault boundary
        return _error(exc)
