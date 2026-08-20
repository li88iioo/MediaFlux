"""SQLite fresh/stale 缓存与进程内单飞锁。"""
from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterator

from app import database
from app.discovery.models import redact_provider_message

_TIMESTAMP = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class CacheLookup:
    status: str
    payload: dict[str, Any] | None = None
    last_error: str = ""
    error_code: str = ""
    status_code: int = 0
    retry_after: int = 0


class DiscoveryCache:
    def __init__(self, clock: Callable[[], datetime] | None = None):
        self._clock = clock or datetime.now
        self._locks: dict[str, threading.Lock] = {}
        self._lock_users: dict[str, int] = {}
        self._locks_guard = threading.Lock()

    @staticmethod
    def make_key(
        provider: str,
        category: str,
        media_type: str,
        page: int,
        filters: dict[str, Any] | None,
    ) -> str:
        canonical = json.dumps(
            {
                "provider": str(provider).strip().lower(),
                "category": str(category).strip().lower(),
                "media_type": str(media_type).strip().lower(),
                "page": int(page),
                "filters": filters or {},
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"discovery:{digest}"

    def get(self, key: str) -> CacheLookup:
        row = database.get_discovery_cache(key)
        if not row:
            return CacheLookup("miss")
        try:
            expires_at = datetime.strptime(row["expires_at"], _TIMESTAMP)
            stale_until = datetime.strptime(row["stale_until"], _TIMESTAMP)
        except (TypeError, ValueError):
            return CacheLookup("miss")
        now = self._clock()
        if "status" in row.keys() and row["status"] == "error":
            metadata: dict[str, Any] = {}
            try:
                parsed = json.loads(row["payload"] or "{}")
                if isinstance(parsed, dict):
                    metadata = parsed
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            if expires_at > now:
                return CacheLookup(
                    "error", None, row["last_error"] or "",
                    str(metadata.get("code") or "unavailable"),
                    int(metadata.get("status_code") or 503),
                    max(0, int(metadata.get("retry_after") or 0)),
                )
            return CacheLookup("expired", None, row["last_error"] or "")
        try:
            payload = json.loads(row["payload"] or "")
            if not isinstance(payload, dict):
                return CacheLookup("miss")
        except (TypeError, ValueError, json.JSONDecodeError):
            return CacheLookup("miss")
        if expires_at > now:
            return CacheLookup("fresh", payload, row["last_error"] or "")
        if stale_until > now:
            return CacheLookup("stale", payload, row["last_error"] or "")
        return CacheLookup("expired", None, row["last_error"] or "")

    def set_success(
        self,
        key: str,
        provider: str,
        payload: dict[str, Any],
        *,
        ttl_seconds: int,
        stale_seconds: int,
    ) -> None:
        now = self._clock()
        ttl = max(1, int(ttl_seconds))
        stale = max(ttl, int(stale_seconds))
        database.upsert_discovery_cache(
            key,
            provider,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            now.strftime(_TIMESTAMP),
            (now + timedelta(seconds=ttl)).strftime(_TIMESTAMP),
            (now + timedelta(seconds=stale)).strftime(_TIMESTAMP),
            "",
        )

    def set_error(
        self, key: str, provider: str, error: str, *, ttl_seconds: int = 30,
        code: str = "unavailable", status_code: int = 503, retry_after: int = 0,
    ) -> None:
        now = self._clock()
        row = database.get_discovery_cache(key)
        if row and row["payload"] and row["status"] != "error":
            try:
                stale_until = datetime.strptime(row["stale_until"], _TIMESTAMP)
            except (TypeError, ValueError):
                stale_until = now
            if stale_until > now:
                database.update_discovery_cache_error(key, redact_provider_message(error))
                return
        ttl = max(1, int(ttl_seconds))
        database.upsert_discovery_cache(
            key,
            provider,
            json.dumps({
                "code": str(code or "unavailable"),
                "status_code": int(status_code or 503),
                "retry_after": max(0, int(retry_after or 0)),
            }, separators=(",", ":")),
            now.strftime(_TIMESTAMP),
            (now + timedelta(seconds=ttl)).strftime(_TIMESTAMP),
            (now + timedelta(seconds=ttl)).strftime(_TIMESTAMP),
            redact_provider_message(error),
            status="error",
        )

    @contextmanager
    def singleflight(self, key: str) -> Iterator[None]:
        with self._locks_guard:
            lock = self._locks.setdefault(key, threading.Lock())
            self._lock_users[key] = self._lock_users.get(key, 0) + 1
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._locks_guard:
                remaining = self._lock_users.get(key, 1) - 1
                if remaining <= 0 and not lock.locked():
                    self._lock_users.pop(key, None)
                    self._locks.pop(key, None)
                else:
                    self._lock_users[key] = remaining
