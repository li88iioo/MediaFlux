"""会话绑定的短期全库巡检安全投影。"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from datetime import date
import logging
import re
import threading
import time
import unicodedata
from typing import Any, Callable

from app.agent.models import ToolResult
from app.agent.session_context import AgentSessionContextRepository


_ALLOWED_FINDING_STATUS = {"updates_available"}
_MAX_OPTIONS = 20
_MAX_EPISODES_PER_OPTION = 20
_SAFE_PATROL_STATUSES = {
    "updates_available", "up_to_date", "inconclusive", "not_configured",
    "unavailable", "failed", "not_run", "running", "pending",
}
logger = logging.getLogger(__name__)


class RecentPatrolStore:
    """线程安全、短期且仅保留必要字段的巡检结果存储。"""

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
        snapshot = build_safe_patrol_snapshot(result)
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
                        context_type="patrol",
                        payload=snapshot,
                        expires_at=self._wall_clock() + self.ttl_seconds,
                    )
                except Exception as exc:
                    logger.warning("Agent 巡检上下文持久化失败 type=%s", type(exc).__name__)

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
                        owner=owner_key, context_type="patrol"
                    )) or removed
                except Exception as exc:
                    logger.warning(
                        "Agent 巡检上下文清理失败 type=%s",
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
                context_type="patrol",
                now=wall_now,
            )
        except Exception as exc:
            logger.warning("Agent 巡检上下文恢复失败 type=%s", type(exc).__name__)
            return None
        if persisted is None:
            return None
        snapshot = validate_safe_patrol_snapshot(persisted.payload)
        remaining = persisted.expires_at - wall_now
        if snapshot is None or remaining <= 0:
            return None
        with self._lock:
            self._prune_locked(now)
            self._entries[owner_key] = (now + min(float(self.ttl_seconds), remaining), snapshot)
            self._entries.move_to_end(owner_key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
        return deepcopy(snapshot)


def build_safe_patrol_snapshot(result: ToolResult) -> dict[str, Any]:
    data = result.data if isinstance(result.data, dict) else {}
    as_of = _safe_date(data.get("as_of"))
    options: list[dict[str, Any]] = []
    findings = data.get("findings")
    if not isinstance(findings, list):
        findings = []

    for finding in findings:
        if len(options) >= _MAX_OPTIONS:
            break
        if not isinstance(finding, dict):
            continue
        if finding.get("status") not in _ALLOWED_FINDING_STATUS:
            continue
        if bool(finding.get("missing_sample_truncated")):
            continue
        title = _safe_title(finding.get("title"))
        tmdb_id = _safe_tmdb_id(finding.get("tmdb_id"))
        if not title or not tmdb_id:
            continue
        grouped: dict[int, set[int]] = {}
        missing_sample = finding.get("missing_sample")
        if not isinstance(missing_sample, list):
            continue
        for item in missing_sample[:_MAX_EPISODES_PER_OPTION]:
            if not isinstance(item, dict):
                continue
            season = item.get("season")
            episode = item.get("episode")
            if (
                isinstance(season, bool)
                or not isinstance(season, int)
                or not 1 <= season <= 100
                or isinstance(episode, bool)
                or not isinstance(episode, int)
                or not 1 <= episode <= 1000
            ):
                continue
            grouped.setdefault(season, set()).add(episode)
        for season in sorted(grouped):
            episodes = sorted(grouped[season])
            if not episodes:
                continue
            options.append({
                "position": len(options) + 1,
                "title": title,
                "tmdb_id": tmdb_id,
                "season": season,
                "missing_count": len(episodes),
                "episode_sample": episodes,
            })
            if len(options) >= _MAX_OPTIONS:
                break

    return {
        "as_of": as_of,
        "patrol_status": _safe_patrol_status(result.status),
        "findings_truncated": bool(data.get("findings_truncated")),
        "options": options,
    }


def validate_safe_patrol_snapshot(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != {
        "as_of", "patrol_status", "findings_truncated", "options"
    }:
        return None
    as_of = _safe_date(value.get("as_of"))
    status = value.get("patrol_status")
    truncated = value.get("findings_truncated")
    raw_options = value.get("options")
    if status not in _SAFE_PATROL_STATUSES or not isinstance(truncated, bool):
        return None
    if not isinstance(raw_options, list) or len(raw_options) > _MAX_OPTIONS:
        return None
    options: list[dict[str, Any]] = []
    for expected_position, item in enumerate(raw_options, start=1):
        if not isinstance(item, dict) or set(item) != {
            "position", "title", "tmdb_id", "season", "missing_count", "episode_sample"
        }:
            return None
        title = _safe_title(item.get("title"))
        tmdb_id = _safe_tmdb_id(item.get("tmdb_id"))
        season = item.get("season")
        missing_count = item.get("missing_count")
        episode_sample = item.get("episode_sample")
        if (
            item.get("position") != expected_position
            or not title
            or not tmdb_id
            or isinstance(season, bool)
            or not isinstance(season, int)
            or not 1 <= season <= 100
            or isinstance(missing_count, bool)
            or not isinstance(missing_count, int)
            or not 1 <= missing_count <= _MAX_EPISODES_PER_OPTION
            or not isinstance(episode_sample, list)
            or len(episode_sample) != missing_count
            or any(
                isinstance(episode, bool)
                or not isinstance(episode, int)
                or not 1 <= episode <= 1000
                for episode in episode_sample
            )
            or episode_sample != sorted(set(episode_sample))
        ):
            return None
        options.append({
            "position": expected_position,
            "title": title,
            "tmdb_id": tmdb_id,
            "season": season,
            "missing_count": missing_count,
            "episode_sample": list(episode_sample),
        })
    return {
        "as_of": as_of,
        "patrol_status": status,
        "findings_truncated": truncated,
        "options": options,
    }


def _safe_patrol_status(value: Any) -> str:
    status = str(value or "").strip()
    return status if status in _SAFE_PATROL_STATUSES else "inconclusive"


def _safe_date(value: Any) -> str:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _safe_title(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    title = unicodedata.normalize("NFKC", value).strip()
    if not title or any(unicodedata.category(char).startswith("C") for char in title):
        return ""
    lowered = title.casefold()
    if any(marker in lowered for marker in (
        "://", "magnet:", "token=", "password=", "passwd=", "cookie=",
        "authorization:", "api_key=", "api-key=", "secret=", "bearer ",
        "/volume/", "/mnt/", "/media/", "/home/", "/srv/", "/data/",
        "\\volume\\", "\\mnt\\", "\\media\\", "\\home\\",
    )) or re.search(r"(?:^|\s)[a-z]:[\\/]", lowered):
        return ""
    return title[:120]


def _safe_tmdb_id(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.isdigit() and 1 <= len(text) <= 10 else ""
