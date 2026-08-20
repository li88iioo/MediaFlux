"""项目配置完整性诊断：只返回固定状态与安全计数。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app import config
from app.agent.models import Evidence, ToolResult


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _state(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def diagnose_config(_arguments: dict[str, Any]) -> ToolResult:
    """只返回配置完整性，不返回任何配置值。"""
    items = config.all_items()
    issues: list[dict[str, str]] = []
    components: list[dict[str, Any]] = []

    def value(key: str) -> str:
        return str(config.get(key, items.get(key, "")) or "").strip()

    def component(name: str, enabled: bool, required: tuple[str, ...], label: str) -> None:
        missing = [key for key in required if not value(key)] if enabled else []
        status = "disabled"
        if enabled:
            status = "ready" if not missing else "incomplete"
        components.append({"name": name, "label": label, "enabled": enabled, "status": status})
        if missing:
            issues.append({
                "code": f"{name}_incomplete",
                "severity": "error",
                "message": f"{label}已启用，但缺少 {len(missing)} 项必要配置。",
            })

    jellyfin_enabled = _state(value("JELLYFIN_ENABLED"))
    emby_enabled = _state(value("EMBY_ENABLED"))
    component("jellyfin", jellyfin_enabled, ("JELLYFIN_URL", "JELLYFIN_API_KEY"), "Jellyfin")
    component("emby", emby_enabled, ("EMBY_URL", "EMBY_TOKEN"), "Emby / Jellyfin 10.x")
    if not jellyfin_enabled and not emby_enabled:
        issues.append({
            "code": "media_server_disabled",
            "severity": "warning",
            "message": "尚未启用媒体服务器，媒体库搜索与缺集检查不可用。",
        })

    tmdb_ready = bool(value("TMDB_API_KEY"))
    components.append({
        "name": "tmdb",
        "label": "TMDB 元数据",
        "enabled": tmdb_ready,
        "status": "ready" if tmdb_ready else "not_configured",
    })
    if not tmdb_ready:
        issues.append({
            "code": "tmdb_not_configured",
            "severity": "warning",
            "message": "TMDB 未配置，更新检查、映射和部分探索能力受限。",
        })

    qb_url = bool(value("QB_URL"))
    qb_auth = bool(value("QB_API_KEY")) or bool(value("QB_USERNAME") and value("QB_PASSWORD"))
    qb_enabled = qb_url or qb_auth
    qb_ready = qb_url and qb_auth
    components.append({
        "name": "qbittorrent",
        "label": "qBittorrent",
        "enabled": qb_enabled,
        "status": "ready" if qb_ready else ("incomplete" if qb_enabled else "not_configured"),
    })
    if qb_enabled and not qb_ready:
        issues.append({
            "code": "qbittorrent_incomplete",
            "severity": "error",
            "message": "qBittorrent 配置不完整，需要地址以及 API Key 或用户名/密码。",
        })

    strm_schedule = _state(value("STRM_SCHEDULE_ENABLED"))
    strm_sources = bool(value("GY_STRM_SOURCE_DIRS"))
    strm_ready = strm_sources and bool(value("GY_STRM_BASE_URL")) and bool(value("STRM_ROOT"))
    components.append({
        "name": "strm",
        "label": "STRM",
        "enabled": strm_schedule or strm_sources,
        "status": "ready" if strm_ready else ("incomplete" if strm_schedule or strm_sources else "not_configured"),
    })
    if strm_schedule and not strm_ready:
        issues.append({
            "code": "strm_schedule_incomplete",
            "severity": "error",
            "message": "STRM 定时任务已启用，但来源、播放地址或输出目录尚不完整。",
        })

    ai_enabled = _state(value("AI_RECOGNITION_ENABLED"))
    ai_provider_ready = bool(
        value("AGENT_LLM_API_URL") and value("AGENT_LLM_MODEL")
    )
    components.append({
        "name": "ai_recognition",
        "label": "AI 识别回退",
        "enabled": ai_enabled,
        "status": "ready" if ai_enabled and ai_provider_ready else ("incomplete" if ai_enabled else "disabled"),
    })
    if ai_enabled and not ai_provider_ready:
        issues.append({
            "code": "ai_recognition_incomplete",
            "severity": "error",
            "message": "AI 识别回退已启用，请先在 Media Agent 设置中补全模型连接。",
        })

    errors = sum(item["severity"] == "error" for item in issues)
    warnings = sum(item["severity"] == "warning" for item in issues)
    status = "healthy" if not issues else ("degraded" if not errors else "attention")
    summary = "关键配置检查通过" if not issues else f"发现 {errors} 个错误、{warnings} 个提醒"
    return ToolResult(
        ok=errors == 0,
        status=status,
        summary=summary,
        data={"components": components, "issues": issues, "counts": {"errors": errors, "warnings": warnings}},
        evidence=[Evidence("config", "仅检查配置项是否存在及组合是否完整；未读取或返回凭据内容。", _now())],
        suggestions=[item["message"] for item in issues[:5]],
    )
