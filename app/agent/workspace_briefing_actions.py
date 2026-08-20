"""工作区系统简报：仅聚合本地安全状态，不主动探测外部服务。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app import config
from app.agent.indexer_readiness_actions import diagnose_indexer_readiness
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.agent.result_projection import public_followup_prompt
from app.agent.workspace_todo_actions import summarize_workspace_todo
from app.logger import get_logger

logger = get_logger(__name__)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def workspace_briefing_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments:
        raise AgentToolError("workspace.briefing 不接受参数")
    return {}


def _state(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _media_server_area() -> dict[str, Any]:
    specs = (
        ("jellyfin", "JELLYFIN_ENABLED", ("JELLYFIN_URL", "JELLYFIN_API_KEY")),
        ("emby", "EMBY_ENABLED", ("EMBY_URL", "EMBY_TOKEN")),
    )
    enabled_count = 0
    ready_count = 0
    incomplete_count = 0
    for _name, enabled_key, required in specs:
        enabled = _state(config.get(enabled_key, "0"))
        if not enabled:
            continue
        enabled_count += 1
        if all(str(config.get(key, "") or "").strip() for key in required):
            ready_count += 1
        else:
            incomplete_count += 1

    if incomplete_count:
        status = "attention"
        reasons = ["media_server_incomplete"]
    elif ready_count:
        status = "ready"
        reasons = []
    else:
        status = "not_configured"
        reasons = ["media_server_not_configured"]
    return {
        "source": "media_servers",
        "status": status,
        "attention_count": incomplete_count,
        "active_count": 0,
        "waiting_count": 0,
        "reason_codes": reasons,
        "next_tool": "config.diagnose",
        "enabled_count": enabled_count,
        "ready_count": ready_count,
        "connectivity": "not_probed",
    }


def _indexer_area(result: ToolResult) -> dict[str, Any]:
    counts = result.data.get("counts") if isinstance(result.data, dict) else {}
    counts = counts if isinstance(counts, dict) else {}
    attention = _safe_count(counts.get("attention"))
    if result.status == "unavailable":
        status = "unavailable"
    elif result.status == "disabled":
        status = "disabled"
    elif attention:
        status = "attention"
    else:
        status = "ready"
    return {
        "source": "indexers",
        "status": status,
        "attention_count": attention,
        "active_count": 0,
        "waiting_count": 0,
        "reason_codes": ["indexer_readiness_attention"] if attention else [],
        "next_tool": "indexer.diagnose_readiness",
        "enabled_count": _safe_count(counts.get("enabled")),
        "searchable_count": _safe_count(counts.get("searchable")),
        "downloadable_count": _safe_count(counts.get("downloadable")),
        "network_probe": "not_probed",
    }


def summarize_workspace_briefing(_arguments: dict[str, Any]) -> ToolResult:
    """生成本地状态简报；任一聚合失败都显式标记，不伪装为空闲。"""
    areas: list[dict[str, Any]] = []
    evidence: list[Evidence] = []

    try:
        todo = summarize_workspace_todo({})
    except Exception as exc:  # 防止单个聚合破坏整个简报
        logger.warning("Agent 系统简报读取工作区待办失败 type=%s", type(exc).__name__)
        todo = ToolResult(False, "unavailable", "工作区待办不可用", data={"areas": []})
    todo_areas = todo.data.get("areas") if isinstance(todo.data, dict) else []
    if isinstance(todo_areas, list) and todo_areas:
        areas.extend(dict(item) for item in todo_areas if isinstance(item, dict))
        todo_evidence = (
            "尝试读取工作区本地安全计数失败；不可用区域已显式标记。"
            if todo.status == "unavailable" or not todo.ok
            else "读取下载、RSS、整理、STRM、本地媒体与 Agent 持久自动化的本地安全计数。"
        )
        evidence.append(Evidence(
            "workspace_local_snapshot",
            todo_evidence,
            _now(),
        ))
    else:
        for source, next_tool in (
            ("downloads", "downloads.diagnose_queue"),
            ("rss", "rss.diagnose"),
            ("organize", "guangya.organize.status"),
            ("strm", "strm.triage_failures"),
            ("local_media", "local_media.diagnose"),
            ("download_verification", "downloads.diagnose_queue"),
            ("library_patrol", "library.patrol_status"),
        ):
            areas.append({
                "source": source,
                "status": "unavailable",
                "attention_count": 0,
                "active_count": 0,
                "waiting_count": 0,
                "reason_codes": ["local_snapshot_unavailable"],
                "next_tool": next_tool,
            })

    try:
        indexers = diagnose_indexer_readiness({})
    except Exception as exc:
        logger.warning("Agent 系统简报读取索引器状态失败 type=%s", type(exc).__name__)
        indexers = ToolResult(False, "unavailable", "索引器状态不可用", data={})
        indexer_evidence = "尝试读取索引器本地状态失败；未访问资源站。"
    else:
        indexer_evidence = "读取索引器本地开关与能力声明；未访问资源站。"
    areas.append(_indexer_area(indexers))
    evidence.append(Evidence(
        "indexer_local_readiness",
        indexer_evidence,
        _now(),
    ))

    try:
        areas.append(_media_server_area())
    except Exception as exc:
        logger.warning("Agent 系统简报读取媒体服务器配置失败 type=%s", type(exc).__name__)
        areas.append({
            "source": "media_servers",
            "status": "unavailable",
            "attention_count": 0,
            "active_count": 0,
            "waiting_count": 0,
            "reason_codes": ["media_server_config_unavailable"],
            "next_tool": "config.diagnose",
            "enabled_count": 0,
            "ready_count": 0,
            "connectivity": "not_probed",
        })
        media_server_evidence = "尝试读取媒体服务器本地配置失败；未连接服务器。"
    else:
        media_server_evidence = "仅检查媒体服务器是否启用及必要配置是否齐全；未连接服务器。"
    evidence.append(Evidence(
        "media_server_local_config",
        media_server_evidence,
        _now(),
    ))

    attention_total = sum(_safe_count(item.get("attention_count")) for item in areas)
    active_total = sum(_safe_count(item.get("active_count")) for item in areas)
    waiting_total = sum(_safe_count(item.get("waiting_count")) for item in areas)
    unavailable = [str(item.get("source")) for item in areas if item.get("status") == "unavailable"]
    disabled = [str(item.get("source")) for item in areas if item.get("status") == "disabled"]
    not_configured = [str(item.get("source")) for item in areas if item.get("status") == "not_configured"]
    available = [
        str(item.get("source"))
        for item in areas
        if item.get("status") not in {"unavailable", "disabled", "not_configured"}
    ]

    if len(unavailable) == len(areas):
        status = "unavailable"
        ok = False
        summary = "暂时无法生成系统简报"
    elif unavailable:
        status = "partial"
        ok = True
        summary = f"系统简报已部分生成，{len(unavailable)} 个区域不可用"
    elif attention_total:
        status = "attention"
        ok = True
        summary = f"系统简报发现 {attention_total} 项需要关注"
    elif active_total:
        status = "active"
        ok = True
        summary = f"系统当前有 {active_total} 项活动任务"
    elif waiting_total:
        status = "waiting"
        ok = True
        summary = f"系统当前有 {waiting_total} 项等待处理"
    else:
        status = "healthy"
        ok = True
        summary = "系统本地状态未发现待处理事项"

    suggestions = [
        public_followup_prompt(item.get("source"))
        for item in areas
        if item.get("status") in {"attention", "unavailable"}
    ][:6]
    return ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data={
            "probe_mode": "local_snapshot",
            "network_accessed": False,
            "content_filesystem_scanned": False,
            "as_of": _now(),
            "attention_total": attention_total,
            "active_total": active_total,
            "waiting_total": waiting_total,
            "coverage": {
                "requested": [str(item.get("source")) for item in areas],
                "available": available,
                "unavailable": unavailable,
                "disabled": disabled,
                "not_configured": not_configured,
                "not_probed": ["media_server_connectivity", "cloud_directory_pending_scan"],
            },
            "areas": areas,
        },
        evidence=evidence,
        suggestions=suggestions,
        error="系统简报当前不可用。" if not ok else "",
    )
