"""媒体反代可复用的 Jellyfin / Emby 配置解析。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app import config


_PROFILE_KEYS = {
    "configured:jellyfin": (
        "jellyfin",
        "Jellyfin",
        "JELLYFIN_URL",
        "JELLYFIN_API_KEY",
        "JELLYFIN_ENABLED",
        "JELLYFIN_USER_ID",
    ),
    "configured:emby": (
        "emby",
        "Emby",
        "EMBY_URL",
        "EMBY_TOKEN",
        "EMBY_ENABLED",
        "EMBY_USER_ID",
    ),
}


@dataclass(frozen=True)
class MediaServerProfile:
    source: str
    server_type: str
    label: str
    url: str
    credential: str
    enabled: bool
    user_id: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.url and self.credential)

    def public_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "server_type": self.server_type,
            "label": self.label,
            "url": self.url,
            "enabled": self.enabled,
            "configured": self.configured,
            "user_configured": bool(self.user_id),
        }


def _profile(source: str) -> MediaServerProfile:
    try:
        server_type, label, url_key, credential_key, enabled_key, user_key = _PROFILE_KEYS[source]
    except KeyError as exc:
        raise ValueError("媒体服务器来源无效") from exc
    return MediaServerProfile(
        source=source,
        server_type=server_type,
        label=label,
        url=str(config.get(url_key, "") or "").strip(),
        credential=str(config.get(credential_key, "") or "").strip(),
        enabled=config.get_bool(enabled_key, False),
        user_id=str(config.get(user_key, "") or "").strip(),
    )


def list_configured_profiles() -> list[MediaServerProfile]:
    """返回看板中可供反代选择的 Jellyfin / Emby 配置。"""
    return [_profile(source) for source in _PROFILE_KEYS]


def _row_value(row: Mapping[str, Any], key: str, default: Any = "") -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def resolve_proxy_instance(row: Mapping[str, Any]) -> dict[str, Any]:
    """解析当前上游；配置凭据仅供管理探测，不替代下游用户令牌。"""
    resolved = dict(row)
    source = str(_row_value(row, "config_source", "custom") or "custom").strip()
    resolved["config_source"] = source
    if source == "custom":
        return resolved

    profile = _profile(source)
    if not profile.enabled:
        raise ValueError(f"{profile.label} 已在看板停用，请先启用")
    if not profile.url:
        raise ValueError(f"{profile.label} 上游地址未配置")
    if not profile.credential:
        raise ValueError(f"{profile.label} API 凭据未配置")
    resolved.update(
        server_type=profile.server_type,
        upstream_url=profile.url,
        api_key=profile.credential,
        source_label=profile.label,
    )
    return resolved
