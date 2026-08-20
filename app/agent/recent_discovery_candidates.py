"""会话绑定的短期影视探索候选安全投影。"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import re
import threading
import time
import unicodedata
from typing import Any, Callable

from app.agent.models import ToolResult

_ALLOWED_PROVIDERS = {"tmdb", "douban", "bangumi"}
_ALLOWED_MEDIA_TYPES = {"movie", "tv"}
_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,180}$")
_MAX_CANDIDATES = 20


class RecentDiscoveryCandidateStore:
    """保存最近一次影视探索结果的安全、短期、owner 绑定候选。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = 600,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

    def capture(self, *, owner: str, result: ToolResult) -> None:
        owner_key = str(owner or "").strip()
        if not owner_key:
            return
        snapshot = _safe_snapshot(result)
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            self._entries.pop(owner_key, None)
            self._entries[owner_key] = (now + self.ttl_seconds, snapshot)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def get(self, *, owner: str) -> dict[str, Any] | None:
        owner_key = str(owner or "").strip()
        if not owner_key:
            return None
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            entry = self._entries.get(owner_key)
            if entry is None:
                return None
            self._entries.move_to_end(owner_key)
            return deepcopy(entry[1])

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()

    def clear_owner(self, *, owner: str) -> bool:
        owner_key = str(owner or "").strip()
        if not owner_key:
            return False
        with self._lock:
            return self._entries.pop(owner_key, None) is not None

    def _prune_locked(self, now: float) -> None:
        expired = [owner for owner, (expires_at, _) in self._entries.items() if expires_at <= now]
        for owner in expired:
            self._entries.pop(owner, None)


def _safe_text(value: Any, limit: int) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    cleaned = "".join(
        " " if unicodedata.category(char).startswith("C") else char
        for char in normalized
    )
    return " ".join(cleaned.split())[:limit]


def _safe_identifier(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()[:180]
    return normalized if _PUBLIC_ID_RE.fullmatch(normalized) else ""


def _safe_snapshot(result: ToolResult) -> dict[str, Any]:
    data = result.data if isinstance(result.data, dict) else {}
    raw_items = data.get("items")
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    if isinstance(raw_items, list):
        for raw in raw_items[:_MAX_CANDIDATES]:
            if not isinstance(raw, dict):
                continue
            provider = _safe_text(raw.get("provider"), 20).casefold()
            external_id = _safe_identifier(raw.get("external_id"))
            media_type = _safe_text(raw.get("media_type"), 12).casefold()
            title = _safe_text(raw.get("title"), 160)
            year = _safe_text(raw.get("year"), 12)
            identity = (provider, external_id, media_type)
            if (
                provider not in _ALLOWED_PROVIDERS
                or media_type not in _ALLOWED_MEDIA_TYPES
                or not external_id
                or not title
                or identity in seen
            ):
                continue
            seen.add(identity)
            candidates.append({
                "position": len(candidates) + 1,
                "provider": provider,
                "external_id": external_id,
                "media_type": media_type,
                "title": title,
                "year": year,
            })
    return {
        "search_status": _safe_text(result.status, 40),
        "query": _safe_text(data.get("query"), 120),
        "candidates": candidates,
    }
