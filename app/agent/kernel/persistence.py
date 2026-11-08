"""Kernel 会话、引用与事件的统一 SQLite 仓储。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app import database as db
from app.modules.web_secret import get_web_secret

from .conversation_codec import conversation_rows, state_json_value
from .conversation_state import commit_conversation
from .events import TERMINAL_EVENT_TYPES, AgentEvent, AgentEventType
from .references import OpaqueReference, ReferenceError
from .session_guard import (
    continuation_execution_guard,
    guarded_state_call,
    run_state_worker,
    session_io,
    session_scope_guard,
)
from .session_state import (
    TRANSIENT_EXTRA_BYTES,
    CandidateSelectionGuard,
    PendingEffectGuard,
    SessionState,
    candidate_metadata_only,
    publication_commit_matches,
    publication_matches,
    state_capacity,
)
from .state import (
    _apply_effect_state_change,
    _begin_publication,
    _cancel_publication,
    _consume_delivered_effect_receipts,
    _effect_time_is_due,
    _record_stop_request,
    cancelled_publication_matches,
)
from .state_types import (
    PublicationLease,
    SelectionInvalidError,
    SessionBusyError,
    StalePublicationError,
    StateUpdate,
)
from .task_state import TaskPatch, reduce_task
from .ux_display import session_display_patch, session_summary

_EFFECT_NEXT_POLL_AT_SQL = (
    "CASE WHEN json_valid(state_json) THEN "
    "json_extract(state_json,'$.metadata.effect_next_poll_at') END"
)
_DUE_EFFECT_WAITS_SQL = (
    "SELECT owner_digest,session_digest,generation,state_json,state_hmac "
    "FROM agent_kernel_sessions "
    f"WHERE {_EFFECT_NEXT_POLL_AT_SQL} <= ? "
    f"ORDER BY {_EFFECT_NEXT_POLL_AT_SQL} LIMIT ?"
)



class SQLiteKernelStore:
    """统一 state/ref/event 持久化接口；表结构由 database.init_db 管理。"""

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
        self.max_state_bytes = max(16 * 1024, max_state_bytes)
        self.max_ref_bytes = max(4 * 1024, max_ref_bytes)
        self.max_event_bytes = max(4 * 1024, max_event_bytes)
        self.max_events_per_session = max(10, max_events_per_session)
        self.event_retention_seconds = max(3_600, event_retention_seconds)
        self._event_maintenance_lock = threading.Lock()
        self._events_since_global_prune = 0

    async def begin_turn(
        self, *, owner: str, session_id: str, request_id: str,
        selection_guard: CandidateSelectionGuard | None = None,
        pending_guard: PendingEffectGuard | None = None,
    ) -> tuple[PublicationLease, SessionState]:
        try:
            return await guarded_state_call(
                owner, session_id, self._begin_turn_sync,
                owner,
                session_id,
                request_id,
                selection_guard,
                pending_guard,
            )
        except SessionBusyError as exc:
            if selection_guard is not None:
                raise SelectionInvalidError("候选正在处理，请使用最新选择状态。") from exc
            raise

    async def cancel_publication(self, *, owner: str, session_id: str, request_id: str) -> bool:
        return await guarded_state_call(
            owner, session_id, self._cancel_publication_sync, owner, session_id, request_id,
            kind="commit", complete_on_cancel=True,
        )

    def _cancel_publication_sync(self, owner: str, session_id: str, request_id: str) -> bool:
        with session_scope_guard(owner, session_id, kind="commit"), db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._load_row(conn, owner, session_id)
            if not _cancel_publication(state, request_id):
                return False
            self._write_state(conn, state)
            return True

    async def settle_cancelled_reads(self, receipts) -> bool:
        from .state_types import _CancelledReadInline
        if not isinstance(receipts, tuple) or not receipts or type(receipts[0]) is not _CancelledReadInline:
            return False
        return await run_state_worker(
            self._settle_cancelled_reads_sync, (receipts,), complete_on_cancel=True,
            scope=(receipts[0].lease.owner, receipts[0].lease.session_id),
        )

    def _settle_cancelled_reads_sync(self, receipts) -> bool:
        from .state import _settle_cancelled_reads
        lease = receipts[0].lease
        with db.get_conn() as conn:
            if not cancelled_publication_matches(self._load_row(conn, lease.owner, lease.session_id), lease):
                return False
            conn.execute("BEGIN IMMEDIATE")
            state = self._load_row(conn, lease.owner, lease.session_id)
            updated = _settle_cancelled_reads(state, receipts, self.max_state_bytes)
            if updated is None:
                return False
            with session_scope_guard(lease.owner, lease.session_id, kind="commit"), conn:
                self._write_state(conn, updated)
            return True

    async def is_current(self, lease: PublicationLease) -> bool:
        return await session_io(self._is_current_sync, lease)

    async def commit(
        self,
        lease: PublicationLease,
        *,
        conversation: Sequence[Mapping[str, Any]] | None = None,
        updates: Sequence[StateUpdate] = (),
        task_patch: TaskPatch | None = None,
    ) -> SessionState:
        try:
            return await run_state_worker(
                self._commit_sync,
                (lease, conversation, tuple(updates), *([task_patch] if task_patch is not None else [])),
                scope=(lease.owner, lease.session_id),
            )
        except SessionBusyError as exc:
            if candidate_metadata_only(conversation, updates):
                raise SelectionInvalidError("选择状态正在更新，请稍后使用当前按钮。") from exc
            raise StalePublicationError("session is protected by another operation") from exc

    async def update_effect_state(
        self, *, owner: str, session_id: str,
        change: Callable[[SessionState], Any],
    ) -> Any:
        return await guarded_state_call(
            owner,
            session_id,
            self._update_effect_state_sync,
            owner,
            session_id,
            change,
            kind="commit",
        )

    async def due_effect_waits(self, *, limit: int = 16) -> list[dict[str, Any]]:
        return await session_io(self._due_effect_waits_sync, limit)

    async def due_continuations(self, *, limit: int = 16) -> list[dict[str, Any]]:
        return await session_io(self._due_continuations_sync, limit)

    def _due_continuations_sync(self, limit):
        from .continuation import due_records
        maximum = max(0, min(16, int(limit)))
        if not maximum:
            return []
        self._retire_interrupted_reads_sync()
        due = []
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT owner_digest,session_digest,generation,state_json,state_hmac FROM agent_kernel_sessions "
                "WHERE json_valid(state_json) AND ("
                "((json_extract(state_json,'$.metadata.read_wait.state') IN ('pending','claimed') "
                "OR json_extract(state_json,'$.metadata.read_wait.delivery_pending')=1) "
                "AND json_extract(state_json,'$.metadata.read_wait.due_at')<=?) OR EXISTS ("
                "SELECT 1 FROM json_each(json_extract(state_json,'$.metadata.effect_waits')) AS effect "
                "WHERE ((json_extract(effect.value,'$.continuation.state') IN ('pending','claimed','running') "
                "AND json_extract(effect.value,'$.continuation.due_at')<=? "
                "AND COALESCE(json_extract(effect.value,'$.continuation.claim_until'),0)<=?) "
                "OR (json_extract(effect.value,'$.continuation.delivery_pending')=1 "
                "AND json_extract(effect.value,'$.continuation.due_at')<=?))) OR EXISTS ("
                "SELECT 1 FROM json_each(json_extract(state_json,'$.metadata.continuation_deliveries')) AS delivery "
                "WHERE json_extract(delivery.value,'$.due_at')<=?)) "
                "ORDER BY updated_at LIMIT ?",
                (self._clock(), self._clock(), self._clock(), self._clock(), self._clock(), maximum),
            ).fetchall()
            for row in rows:
                try:
                    payload = self._decode(row["state_json"], row["state_hmac"],
                        domain=f"state:v1:{row['owner_digest']}:{row['session_digest']}:{row['generation']}".encode(), expected_type=dict)
                    for record in due_records(payload.get("metadata") or {}, self._clock()):
                        if self._scope(record["owner"], record["session_id"]) == (row["owner_digest"], row["session_digest"]):
                            due.append(record)
                except (ValueError, KeyError, TypeError):
                    continue
        return sorted(due, key=lambda r: r["due_at"])[:maximum]

    def _retire_interrupted_reads_sync(self):
        from .continuation import interrupt_read_wait
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT owner_digest,session_digest,generation,state_json,state_hmac FROM agent_kernel_sessions "
                "WHERE json_valid(state_json) AND json_extract(state_json,'$.metadata.read_wait.state')='running'",
            ).fetchall()
        for row in rows:
            try:
                payload = self._decode(row["state_json"], row["state_hmac"],
                    domain=f"state:v1:{row['owner_digest']}:{row['session_digest']}:{row['generation']}".encode(), expected_type=dict)
                record = payload["metadata"]["read_wait"]
                owner, session_id = record["owner"], record["session_id"]
                if self._scope(owner, session_id) != (row["owner_digest"], row["session_digest"]):
                    continue
            except (ValueError, KeyError, TypeError):
                continue
            with continuation_execution_guard(owner, session_id) as acquired:
                if not acquired:
                    continue
                try:
                    with session_scope_guard(owner, session_id, kind="commit"), db.get_conn() as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        state = self._load_row(conn, owner, session_id)
                        if not interrupt_read_wait(state, self._clock()):
                            continue
                        record = state.metadata["read_wait"]
                        sequence = conn.execute(
                            "SELECT COALESCE(MAX(sequence),0)+1 FROM agent_kernel_events "
                            "WHERE owner_digest=? AND session_digest=? AND turn_id=?",
                            (row["owner_digest"], row["session_digest"], record["resume_turn_id"]),
                        ).fetchone()[0]
                        event = AgentEvent(AgentEventType.TURN_FAILED, session_id, record["resume_turn_id"],
                            record["source_request_id"], sequence, {
                                "code": "continuation_interrupted", "turn_status": "failed",
                                "message": "任务续行已中断，已保存的查询结果保留；不会自动重放。",
                            })
                        self._write_state(conn, state)
                        self._write_event(conn, event, row["owner_digest"], row["session_digest"], state)
                except SessionBusyError:
                    continue

    async def request_stop_generation(self, *, owner: str, session_id: str, generation: int) -> bool:
        # Only an intent flag: no file-guard acquisition and no change of publication authority.
        # The fresh signed state is updated under the same SQLite transaction as the CAS.
        return await session_io(self._request_stop_generation_sync, owner, session_id, generation)

    def _request_stop_generation_sync(self, owner, session_id, generation):
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM agent_kernel_sessions WHERE owner_digest=? AND session_digest=?",
                            self._scope(owner, session_id)).fetchone() is None:
                return False
            state = self._load_row(conn, owner, session_id)
            if not _record_stop_request(state, generation):
                return False
            self._write_state(conn, state)
            return True

    async def claim_continuation(self, candidate, binding):
        return await guarded_state_call(candidate['owner'], candidate['session_id'],
            self._claim_continuation_sync, candidate, binding, kind="commit")

    def _claim_continuation_sync(self, candidate, binding):
        from .continuation import claim
        with session_scope_guard(candidate['owner'], candidate['session_id'], kind="commit"), db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            scope = self._scope(candidate['owner'], candidate['session_id'])
            if conn.execute("SELECT 1 FROM agent_kernel_sessions WHERE owner_digest=? AND session_digest=?", scope).fetchone() is None:
                return None  # A purge between scan and claim must never resurrect an empty session.
            state = self._load_row(conn, candidate['owner'], candidate['session_id'])
            lease = claim(state, candidate, self._clock(), binding, conn)
            self._write_state(conn, state)
            return (lease, state.clone()) if lease else None

    async def load(self, *, owner: str, session_id: str) -> SessionState:
        return await session_io(self._load_sync, owner, session_id)

    async def put(
        self,
        *,
        owner: str,
        session_id: str,
        kind: str,
        value: Any,
        ttl_seconds: int = 900,
    ) -> OpaqueReference:
        return await session_io(
            self._put_ref_sync,
            owner,
            session_id,
            kind,
            value,
            ttl_seconds,
        )


    async def put_for_turn(self, lease: PublicationLease, **kwargs) -> OpaqueReference:
        if kwargs['owner'] != lease.owner or kwargs['session_id'] != lease.session_id:
            raise StalePublicationError("reference scope mismatch")
        return await guarded_state_call(lease.owner, lease.session_id, self._put_ref_sync,
            lease.owner, lease.session_id, kwargs['kind'], kwargs['value'], kwargs.get('ttl_seconds', 900),
            lease, kind="commit")

    async def resolve(
        self,
        ref: str,
        *,
        owner: str,
        session_id: str,
        expected_kind: str = "",
    ) -> Any:
        value, _ = await session_io(
            self._resolve_ref_sync,
            ref,
            owner,
            session_id,
            expected_kind,
        )
        return value

    async def deadline(
        self, ref: str, *, owner: str, session_id: str, expected_kind: str = "",
    ) -> float:
        _, deadline = await session_io(self._resolve_ref_sync, ref, owner, session_id, expected_kind)
        return deadline

    async def append(self, event: AgentEvent, *, owner: str) -> None:
        await session_io(self._append_event_sync, event, owner)

    async def append_for_turn(self, event: AgentEvent, lease: PublicationLease) -> None:
        await session_io(self._append_event_sync, event, lease.owner, lease)

    async def list_events(
        self,
        *,
        owner: str,
        session_id: str,
        limit: int = 200,
        request_id: str = "",
        turn_id: str = "",
    ) -> list[dict[str, Any]]:
        if bool(request_id) != bool(turn_id):
            raise ValueError("event turn scope requires request_id and turn_id")
        return await session_io(
            self._list_events_sync,
            owner,
            session_id,
            limit,
            request_id,
            turn_id,
        )

    async def list_sessions(
        self,
        *,
        owner: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await session_io(self._list_sessions_sync, owner, limit)

    async def patch_session_display(
        self, *, owner: str, session_id: str, patch: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        validated = session_display_patch(dict(patch))
        return await session_io(self._patch_session_display_sync, owner, session_id, validated)

    async def reset_session(self, *, owner: str, session_id: str) -> SessionState:
        return await guarded_state_call(owner, session_id, self._reset_session_sync, owner, session_id)

    async def delete_session(self, *, owner: str, session_id: str) -> bool:
        return await guarded_state_call(owner, session_id, self._delete_session_sync, owner, session_id)

    def _secret(self) -> bytes:
        secret = (self._secret_provider() or "")
        if not secret:
            raise ValueError("Agent Kernel 持久化密钥不可用")
        return secret.encode("utf-8")

    def _digest(self, value: str, *, domain: bytes) -> str:
        normalized = (value or "").strip()
        if not normalized or len(normalized) > 512:
            raise ValueError("Agent Kernel scope 无效")
        return hmac.new(
            self._secret(), domain + b"\0" + normalized.encode(), hashlib.sha256
        ).hexdigest()

    def _scope(self, owner: str, session_id: str) -> tuple[str, str]:
        owner_digest = db.native_agent_owner_digest(owner, secret=self._secret())
        session_digest = self._digest(
            f"{owner_digest}\x1f{session_id}", domain=b"session:v1"
        )
        return owner_digest, session_digest

    @staticmethod
    def _json_payload(value: Any, *, label: str) -> tuple[str, bytes]:
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
        ) as exc:
            raise ValueError(f"Agent Kernel {label}无法序列化") from exc
        return encoded, payload

    def _encode(self, value: Any, *, domain: bytes, maximum: int) -> tuple[str, str]:
        if domain.startswith(b"state:v1:"):
            maximum = state_capacity(value, maximum)
        encoded, payload = self._json_payload(value, label="数据")
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
        if domain.startswith(b"state:v1:") and len(payload) > self.max_state_bytes + TRANSIENT_EXTRA_BYTES:
            raise ValueError("Agent Kernel 数据超过持久化上限")
        expected = hmac.new(
            self._secret(), domain + b"\0" + payload, hashlib.sha256
        ).hexdigest()
        if not secrets.compare_digest(expected, str(signature or "")):
            raise ValueError("Agent Kernel 持久化数据校验失败")
        value = state_json_value(text) if domain.startswith(b"state:v1:") else json.loads(text)
        if not isinstance(value, expected_type):
            raise TypeError("Agent Kernel 持久化数据类型无效")
        if domain.startswith(b"state:v1:"):
            if len(payload) > state_capacity(value, self.max_state_bytes):
                raise ValueError("Agent Kernel 数据超过持久化上限")
            conversation_rows(value.get("conversation", []))
            from .continuation import normalize_delivery_payload
            normalize_delivery_payload(value)
        return value

    def _reference_cipher(self) -> Fernet:
        key = hashlib.sha256(
            b"mediaflux-agent-kernel-reference:v1\0" + self._secret()
        ).digest()
        return Fernet(base64.urlsafe_b64encode(key))

    def _encode_reference(
        self, value: Any, *, domain: bytes
    ) -> tuple[str, str]:
        _, plaintext = self._json_payload(value, label="引用")
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

    _state_payload = staticmethod(SessionState.to_payload)
    _state_from_payload = staticmethod(SessionState.from_payload)

    def _empty_state(self, owner: str, session_id: str) -> SessionState:
        return SessionState(owner=owner, session_id=session_id)

    def _should_prune_all_events(self) -> bool:
        with self._event_maintenance_lock:
            self._events_since_global_prune += 1
            if self._events_since_global_prune < 128:
                return False
            self._events_since_global_prune = 0
            return True

    def load_existing_session(self, *, owner: str, session_id: str) -> SessionState | None:
        """实际投递只接受物理存在且通过签名校验的会话，不创建默认状态。"""
        with db.get_conn() as conn:
            return self._load_existing_row(conn, owner, session_id)

    def _load_row(self, conn: Any, owner: str, session_id: str) -> SessionState:
        state = self._load_existing_row(conn, owner, session_id)
        return state if state is not None else self._empty_state(owner, session_id)

    def _load_existing_row(self, conn: Any, owner: str, session_id: str) -> SessionState | None:
        owner_digest, session_digest = self._scope(owner, session_id)
        row = conn.execute(
            "SELECT generation,state_json,state_hmac FROM agent_kernel_sessions "
            "WHERE owner_digest=? AND session_digest=?",
            (owner_digest, session_digest),
        ).fetchone()
        if row is None:
            return None
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
        payload = self._state_payload(state)
        conversation_rows(payload["conversation"])
        encoded, signature = self._encode(
            payload,
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
            (owner_digest, session_digest, max(0, generation), self._clock()),
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
        self, owner: str, session_id: str, request_id: str,
        selection_guard: CandidateSelectionGuard | None = None,
        pending_guard: PendingEffectGuard | None = None,
    ) -> tuple[PublicationLease, SessionState]:
        with session_scope_guard(owner, session_id), db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._load_row(conn, owner, session_id)
            if selection_guard is not None:
                selection_guard.check(state)
            if pending_guard is not None:
                pending_guard.check(state)
            else:
                # Revoke cross-generation authority in this same transaction;
                # keep the pending ID available for the existing cleanup path.
                state.metadata.pop("pending_effect_binding", None)
            _consume_delivered_effect_receipts(state)
            owner_digest, session_digest = self._scope(owner, session_id)
            state.generation = max(
                state.generation,
                self._generation_floor(
                    conn,
                    owner_digest=owner_digest,
                    session_digest=session_digest,
                ),
            ) + 1
            turn_id = secrets.token_urlsafe(12)
            _begin_publication(state, request_id, turn_id)
            self._write_state(conn, state)
        lease = PublicationLease(
            owner=owner,
            session_id=session_id,
            generation=state.generation,
            turn_id=turn_id,
            request_id=request_id,
        )
        return lease, state.clone()

    def _is_current_sync(self, lease: PublicationLease) -> bool:
        from app.agent.feature_gate import RuntimeFenceError

        with db.get_conn() as conn:
            conn.execute("BEGIN")
            try:
                self.require_current_in_transaction(lease, conn)
            except (StalePublicationError, RuntimeFenceError):
                return False
            return True

    def require_current_in_transaction(
        self, lease: PublicationLease, conn: sqlite3.Connection,
    ) -> None:
        """复用调用方事务校验发布权；不另开连接、不提交，也不创建默认会话。"""
        if not conn.in_transaction:
            raise ValueError("publication check requires an active SQLite transaction")
        if lease.continuation_id:
            from .continuation import check_claim

            state = self._load_row(conn, lease.owner, lease.session_id)
            check_claim(state, lease, self._clock(), conn, publication=True)
            current = publication_matches(lease, generation=state.generation,
                                          confirmed=state.metadata.get("confirmed_publication"))
        else:
            owner_digest, session_digest = self._scope(lease.owner, lease.session_id)
            row = conn.execute(
                "SELECT generation, "
                "json_extract(CASE WHEN json_valid(state_json) THEN state_json ELSE '{}' END, "
                "'$.metadata.confirmed_publication.generation') AS confirmed_generation, "
                "json_extract(CASE WHEN json_valid(state_json) THEN state_json ELSE '{}' END, "
                "'$.metadata.confirmed_publication.turn_id') AS confirmed_turn "
                "FROM agent_kernel_sessions WHERE owner_digest=? AND session_digest=?",
                (owner_digest, session_digest),
            ).fetchone()
            current = row is not None and publication_matches(
                lease, generation=int(row["generation"]),
                confirmed={"generation": row["confirmed_generation"], "turn_id": row["confirmed_turn"]},
            )
        if not current:
            raise StalePublicationError("turn no longer owns publication authority")

    def _commit_sync(
        self,
        lease: PublicationLease,
        conversation: Sequence[Mapping[str, Any]] | None,
        updates: Sequence[StateUpdate],
        task_patch: TaskPatch | None = None,
    ) -> SessionState:
        with db.get_conn() as conn:
            # 只读快拒绝避免旧lease等待确认回执的写锁；它不授予后续提交权。
            if not publication_commit_matches(lease, self._load_row(conn, lease.owner, lease.session_id), conversation, updates):
                raise StalePublicationError("turn no longer owns publication authority")
            conn.execute("BEGIN IMMEDIATE")
            state = self._load_row(conn, lease.owner, lease.session_id)
            if not publication_commit_matches(lease, state, conversation, updates):
                raise StalePublicationError("turn no longer owns publication authority")
            # 写事务把发布权复验与保存串成同一CAS；旧lease在争用会话锁前退出。
            # 确认执行仍持原会话锁，普通提交不得阻塞等待正在执行的外部写。
            with session_scope_guard(lease.owner, lease.session_id, kind="commit"), conn:
                from .continuation import apply_actions, check_claim, task_changed
                check_claim(state, lease, self._clock(), conn)
                if task_patch is not None:
                    state.metadata["task"] = reduce_task(state.metadata.get("task"), task_patch, owner=state.owner, session_id=state.session_id)
                    task_changed(state)
                if conversation is not None:
                    commit_conversation(state, conversation, updates)
                apply_actions(state, lease, updates, self._clock(), conn)
                state.apply(updates)
                self._write_state(conn, state)
        return state.clone()

    def _update_effect_state_sync(
        self, owner: str, session_id: str,
        change: Callable[[SessionState], Any],
    ) -> Any:
        with session_scope_guard(owner, session_id, kind="commit"), db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self.update_effect_state_in_transaction(
                conn, owner=owner, session_id=session_id, change=change,
            )

    def update_effect_state_in_transaction(
        self, conn: Any, *, owner: str, session_id: str,
        change: Callable[[SessionState], Any],
    ) -> Any:
        """借调用方已有事务；确认管道已持会话锁，不另开连接或提交。"""
        if not conn.in_transaction:
            raise ValueError("effect admission requires an active SQLite transaction")
        owner_digest, session_digest = self._scope(owner, session_id)
        row = conn.execute(
            "SELECT 1 FROM agent_kernel_sessions WHERE owner_digest=? AND session_digest=?",
            (owner_digest, session_digest),
        ).fetchone()
        if row is None:
            return None
        current = self._load_row(conn, owner, session_id)
        updated, result, changed = _apply_effect_state_change(current, change)
        if changed:
            self._write_state(conn, updated)
        return result

    def _due_effect_waits_sync(self, limit: int = 16) -> list[dict[str, Any]]:
        maximum = max(0, limit)
        if not maximum:
            return []
        now = self._clock()
        due: list[tuple[float, str, dict[str, Any]]] = []
        with db.get_conn() as conn:
            rows = conn.execute(_DUE_EFFECT_WAITS_SQL, (now, maximum)).fetchall()
            for row in rows:
                owner_digest = str(row["owner_digest"])
                session_digest = str(row["session_digest"])
                generation = int(row["generation"])
                try:
                    payload = self._decode(
                        row["state_json"],
                        row["state_hmac"],
                        domain=f"state:v1:{owner_digest}:{session_digest}:{generation}".encode(),
                        expected_type=dict,
                    )
                except (TypeError, ValueError):
                    continue
                metadata = payload.get("metadata")
                if not isinstance(metadata, Mapping) or not _effect_time_is_due(
                    metadata.get("effect_next_poll_at"), now,
                ):
                    continue
                waits = metadata.get("effect_waits")
                if not isinstance(waits, Mapping):
                    continue
                for plan_id, record in waits.items():
                    if (
                        not isinstance(record, Mapping)
                        or record.get("delivered") is True
                        or not _effect_time_is_due(record.get("next_poll_at"), now)
                    ):
                        continue
                    item = deepcopy(dict(record))
                    item["plan_id"] = plan_id
                    due.append((float(record["next_poll_at"]), str(plan_id), item))
        due.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in due[:maximum]]

    def _load_sync(self, owner: str, session_id: str) -> SessionState:
        with db.get_conn() as conn:
            state = self._load_existing_row(conn, owner, session_id)
            # 缺行只是稳定的读取视图；实际首次写入才创建新的生命周期。
            return state if state is not None else SessionState(owner=owner, session_id=session_id, lifecycle_id="")

    def _patch_session_display_sync(
        self, owner: str, session_id: str, patch: dict[str, Any]
    ) -> dict[str, Any] | None:
        owner_digest, session_digest = self._scope(owner, session_id)
        with db.get_conn() as conn:
            # 跨进程事务内读最新签名状态；禁止 load -> commit 覆盖新回合。
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT generation,state_json,state_hmac,updated_at FROM agent_kernel_sessions "
                "WHERE owner_digest=? AND session_digest=?",
                (owner_digest, session_digest),
            ).fetchone()
            if row is None:
                return None
            generation = int(row["generation"])
            domain = f"state:v1:{owner_digest}:{session_digest}:{generation}".encode()
            payload = self._decode(
                row["state_json"], row["state_hmac"], domain=domain, expected_type=dict,
            )
            metadata = payload.get("metadata", {})
            if not isinstance(metadata, dict) or payload.get("session_id") != session_id:
                raise ValueError("会话显示元数据无效")
            # 不经过 State DTO 的截断/归一化往返，保留所有非显示字段的原始 JSON 值。
            payload["metadata"] = {**metadata, **deepcopy(patch)}
            state = self._state_from_payload(
                owner=owner, session_id=session_id, generation=generation, payload=payload,
            )
            payload["lifecycle_id"] = state.lifecycle_id
            encoded, signature = self._encode(
                payload, domain=domain, maximum=self.max_state_bytes,
            )
            # 显示更新不更改 generation/epoch/updated_at，也不碰确认与对话。
            conn.execute(
                "UPDATE agent_kernel_sessions SET state_json=?,state_hmac=? "
                "WHERE owner_digest=? AND session_digest=?",
                (encoded, signature, owner_digest, session_digest),
            )
            return session_summary(state, updated_at=float(row["updated_at"]))

    def _list_sessions_sync(self, owner: str, limit: int) -> list[dict[str, Any]]:
        owner_digest = db.native_agent_owner_digest(owner, secret=self._secret())
        maximum = max(1, min(limit, 100))
        result: list[dict[str, Any]] = []
        with db.get_conn() as conn:
            # 排序与按键读取共用一个读快照，避免并发 PATCH/新回合混入旧排序。
            conn.execute("BEGIN")
            # 全历史只排序小键；不让完整 state_json 进入临时 B-tree。
            keys = conn.execute(
                "SELECT session_digest "
                "FROM agent_kernel_sessions WHERE owner_digest=? "
                "ORDER BY CASE WHEN json_valid(state_json) THEN "
                "CASE WHEN json_type(state_json,'$.metadata.pinned')='true' "
                "THEN 1 ELSE 0 END ELSE 0 END DESC, updated_at DESC, session_digest",
                (owner_digest,),
            )
            for key in keys:
                session_digest = str(key["session_digest"])
                row = conn.execute(
                    "SELECT generation,state_json,state_hmac,updated_at "
                    "FROM agent_kernel_sessions WHERE owner_digest=? AND session_digest=?",
                    (owner_digest, session_digest),
                ).fetchone()
                if row is None:
                    continue
                generation = int(row["generation"])
                try:
                    payload = self._decode(
                        row["state_json"], row["state_hmac"],
                        domain=f"state:v1:{owner_digest}:{session_digest}:{generation}".encode(),
                        expected_type=dict,
                    )
                    session_id = str(payload.get("session_id") or "").strip()
                    if not session_id or self._scope(owner, session_id)[1] != session_digest:
                        continue
                    state = self._state_from_payload(
                        owner=owner, session_id=session_id, generation=generation, payload=payload,
                    )
                except (TypeError, ValueError):
                    continue
                result.append(session_summary(state, updated_at=float(row["updated_at"])))
                # 未通过 HMAC 的记录不能占用有限列表的名额。
                if len(result) >= maximum:
                    break
        return result

    def _reset_session_sync(self, owner: str, session_id: str) -> SessionState:
        owner_digest, session_digest = self._scope(owner, session_id)
        with session_scope_guard(owner, session_id), db.get_conn() as conn:
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
        with session_scope_guard(owner, session_id), db.get_conn() as conn:
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
        lease: PublicationLease | None = None,
    ) -> OpaqueReference:
        owner_digest, session_digest = self._scope(owner, session_id)
        normalized_kind = (kind or "").strip().casefold()
        if not normalized_kind:
            raise ReferenceError("reference kind is required")
        now = self._clock()
        expires_at = now + max(1, min(ttl_seconds, 86_400))
        ref_id = "ref_" + secrets.token_urlsafe(18)
        encoded, signature = self._encode_reference(
            value,
            domain=f"ref:v1:{ref_id}:{owner_digest}:{session_digest}:{normalized_kind}:{expires_at}".encode(),
        )
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM agent_kernel_refs WHERE expires_at<=?", (now,))
            if lease is not None:
                from .continuation import check_claim
                state = self._load_row(conn, lease.owner, lease.session_id)
                if not publication_commit_matches(lease, state, None, ()):
                    raise StalePublicationError("reference publication revoked")
                check_claim(state, lease, self._clock(), conn)
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
    ) -> tuple[Any, float]:
        ref_id = (ref or "").strip()
        if not ref_id.startswith("ref_") or len(ref_id) > 200:
            raise ReferenceError("reference is invalid")
        owner_digest, session_digest = self._scope(owner, session_id)
        observed_at = time.monotonic()
        now = self._clock()
        with db.get_conn() as conn:
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
        expected = (expected_kind or "").strip().casefold()
        if expected and expected != kind:
            raise ReferenceError("reference type mismatch")
        expires_at = float(row["expires_at"])
        try:
            value = self._decode_reference(
                row["value_json"],
                row["value_hmac"],
                domain=f"ref:v1:{ref_id}:{owner_digest}:{session_digest}:{kind}:{expires_at}".encode(),
            )
        except (TypeError, ValueError) as exc:
            raise ReferenceError("reference payload is invalid") from exc
        return value, observed_at + (expires_at - now)

    def _append_event_sync(self, event: AgentEvent, owner: str, lease: PublicationLease | None = None) -> None:
        owner_digest, session_digest = self._scope(owner, event.session_id)
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if lease is not None:
                state = self._load_row(conn, owner, lease.session_id)
                if (event.session_id, event.turn_id, event.request_id) != (lease.session_id, lease.turn_id, lease.request_id):
                    raise StalePublicationError("event publication revoked")
                if not (event.type is AgentEventType.TURN_CANCELLED and cancelled_publication_matches(state, lease)):
                    if not publication_commit_matches(lease, state, None, ()):
                        raise StalePublicationError("event publication revoked")
                    from .continuation import check_claim
                    check_claim(state, lease, self._clock(), conn, publication=True)
            else:
                try:
                    state = self._load_existing_row(conn, owner, event.session_id)
                except ValueError:
                    # 无lease的失败审计仍可落盘；损坏state不能授予活跃轮保留。
                    state = None
            self._write_event(conn, event, owner_digest, session_digest, state)

    def _write_event(self, conn, event, owner_digest, session_digest, state):
        encoded, signature = self._encode(
            event.to_dict(),
            domain=f"event:v1:{event.event_id}:{owner_digest}:{session_digest}".encode(),
            maximum=self.max_event_bytes,
        )
        now = self._clock()
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
        active = (state.metadata.get("active_publication") or {}) if state is not None else {}
        protected = ((active["turn_id"], active["request_id"])
                     if state is not None and active.get("generation") == state.generation and active.get("status") == "running"
                     and active.get("turn_id") and active.get("request_id") else None)
        if event.type in TERMINAL_EVENT_TYPES and protected == (event.turn_id, event.request_id):
            protected = None
        history_scope = "owner_digest=? AND session_digest=?"
        history_args = (owner_digest, session_digest)
        if protected:
            history_scope += " AND NOT (turn_id=? AND request_id=?)"
            history_args += protected
        conn.execute(
            "DELETE FROM agent_kernel_events WHERE rowid IN ("
            "SELECT rowid FROM agent_kernel_events "
            f"WHERE {history_scope} "
            "ORDER BY created_at DESC,rowid DESC LIMIT -1 OFFSET ?)",
            (*history_args, self.max_events_per_session),
        )
        conn.execute(
            f"DELETE FROM agent_kernel_events WHERE {history_scope} AND created_at<?",
            (*history_args, now - self.event_retention_seconds),
        )
        if self._should_prune_all_events():
            expired_scope = "created_at<?"
            expired_args = (now - self.event_retention_seconds,)
            if protected:
                expired_scope += " AND NOT (owner_digest=? AND session_digest=? AND turn_id=? AND request_id=?)"
                expired_args += (owner_digest, session_digest, *protected)
            conn.execute(f"DELETE FROM agent_kernel_events WHERE {expired_scope}", expired_args)

    def _list_events_sync(
        self, owner: str, session_id: str, limit: int, request_id: str = "", turn_id: str = "",
    ) -> list[dict[str, Any]]:
        owner_digest, session_digest = self._scope(owner, session_id)
        bounded = max(1, min(limit, 500))
        with db.get_conn() as conn:
            query = "SELECT event_id,event_json,event_hmac FROM agent_kernel_events WHERE owner_digest=? AND session_digest=? "
            if turn_id:
                rows = conn.execute(query + "AND request_id=? AND turn_id=? ORDER BY sequence",
                                    (owner_digest, session_digest, request_id, turn_id)).fetchall()
            else:
                rows = list(reversed(conn.execute(query + "ORDER BY created_at DESC,rowid DESC LIMIT ?",
                                                 (owner_digest, session_digest, bounded)).fetchall()))
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                value = self._decode(
                    row["event_json"],
                    row["event_hmac"],
                    domain=f"event:v1:{row['event_id']}:{owner_digest}:{session_digest}".encode(),
                    expected_type=dict,
                )
            except ValueError:
                continue
            if turn_id and (value.get("request_id"), value.get("turn_id")) != (request_id, turn_id):
                continue
            result.append(value)
        return result
