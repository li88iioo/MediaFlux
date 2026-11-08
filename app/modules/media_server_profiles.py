"""Jellyfin / Emby 配置快照、固定目标连接与身份诊断。"""
from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from sqlite3 import Row
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import requests

from app import config
from app.sensitive_data import contains_sensitive_credential

if TYPE_CHECKING:
    from app.clients.base import MediaServerClient

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

    def create_client(self) -> MediaServerClient:
        """用同一配置快照构造客户端，显式用户优先于服务器默认用户。"""
        from app.clients.base import normalize_explicit_media_user_id
        from app.clients.emby import EmbyClient
        from app.clients.jellyfin import JellyfinClient

        user_id = normalize_explicit_media_user_id(self.user_id) if self.user_id else ""
        if self.server_type not in {"jellyfin", "emby"}:
            raise ValueError("媒体服务器类型不受支持")
        factory = JellyfinClient if self.server_type == "jellyfin" else EmbyClient
        client = factory(self.url, self.credential, timeout=10)
        if user_id:
            client._cached_user_id = user_id
        return client

    def fingerprint(self) -> str:
        from app.modules.web_secret import signed_context_fingerprint

        return signed_context_fingerprint(
            {
                "profile_ref": self.source,
                "server_type": self.server_type,
                "url": self.url.rstrip("/"),
                "credential": self.credential,
                "user_id": self.user_id,
                "enabled": self.enabled,
            },
            domain="provider-profile-revision",
        )

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
    values = config.get_many((url_key, credential_key, enabled_key, user_key))
    return MediaServerProfile(
        source=source,
        server_type=server_type,
        label=label,
        url=(values[url_key] or "").strip(),
        credential=(values[credential_key] or "").strip(),
        enabled=(values[enabled_key] or "").strip().lower() in {"1", "true", "yes", "on", "y"},
        user_id=(values[user_key] or "").strip(),
    )


def list_configured_profiles() -> list[MediaServerProfile]:
    """返回看板中可供反代选择的 Jellyfin / Emby 配置。"""
    return [_profile(source) for source in _PROFILE_KEYS]


_SLOT_LABELS = {"jellyfin": "Jellyfin 12", "emby": "Emby / Jellyfin 10.x"}
_VERSION_RE = re.compile(r"^(?P<major>0|[1-9]\d{0,2})(?:\.\d{1,4}){0,3}$")
_CONNECTION_LIMIT = threading.BoundedSemaphore(2)
_FAILURES = {
    "disabled": ("server_disabled", "媒体服务器当前未启用"),
    "not_configured": ("configuration_incomplete", "媒体服务器配置不完整"),
    "redirect_not_allowed": ("redirect_blocked", "媒体服务器拒绝固定目标连接"),
    "timeout": ("request_timeout", "媒体服务器连接超时"),
    "connection": ("connection_failed", "无法连接媒体服务器"),
    "authentication": ("authentication_failed", "媒体服务器鉴权失败"),
    "not_found": ("system_info_not_found", "媒体服务器接口不可用"),
    "http_error": ("upstream_http_error", "媒体服务器返回异常状态"),
    "invalid_response": ("invalid_system_info_response", "媒体服务器响应格式无效"),
    "unavailable": ("unexpected_upstream_failure", "媒体服务器暂时不可用"),
}


def _valid_endpoint(profile: MediaServerProfile) -> bool:
    try:
        parsed = urlsplit(profile.url.strip().rstrip("/"))
        return bool(profile.credential.strip() and parsed.scheme in {"http", "https"} and parsed.hostname
                    and parsed.username is None and parsed.password is None and not parsed.query and not parsed.fragment
                    and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        return False


def _identity(value, profile, *, limit):
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = " ".join("".join(" " if unicodedata.category(c).startswith("C") else c for c in text).split())
    parsed = urlsplit(profile.url)
    forbidden = (profile.url, parsed.netloc, parsed.hostname, profile.credential)
    if (not text or "://" in text or text.startswith(("/", "\\")) or contains_sensitive_credential(text)
            or any(secret and secret.strip().casefold() in text.casefold() for secret in forbidden)):
        return None
    return text[:limit]


def _compatibility(slot, product, major):
    if product == "unknown":
        return "review", "product_unrecognized"
    if slot == "jellyfin":
        if product == "emby" or major is not None and major <= 10:
            return "wrong_slot", "use_legacy_slot"
        if major is None:
            return "review", "version_unrecognized"
        return ("compatible", "jellyfin12_slot_compatible") if major >= 12 else ("review", "jellyfin_major_not_classified")
    if product == "emby":
        return "compatible", "emby_legacy_slot_compatible"
    if major is None:
        return "review", "version_unrecognized"
    if major == 10:
        return "compatible", "jellyfin10_legacy_slot_compatible"
    return ("wrong_slot", "use_jellyfin12_slot") if major >= 12 else ("review", "jellyfin_major_not_classified")


def _connection_node(slot, profile=None):
    enabled = profile.enabled if profile is not None else None
    configured = bool(enabled and _valid_endpoint(profile)) if profile is not None else None
    status = "disabled" if enabled is False else "not_configured" if configured is False else "unavailable"
    reason, error = _FAILURES[status]
    return {"slot": slot, "slot_label": _SLOT_LABELS[slot], "enabled": enabled, "configured": configured,
            "online": False if profile is not None else None, "connection_status": status,
            "server_name": None, "product": "unknown", "product_name": None, "product_detected": False,
            "version": None, "major_version": None, "latency_ms": None,
            "compatibility": status if status in {"disabled", "not_configured"} else "unavailable",
            "reason_code": reason, "error": error, "network_accessed": False}


def _probe_connection(profile: MediaServerProfile) -> dict[str, Any]:
    node = _connection_node(profile.server_type, profile)
    if not node["configured"]:
        return node
    started = time.perf_counter()
    headers = {"Accept": "application/json", "Authorization": f'MediaBrowser Token="{profile.credential}"'}
    if profile.server_type == "emby":
        headers["X-Emby-Token"] = profile.credential
    node["network_accessed"] = True
    try:
        response = requests.get(profile.url.strip().rstrip("/") + "/System/Info", headers=headers,
                                params=None, timeout=(3.5, 8), allow_redirects=False)
        if 300 <= response.status_code < 400:
            status = "redirect_not_allowed"
        else:
            response.raise_for_status()
            info = response.json()
            if not isinstance(info, dict):
                raise ValueError("invalid response")
            products = [_identity(info.get(key), profile, limit=40) for key in ("ProductName", "Product")]
            hints = " ".join(p for p in products if p).casefold()
            product = "jellyfin" if "jellyfin" in hints else "emby" if "emby" in hints else "unknown"
            version = _identity(info.get("Version") or info.get("ServerVersion"), profile, limit=64)
            matched = _VERSION_RE.fullmatch(version or "")
            major = int(matched.group("major")) if matched else None
            compatibility, reason = _compatibility(profile.server_type, product, major)
            node.update(online=True, connection_status="success", error="",
                        server_name=_identity(info.get("ServerName") or info.get("Name"), profile, limit=120),
                        product=product, product_name=next((p for p in products if p), None), product_detected=product != "unknown",
                        version=version if matched else None, major_version=major, compatibility=compatibility, reason_code=reason,
                        latency_ms=max(1, min(round((time.perf_counter() - started) * 1000), 11_500)))
            return node
    except requests.Timeout:
        status = "timeout"
    except requests.ConnectionError:
        status = "connection"
    except requests.HTTPError as exc:
        code = getattr(exc.response, "status_code", None)
        status = "authentication" if code in {401, 403} else "not_found" if code == 404 else "http_error"
    except (ValueError, TypeError):
        status = "invalid_response"
    except Exception:  # noqa: BLE001 - 上游异常只转固定错误，禁止回显凭据或响应正文。
        status = "unavailable"
    reason, error = _FAILURES[status]
    node.update(connection_status=status, reason_code=reason, error=error)
    return node


def _connection_count(values):
    values = list(values)
    return None if any(value is None for value in values) else sum(bool(value) for value in values)


def connection(server: str = "all") -> dict:
    """只探测服务端当前配置的固定系统信息接口，单槽位或全部槽位共用身份与兼容判断。"""
    if not isinstance(server, str):
        raise TypeError("server必须为all、jellyfin或emby字符串")
    server = unicodedata.normalize("NFKC", server).strip().casefold()
    if server not in {"all", "jellyfin", "emby"}:
        raise ValueError("server必须为all、jellyfin或emby")
    slots = list(_SLOT_LABELS) if server == "all" else [server]
    complete = False
    error = ""
    try:
        profiles = {profile.server_type: profile for profile in list_configured_profiles()}
        selected = [profiles[slot] for slot in slots]
    except Exception:  # noqa: BLE001 - 配置读取失败保留未知事实，不输出原异常。
        nodes = [_connection_node(slot) for slot in slots]
        error = "无法读取媒体服务器当前配置"
    else:
        nodes = [_connection_node(profile.server_type, profile) for profile in selected]
        if not _CONNECTION_LIMIT.acquire(timeout=0.5):
            error = "媒体服务器诊断正忙，请稍后重试"
            for node in nodes:
                if node["configured"]:
                    node.update(online=None, reason_code="diagnostic_busy")
        else:
            try:
                with ThreadPoolExecutor(max_workers=len(selected), thread_name_prefix="media-connection") as pool:
                    nodes = list(pool.map(_probe_connection, selected))
                complete = True
            except Exception:  # noqa: BLE001 - 并发探测未完成时保留unknown及固定错误。
                error = "暂时无法完成媒体服务器诊断"
                for node in nodes:
                    if node["configured"]:
                        node.update(online=None, reason_code="diagnostic_incomplete", network_accessed=None)
            finally:
                _CONNECTION_LIMIT.release()
    counts = {"enabled": _connection_count(n["enabled"] for n in nodes),
              "configured": _connection_count(n["configured"] for n in nodes),
              "online": _connection_count(n["online"] for n in nodes),
              "compatible": _connection_count(None if n["online"] is None else n["compatibility"] == "compatible" for n in nodes),
              "attention": _connection_count(None if n["online"] is None else n["enabled"] and n["compatibility"] != "compatible" for n in nodes)}
    if not complete:
        status, ok, summary = "unavailable", False, error
    elif server != "all" and nodes[0]["connection_status"] != "success":
        status, ok, summary = nodes[0]["connection_status"], False, nodes[0]["error"]
    elif counts["enabled"] == 0:
        status, ok, summary = "not_configured", True, "尚未启用媒体服务器"
    elif counts["online"] == 0 and counts["configured"]:
        status, ok, summary = "unavailable", False, "已配置的媒体服务器当前均不可用"
    elif counts["attention"]:
        status, ok, summary = "attention", True, f"媒体服务器诊断完成，{counts['attention']}个节点需要关注"
    else:
        status, ok, summary = "healthy", True, f"媒体服务器兼容性正常，{counts['compatible']}个节点在线"
    return {"ok": ok, "status": status, "summary": summary,
            "data": {"server": server, "probe_mode": "configured_endpoints", "probe_complete": complete,
                     "network_accessed": True if any(n["network_accessed"] is True for n in nodes) else
                         None if any(n["network_accessed"] is None for n in nodes) else False,
                     "counts": counts, "nodes": nodes,
                     "checked_at": datetime.now().astimezone().isoformat(timespec="seconds")},
            "error": "" if ok else summary}


def _row_value(row: Mapping[str, Any] | Row, key: str, default: Any = "") -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def resolve_proxy_instance(row: Mapping[str, Any] | Row) -> dict[str, Any]:
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
