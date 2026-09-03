"""Media Agent 的服务端配置连通性只读动作。"""

from __future__ import annotations

import time
import unicodedata
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import requests

from app import config
from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolResult

_ALLOWED_ARGUMENTS = {"server_type"}


class _RedirectNotAllowed(Exception):
    """媒体连接测试不允许离开已配置目标。"""


_SERVER_CONFIG = {
    "jellyfin": {
        "label": "Jellyfin",
        "enabled": "JELLYFIN_ENABLED",
        "url": "JELLYFIN_URL",
        "credential": "JELLYFIN_API_KEY",
    },
    "emby": {
        "label": "Emby / Jellyfin 10.x",
        "enabled": "EMBY_ENABLED",
        "url": "EMBY_URL",
        "credential": "EMBY_TOKEN",
    },
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _visible_text(value: Any, *, limit: int, default: str = "") -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    cleaned = "".join(
        " " if unicodedata.category(char).startswith("C") else char
        for char in normalized
    )
    return " ".join(cleaned.split())[:limit] or default


def _safe_identity_text(
    value: Any,
    *,
    limit: int,
    default: str,
    forbidden: tuple[str, ...],
) -> str:
    text = _visible_text(value, limit=limit, default=default)
    folded = text.casefold()
    if "://" in folded or folded.startswith(("/", "\\")):
        return default
    for secret in forbidden:
        normalized = str(secret or "").strip().casefold()
        if normalized and normalized in folded:
            return default
    return text


def media_server_arguments(arguments: dict[str, Any]) -> dict[str, str]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    extra = set(arguments) - _ALLOWED_ARGUMENTS
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    server_type = arguments.get("server_type")
    if not isinstance(server_type, str):
        raise AgentToolError("server_type 必须是字符串")
    server_type = unicodedata.normalize("NFKC", server_type).strip().lower()
    if server_type not in _SERVER_CONFIG:
        raise AgentToolError("server_type 仅支持 jellyfin 或 emby")
    return {"server_type": server_type}


def _safe_result(
    server_type: str,
    *,
    ok: bool,
    status: str,
    summary: str,
    suggestions: list[str] | None = None,
    data: dict[str, Any] | None = None,
) -> ToolResult:
    payload: dict[str, Any] = {
        "server_type": server_type,
        "connection_status": status,
    }
    if data:
        payload.update(data)
    return ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data=payload,
        evidence=[
            Evidence(
                "configured_media_server",
                "使用服务端当前生效配置验证媒体服务器，不返回地址或访问凭据。",
                _now(),
            )
        ],
        suggestions=suggestions or [],
        error="" if ok else summary,
    )


def _configured_endpoint(server_type: str) -> tuple[str, str] | None:
    keys = _SERVER_CONFIG[server_type]
    if not config.get_bool(keys["enabled"], False):
        return None
    url = str(config.get(keys["url"], "") or "").strip().rstrip("/")
    credential = str(config.get(keys["credential"], "") or "").strip()
    try:
        parsed = urlsplit(url)
        port_valid = parsed.port is None or 1 <= parsed.port <= 65535
    except ValueError:
        return "", ""
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
        or not port_valid
        or not credential
    ):
        return "", ""
    return url, credential


def _request_headers(server_type: str, credential: str) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    headers["Authorization"] = f'MediaBrowser Token="{credential}"'
    if server_type == "emby":
        headers["X-Emby-Token"] = credential
    return headers


def _failure_status(exc: Exception) -> tuple[str, str, list[str]]:
    if isinstance(exc, _RedirectNotAllowed):
        return (
            "redirect_not_allowed",
            "媒体服务器拒绝固定目标连接",
            ["请检查媒体服务器地址或反向代理重定向配置。"],
        )
    if isinstance(exc, requests.Timeout):
        return (
            "timeout",
            "媒体服务器连接超时",
            ["请检查服务状态、网络路由或反向代理后重试。"],
        )
    if isinstance(exc, requests.ConnectionError):
        return (
            "connection",
            "无法连接媒体服务器",
            ["请确认服务已启动且当前配置地址可从 MediaFlux 访问。"],
        )
    if isinstance(exc, requests.HTTPError):
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code in {401, 403}:
            return (
                "authentication",
                "媒体服务器鉴权失败",
                ["请在设置中更新访问凭据后重试。"],
            )
        if status_code == 404:
            return (
                "not_found",
                "媒体服务器接口不可用",
                ["请确认服务类型与反向代理路径配置正确。"],
            )
        return (
            "http_error",
            "媒体服务器返回异常状态",
            ["请检查媒体服务器日志与反向代理配置。"],
        )
    if isinstance(exc, (ValueError, TypeError)):
        return (
            "invalid_response",
            "媒体服务器响应格式无效",
            ["请确认目标确为兼容的 Jellyfin 或 Emby 服务。"],
        )
    return (
        "unavailable",
        "媒体服务器暂时不可用",
        ["请稍后重试，或从设置页重新校验媒体服务器配置。"],
    )


def test_media_server(arguments: dict[str, Any]) -> ToolResult:
    server_type = str(arguments["server_type"])
    label = str(_SERVER_CONFIG[server_type]["label"])
    configured = _configured_endpoint(server_type)
    if configured is None:
        return _safe_result(
            server_type,
            ok=False,
            status="disabled",
            summary=f"{label} 当前未启用",
            suggestions=["可在设置中启用并配置该媒体服务器。"],
        )
    url, credential = configured
    if not url or not credential:
        return _safe_result(
            server_type,
            ok=False,
            status="not_configured",
            summary=f"{label} 配置不完整",
            suggestions=["请在设置中补全服务地址与访问凭据。"],
        )

    started = time.perf_counter()
    try:
        response = requests.get(
            f"{url}/System/Info",
            headers=_request_headers(server_type, credential),
            params=None,
            timeout=(3.5, 8),
            allow_redirects=False,
        )
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and 300 <= status_code < 400:
            raise _RedirectNotAllowed()
        response.raise_for_status()
        info = response.json()
        if not isinstance(info, dict):
            raise ValueError("invalid response")
        latency_ms = max(1, round((time.perf_counter() - started) * 1000))
        parsed_url = urlsplit(url)
        forbidden = (url, parsed_url.netloc, parsed_url.hostname or "", credential)
        product_hints = (
            _safe_identity_text(
                info.get("ProductName"),
                limit=40,
                default="",
                forbidden=forbidden,
            ),
            _safe_identity_text(
                info.get("Product"),
                limit=40,
                default="",
                forbidden=forbidden,
            ),
        )
        server_name = _safe_identity_text(
            info.get("ServerName") or info.get("Name"),
            limit=120,
            default=label,
            forbidden=forbidden,
        )
        product_fingerprint = " ".join(product_hints).casefold()
        product_detected = True
        if "jellyfin" in product_fingerprint:
            product = "Jellyfin"
        elif "emby" in product_fingerprint:
            product = "Emby"
        else:
            product_detected = False
            product = "Jellyfin" if server_type == "jellyfin" else "Emby"
        version = _safe_identity_text(
            info.get("Version") or info.get("ServerVersion"),
            limit=64,
            default="未知",
            forbidden=forbidden,
        )
        return _safe_result(
            server_type,
            ok=True,
            status="success",
            summary=f"{product} 连接正常",
            data={
                "server_name": server_name,
                "product": product,
                "product_detected": product_detected,
                "version": version,
                "latency_ms": latency_ms,
            },
        )
    except Exception as exc:
        status, summary, suggestions = _failure_status(exc)
        return _safe_result(
            server_type,
            ok=False,
            status=status,
            summary=summary,
            suggestions=suggestions,
        )
