"""Media Agent API：只读查询与必须确认的受控动作。"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from app import config
from app.agent.action_plan_id import normalize_action_plan_id
from app.agent.confirmation import confirmation_reply_intent
from app.agent.conversation_compaction import schedule_conversation_compaction
from app.agent.metrics import agent_metrics
from app.agent.conversation_history import get_agent_conversation_history_repository
from app.agent.owner_routes import web_agent_owner
from app.agent.local_media_intents import (
    is_local_media_diagnosis_message,
    is_local_media_history_summary_message,
    is_local_media_review_queue_summary_message,
)
from app.agent.query_lifecycle import (
    begin_query_confirmation_epoch,
    invalidate_query_confirmation_epoch,
)
from app.agent.orchestrator import (
    AgentInputError,
    agent_action_history_request,
    config_component_explain_request,
    is_automation_pipeline_diagnosis_message,
    is_bangumi_calendar_message,
    is_discovery_recommend_message,
    is_discovery_search_message,
    is_download_queue_diagnosis_message,
    download_retry_submission_request,
    is_episode_audit_message,
    is_feature_summary_message,
    is_feature_state_message,
    guangya_organize_schedule_policy_request,
    is_guangya_connection_status_message,
    is_guangya_organize_clean_empty_message,
    is_guangya_organize_schedule_policy_summary_message,
    is_guangya_organize_preview_message,
    is_guangya_organize_run_message,
    is_guangya_organize_stop_message,
    is_indexer_readiness_diagnosis_message,
    is_indexer_resource_search_message,
    indexer_sites_request,
    indexer_site_change_request,
    is_library_update_check_message,
    is_library_episode_patrol_message,
    is_library_patrol_policy_summary_message,
    is_library_patrol_status_message,
    library_patrol_policy_request,
    is_recent_library_patrol_resource_message,
    is_recent_resource_submit_message,
    is_recent_download_explanation_message,
    is_recent_download_library_verification_message,
    is_recent_download_status_message,
    is_media_server_diagnosis_message,
    is_media_server_test_message,
    is_media_proxy_status_summary_message,
    is_telegram_test_notification_message,
    media_proxy_instance_enabled_request,
    media_proxy_test_request,
    is_missing_episode_resource_search_message,
    is_missing_season_resource_search_message,
    is_rss_diagnosis_message,
    rss_failure_retry_request,
    rss_pending_download_request,
    rss_subscription_refresh_request,
    is_strm_failure_triage_message,
    organize_audit_request,
    recognition_rule_enabled_request,
    strm_failure_retry_request,
    is_workspace_briefing_message,
    is_workspace_health_message,
    is_workspace_next_actions_message,
    is_workspace_search_message,
    is_workspace_todo_message,
    is_web_search_message,
    normalize_agent_message,
    is_safe_policy_summary_message,
    safe_policy_request,
)
from app.agent.rate_limit import agent_rate_limiter, allow_agent_tool
from app.agent.feature_gate import AgentRuntimeDisabled, agent_runtime_admission
from app.agent.registry import AgentToolError
from app.agent.response_contract import response_contract
from app.agent.result_projection import (
    attach_public_fallback_presentation,
    project_agent_result_for_user,
    project_public_guidance,
    project_public_notices,
    public_tool_label,
    sanitize_public_multiline_text,
    sanitize_public_text,
)
from app.agent.presentation_stream import (
    PublicNarrativeProjector,
    PublicNarrativeValidationError,
    apply_streamed_answer,
    select_agent_answer_stream,
)
from app.agent.service import get_agent_service
from app.agent.state_commit import (
    AgentStateCommitBuffer,
    defer_agent_state_commits,
)
from app.agent.feature_gate import (
    agent_runtime_generation_is_current,
    current_agent_runtime_generation,
    is_agent_enabled,
)
from app.agent.llm_router import (
    begin_llm_request_budget,
    reset_llm_request_budget,
    stream_existing_answer,
    stream_tool_answer,
)
from app.agent.operation_coordinator import (
    AgentOperationLease,
    get_agent_operation_coordinator,
)
from app.clients.openai_compatible import ProviderStreamError
from app.web import api_error, api_response, csrf_token, require_api_login


def _metrics_scrape_authorized(request: Request) -> bool:
    """允许监控系统用独立 Bearer Key 抓取且不扩散到其他 Agent API。"""
    expected = str(config.get("AGENT_METRICS_SCRAPE_KEY", "") or "").strip()
    if not 24 <= len(expected) <= 512 or any(ord(char) < 33 for char in expected):
        return False
    authorization = str(request.headers.get("authorization") or "")
    if len(authorization) > 520 or not authorization.lower().startswith("bearer "):
        return False
    supplied = authorization[7:]
    if not supplied or supplied != supplied.strip():
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _require_agent_enabled(request: Request) -> None:
    scrape_request = (
        request.url.path == "/api/agent/metrics"
        and _metrics_scrape_authorized(request)
    )
    if not scrape_request:
        require_api_login(request)
    if not is_agent_enabled():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "agent_disabled",
                "message": "Media Agent 已关闭，请先在设置中启用",
            },
        )


router = APIRouter(
    prefix="/api/agent",
    dependencies=[Depends(_require_agent_enabled)],
)
logger = logging.getLogger(__name__)
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def _client_key(request: Request, scope: str) -> str:
    try:
        principal = csrf_token(request)
    except Exception:
        principal = ""
    if principal:
        digest = hashlib.sha256(
            b"mediaflux-agent-http-rate:v1\0" + principal.encode("utf-8")
        ).hexdigest()[:32]
        return f"principal:{digest}:{scope}"
    host = request.client.host if request.client else "unknown"
    return f"host:{host}:{scope}"


def _check_rate_limit(request: Request, scope: str, *, limit: int, cost: int = 1) -> None:
    if not agent_rate_limiter.allow(
        _client_key(request, scope), limit=limit, window_seconds=60, cost=cost
    ):
        raise AgentToolError("Agent 请求过于频繁，请稍后重试", code="rate_limited")


def _prepare_rate_limit(tool_name: str) -> int:
    tool_name = str(tool_name or "").strip()
    return {
        "config.set_feature_state": 4,
        "config.set_indexer_sites": 4,
        "config.set_safe_policy": 4,
        "library.set_patrol_policy": 4,
        "library.trigger_patrol_now": 2,
        "guangya.organize.cleanup.execute": 3,
        "guangya.rename.execute": 3,
        "guangya.organize.set_schedule_policy": 4,
        "guangya.directory_scrape.run": 3,
        "guangya.organize.run_once": 4,
        "guangya.organize.stop": 4,
        "indexer.submit_candidate": 6,
        "indexer.submit_candidates": 4,
        "strm.retry_failures": 3,
        "rss.refresh_subscription": 3,
        "rss.refresh_subscriptions": 3,
        "rss.create_subscription": 4,
        "rss.update_subscription": 4,
        "rss.mark_entries": 4,
        "rss.submit_entries_to_qb": 3,
        "rss.submit_pending_to_qb": 3,
        "rss.retry_failed_to_qb": 3,
        "downloads.retry_submission": 3,
        "discovery.confirm_mapping": 4,
        "media_proxy.set_instance_enabled": 4,
        "media_proxy.restart_instance": 3,
        "local_media.scan_sources": 3,
        "recognition.set_rule_enabled": 4,
    }.get(tool_name, 10)


def _agent_error(exc: AgentToolError):
    status_code = {
        "tool_not_found": 404,
        "confirmation_required": 409,
        "confirmation_invalid": 409,
        "confirmation_stale": 409,
        "confirmation_not_supported": 409,
        "confirmation_unavailable": 503,
        "agent_unavailable": 503,
        "selection_required": 409,
        "precondition_failed": 409,
        "rate_limited": 429,
    }.get(exc.code, 400)
    return api_error(exc.safe_message, status_code)


def _service_is_read_tool(service: Any, tool_name: str) -> bool:
    """严格读取服务能力；接口缺失或返回非布尔值时拒绝直调。"""
    try:
        value = service.is_read_tool(tool_name)
    except AgentToolError:
        raise
    except Exception as exc:
        raise AgentToolError(
            "Agent 服务能力暂不可用，请稍后重试",
            code="agent_unavailable",
        ) from exc
    if type(value) is not bool:
        raise AgentToolError(
            "Agent 服务能力暂不可用，请稍后重试",
            code="agent_unavailable",
        )
    return value


def _agent_runtime_retry_response(exc: AgentRuntimeDisabled):
    return api_response(
        {
            "error": str(exc),
            "code": "agent_runtime_disabled",
            "retryable": True,
        },
        409,
    )


def _cancel_runtime_changed_operation(
    *, service: Any, operation: AgentOperationLease
) -> bool:
    """只撤销仍由本操作持有的确认 epoch，避免旧请求误伤后继请求。"""
    return get_agent_operation_coordinator().cancel(
        owner=operation.owner,
        operation_id=operation.operation_id,
        reason="runtime_changed",
        remember=False,
        invalidate=lambda: invalidate_query_confirmation_epoch(
            service, owner=operation.owner
        ),
    )


def _action_plan_id(value: Any) -> str:
    if not isinstance(value, str):
        raise AgentToolError("plan_id 必须是字符串")
    token = normalize_action_plan_id(value)
    if not token:
        raise AgentToolError("plan_id 无效")
    return token


def _session_id(value: Any) -> str:
    if not isinstance(value, str):
        raise AgentToolError("session_id 必须是字符串")
    session_id = value.strip()
    if not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise AgentToolError("session_id 无效")
    return session_id


def _request_id(value: Any, *, required: bool = False) -> str:
    if value is None and not required:
        return secrets.token_urlsafe(18)
    if not isinstance(value, str):
        raise AgentToolError("request_id 必须是字符串")
    request_id = value.strip()
    if not _REQUEST_ID_PATTERN.fullmatch(request_id):
        raise AgentToolError("request_id 无效")
    return request_id


def _agent_owner(request: Request, data: dict[str, Any]) -> str:
    """为 Web Agent 会话生成不可逆的隔离 owner。"""
    session_owner = csrf_token(request)
    return web_agent_owner(
        session_owner, session_id=_session_id(data.get("session_id"))
    )


def _agent_llm_rate_owner(request: Request) -> str:
    """LLM 外部调用按登录会话限流，不接受客户端 session_id 扩容。"""
    digest = hashlib.sha256(
        b"mediaflux-agent-llm-rate:v1\0" + csrf_token(request).encode("utf-8")
    ).hexdigest()
    return f"web-rate:v1:{digest}"


def _agent_tool_rate_owner(request: Request) -> str:
    """工具预算绑定已认证浏览器会话，避免代理出口 IP 让所有用户互相挤占。"""
    digest = hashlib.sha256(
        b"mediaflux-agent-tool-rate:v1\0" + csrf_token(request).encode("utf-8")
    ).hexdigest()
    return f"web-tool-rate:v1:{digest}"


def _agent_history_principal(request: Request) -> str:
    """历史按当前已登录浏览器会话隔离；原始凭据不会写入数据库。"""
    return csrf_token(request)


def _history_generation(request: Request, *, session_id: str) -> int | None:
    try:
        return get_agent_conversation_history_repository().session_generation(
            principal=_agent_history_principal(request),
            session_id=session_id,
        )
    except Exception as exc:
        # 无法取得删除 epoch 时宁可不归档，避免晚到请求复活已删除会话。
        logger.warning("Agent 对话历史 epoch 读取失败 type=%s", type(exc).__name__)
        return None


def _direct_tool_history_message(tool_name: str, arguments: dict[str, Any]) -> str:
    """为只读直调生成可持久化、无客户端自定义文案的用户消息。"""
    if tool_name == "indexer.search_resources":
        title = " ".join(str(arguments.get("title") or "").split())[:120]
        if title:
            return f"搜索《{title}》资源候选"
    return f"执行只读检查 · {public_tool_label(tool_name)}"


def _confirmation_history_message(response: dict[str, Any]) -> str:
    """为确认结果生成不可重放、无确认票据的安全用户消息。"""
    tool_call = response.get("tool_call") if isinstance(response, dict) else None
    tool_name = tool_call.get("name") if isinstance(tool_call, dict) else None
    normalized = str(tool_name or "").strip()[:120]
    return f"确认并执行 · {public_tool_label(normalized)}" if normalized else "确认并执行受控操作"


def _public_history_text(value: Any, *, limit: int, fallback: str = "") -> str:
    """把历史中的旧内部标识转换为稳定公开文案。"""
    return sanitize_public_text(value, limit=limit) or fallback


def _public_history_multiline_text(
    value: Any, *, limit: int, fallback: str = ""
) -> str:
    return sanitize_public_multiline_text(value, limit=limit) or fallback


def _public_session_projection(session: dict[str, Any]) -> dict[str, Any]:
    """只向浏览器返回面向用户的历史投影，不暴露内部工具协议。"""
    projected = {
        key: value
        for key, value in session.items()
        if key != "messages"
    }
    messages: list[dict[str, Any]] = []
    for item in session.get("messages") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        public_data = dict(data)
        if role == "assistant":
            tool_name = str(public_data.pop("tool_name", "") or "").strip()
            if tool_name:
                public_data["tool_label"] = public_tool_label(tool_name)
            public_data["summary"] = _public_history_multiline_text(
                public_data.get("summary"),
                limit=600,
                fallback="Agent 已返回结果",
            )
            narrative = _public_history_multiline_text(
                public_data.get("narrative"), limit=1200
            )
            if narrative:
                public_data["narrative"] = narrative
            else:
                public_data.pop("narrative", None)
            error = _public_history_text(public_data.get("error"), limit=300)
            if error:
                public_data["error"] = error
            else:
                public_data.pop("error", None)
            suggestions = [
                text
                for value in (public_data.get("suggestions") or [])[:4]
                if (text := _public_history_text(value, limit=180))
            ]
            public_data["suggestions"] = suggestions
            guidance = project_public_guidance(suggestions)
            if guidance:
                public_data["guidance"] = guidance
            stored_notices = [
                text
                for value in (public_data.get("notices") or [])[:3]
                if (text := _public_history_text(value, limit=220))
            ]
            notices = stored_notices or project_public_notices(suggestions)
            if notices:
                public_data["notices"] = notices
            else:
                public_data.pop("notices", None)
            if public_data.get("presentation_source") not in {"llm", "system", "native"}:
                public_data.pop("presentation_source", None)
        elif role == "user":
            public_data["text"] = _public_history_multiline_text(
                public_data.get("text"),
                limit=1000,
                fallback="历史消息已隐藏",
            )
        messages.append({
            "role": role,
            "data": public_data,
            "created_at": str(item.get("created_at") or ""),
        })
    projected["messages"] = messages
    return projected


def _conversation_context(request: Request, *, session_id: str) -> list[dict[str, Any]]:
    """读取已脱敏的历史摘要，供 LLM 理解追问；不包含工具原始数据。"""
    try:
        context = get_agent_conversation_history_repository().get_llm_context(
            principal=_agent_history_principal(request),
            session_id=session_id,
            tail_limit=10,
        )
    except Exception as exc:
        logger.warning("Agent 对话上下文读取失败 type=%s", type(exc).__name__)
        return []
    return context if isinstance(context, list) else []


def _record_query_history(
    request: Request,
    *,
    session_id: str,
    message: str,
    response: dict[str, Any],
    expected_generation: int | None,
) -> None:
    if expected_generation is None:
        return
    principal = _agent_history_principal(request)
    repository = get_agent_conversation_history_repository()
    try:
        persisted = repository.append_query_turn(
            principal=principal,
            session_id=session_id,
            message=message,
            response=response,
            expected_generation=expected_generation,
        )
        if persisted:
            schedule_conversation_compaction(
                principal=principal,
                session_id=session_id,
                llm_owner=_agent_llm_rate_owner(request),
                repository=repository,
            )
    except Exception as exc:
        # 历史记录是辅助能力，失败不得吞掉已完成的 Agent 查询。
        logger.warning("Agent 对话历史写入失败 type=%s", type(exc).__name__)


def _ndjson_event(event_type: str, **payload: Any) -> bytes:
    """编码单个 Agent 流事件；每行都是独立、有限的 UTF-8 JSON。"""
    body = {"type": event_type, **payload}
    return (
        json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _public_deterministic_fallback_response(
    response: dict[str, Any],
    *,
    request_id: str,
) -> dict[str, Any]:
    """流叙述越界时，只保留已经过公开投影的确定性结果。"""
    result = response.get("result")
    result = result if isinstance(result, dict) else {}
    # 不信任调用方附带的 display；在安全边界内从原结果重新投影。
    display = project_agent_result_for_user(result)

    status = sanitize_public_text(result.get("status"), limit=64) or "unknown"
    summary = sanitize_public_multiline_text(display.get("summary"), limit=1200)
    error = sanitize_public_multiline_text(display.get("error"), limit=420)
    details = display.get("details")
    if not isinstance(details, (dict, list)):
        details = {}

    safe_tool_call: dict[str, Any] | None = None
    tool_call = response.get("tool_call")
    if isinstance(tool_call, dict):
        tool_name = str(tool_call.get("name") or "").strip()
        if re.fullmatch(r"[a-z][a-z0-9_.]{0,95}", tool_name):
            safe_tool_call = {"name": tool_name}
            elapsed_ms = tool_call.get("elapsed_ms")
            if isinstance(elapsed_ms, int) and not isinstance(elapsed_ms, bool):
                safe_tool_call["elapsed_ms"] = min(max(0, elapsed_ms), 86_400_000)

    raw_mode = str(response.get("mode") or "read_only").strip().casefold()
    mode = raw_mode if raw_mode in {
        "read_only", "read_plan", "conversation", "clarification",
        "tool_result", "confirmed_action",
    } else "read_only"
    safe_request_id = (
        request_id if _REQUEST_ID_PATTERN.fullmatch(str(request_id or "")) else ""
    )
    safe_contract = response_contract(response)
    projected = {
        "request_id": safe_request_id,
        "mode": mode,
        "tool_call": safe_tool_call,
        **({"response_contract": safe_contract} if safe_contract else {}),
        "result": {
            "ok": bool(result.get("ok")),
            "status": status,
            "summary": summary or "检查已完成。",
            "error": error if not result.get("ok") else "",
            "suggestions": [],
            "data": details,
            "evidence": [],
        },
        "display": display,
    }
    projected = attach_public_fallback_presentation(projected)
    presentation = projected.get("presentation")
    if isinstance(presentation, dict):
        safe_presentation = dict(presentation)
        guidance = display.get("guidance")
        notices = display.get("notices")
        if isinstance(guidance, list) and guidance:
            safe_presentation["guidance"] = guidance
        if isinstance(notices, list) and notices:
            safe_presentation["notices"] = notices
        projected["presentation"] = safe_presentation
    return projected


async def _stream_query_events(
    request: Request,
    *,
    message: str,
    service: Any,
    query_kwargs: dict[str, Any],
    llm_owner: str,
    session_id: str | None,
    history_generation: int | None,
    operation: AgentOperationLease,
    runtime_generation: int | None = None,
) -> AsyncIterator[bytes]:
    """流式执行 Agent 查询，并只允许当前操作发布与落库。"""
    coordinator = get_agent_operation_coordinator()
    if runtime_generation is None:
        runtime_generation = current_agent_runtime_generation()
    state_buffer = AgentStateCommitBuffer(owner=operation.owner)
    llm_budget_token = begin_llm_request_budget(llm_owner)

    def cancelled_event() -> bytes:
        return _ndjson_event(
            "cancelled",
            request_id=operation.operation_id,
            reason=coordinator.reason(operation) or "cancelled",
            message="本次任务已停止，结果未写入会话历史。",
        )

    def current_event(
        event_type: str,
        **payload: Any,
    ) -> bytes | None:
        # 只在进程内锁中完成“当前请求”判定与事件快照生成。真正向 ASGI
        # yield 必须发生在锁外，否则慢客户端会阻塞同会话的取消/重置。
        if not agent_runtime_generation_is_current(runtime_generation):
            _cancel_runtime_changed_operation(service=service, operation=operation)
            return None
        published, event = coordinator.publish_if_current(
            operation,
            lambda: _ndjson_event(
                event_type,
                request_id=operation.operation_id,
                **payload,
            ),
        )
        return event if published else None

    try:
        event = current_event("status", phase="routing")
        if event is None:
            yield cancelled_event()
            return
        yield event

        def execute_query() -> dict[str, Any]:
            with defer_agent_state_commits(state_buffer):
                return service.query(
                    message,
                    present=False,
                    **query_kwargs,
                )

        query_task = asyncio.create_task(asyncio.to_thread(execute_query))

        def consume_detached_query(task: asyncio.Task[Any]) -> None:
            # Python 无法安全终止已进入线程池的同步调用；撤销时立即收回发布权，
            # 后台调用完成后仅消费异常，绝不再写历史、票据或客户端事件。
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.info(
                    "已撤销 Agent 后台查询结束 type=%s", type(exc).__name__
                )

        try:
            while True:
                done, _pending = await asyncio.wait({query_task}, timeout=0.1)
                if query_task in done:
                    response = query_task.result()
                    break
                if (
                    not coordinator.is_current(operation)
                    or not agent_runtime_generation_is_current(runtime_generation)
                ):
                    if not agent_runtime_generation_is_current(runtime_generation):
                        _cancel_runtime_changed_operation(
                            service=service, operation=operation
                        )
                    query_task.add_done_callback(consume_detached_query)
                    yield cancelled_event()
                    return
                if await request.is_disconnected():
                    coordinator.cancel(
                        owner=operation.owner,
                        operation_id=operation.operation_id,
                        reason="client_disconnected",
                        remember=False,
                        invalidate=lambda: invalidate_query_confirmation_epoch(
                            service, owner=operation.owner
                        ),
                    )
                    query_task.add_done_callback(consume_detached_query)
                    return
        except (AgentInputError, AgentToolError) as exc:
            safe_message = (
                str(exc)
                if isinstance(exc, AgentInputError)
                else exc.safe_message
            )
            event = current_event(
                "error",
                code=getattr(exc, "code", "invalid_request"),
                message=safe_message,
            )
            if event is None:
                yield cancelled_event()
            else:
                yield event
            return
        except Exception as exc:
            logger.warning("Agent 查询执行失败 type=%s", type(exc).__name__)
            event = current_event(
                "error",
                code="agent_unavailable",
                message="Agent 暂时不可用，请稍后重试。",
            )
            if event is None:
                yield cancelled_event()
            else:
                yield event
            return

        event = current_event("status", phase="reviewing")
        if event is None:
            yield cancelled_event()
            return
        yield event

        trace = response.get("agent_trace")
        if isinstance(trace, list):
            for index, item in enumerate(trace[:8], start=1):
                if not isinstance(item, dict):
                    continue
                event = current_event(
                    "step",
                    step="tool_finish",
                    phase="running",
                    index=index,
                    label=str(item.get("label") or "检查")[:80],
                    ok=item.get("ok") is not False,
                    summary=str(item.get("summary") or "")[:240],
                )
                if event is not None:
                    yield event
            event = current_event(
                "step",
                step="summary",
                phase="answering",
                label="正在组织答复…",
            )
            if event is not None:
                yield event

        stream = select_agent_answer_stream(
            message,
            response,
            owner=llm_owner,
            tool_stream_factory=stream_tool_answer,
            conversation_stream_factory=stream_existing_answer,
        )

        projector = PublicNarrativeProjector()
        emitted = False
        deterministic_public_fallback = False

        if stream is None and isinstance(trace, list):
            presentation = response.get("presentation")
            narrative = (
                str(presentation.get("narrative") or "")
                if isinstance(presentation, dict) else ""
            )
            if narrative:
                async def _native_narrative_stream() -> AsyncIterator[str]:
                    for offset in range(0, len(narrative), 96):
                        yield narrative[offset:offset + 96]
                        await asyncio.sleep(0)
                stream = _native_narrative_stream()

        def interrupted_event() -> bytes | None:
            """在线性化窗口内提交工具状态、保存安全前缀并发布中断事件。"""
            state_buffer.commit()
            partial_answer = projector.published_answer()
            interrupted_response = apply_streamed_answer(
                response,
                partial_answer,
                result_projector=project_agent_result_for_user,
            )
            result = interrupted_response.get("result")
            if isinstance(result, dict):
                interrupted_result = dict(result)
                interrupted_result["status"] = "interrupted"
                if partial_answer:
                    interrupted_result["summary"] = partial_answer
                interrupted_response["result"] = interrupted_result
            presentation = interrupted_response.get("presentation")
            if isinstance(presentation, dict):
                interrupted_response["presentation"] = {
                    **presentation,
                    "status": "interrupted",
                }
            if session_id is not None:
                _record_query_history(
                    request,
                    session_id=session_id,
                    message=message,
                    response=interrupted_response,
                    expected_generation=history_generation,
                )
            return _ndjson_event(
                "error",
                request_id=operation.operation_id,
                code="stream_interrupted",
                message="回答生成中断，已保留当前内容；可立即重试或继续追问。",
            )

        def finalize_interrupted() -> bytes | None:
            try:
                with agent_runtime_admission(
                    expected_generation=runtime_generation
                ):
                    published, event = coordinator.finalize_if_current(
                        operation,
                        interrupted_event,
                    )
            except AgentRuntimeDisabled:
                _cancel_runtime_changed_operation(
                    service=service, operation=operation
                )
                return None
            return event if published else None

        if stream is not None:
            event = current_event("status", phase="answering")
            if event is None:
                yield cancelled_event()
                return
            yield event
            try:
                async for delta in stream:
                    if not delta:
                        continue
                    projected = projector.feed(delta)
                    if projected is not None:
                        event = current_event(
                            "delta",
                            delta=projected.delta,
                        )
                        if event is None:
                            yield cancelled_event()
                            return
                        emitted = True
                        yield event
                    projector.raise_pending_error()
            except PublicNarrativeValidationError:
                if not coordinator.is_current(operation):
                    yield cancelled_event()
                    return
                # Provider narrative 只负责润色。流内容越过安全边界时丢弃
                # 整段 narrative，并用公开投影后的确定性结果正常收口。
                deterministic_public_fallback = True
                projector = PublicNarrativeProjector()
                logger.warning(
                    "Agent LLM 流式回答未通过公开校验，回退公开确定性结果"
                )
            except ProviderStreamError:
                if not coordinator.is_current(operation):
                    yield cancelled_event()
                    return
                if emitted:
                    event = finalize_interrupted()
                    if event is None:
                        yield cancelled_event()
                    else:
                        yield event
                    return
                projector = PublicNarrativeProjector()
                deterministic_public_fallback = True
                logger.warning("Agent LLM 流在首个公开文本前失败，回退自然语言确定性结果")
            except Exception as exc:
                if not coordinator.is_current(operation):
                    yield cancelled_event()
                    return
                if emitted:
                    logger.warning(
                        "Agent LLM 流式回答中断 type=%s", type(exc).__name__
                    )
                    event = finalize_interrupted()
                    if event is None:
                        yield cancelled_event()
                    else:
                        yield event
                    return
                projector = PublicNarrativeProjector()
                deterministic_public_fallback = True
                logger.warning(
                    "Agent LLM 流在首个公开文本前失败 type=%s，回退自然语言确定性结果",
                    type(exc).__name__,
                )

        final_response = response
        if projector.accumulated:
            try:
                answer = projector.finalize()
            except ProviderStreamError:
                answer = ""
            if answer:
                final_response = apply_streamed_answer(
                    response,
                    answer,
                    result_projector=project_agent_result_for_user,
                )
            else:
                deterministic_public_fallback = True
                logger.warning(
                    "Agent LLM 流式回答未通过最终公开校验，回退公开确定性结果"
                )
        if deterministic_public_fallback:
            final_response = _public_deterministic_fallback_response(
                response,
                request_id=operation.operation_id,
            )
        else:
            final_response = attach_public_fallback_presentation(final_response)

        if await request.is_disconnected():
            coordinator.cancel(
                owner=operation.owner,
                operation_id=operation.operation_id,
                reason="client_disconnected",
                remember=False,
                invalidate=lambda: invalidate_query_confirmation_epoch(
                    service, owner=operation.owner
                ),
            )
            return

        def persist_final() -> None:
            if session_id is not None:
                _record_query_history(
                    request,
                    session_id=session_id,
                    message=message,
                    response=final_response,
                    expected_generation=history_generation,
                )

        def finalize_response() -> bytes:
            state_buffer.commit()
            persist_final()
            # final 始终是权威完整快照；客户端应原位替换此前的流式草稿。
            return _ndjson_event(
                "final",
                request_id=operation.operation_id,
                payload=final_response,
            )

        try:
            with agent_runtime_admission(
                expected_generation=runtime_generation
            ):
                published, final_event = coordinator.finalize_if_current(
                    operation,
                    finalize_response,
                )
        except AgentRuntimeDisabled:
            _cancel_runtime_changed_operation(service=service, operation=operation)
            yield cancelled_event()
            return
        if not published or final_event is None:
            yield cancelled_event()
            return
        # 网络背压不属于进程内线性化临界区；最终事件快照已在锁内提交。
        yield final_event
    finally:
        state_buffer.discard()
        reset_llm_request_budget(llm_budget_token)
        coordinator.finish(operation)


@router.get("/capabilities")
def capabilities(request: Request):
    require_api_login(request)
    return api_response(get_agent_service().capabilities())


@router.get("/metrics")
def metrics(request: Request, format: str = "json"):
    if not _metrics_scrape_authorized(request):
        require_api_login(request)
    output_format = str(format or "json").strip().lower()
    if output_format == "json":
        return api_response(agent_metrics.snapshot())
    if output_format in {"prometheus", "text"}:
        return PlainTextResponse(
            agent_metrics.prometheus(),
            media_type="text/plain; version=0.0.4",
        )
    return api_error("format 仅支持 json 或 prometheus", 400)


@router.post("/query")
def query(request: Request, data: Any = Body(default=None)):
    require_api_login(request)
    if (
        not isinstance(data, dict)
        or "message" not in data
        or not set(data).issubset({"message", "session_id", "stream", "request_id"})
    ):
        return api_error("请求必须包含 message，且只能附带 session_id、stream 与 request_id", 400)
    if not isinstance(data.get("message"), str):
        return api_error("message 必须是字符串", 400)
    if "stream" in data and not isinstance(data.get("stream"), bool):
        return api_error("stream 必须是布尔值", 400)
    try:
        message = normalize_agent_message(data["message"])
        owner = _agent_owner(request, data)
        request_key = _request_id(data.get("request_id"))
        if confirmation_reply_intent(message) is not None:
            _check_rate_limit(request, "query", limit=30)
            return api_error(
                "为确保执行对象准确，请使用行动计划卡片上的执行或取消按钮",
                409,
            )
        action_history_request = agent_action_history_request(message)
        recent_resource_submit = is_recent_resource_submit_message(message)
        recent_download_explanation = is_recent_download_explanation_message(message)
        recent_download_library_verification = is_recent_download_library_verification_message(message)
        recent_download_status = is_recent_download_status_message(message)
        recent_patrol_resource_request = is_recent_library_patrol_resource_message(message)
        missing_season_resource_request = is_missing_season_resource_search_message(message)
        missing_episode_resource_request = is_missing_episode_resource_search_message(message)
        download_diagnosis_request = is_download_queue_diagnosis_message(message)
        download_retry_request = download_retry_submission_request(message)
        rss_refresh = rss_subscription_refresh_request(message)
        rss_pending_download = rss_pending_download_request(message)
        rss_failure_retry = rss_failure_retry_request(message)
        rss_diagnosis_request = is_rss_diagnosis_message(message)
        local_media_review_request = is_local_media_review_queue_summary_message(message)
        local_media_history_request = is_local_media_history_summary_message(message)
        local_media_diagnosis_request = is_local_media_diagnosis_message(message)
        strm_failure_retry = strm_failure_retry_request(message)
        strm_failure_triage_request = is_strm_failure_triage_message(message)
        automation_diagnosis_request = is_automation_pipeline_diagnosis_message(message)
        library_patrol_request = is_library_episode_patrol_message(message)
        patrol_policy_update = library_patrol_policy_request(message)
        patrol_policy_summary = is_library_patrol_policy_summary_message(message)
        library_patrol_status_request = is_library_patrol_status_message(message)
        update_request = is_library_update_check_message(message)
        audit_request = is_episode_audit_message(message)
        guangya_schedule_update = guangya_organize_schedule_policy_request(message)
        guangya_schedule_summary = is_guangya_organize_schedule_policy_summary_message(message)
        guangya_connection_request = is_guangya_connection_status_message(message)
        organize_audit = organize_audit_request(message)
        organize_clean_empty_request = is_guangya_organize_clean_empty_message(message)
        organize_request = (
            is_guangya_organize_preview_message(message)
            or is_guangya_organize_run_message(message)
            or is_guangya_organize_stop_message(message)
        )
        resource_request = is_indexer_resource_search_message(message)
        bangumi_calendar_request = is_bangumi_calendar_message(message)
        discovery_recommend_request = is_discovery_recommend_message(message)
        discovery_request = is_discovery_search_message(message)
        web_search_request = is_web_search_message(message)
        workspace_next_actions_request = is_workspace_next_actions_message(message)
        workspace_briefing_request = is_workspace_briefing_message(message)
        workspace_health_request = is_workspace_health_message(message)
        workspace_todo_request = is_workspace_todo_message(message)
        workspace_request = is_workspace_search_message(message)
        indexer_readiness_request = is_indexer_readiness_diagnosis_message(message)
        media_server_diagnosis_request = is_media_server_diagnosis_message(message)
        media_server_test_request = is_media_server_test_message(message)
        recognition_enabled_request = recognition_rule_enabled_request(message)
        media_proxy_enabled_request = media_proxy_instance_enabled_request(message)
        media_proxy_probe_request = media_proxy_test_request(message)
        media_proxy_status_request = is_media_proxy_status_summary_message(message)
        telegram_test_request = is_telegram_test_notification_message(message)
        feature_state_request = is_feature_state_message(message)
        indexer_site_request = indexer_sites_request(message)
        indexer_site_change = indexer_site_change_request(message)
        safe_policy_update = safe_policy_request(message)
        safe_policy_summary = is_safe_policy_summary_message(message)
        feature_summary_request = is_feature_summary_message(message)
        config_component_request = config_component_explain_request(message) is not None
        if recent_resource_submit:
            _check_rate_limit(request, "action:prepare:indexer.submit_candidate", limit=6)
        elif recent_download_library_verification:
            _check_rate_limit(request, "recent-download-library-verification", limit=6)
        elif recent_download_explanation:
            _check_rate_limit(request, "recent-download-explanation", limit=8)
        elif recent_download_status:
            _check_rate_limit(request, "recent-download-status", limit=12)
        elif action_history_request is not None:
            _check_rate_limit(request, "agent-action-history", limit=12)
        elif recent_patrol_resource_request:
            _check_rate_limit(request, "missing-episode-resources", limit=4, cost=2)
        elif indexer_site_request is not None or indexer_site_change is not None:
            _check_rate_limit(request, "action:prepare:config.set_indexer_sites", limit=4)
        elif safe_policy_update is not None:
            _check_rate_limit(request, "action:prepare:config.set_safe_policy", limit=4)
        elif patrol_policy_update is not None:
            _check_rate_limit(request, "action:prepare:library.set_patrol_policy", limit=4)
        elif feature_state_request:
            _check_rate_limit(request, "action:prepare:config.set_feature_state", limit=4)
        elif config_component_request:
            _check_rate_limit(request, "config-component-explain", limit=12)
        elif feature_summary_request:
            _check_rate_limit(request, "feature-summary", limit=12)
        elif safe_policy_summary:
            _check_rate_limit(request, "safe-policy-summary", limit=12)
        elif missing_season_resource_request:
            _check_rate_limit(request, "missing-episode-resources", limit=4, cost=2)
        elif missing_episode_resource_request:
            _check_rate_limit(request, "missing-episode-resources", limit=4)
        elif workspace_next_actions_request:
            _check_rate_limit(request, "query:workspace-next-actions", limit=4)
        elif workspace_briefing_request:
            _check_rate_limit(request, "workspace-briefing", limit=4)
        elif workspace_health_request:
            _check_rate_limit(request, "workspace-health", limit=4)
        elif workspace_todo_request:
            _check_rate_limit(request, "workspace-todo", limit=4)
        elif workspace_request:
            _check_rate_limit(request, "workspace-search", limit=4)
        elif indexer_readiness_request:
            _check_rate_limit(request, "indexer-readiness", limit=4)
        elif download_retry_request is not None:
            _check_rate_limit(request, "action:prepare:downloads.retry_submission", limit=3)
        elif download_diagnosis_request:
            _check_rate_limit(request, "download-queue-diagnosis", limit=4)
        elif rss_refresh is not None:
            _check_rate_limit(request, "action:prepare:rss.refresh_subscription", limit=3)
        elif rss_pending_download is not None:
            _check_rate_limit(request, "action:prepare:rss.submit_pending_to_qb", limit=3)
        elif rss_failure_retry is not None:
            _check_rate_limit(request, "action:prepare:rss.retry_failed_to_qb", limit=3)
        elif rss_diagnosis_request:
            _check_rate_limit(request, "rss-diagnosis", limit=4)
        elif local_media_review_request or local_media_history_request:
            _check_rate_limit(request, "local-media-diagnosis", limit=4)
        elif local_media_diagnosis_request:
            _check_rate_limit(request, "local-media-diagnosis", limit=4)
        elif strm_failure_retry is not None:
            _check_rate_limit(request, "action:prepare:strm.retry_failures", limit=3)
        elif strm_failure_triage_request:
            _check_rate_limit(request, "strm-failure-triage", limit=4)
        elif automation_diagnosis_request:
            _check_rate_limit(request, "automation-pipeline-diagnosis", limit=4)
        elif patrol_policy_summary:
            _check_rate_limit(request, "library-patrol-policy", limit=12)
        elif library_patrol_status_request:
            _check_rate_limit(request, "library-patrol-status", limit=12)
        elif library_patrol_request:
            _check_rate_limit(request, "library-full-audit", limit=2)
        elif update_request or audit_request:
            _check_rate_limit(request, "library-update-check", limit=6)
        elif guangya_schedule_update is not None:
            _check_rate_limit(
                request,
                "action:prepare:guangya.organize.set_schedule_policy",
                limit=4,
            )
        elif guangya_schedule_summary:
            _check_rate_limit(request, "guangya-organize-schedule-policy", limit=12)
        elif guangya_connection_request:
            _check_rate_limit(request, "guangya-connection-status", limit=4)
        elif organize_audit is not None:
            _check_rate_limit(request, "organize-audit-logs", limit=4)
        elif organize_clean_empty_request:
            _check_rate_limit(
                request,
                f"action:prepare:{"guangya.organize.cleanup.execute"}",
                limit=_prepare_rate_limit("guangya.organize.cleanup.execute"),
            )
        elif organize_request:
            _check_rate_limit(request, "query:organize", limit=4)
        elif resource_request:
            _check_rate_limit(request, "query:indexer", limit=6)
        elif bangumi_calendar_request:
            _check_rate_limit(request, "bangumi-calendar", limit=6)
        elif discovery_recommend_request:
            _check_rate_limit(request, "discovery-recommend", limit=6)
        elif web_search_request:
            _check_rate_limit(request, "web-search", limit=6)
        elif discovery_request:
            _check_rate_limit(request, "query:discovery", limit=6)
        elif recognition_enabled_request is not None:
            _check_rate_limit(
                request,
                "action:prepare:recognition.set_rule_enabled",
                limit=4,
            )
        elif media_proxy_enabled_request is not None:
            _check_rate_limit(
                request,
                "action:prepare:media_proxy.set_instance_enabled",
                limit=4,
            )
        elif media_proxy_probe_request is not None:
            _check_rate_limit(request, "media-proxy-test", limit=4)
        elif media_proxy_status_request:
            _check_rate_limit(request, "media-proxy-status", limit=12)
        elif telegram_test_request:
            _check_rate_limit(
                request,
                "action:prepare:telegram.send_test_notification",
                limit=3,
            )
        elif media_server_diagnosis_request:
            _check_rate_limit(request, "media-server-diagnosis", limit=4)
        elif media_server_test_request:
            _check_rate_limit(request, "media-server-test", limit=6)
        else:
            _check_rate_limit(request, "query", limit=30)
        session_key = (
            _session_id(data.get("session_id")) if "session_id" in data else None
        )
        history_generation = (
            _history_generation(request, session_id=session_key)
            if session_key is not None
            else None
        )
        tool_rate_identity = _agent_tool_rate_owner(request)
        conversation_context = (
            _conversation_context(request, session_id=session_key)
            if session_key is not None
            else []
        )
        query_kwargs = {
            "owner": owner,
            "llm_rate_owner": _agent_llm_rate_owner(request),
            "query_tool_rate_identity": tool_rate_identity,
            "llm_tool_rate_identity": tool_rate_identity,
            "request_id": request_key,
            "session_id": session_key or "",
        }
        # 无会话或空历史时保持既有 service 调用契约；只有真实上下文才透传。
        if conversation_context:
            query_kwargs["conversation_context"] = conversation_context
            query_kwargs["trusted_conversation_context"] = True
        streaming = bool(data.get("stream"))
        service = get_agent_service()
        coordinator = get_agent_operation_coordinator()
        runtime_generation = current_agent_runtime_generation()
        operation, confirmation_epoch = coordinator.begin_with_context(
            owner=owner,
            operation_id=request_key,
            initialize=lambda: begin_query_confirmation_epoch(service, owner=owner),
        )
        if confirmation_epoch is not None:
            query_kwargs["confirmation_owner_generation"] = confirmation_epoch
        if streaming:
            return StreamingResponse(
                _stream_query_events(
                    request,
                    message=message,
                    service=service,
                    query_kwargs=query_kwargs,
                    llm_owner=str(query_kwargs["llm_rate_owner"]),
                    session_id=session_key,
                    history_generation=history_generation,
                    operation=operation,
                    runtime_generation=runtime_generation,
                ),
                media_type="application/x-ndjson",
                headers={
                    "Cache-Control": "no-store",
                    "X-Accel-Buffering": "no",
                },
            )
        state_buffer = AgentStateCommitBuffer(owner=owner)
        if not coordinator.is_current(operation):
            coordinator.finish(operation)
            state_buffer.discard()
            return api_error("本次请求已被停止", 409)
        try:
            try:
                with defer_agent_state_commits(state_buffer):
                    response = service.query(message, **query_kwargs)
            except (AgentInputError, AgentToolError):
                if not coordinator.is_current(operation):
                    return api_error("本次请求已被更新操作取代", 409)
                raise

            def persist_final() -> None:
                if session_key is not None:
                    _record_query_history(
                        request,
                        session_id=session_key,
                        message=message,
                        response=response,
                        expected_generation=history_generation,
                    )

            try:
                with agent_runtime_admission(
                    expected_generation=runtime_generation
                ):
                    with coordinator.finalization_window_if_current(operation) as published:
                        if published:
                            state_buffer.commit()
                            persist_final()
                            return api_response(response)
            except AgentRuntimeDisabled as exc:
                _cancel_runtime_changed_operation(
                    service=service, operation=operation
                )
                return _agent_runtime_retry_response(exc)
            return api_error("本次请求已被更新操作取代", 409)
        finally:
            state_buffer.discard()
            coordinator.finish(operation)
    except AgentInputError as exc:
        return api_error(str(exc), 400)
    except AgentToolError as exc:
        return _agent_error(exc)


@router.post("/query/cancel")
def cancel_query(request: Request, data: Any = Body(default=None)):
    """显式撤销当前请求的发布权；可先于流请求抵达。"""
    require_api_login(request)
    if (
        not isinstance(data, dict)
        or "request_id" not in data
        or not set(data).issubset({"request_id", "session_id"})
    ):
        return api_error("请求必须包含 request_id，且只能附带 session_id", 400)
    try:
        owner = _agent_owner(request, data)
        request_key = _request_id(data.get("request_id"), required=True)
        _check_rate_limit(request, "query:cancel", limit=60)
        service = get_agent_service()
        accepted = get_agent_operation_coordinator().cancel(
            owner=owner,
            operation_id=request_key,
            reason="user_cancelled",
            remember=True,
            invalidate=lambda: invalidate_query_confirmation_epoch(
                service, owner=owner
            ),
        )
        return api_response(
            {
                "cancelled": accepted,
                "request_id": request_key,
            }
        )
    except AgentToolError as exc:
        return _agent_error(exc)


@router.post("/tools/{tool_name}")
def invoke_tool(request: Request, tool_name: str, data: Any = Body(default=None)):
    require_api_login(request)
    if data is None:
        data = {}
    if not isinstance(data, dict) or not set(data).issubset({"arguments", "session_id"}):
        return api_error("请求只能包含 arguments 与 session_id", 400)
    arguments = data.get("arguments", {})
    if not isinstance(arguments, dict):
        return api_error("arguments 必须是 JSON 对象", 400)
    try:
        tool_name = str(tool_name or "").strip()
        owner = _agent_owner(request, data)
        service = get_agent_service()
        if not service.has_tool(tool_name):
            raise AgentToolError("未知 Agent 工具", code="tool_not_found")
        if not allow_agent_tool(_agent_tool_rate_owner(request), tool_name):
            raise AgentToolError(
                "Agent 请求过于频繁，请稍后重试", code="rate_limited"
            )
        session_key = (
            _session_id(data.get("session_id")) if "session_id" in data else None
        )
        history_generation = (
            _history_generation(request, session_id=session_key)
            if session_key is not None
            else None
        )
        request_key = f"tool_{secrets.token_urlsafe(12)}"
        is_read_tool = _service_is_read_tool(service, tool_name)
        if not is_read_tool:
            # 非只读工具仍由 Registry 拒绝直接执行并引导走 prepare/confirm；
            # 无效直调不应撤销用户已经看到的确认票据。
            return api_response(service.invoke(
                tool_name,
                arguments,
                owner=owner,
                request_id=request_key,
                session_id=session_key or "",
            ))

        coordinator = get_agent_operation_coordinator()
        runtime_generation = current_agent_runtime_generation()
        operation, _ = coordinator.begin_with_context(
            owner=owner,
            operation_id=request_key,
            initialize=lambda: invalidate_query_confirmation_epoch(
                service, owner=owner
            ),
        )
        state_buffer = AgentStateCommitBuffer(owner=owner)
        try:
            try:
                with defer_agent_state_commits(state_buffer):
                    result = service.invoke(
                        tool_name,
                        arguments,
                        owner=owner,
                        request_id=request_key,
                        session_id=session_key or "",
                    )
            except AgentToolError:
                if not coordinator.is_current(operation):
                    return api_error("本次请求已被更新操作取代", 409)
                raise

            def persist_final() -> None:
                if session_key is not None:
                    _record_query_history(
                        request,
                        session_id=session_key,
                        message=_direct_tool_history_message(tool_name, arguments),
                        response=result,
                        expected_generation=history_generation,
                    )

            try:
                with agent_runtime_admission(
                    expected_generation=runtime_generation
                ):
                    with coordinator.finalization_window_if_current(operation) as published:
                        if published:
                            state_buffer.commit()
                            persist_final()
                            return api_response(result)
            except AgentRuntimeDisabled as exc:
                _cancel_runtime_changed_operation(
                    service=service, operation=operation
                )
                return _agent_runtime_retry_response(exc)
            return api_error("本次请求已被更新操作取代", 409)
        finally:
            state_buffer.discard()
            coordinator.finish(operation)
    except AgentToolError as exc:
        return _agent_error(exc)


@router.post("/workspace-actions/invoke")
def invoke_workspace_action(request: Request, data: Any = Body(default=None)):
    require_api_login(request)
    if not isinstance(data, dict) or "action_key" not in data or not set(data).issubset({
        "action_key", "session_id",
    }):
        return api_error(
            "请求必须包含 action_key，且只能附带 session_id",
            400,
        )
    try:
        owner = _agent_owner(request, data)
        session_key = (
            _session_id(data.get("session_id")) if "session_id" in data else None
        )
        history_generation = (
            _history_generation(request, session_id=session_key)
            if session_key is not None
            else None
        )
        action_key = data.get("action_key")
        if not isinstance(action_key, str):
            raise AgentToolError("action_key 必须是字符串")
        service = get_agent_service()
        request_key = f"workspace_{secrets.token_urlsafe(12)}"
        coordinator = get_agent_operation_coordinator()
        runtime_generation = current_agent_runtime_generation()
        operation, _ = coordinator.begin_with_context(
            owner=owner,
            operation_id=request_key,
            initialize=lambda: invalidate_query_confirmation_epoch(
                service, owner=owner
            ),
        )
        state_buffer = AgentStateCommitBuffer(owner=owner)
        try:
            try:
                with defer_agent_state_commits(state_buffer):
                    response = service.invoke_workspace_action(
                        action_key,
                        owner=owner,
                        rate_identity=_agent_tool_rate_owner(request),
                        request_id=request_key,
                        session_id=session_key or "",
                    )
            except AgentToolError:
                if not coordinator.is_current(operation):
                    return api_error("本次请求已被更新操作取代", 409)
                raise

            def persist_final() -> None:
                if session_key is None:
                    return
                tool_call = (
                    response.get("tool_call")
                    if isinstance(response, dict)
                    and isinstance(response.get("tool_call"), dict)
                    else {}
                )
                action_label = public_tool_label(tool_call.get("name"))
                _record_query_history(
                    request,
                    session_id=session_key,
                    message=f"执行工作区行动 · {action_label}",
                    response=response,
                    expected_generation=history_generation,
                )

            try:
                with agent_runtime_admission(
                    expected_generation=runtime_generation
                ):
                    with coordinator.finalization_window_if_current(operation) as published:
                        if published:
                            state_buffer.commit()
                            persist_final()
                            return api_response(response)
            except AgentRuntimeDisabled as exc:
                _cancel_runtime_changed_operation(
                    service=service, operation=operation
                )
                return _agent_runtime_retry_response(exc)
            return api_error("本次请求已被更新操作取代", 409)
        finally:
            state_buffer.discard()
            coordinator.finish(operation)
    except AgentToolError as exc:
        return _agent_error(exc)


@router.post("/actions/{tool_name}/prepare")
def prepare_action(request: Request, tool_name: str, data: Any = Body(default=None)):
    require_api_login(request)
    if data is None:
        data = {}
    if not isinstance(data, dict) or not set(data).issubset({"arguments", "session_id"}):
        return api_error("请求只能包含 arguments 与 session_id", 400)
    arguments = data.get("arguments", {})
    if not isinstance(arguments, dict):
        return api_error("arguments 必须是 JSON 对象", 400)
    try:
        tool_name = str(tool_name or "").strip()
        owner = _agent_owner(request, data)
        session_key = (
            _session_id(data.get("session_id")) if "session_id" in data else None
        )
        service = get_agent_service()
        if not service.has_tool(tool_name):
            raise AgentToolError("未知 Agent 工具", code="tool_not_found")
        _check_rate_limit(
            request,
            f"action:prepare:{tool_name}",
            limit=_prepare_rate_limit(tool_name),
        )
        coordinator = get_agent_operation_coordinator()
        runtime_generation = current_agent_runtime_generation()
        operation, confirmation_epoch = coordinator.begin_with_context(
            owner=owner,
            operation_id=f"prepare_{secrets.token_urlsafe(12)}",
            initialize=lambda: begin_query_confirmation_epoch(service, owner=owner),
        )
        try:
            prepare_kwargs: dict[str, Any] = {
                "owner": owner,
                "request_id": operation.operation_id,
                "session_id": session_key or "",
                "rate_identity": _agent_tool_rate_owner(request),
            }
            if confirmation_epoch is not None:
                prepare_kwargs["expected_owner_generation"] = confirmation_epoch
            response = service.prepare(
                tool_name, arguments, **prepare_kwargs
            )
            try:
                with agent_runtime_admission(
                    expected_generation=runtime_generation
                ):
                    published, finalized_response = coordinator.finalize_if_current(
                        operation,
                        lambda: response,
                    )
            except AgentRuntimeDisabled as exc:
                _cancel_runtime_changed_operation(
                    service=service, operation=operation
                )
                return _agent_runtime_retry_response(exc)
            if not published or finalized_response is None:
                return api_error("会话状态已变化，请重新预检", 409)
            return api_response(finalized_response)
        finally:
            coordinator.finish(operation)
    except AgentToolError as exc:
        return _agent_error(exc)


@router.post("/actions/confirm")
def confirm_action(request: Request, data: Any = Body(default=None)):
    require_api_login(request)
    if (
        not isinstance(data, dict)
        or not set(data).issubset({"plan_id", "session_id"})
        or "plan_id" not in data
    ):
        return api_error(
            "请求必须包含 plan_id，并可附带 session_id",
            400,
        )
    try:
        owner = _agent_owner(request, data)
        session_key = (
            _session_id(data.get("session_id")) if "session_id" in data else None
        )
        history_generation = (
            _history_generation(request, session_id=session_key)
            if session_key is not None
            else None
        )
        plan_id = _action_plan_id(data.get("plan_id"))
        _check_rate_limit(request, "action:confirm", limit=10)
        service = get_agent_service()
        coordinator = get_agent_operation_coordinator()
        operation = coordinator.begin(
            owner=owner,
            operation_id=f"confirm_{secrets.token_urlsafe(12)}",
        )

        def execute_confirmed_action() -> dict[str, Any]:
            response = service.confirm(
                plan_id,
                owner=owner,
                request_id=operation.operation_id,
                session_id=session_key or "",
            )
            if session_key is not None:
                _record_query_history(
                    request,
                    session_id=session_key,
                    message=_confirmation_history_message(response),
                    response=response,
                    expected_generation=history_generation,
                )
            return response

        try:
            # 确认后的写操作已经越过可撤销边界。将受控执行和历史落库放在
            # 同一个 owner 终态窗口内，使 reset/delete 只能排在其前或其后，
            # 不会先返回后再出现迟到副作用；HTTP 响应发送仍发生在窗口外。
            with agent_runtime_admission():
                published, response = coordinator.finalize_if_current(
                    operation,
                    execute_confirmed_action,
                )
            if not published or response is None:
                return api_error("会话状态已变化，请重新生成确认请求", 409)
        finally:
            coordinator.finish(operation)
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        if result.get("ok"):
            return api_response(response, 202 if result.get("status") == "accepted" else 200)
        conflict_statuses = {
            "busy", "conflict", "not_configured", "no_changes", "environment_override",
        }
        return api_response(
            response,
            409 if result.get("status") in conflict_statuses else 503,
        )
    except AgentRuntimeDisabled as exc:
        # 确认准入发生在票据领取之前；拒绝不会消费行动计划。
        return _agent_runtime_retry_response(exc)
    except AgentToolError as exc:
        return _agent_error(exc)


@router.post("/actions/confirm/discard")
def discard_confirmation(request: Request, data: Any = Body(default=None)):
    require_api_login(request)
    if (
        not isinstance(data, dict)
        or not set(data).issubset({"plan_id", "session_id"})
        or "plan_id" not in data
    ):
        return api_error(
            "请求必须包含 plan_id，并可附带 session_id",
            400,
        )
    try:
        owner = _agent_owner(request, data)
        plan_id = _action_plan_id(data.get("plan_id"))
        _check_rate_limit(request, "action:discard", limit=20)
        with get_agent_operation_coordinator().owner_window(owner):
            discarded = get_agent_service().discard_confirmation(
                plan_id,
                owner=owner,
            )
        return api_response({"discarded": discarded})
    except AgentToolError as exc:
        return _agent_error(exc)


@router.post("/session/reset")
def reset_session(request: Request, data: Any = Body(default=None)):
    require_api_login(request)
    if not isinstance(data, dict) or set(data) != {"session_id"}:
        return api_error("请求必须仅包含 session_id", 400)
    try:
        owner = _agent_owner(request, data)
        _check_rate_limit(request, "session:reset", limit=12)
        service = get_agent_service()
        reset_result: dict[str, Any] = {}

        def reset_runtime() -> None:
            nonlocal reset_result
            reset_result = service.reset_session(owner=owner)

        get_agent_operation_coordinator().invalidate_owner(
            owner=owner,
            reason="session_reset",
            invalidate=reset_runtime,
        )
        return api_response(reset_result)
    except AgentToolError as exc:
        return _agent_error(exc)


@router.get("/sessions")
def list_sessions(request: Request):
    require_api_login(request)
    try:
        _check_rate_limit(request, "sessions:list", limit=30)
        sessions = get_agent_conversation_history_repository().list_sessions(
            principal=_agent_history_principal(request),
            limit=24,
        )
        return api_response({"sessions": sessions})
    except (ValueError, AgentToolError) as exc:
        return _agent_error(exc) if isinstance(exc, AgentToolError) else api_error(str(exc), 400)


@router.get("/sessions/{session_id}")
def get_session_history(request: Request, session_id: str):
    require_api_login(request)
    try:
        session_key = _session_id(session_id)
        _check_rate_limit(request, "sessions:get", limit=40)
        session = get_agent_conversation_history_repository().get_session(
            principal=_agent_history_principal(request),
            session_id=session_key,
            limit=120,
        )
        if session is None:
            return api_error("Agent 会话不存在", 404)
        return api_response({"session": _public_session_projection(session)})
    except (ValueError, AgentToolError) as exc:
        return _agent_error(exc) if isinstance(exc, AgentToolError) else api_error(str(exc), 400)


@router.delete("/sessions/{session_id}")
def delete_session_history(request: Request, session_id: str):
    require_api_login(request)
    try:
        session_key = _session_id(session_id)
        _check_rate_limit(request, "sessions:delete", limit=12)
        owner = _agent_owner(request, {"session_id": session_key})
        service = get_agent_service()
        reset: dict[str, Any] = {}
        deleted = False
        principal = _agent_history_principal(request)

        def delete_runtime_and_history() -> None:
            nonlocal deleted, reset
            try:
                reset = service.reset_session(owner=owner)
            except AgentToolError as exc:
                logger.warning(
                    "Agent 历史删除时运行态撤销失败 code=%s",
                    getattr(exc, "code", "agent_error"),
                )
                reset = {
                    "reset": False,
                    "error": "Agent 运行态撤销失败，请稍后新建会话",
                }
            deleted = get_agent_conversation_history_repository().delete_session(
                principal=principal,
                session_id=session_key,
            )

        get_agent_operation_coordinator().invalidate_owner(
            owner=owner,
            reason="session_deleted",
            invalidate=delete_runtime_and_history,
        )
        return api_response({"deleted": deleted, "reset": reset})
    except (ValueError, AgentToolError) as exc:
        return _agent_error(exc) if isinstance(exc, AgentToolError) else api_error(str(exc), 400)
