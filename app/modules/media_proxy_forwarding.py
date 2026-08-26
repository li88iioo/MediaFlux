"""媒体反代可信代理来源的校验、序列化与运行时解析。"""
from __future__ import annotations

import ipaddress
import json
import re
from typing import Any, Mapping


MAX_TRUSTED_PROXY_CIDRS = 64
_SPLIT_RE = re.compile(r"[\n,]+")


def _row_value(row: Mapping[str, Any], key: str, default: Any = "") -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _raw_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError("可信代理地址格式无效") from exc
            if not isinstance(decoded, list):
                raise ValueError("可信代理地址必须是列表")
            return decoded
        return _SPLIT_RE.split(text)
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    raise ValueError("可信代理地址必须是列表或每行一个地址")


def normalize_trusted_proxy_cidrs(value: Any) -> tuple[str, ...]:
    """规范化单 IP/CIDR 列表；拒绝通配符与全网信任。"""
    raw_values = _raw_values(value)
    if len(raw_values) > MAX_TRUSTED_PROXY_CIDRS:
        raise ValueError(f"可信代理来源最多允许 {MAX_TRUSTED_PROXY_CIDRS} 项")

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        item = str(raw or "").strip()
        if not item:
            continue
        if item == "*":
            raise ValueError("可信代理来源不能使用通配符 *")
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError as exc:
            raise ValueError(f"可信代理地址无效：{item}") from exc
        if network.prefixlen == 0:
            raise ValueError("可信代理来源不能信任整个 IPv4 或 IPv6 网络")
        canonical = network.with_prefixlen
        if canonical in seen:
            continue
        seen.add(canonical)
        normalized.append(canonical)
    return tuple(normalized)


def encode_trusted_proxy_cidrs(value: Any) -> str:
    return json.dumps(
        list(normalize_trusted_proxy_cidrs(value)),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_trusted_proxy_cidrs(value: Any, *, strict: bool = False) -> tuple[str, ...]:
    try:
        return normalize_trusted_proxy_cidrs(value)
    except ValueError:
        if strict:
            raise
        return ()


def media_proxy_forwarding_config(
    row: Mapping[str, Any],
) -> tuple[bool, tuple[str, ...]]:
    """读取实例运行配置；启用时缺少有效可信来源即拒绝启动。"""
    trust_forwarded_headers = bool(
        int(_row_value(row, "trust_forwarded_headers", 0) or 0)
    )
    trusted_proxy_cidrs = decode_trusted_proxy_cidrs(
        _row_value(row, "trusted_proxy_cidrs_json", "[]"),
        strict=trust_forwarded_headers,
    )
    if trust_forwarded_headers and not trusted_proxy_cidrs:
        raise ValueError("启用转发头信任时至少填写一个可信代理地址")
    return trust_forwarded_headers, trusted_proxy_cidrs
