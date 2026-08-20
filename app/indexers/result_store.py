from __future__ import annotations

import copy
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .errors import IndexerResultExpired, IndexerResultNotFound
from .models import IndexerItem


@dataclass(slots=True)
class _StoredEntry:
    item: IndexerItem
    expires_at: datetime


class IndexerResultStore:
    """Bounded in-memory store mapping opaque short-lived IDs to provider results."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 600,
        max_entries: int = 10_000,
        clock: Callable[[], datetime] | None = None,
    ):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self.ttl_seconds = int(ttl_seconds)
        self.max_entries = int(max_entries)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._entries: OrderedDict[str, _StoredEntry] = OrderedDict()
        self._expired_ids: set[str] = set()
        self._lock = threading.RLock()

    def put(self, item: IndexerItem) -> str:
        now = self._clock()
        result_id = secrets.token_urlsafe(24)
        with self._lock:
            self._prune_expired(now)
            while len(self._entries) >= self.max_entries:
                evicted_id, _ = self._entries.popitem(last=False)
                self._expired_ids.discard(evicted_id)
            self._entries[result_id] = _StoredEntry(
                item=copy.deepcopy(item),
                expires_at=now + timedelta(seconds=self.ttl_seconds),
            )
        return result_id

    def get(self, result_id: str) -> IndexerItem:
        token = str(result_id or "").strip()
        if not token:
            raise IndexerResultNotFound()
        now = self._clock()
        with self._lock:
            entry = self._entries.get(token)
            if entry is None:
                if token in self._expired_ids:
                    raise IndexerResultExpired()
                raise IndexerResultNotFound()
            if entry.expires_at <= now:
                self._entries.pop(token, None)
                self._expired_ids.add(token)
                raise IndexerResultExpired()
            return copy.deepcopy(entry.item)

    def _prune_expired(self, now: datetime) -> None:
        expired = [result_id for result_id, entry in self._entries.items() if entry.expires_at <= now]
        for result_id in expired:
            self._entries.pop(result_id, None)
            self._expired_ids.add(result_id)
        if len(self._expired_ids) > self.max_entries:
            self._expired_ids = set(list(self._expired_ids)[-self.max_entries :])
