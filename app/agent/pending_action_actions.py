"""当前会话待确认行动计划的安全控制能力。"""
from __future__ import annotations

from typing import Any

from app.agent.models import ToolContext, ToolResult
from app.agent.registry import AgentToolError
from app.agent.result_projection import sanitize_public_text


def pending_action_arguments(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or value:
        raise AgentToolError("取消待确认计划不接受参数", code="invalid_arguments")
    return {}


def cancel_pending_action(
    _arguments: dict[str, Any], context: ToolContext
) -> ToolResult:
    """取消当前 owner 唯一待确认计划；不触碰已执行的业务状态。"""
    owner = str(context.owner or "").strip()
    if not owner:
        raise AgentToolError("当前会话无法取消行动计划", code="login_required")

    # 延迟导入避免 service -> tools -> pending_action_actions -> service 环。
    from app.agent.service import get_agent_service

    service = get_agent_service()
    tickets = service.confirmation_store.list_active_tickets(owner=owner)
    service._reconcile_missing_confirmations(owner, tickets)
    if not tickets:
        return ToolResult(
            ok=True,
            status="already_clear",
            summary="当前没有等待执行的行动计划。",
        )
    if len(tickets) != 1:
        raise AgentToolError(
            "当前有多个待确认计划，请在对应计划卡片上取消。",
            code="selection_required",
        )
    ticket = tickets[0]
    if not service.discard_confirmation(ticket.confirmation_id, owner=owner):
        raise AgentToolError(
            "行动计划已经失效，请重新检查当前状态。",
            code="confirmation_invalid",
        )
    action = sanitize_public_text(
        ticket.confirmation_contract.get("action"), limit=120
    ) or "本次行动计划"
    return ToolResult(
        ok=True,
        status="cancelled",
        summary=f"已取消“{action}”，没有执行任何写操作。",
        suggestions=["需要时可以根据新的要求重新生成行动计划。"],
    )
