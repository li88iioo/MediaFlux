"""会话绑定的最近只读操作，用于安全地处理“重试/再查一次”等续句。"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import re
import threading
import time
from typing import Any, Callable

from app.sensitive_data import contains_sensitive_credential

# 仅记录参数不含凭据、路径、分享口令或内部句柄的幂等只读工具。
# 新工具必须经过显式审查后才能加入，避免把“重试”扩展成任意调用重放。
_REPLAYABLE_READ_TOOLS = frozenset({
    "workspace.briefing",
    "workspace.health",
    "workspace.todo",
    "workspace.next_actions",
    "workspace.search",
    "downloads.diagnose_queue",
    "library.search",
    "library.count_series_episodes",
    "library.audit_episodes",
    "library.audit_library_episodes",
    "library.patrol_status",
    "library.check_updates",
    "rss.diagnose",
    "rss.subscription_summaries",
    "rss.get_subscription_summary",
    "rss.recent_activity",
    "media.subscription_summaries",
    "media.get_subscription_summary",
    "config.indexer_sites_summary",
    "indexer.diagnose_readiness",
    "indexer.search_resources",
    "discovery.search",
    "discovery.recommend",
    "discovery.lookup_rating",
    "discovery.watchlist_summaries",
    "discovery.get_watchlist_summary",
    "bangumi.calendar",
    "config.diagnose",
    "config.explain_component",
    "config.feature_summary",
    "config.safe_policy_summary",
    "web.search",
})
READ_PLAN_OPERATION = "agent.read_plan"
_MIN_PLAN_STEPS = 2
_MAX_PLAN_STEPS = 4
_MAX_DEPTH = 4
_MAX_ITEMS = 32
_MAX_STRING = 240
_SENSITIVE_KEY_PARTS = frozenset({
    "api_key", "apikey", "authorization", "cookie", "credential", "credentials",
    "password", "passwd", "pwd", "secret", "session_id", "signature", "token",
    "access_token", "refresh_token", "share_url", "share_code", "extract_code",
})
_PATH_OR_URL_RE = re.compile(
    r"(?i)(?:^[a-z][a-z0-9+.-]{1,20}://|^[a-z]:[\\/]|^/(?:[^/\s]+/)+)"
)


class RecentReadOperationStore:
    """短期保存已通过注册表校验的安全只读调用。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = 900,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, tuple[float, str, dict[str, Any]]] = OrderedDict()

    def capture(self, *, owner: str, tool_name: str, arguments: dict[str, Any]) -> bool:
        owner_key = str(owner or "").strip()
        name = str(tool_name or "").strip()
        if not owner_key or name not in _REPLAYABLE_READ_TOOLS:
            return False
        safe_arguments = _safe_json_object(arguments)
        if safe_arguments is None:
            return False
        return self._store(owner_key=owner_key, name=name, payload=safe_arguments)

    def capture_plan(self, *, owner: str, steps: list[tuple[str, dict[str, Any]]]) -> bool:
        """原子保存已完成的复合只读计划，避免“重试”只重放最后一步。"""
        owner_key = str(owner or "").strip()
        if not owner_key or not _MIN_PLAN_STEPS <= len(steps) <= _MAX_PLAN_STEPS:
            return False

        safe_steps: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for raw_name, raw_arguments in steps:
            name = str(raw_name or "").strip()
            if name in seen_names or name not in _REPLAYABLE_READ_TOOLS:
                return False
            safe_arguments = _safe_json_object(raw_arguments)
            if safe_arguments is None:
                return False
            seen_names.add(name)
            safe_steps.append({"tool_name": name, "arguments": safe_arguments})

        return self._store(
            owner_key=owner_key,
            name=READ_PLAN_OPERATION,
            payload={"steps": safe_steps},
        )

    def _store(self, *, owner_key: str, name: str, payload: dict[str, Any]) -> bool:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            self._entries.pop(owner_key, None)
            self._entries[owner_key] = (
                now + self.ttl_seconds,
                name,
                deepcopy(payload),
            )
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
        return True

    def get(self, *, owner: str) -> tuple[str, dict[str, Any]] | None:
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
            return entry[1], deepcopy(entry[2])

    def clear_owner(self, *, owner: str) -> bool:
        owner_key = str(owner or "").strip()
        if not owner_key:
            return False
        with self._lock:
            return self._entries.pop(owner_key, None) is not None

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()

    def _prune_locked(self, now: float) -> None:
        for owner, (expires_at, _name, _arguments) in tuple(self._entries.items()):
            if expires_at <= now:
                self._entries.pop(owner, None)


def _safe_json_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or len(value) > _MAX_ITEMS:
        return None
    projected = _safe_json_value(value, depth=0)
    return projected if isinstance(projected, dict) else None


def _safe_json_value(value: Any, *, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if contains_sensitive_credential(normalized) or _PATH_OR_URL_RE.search(normalized):
            return None
        return value[:_MAX_STRING]
    if isinstance(value, list):
        if len(value) > _MAX_ITEMS:
            return None
        items: list[Any] = []
        for item in value:
            safe = _safe_json_value(item, depth=depth + 1)
            if safe is None and item is not None:
                return None
            items.append(safe)
        return items
    if isinstance(value, dict):
        if len(value) > _MAX_ITEMS:
            return None
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str) or not raw_key or len(raw_key) > 80:
                return None
            normalized_key = raw_key.strip().casefold().replace("-", "_")
            if (
                normalized_key in _SENSITIVE_KEY_PARTS
                or normalized_key.endswith(("_token", "_secret", "_password", "_api_key"))
            ):
                return None
            safe = _safe_json_value(item, depth=depth + 1)
            if safe is None and item is not None:
                return None
            result[raw_key] = safe
        return result
    return None
