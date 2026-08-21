"""会话绑定的短期影视探索候选安全投影。"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import logging
import re
import threading
import time
import unicodedata
from typing import Any, Callable

from app.agent.models import ToolResult
from app.agent.session_context import AgentSessionContextRepository

_ALLOWED_PROVIDERS = {"tmdb", "douban", "bangumi"}
_ALLOWED_MEDIA_TYPES = {"movie", "tv"}
_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,180}$")
_MAX_CANDIDATES = 20
_CONTEXT_TYPE = "discovery_candidates"
logger = logging.getLogger(__name__)


class RecentDiscoveryCandidateStore:
    """保存最近一次影视探索结果的安全、短期、owner 绑定候选。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = 600,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        repository: AgentSessionContextRepository | None = None,
    ) -> None:
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._wall_clock = wall_clock
        self._repository = repository
        self._lock = threading.RLock()
        self._owner_locks = tuple(threading.RLock() for _ in range(64))
        self._entries: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

    def capture(self, *, owner: str, result: ToolResult) -> None:
        owner_key = str(owner or "").strip()
        if not owner_key:
            return
        snapshot = _safe_snapshot(result)
        with self._owner_lock(owner_key):
            now = self._clock()
            with self._lock:
                self._prune_locked(now)
                self._entries.pop(owner_key, None)
                self._entries[owner_key] = (now + self.ttl_seconds, snapshot)
                while len(self._entries) > self.max_entries:
                    self._entries.popitem(last=False)
            if self._repository is not None:
                try:
                    self._repository.replace_latest(
                        owner=owner_key,
                        context_type=_CONTEXT_TYPE,
                        payload=snapshot,
                        expires_at=self._wall_clock() + self.ttl_seconds,
                    )
                except Exception as exc:
                    logger.warning(
                        "Agent 探索候选上下文持久化失败 type=%s",
                        type(exc).__name__,
                    )

    def get(self, *, owner: str) -> dict[str, Any] | None:
        owner_key = str(owner or "").strip()
        if not owner_key:
            return None
        with self._owner_lock(owner_key):
            now = self._clock()
            with self._lock:
                self._prune_locked(now)
                entry = self._entries.get(owner_key)
                if entry is not None:
                    self._entries.move_to_end(owner_key)
                    return deepcopy(entry[1])
            return self._restore(owner_key=owner_key, now=now)

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()

    def clear_owner(self, *, owner: str) -> bool:
        owner_key = str(owner or "").strip()
        if not owner_key:
            return False
        removed = False
        with self._owner_lock(owner_key):
            with self._lock:
                removed = self._entries.pop(owner_key, None) is not None
            if self._repository is not None:
                try:
                    removed = bool(self._repository.delete_latest(
                        owner=owner_key, context_type=_CONTEXT_TYPE
                    )) or removed
                except Exception as exc:
                    logger.warning(
                        "Agent 探索候选上下文清理失败 type=%s",
                        type(exc).__name__,
                    )
        return removed

    def _owner_lock(self, owner_key: str) -> threading.RLock:
        return self._owner_locks[hash(owner_key) % len(self._owner_locks)]

    def _prune_locked(self, now: float) -> None:
        expired = [owner for owner, (expires_at, _) in self._entries.items() if expires_at <= now]
        for owner in expired:
            self._entries.pop(owner, None)

    def _restore(self, *, owner_key: str, now: float) -> dict[str, Any] | None:
        if self._repository is None:
            return None
        wall_now = self._wall_clock()
        try:
            persisted = self._repository.get_latest(
                owner=owner_key,
                context_type=_CONTEXT_TYPE,
                now=wall_now,
            )
        except Exception as exc:
            logger.warning(
                "Agent 探索候选上下文恢复失败 type=%s", type(exc).__name__
            )
            return None
        if persisted is None:
            return None
        snapshot = validate_safe_discovery_snapshot(persisted.payload)
        remaining = persisted.expires_at - wall_now
        if snapshot is None or remaining <= 0:
            return None
        with self._lock:
            self._prune_locked(now)
            self._entries[owner_key] = (
                now + min(float(self.ttl_seconds), remaining),
                snapshot,
            )
            self._entries.move_to_end(owner_key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
        return deepcopy(snapshot)


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


def validate_safe_discovery_snapshot(value: Any) -> dict[str, Any] | None:
    """严格验证持久化探索投影，拒绝额外字段与可疑标识。"""
    if not isinstance(value, dict) or set(value) != {
        "search_status", "query", "candidates"
    }:
        return None
    search_status = _safe_text(value.get("search_status"), 40)
    query = _safe_text(value.get("query"), 120)
    raw_candidates = value.get("candidates")
    if (
        search_status != value.get("search_status")
        or query != value.get("query")
        or not isinstance(raw_candidates, list)
        or len(raw_candidates) > _MAX_CANDIDATES
    ):
        return None
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    expected_keys = {
        "position", "provider", "external_id", "media_type", "title", "year"
    }
    for expected_position, raw in enumerate(raw_candidates, start=1):
        if not isinstance(raw, dict) or set(raw) != expected_keys:
            return None
        provider = _safe_text(raw.get("provider"), 20).casefold()
        external_id = _safe_identifier(raw.get("external_id"))
        media_type = _safe_text(raw.get("media_type"), 12).casefold()
        title = _safe_text(raw.get("title"), 160)
        year = _safe_text(raw.get("year"), 12)
        identity = (provider, external_id, media_type)
        projected = {
            "position": expected_position,
            "provider": provider,
            "external_id": external_id,
            "media_type": media_type,
            "title": title,
            "year": year,
        }
        if (
            raw != projected
            or provider not in _ALLOWED_PROVIDERS
            or media_type not in _ALLOWED_MEDIA_TYPES
            or not external_id
            or not title
            or identity in seen
        ):
            return None
        seen.add(identity)
        candidates.append(projected)
    return {
        "search_status": search_status,
        "query": query,
        "candidates": candidates,
    }
