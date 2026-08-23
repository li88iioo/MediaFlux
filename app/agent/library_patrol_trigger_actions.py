"""按当前策略立即排队一次全库缺集巡检。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app import config, database as db
from app.agent.confirmation import confirmation_context_fingerprint
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def patrol_trigger_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if arguments:
        raise AgentToolError("library.trigger_patrol_now 不接受参数")
    return {}


def _capture() -> dict[str, Any]:
    row = db.get_agent_library_patrol()
    policy = {
        "enabled": config.get_bool("AGENT_LIBRARY_PATROL_ENABLED", False),
        "interval_hours": max(1, min(config.get_int("AGENT_LIBRARY_PATROL_INTERVAL_HOURS", 24), 168)),
        "max_series": max(1, min(config.get_int("AGENT_LIBRARY_PATROL_MAX_SERIES", 50), 100)),
    }
    state = {
        "task_status": str(row["status"] or "not_scheduled") if row is not None else "not_scheduled",
        "next_run_at": str(row["next_run_at"] or "") if row is not None else "",
        "lease_generation": int(row["lease_generation"] or 0) if row is not None else 0,
    }
    fingerprint = confirmation_context_fingerprint(
        {"policy": policy, "state": state},
        domain="library-trigger-patrol-now",
    )
    return {"policy": policy, "state": state, "fingerprint": fingerprint}


def prepare_trigger_patrol_now(_arguments: dict[str, Any]) -> tuple[ToolResult, str]:
    snapshot = _capture()
    policy = snapshot["policy"]
    state = snapshot["state"]
    if not policy["enabled"]:
        return ToolResult(
            ok=False,
            status="disabled",
            summary="全库缺集巡检当前未启用",
            data={"enabled": False, "task_status": state["task_status"]},
            error="请先启用巡检策略，再请求按当前策略立即巡检。",
            suggestions=["可先查看或修改全库缺集巡检策略。"],
        ), snapshot["fingerprint"]
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary="确认后将按当前策略把全库缺集巡检排到现在",
        data={
            "enabled": True,
            "interval_hours": policy["interval_hours"],
            "max_series": policy["max_series"],
            "task_status": state["task_status"],
            "effects": [
                "只会唤醒后台巡检调度器，不在确认请求中同步扫描媒体库。",
                "不会修改巡检策略、搜索资源或创建下载任务。",
                "若巡检已经运行，将复用现有任务而不会创建第二个任务。",
            ],
        },
        evidence=[Evidence(
            "patrol_policy",
            "已只读核对当前巡检策略和单例任务状态。",
            _now(),
        )],
    ), snapshot["fingerprint"]


def trigger_patrol_now(_arguments: dict[str, Any]) -> ToolResult:
    raise AgentToolError("立即巡检必须先预检并确认", code="confirmation_required")


def trigger_patrol_now_confirmed(
    _arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    current = _capture()
    if current["fingerprint"] != str(expected_context or ""):
        raise AgentToolError("巡检策略或任务状态已变化，请重新预检", code="confirmation_stale")
    if not current["policy"]["enabled"]:
        raise AgentToolError("全库缺集巡检当前未启用", code="precondition_failed")

    from app.modules.agent_library_patrol_scheduler import (
        get_agent_library_patrol_scheduler,
    )

    outcome = get_agent_library_patrol_scheduler().trigger_now()
    status = str(outcome.get("status") or "unavailable")
    if status == "queued":
        return ToolResult(
            ok=True,
            status="accepted",
            summary="已按当前策略把全库缺集巡检排到现在",
            data={"queued": True, "reused": False, "task_status": "pending"},
            evidence=[Evidence("patrol_scheduler", "后台调度器已被唤醒。", _now())],
            suggestions=["稍后可询问：最近一次全库巡检结果。"],
        )
    if status == "already_running":
        return ToolResult(
            ok=True,
            status="accepted",
            summary="全库缺集巡检已在运行，本次未重复创建任务",
            data={"queued": False, "reused": True, "task_status": "running"},
            evidence=[Evidence("patrol_scheduler", "已复用当前运行中的巡检单例。", _now())],
            suggestions=["可询问：全库巡检现在到哪了。"],
        )
    raise AgentToolError("当前无法安排全库缺集巡检，请稍后重试", code="precondition_failed")
