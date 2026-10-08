"""项目配置完整性诊断：只返回固定状态与安全计数。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.agent.config_explain_actions import (
    _effective_value,
    _state,
    component_configuration,
)
from app.agent.models import Evidence, ToolResult


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def diagnose_config(_arguments: dict[str, Any]) -> ToolResult:
    """只投影共用组件状态，不再维护另一套字段存在性判断。"""
    value = _effective_value()
    payloads = {
        name: component_configuration(name, value=value)
        for name in ("jellyfin", "emby", "tmdb", "qbittorrent", "strm", "ai_recognition")
    }
    components = [
        {"name": name, **{key: data[key] for key in ("label", "enabled", "status")}}
        for name, data in payloads.items()
    ]
    issues: list[dict[str, str]] = []
    for name, data in payloads.items():
        if data["status"] == "incomplete":
            code = "strm_schedule_incomplete" if name == "strm" and _state(value("STRM_SCHEDULE_ENABLED")) else f"{name}_incomplete"
            message = {
                "qbittorrent": "qBittorrent 配置不完整，需要地址以及 API Key 或用户名/密码。",
                "strm": "STRM 来源、播放地址或输出目录缺失或无效；来源不能使用云盘根目录。",
                "ai_recognition": "AI 识别回退已启用，请先在 Media Agent 设置中补全模型连接。",
            }.get(name, f"{data['label']}已启用，但缺少 {len(data['missing_field_labels'])} 项必要配置。")
            issues.append({"code": code, "severity": "error", "message": message})
        if name == "emby" and not any(payloads[slot]["enabled"] for slot in ("jellyfin", "emby")):
            issues.append({"code": "media_server_disabled", "severity": "warning", "message": "尚未启用媒体服务器，媒体库搜索与缺集检查不可用。"})
        if name == "tmdb" and data["status"] != "ready":
            issues.append({"code": "tmdb_not_configured", "severity": "warning", "message": "TMDB 未配置，更新检查、映射和部分探索能力受限。"})

    errors = sum(item["severity"] == "error" for item in issues)
    warnings = sum(item["severity"] == "warning" for item in issues)
    status = "healthy" if not issues else ("degraded" if not errors else "attention")
    summary = "关键配置检查通过" if not issues else f"发现 {errors} 个错误、{warnings} 个提醒"
    return ToolResult(
        ok=errors == 0,
        status=status,
        summary=summary,
        data={"components": components, "issues": issues, "counts": {"errors": errors, "warnings": warnings},
              "network_accessed": False, "probe_mode": "configuration_only"},
        evidence=[Evidence("config", "仅检查配置项是否存在及组合是否完整；未读取或返回凭据内容。", _now())],
        suggestions=[item["message"] for item in issues[:5]],
    )
