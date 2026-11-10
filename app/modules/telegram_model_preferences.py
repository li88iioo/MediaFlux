"""Telegram Agent 按会话持久化模型选择，复用 settings_kv。"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from typing import TYPE_CHECKING, Any

from app import config
from app import database as db
from app.agent.model_catalog import (
    normalize_provider_model_id,
    provider_channel_fingerprint,
)
from app.modules.telegram_topic_routing import expiry_is_live

if TYPE_CHECKING:
    from app.agent.kernel.provider_model import ProviderSettings

_MODEL_CALLBACK_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,16}$")
_MODEL_CALLBACK_TTL_SECONDS = 60 * 60
_THREAD_RE = re.compile(r"^[1-9][0-9]{0,18}$")


def _key(owner: str, session_id: str) -> str:
    owner_value = (owner or "").strip()
    session_value = (session_id or "").strip()
    if not owner_value or not session_value:
        raise ValueError("Telegram model preference scope is invalid")
    digest = hashlib.sha256(
        b"mediaflux-telegram-model:v1\0"
        + owner_value.encode("utf-8")
        + b"\0"
        + session_value.encode("utf-8")
    ).hexdigest()
    return f"telegram_model_preference:v1:{digest}"


def _callback_key(owner: str, token: str) -> str:
    owner_digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()
    return f"telegram_model_callback:v1:{owner_digest}:{token}"


def create_model_callback(
    owner: str, session_id: str, thread_id: object, *,
    action: str, model_id: str = "", page: int | str | float = 0,
) -> str:
    """Create a short-lived owner/session/thread-bound model-menu callback."""
    from app.modules.telegram_topic_routing import _maybe_prune_expired_routes

    _maybe_prune_expired_routes()
    normalized_owner = (owner or "").strip()
    normalized_session = (session_id or "").strip()
    if not normalized_owner or not normalized_session:
        raise ValueError("Telegram model callback scope is invalid")
    normalized_thread = str(thread_id or "").strip()
    if normalized_thread and not _THREAD_RE.fullmatch(normalized_thread):
        raise ValueError("Telegram model callback topic is invalid")
    if action not in {"select", "page", "cancel"}:
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
                "channel": provider_channel_fingerprint(config.get_many((
                    "AGENT_LLM_API_URL", "AGENT_LLM_PROTOCOL", "AGENT_LLM_API_KEY",
                ))),
                "expires_at": time.time() + _MODEL_CALLBACK_TTL_SECONDS,
            },
            separators=(",", ":"),
        ),
    )
    return token


def resolve_model_callback(
    owner: str, token: str, thread_id: object
) -> dict[str, Any] | None:
    normalized_token = (token or "")
    if not _MODEL_CALLBACK_TOKEN_RE.fullmatch(normalized_token):
        return None
    try:
        route = json.loads(db.kv_get(_callback_key(owner, normalized_token), "") or "{}")
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(route, dict)
        or not expiry_is_live(route.get("expires_at"))
        or str(route.get("thread_id") or "") != str(thread_id or "")
        or route.get("channel") != provider_channel_fingerprint(config.get_many((
            "AGENT_LLM_API_URL", "AGENT_LLM_PROTOCOL", "AGENT_LLM_API_KEY",
        )))
        or route.get("action") not in {"select", "page", "cancel"}
        or not isinstance(route.get("session_id"), str)
    ):
        return None
    return route


def get_telegram_model_preference(
    owner: str, session_id: str, *, settings: ProviderSettings | None = None,
) -> str:
    try:
        stored = json.loads(db.kv_get(_key(owner, session_id), "") or "{}")
    except (TypeError, ValueError):
        return ""
    if settings is None:
        channel = provider_channel_fingerprint(config.get_many((
            "AGENT_LLM_API_URL", "AGENT_LLM_PROTOCOL", "AGENT_LLM_API_KEY",
        )))
    else:
        channel = settings.telegram_channel or provider_channel_fingerprint({
            "AGENT_LLM_API_URL": settings.api_url,
            "AGENT_LLM_PROTOCOL": settings.protocol,
            "AGENT_LLM_API_KEY": settings.api_key,
        })
    if not isinstance(stored, dict) or stored.get("channel") != channel:
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
            {"model_id": normalized, "channel": provider_channel_fingerprint(config.get_many((
                "AGENT_LLM_API_URL", "AGENT_LLM_PROTOCOL", "AGENT_LLM_API_KEY",
            )))},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
