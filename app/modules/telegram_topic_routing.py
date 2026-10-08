"""Telegram topic mode and durable routing hints, stored in existing settings_kv."""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from app import database as db

_THREAD_RE = re.compile(r"^[1-9][0-9]{0,18}$")
_ROUTE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,16}$")
_CALLBACK_ROUTE_TTL = 24 * 60 * 60
_DOWNLOAD_ROUTE_TTL = 30 * 24 * 60 * 60
_ROUTE_SWEEP_INTERVAL = 15 * 60
_ROUTE_SWEEP_BATCH = 200
_ROUTE_SWEEP_LOCK = threading.Lock()
_last_route_sweep = 0.0
_CALLBACK_ROUTE_PREFIX = "telegram_callback_route:v1:"
_MODEL_CALLBACK_PREFIX = "telegram_model_callback:v1:"
_DOWNLOAD_ROUTE_PREFIX = "telegram_download_thread:v1:"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _thread(value: object) -> str:
    normalized = str(value or "").strip()
    return normalized if _THREAD_RE.fullmatch(normalized) else ""


def topic_mode_enabled(owner: str) -> bool:
    return db.kv_get(f"telegram_topic_mode:v1:{_digest(str(owner))}") == "1"


def set_topic_mode(owner: str, enabled: bool) -> None:
    db.kv_set(f"telegram_topic_mode:v1:{_digest(str(owner))}", "1" if enabled else "0")


def telegram_session_scope(owner: str, thread_id: object) -> str:
    """Append the private topic only while this owner's app isolation is on."""
    thread = _thread(thread_id)
    owner_scope = str(owner)
    if thread and topic_mode_enabled(owner_scope):
        return f"{owner_scope}\x1fthread:{thread}"
    return owner_scope


def _expiry_is_live(value: object) -> bool:
    try:
        expires_at = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(expires_at) and expires_at > time.time()


def _maybe_prune_expired_routes(*, force: bool = False) -> int:
    """Periodically delete a bounded batch from route-key ranges only."""
    global _last_route_sweep
    current_tick = time.monotonic()
    if not force and current_tick - _last_route_sweep < _ROUTE_SWEEP_INTERVAL:
        return 0
    if not _ROUTE_SWEEP_LOCK.acquire(blocking=False):
        return 0
    try:
        current_tick = time.monotonic()
        if not force and current_tick - _last_route_sweep < _ROUTE_SWEEP_INTERVAL:
            return 0
        _last_route_sweep = current_tick
        current_time = datetime.now()
        namespaces = (
            (_CALLBACK_ROUTE_PREFIX, _CALLBACK_ROUTE_TTL),
            (_MODEL_CALLBACK_PREFIX, 60 * 60),
            (_DOWNLOAD_ROUTE_PREFIX, _DOWNLOAD_ROUTE_TTL),
        )
        removed = 0
        with db.get_conn() as conn:
            for prefix, ttl in namespaces:
                cutoff = (current_time - timedelta(seconds=ttl)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                rows = conn.execute(
                    "SELECT key FROM settings_kv "
                    "WHERE key>=? AND key<? AND COALESCE(updated_at,'')<=? "
                    "ORDER BY key LIMIT ?",
                    (prefix, prefix + "\uffff", cutoff, _ROUTE_SWEEP_BATCH),
                ).fetchall()
                if rows:
                    conn.executemany(
                        "DELETE FROM settings_kv WHERE key=?",
                        [(row["key"],) for row in rows],
                    )
                    removed += len(rows)
        return removed
    except sqlite3.Error:
        return 0
    finally:
        _ROUTE_SWEEP_LOCK.release()


def create_session_callback_route(
    owner: str, session_id: str, thread_id: object
) -> str:
    _maybe_prune_expired_routes()
    thread = _thread(thread_id)
    token = secrets.token_urlsafe(6)
    if not _ROUTE_TOKEN_RE.fullmatch(token):
        raise RuntimeError("Telegram callback route token is invalid")
    key = f"telegram_callback_route:v1:{_digest(str(owner))}:{token}"
    db.kv_set(
        key,
        json.dumps(
            {
                "session_id": str(session_id),
                "thread_id": thread,
                "expires_at": time.time() + _CALLBACK_ROUTE_TTL,
            },
            separators=(",", ":"),
        ),
    )
    return token


def resolve_session_callback_route(
    owner: str, token: str, thread_id: object
) -> str:
    normalized_token = str(token or "")
    if not _ROUTE_TOKEN_RE.fullmatch(normalized_token):
        return ""
    key = f"telegram_callback_route:v1:{_digest(str(owner))}:{normalized_token}"
    try:
        route = json.loads(db.kv_get(key, "") or "{}")
    except (TypeError, ValueError):
        return ""
    if (
        not isinstance(route, dict)
        or not _expiry_is_live(route.get("expires_at"))
        or route.get("thread_id") != _thread(thread_id)
    ):
        return ""
    session_id = route.get("session_id")
    return session_id if isinstance(session_id, str) else ""


def bind_download_request_thread(
    request_id: object, thread_id: object, *, only_if_unbound: bool = False
) -> None:
    _maybe_prune_expired_routes()
    thread = _thread(thread_id)
    try:
        request = int(request_id)
    except (TypeError, ValueError):
        return
    if request <= 0 or not thread:
        return
    key = f"telegram_download_thread:v1:{request}"
    value = json.dumps(
        {"thread_id": thread, "expires_at": time.time() + _DOWNLOAD_ROUTE_TTL},
        separators=(",", ":"),
    )
    if not only_if_unbound:
        db.kv_set(key, value)
        return
    with db.get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO settings_kv(key,value,updated_at) VALUES(?,?,?)",
            (key, value, db.now()),
        )


def download_request_thread(request_id: object) -> int | None:
    try:
        request = int(request_id)
    except (TypeError, ValueError):
        return None
    if request <= 0:
        return None
    key = f"telegram_download_thread:v1:{request}"
    try:
        route = json.loads(db.kv_get(key, "") or "{}")
        thread = _thread(route.get("thread_id")) if isinstance(route, dict) else ""
        expires_at = float(route.get("expires_at") or 0) if isinstance(route, dict) else 0
    except (TypeError, ValueError):
        thread, expires_at = "", 0
    if not thread or expires_at <= time.time():
        _delete(key)
        return None
    return int(thread)


def _delete(key: str) -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM settings_kv WHERE key=?", (key,))
