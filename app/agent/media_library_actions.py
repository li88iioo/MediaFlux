"""已配置 Jellyfin / Emby 媒体库的受控精准刷新。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import hmac
import json
import secrets
from typing import Any

from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.agent.result_projection import sanitize_public_text
from app.modules.media_server_profiles import MediaServerProfile, list_configured_profiles
from app.modules.web_secret import get_web_secret

_ALLOWED_PROVIDERS = {"auto", "jellyfin", "emby"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def media_library_refresh_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != {"provider", "library_name"}:
        raise AgentToolError(
            "library.refresh_library 只接受 provider 和 library_name 参数"
        )
    provider = str(arguments.get("provider") or "auto").strip().lower()
    if provider not in _ALLOWED_PROVIDERS:
        raise AgentToolError("provider 仅支持 auto、jellyfin 或 emby")
    library_name = sanitize_public_text(arguments.get("library_name"), limit=80)
    if not library_name:
        raise AgentToolError("library_name 不能为空")
    return {"provider": provider, "library_name": library_name}


def _client_for(profile: MediaServerProfile):
    if profile.server_type == "jellyfin":
        from app.clients.jellyfin import JellyfinClient
        return JellyfinClient(profile.url, profile.credential)
    if profile.server_type == "emby":
        from app.clients.emby import EmbyClient
        return EmbyClient(profile.url, profile.credential)
    raise AgentToolError("媒体服务器类型无效", code="precondition_failed")


def _resolve(arguments: dict[str, Any]) -> dict[str, Any]:
    provider = str(arguments["provider"])
    requested_name = str(arguments["library_name"])
    profiles = [
        profile for profile in list_configured_profiles()
        if profile.enabled and profile.configured
        and (provider == "auto" or profile.server_type == provider)
    ]
    if not profiles:
        raise AgentToolError(
            "没有可用的已启用媒体服务器", code="precondition_failed"
        )
    matches: list[dict[str, Any]] = []
    queried_profiles = 0
    unavailable_profiles: list[str] = []
    for profile in profiles:
        try:
            folders = _client_for(profile).list_virtual_folders()
        except Exception:
            unavailable_profiles.append(profile.label)
            continue
        queried_profiles += 1
        for folder in folders:
            name = sanitize_public_text(folder.get("name"), limit=80)
            library_id = str(folder.get("id") or "").strip()
            if name.casefold() == requested_name.casefold() and library_id:
                matches.append({
                    "profile": profile,
                    "library_id": library_id,
                    "library_name": name,
                })
    if not matches:
        if not queried_profiles:
            raise AgentToolError(
                "暂时无法读取已配置媒体服务器的媒体库",
                code="precondition_failed",
            )
        detail = "；部分媒体服务器暂时不可用" if unavailable_profiles else ""
        raise AgentToolError(
            f"没有找到名为《{requested_name}》的媒体库{detail}",
            code="precondition_failed",
        )
    if len(matches) > 1:
        raise AgentToolError(
            "该媒体库名称在多个服务器中重复，请明确 Jellyfin 或 Emby",
            code="selection_required",
        )
    return matches[0]


def _fingerprint(scope: dict[str, Any]) -> str:
    profile: MediaServerProfile = scope["profile"]
    payload = json.dumps({
        "server_type": profile.server_type,
        "url": profile.url.rstrip("/"),
        "credential": profile.credential,
        "enabled": profile.enabled,
        "library_id": scope["library_id"],
        "library_name": scope["library_name"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(
        get_web_secret().encode("utf-8"),
        b"mediaflux-agent-library-refresh:v1\0" + payload,
        hashlib.sha256,
    ).hexdigest()


def prepare_refresh_media_library(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    normalized = media_library_refresh_arguments(arguments)
    scope = _resolve(normalized)
    profile: MediaServerProfile = scope["profile"]
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary=(
            f"确认后将通知 {profile.label} 扫描媒体库"
            f"《{scope['library_name']}》"
        ),
        data={
            "provider": profile.server_type,
            "library": scope["library_name"],
            "effects": [
                "只刷新名称唯一匹配的一个已配置媒体库。",
                "不会刷新其他媒体库，也不会移动、覆盖或删除媒体文件。",
                "媒体服务器可能在后台重新扫描目录和更新元数据。",
            ],
        },
        evidence=[Evidence(
            "media_server_library",
            "已从当前媒体服务器实时列表解析唯一媒体库；内部 ID、地址和凭据仅参与确认指纹。",
            _now(),
        )],
    ), _fingerprint(scope)


def refresh_media_library(_arguments: dict[str, Any]) -> ToolResult:
    raise AgentToolError("该动作需要先预检并确认", code="confirmation_required")


def refresh_media_library_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    normalized = media_library_refresh_arguments(arguments)
    scope = _resolve(normalized)
    if not secrets.compare_digest(
        _fingerprint(scope), str(expected_context or "")
    ):
        raise AgentToolError(
            "媒体服务器或媒体库已变化，请重新预检",
            code="confirmation_stale",
        )
    profile: MediaServerProfile = scope["profile"]
    try:
        refreshed = bool(_client_for(profile).refresh_library(scope["library_id"]))
    except Exception:
        refreshed = False
    if not refreshed:
        return ToolResult(
            ok=False,
            status="failed",
            summary=f"{profile.label} 媒体库刷新未完成",
            data={
                "operation": "refresh_library",
                "provider": profile.server_type,
                "library": scope["library_name"],
                "refreshed": False,
            },
            evidence=[Evidence(
                "media_server_library",
                "已向唯一匹配的媒体库提交刷新请求，但上游未确认接纳。",
                _now(),
            )],
            suggestions=["请测试媒体服务器连接后重试。"],
            error="媒体服务器未接纳刷新请求。",
        )
    return ToolResult(
        ok=True,
        status="accepted",
        summary=f"{profile.label} 已接纳媒体库《{scope['library_name']}》刷新",
        data={
            "operation": "refresh_library",
            "provider": profile.server_type,
            "library": scope["library_name"],
            "refreshed": True,
        },
        evidence=[Evidence(
            "media_server_library",
            "已向唯一匹配的媒体库提交刷新请求；未返回内部 ID、地址或凭据。",
            _now(),
        )],
        suggestions=["媒体库扫描在服务器后台执行，可稍后检查入库结果。"],
    )
