"""本地媒体任务的 owner 绑定安全列表、检查、重试、刷新与入库核验。"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app import database as db
from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolContext, ToolResult
from app.agent.public_safety import sanitize_public_text
from app.agent.session_context import (
    AgentContextWriteGuard,
    AgentSessionContextRepository,
)
from app.agent.state_commit import (
    AgentStateCommitBuffer,
    active_agent_state_commit_buffer,
)
from app.clients.base import close_media_server_client
from app.modules.local_media_models import LOCAL_BUSY_TASK_STATUSES, LOCAL_TASK_STATUSES
from app.modules.local_media_scheduler import get_local_media_scheduler
from app.modules.local_media_service import (
    LocalMediaServiceError,
    get_local_media_service,
)
from app.modules.media_server_path_mapping import (
    MediaServerPathMapping,
    configured_media_server_refresh_options,
)
from app.modules.media_server_profiles import list_configured_profiles
from app.modules.web_secret import get_web_secret

_WORKSPACE_OWNER = "admin"
_MAX_TASK_NUMBER = 100
_MAX_INSPECTION_NUMBER = 2_147_483_647
_ALLOWED_SCOPES = frozenset({"all", "attention", "active", "history"})
_RETRYABLE = frozenset({"failed", "requires_manual"})

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _TaskRef:
    task_id: int
    version: int
    status: str
    source_id: int


@dataclass(frozen=True)
class _InspectionRef:
    number: int
    task: _TaskRef
    inspection_id: str
    digest: str


@dataclass
class _BufferedContext:
    """单次 Agent 请求私有的本地媒体续接视图。"""

    tasks: tuple[_TaskRef, ...]
    inspections: tuple[_InspectionRef, ...]
    next_inspection: int
    guard: AgentContextWriteGuard | None = None


class LocalMediaAgentContextStore:
    """保存短 TTL 的任务序号和检查句柄，并以 owner 指纹跨 Worker 续接。"""

    _CONTEXT_TYPE = "local_media_tasks"
    _PAYLOAD_VERSION = 1

    def __init__(
        self,
        *,
        ttl_seconds: int = 1800,
        max_owners: int = 256,
        max_inspections_per_owner: int = 16,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        repository: AgentSessionContextRepository | None = None,
    ) -> None:
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_owners = max(1, int(max_owners))
        self.max_inspections_per_owner = max(1, int(max_inspections_per_owner))
        self._clock = clock
        self._wall_clock = wall_clock
        self._repository = repository
        self._lock = threading.RLock()
        self._tasks: OrderedDict[str, tuple[float, tuple[_TaskRef, ...]]] = (
            OrderedDict()
        )
        self._inspections: OrderedDict[
            str, tuple[float, tuple[_InspectionRef, ...]]
        ] = OrderedDict()
        self._next_inspection: dict[str, int] = {}

    def set_repository(self, repository: AgentSessionContextRepository | None) -> None:
        with self._lock:
            self._repository = repository

    def capture_tasks(self, *, owner: str, tasks: list[Any]) -> tuple[_TaskRef, ...]:
        owner_key = str(owner or "").strip()
        if not owner_key:
            return ()
        refs = tuple(
            _TaskRef(
                task_id=int(task.id),
                version=int(task.version),
                status=str(task.status),
                source_id=int(task.source_id),
            )
            for task in tasks[:_MAX_TASK_NUMBER]
        )
        buffered = self._buffered_context(owner_key)
        if buffered is not None:
            buffer, state = buffered
            if not self._stage_buffered_commit(buffer, owner_key, state):
                return ()
            with self._lock:
                state.tasks = refs
            return refs
        if active_agent_state_commit_buffer() is not None:
            return ()
        with self._lock:
            self._prune_locked()
            updated = self._mutate_persisted_locked(
                owner_key,
                lambda current: (
                    refs,
                    current[1]
                    if current is not None
                    else (self._inspections.get(owner_key, (0.0, ()))[1]),
                    current[2]
                    if current is not None
                    else self._next_inspection.get(owner_key, 0),
                ),
            )
            if updated is not None:
                return updated[0]
            self._restore_locked(owner_key, required="inspections")
            self._tasks.pop(owner_key, None)
            self._tasks[owner_key] = (self._clock() + self.ttl_seconds, refs)
            self._trim_locked(self._tasks)
            self._persist_locked(owner_key)
        return refs

    def task(self, *, owner: str, number: int) -> _TaskRef | None:
        owner_key = str(owner or "").strip()
        buffered = self._buffered_context(owner_key)
        if buffered is not None:
            _, state = buffered
            with self._lock:
                if not 1 <= int(number) <= len(state.tasks):
                    return None
                return state.tasks[int(number) - 1]
        if active_agent_state_commit_buffer() is not None:
            return None
        with self._lock:
            self._prune_locked()
            self._restore_locked(owner_key, required="tasks")
            entry = self._tasks.get(owner_key)
            if entry is None or not 1 <= int(number) <= len(entry[1]):
                return None
            self._tasks.move_to_end(owner_key)
            return entry[1][int(number) - 1]

    def capture_inspection(
        self,
        *,
        owner: str,
        task: _TaskRef,
        inspection_id: str,
        digest: str,
    ) -> int:
        owner_key = str(owner or "").strip()
        if not owner_key or not inspection_id or not digest:
            return 0
        buffered = self._buffered_context(owner_key)
        if buffered is not None:
            buffer, state = buffered
            if not self._stage_buffered_commit(buffer, owner_key, state):
                return 0
            with self._lock:
                used = {item.number for item in state.inspections}
                number = (
                    1
                    if state.next_inspection >= _MAX_INSPECTION_NUMBER
                    else state.next_inspection + 1
                )
                while number in used:
                    number = 1 if number >= _MAX_INSPECTION_NUMBER else number + 1
                state.next_inspection = number
                ref = _InspectionRef(number, task, str(inspection_id), str(digest))
                state.inspections = (ref, *state.inspections)[
                    : self.max_inspections_per_owner
                ]
                return number
        if active_agent_state_commit_buffer() is not None:
            return 0
        with self._lock:
            self._prune_locked()
            assigned: list[int] = []

            def append_inspection(
                current: tuple[tuple[_TaskRef, ...], tuple[_InspectionRef, ...], int]
                | None,
            ) -> tuple[tuple[_TaskRef, ...], tuple[_InspectionRef, ...], int]:
                current_tasks = (
                    current[0]
                    if current is not None
                    else (self._tasks.get(owner_key, (0.0, ()))[1])
                )
                current_inspections = (
                    current[1]
                    if current is not None
                    else (self._inspections.get(owner_key, (0.0, ()))[1])
                )
                next_number = (
                    current[2]
                    if current is not None
                    else (self._next_inspection.get(owner_key, 0))
                )
                used = {item.number for item in current_inspections}
                number = 1 if next_number >= _MAX_INSPECTION_NUMBER else next_number + 1
                while number in used:
                    number = 1 if number >= _MAX_INSPECTION_NUMBER else number + 1
                assigned[:] = [number]
                ref = _InspectionRef(number, task, str(inspection_id), str(digest))
                return (
                    current_tasks,
                    (ref, *current_inspections)[: self.max_inspections_per_owner],
                    number,
                )

            updated = self._mutate_persisted_locked(owner_key, append_inspection)
            if updated is not None and assigned:
                return assigned[0]
            self._restore_locked(owner_key, required="inspections")
            current = self._inspections.pop(owner_key, (0.0, ()))[1]
            used = {item.number for item in current}
            number = self._next_inspection.get(owner_key, 0) + 1
            if number > _MAX_INSPECTION_NUMBER:
                number = 1
            while number in used:
                number = 1 if number >= _MAX_INSPECTION_NUMBER else number + 1
            self._next_inspection[owner_key] = number
            ref = _InspectionRef(number, task, str(inspection_id), str(digest))
            self._inspections[owner_key] = (
                self._clock() + self.ttl_seconds,
                (ref, *current)[: self.max_inspections_per_owner],
            )
            self._trim_locked(self._inspections)
            self._persist_locked(owner_key)
            return number

    def inspection(self, *, owner: str, number: int) -> _InspectionRef | None:
        owner_key = str(owner or "").strip()
        buffered = self._buffered_context(owner_key)
        if buffered is not None:
            _, state = buffered
            with self._lock:
                return next(
                    (item for item in state.inspections if item.number == int(number)),
                    None,
                )
        if active_agent_state_commit_buffer() is not None:
            return None
        with self._lock:
            self._prune_locked()
            self._restore_locked(owner_key, required="inspections")
            entry = self._inspections.get(owner_key)
            if entry is None:
                return None
            selected = next(
                (item for item in entry[1] if item.number == int(number)), None
            )
            if selected is not None:
                self._inspections.move_to_end(owner_key)
            return selected

    def replace_inspection(
        self,
        *,
        owner: str,
        number: int,
        inspection_id: str,
        digest: str,
    ) -> bool:
        owner_key = str(owner or "").strip()
        if not owner_key or not inspection_id or not digest:
            return False
        buffered = self._buffered_context(owner_key)
        if buffered is not None:
            buffer, state = buffered
            if not self._stage_buffered_commit(buffer, owner_key, state):
                return False
            with self._lock:
                refs: list[_InspectionRef] = []
                replaced = False
                for item in state.inspections:
                    if item.number == int(number):
                        refs.append(
                            _InspectionRef(
                                item.number, item.task, str(inspection_id), str(digest)
                            )
                        )
                        replaced = True
                    else:
                        refs.append(item)
                if replaced:
                    state.inspections = tuple(refs)
                return replaced
        if active_agent_state_commit_buffer() is not None:
            return False
        with self._lock:
            self._prune_locked()
            replaced = [False]

            def replace(
                current: tuple[tuple[_TaskRef, ...], tuple[_InspectionRef, ...], int]
                | None,
            ) -> tuple[tuple[_TaskRef, ...], tuple[_InspectionRef, ...], int]:
                if current is None:
                    tasks = self._tasks.get(owner_key, (0.0, ()))[1]
                    inspections = self._inspections.get(owner_key, (0.0, ()))[1]
                    next_number = self._next_inspection.get(owner_key, 0)
                else:
                    tasks, inspections, next_number = current
                refs: list[_InspectionRef] = []
                for item in inspections:
                    if item.number == int(number):
                        refs.append(
                            _InspectionRef(
                                item.number, item.task, str(inspection_id), str(digest)
                            )
                        )
                        replaced[0] = True
                    else:
                        refs.append(item)
                return tasks, tuple(refs), next_number

            updated = self._mutate_persisted_locked(owner_key, replace)
            if updated is not None:
                return replaced[0]
            self._restore_locked(owner_key, required="inspections")
            entry = self._inspections.get(owner_key)
            if entry is None:
                return False
            refs = []
            for item in entry[1]:
                if item.number == int(number):
                    refs.append(
                        _InspectionRef(
                            item.number, item.task, str(inspection_id), str(digest)
                        )
                    )
                    replaced[0] = True
                else:
                    refs.append(item)
            if not replaced[0]:
                return False
            self._inspections[owner_key] = (entry[0], tuple(refs))
            self._inspections.move_to_end(owner_key)
            self._persist_locked(owner_key)
            return True

    def clear_owner(self, *, owner: str, invalidate_persisted: bool = True) -> None:
        owner_key = str(owner or "").strip()
        if not owner_key:
            return
        with self._lock:
            self._tasks.pop(owner_key, None)
            self._inspections.pop(owner_key, None)
            self._next_inspection.pop(owner_key, None)
            repository = self._repository
        if repository is not None and invalidate_persisted:
            try:
                invalidate = getattr(repository, "invalidate_context", None)
                if callable(invalidate):
                    invalidate(
                        owner=owner_key,
                        context_type=self._CONTEXT_TYPE,
                    )
                else:
                    repository.delete_latest(
                        owner=owner_key, context_type=self._CONTEXT_TYPE
                    )
            except Exception as exc:
                logger.warning(
                    "本地媒体 Agent 上下文清理失败 type=%s", type(exc).__name__
                )

    def reset(self) -> None:
        """仅清空本进程缓存；持久化上下文用于模拟重启与跨 Worker 续接。"""
        with self._lock:
            self._tasks.clear()
            self._inspections.clear()
            self._next_inspection.clear()

    def _buffered_context(
        self, owner: str
    ) -> tuple[AgentStateCommitBuffer, _BufferedContext] | None:
        buffer = active_agent_state_commit_buffer()
        if buffer is None or not owner:
            return None
        key = self._buffered_state_key(owner)
        state = buffer.get_or_create_request_state(
            owner=owner,
            key=key,
            factory=lambda: self._snapshot_context(owner),
        )
        if not isinstance(state, _BufferedContext):
            return None
        return buffer, state

    def _snapshot_context(self, owner: str) -> _BufferedContext:
        with self._lock:
            self._prune_locked()
            repository = self._repository
            begin_update = (
                getattr(repository, "begin_context_update", None)
                if repository is not None
                else None
            )
            if callable(begin_update):
                persisted, guard = begin_update(
                    owner=owner, context_type=self._CONTEXT_TYPE
                )
                decoded = (
                    self._decode_payload(persisted.payload)
                    if persisted is not None
                    else None
                )
                if decoded is None:
                    self._tasks.pop(owner, None)
                    self._inspections.pop(owner, None)
                    self._next_inspection.pop(owner, None)
                    return _BufferedContext((), (), 0, guard)
                self._install_decoded_locked(
                    owner, decoded, persisted_expires_at=persisted.expires_at
                )
                return _BufferedContext(*decoded, guard)
            self._restore_locked(owner, required="tasks")
            self._restore_locked(owner, required="inspections")
            return _BufferedContext(
                tasks=self._tasks.get(owner, (0.0, ()))[1],
                inspections=self._inspections.get(owner, (0.0, ()))[1],
                next_inspection=self._next_inspection.get(owner, 0),
            )

    def _stage_buffered_commit(
        self,
        buffer: AgentStateCommitBuffer,
        owner: str,
        state: _BufferedContext,
    ) -> bool:
        return buffer.add_once(
            key=f"{self._buffered_state_key(owner)}:commit",
            action=lambda: self._commit_buffered_context(owner, state),
        )

    def _commit_buffered_context(self, owner: str, state: _BufferedContext) -> bool:
        with self._lock:
            decoded = (state.tasks, state.inspections, state.next_inspection)
            expires_at = self._wall_clock() + self.ttl_seconds
            repository = self._repository
            if repository is not None:
                guarded = getattr(repository, "replace_latest_guarded", None)
                if state.guard is not None and callable(guarded):
                    persisted = guarded(
                        owner=owner,
                        context_type=self._CONTEXT_TYPE,
                        payload=self._encode_payload(*decoded),
                        expires_at=expires_at,
                        guard=state.guard,
                    )
                    if persisted is None:
                        self._tasks.pop(owner, None)
                        self._inspections.pop(owner, None)
                        self._next_inspection.pop(owner, None)
                        return False
                    expires_at = persisted.expires_at
                else:
                    repository.replace_latest(
                        owner=owner,
                        context_type=self._CONTEXT_TYPE,
                        payload=self._encode_payload(*decoded),
                        expires_at=expires_at,
                    )
            self._install_decoded_locked(
                owner, decoded, persisted_expires_at=expires_at
            )
            return True

    def _buffered_state_key(self, owner: str) -> str:
        return f"local-media:{id(self)}:{owner}"

    def _restore_locked(self, owner: str, *, required: str) -> None:
        if not owner:
            return
        if required == "tasks" and owner in self._tasks:
            return
        if required == "inspections" and owner in self._inspections:
            return
        repository = self._repository
        if repository is None:
            return
        try:
            persisted = repository.get_latest(
                owner=owner,
                context_type=self._CONTEXT_TYPE,
                now=self._wall_clock(),
            )
        except Exception as exc:
            logger.warning("本地媒体 Agent 上下文恢复失败 type=%s", type(exc).__name__)
            return
        if persisted is None:
            return
        decoded = self._decode_payload(persisted.payload)
        if decoded is None:
            try:
                repository.delete_latest(owner=owner, context_type=self._CONTEXT_TYPE)
            except Exception:
                pass
            return
        self._install_decoded_locked(
            owner, decoded, persisted_expires_at=persisted.expires_at
        )

    def _mutate_persisted_locked(
        self,
        owner: str,
        updater: Callable[
            [tuple[tuple[_TaskRef, ...], tuple[_InspectionRef, ...], int] | None],
            tuple[tuple[_TaskRef, ...], tuple[_InspectionRef, ...], int],
        ],
    ) -> tuple[tuple[_TaskRef, ...], tuple[_InspectionRef, ...], int] | None:
        repository = self._repository
        mutate = (
            getattr(repository, "mutate_latest", None)
            if repository is not None
            else None
        )
        if not callable(mutate):
            return None

        def update_payload(
            current_payload: dict[str, Any] | None,
        ) -> dict[str, Any]:
            current = (
                self._decode_payload(current_payload)
                if isinstance(current_payload, dict)
                else None
            )
            return self._encode_payload(*updater(current))

        try:
            persisted = mutate(
                owner=owner,
                context_type=self._CONTEXT_TYPE,
                updater=update_payload,
                expires_at=self._wall_clock() + self.ttl_seconds,
            )
        except Exception as exc:
            logger.warning(
                "本地媒体 Agent 上下文原子更新失败 type=%s", type(exc).__name__
            )
            return None
        decoded = self._decode_payload(persisted.payload)
        if decoded is None:
            return None
        self._install_decoded_locked(
            owner, decoded, persisted_expires_at=persisted.expires_at
        )
        return decoded

    def _install_decoded_locked(
        self,
        owner: str,
        decoded: tuple[tuple[_TaskRef, ...], tuple[_InspectionRef, ...], int],
        *,
        persisted_expires_at: float,
    ) -> None:
        tasks, inspections, next_inspection = decoded
        lifetime = max(
            1.0,
            min(
                float(self.ttl_seconds),
                float(persisted_expires_at) - self._wall_clock(),
            ),
        )
        expiry = self._clock() + lifetime
        self._tasks.pop(owner, None)
        self._inspections.pop(owner, None)
        if tasks:
            self._tasks[owner] = (expiry, tasks)
            self._trim_locked(self._tasks)
        if inspections:
            self._inspections[owner] = (expiry, inspections)
            self._trim_locked(self._inspections)
        if tasks or inspections:
            self._next_inspection[owner] = next_inspection
        else:
            self._next_inspection.pop(owner, None)

    def _encode_payload(
        self,
        tasks: tuple[_TaskRef, ...],
        inspections: tuple[_InspectionRef, ...],
        next_inspection: int,
    ) -> dict[str, Any]:
        return {
            "version": self._PAYLOAD_VERSION,
            "tasks": [self._task_payload(item) for item in tasks],
            "inspections": [
                {
                    "number": item.number,
                    "task": self._task_payload(item.task),
                    "inspection_id": item.inspection_id,
                    "digest": item.digest,
                }
                for item in inspections
            ],
            "next_inspection": next_inspection,
        }

    def _persist_locked(self, owner: str) -> None:
        repository = self._repository
        if repository is None:
            return
        tasks = self._tasks.get(owner, (0.0, ()))[1]
        inspections = self._inspections.get(owner, (0.0, ()))[1]
        if not tasks and not inspections:
            return
        payload = self._encode_payload(
            tasks, inspections, self._next_inspection.get(owner, 0)
        )
        try:
            repository.replace_latest(
                owner=owner,
                context_type=self._CONTEXT_TYPE,
                payload=payload,
                expires_at=self._wall_clock() + self.ttl_seconds,
            )
        except Exception as exc:
            logger.warning(
                "本地媒体 Agent 上下文持久化失败 type=%s", type(exc).__name__
            )

    @staticmethod
    def _task_payload(item: _TaskRef) -> dict[str, Any]:
        return {
            "task_id": item.task_id,
            "version": item.version,
            "status": item.status,
            "source_id": item.source_id,
        }

    def _decode_payload(
        self, payload: dict[str, Any]
    ) -> tuple[tuple[_TaskRef, ...], tuple[_InspectionRef, ...], int] | None:
        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "tasks", "inspections", "next_inspection"}
            or payload.get("version") != self._PAYLOAD_VERSION
        ):
            return None
        raw_tasks = payload.get("tasks")
        raw_inspections = payload.get("inspections")
        if not isinstance(raw_tasks, list) or not isinstance(raw_inspections, list):
            return None
        if (
            len(raw_tasks) > _MAX_TASK_NUMBER
            or len(raw_inspections) > self.max_inspections_per_owner
        ):
            return None
        tasks: list[_TaskRef] = []
        for raw in raw_tasks:
            ref = self._decode_task(raw)
            if ref is None:
                return None
            tasks.append(ref)
        inspections: list[_InspectionRef] = []
        for raw in raw_inspections:
            if not isinstance(raw, dict) or set(raw) != {
                "number",
                "task",
                "inspection_id",
                "digest",
            }:
                return None
            task = self._decode_task(raw.get("task"))
            inspection_id = raw.get("inspection_id")
            digest = raw.get("digest")
            number = raw.get("number")
            if (
                task is None
                or isinstance(number, bool)
                or not isinstance(number, int)
                or not 1 <= number <= _MAX_INSPECTION_NUMBER
                or not isinstance(inspection_id, str)
                or not 1 <= len(inspection_id) <= 256
                or not isinstance(digest, str)
                or not 1 <= len(digest) <= 256
            ):
                return None
            inspections.append(_InspectionRef(number, task, inspection_id, digest))
        next_inspection = payload.get("next_inspection")
        if (
            isinstance(next_inspection, bool)
            or not isinstance(next_inspection, int)
            or not 0 <= next_inspection <= _MAX_INSPECTION_NUMBER
        ):
            return None
        return tuple(tasks), tuple(inspections), next_inspection

    @staticmethod
    def _decode_task(raw: Any) -> _TaskRef | None:
        if not isinstance(raw, dict) or set(raw) != {
            "task_id",
            "version",
            "status",
            "source_id",
        }:
            return None
        values = (raw.get("task_id"), raw.get("version"), raw.get("source_id"))
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in values
        ):
            return None
        status = raw.get("status")
        if not isinstance(status, str) or status not in LOCAL_TASK_STATUSES:
            return None
        return _TaskRef(values[0], values[1], status, values[2])

    def _prune_locked(self) -> None:
        now = self._clock()
        for mapping in (self._tasks, self._inspections):
            for owner, entry in list(mapping.items()):
                if entry[0] <= now:
                    mapping.pop(owner, None)
                    self._cleanup_owner_counter_locked(owner)

    def _trim_locked(self, mapping: OrderedDict[str, Any]) -> None:
        while len(mapping) > self.max_owners:
            owner, _entry = mapping.popitem(last=False)
            self._cleanup_owner_counter_locked(owner)

    def _cleanup_owner_counter_locked(self, owner: str) -> None:
        if owner not in self._tasks and owner not in self._inspections:
            self._next_inspection.pop(owner, None)


_context_store = LocalMediaAgentContextStore()


def configure_local_media_agent_context(
    repository: AgentSessionContextRepository | None,
) -> None:
    _context_store.set_repository(repository)


def clear_local_media_agent_context(
    *, owner: str, invalidate_persisted: bool = True
) -> None:
    _context_store.clear_owner(owner=owner, invalidate_persisted=invalidate_persisted)


def reset_local_media_agent_context_for_tests() -> None:
    _context_store.reset()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _require_owner(context: ToolContext) -> str:
    owner = str(context.owner or "").strip()
    if not owner:
        raise AgentToolError(
            "本地媒体任务操作需要已登录会话", code="precondition_failed"
        )
    return owner


def _strict_number(value: Any, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_TASK_NUMBER
    ):
        raise AgentToolError(f"{label}必须是 1 到 {_MAX_TASK_NUMBER} 的整数")
    return value


def local_media_task_summaries_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or not set(arguments).issubset(
        {"scope", "limit"}
    ):
        raise AgentToolError("本地媒体任务列表参数无效")
    scope = str(arguments.get("scope") or "all").strip().lower()
    if scope not in _ALLOWED_SCOPES and scope not in LOCAL_TASK_STATUSES:
        raise AgentToolError("不支持的本地媒体任务范围")
    limit = arguments.get("limit", 12)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise AgentToolError("limit 必须是 1 到 20 的整数")
    return {"scope": scope, "limit": limit}


def local_media_task_number_arguments(arguments: dict[str, Any]) -> dict[str, int]:
    if not isinstance(arguments, dict) or set(arguments) != {"task_number"}:
        raise AgentToolError("本地媒体任务参数无效")
    return {"task_number": _strict_number(arguments.get("task_number"), "task_number")}


def local_media_inspection_arguments(arguments: dict[str, Any]) -> dict[str, int]:
    if not isinstance(arguments, dict) or set(arguments) != {"inspection_number"}:
        raise AgentToolError("本地媒体检查参数无效")
    value = arguments.get("inspection_number")
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_INSPECTION_NUMBER
    ):
        raise AgentToolError("inspection_number 必须是正整数")
    return {"inspection_number": value}


def _safe_title(value: Any) -> str:
    text = sanitize_public_text(value, limit=120)
    if not text or "/" in text or "\\" in text:
        return "未识别媒体"
    return text


def _status_reason(status: str) -> str:
    return {
        "requires_manual": "manual_match_required",
        "failed": "processing_failed",
        "waiting_stable": "waiting_to_run",
        "recognizing": "recognizing",
        "planned": "plan_ready",
        "moving": "moving_files",
        "verifying": "verifying_files",
        "refreshing": "refreshing_library",
        "rolling_back": "rolling_back",
        "completed": "processing_completed",
    }.get(status, "unknown")


def _task_public(task: Any, number: int) -> dict[str, Any]:
    return {
        "task_number": number,
        "status": str(task.status),
        "reason_code": _status_reason(str(task.status)),
        "title": _safe_title(task.title),
        "media_type": str(task.media_type)
        if str(task.media_type) in {"movie", "tv"}
        else "unknown",
        "season": int(task.season_override)
        if task.season_override is not None
        else None,
        "episode": int(task.episode_override)
        if task.episode_override is not None
        else None,
        "trigger": str(task.trigger)
        if str(task.trigger) in {"qb_completed", "scan", "manual"}
        else "unknown",
        "attempts": max(0, int(task.attempts)),
        "can_inspect": str(task.status) == "requires_manual",
        "can_retry": str(task.status) in _RETRYABLE,
        "can_refresh_library": str(task.status) == "completed",
        "can_verify_library": str(task.status) == "completed",
    }


def _scope_tasks(scope: str, limit: int) -> list[Any]:
    rows = db.list_local_media_tasks(owner=_WORKSPACE_OWNER, limit=100)
    if scope == "attention":
        rows = [item for item in rows if item.status in _RETRYABLE]
    elif scope == "active":
        rows = [item for item in rows if item.status in LOCAL_BUSY_TASK_STATUSES]
    elif scope == "history":
        rows = [item for item in rows if item.status in {"completed", "failed"}]
    elif scope != "all":
        rows = [item for item in rows if item.status == scope]
    return rows[:limit]


def list_local_media_task_summaries(
    arguments: dict[str, Any], context: ToolContext
) -> ToolResult:
    owner = _require_owner(context)
    tasks = _scope_tasks(str(arguments["scope"]), int(arguments["limit"]))
    _context_store.capture_tasks(owner=owner, tasks=tasks)
    items = [_task_public(task, index) for index, task in enumerate(tasks, start=1)]
    attention = sum(item["status"] in _RETRYABLE for item in items)
    return ToolResult(
        True,
        "attention" if attention else "completed",
        f"已列出 {len(items)} 个本地媒体任务"
        if items
        else "当前范围内没有本地媒体任务",
        data={
            "scope": arguments["scope"],
            "total": len(items),
            "attention": attention,
            "tasks": items,
            "expires_in_seconds": _context_store.ttl_seconds,
        },
        evidence=[
            Evidence(
                "sqlite:local_media_tasks",
                "仅返回当前管理工作区任务的短期公开序号、媒体标题、阶段和可执行动作；未返回数据库 ID、路径、哈希、错误正文、规则或媒体库内部标识。",
                _now(),
            )
        ],
        suggestions=["可继续说：检查本地媒体任务 1，或重试本地媒体任务 1。"]
        if items
        else [],
    )


def _current_task(owner: str, task_number: int) -> tuple[_TaskRef, Any]:
    ref = _context_store.task(owner=owner, number=task_number)
    if ref is None:
        raise AgentToolError(
            "任务序号不存在或已过期，请先重新列出本地媒体任务",
            code="precondition_failed",
        )
    task = db.get_local_media_task(ref.task_id, owner=_WORKSPACE_OWNER)
    if task is None or (
        int(task.version) != ref.version
        or str(task.status) != ref.status
        or int(task.source_id) != ref.source_id
    ):
        raise AgentToolError(
            "本地媒体任务状态已变化，请重新列出任务", code="precondition_failed"
        )
    return ref, task


def inspect_local_media_task(
    arguments: dict[str, Any], context: ToolContext
) -> ToolResult:
    owner = _require_owner(context)
    task_number = int(arguments["task_number"])
    ref, task = _current_task(owner, task_number)
    if task.status != "requires_manual":
        raise AgentToolError("只有待人工确认的任务可以检查", code="precondition_failed")
    try:
        raw = get_local_media_service().inspect_task(_WORKSPACE_OWNER, task.id)
    except (LocalMediaServiceError, ValueError, OSError):
        raise AgentToolError(
            "暂时无法检查该任务，请确认源文件仍可访问", code="precondition_failed"
        ) from None
    files = raw.get("files") if isinstance(raw.get("files"), list) else []
    roles = {"video": 0, "subtitle": 0, "other": 0}
    names: list[str] = []
    for item in files[:50]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "other")
        roles[role if role in roles else "other"] += 1
        name = sanitize_public_text(item.get("name"), limit=120)
        if name and "/" not in name and "\\" not in name and len(names) < 8:
            names.append(name)
    inspection_number = _context_store.capture_inspection(
        owner=owner,
        task=ref,
        inspection_id=str(raw.get("inspection_id") or ""),
        digest=str(raw.get("digest") or ""),
    )
    if not inspection_number:
        raise AgentToolError("无法创建安全检查句柄，请重试", code="unavailable")
    return ToolResult(
        True,
        "completed",
        f"本地媒体任务 {task_number} 的只读检查已完成",
        data={
            "task_number": task_number,
            "inspection_number": inspection_number,
            "title": _safe_title(raw.get("task_title") or task.title),
            "year": sanitize_public_text(raw.get("task_year"), limit=4),
            "media_type": str(
                raw.get("task_media_type") or task.media_type or "unknown"
            ),
            "file_count": len(files),
            "video_count": roles["video"],
            "subtitle_count": roles["subtitle"],
            "file_name_sample": names,
            "suggested_query": _safe_title(raw.get("suggested_query")),
            "parsed_season": raw.get("parsed_season")
            if isinstance(raw.get("parsed_season"), int)
            else None,
            "parsed_episode": raw.get("parsed_episode")
            if isinstance(raw.get("parsed_episode"), int)
            else None,
            "cloud_write": False,
            "expires_in_seconds": _context_store.ttl_seconds,
        },
        evidence=[
            Evidence(
                "local_media_inspection",
                "在已配置来源边界内只读扫描任务文件并生成 owner 绑定短期检查序号；未返回绝对/相对路径、源摘要、任务错误或内部 inspection ID。",
                _now(),
            )
        ],
        suggestions=[
            f"如需查看整理匹配预览，请说：预览本地媒体检查 {inspection_number}。"
        ],
    )


def _current_inspection(owner: str, number: int) -> tuple[_InspectionRef, Any]:
    ref = _context_store.inspection(owner=owner, number=number)
    if ref is None:
        raise AgentToolError(
            "检查序号不存在或已过期，请重新检查任务", code="precondition_failed"
        )
    task = db.get_local_media_task(ref.task.task_id, owner=_WORKSPACE_OWNER)
    if task is None or (
        int(task.version) != ref.task.version
        or str(task.status) != ref.task.status
        or (
            bool(str(task.snapshot_digest or ""))
            and str(task.snapshot_digest or "") != str(ref.digest or "")
        )
    ):
        raise AgentToolError(
            "任务或源文件状态已变化，请重新检查", code="precondition_failed"
        )
    return ref, task


def preview_local_media_task(
    arguments: dict[str, Any], context: ToolContext
) -> ToolResult:
    owner = _require_owner(context)
    number = int(arguments["inspection_number"])
    ref, task = _current_inspection(owner, number)
    service = get_local_media_service()

    def preview(inspection_id: str) -> dict[str, Any]:
        return service.preview(
            _WORKSPACE_OWNER,
            inspection_id,
            tmdb_id=str(task.tmdb_id or ""),
            media_type=str(task.media_type or ""),
            automatic=False,
            season_override=task.season_override,
            episode_override=task.episode_override,
            numbering_mode=task.numbering_mode,
        )

    try:
        raw = preview(ref.inspection_id)
    except LocalMediaServiceError:
        # inspection 的完整文件快照只保存在执行 Worker 内存中。跨 Worker 或
        # 进程重启后按任务边界重新只读检查，并用持久化摘要证明仍是同一快照。
        try:
            rebuilt = service.inspect_task(_WORKSPACE_OWNER, task.id)
            rebuilt_digest = str(rebuilt.get("digest") or "")
            if not rebuilt_digest or not secrets.compare_digest(
                rebuilt_digest, str(ref.digest or "")
            ):
                raise AgentToolError(
                    "源文件在检查后发生变化，请重新检查任务",
                    code="precondition_failed",
                )
            rebuilt_id = str(rebuilt.get("inspection_id") or "")
            if not rebuilt_id:
                raise AgentToolError(
                    "整理预览已失效，请重新检查任务", code="precondition_failed"
                )
            raw = preview(rebuilt_id)
            _context_store.replace_inspection(
                owner=owner,
                number=number,
                inspection_id=rebuilt_id,
                digest=rebuilt_digest,
            )
        except AgentToolError:
            raise
        except (LocalMediaServiceError, ValueError, OSError):
            raise AgentToolError(
                "整理预览已失效或条件不足，请重新检查任务",
                code="precondition_failed",
            ) from None
    except (ValueError, OSError):
        raise AgentToolError(
            "整理预览已失效或条件不足，请重新检查任务",
            code="precondition_failed",
        ) from None
    candidates: list[dict[str, Any]] = []
    for item in (
        raw.get("candidates") if isinstance(raw.get("candidates"), list) else []
    )[:5]:
        if not isinstance(item, dict):
            continue
        candidates.append(
            {
                "title": _safe_title(item.get("title")),
                "year": sanitize_public_text(item.get("year"), limit=4),
                "media_type": str(item.get("media_type") or "unknown"),
                "confidence": str(item.get("confidence") or "unknown"),
            }
        )
    plans: list[dict[str, Any]] = []
    for item in (raw.get("plans") if isinstance(raw.get("plans"), list) else [])[:12]:
        if not isinstance(item, dict):
            continue
        plans.append(
            {
                "role": str(item.get("role") or "other"),
                "action": str(item.get("action") or "move"),
                "target_name": sanitize_public_text(item.get("target_name"), limit=140),
            }
        )
    status = str(raw.get("status") or "inconclusive")
    return ToolResult(
        status == "planned",
        "completed" if status == "planned" else "attention",
        "整理匹配预览已生成" if status == "planned" else "整理匹配仍需要人工选择",
        data={
            "inspection_number": number,
            "preview_status": status
            if status in {"planned", "requires_manual"}
            else "inconclusive",
            "candidate_count": len(candidates),
            "candidates": candidates,
            "plan_count": len(plans),
            "plans": plans,
            "cloud_write": False,
        },
        evidence=[
            Evidence(
                "local_media_preview",
                "重新核验文件摘要后只生成整理匹配和文件动作预览；未移动、覆盖、删除文件，也未返回路径、TMDB ID、规则快照或内部检查句柄。",
                _now(),
            )
        ],
        suggestions=["当前仅为只读预览；实际整理仍需使用既有人工确认流程。"],
        error="匹配结果尚不足以自动整理。" if status != "planned" else "",
    )


def _task_snapshot(owner: str, task_number: int) -> tuple[dict[str, Any], Any]:
    _ref, task = _current_task(owner, task_number)
    return {
        "task_id": int(task.id),
        "version": int(task.version),
        "status": str(task.status),
        "source_id": int(task.source_id),
        "snapshot_digest": str(task.snapshot_digest or ""),
        "updated_at": str(task.updated_at or ""),
    }, task


def _fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def prepare_retry_local_media_task(
    arguments: dict[str, Any], context: ToolContext
) -> tuple[ToolResult, str]:
    owner = _require_owner(context)
    task_number = int(arguments["task_number"])
    snapshot, task = _task_snapshot(owner, task_number)
    if task.status not in _RETRYABLE:
        raise AgentToolError(
            "只有失败或待人工确认的任务可以重试", code="precondition_failed"
        )
    return ToolResult(
        True,
        "confirmation_required",
        f"确认后将重试本地媒体任务 {task_number}",
        data={
            "task_number": task_number,
            "current_status": str(task.status),
            "title": _safe_title(task.title),
            "effects": [
                "任务会回到等待执行阶段，并由调度器立即重新检查和处理。",
                "会生成新的操作幂等标识；不会复用上一次中断的文件步骤。",
                "本次确认不会直接移动、覆盖或删除媒体文件。",
            ],
        },
        evidence=[
            Evidence(
                "sqlite:local_media_tasks",
                "已核验任务当前版本和可重试状态；未返回路径、哈希、错误正文或操作标识。",
                _now(),
            )
        ],
    ), _fingerprint(snapshot)


def retry_local_media_task_confirmed(
    arguments: dict[str, Any], expected_context: str, context: ToolContext
) -> ToolResult:
    owner = _require_owner(context)
    task_number = int(arguments["task_number"])
    try:
        snapshot, task = _task_snapshot(owner, task_number)
    except AgentToolError as exc:
        raise AgentToolError(exc.safe_message, code="confirmation_stale") from None
    if not secrets.compare_digest(_fingerprint(snapshot), str(expected_context or "")):
        raise AgentToolError("任务状态已变化，请重新预检", code="confirmation_stale")
    if not db.reset_local_media_task_if_current(
        task.id,
        owner=_WORKSPACE_OWNER,
        expected_version=task.version,
        expected_status=task.status,
    ):
        raise AgentToolError("任务状态已变化，请重新预检", code="confirmation_stale")
    get_local_media_scheduler().reload()
    return ToolResult(
        True,
        "accepted",
        f"本地媒体任务 {task_number} 已重新排队",
        data={
            "operation": "retry",
            "task_number": task_number,
            "affected": 1,
            "runtime_refreshed": True,
        },
        evidence=[
            Evidence(
                "sqlite:local_media_tasks",
                "使用一次性确认票据、任务版本和原子状态条件重新排队，并生成新的内部操作标识。",
                _now(),
            )
        ],
        suggestions=["可稍后重新列出本地媒体任务查看进度。"],
    )


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _client_for_provider(
    provider: str, profile: Any, *, refresh_options: dict[str, object]
) -> Any:
    if provider == "jellyfin":
        from app.clients.jellyfin import JellyfinClient

        return JellyfinClient(profile.url, profile.credential, **refresh_options)
    if provider == "emby":
        from app.clients.emby import EmbyClient

        return EmbyClient(profile.url, profile.credential, **refresh_options)
    raise AgentToolError("绑定的媒体服务器类型不受支持", code="precondition_failed")


def _server_config_digest(
    provider: str, profile: Any, refresh_options: dict[str, object]
) -> str:
    mappings = refresh_options.get("path_mappings") or ()
    canonical = {
        "provider": provider,
        "url": str(profile.url or "").strip().rstrip("/"),
        "credential": str(profile.credential or ""),
        "path_mappings": [
            {
                "local_prefix": str(getattr(item, "local_prefix", "")),
                "server_prefix": str(getattr(item, "server_prefix", "")),
            }
            for item in mappings
        ],
        "allow_global_refresh_fallback": bool(
            refresh_options.get("allow_global_refresh_fallback", False)
        ),
    }
    try:
        secret = get_web_secret().encode("utf-8")
    except Exception:
        raise AgentToolError(
            "媒体服务器配置指纹暂时不可用", code="precondition_failed"
        ) from None
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(
        secret,
        b"mediaflux-agent-media-server-config:v1\0" + payload,
        hashlib.sha256,
    ).hexdigest()


def _bound_task_scope(task: Any) -> dict[str, Any]:
    if task.status != "completed":
        raise AgentToolError(
            "只有已完成整理的任务可以刷新或核验媒体库", code="precondition_failed"
        )
    items = [
        item
        for item in db.list_local_media_task_items(task.id, owner=_WORKSPACE_OWNER)
        if str(item["role"] or "") == "video" and str(item["target_path"] or "").strip()
    ]
    targets = db.list_local_library_targets(task.source_id, owner=_WORKSPACE_OWNER)
    if not items or not targets:
        raise AgentToolError(
            "任务没有可验证的已绑定媒体库目标", code="precondition_failed"
        )
    resolved: list[tuple[Any, Path]] = []
    for item in items:
        target_path = Path(str(item["target_path"])).expanduser().resolve(strict=False)
        matches = [
            target for target in targets if _path_within(target_path, Path(target.path))
        ]
        if not matches:
            raise AgentToolError(
                "任务目标未绑定到明确媒体库", code="precondition_failed"
            )
        longest = max(
            len(str(Path(target.path).resolve(strict=False))) for target in matches
        )
        winners = [
            target
            for target in matches
            if len(str(Path(target.path).resolve(strict=False))) == longest
        ]
        if len(winners) != 1:
            raise AgentToolError(
                "任务目标媒体库绑定存在歧义", code="precondition_failed"
            )
        resolved.append((winners[0], target_path.parent))
    keys = {
        (str(target.provider).lower(), str(target.library_id), str(target.library_name))
        for target, _path in resolved
    }
    if len(keys) != 1:
        raise AgentToolError(
            "一个任务涉及多个媒体库，Agent 不会批量猜测刷新范围",
            code="precondition_failed",
        )
    provider, configured_library_id, configured_library_name = next(iter(keys))
    if provider not in {"jellyfin", "emby"} or not (
        configured_library_id or configured_library_name
    ):
        raise AgentToolError(
            "任务目标没有有效的媒体服务器与媒体库绑定", code="precondition_failed"
        )
    profiles = {
        item.server_type: item
        for item in list_configured_profiles()
        if item.enabled and item.configured
    }
    profile = profiles.get(provider)
    if profile is None:
        raise AgentToolError(
            "绑定的媒体服务器当前未启用或未配置", code="precondition_failed"
        )
    refresh_options = configured_media_server_refresh_options(provider)
    client = _client_for_provider(provider, profile, refresh_options=refresh_options)
    try:
        try:
            folders = client.list_virtual_folders()
        except Exception:
            raise AgentToolError(
                "暂时无法读取绑定媒体库", code="precondition_failed"
            ) from None
        if configured_library_id:
            matches = [
                item
                for item in folders
                if str(item.get("id") or "").strip() == configured_library_id
            ]
            if len(matches) != 1:
                raise AgentToolError(
                    "媒体库绑定已变化，请重新绑定", code="precondition_failed"
                )
            folder = matches[0]
            actual_name = str(folder.get("name") or "").strip()
            if (
                configured_library_name
                and actual_name.casefold() != configured_library_name.casefold()
            ):
                raise AgentToolError(
                    "媒体库名称与绑定不一致，请重新绑定", code="precondition_failed"
                )
        else:
            matches = [
                item
                for item in folders
                if str(item.get("name") or "").strip().casefold()
                == configured_library_name.casefold()
            ]
            if len(matches) != 1:
                raise AgentToolError(
                    "媒体库名称无法唯一解析，请重新绑定", code="precondition_failed"
                )
            folder = matches[0]
        library_id = str(folder.get("id") or "").strip()
        if not library_id:
            raise AgentToolError("媒体库绑定缺少可用标识", code="precondition_failed")
        try:
            paths = sorted(
                {
                    MediaServerPathMapping(target.path, target.server_path).apply(
                        str(path)
                    )
                    if str(getattr(target, "server_path", "") or "").strip()
                    else str(path)
                    for target, path in resolved
                }
            )
        except ValueError:
            raise AgentToolError(
                "媒体库服务端路径映射无效，请重新绑定", code="precondition_failed"
            ) from None
        server_config_digest = _server_config_digest(provider, profile, refresh_options)
        binding = {
            "provider": provider,
            "server_config_digest": server_config_digest,
            "library_id": library_id,
            "library_name": str(
                folder.get("name") or configured_library_name or ""
            ).strip(),
            "paths_digest": hashlib.sha256(
                "\n".join(paths).encode("utf-8")
            ).hexdigest(),
        }
        return {
            "task": task,
            "client": client,
            "paths": paths,
            "binding": binding,
            "server_label": str(profile.label),
        }
    except Exception:
        close_media_server_client(client)
        raise


def _refresh_snapshot(
    owner: str, task_number: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    base, task = _task_snapshot(owner, task_number)
    scope = _bound_task_scope(task)
    try:
        return {**base, "binding": scope["binding"]}, scope
    except Exception:
        close_media_server_client(scope["client"])
        raise


def prepare_refresh_local_media_task_library(
    arguments: dict[str, Any], context: ToolContext
) -> tuple[ToolResult, str]:
    owner = _require_owner(context)
    task_number = int(arguments["task_number"])
    snapshot, scope = _refresh_snapshot(owner, task_number)
    try:
        return ToolResult(
            True,
            "confirmation_required",
            f"确认后将精准刷新本地媒体任务 {task_number} 的绑定媒体库",
            data={
                "task_number": task_number,
                "server": scope["server_label"],
                "library": sanitize_public_text(
                    scope["binding"]["library_name"], limit=80
                ),
                "path_count": len(scope["paths"]),
                "effects": [
                    "只刷新任务已绑定且重新校验通过的一个媒体服务器和媒体库。",
                    "不会接受或使用用户提供的 URL、路径、服务器 ID 或媒体库内部 ID。",
                    "无法唯一定位时会安全停止，绝不退化为全库刷新。",
                ],
            },
            evidence=[
                Evidence(
                    "media_server_binding",
                    "已从任务和服务端配置重新解析唯一绑定媒体库；内部路径和媒体库 ID 仅参与确认指纹。",
                    _now(),
                )
            ],
        ), _fingerprint(snapshot)
    finally:
        close_media_server_client(scope["client"])


def refresh_local_media_task_library_confirmed(
    arguments: dict[str, Any], expected_context: str, context: ToolContext
) -> ToolResult:
    owner = _require_owner(context)
    task_number = int(arguments["task_number"])
    try:
        snapshot, scope = _refresh_snapshot(owner, task_number)
    except AgentToolError as exc:
        raise AgentToolError(exc.safe_message, code="confirmation_stale") from None
    try:
        if not secrets.compare_digest(
            _fingerprint(snapshot), str(expected_context or "")
        ):
            raise AgentToolError(
                "任务或媒体库绑定已变化，请重新预检", code="confirmation_stale"
            )
        lease_key = hashlib.sha256(
            json.dumps(
                {
                    "operation": "local_media.precise_refresh",
                    "task_id": int(scope["task"].id),
                    "task_version": int(scope["task"].version),
                    "server_config_digest": scope["binding"]["server_config_digest"],
                    "library_id": scope["binding"]["library_id"],
                    "paths_digest": scope["binding"]["paths_digest"],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if db.claim_agent_action_lease(lease_key, ttl_seconds=90) is None:
            return ToolResult(
                False,
                "conflict",
                "相同的绑定媒体库刷新请求刚刚已提交",
                data={
                    "operation": "precise_refresh",
                    "task_number": task_number,
                    "refreshed": 0,
                },
                error="为避免重复刷新，请稍后核验媒体库可见状态。",
                suggestions=["可直接核验该任务是否已在绑定媒体库中可见。"],
            )
        try:
            outcome = scope["client"].refresh_for_paths(
                scope["paths"],
                allowed_library_ids=(scope["binding"]["library_id"],),
                allow_library_fallback=False,
            )
        except Exception:
            outcome = None
        if (
            not isinstance(outcome, dict)
            or not outcome.get("ok")
            or outcome.get("skipped")
            or outcome.get("scope") != "item"
        ):
            return ToolResult(
                False,
                "unavailable",
                "绑定媒体库未完成精准刷新",
                data={
                    "operation": "precise_refresh",
                    "task_number": task_number,
                    "refreshed": 0,
                },
                error="媒体服务器未接受精准刷新请求。",
            )
        return ToolResult(
            True,
            "completed",
            "绑定媒体库已完成精准刷新请求",
            data={
                "operation": "precise_refresh",
                "task_number": task_number,
                "refreshed": 1,
                "matched_paths": max(0, int(outcome.get("matched") or 0)),
            },
            evidence=[
                Evidence(
                    "media_server_refresh",
                    "仅向重新校验通过的绑定媒体库提交受限路径刷新；未执行全库刷新。",
                    _now(),
                )
            ],
            suggestions=["刷新请求不等于媒体已可见，可继续核验该任务的入库可见状态。"],
        )
    finally:
        close_media_server_client(scope["client"])


def verify_local_media_task_library_visibility(
    arguments: dict[str, Any], context: ToolContext
) -> ToolResult:
    owner = _require_owner(context)
    task_number = int(arguments["task_number"])
    _snapshot, scope = _refresh_snapshot(owner, task_number)
    try:
        task = scope["task"]
        tmdb_id = str(task.tmdb_id or "").strip()
        media_type = str(task.media_type or "").strip().lower()
        index_status = "inconclusive"
        reason_code = "identity_unavailable"
        if tmdb_id.isascii() and tmdb_id.isdigit() and media_type in {"movie", "tv"}:
            try:
                if media_type == "movie":
                    visible = scope["client"].has_tmdb_media(
                        tmdb_id,
                        "movie",
                        parent_id=scope["binding"]["library_id"],
                    )
                    index_status = "visible" if visible else "missing"
                    reason_code = "movie_indexed" if visible else "movie_not_indexed"
                else:
                    search = scope["client"].find_series_candidates_by_tmdb(
                        tmdb_id,
                        limit=20,
                        parent_id=scope["binding"]["library_id"],
                    )
                    candidates = list(search.candidates)
                    if len(candidates) == 1 and not search.truncated:
                        season = task.season_override
                        episode = task.episode_override
                        if season is not None and episode is not None:
                            inventory = scope["client"].list_series_episode_inventory(
                                candidates[0].id,
                                include_specials=int(season) == 0,
                            )
                            visible = (int(season), int(episode)) in set(
                                inventory.episodes
                            )
                            index_status = "visible" if visible else "missing"
                            reason_code = (
                                "episode_indexed" if visible else "episode_not_indexed"
                            )
                        else:
                            index_status = "inconclusive"
                            reason_code = "series_indexed_episode_unverified"
                    elif not candidates and not search.truncated:
                        index_status = "missing"
                        reason_code = "series_not_indexed"
                    else:
                        reason_code = "series_mapping_ambiguous"
            except Exception:
                index_status = "inconclusive"
                reason_code = "media_server_unavailable"
        summary = {
            "visible": "媒体已在绑定媒体库中可见",
            "missing": "绑定媒体库中尚未看到该媒体",
            "inconclusive": "暂时无法确认媒体库可见状态",
        }[index_status]
        return ToolResult(
            index_status == "visible",
            index_status,
            summary,
            data={
                "task_number": task_number,
                "title": _safe_title(task.title),
                "index_status": index_status,
                "reason_code": reason_code,
                "server": scope["server_label"],
                "library": sanitize_public_text(
                    scope["binding"]["library_name"], limit=80
                ),
                "playback_status": "not_checked",
                "playback_claim": "not_probed",
            },
            evidence=[
                Evidence(
                    "bound_media_library",
                    "按任务保存的媒体身份在唯一绑定媒体库中查询索引；未读取播放历史，也未发起真实播放探测。",
                    _now(),
                )
            ],
            suggestions=[
                "“库中可见”仅表示媒体服务器已经索引；本次没有证明文件可实际解码或播放。"
            ],
            error="媒体库尚未可见或本次核验不确定。"
            if index_status != "visible"
            else "",
        )
    finally:
        close_media_server_client(scope["client"])
