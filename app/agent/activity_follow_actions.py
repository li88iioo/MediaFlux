"""经确认的单任务跟踪；复用持久规则与通知中心，不启动 Agent 轮询循环。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from app.agent.activity_actions import (
    select_activity,
    selection_arguments,
    timeline_snapshot,
)
from app.agent.errors import AgentToolError
from app.agent.models import ToolContext, ToolResult
from app.agent.public_safety import sanitize_public_text
from app.modules.media_automation_rules import notification_route_settings
from app.repositories import media_automation_rules as rules
from app.repositories.agent_jobs import agent_job_owner_digest


def follow_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) - {
        "activity_selection_ref",
        "position",
        "hours",
    }:
        raise AgentToolError("只接受活动引用、序号与跟踪小时数")
    normalized = selection_arguments(
        {key: value for key, value in arguments.items() if key != "hours"}
    )
    hours = arguments.get("hours", 24)
    if type(hours) is not int or not 1 <= hours <= 168:
        raise AgentToolError("跟踪时长应为 1–168 小时")
    return {**normalized, "hours": hours}


def list_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or arguments:
        raise AgentToolError("跟踪列表不接受额外参数")
    return {}


def stop_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != {"rule_id"}:
        raise AgentToolError("需要跟踪规则 rule_id")
    key = arguments["rule_id"]
    if (
        not isinstance(key, str)
        or not key.startswith("auto_")
        or not 8 <= len(key) <= 100
    ):
        raise AgentToolError("跟踪规则引用无效")
    return {"rule_id": key}


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _public(rule: dict[str, Any]) -> dict[str, Any]:
    settings = rule["settings"]
    return {
        "rule_id": rule["id"],
        "enabled": rule["enabled"],
        "title": sanitize_public_text(settings.get("title"), limit=120),
        "expires_at": settings.get("expires_at", ""),
        "next_check_at": rule["next_run_at"],
    }


def list_follows(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    list_arguments(arguments)
    items = [
        _public(row)
        for row in rules.list_rules(agent_job_owner_digest(context.owner))
        if row["kind"] == "activity_follow"
    ]
    return ToolResult(
        True, "completed", f"已读取 {len(items)} 条活动跟踪规则", data={"items": items}
    )


def _prepare(
    arguments: dict[str, Any], context: ToolContext
) -> tuple[dict, dict, ToolResult]:
    target = select_activity(arguments)
    hours = arguments.get("hours", 24)
    if type(hours) is not int or not 1 <= hours <= 168:
        raise AgentToolError("跟踪时长应为 1–168 小时")
    timeline = timeline_snapshot(target)
    if not timeline.ok:
        raise AgentToolError("活动记录已不存在", code="precondition_failed")
    owner = agent_job_owner_digest(context.owner)
    current = next(
        (
            row
            for row in rules.list_rules(owner)
            if row["kind"] == "activity_follow"
            and row["settings"].get("target") == target
        ),
        None,
    )
    frozen = {
        "owner": owner,
        "session": context.session_id,
        "target": target,
        "hours": hours,
        "rule_id": current["id"] if current else "",
        "revision": current["revision"] if current else 0,
        "route": notification_route_settings(context.owner),
    }
    return frozen, timeline.data, timeline


def prepare_follow(
    arguments: dict[str, Any], context: ToolContext
) -> tuple[ToolResult, str]:
    frozen, data, _ = _prepare(arguments, context)
    return ToolResult(
        True,
        "confirmation_required",
        "确认后跟踪这项活动的最终状态",
        data={
            "title": data["title"],
            "hours": frozen["hours"],
            "current_status": data["explanation"],
            "effects": [
                "约每 5 分钟检查一次已有任务记录；发现异常、记录的阶段结束或到期时通知一次并停止跟踪。",
                "只观察，不启动整理、不自动重试，也不将下载完成视为媒体库入库。",
            ],
            "delivery": "Telegram 通知中心；受现有通知开关控制",
            "gaps": data.get("gaps", []),
        },
    ), _digest(frozen)


def follow_confirmed(
    arguments: dict[str, Any], expected_context: str, context: ToolContext
) -> ToolResult:
    frozen, data, _ = _prepare(arguments, context)
    if expected_context != _digest(frozen):
        raise AgentToolError(
            "活动跟踪配置或通知目标已改变，请重新确认", code="precondition_failed"
        )
    clock = datetime.now().astimezone()
    settings = {
        "target": frozen["target"],
        "title": data["title"],
        "expires_at": (clock + timedelta(hours=frozen["hours"])).isoformat(
            timespec="seconds"
        ),
        **frozen["route"],
    }
    row = rules.save_rule(
        frozen["owner"],
        kind="activity_follow",
        settings=settings,
        enabled=True,
        next_run_at=clock.isoformat(timespec="seconds"),
        rule_id=frozen["rule_id"],
        expected_revision=frozen["revision"],
    )
    if row is None:
        raise AgentToolError("活动跟踪已被修改，请重新确认", code="precondition_failed")
    return ToolResult(
        True, "completed", "活动跟踪已保存；这不表示原任务已完成", data=_public(row)
    )


def _stop_state(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    args = stop_arguments(arguments)
    row = rules.get_rule(agent_job_owner_digest(context.owner), args["rule_id"])
    if row is None or row["kind"] != "activity_follow":
        raise AgentToolError(
            "活动跟踪不存在或不属于当前用户", code="precondition_failed"
        )
    return row


def prepare_stop(
    arguments: dict[str, Any], context: ToolContext
) -> tuple[ToolResult, str]:
    row = _stop_state(arguments, context)
    return ToolResult(
        True, "confirmation_required", "确认后停止跟踪，不取消原任务", data=_public(row)
    ), _digest(row)


def stop_confirmed(
    arguments: dict[str, Any], expected_context: str, context: ToolContext
) -> ToolResult:
    row = _stop_state(arguments, context)
    if expected_context != _digest(row):
        raise AgentToolError("跟踪状态已变化，请重新预检", code="precondition_failed")
    saved = rules.save_rule(
        row["owner_digest"],
        kind=row["kind"],
        settings=row["settings"],
        enabled=False,
        next_run_at=row["next_run_at"],
        rule_id=row["id"],
        expected_revision=row["revision"],
    )
    if saved is None:
        raise AgentToolError("跟踪已被修改，请重新预检", code="precondition_failed")
    return ToolResult(
        True, "completed", "活动跟踪已停用；原任务保持原样", data=_public(saved)
    )
