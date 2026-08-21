"""会话绑定的最近只读操作，用于安全地处理“重试/再查一次”等续句。"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import logging
import re
import threading
import time
from typing import Any, Callable

from app.agent.session_context import AgentSessionContextRepository
from app.sensitive_data import contains_sensitive_credential

logger = logging.getLogger(__name__)

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
    "media.subscription_updates",
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
_CONTEXT_TYPE = "read_operation"


class RecentReadOperationStore:
    """短期保存已通过注册表校验的安全只读调用。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = 900,
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
        with self._owner_lock(owner_key):
            now = self._clock()
            safe_payload = deepcopy(payload)
            with self._lock:
                self._prune_locked(now)
                self._entries.pop(owner_key, None)
                self._entries[owner_key] = (
                    now + self.ttl_seconds,
                    name,
                    safe_payload,
                )
                while len(self._entries) > self.max_entries:
                    self._entries.popitem(last=False)
            if self._repository is not None:
                snapshot = (
                    {"tool_name": name, "steps": deepcopy(safe_payload["steps"])}
                    if name == READ_PLAN_OPERATION
                    else {"tool_name": name, "arguments": deepcopy(safe_payload)}
                )
                try:
                    self._repository.replace_latest(
                        owner=owner_key,
                        context_type=_CONTEXT_TYPE,
                        payload=snapshot,
                        expires_at=self._wall_clock() + self.ttl_seconds,
                    )
                except Exception as exc:
                    logger.warning(
                        "Agent 最近只读操作持久化失败 type=%s",
                        type(exc).__name__,
                    )
        return True

    def get(self, *, owner: str) -> tuple[str, dict[str, Any]] | None:
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
                    return entry[1], deepcopy(entry[2])
            return self._restore(owner_key=owner_key, now=now)

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
                        "Agent 最近只读操作清理失败 type=%s",
                        type(exc).__name__,
                    )
        return removed

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()

    def _prune_locked(self, now: float) -> None:
        for owner, (expires_at, _name, _arguments) in tuple(self._entries.items()):
            if expires_at <= now:
                self._entries.pop(owner, None)

    def _owner_lock(self, owner_key: str) -> threading.RLock:
        return self._owner_locks[hash(owner_key) % len(self._owner_locks)]

    def _restore(
        self, *, owner_key: str, now: float
    ) -> tuple[str, dict[str, Any]] | None:
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
                "Agent 最近只读操作恢复失败 type=%s", type(exc).__name__
            )
            return None
        if persisted is None:
            return None
        restored = validate_safe_read_operation_snapshot(persisted.payload)
        remaining = persisted.expires_at - wall_now
        if restored is None or remaining <= 0:
            return None
        name, payload = restored
        with self._lock:
            self._prune_locked(now)
            self._entries.pop(owner_key, None)
            self._entries[owner_key] = (
                now + min(float(self.ttl_seconds), remaining),
                name,
                deepcopy(payload),
            )
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
        return name, deepcopy(payload)


def validate_safe_read_operation_snapshot(
    value: Any,
) -> tuple[str, dict[str, Any]] | None:
    """验证持久化的只读重放快照，任何额外字段均 fail-closed。"""
    if not isinstance(value, dict):
        return None
    name = str(value.get("tool_name") or "").strip()
    if name == READ_PLAN_OPERATION:
        if set(value) != {"tool_name", "steps"}:
            return None
        raw_steps = value.get("steps")
        if not isinstance(raw_steps, list) or not _MIN_PLAN_STEPS <= len(raw_steps) <= _MAX_PLAN_STEPS:
            return None
        safe_steps: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for raw_step in raw_steps:
            if not isinstance(raw_step, dict) or set(raw_step) != {"tool_name", "arguments"}:
                return None
            step_name = str(raw_step.get("tool_name") or "").strip()
            safe_arguments = _safe_json_object(raw_step.get("arguments"))
            if (
                step_name not in _REPLAYABLE_READ_TOOLS
                or step_name in seen_names
                or safe_arguments is None
            ):
                return None
            seen_names.add(step_name)
            safe_steps.append({"tool_name": step_name, "arguments": safe_arguments})
        return READ_PLAN_OPERATION, {"steps": safe_steps}
    if set(value) != {"tool_name", "arguments"} or name not in _REPLAYABLE_READ_TOOLS:
        return None
    safe_arguments = _safe_json_object(value.get("arguments"))
    if safe_arguments is None:
        return None
    return name, safe_arguments


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
