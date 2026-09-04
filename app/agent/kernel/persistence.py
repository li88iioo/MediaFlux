"""Kernel 会话、引用与事件的统一 SQLite 仓储。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app import database as db
from app.modules.web_secret import get_web_secret

from .events import AgentEvent
from .references import OpaqueReference, ReferenceError
from .state import PublicationLease, SessionState, StalePublicationError, StateUpdate

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_kernel_sessions (
    owner_digest TEXT NOT NULL,
    session_digest TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation >= 0),
    state_json TEXT NOT NULL,
    state_hmac TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(owner_digest, session_digest)
);
CREATE TABLE IF NOT EXISTS agent_kernel_session_epochs (
    owner_digest TEXT NOT NULL,
    session_digest TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation >= 0),
    updated_at REAL NOT NULL,
    PRIMARY KEY(owner_digest, session_digest)
);
CREATE TABLE IF NOT EXISTS agent_kernel_refs (
    ref_id TEXT PRIMARY KEY,
    owner_digest TEXT NOT NULL,
    session_digest TEXT NOT NULL,
    kind TEXT NOT NULL,
    value_json TEXT NOT NULL,
    value_hmac TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_kernel_refs_scope
    ON agent_kernel_refs(owner_digest, session_digest, expires_at);
CREATE TABLE IF NOT EXISTS agent_kernel_events (
    event_id TEXT PRIMARY KEY,
    owner_digest TEXT NOT NULL,
    session_digest TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    event_type TEXT NOT NULL,
    event_json TEXT NOT NULL,
    event_hmac TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(owner_digest, session_digest, turn_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_agent_kernel_events_session
    ON agent_kernel_events(owner_digest, session_digest, created_at, sequence);
"""


class SQLiteKernelStore:
    """上层唯一依赖的 SQLite 接口，同时实现 state/ref/event 三种 port。"""

    def __init__(
        self,
        *,
        secret_provider: Callable[[], str] = get_web_secret,
        clock: Callable[[], float] = time.time,
        max_state_bytes: int = 256 * 1024,
        max_ref_bytes: int = 128 * 1024,
        max_event_bytes: int = 128 * 1024,
        max_events_per_session: int = 2_000,
        event_retention_seconds: int = 30 * 24 * 60 * 60,
    ) -> None:
        self._secret_provider = secret_provider
        self._clock = clock
        self.max_state_bytes = max(16 * 1024, int(max_state_bytes))
        self.max_ref_bytes = max(4 * 1024, int(max_ref_bytes))
        self.max_event_bytes = max(4 * 1024, int(max_event_bytes))
        self.max_events_per_session = max(10, int(max_events_per_session))
        self.event_retention_seconds = max(3_600, int(event_retention_seconds))
        self._event_maintenance_lock = threading.Lock()
        self._events_since_global_prune = 0

    async def begin_turn(
        self, *, owner: str, session_id: str, request_id: str
    ) -> tuple[PublicationLease, SessionState]:
        return await asyncio.to_thread(
            self._begin_turn_sync,
            owner,
            session_id,
            request_id,
        )

    async def is_current(self, lease: PublicationLease) -> bool:
        return await asyncio.to_thread(self._is_current_sync, lease)

    async def commit(
        self,
        lease: PublicationLease,
        *,
        conversation: Sequence[Mapping[str, Any]] | None = None,
        updates: Sequence[StateUpdate] = (),
    ) -> SessionState:
        return await asyncio.to_thread(
            self._commit_sync,
            lease,
            conversation,
            tuple(updates),
        )

    async def load(self, *, owner: str, session_id: str) -> SessionState:
        return await asyncio.to_thread(self._load_sync, owner, session_id)

    async def put(
        self,
        *,
        owner: str,
        session_id: str,
        kind: str,
        value: Any,
        ttl_seconds: int = 900,
    ) -> OpaqueReference:
        return await asyncio.to_thread(
            self._put_ref_sync,
            owner,
            session_id,
            kind,
            value,
            ttl_seconds,
        )

    async def resolve(
        self,
        ref: str,
        *,
        owner: str,
        session_id: str,
        expected_kind: str = "",
    ) -> Any:
        return await asyncio.to_thread(
            self._resolve_ref_sync,
            ref,
            owner,
            session_id,
            expected_kind,
        )

    async def append(self, event: AgentEvent, *, owner: str) -> None:
        await asyncio.to_thread(self._append_event_sync, event, owner)

    async def list_events(
        self,
        *,
        owner: str,
        session_id: str,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._list_events_sync,
            owner,
            session_id,
            limit,
        )

    async def list_sessions(
        self,
        *,
        owner: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_sessions_sync, owner, limit)

    async def reset_session(self, *, owner: str, session_id: str) -> SessionState:
        return await asyncio.to_thread(self._reset_session_sync, owner, session_id)

    async def delete_session(self, *, owner: str, session_id: str) -> bool:
        return await asyncio.to_thread(self._delete_session_sync, owner, session_id)

    @staticmethod
    def _ensure_schema(conn: Any) -> None:
        conn.executescript(_SCHEMA)

    def _secret(self) -> bytes:
        secret = str(self._secret_provider() or "")
        if not secret:
            raise ValueError("Agent Kernel 持久化密钥不可用")
        return secret.encode("utf-8")

    def _digest(self, value: str, *, domain: bytes) -> str:
        normalized = str(value or "").strip()
        if not normalized or len(normalized) > 512:
            raise ValueError("Agent Kernel scope 无效")
        return hmac.new(
            self._secret(), domain + b"\0" + normalized.encode(), hashlib.sha256
        ).hexdigest()

    def _scope(self, owner: str, session_id: str) -> tuple[str, str]:
        owner_digest = self._digest(owner, domain=b"owner:v1")
        session_digest = self._digest(
            f"{owner_digest}\x1f{session_id}", domain=b"session:v1"
        )
        return owner_digest, session_digest

    def _encode(self, value: Any, *, domain: bytes, maximum: int) -> tuple[str, str]:
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            payload = encoded.encode("utf-8")
        except (
            TypeError,
            ValueError,
            OverflowError,
            RecursionError,
            UnicodeError,
        ) as exc:
            raise ValueError("Agent Kernel 数据无法序列化") from exc
        if len(payload) > maximum:
            raise ValueError("Agent Kernel 数据超过持久化上限")
        signature = hmac.new(
            self._secret(), domain + b"\0" + payload, hashlib.sha256
        ).hexdigest()
        return encoded, signature

    def _decode(
        self,
        encoded: Any,
        signature: Any,
        *,
        domain: bytes,
        expected_type: type,
    ) -> Any:
        text = str(encoded or "")
        payload = text.encode("utf-8")
        expected = hmac.new(
            self._secret(), domain + b"\0" + payload, hashlib.sha256
        ).hexdigest()
        if not secrets.compare_digest(expected, str(signature or "")):
            raise ValueError("Agent Kernel 持久化数据校验失败")
        value = json.loads(text)
        if not isinstance(value, expected_type):
            raise TypeError("Agent Kernel 持久化数据类型无效")
        return value

    def _reference_cipher(self) -> Fernet:
        key = hashlib.sha256(
            b"mediaflux-agent-kernel-reference:v1\0" + self._secret()
        ).digest()
        return Fernet(base64.urlsafe_b64encode(key))

    def _encode_reference(
        self, value: Any, *, domain: bytes
    ) -> tuple[str, str]:
        try:
            plaintext = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (
            TypeError,
            ValueError,
            OverflowError,
            RecursionError,
            UnicodeError,
        ) as exc:
            raise ValueError("Agent Kernel 引用无法序列化") from exc
        if len(plaintext) > self.max_ref_bytes:
            raise ValueError("Agent Kernel 引用超过持久化上限")
        encoded = "enc:v1:" + self._reference_cipher().encrypt(plaintext).decode(
            "ascii"
        )
        payload = encoded.encode("utf-8")
        signature = hmac.new(
            self._secret(), domain + b"\0" + payload, hashlib.sha256
        ).hexdigest()
        return encoded, signature

    def _decode_reference(
        self,
        encoded: Any,
        signature: Any,
        *,
        domain: bytes,
    ) -> Any:
        text = str(encoded or "")
        payload = text.encode("utf-8")
        expected = hmac.new(
            self._secret(), domain + b"\0" + payload, hashlib.sha256
        ).hexdigest()
        if not secrets.compare_digest(expected, str(signature or "")):
            raise ValueError("Agent Kernel 引用校验失败")
        if text.startswith("enc:v1:"):
            try:
                plaintext = self._reference_cipher().decrypt(
                    text.removeprefix("enc:v1:").encode("ascii")
                )
            except (InvalidToken, UnicodeError, ValueError) as exc:
                raise ValueError("Agent Kernel 引用解密失败") from exc
            if len(plaintext) > self.max_ref_bytes:
                raise ValueError("Agent Kernel 引用超过持久化上限")
            return json.loads(plaintext.decode("utf-8"))
        # 兼容切换前已签名但未加密的短期引用；新写入一律使用 enc:v1。
        return json.loads(text)

    @staticmethod
    def _state_payload(state: SessionState) -> dict[str, Any]:
        return {
            "session_id": state.session_id,
            "conversation": deepcopy(state.conversation[-80:]),
            "summary": state.summary,
            "recent_refs": list(state.recent_refs[-100:]),
            "ref_kinds": sorted(state.ref_kinds),
            "pending_effect_plan_id": state.pending_effect_plan_id,
            "metadata": deepcopy(state.metadata),
        }

    @staticmethod
    def _state_from_payload(
        *, owner: str, session_id: str, generation: int, payload: Mapping[str, Any]
    ) -> SessionState:
        conversation = payload.get("conversation")
        metadata = payload.get("metadata")
        return SessionState(
            owner=owner,
            session_id=session_id,
            generation=max(0, int(generation)),
            conversation=[dict(item) for item in conversation if isinstance(item, dict)]
            if isinstance(conversation, list)
            else [],
            summary=str(payload.get("summary") or "")[:8_000],
            recent_refs=[
                str(item) for item in payload.get("recent_refs", ()) if str(item)
            ][-100:],
            ref_kinds={str(item) for item in payload.get("ref_kinds", ()) if str(item)},
            pending_effect_plan_id=str(payload.get("pending_effect_plan_id") or "")[
                :200
            ],
            metadata=deepcopy(metadata) if isinstance(metadata, dict) else {},
        )

    def _empty_state(self, owner: str, session_id: str) -> SessionState:
        return SessionState(owner=owner, session_id=session_id)

    def _should_prune_all_events(self) -> bool:
        with self._event_maintenance_lock:
            self._events_since_global_prune += 1
            if self._events_since_global_prune < 128:
                return False
            self._events_since_global_prune = 0
            return True

    def _load_row(self, conn: Any, owner: str, session_id: str) -> SessionState:
        owner_digest, session_digest = self._scope(owner, session_id)
        row = conn.execute(
            "SELECT generation,state_json,state_hmac FROM agent_kernel_sessions "
            "WHERE owner_digest=? AND session_digest=?",
            (owner_digest, session_digest),
        ).fetchone()
        if row is None:
            return self._empty_state(owner, session_id)
        generation = int(row["generation"])
        payload = self._decode(
            row["state_json"],
            row["state_hmac"],
            domain=f"state:v1:{owner_digest}:{session_digest}:{generation}".encode(),
            expected_type=dict,
        )
        return self._state_from_payload(
            owner=owner,
            session_id=session_id,
            generation=generation,
            payload=payload,
        )

    def _write_state(self, conn: Any, state: SessionState) -> None:
        owner_digest, session_digest = self._scope(state.owner, state.session_id)
        encoded, signature = self._encode(
            self._state_payload(state),
            domain=f"state:v1:{owner_digest}:{session_digest}:{state.generation}".encode(),
            maximum=self.max_state_bytes,
        )
        conn.execute(
            "INSERT INTO agent_kernel_sessions(owner_digest,session_digest,generation,state_json,state_hmac,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(owner_digest,session_digest) DO UPDATE SET "
            "generation=excluded.generation,state_json=excluded.state_json,state_hmac=excluded.state_hmac,updated_at=excluded.updated_at",
            (
                owner_digest,
                session_digest,
                state.generation,
                encoded,
                signature,
                self._clock(),
            ),
        )
        self._write_epoch(
            conn,
            owner_digest=owner_digest,
            session_digest=session_digest,
            generation=state.generation,
        )

    def _write_epoch(
        self,
        conn: Any,
        *,
        owner_digest: str,
        session_digest: str,
        generation: int,
    ) -> None:
        conn.execute(
            "INSERT INTO agent_kernel_session_epochs("
            "owner_digest,session_digest,generation,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(owner_digest,session_digest) DO UPDATE SET "
            "generation=MAX(agent_kernel_session_epochs.generation,excluded.generation),"
            "updated_at=excluded.updated_at",
            (owner_digest, session_digest, max(0, int(generation)), self._clock()),
        )

    @staticmethod
    def _generation_floor(
        conn: Any,
        *,
        owner_digest: str,
        session_digest: str,
    ) -> int:
        row = conn.execute(
            "SELECT generation FROM agent_kernel_session_epochs "
            "WHERE owner_digest=? AND session_digest=?",
            (owner_digest, session_digest),
        ).fetchone()
        return max(0, int(row["generation"])) if row is not None else 0

    def _begin_turn_sync(
        self, owner: str, session_id: str, request_id: str
    ) -> tuple[PublicationLease, SessionState]:
        with db.get_conn() as conn:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            state = self._load_row(conn, owner, session_id)
            owner_digest, session_digest = self._scope(owner, session_id)
            state.generation = max(
                state.generation,
                self._generation_floor(
                    conn,
                    owner_digest=owner_digest,
                    session_digest=session_digest,
                ),
            ) + 1
            self._write_state(conn, state)
        lease = PublicationLease(
            owner=owner,
            session_id=session_id,
            generation=state.generation,
            turn_id=secrets.token_urlsafe(12),
            request_id=request_id,
        )
        return lease, state.clone()

    def _is_current_sync(self, lease: PublicationLease) -> bool:
        owner_digest, session_digest = self._scope(lease.owner, lease.session_id)
        with db.get_conn() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT generation FROM agent_kernel_sessions WHERE owner_digest=? AND session_digest=?",
                (owner_digest, session_digest),
            ).fetchone()
        return bool(row is not None and int(row["generation"]) == lease.generation)

    def _commit_sync(
        self,
        lease: PublicationLease,
        conversation: Sequence[Mapping[str, Any]] | None,
        updates: Sequence[StateUpdate],
    ) -> SessionState:
        with db.get_conn() as conn:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            state = self._load_row(conn, lease.owner, lease.session_id)
            if state.generation != lease.generation:
                raise StalePublicationError("turn no longer owns publication authority")
            if conversation is not None:
                state.conversation = deepcopy([dict(item) for item in conversation])[
                    -80:
                ]
            state.apply(updates)
            self._write_state(conn, state)
        return state.clone()

    def _load_sync(self, owner: str, session_id: str) -> SessionState:
        with db.get_conn() as conn:
            self._ensure_schema(conn)
            return self._load_row(conn, owner, session_id)

    def _list_sessions_sync(self, owner: str, limit: int) -> list[dict[str, Any]]:
        owner_digest = self._digest(owner, domain=b"owner:v1")
        maximum = max(1, min(int(limit), 100))
        result: list[dict[str, Any]] = []
        with db.get_conn() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT session_digest,generation,state_json,state_hmac,updated_at "
                "FROM agent_kernel_sessions WHERE owner_digest=? "
                "ORDER BY updated_at DESC LIMIT ?",
                (owner_digest, maximum),
            ).fetchall()
        for row in rows:
            generation = int(row["generation"])
            session_digest = str(row["session_digest"])
            try:
                payload = self._decode(
                    row["state_json"],
                    row["state_hmac"],
                    domain=f"state:v1:{owner_digest}:{session_digest}:{generation}".encode(),
                    expected_type=dict,
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            session_id = str(payload.get("session_id") or "").strip()
            if not session_id:
                continue
            conversation = payload.get("conversation")
            title = "新对话"
            message_count = 0
            if isinstance(conversation, list):
                message_count = len(conversation)
                for item in conversation:
                    if not isinstance(item, dict) or item.get("role") != "user":
                        continue
                    candidate = str(item.get("content") or "").strip()
                    if candidate:
                        title = candidate[:80]
                        break
            result.append(
                {
                    "session_id": session_id,
                    "generation": generation,
                    "title": title,
                    "message_count": message_count,
                    "pending_approval": bool(payload.get("pending_effect_plan_id")),
                    "updated_at": float(row["updated_at"]),
                }
            )
        return result

    def _reset_session_sync(self, owner: str, session_id: str) -> SessionState:
        owner_digest, session_digest = self._scope(owner, session_id)
        with db.get_conn() as conn:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            current = self._load_row(conn, owner, session_id)
            reset = SessionState(
                owner=owner,
                session_id=session_id,
                generation=max(
                    current.generation,
                    self._generation_floor(
                        conn,
                        owner_digest=owner_digest,
                        session_digest=session_digest,
                    ),
                ) + 1,
            )
            self._write_state(conn, reset)
            conn.execute(
                "DELETE FROM agent_kernel_refs WHERE owner_digest=? AND session_digest=?",
                (owner_digest, session_digest),
            )
            conn.execute(
                "DELETE FROM agent_kernel_events WHERE owner_digest=? AND session_digest=?",
                (owner_digest, session_digest),
            )
        return reset.clone()

    def _delete_session_sync(self, owner: str, session_id: str) -> bool:
        owner_digest, session_digest = self._scope(owner, session_id)
        with db.get_conn() as conn:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT generation FROM agent_kernel_sessions "
                "WHERE owner_digest=? AND session_digest=?",
                (owner_digest, session_digest),
            ).fetchone()
            if current is not None:
                self._write_epoch(
                    conn,
                    owner_digest=owner_digest,
                    session_digest=session_digest,
                    generation=int(current["generation"]),
                )
            cursor = conn.execute(
                "DELETE FROM agent_kernel_sessions WHERE owner_digest=? AND session_digest=?",
                (owner_digest, session_digest),
            )
            conn.execute(
                "DELETE FROM agent_kernel_refs WHERE owner_digest=? AND session_digest=?",
                (owner_digest, session_digest),
            )
            conn.execute(
                "DELETE FROM agent_kernel_events WHERE owner_digest=? AND session_digest=?",
                (owner_digest, session_digest),
            )
        return bool(cursor.rowcount)

    def _put_ref_sync(
        self,
        owner: str,
        session_id: str,
        kind: str,
        value: Any,
        ttl_seconds: int,
    ) -> OpaqueReference:
        owner_digest, session_digest = self._scope(owner, session_id)
        normalized_kind = str(kind or "").strip().casefold()
        if not normalized_kind:
            raise ReferenceError("reference kind is required")
        now = self._clock()
        expires_at = now + max(1, min(int(ttl_seconds), 86_400))
        ref_id = "ref_" + secrets.token_urlsafe(18)
        encoded, signature = self._encode_reference(
            value,
            domain=f"ref:v1:{ref_id}:{owner_digest}:{session_digest}:{normalized_kind}:{expires_at}".encode(),
        )
        with db.get_conn() as conn:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM agent_kernel_refs WHERE expires_at<=?", (now,))
            conn.execute(
                "INSERT INTO agent_kernel_refs(ref_id,owner_digest,session_digest,kind,value_json,value_hmac,expires_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    ref_id,
                    owner_digest,
                    session_digest,
                    normalized_kind,
                    encoded,
                    signature,
                    expires_at,
                    now,
                ),
            )
        return OpaqueReference(ref=ref_id, kind=normalized_kind, expires_at=expires_at)

    def _resolve_ref_sync(
        self,
        ref: str,
        owner: str,
        session_id: str,
        expected_kind: str,
    ) -> Any:
        ref_id = str(ref or "").strip()
        if not ref_id.startswith("ref_") or len(ref_id) > 200:
            raise ReferenceError("reference is invalid")
        owner_digest, session_digest = self._scope(owner, session_id)
        now = self._clock()
        with db.get_conn() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT owner_digest,session_digest,kind,value_json,value_hmac,expires_at "
                "FROM agent_kernel_refs WHERE ref_id=? AND expires_at>?",
                (ref_id, now),
            ).fetchone()
        if row is None:
            raise ReferenceError("reference is missing or expired")
        if not secrets.compare_digest(
            str(row["owner_digest"]), owner_digest
        ) or not secrets.compare_digest(str(row["session_digest"]), session_digest):
            raise ReferenceError("reference scope mismatch")
        kind = str(row["kind"] or "")
        expected = str(expected_kind or "").strip().casefold()
        if expected and expected != kind:
            raise ReferenceError("reference type mismatch")
        expires_at = float(row["expires_at"])
        try:
            return self._decode_reference(
                row["value_json"],
                row["value_hmac"],
                domain=f"ref:v1:{ref_id}:{owner_digest}:{session_digest}:{kind}:{expires_at}".encode(),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ReferenceError("reference payload is invalid") from exc

    def _append_event_sync(self, event: AgentEvent, owner: str) -> None:
        owner_digest, session_digest = self._scope(owner, event.session_id)
        encoded, signature = self._encode(
            event.to_dict(),
            domain=f"event:v1:{event.event_id}:{owner_digest}:{session_digest}".encode(),
            maximum=self.max_event_bytes,
        )
        with db.get_conn() as conn:
            self._ensure_schema(conn)
            now = self._clock()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO agent_kernel_events(event_id,owner_digest,session_digest,turn_id,request_id,sequence,event_type,event_json,event_hmac,occurred_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event.event_id,
                    owner_digest,
                    session_digest,
                    event.turn_id,
                    event.request_id,
                    event.sequence,
                    event.type.value,
                    encoded,
                    signature,
                    event.occurred_at,
                    now,
                ),
            )
            conn.execute(
                "DELETE FROM agent_kernel_events WHERE rowid IN ("
                "SELECT rowid FROM agent_kernel_events "
                "WHERE owner_digest=? AND session_digest=? "
                "ORDER BY created_at DESC,rowid DESC LIMIT -1 OFFSET ?)",
                (
                    owner_digest,
                    session_digest,
                    self.max_events_per_session,
                ),
            )
            conn.execute(
                "DELETE FROM agent_kernel_events WHERE owner_digest=? "
                "AND session_digest=? AND created_at<?",
                (
                    owner_digest,
                    session_digest,
                    now - self.event_retention_seconds,
                ),
            )
            if self._should_prune_all_events():
                conn.execute(
                    "DELETE FROM agent_kernel_events WHERE created_at<?",
                    (now - self.event_retention_seconds,),
                )

    def _list_events_sync(
        self, owner: str, session_id: str, limit: int
    ) -> list[dict[str, Any]]:
        owner_digest, session_digest = self._scope(owner, session_id)
        bounded = max(1, min(int(limit), 500))
        with db.get_conn() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT event_id,event_json,event_hmac FROM agent_kernel_events WHERE owner_digest=? AND session_digest=? "
                "ORDER BY created_at DESC,rowid DESC LIMIT ?",
                (owner_digest, session_digest, bounded),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in reversed(rows):
            try:
                value = self._decode(
                    row["event_json"],
                    row["event_hmac"],
                    domain=f"event:v1:{row['event_id']}:{owner_digest}:{session_digest}".encode(),
                    expected_type=dict,
                )
            except (ValueError, json.JSONDecodeError):
                continue
            result.append(value)
        return result
