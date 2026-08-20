"""媒体系统健康总检：聚合本地快照、配置完整性与媒体节点探测。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from app.agent.config_diagnosis_actions import diagnose_config
from app.agent.media_server_actions import diagnose_media_servers
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.agent.result_projection import public_followup_prompt
from app.agent.workspace_briefing_actions import summarize_workspace_briefing
from app.logger import get_logger

logger = get_logger(__name__)

_NOT_PROBED = [
    "cloud_directory_content_scan",
    "per_title_episode_audit",
    "per_title_update_check",
    "indexer_network_search",
    "download_submission",
]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def workspace_health_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments:
        raise AgentToolError("workspace.health 不接受参数")
    return {}


def _safe_count(value: Any, *, maximum: int = 1_000_000_000) -> int:
    try:
        return max(0, min(int(value or 0), maximum))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_result(source: str, handler: Callable[[dict[str, Any]], ToolResult]) -> ToolResult | None:
    try:
        result = handler({})
    except Exception as exc:
        logger.warning("Agent 媒体健康总检子检查失败 source=%s type=%s", source, type(exc).__name__)
        return None
    if not isinstance(result, ToolResult):
        logger.warning("Agent 媒体健康总检子检查返回类型无效 source=%s", source)
        return None
    return result


def _workspace_area(result: ToolResult | None) -> dict[str, Any]:
    if result is None or result.status == "unavailable" or not isinstance(result.data, dict):
        return {
            "source": "workspace",
            "status": "unavailable",
            "attention_count": 0,
            "reason_codes": ["workspace_snapshot_unavailable"],
            "next_tool": "workspace.briefing",
            "active_count": 0,
            "waiting_count": 0,
            "unavailable_area_count": 0,
        }
    coverage = result.data.get("coverage") if isinstance(result.data.get("coverage"), dict) else {}
    unavailable_count = len(coverage.get("unavailable")) if isinstance(coverage.get("unavailable"), list) else 0
    attention = _safe_count(result.data.get("attention_total"))
    status = "attention" if result.status in {"partial", "attention"} or unavailable_count or attention else "healthy"
    reasons: list[str] = []
    if unavailable_count:
        reasons.append("workspace_partial")
    if attention:
        reasons.append("workspace_attention")
    if result.status in {"active", "waiting"}:
        reasons.append(f"workspace_{result.status}")
    return {
        "source": "workspace",
        "status": status,
        "attention_count": max(attention, 1 if unavailable_count else 0),
        "reason_codes": reasons,
        "next_tool": "workspace.briefing",
        "active_count": _safe_count(result.data.get("active_total")),
        "waiting_count": _safe_count(result.data.get("waiting_total")),
        "unavailable_area_count": unavailable_count,
    }


def _configuration_area(result: ToolResult | None) -> dict[str, Any]:
    if result is None or result.status == "unavailable" or not isinstance(result.data, dict):
        return {
            "source": "configuration",
            "status": "unavailable",
            "attention_count": 0,
            "reason_codes": ["configuration_diagnosis_unavailable"],
            "next_tool": "config.diagnose",
            "error_count": 0,
            "warning_count": 0,
            "ready_component_count": 0,
            "component_count": 0,
        }
    counts = result.data.get("counts") if isinstance(result.data.get("counts"), dict) else {}
    components = result.data.get("components") if isinstance(result.data.get("components"), list) else []
    errors = _safe_count(counts.get("errors"))
    warnings = _safe_count(counts.get("warnings"))
    reasons = []
    if errors:
        reasons.append("configuration_errors")
    if warnings:
        reasons.append("configuration_warnings")
    attention = errors + warnings
    return {
        "source": "configuration",
        "status": "attention" if attention or result.status != "healthy" else "healthy",
        "attention_count": max(attention, 1 if result.status != "healthy" else 0),
        "reason_codes": reasons,
        "next_tool": "config.diagnose",
        "error_count": errors,
        "warning_count": warnings,
        "ready_component_count": sum(
            isinstance(item, dict) and item.get("status") == "ready" for item in components
        ),
        "component_count": min(len(components), 100),
    }


def _media_server_area(result: ToolResult | None) -> tuple[dict[str, Any], bool]:
    if result is None or not isinstance(result.data, dict):
        return ({
            "source": "media_servers",
            "status": "unavailable",
            "attention_count": 0,
            "reason_codes": ["media_server_diagnosis_unavailable"],
            "next_tool": "config.diagnose_media_servers",
            "enabled_count": 0,
            "configured_count": 0,
            "online_count": 0,
            "compatible_count": 0,
        }, False)
    counts = result.data.get("counts") if isinstance(result.data.get("counts"), dict) else {}
    nodes = result.data.get("nodes") if isinstance(result.data.get("nodes"), list) else []
    network_accessed = result.data.get("network_accessed") is True
    enabled = _safe_count(counts.get("enabled"), maximum=2)
    configured = _safe_count(counts.get("configured"), maximum=2)
    online = _safe_count(counts.get("online"), maximum=2)
    compatible = _safe_count(counts.get("compatible"), maximum=2)
    reported_attention = _safe_count(counts.get("attention"), maximum=2)

    if result.status == "unavailable" and not nodes:
        status = "unavailable"
        attention = 0
        reasons = ["media_server_diagnosis_unavailable"]
    else:
        reasons: list[str] = []
        if enabled == 0:
            reasons.append("media_server_not_configured")
        elif configured and online == 0:
            reasons.append("media_server_offline")
        if reported_attention:
            reasons.append("media_server_compatibility_attention")
        attention = max(reported_attention, 1 if reasons or result.status != "healthy" else 0)
        status = "attention" if attention else "healthy"

    return ({
        "source": "media_servers",
        "status": status,
        "attention_count": attention,
        "reason_codes": reasons,
        "next_tool": "config.diagnose_media_servers",
        "enabled_count": enabled,
        "configured_count": configured,
        "online_count": online,
        "compatible_count": compatible,
    }, network_accessed)


def diagnose_workspace_health(_arguments: dict[str, Any]) -> ToolResult:
    """执行固定范围的只读健康检查，并显式声明未覆盖的昂贵操作。"""
    workspace = _safe_result("workspace", summarize_workspace_briefing)
    configuration = _safe_result("configuration", diagnose_config)
    media_servers = _safe_result("media_servers", diagnose_media_servers)

    workspace_area = _workspace_area(workspace)
    configuration_area = _configuration_area(configuration)
    media_server_area, network_accessed = _media_server_area(media_servers)
    areas = [workspace_area, configuration_area, media_server_area]

    unavailable = [item["source"] for item in areas if item["status"] == "unavailable"]
    available = [item["source"] for item in areas if item["status"] != "unavailable"]
    attention_total = sum(_safe_count(item.get("attention_count")) for item in areas)

    if len(unavailable) == len(areas):
        ok = False
        status = "unavailable"
        summary = "媒体系统健康总检暂时不可用"
    elif unavailable:
        ok = True
        status = "partial"
        summary = f"媒体系统健康总检已部分完成，{len(unavailable)} 个区域不可用"
    elif attention_total:
        ok = True
        status = "attention"
        summary = f"媒体系统健康总检发现 {attention_total} 项需要关注"
    else:
        ok = True
        status = "healthy"
        summary = "媒体系统健康总检通过"

    suggestions = [
        public_followup_prompt(item.get("source"))
        for item in areas
        if item["status"] in {"attention", "unavailable"}
    ]
    return ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data={
            "probe_mode": "local_and_media_endpoints",
            "network_accessed": network_accessed,
            "content_filesystem_scanned": False,
            "as_of": _now(),
            "attention_total": attention_total,
            "coverage": {
                "requested": ["workspace", "configuration", "media_servers"],
                "available": available,
                "unavailable": unavailable,
                "not_probed": list(_NOT_PROBED),
            },
            "areas": areas,
        },
        evidence=[
            Evidence("workspace_local_snapshot", "读取工作区本地安全计数与能力就绪状态。", _now()),
            Evidence("configuration_completeness", "仅检查关键配置组合是否完整，未返回配置值。", _now()),
            Evidence(
                "configured_media_servers",
                "对已配置媒体节点执行固定系统信息探测；未返回地址、名称或访问凭据。",
                _now(),
            ),
        ],
        suggestions=suggestions,
        error="媒体系统健康总检当前不可用。" if not ok else "",
    )
