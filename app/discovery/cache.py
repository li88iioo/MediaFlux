"""SQLite fresh/stale 缓存与进程内单飞锁。"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterator

from app import database
from app.discovery.models import redact_provider_message

_TIMESTAMP = "%Y-%m-%d %H:%M:%S"
_CACHE_MAINTENANCE_INTERVAL = timedelta(hours=1)
logger = logging.getLogger(__name__)


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
        self._maintenance_lock = threading.Lock()
        self._next_maintenance_at: datetime | None = None

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

    @staticmethod
    def make_detail_key(provider: str, media_type: str, external_id: str) -> str:
        canonical = json.dumps(
            [
                str(provider or "").strip().lower(),
                str(media_type or "").strip().lower(),
                str(external_id or "").strip(),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"discovery:detail:{digest}"

    def _lookup(self, row: Any) -> CacheLookup:
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
                try:
                    status_code = int(metadata.get("status_code") or 503)
                    retry_after = max(0, int(metadata.get("retry_after") or 0))
                except (TypeError, ValueError):
                    status_code, retry_after = 503, 0
                return CacheLookup(
                    "error", None, row["last_error"] or "",
                    str(metadata.get("code") or "unavailable"),
                    status_code, retry_after,
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

    def get(self, key: str) -> CacheLookup:
        return self._lookup(database.get_discovery_cache(key))

    def get_detail_metadata(
        self, identities: list[tuple[str, str, str]],
    ) -> dict[tuple[str, str, str], dict[str, str]]:
        """按 provider/type/id 批量取可复用的详情日期字段。"""
        normalized = list(dict.fromkeys(
            (str(provider or "").strip().lower(),
             str(media_type or "").strip().lower(),
             str(external_id or "").strip())
            for provider, media_type, external_id in identities
            if str(provider or "").strip()
            and str(media_type or "").strip()
            and str(external_id or "").strip()
        ))
        if not normalized:
            return {}
        keys = {
            identity: self.make_detail_key(*identity) for identity in normalized
        }
        try:
            rows = database.get_discovery_cache_many(list(keys.values()))
        except Exception as exc:
            logger.warning("读取探索详情缓存失败: %s", exc)
            return {}

        result: dict[tuple[str, str, str], dict[str, str]] = {}
        for identity, key in keys.items():
            lookup = self._lookup(rows.get(key))
            payload = lookup.payload
            if lookup.status not in {"fresh", "stale"} or not payload:
                continue
            if payload.get("identity") != list(identity):
                continue
            result[identity] = {
                field: str(payload.get(field) or "")
                for field in ("year", "release_date")
            }
        return result

    def set_detail_metadata(
        self,
        provider: str,
        media_type: str,
        external_id: str,
        *,
        year: str,
        release_date: str,
        ttl_seconds: int,
        stale_seconds: int,
    ) -> None:
        identity = (
            str(provider or "").strip().lower(),
            str(media_type or "").strip().lower(),
            str(external_id or "").strip(),
        )
        if not all(identity) or not (year or release_date):
            return
        self.set_success(
            self.make_detail_key(*identity),
            identity[0],
            {
                "identity": list(identity),
                "year": str(year or ""),
                "release_date": str(release_date or ""),
            },
            ttl_seconds=ttl_seconds,
            stale_seconds=stale_seconds,
        )

    def _maybe_maintain(self, now: datetime) -> None:
        if self._next_maintenance_at is not None and now < self._next_maintenance_at:
            return
        if not self._maintenance_lock.acquire(blocking=False):
            return
        try:
            if self._next_maintenance_at is not None and now < self._next_maintenance_at:
                return
            # 即使清理失败也限频，避免数据库异常期间每次请求都叠加清理压力。
            self._next_maintenance_at = now + _CACHE_MAINTENANCE_INTERVAL
            try:
                database.purge_discovery_cache(now.strftime(_TIMESTAMP))
            except Exception as exc:  # pragma: no cover - 日志兜底，不影响业务写入
                logger.warning("探索缓存清理失败: %s", exc)
        finally:
            self._maintenance_lock.release()

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
        self._maybe_maintain(now)

    def set_error(
        self, key: str, provider: str, error: str, *, ttl_seconds: int = 30,
        code: str = "unavailable", status_code: int = 503, retry_after: int = 0,
        preserve_stale: bool = True,
    ) -> None:
        """默认保留旧数据；调用方确认旧内容不可用时可改用短期错误缓存。"""
        now = self._clock()
        row = database.get_discovery_cache(key)
        if preserve_stale and row and row["payload"] and row["status"] != "error":
            try:
                stale_until = datetime.strptime(row["stale_until"], _TIMESTAMP)
            except (TypeError, ValueError):
                stale_until = now
            if stale_until > now:
                database.update_discovery_cache_error(key, redact_provider_message(error))
                self._maybe_maintain(now)
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
        self._maybe_maintain(now)

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
