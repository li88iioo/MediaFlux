"""owner-scoped、带类型与 TTL 的不透明引用。"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from copy import deepcopy
from dataclasses import dataclass
from time import monotonic
from typing import Any, Protocol

from app.concurrency import CrossLoopAsyncLock


class ReferenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OpaqueReference:
    ref: str
    kind: str
    expires_at: float


@dataclass(slots=True)
class _ReferenceRecord:
    owner: str
    session_id: str
    kind: str
    value: Any
    expires_at: float


class ReferenceStore(Protocol):
    async def put(
        self,
        *,
        owner: str,
        session_id: str,
        kind: str,
        value: Any,
        ttl_seconds: int = 900,
    ) -> OpaqueReference: ...

    async def resolve(
        self,
        ref: str,
        *,
        owner: str,
        session_id: str,
        expected_kind: str = "",
    ) -> Any: ...

    async def deadline(
        self, ref: str, *, owner: str, session_id: str, expected_kind: str = "",
    ) -> float:
        """读取已校验引用的剩余 TTL，换算成当前进程的 monotonic 截止。"""
        ...


# 限制原始 JSON 字符数，不代表解码后的严格堆内存上限。
_RESULT_CACHE_CHARS = 8 * 1024 * 1024


class ResultSnapshotReader:
    """工作区共用的单份结果读取缓存；引用仍由调用方按当前会话校验。"""

    def __init__(self, store: ReferenceStore):
        self.store = store
        self._cache = None
        self._lock = asyncio.Lock()

    async def read(self, handle, manifest, *, owner, session_id):
        if not isinstance(manifest, dict) or not isinstance(manifest.get("parts"), list) or not manifest["parts"]:
            raise ReferenceError("invalid result manifest")
        key = (owner, session_id, handle, tuple(manifest["parts"]))
        async with self._lock:
            cached = self._cache
            if cached is not None and cached[0] == key and monotonic() < cached[1]:
                return cached[2], True
            self._cache = cached = None
            scope = {"owner": owner, "session_id": session_id}
            parts = []
            deadline = float("inf")
            for part in manifest["parts"]:
                parts.append(await self.store.resolve(part, **scope, expected_kind="tool_result_part"))
                deadline = min(deadline, await self.store.deadline(part, **scope, expected_kind="tool_result_part"))
            content = "".join(parts)
            node = json.loads(content)
            cacheable = len(content) <= _RESULT_CACHE_CHARS
            if cacheable:
                self._cache = (key, deadline, node)
            return node, cacheable


class InMemoryReferenceStore:
    def __init__(self, *, clock=time.monotonic, max_entries: int = 2048) -> None:
        self._clock = clock
        self._max_entries = max(32, max_entries)
        self._lock = CrossLoopAsyncLock()
        self._records: dict[str, _ReferenceRecord] = {}

    async def put(
        self,
        *,
        owner: str,
        session_id: str,
        kind: str,
        value: Any,
        ttl_seconds: int = 900,
    ) -> OpaqueReference:
        owner_key = (owner or "").strip()
        session_key = (session_id or "").strip()
        kind_key = (kind or "").strip().casefold()
        if not owner_key or not session_key or not kind_key:
            raise ReferenceError("reference owner/session/kind is required")
        now = self._clock()
        expires_at = now + max(1, min(ttl_seconds, 86_400))
        async with self._lock:
            self._prune(now)
            while len(self._records) >= self._max_entries:
                oldest = min(
                    self._records, key=lambda key: self._records[key].expires_at
                )
                self._records.pop(oldest, None)
            token = "ref_" + secrets.token_urlsafe(18)
            while token in self._records:
                token = "ref_" + secrets.token_urlsafe(18)
            self._records[token] = _ReferenceRecord(
                owner=owner_key,
                session_id=session_key,
                kind=kind_key,
                value=deepcopy(value),
                expires_at=expires_at,
            )
        return OpaqueReference(ref=token, kind=kind_key, expires_at=expires_at)

    async def resolve(
        self,
        ref: str,
        *,
        owner: str,
        session_id: str,
        expected_kind: str = "",
    ) -> Any:
        value, _ = await self._read(ref, owner=owner, session_id=session_id, expected_kind=expected_kind)
        return value

    async def deadline(
        self, ref: str, *, owner: str, session_id: str, expected_kind: str = "",
    ) -> float:
        _, deadline = await self._read(ref, owner=owner, session_id=session_id, expected_kind=expected_kind)
        return deadline

    async def _read(
        self, ref: str, *, owner: str, session_id: str, expected_kind: str,
    ) -> tuple[Any, float]:
        token = (ref or "").strip()
        if not token.startswith("ref_") or len(token) > 200:
            raise ReferenceError("reference is invalid")
        async with self._lock:
            observed_at = time.monotonic()
            now = self._clock()
            self._prune(now)
            record = self._records.get(token)
            if record is None:
                raise ReferenceError("reference is missing or expired")
            if not secrets.compare_digest(record.owner, (owner or "").strip()):
                raise ReferenceError("reference owner mismatch")
            if not secrets.compare_digest(
                record.session_id, (session_id or "").strip()
            ):
                raise ReferenceError("reference session mismatch")
            kind = (expected_kind or "").strip().casefold()
            if kind and record.kind != kind:
                raise ReferenceError("reference type mismatch")
            return deepcopy(record.value), observed_at + (record.expires_at - now)

    def _prune(self, now: float) -> None:
        expired = [
            key for key, value in self._records.items() if value.expires_at <= now
        ]
        for key in expired:
            self._records.pop(key, None)
