"""编排层与各消息渠道共享的 Agent 回合语义契约。

该契约只决定结果如何展示，绝不授予写工具执行权限；写操作安全仍完全由工具注册表
和确认流水线负责。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.agent.tool_semantics import RESOURCE_CANDIDATE_TOOLS


_TASK_KINDS = frozenset({"conversation", "informational", "resource_search", "action"})
_PRESENTATIONS = frozenset({"narrative", "resource_candidates", "confirmation"})
_RESOURCE_ROLES = frozenset({"none", "supporting", "primary"})


def build_response_contract(
    *,
    task_kind: str,
    presentation: str,
    resource_candidates: str = "none",
) -> dict[str, str]:
    """为下游消息适配器生成经过校验的最小语义契约。"""
    normalized_task = str(task_kind or "").strip().lower()
    normalized_presentation = str(presentation or "").strip().lower()
    normalized_resource_role = str(resource_candidates or "none").strip().lower()
    if normalized_task not in _TASK_KINDS:
        raise ValueError("invalid agent task kind")
    if normalized_presentation not in _PRESENTATIONS:
        raise ValueError("invalid agent presentation kind")
    if normalized_resource_role not in _RESOURCE_ROLES:
        raise ValueError("invalid resource candidate role")
    if (
        normalized_presentation == "resource_candidates"
        and normalized_resource_role != "primary"
    ):
        raise ValueError("resource candidate presentation requires primary candidates")
    if (
        normalized_resource_role == "primary"
        and normalized_task not in {"resource_search", "action"}
    ):
        raise ValueError("primary candidates require a resource or action task")
    return {
        "task_kind": normalized_task,
        "presentation": normalized_presentation,
        "resource_candidates": normalized_resource_role,
    }


def attach_response_contract(
    response: dict[str, Any],
    *,
    task_kind: str,
    presentation: str,
    resource_candidates: str = "none",
) -> dict[str, Any]:
    """把经过校验的回合契约附加到响应顶层。"""
    if not isinstance(response, dict):
        raise TypeError("response must be a dictionary")
    contract = build_response_contract(
        task_kind=task_kind,
        presentation=presentation,
        resource_candidates=resource_candidates,
    )
    response["response_contract"] = contract
    try:
        from app.agent.turn_runtime import record_agent_response_contract

        record_agent_response_contract(contract)
    except Exception:
        pass
    return response


def response_contract(value: Any) -> dict[str, str]:
    """返回有效契约的安全副本；无效或缺失时返回空映射。"""
    if not isinstance(value, dict):
        return {}
    raw = value.get("response_contract")
    if not isinstance(raw, dict) or set(raw) != {
        "task_kind", "presentation", "resource_candidates"
    }:
        return {}
    try:
        return deepcopy(build_response_contract(
            task_kind=raw.get("task_kind", ""),
            presentation=raw.get("presentation", ""),
            resource_candidates=raw.get("resource_candidates", "none"),
        ))
    except ValueError:
        return {}

_ACTION_MODES = frozenset({
    "confirmation_required", "confirmed_action", "cancelled_action",
})


def infer_response_contract(response: Any) -> dict[str, str]:
    """从服务端稳定字段推导旧响应的语义契约。

    推导只发生在响应生产边界，消息渠道不得再重复猜测。显式附加的有效契约
    始终优先；此函数主要覆盖确定性工具、确认结果和兼容回退响应。
    """
    current = response_contract(response)
    if current:
        return current
    if not isinstance(response, dict):
        return {}

    mode = str(response.get("mode") or "").strip().lower()
    tool_call = response.get("tool_call")
    tool_name = (
        str(tool_call.get("name") or "").strip()
        if isinstance(tool_call, dict) else ""
    )
    has_confirmation = isinstance(response.get("confirmation"), dict)

    if mode == "confirmation_required" or has_confirmation:
        return build_response_contract(
            task_kind="action",
            presentation="confirmation",
        )
    if tool_name in RESOURCE_CANDIDATE_TOOLS:
        return build_response_contract(
            task_kind="resource_search",
            presentation="resource_candidates",
            resource_candidates="primary",
        )
    if mode in _ACTION_MODES:
        return build_response_contract(
            task_kind="action",
            presentation="narrative",
        )
    if mode == "conversation":
        return build_response_contract(
            task_kind="conversation",
            presentation="narrative",
        )
    return build_response_contract(
        task_kind="informational",
        presentation="narrative",
    )


def ensure_response_contract(response: dict[str, Any]) -> dict[str, Any]:
    """确保标准 Agent 响应在离开服务端前拥有唯一语义契约。"""
    if not isinstance(response, dict):
        raise TypeError("response must be a dictionary")
    contract = infer_response_contract(response)
    if contract:
        response["response_contract"] = contract
        try:
            from app.agent.turn_runtime import record_agent_response_contract

            record_agent_response_contract(contract)
        except Exception:
            pass
    return response


def resource_candidates_are_primary(value: Any) -> bool | None:
    """返回契约判断；``None`` 表示旧版响应尚无该契约。"""
    contract = response_contract(value)
    if not contract:
        return None
    return contract["resource_candidates"] == "primary"
