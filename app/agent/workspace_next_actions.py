"""从工作区安全待办快照派生可执行的只读下一步。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.agent.workspace_todo_actions import summarize_workspace_todo

_ACTION_SPECS: dict[str, dict[str, str]] = {
    "downloads": {
        "action_key": "review_downloads",
        "label": "检查下载队列",
        "target_tool": "downloads.diagnose_queue",
        "prompt": "诊断下载队列",
        "why": "下载队列存在需要人工关注的状态。",
    },
    "rss": {
        "action_key": "review_rss",
        "label": "检查 RSS 订阅",
        "target_tool": "rss.diagnose",
        "prompt": "诊断 RSS 订阅",
        "why": "RSS 条目存在失败或待处理状态。",
    },
    "organize": {
        "action_key": "review_organize",
        "label": "检查云盘整理",
        "target_tool": "guangya.organize.status",
        "prompt": "查看光鸭整理状态",
        "why": "云盘整理流程存在需要核对的状态。",
    },
    "strm": {
        "action_key": "review_strm",
        "label": "检查 STRM 失败项",
        "target_tool": "strm.triage_failures",
        "prompt": "分析 STRM 同步失败项",
        "why": "STRM 同步存在失败或中断记录。",
    },
    "local_media": {
        "action_key": "review_local_media",
        "label": "检查本地媒体",
        "target_tool": "local_media.diagnose",
        "prompt": "诊断本地媒体",
        "why": "本地媒体流程存在待确认、失败或目标缺失。",
    },
    "download_verification": {
        "action_key": "review_download_verification",
        "label": "检查下载后入库核验",
        "target_tool": "downloads.diagnose_queue",
        "prompt": "诊断下载队列",
        "why": "下载后的媒体库可见性核验需要关注。",
    },
    "library_patrol": {
        "action_key": "review_library_patrol",
        "label": "检查媒体库巡检",
        "target_tool": "library.patrol_status",
        "prompt": "查看最近全库巡检结果",
        "why": "媒体库巡检发现可更新内容或未能完成检查。",
    },
}

_ALLOWED_REASON_CODES: dict[str, frozenset[str]] = {
    "downloads": frozenset({"download_needs_review"}),
    "rss": frozenset({"rss_failed", "rss_pending"}),
    "organize": frozenset({"organize_issue"}),
    "strm": frozenset({
        "strm_open_failure",
        "strm_last_run_failed",
        "strm_running",
    }),
    "local_media": frozenset({
        "local_media_requires_manual",
        "local_media_failed",
        "local_media_missing_target",
        "local_media_active",
        "local_media_waiting",
    }),
    "download_verification": frozenset({
        "download_verification_attention",
        "download_verification_running",
        "download_verification_pending",
        "download_verification_retry_wait",
    }),
    "library_patrol": frozenset({
        "library_patrol_updates_available",
        "library_patrol_inconclusive",
        "library_patrol_not_configured",
        "library_patrol_unavailable",
        "library_patrol_failed",
        "library_patrol_running",
        "library_patrol_retry_wait",
    }),
}

_SNAPSHOT_STATUSES = frozenset({
    "attention", "active", "waiting", "empty", "partial", "unavailable",
})
_ACTION_BY_KEY = {
    spec["action_key"]: spec
    for spec in _ACTION_SPECS.values()
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def workspace_next_actions_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments:
        raise AgentToolError("workspace.next_actions 不接受参数")
    return {}


def workspace_action_handoff_arguments(arguments: dict[str, Any]) -> dict[str, str]:
    if not isinstance(arguments, dict) or set(arguments) != {"action_key"}:
        raise AgentToolError("工作区行动接力必须且只能包含 action_key")
    action_key = arguments.get("action_key")
    if not isinstance(action_key, str):
        raise AgentToolError("action_key 必须是字符串")
    normalized = action_key.strip()
    if normalized not in _ACTION_BY_KEY:
        raise AgentToolError("不支持的工作区行动")
    return {"action_key": normalized}


def resolve_workspace_action_handoff(arguments: dict[str, Any]) -> dict[str, Any]:
    """重新读取当前快照，并把固定 action key 解析为可信只读目标。"""
    normalized = workspace_action_handoff_arguments(arguments)
    action_key = normalized["action_key"]
    snapshot = summarize_workspace_next_actions({})
    if not snapshot.ok:
        raise AgentToolError(
            "当前无法核对工作区行动，请稍后重试",
            code="precondition_failed",
        )
    raw_actions = snapshot.data.get("actions") if isinstance(snapshot.data, dict) else []
    actions = raw_actions if isinstance(raw_actions, list) else []
    if not any(
        isinstance(action, dict) and action.get("action_key") == action_key
        for action in actions
    ):
        raise AgentToolError(
            "该工作区行动已失效，请刷新下一步后重试",
            code="precondition_failed",
        )
    spec = _ACTION_BY_KEY[action_key]
    return {
        "action_key": action_key,
        "label": spec["label"],
        "target_tool": spec["target_tool"],
        "arguments": {},
    }


def _safe_sources(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    allowed = set(_ACTION_SPECS)
    return [source for source in _ACTION_SPECS if source in value and source in allowed]


def _project_action(area: Any) -> dict[str, Any] | None:
    if not isinstance(area, dict):
        return None
    source = str(area.get("source") or "").strip()
    spec = _ACTION_SPECS.get(source)
    attention_count = _count(area.get("attention_count"))
    if spec is None or str(area.get("status") or "") != "attention" or not attention_count:
        return None
    allowed_reasons = _ALLOWED_REASON_CODES[source]
    raw_reasons = area.get("reason_codes")
    reason_codes: list[str] = []
    if isinstance(raw_reasons, list):
        for value in raw_reasons:
            reason = str(value or "").strip()
            if reason in allowed_reasons and reason not in reason_codes:
                reason_codes.append(reason)
    if not reason_codes:
        return None
    return {
        "action_key": spec["action_key"],
        "source": source,
        "status": "attention",
        "attention_count": attention_count,
        "reason_codes": reason_codes,
        "label": spec["label"],
        "why": spec["why"],
        "target_tool": spec["target_tool"],
        "prompt": spec["prompt"],
        "risk": "read",
        "requires_confirmation": False,
        "precondition": "not_required",
        "staleness": "snapshot_only",
    }


def summarize_workspace_next_actions(_arguments: dict[str, Any]) -> ToolResult:
    """只投影现有待办快照，不执行诊断、预检或写操作。"""
    todo = summarize_workspace_todo({})
    todo_data = todo.data if isinstance(todo.data, dict) else {}
    raw_areas = todo_data.get("areas")
    projected_by_source: dict[str, dict[str, Any]] = {}
    for area in raw_areas if isinstance(raw_areas, list) else []:
        action = _project_action(area)
        if action is not None:
            projected_by_source.setdefault(action["source"], action)
    actions = [
        projected_by_source[source]
        for source in _ACTION_SPECS
        if source in projected_by_source
    ]
    snapshot_status = str(todo.status or "").strip()
    if snapshot_status not in _SNAPSHOT_STATUSES:
        snapshot_status = "unavailable" if not todo.ok else "partial"
    unavailable_areas = _safe_sources(todo_data.get("unavailable_areas"))
    data = {
        "probe_mode": "derived_local_snapshot",
        "network_accessed": False,
        "filesystem_accessed": False,
        "source_tool": "workspace.todo",
        "snapshot_status": snapshot_status,
        "attention_total": _count(todo_data.get("attention_total")),
        "active_total": _count(todo_data.get("active_total")),
        "waiting_total": _count(todo_data.get("waiting_total")),
        "unavailable_areas": unavailable_areas,
        "actions": actions,
    }
    evidence = [Evidence(
        "workspace_todo_projection",
        "从工作区本地安全待办快照生成只读行动卡；未访问网络、文件系统，也未执行诊断、预检或写操作。",
        _now(),
    )]

    if not todo.ok or snapshot_status == "unavailable":
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法生成工作区下一步",
            data=data,
            evidence=evidence,
            suggestions=["请检查本地数据库状态后重试。"],
            error="工作区下一步当前不可用。",
        )

    suggestions = [f"可询问：{action['prompt']}。" for action in actions[:5]]
    if unavailable_areas:
        suggestions.append("部分本地聚合暂时不可用，请稍后重试。")

    if snapshot_status == "partial":
        status = "partial"
        summary = (
            f"已生成 {len(actions)} 个安全下一步，部分工作区状态暂不可用"
            if actions
            else "部分工作区状态暂不可用，当前没有可安全生成的下一步"
        )
    elif actions:
        status = "attention"
        summary = f"已生成 {len(actions)} 个安全下一步"
    elif snapshot_status == "active":
        status = "active"
        summary = "工作区任务正在处理，当前无需额外操作"
    elif snapshot_status == "waiting":
        status = "waiting"
        summary = "工作区存在等待项，当前没有需要立即执行的下一步"
    else:
        status = "empty"
        summary = "工作区当前没有需要处理的下一步"

    return ToolResult(
        ok=True,
        status=status,
        summary=summary,
        data=data,
        evidence=evidence,
        suggestions=suggestions,
    )
