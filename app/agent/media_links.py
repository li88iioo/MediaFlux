"""Agent 媒体条目的可信打开链接。

只接受已配置媒体服务器生成的 HTTP(S) 页面地址；若同一上游存在运行中的
媒体反代实例，则复用 MediaFlux 已配置的可访问主机并替换为反代端口。
本模块不会拼接凭据，也不会从用户文本接收 URL。
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app import config
from app.modules.media_server_profiles import MediaServerProfile, resolve_proxy_instance
from app.sensitive_data import contains_sensitive_credential

logger = logging.getLogger(__name__)

_WILDCARD_OR_LOOPBACK_HOSTS = frozenset(
    {"", "0.0.0.0", "127.0.0.1", "::", "::1", "localhost"}
)


def _row_value(row: Mapping[str, Any], key: str, default: Any = "") -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _origin(parsed) -> tuple[str, str, int] | None:
    scheme = str(parsed.scheme or "").casefold()
    host = str(parsed.hostname or "").rstrip(".").casefold()
    default_port = 443 if scheme == "https" else 80
    try:
        port = int(parsed.port or default_port)
    except (TypeError, ValueError, OverflowError):
        return None
    if not 1 <= port <= 65_535:
        return None
    return scheme, host, port


def _path_contains_base(item_path: str, base_path: str) -> bool:
    normalized_base = "/" + str(base_path or "").strip("/") if base_path else ""
    normalized_base = "" if normalized_base == "/" else normalized_base.rstrip("/")
    normalized_item = "/" + str(item_path or "").lstrip("/")
    if not normalized_base:
        return True
    return normalized_item == normalized_base or normalized_item.startswith(
        normalized_base + "/"
    )


def sanitize_media_open_url(value: object, *, expected_base: object = "") -> str:
    """返回可公开的媒体页面 URL；任何不确定输入都关闭失败。"""

    raw = str(value or "").strip()
    if not raw or len(raw) > 4_096 or any(char.isspace() for char in raw):
        return ""
    try:
        parsed = urlsplit(raw)
    except (TypeError, ValueError):
        return ""
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or _origin(parsed) is None
        or parsed.username is not None
        or parsed.password is not None
        or contains_sensitive_credential(raw)
    ):
        return ""

    base = str(expected_base or "").strip().rstrip("/")
    if base:
        try:
            base_parsed = urlsplit(base)
        except (TypeError, ValueError):
            return ""
        if (
            base_parsed.scheme.casefold() not in {"http", "https"}
            or not base_parsed.hostname
            or _origin(base_parsed) is None
            or base_parsed.username is not None
            or base_parsed.password is not None
            or base_parsed.query
            or base_parsed.fragment
            or _origin(parsed) != _origin(base_parsed)
            or not _path_contains_base(parsed.path, base_parsed.path)
        ):
            return ""
    return raw


def _normalized_server_base(value: object) -> tuple[str, str, int, str] | None:
    raw = str(value or "").strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or _origin(parsed) is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    path = "/" + str(parsed.path or "").strip("/") if parsed.path else ""
    path = "" if path == "/" else path.rstrip("/")
    origin = _origin(parsed)
    if origin is None:
        return None
    return (*origin, path.casefold())


def _reachable_host(value: object) -> str | None:
    host = str(value or "").strip().strip("[]").rstrip(".")
    if not host:
        return None
    normalized_host = host.casefold()
    if normalized_host in _WILDCARD_OR_LOOPBACK_HOSTS:
        return None
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        address = None
    if address is not None and (address.is_unspecified or address.is_loopback):
        return None
    return host


def _public_mediaflux_host(listen_host: object) -> str | None:
    explicit_host = _reachable_host(listen_host)
    if explicit_host:
        return explicit_host
    raw = str(config.get("GY_STRM_BASE_URL", "") or "").strip()
    try:
        parsed = urlsplit(raw)
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return _reachable_host(parsed.hostname)


def _proxy_base_url(row: Mapping[str, Any], runtime: Mapping[str, Any]) -> str:
    if not bool(runtime.get("running")):
        return ""
    host = _public_mediaflux_host(
        runtime.get("listen_host", _row_value(row, "listen_host", ""))
    )
    if not host:
        return ""
    raw_port = runtime.get("listen_port", _row_value(row, "listen_port", 0))
    try:
        port = int(raw_port)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not 1_024 <= port <= 65_535:
        return ""
    netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return urlunsplit(("http", netloc, "", "", "")).rstrip("/")


def _matching_proxy_base(*, server_type: str, server_url: str) -> str:
    """查找同一媒体上游的运行中反代；异常只降级为服务器直连。"""

    server_type_expected = str(server_type or "").strip().casefold()
    expected_base = _normalized_server_base(server_url)
    if expected_base is None:
        return ""
    try:
        from app.modules.media_proxy import get_media_proxy_manager
        from app.repositories.media_proxy import list_media_proxy_instances

        runtime_by_id = get_media_proxy_manager().status()
        rows = list_media_proxy_instances()
    except Exception as exc:  # noqa: BLE001 - 可选运行能力不可阻断媒体查询
        logger.debug("Agent 媒体链接读取反代状态失败 type=%s", type(exc).__name__)
        return ""
    if not isinstance(runtime_by_id, Mapping):
        return ""

    for row in rows:
        try:
            if not bool(int(_row_value(row, "enabled", 0) or 0)):
                continue
            instance_id = int(_row_value(row, "id", 0) or 0)
            runtime = runtime_by_id.get(instance_id) or runtime_by_id.get(
                str(instance_id), {}
            )
            if not isinstance(runtime, Mapping) or not runtime.get("running"):
                continue
            resolved = resolve_proxy_instance(row)
            resolved_type = str(resolved.get("server_type") or "").strip().casefold()
            if resolved_type != server_type_expected:
                continue
            if _normalized_server_base(resolved.get("upstream_url")) != expected_base:
                continue
            proxy_base = _proxy_base_url(row, runtime)
            if proxy_base:
                return proxy_base
        except (TypeError, ValueError, OverflowError):
            continue
    return ""


def media_open_url_resolver(
    *,
    server_type: object,
    server_url: object,
) -> Callable[[object], str]:
    """为同一查询批次构造链接解析器，反代状态只读取一次。"""

    normalized_type = str(server_type or "").strip().casefold()
    normalized_server_url = str(server_url or "").strip().rstrip("/")
    if normalized_type not in {"jellyfin", "emby"} or not normalized_server_url:
        return lambda _item_url: ""
    proxy_base = _matching_proxy_base(
        server_type=normalized_type,
        server_url=normalized_server_url,
    )

    def resolve(item_url: object) -> str:
        direct_url = sanitize_media_open_url(
            item_url,
            expected_base=normalized_server_url,
        )
        if not direct_url or not proxy_base:
            return direct_url
        parsed = urlsplit(direct_url)
        proxy = urlsplit(proxy_base)
        proxied = urlunsplit(
            (proxy.scheme, proxy.netloc, parsed.path, parsed.query, parsed.fragment)
        )
        return sanitize_media_open_url(proxied, expected_base=proxy_base)

    return resolve


def resolve_media_open_url(
    profile: MediaServerProfile,
    item_url: object,
) -> str:
    """优先返回匹配的运行中媒体反代链接，否则返回服务器原始页面链接。"""

    return media_open_url_resolver(
        server_type=profile.server_type,
        server_url=profile.url,
    )(item_url)
