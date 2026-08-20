"""跨模块的敏感凭据检测与日志安全脱敏。

本模块只依赖标准库，供 Agent、会话历史、日志和 Telegram 共同使用，
避免各边界维护不一致的正则，也避免 logger 与 Agent 模块形成循环依赖。
"""
from __future__ import annotations

import re
from urllib.parse import unquote

_ASSIGNMENT_RE = re.compile(
    r'''(?ix)
    (?P<prefix>
        (?<![\w-])["']?
        (?P<label>[a-z][a-z0-9_-]{0,80})
        ["']?\s*(?::|=|\bis\b)\s*["']?(?!//)
    )
    (?P<value>\$\{[^}\r\n]+\}|<[^>\r\n]+>|[^"'\s,;&}\]]{1,512})
    '''
)
_CHINESE_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<prefix>(?:密码|口令|密钥|令牌|访问令牌|授权码|凭据|凭证|授权)"
    r"\s*(?::|：|=|是|为)\s*)(?P<value>\S+)"
)
_AUTH_SCHEME_RE = re.compile(
    r"(?ix)(?P<prefix>\b(?:authorization\b\s*[:=]?\s*)?"
    r"(?P<scheme>bearer|basic)\s+)(?P<value>[A-Za-z0-9._~+/=$-]{1,})"
)
_AUTHORIZATION_HEADER_RE = re.compile(
    r"(?im)(?P<prefix>\b(?:proxy-)?authorization\s*[:=]\s*)"
    r"(?P<value>[^\r\n]+)"
)
_MEDIA_BROWSER_AUTH_RE = re.compile(
    r'''(?ix)(?P<prefix>\bauthorization\s*[:=]\s*mediabrowser\s+token\s*=\s*["']?)'''
    r'''(?P<value>[^"'\s,;&}\]]+)'''
)
_PROVIDER_TOKEN_RE = re.compile(
    r"(?ix)(?:"
    r"\bsk-[A-Za-z0-9_-]{16,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bxox[baprs]-[A-Za-z0-9-]{16,}"
    r"|\bAKIA[A-Z0-9]{16}\b"
    r"|\b\d{6,}:[A-Za-z0-9_-]{20,}\b"
    r")"
)
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_URL_USERINFO_RE = re.compile(
    r"(?i)(?P<prefix>\b[a-z][a-z0-9+.-]{1,20}://)(?P<userinfo>[^\s/@]+:[^\s/@]+)@"
)
_COOKIE_HEADER_RE = re.compile(
    r"(?ix)(?P<prefix>\b(?:cookie|set-cookie)\s*[:=]\s*)(?P<value>[^\r\n]+)"
)
_BOT_URL_RE = re.compile(r"(?i)(?P<prefix>/bot\d+:)(?P<value>[^/\s?#]+)")
_URL_QUERY_ASSIGNMENT_RE = re.compile(
    r'''(?ix)(?P<prefix>(?:[?&;]|&amp;)(?P<label>[a-z_][a-z0-9_.~-]{0,80})=)'''
    r'''(?P<value>[^&#\s"']{1,1024})'''
)
_PLACEHOLDER_RE = re.compile(
    r"(?ix)^(?:"
    r"(?:your|example|sample|dummy|test|replace(?:[_ -]?me)?)[_ -]*"
    r"(?:api[_ -]?key|token|password|secret|credential)?"
    r"|(?:api[_ -]?key|token|password|secret|credential)[_ -]*here"
    r"|x{2,}|\*{2,}"
    r")$"
)
_DOCUMENTATION_WORDS = frozenset(
    {
        "authentication",
        "configuration",
        "documentation",
        "example",
        "explained",
        "format",
        "guide",
        "handling",
        "header",
        "mean",
        "policy",
        "rotation",
        "security",
        "syntax",
        "usage",
    }
)
_EXACT_SENSITIVE_LABELS = frozenset(
    {
        "authorization",
        "auth",
        "api_key",
        "apikey",
        "access_token",
        "accesstoken",
        "refresh_token",
        "refreshtoken",
        "token",
        "password",
        "passwd",
        "pwd",
        "secret",
        "credential",
        "credentials",
        "signature",
        "sign",
        "sig",
        "_sig",
        "passkey",
        "authkey",
        "rsskey",
        "cookie",
        "dbcl2",
        "session",
        "sessionid",
        "session_id",
        "x_emby_token",
        "x_mediabrowser_token",
    }
)
_SENSITIVE_SUFFIXES = (
    "_api_key",
    "_apikey",
    "_access_token",
    "_refresh_token",
    "_bot_token",
    "_token",
    "_password",
    "_passwd",
    "_secret_key",
    "_secret",
    "_credential",
    "_credentials",
    "_passkey",
    "_authkey",
    "_rsskey",
    "_signature",
    "_session_token",
    "_session_secret",
    "_session_id",
    "_sessionid",
    "_dbcl2",
)


def _decoded_text(value: object) -> str:
    text = str(value or "")
    for _ in range(2):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return text


def _is_sensitive_label(value: object) -> bool:
    label = re.sub(r"[-\s]+", "_", str(value or "").strip().casefold())
    return label in _EXACT_SENSITIVE_LABELS or any(
        label.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES
    )


def is_sensitive_key(value: object) -> bool:
    """判断配置键、JSON 字段或查询参数名是否代表敏感值。"""
    return _is_sensitive_label(value)


def _contains_only_documentation_words(value: object) -> bool:
    words = re.findall(r"[a-z]+", str(value or "").casefold())
    return bool(words) and all(word in _DOCUMENTATION_WORDS for word in words)


def _is_documentation_authorization_value(value: object) -> bool:
    candidate = str(value or "").strip()
    if _is_placeholder_or_documentation(candidate):
        return True
    parts = candidate.split()
    if len(parts) >= 2 and parts[0].casefold() in {"bearer", "basic"}:
        if _is_placeholder_or_documentation(parts[1]):
            return True
        return _contains_only_documentation_words(" ".join(parts[1:]))
    if len(parts) < 2:
        return _contains_only_documentation_words(candidate)
    return _is_placeholder_or_documentation(" ".join(parts[1:])) or (
        _contains_only_documentation_words(" ".join(parts[1:]))
    )


def _redact_authorization_header(match: re.Match[str]) -> str:
    value = match.group("value").strip()
    if _is_documentation_authorization_value(value):
        return match.group(0)
    parts = value.split(None, 2)
    if len(parts) >= 2 and parts[0].casefold() in {"bearer", "basic"}:
        suffix = f" {parts[2]}" if len(parts) == 3 else ""
        return f'{match.group("prefix")}{parts[0]} ********{suffix}'
    return f'{match.group("prefix")}********'


def _is_authorization_scheme_marker(label: object, value: object) -> bool:
    normalized = re.sub(r"[-\s]+", "_", str(label or "").strip().casefold())
    return normalized in {"authorization", "auth"} and str(value or "").casefold() in {
        "bearer",
        "basic",
        "mediabrowser",
    }


def _is_placeholder_or_documentation(value: object) -> bool:
    candidate = str(value or "").strip()
    if not candidate:
        return True
    if (
        (candidate.startswith("${") and candidate.endswith("}"))
        or (candidate.startswith("<") and candidate.endswith(">"))
    ):
        return True
    candidate = candidate.lstrip("$").strip().strip('"\'').rstrip(".?:")
    if not candidate:
        return True
    return bool(_PLACEHOLDER_RE.fullmatch(candidate)) or (
        candidate.casefold() in _DOCUMENTATION_WORDS
    )


def contains_sensitive_credential(value: object) -> bool:
    """判断文本是否包含不应写入历史或发送给外部 Provider 的凭据。"""
    text = _decoded_text(value)
    if _URL_USERINFO_RE.search(text) or _PROVIDER_TOKEN_RE.search(text) or _JWT_RE.search(text):
        return True
    for match in _AUTHORIZATION_HEADER_RE.finditer(text):
        if not _is_documentation_authorization_value(match.group("value")):
            return True
    for match in _AUTH_SCHEME_RE.finditer(text):
        if not _is_placeholder_or_documentation(match.group("value")):
            return True
    for match in _MEDIA_BROWSER_AUTH_RE.finditer(text):
        if not _is_placeholder_or_documentation(match.group("value")):
            return True
    for match in _COOKIE_HEADER_RE.finditer(text):
        if not _is_placeholder_or_documentation(match.group("value")):
            return True
    for match in _URL_QUERY_ASSIGNMENT_RE.finditer(text):
        if _is_sensitive_label(match.group("label")) and not _is_placeholder_or_documentation(
            match.group("value")
        ):
            return True
    for match in _ASSIGNMENT_RE.finditer(text):
        if _is_authorization_scheme_marker(match.group("label"), match.group("value")):
            continue
        if _is_sensitive_label(match.group("label")) and not _is_placeholder_or_documentation(
            match.group("value")
        ):
            return True
    for match in _CHINESE_ASSIGNMENT_RE.finditer(text):
        if not _is_placeholder_or_documentation(match.group("value")):
            return True
    return False


def _redact_sensitive_text_once(value: object) -> str:
    message = str(value or "")
    message = _BOT_URL_RE.sub(lambda match: f'{match.group("prefix")}********', message)
    message = _AUTHORIZATION_HEADER_RE.sub(_redact_authorization_header, message)
    message = _URL_USERINFO_RE.sub(
        lambda match: f'{match.group("prefix")}********@', message
    )
    message = _MEDIA_BROWSER_AUTH_RE.sub(
        lambda match: f'{match.group("prefix")}********', message
    )
    message = _AUTH_SCHEME_RE.sub(
        lambda match: (
            match.group(0)
            if _is_placeholder_or_documentation(match.group("value"))
            else f'{match.group("prefix")}********'
        ),
        message,
    )
    message = _COOKIE_HEADER_RE.sub(
        lambda match: (
            match.group(0)
            if _is_placeholder_or_documentation(match.group("value"))
            else f'{match.group("prefix")}********'
        ),
        message,
    )

    def redact_url_query_assignment(match: re.Match[str]) -> str:
        if not _is_sensitive_label(match.group("label")):
            return match.group(0)
        if _is_placeholder_or_documentation(match.group("value")):
            return match.group(0)
        return f'{match.group("prefix")}********'

    message = _URL_QUERY_ASSIGNMENT_RE.sub(redact_url_query_assignment, message)

    def redact_assignment(match: re.Match[str]) -> str:
        if _is_authorization_scheme_marker(match.group("label"), match.group("value")):
            return match.group(0)
        if not _is_sensitive_label(match.group("label")):
            return match.group(0)
        if _is_placeholder_or_documentation(match.group("value")):
            return match.group(0)
        return f'{match.group("prefix")}********'

    message = _ASSIGNMENT_RE.sub(redact_assignment, message)
    message = _CHINESE_ASSIGNMENT_RE.sub(
        lambda match: (
            match.group(0)
            if _is_placeholder_or_documentation(match.group("value"))
            else f'{match.group("prefix")}********'
        ),
        message,
    )
    message = _PROVIDER_TOKEN_RE.sub("********", message)
    message = _JWT_RE.sub("********", message)
    return message


def redact_sensitive_text(value: object) -> str:
    """将日志/通知中的凭据替换为固定掩码，不改变普通文本。"""
    message = _redact_sensitive_text_once(value)
    if not contains_sensitive_credential(message):
        return message

    # 检测器会解码最多两轮 URL 编码；脱敏器也必须遵守同一安全边界。
    # 只有仍含凭据时才输出解码后的脱敏文本，避免改变普通 URL 日志。
    decoded = _decoded_text(message)
    if decoded != message:
        message = _redact_sensitive_text_once(decoded)
    if contains_sensitive_credential(message):
        # 未知格式宁可牺牲单条日志可读性，也不能把已判定的凭据写入日志。
        return "********"
    return message
