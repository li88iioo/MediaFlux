"""Agent 行动计划的稳定公开协议。

行动计划只是一张服务端确认票据的脱敏投影；执行参数、内部工具名与上下文
指纹始终保留在服务端，模型和消息渠道都不能借此绕过确认门。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.agent.action_plan_id import normalize_action_plan_id
from app.agent.confirmation_contract import (
    normalize_confirmation_timestamp,
    sanitize_confirmation_contract,
)
from app.agent.result_projection import sanitize_public_text

ACTION_PLAN_VERSION = 1
_ACTION_PLAN_STATUSES = frozenset({
    "awaiting_approval", "executing", "completed", "failed", "cancelled", "expired",
})
_ACTION_PLAN_RISKS = frozenset({"low_write", "write", "danger"})


def _safe_positive_int(value: Any, *, maximum: int = 86_400) -> int:
    if type(value) is not int:
        return 0
    return value if 0 < value <= maximum else 0


def _decisions_for_status(status: str) -> list[dict[str, str]]:
    if status != "awaiting_approval":
        return []
    return [
        {"id": "execute", "label": "执行"},
        {"id": "cancel", "label": "取消"},
    ]


def build_action_plan(
    *,
    plan_id: str,
    confirmation_contract: Mapping[str, Any] | None,
    expires_in: int,
    status: str = "awaiting_approval",
) -> dict[str, Any]:
    """从服务端确认契约构造不含执行参数的公开行动计划。"""
    normalized_id = normalize_action_plan_id(plan_id)
    if not normalized_id:
        return {}
    contract = sanitize_confirmation_contract(confirmation_contract)
    if not contract:
        return {}
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in _ACTION_PLAN_STATUSES:
        return {}
    risk = str(contract.get("risk") or "").strip().lower()
    preflight_at = normalize_confirmation_timestamp(contract.get("preflight_at"))
    if risk not in _ACTION_PLAN_RISKS or not preflight_at:
        return {}
    plan: dict[str, Any] = {
        "version": ACTION_PLAN_VERSION,
        "plan_id": normalized_id,
        "status": normalized_status,
        "title": sanitize_public_text(contract.get("action"), limit=128)
        or "执行受控操作",
        "target": sanitize_public_text(contract.get("object"), limit=128)
        or "当前预检选中的对象",
        "impact": sanitize_public_text(contract.get("impact"), limit=160)
        or "执行后会应用服务端预检通过的受控变更。",
        "reversibility": sanitize_public_text(
            contract.get("reversibility"), limit=160
        ) or "执行后可能需要在对应功能页手动撤销。",
        "risk": risk,
        "preflight_at": preflight_at,
        "decisions": _decisions_for_status(normalized_status),
    }
    ttl = _safe_positive_int(expires_in)
    if ttl:
        plan["expires_in"] = ttl
    summary = sanitize_public_text(contract.get("preflight_summary"), limit=160)
    if summary:
        plan["preflight_summary"] = summary
    return plan


def sanitize_action_plan(value: Any) -> dict[str, Any]:
    """严格重投影外部行动计划；无效结构返回空映射。"""
    if not isinstance(value, Mapping):
        return {}
    raw_version = value.get("version")
    if type(raw_version) is not int or raw_version != ACTION_PLAN_VERSION:
        return {}
    plan_id = normalize_action_plan_id(value.get("plan_id"))
    status = str(value.get("status") or "").strip().lower()
    risk = str(value.get("risk") or "").strip().lower()
    preflight_at = normalize_confirmation_timestamp(value.get("preflight_at"))
    if (
        not plan_id
        or status not in _ACTION_PLAN_STATUSES
        or risk not in _ACTION_PLAN_RISKS
        or not preflight_at
    ):
        return {}
    projected = {
        "version": ACTION_PLAN_VERSION,
        "plan_id": plan_id,
        "status": status,
        "title": sanitize_public_text(value.get("title"), limit=128)
        or "执行受控操作",
        "target": sanitize_public_text(value.get("target"), limit=128)
        or "当前预检选中的对象",
        "impact": sanitize_public_text(value.get("impact"), limit=160)
        or "执行后会应用服务端预检通过的受控变更。",
        "reversibility": sanitize_public_text(value.get("reversibility"), limit=160)
        or "执行后可能需要在对应功能页手动撤销。",
        "risk": risk,
        "preflight_at": preflight_at,
        "decisions": _decisions_for_status(status),
    }
    ttl = _safe_positive_int(value.get("expires_in"))
    if ttl:
        projected["expires_in"] = ttl
    summary = sanitize_public_text(value.get("preflight_summary"), limit=160)
    if summary:
        projected["preflight_summary"] = summary
    return projected


def action_plan_model_context(value: Any) -> str:
    """生成供模型消解指代的脱敏摘要，刻意排除 plan_id 与内部工具信息。"""
    plan = sanitize_action_plan(value)
    if not plan or plan["status"] != "awaiting_approval":
        return ""
    lines = [
        "当前有一项尚未执行的行动计划：",
        f"动作：{plan['title']}",
        f"对象：{plan['target']}",
        f"影响：{plan['impact']}",
        "用户可执行、取消，或提出修改后重新生成计划；不得声称该计划已经执行。",
    ]
    return "\n".join(lines)
