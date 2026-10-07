"""Kernel 会话、引用与事件的统一 SQLite 仓储。"""

from __future__ import annotations

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
from .session_guard import guarded_state_call, session_io, session_scope_guard
from .state import (
    CandidateSelectionGuard,
    PublicationLease,
    SelectionInvalidError,
    SessionBusyError,
    SessionState,
    StalePublicationError,
    StateUpdate,
    _apply_effect_state_change,
    _consume_delivered_effect_receipts,
    _effect_time_is_due,
    candidate_metadata_only,
    merge_effect_receipts,
    retain_conversation,
    publication_commit_matches,
    publication_matches,
)
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
        self.max_state_bytes = max(16 * 1024, int(max_state_bytes))
        self.max_ref_bytes = max(4 * 1024, int(max_ref_bytes))
        self.max_event_bytes = max(4 * 1024, int(max_event_bytes))
        self.max_events_per_session = max(10, int(max_events_per_session))
        self.event_retention_seconds = max(3_600, int(event_retention_seconds))
        self._event_maintenance_lock = threading.Lock()
        self._events_since_global_prune = 0

    async def begin_turn(
        self, *, owner: str, session_id: str, request_id: str,
        selection_guard: CandidateSelectionGuard | None = None,
    ) -> tuple[PublicationLease, SessionState]:
        try:
            return await guarded_state_call(
                owner, session_id, self._begin_turn_sync,
                owner,
                session_id,
                request_id,
                selection_guard,
            )
        except SessionBusyError as exc:
            if selection_guard is not None:
                raise SelectionInvalidError("候选正在处理，请使用最新选择状态。") from exc
            raise

    async def is_current(self, lease: PublicationLease) -> bool:
        return await session_io(self._is_current_sync, lease)

    async def commit(
        self,
        lease: PublicationLease,
        *,
        conversation: Sequence[Mapping[str, Any]] | None = None,
        updates: Sequence[StateUpdate] = (),
    ) -> SessionState:
        try:
            return await guarded_state_call(
                lease.owner, lease.session_id, self._commit_sync,
                lease,
                conversation,
                tuple(updates),
                kind="commit",
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

    async def resolve(
        self,
        ref: str,
        *,
        owner: str,
        session_id: str,
        expected_kind: str = "",
    ) -> Any:
        return await session_io(
            self._resolve_ref_sync,
            ref,
            owner,
            session_id,
            expected_kind,
        )

    async def append(self, event: AgentEvent, *, owner: str) -> None:
        await session_io(self._append_event_sync, event, owner)

    async def list_events(
        self,
        *,
        owner: str,
        session_id: str,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return await session_io(
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
            "conversation": deepcopy(retain_conversation(state.conversation)),
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
        self, owner: str, session_id: str, request_id: str,
        selection_guard: CandidateSelectionGuard | None = None,
    ) -> tuple[PublicationLease, SessionState]:
        with session_scope_guard(owner, session_id), db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._load_row(conn, owner, session_id)
            if selection_guard is not None:
                selection_guard.check(state)
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
            row = conn.execute(
                "SELECT generation, "
                "json_extract(CASE WHEN json_valid(state_json) THEN state_json ELSE '{}' END, "
                "'$.metadata.confirmed_publication.generation') AS confirmed_generation, "
                "json_extract(CASE WHEN json_valid(state_json) THEN state_json ELSE '{}' END, "
                "'$.metadata.confirmed_publication.turn_id') AS confirmed_turn "
                "FROM agent_kernel_sessions WHERE owner_digest=? AND session_digest=?",
                (owner_digest, session_digest),
            ).fetchone()
        return bool(row is not None and publication_matches(
            lease, generation=int(row["generation"]),
            confirmed={"generation": row["confirmed_generation"], "turn_id": row["confirmed_turn"]},
        ))

    def _commit_sync(
        self,
        lease: PublicationLease,
        conversation: Sequence[Mapping[str, Any]] | None,
        updates: Sequence[StateUpdate],
    ) -> SessionState:
        with session_scope_guard(lease.owner, lease.session_id, kind="commit"), db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._load_row(conn, lease.owner, lease.session_id)
            if not publication_commit_matches(lease, state, conversation, updates):
                raise StalePublicationError("turn no longer owns publication authority")
            if conversation is not None:
                state.conversation = retain_conversation(merge_effect_receipts(
                    conversation, state.metadata,
                ))
            state.apply(updates)
            self._write_state(conn, state)
        return state.clone()

    def _update_effect_state_sync(
        self, owner: str, session_id: str,
        change: Callable[[SessionState], Any],
    ) -> Any:
        owner_digest, session_digest = self._scope(owner, session_id)
        with session_scope_guard(owner, session_id, kind="commit"), db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT 1 FROM agent_kernel_sessions "
                "WHERE owner_digest=? AND session_digest=?",
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
        maximum = max(0, int(limit))
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
                except (TypeError, ValueError, json.JSONDecodeError):
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
            return self._load_row(conn, owner, session_id)

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
            encoded, signature = self._encode(
                payload, domain=domain, maximum=self.max_state_bytes,
            )
            # 显示更新不更改 generation/epoch/updated_at，也不碰确认与对话。
            conn.execute(
                "UPDATE agent_kernel_sessions SET state_json=?,state_hmac=? "
                "WHERE owner_digest=? AND session_digest=?",
                (encoded, signature, owner_digest, session_digest),
            )
            state = self._state_from_payload(
                owner=owner, session_id=session_id, generation=generation, payload=payload,
            )
            return session_summary(state, updated_at=float(row["updated_at"]))

    def _list_sessions_sync(self, owner: str, limit: int) -> list[dict[str, Any]]:
        owner_digest = self._digest(owner, domain=b"owner:v1")
        maximum = max(1, min(int(limit), 100))
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
                except (TypeError, ValueError, json.JSONDecodeError):
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
