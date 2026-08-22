"""媒体反代诊断字段的最小化与脱敏工具。"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlsplit

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:(?!\s)")
_DANGEROUS_OPAQUE_URI_SCHEMES = {
    "data",
    "ed2k",
    "file",
    "ftp",
    "ftps",
    "javascript",
    "magnet",
    "mailto",
    "sftp",
    "ssh",
    "urn",
    "vbscript",
}
_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)(?:^|[?&#;])(?:api[_-]?key|token|access[_-]?token|"
    r"refresh[_-]?token|authorization|auth|signature|sign|expires?)="
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:api[_-]?key|token|access[_-]?token|refresh[_-]?token|"
    r"authorization|auth|signature|sign|expires?)\s*="
)

def safe_media_name(value: Any, *, path_value: bool = False,
                    limit: int = 256) -> str:
    """返回可展示名称；路径/URI 只保留 basename，查询、authority 与凭据不返回。"""
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""

    decoded = unquote(raw) if "%" in raw else raw
    try:
        parsed = urlsplit(decoded)
    except ValueError:
        if "://" in decoded or decoded.startswith("//"):
            return ""
        parsed = None

    has_uri_scheme = bool(
        parsed is not None
        and parsed.scheme
        and _URI_SCHEME_RE.match(decoded)
        and not _WINDOWS_DRIVE_RE.match(decoded)
    )
    has_sensitive_query = bool(_SENSITIVE_QUERY_RE.search(decoded))
    explicit_uri = bool(
        parsed is not None
        and (
            parsed.netloc
            or decoded.startswith("//")
            or "://" in decoded
        )
    )
    path_style_uri = bool(
        has_uri_scheme
        and parsed is not None
        and str(parsed.path or "").startswith("/")
    )
    opaque_uri = bool(
        has_uri_scheme
        and not explicit_uri
        and parsed is not None
        and (
            str(parsed.scheme or "").lower() in _DANGEROUS_OPAQUE_URI_SCHEMES
            or "@" in str(parsed.path or "")
            or has_sensitive_query
            or bool(_SENSITIVE_ASSIGNMENT_RE.search(str(parsed.path or "")))
        )
    )
    if (
        opaque_uri
        and not parsed.netloc
        and not str(parsed.path or "").startswith("/")
    ):
        return ""
    is_uri = bool(
        explicit_uri or path_style_uri or (path_value and has_uri_scheme)
    )
    is_path = bool(
        path_value
        or explicit_uri
        or path_style_uri
        or decoded.startswith(("/", "\\"))
        or _WINDOWS_DRIVE_RE.match(decoded)
    )

    if is_path or has_sensitive_query:
        if parsed is not None:
            selected = (
                parsed.path
                if (is_uri or parsed.netloc or parsed.scheme)
                else (parsed.path or decoded)
            )
        else:
            selected = decoded.split("?", 1)[0].split("#", 1)[0]
        if not selected:
            return ""
        selected = selected.replace("\\", "/").rstrip("/")
        selected = selected.rsplit("/", 1)[-1]
    else:
        selected = raw

    selected = _CONTROL_RE.sub(" ", selected).strip()
    return selected[:max(1, int(limit))]
