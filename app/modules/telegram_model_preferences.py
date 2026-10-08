"""Telegram Agent 按会话持久化模型选择，复用 settings_kv。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time

from app import config
from app import database as db
from app.agent.model_catalog import normalize_provider_model_id

_MODEL_CALLBACK_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,16}$")
_MODEL_CALLBACK_TTL_SECONDS = 60 * 60
_THREAD_RE = re.compile(r"^[1-9][0-9]{0,18}$")


def _key(owner: str, session_id: str) -> str:
    owner_value = str(owner or "").strip()
    session_value = str(session_id or "").strip()
    if not owner_value or not session_value:
        raise ValueError("Telegram model preference scope is invalid")
    digest = hashlib.sha256(
        b"mediaflux-telegram-model:v1\0"
        + owner_value.encode("utf-8")
        + b"\0"
        + session_value.encode("utf-8")
    ).hexdigest()
    return f"telegram_model_preference:v1:{digest}"


def _channel_fingerprint() -> str:
    """摘要化当前 Provider 渠道；KV 只保存摘要，不保存 API key。"""
    base_url = str(config.get("AGENT_LLM_API_URL", "") or "").strip().rstrip("/")
    protocol = str(config.get("AGENT_LLM_PROTOCOL", "auto") or "auto").strip().casefold()
    api_key = str(config.get("AGENT_LLM_API_KEY", "") or "")
    credential_digest = hashlib.sha256(api_key.encode("utf-8")).digest()
    material = b"\0".join((
        b"mediaflux-telegram-model-channel:v1",
        base_url.encode("utf-8"),
        protocol.encode("utf-8"),
        credential_digest,
    ))
    return hashlib.sha256(material).hexdigest()


def _callback_key(owner: str, token: str) -> str:
    owner_digest = hashlib.sha256(str(owner).encode("utf-8")).hexdigest()
    return f"telegram_model_callback:v1:{owner_digest}:{token}"


def _expiry_is_live(value: object) -> bool:
    try:
        expires_at = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(expires_at) and expires_at > time.time()


def create_model_callback(
    owner: str, session_id: str, thread_id: object, *,
    action: str, model_id: str = "", page: int = 0,
) -> str:
    """Create a short-lived owner/session/thread-bound model-menu callback."""
    from app.modules.telegram_topic_routing import _maybe_prune_expired_routes

    _maybe_prune_expired_routes()
    normalized_owner = str(owner or "").strip()
    normalized_session = str(session_id or "").strip()
    if not normalized_owner or not normalized_session:
        raise ValueError("Telegram model callback scope is invalid")
    normalized_thread = str(thread_id or "").strip()
    if normalized_thread and not _THREAD_RE.fullmatch(normalized_thread):
        raise ValueError("Telegram model callback topic is invalid")
    if action not in {"select", "page"}:
        raise ValueError("Telegram model callback action is invalid")
    normalized_model = normalize_provider_model_id(model_id)
    if action == "select" and not normalized_model:
        raise ValueError("Provider model ID is invalid")
    try:
        page_value = max(0, int(page))
    except (TypeError, ValueError) as exc:
        raise ValueError("Telegram model page is invalid") from exc
    token = secrets.token_urlsafe(6)
    if not _MODEL_CALLBACK_TOKEN_RE.fullmatch(token):
        raise RuntimeError("Telegram model callback token is invalid")
    db.kv_set(
        _callback_key(normalized_owner, token),
        json.dumps(
            {
                "session_id": normalized_session,
                "thread_id": normalized_thread,
                "action": action,
                "model_id": normalized_model,
                "page": page_value,
                "channel": _channel_fingerprint(),
                "expires_at": time.time() + _MODEL_CALLBACK_TTL_SECONDS,
            },
            separators=(",", ":"),
        ),
    )
    return token


def resolve_model_callback(
    owner: str, token: str, thread_id: object
) -> dict[str, object] | None:
    normalized_token = str(token or "")
    if not _MODEL_CALLBACK_TOKEN_RE.fullmatch(normalized_token):
        return None
    try:
        route = json.loads(db.kv_get(_callback_key(owner, normalized_token), "") or "{}")
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(route, dict)
        or not _expiry_is_live(route.get("expires_at"))
        or str(route.get("thread_id") or "") != str(thread_id or "")
        or route.get("channel") != _channel_fingerprint()
        or route.get("action") not in {"select", "page"}
        or not isinstance(route.get("session_id"), str)
    ):
        return None
    return route


def get_telegram_model_preference(owner: str, session_id: str) -> str:
    try:
        stored = json.loads(db.kv_get(_key(owner, session_id), "") or "{}")
    except (TypeError, ValueError):
        return ""
    if not isinstance(stored, dict) or stored.get("channel") != _channel_fingerprint():
        return ""
    return normalize_provider_model_id(stored.get("model_id"))


def set_telegram_model_preference(
    owner: str, session_id: str, model_id: str
) -> None:
    normalized = normalize_provider_model_id(model_id)
    if not normalized:
        raise ValueError("Provider model ID is invalid")
    db.kv_set(
        _key(owner, session_id),
        json.dumps(
            {"model_id": normalized, "channel": _channel_fingerprint()},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
