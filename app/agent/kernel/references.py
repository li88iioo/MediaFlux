"""owner-scoped、带类型与 TTL 的不透明引用。"""

from __future__ import annotations

import secrets
import time
from copy import deepcopy
from dataclasses import dataclass
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


class InMemoryReferenceStore:
    def __init__(self, *, clock=time.monotonic, max_entries: int = 2048) -> None:
        self._clock = clock
        self._max_entries = max(32, int(max_entries))
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
        owner_key = str(owner or "").strip()
        session_key = str(session_id or "").strip()
        kind_key = str(kind or "").strip().casefold()
        if not owner_key or not session_key or not kind_key:
            raise ReferenceError("reference owner/session/kind is required")
        now = self._clock()
        expires_at = now + max(1, min(int(ttl_seconds), 86_400))
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
        token = str(ref or "").strip()
        if not token.startswith("ref_") or len(token) > 200:
            raise ReferenceError("reference is invalid")
        now = self._clock()
        async with self._lock:
            self._prune(now)
            record = self._records.get(token)
            if record is None:
                raise ReferenceError("reference is missing or expired")
            if not secrets.compare_digest(record.owner, str(owner or "").strip()):
                raise ReferenceError("reference owner mismatch")
            if not secrets.compare_digest(
                record.session_id, str(session_id or "").strip()
            ):
                raise ReferenceError("reference session mismatch")
            kind = str(expected_kind or "").strip().casefold()
            if kind and record.kind != kind:
                raise ReferenceError("reference type mismatch")
            return deepcopy(record.value)

    def _prune(self, now: float) -> None:
        expired = [
            key for key, value in self._records.items() if value.expires_at <= now
        ]
        for key in expired:
            self._records.pop(key, None)
