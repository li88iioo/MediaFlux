"""Provider 原始响应的有界安全投影。"""

from __future__ import annotations

import math
from typing import Any

from app.agent.media_links import sanitize_media_open_url
from app.agent.public_safety import (
    sanitize_public_text,
    sanitize_untrusted_filename,
)

_SENSITIVE_KEYS = frozenset(
    {
        "path",
        "save_path",
        "content_path",
        "url",
        "web_url",
        "image_url",
        "token",
        "api_key",
        "authorization",
        "cookie",
        "password",
        "username",
        "tracker",
        "magnet",
        "hash",
        "device_id",
        "headers",
        "response_headers",
    }
)


def _safe_key(value: Any) -> str:
    return str(value or "").strip()[:64]


def _project_filename(value: str) -> str:
    """仅为明确的 name 字段保留 dotted filename；路径仍按普通文本拒绝。"""
    if "/" in value or "\\" in value:
        return sanitize_public_text(value, limit=500)
    return sanitize_untrusted_filename(value, limit=500)


def project_provider_value(
    value: Any,
    *,
    depth: int = 0,
    max_depth: int = 4,
    max_items: int = 32,
    allow_open_url: bool = False,
) -> Any:
    if depth > max_depth:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return max(-(2**63), min(value, 2**63 - 1))
    if isinstance(value, float):
        return round(value, 4) if math.isfinite(value) else 0.0
    if isinstance(value, str):
        return sanitize_public_text(value, limit=500)
    if isinstance(value, (list, tuple)):
        return [
            project_provider_value(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                allow_open_url=allow_open_url,
            )
            for item in list(value)[:max_items]
        ]
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:max_items]:
            key = _safe_key(raw_key)
            normalized_key = key.casefold()
            if not key or normalized_key in _SENSITIVE_KEYS:
                continue
            if normalized_key == "open_url":
                if allow_open_url:
                    open_url = sanitize_media_open_url(raw_value)
                    if open_url:
                        projected[key] = open_url
            elif normalized_key == "name" and isinstance(raw_value, str):
                projected[key] = _project_filename(raw_value)
            else:
                projected[key] = project_provider_value(
                    raw_value,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_items=max_items,
                    allow_open_url=allow_open_url,
                )
        return projected
    return sanitize_public_text(value, limit=160)
