"""Agent 短期会话上下文的安全 SQLite 持久化。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import math
import time
from typing import Any, Callable, Protocol

from app import database as db
from app.modules.web_secret import get_web_secret

logger = logging.getLogger(__name__)

_CONTEXT_TYPES = frozenset({
    "patrol",
    "download_submission",
    "resource_candidates",
    "discovery_candidates",
    "read_operation",
    "local_media_tasks",
    "discovery_mapping",
    "directory_scrape",
})
_SCHEMA_VERSION = 1
_DEFAULT_MAX_PAYLOAD_BYTES = 32 * 1024
_DEFAULT_MAX_ROWS = 4096
_DEFAULT_MAX_EPOCHS = 8192


@dataclass(frozen=True)
class PersistedAgentContext:
    """已通过版本与基础结构校验的持久化上下文。"""

    payload: dict[str, Any]
    expires_at: float
    revision: int = 0
    generation: int = 0


@dataclass(frozen=True)
class AgentContextWriteGuard:
    """owner/context 维度的 latest-wins 写入令牌。"""

    generation: int
    revision: int


class AgentSessionContextRepository(Protocol):
    """Store 所依赖的最小持久化接口。"""

    def replace_latest(
        self,
        *,
        owner: str,
        context_type: str,
        payload: dict[str, Any],
        expires_at: float,
    ) -> None: ...

    def mutate_latest(
        self,
        *,
        owner: str,
        context_type: str,
        updater: Callable[[dict[str, Any] | None], dict[str, Any]],
        expires_at: float,
    ) -> PersistedAgentContext: ...

    def append_download(
        self,
        *,
        owner: str,
        payload: dict[str, Any],
        expires_at: float,
        max_items: int,
    ) -> None: ...

    def append_snapshot(
        self,
        *,
        owner: str,
        context_type: str,
        payload: dict[str, Any],
        expires_at: float,
        max_items: int,
    ) -> None: ...

    def get_latest(
        self,
        *,
        owner: str,
        context_type: str,
        now: float,
    ) -> PersistedAgentContext | None: ...

    def list_downloads(
        self,
        *,
        owner: str,
        now: float,
        limit: int,
    ) -> tuple[PersistedAgentContext, ...]: ...

    def list_snapshots(
        self,
        *,
        owner: str,
        context_type: str,
        now: float,
        limit: int,
    ) -> tuple[PersistedAgentContext, ...]: ...

    def delete_latest(self, *, owner: str, context_type: str) -> int: ...

    def delete_downloads(self, *, owner: str) -> int: ...

    def delete_owner(self, *, owner: str) -> int: ...

    def begin_context(self, *, owner: str, context_type: str) -> AgentContextWriteGuard: ...

    def begin_context_update(
        self, *, owner: str, context_type: str
    ) -> tuple[PersistedAgentContext | None, AgentContextWriteGuard]: ...

    def replace_latest_guarded(
        self,
        *,
        owner: str,
        context_type: str,
        payload: dict[str, Any],
        expires_at: float,
        guard: AgentContextWriteGuard,
    ) -> PersistedAgentContext | None: ...

    def consume_latest_guarded(
        self,
        *,
        owner: str,
        context_type: str,
        guard: AgentContextWriteGuard,
    ) -> bool: ...

    def invalidate_owner(
        self, *, owner: str, context_types: tuple[str, ...]
    ) -> int: ...

    def invalidate_context(self, *, owner: str, context_type: str) -> int: ...


class SQLiteAgentSessionContextRepository:
    """只保存安全投影、按会话指纹隔离且自动过期的 SQLite 仓储。"""

    def __init__(
        self,
        *,
        secret_provider: Callable[[], str] = get_web_secret,
        clock: Callable[[], float] = time.time,
        max_payload_bytes: int = _DEFAULT_MAX_PAYLOAD_BYTES,
        max_rows: int = _DEFAULT_MAX_ROWS,
        max_epochs: int = _DEFAULT_MAX_EPOCHS,
    ) -> None:
        self._secret_provider = secret_provider
        self._clock = clock
        self.max_payload_bytes = max(1024, int(max_payload_bytes))
        self.max_rows = max(128, int(max_rows))
        self.max_epochs = max(128, int(max_epochs))

    def replace_latest(
        self,
        *,
        owner: str,
        context_type: str,
        payload: dict[str, Any],
        expires_at: float,
    ) -> None:
        normalized_type = self._context_type(context_type, allow_download=False)
        owner_digest = self._owner_digest(owner)
        expiry = self._expiry(expires_at)
        encoded = self._encode(
            owner_digest=owner_digest,
            context_type=normalized_type,
            payload=payload,
            expires_at=expiry,
        )
        now = float(self._clock())
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._prune(conn, now=now)
            existing_count = self._context_row_count(
                conn, owner_digest=owner_digest, context_type=normalized_type,
            )
            self._require_row_capacity(conn, reclaimed_rows=existing_count)
            conn.execute(
                "DELETE FROM agent_session_context "
                "WHERE owner_digest=? AND context_type=?",
                (owner_digest, normalized_type),
            )
            conn.execute(
                "INSERT INTO agent_session_context("
                "owner_digest,context_type,payload,expires_at,created_at"
                ") VALUES(?,?,?,?,?)",
                (owner_digest, normalized_type, encoded, expiry, db.now()),
            )

    def begin_context(
        self, *, owner: str, context_type: str,
    ) -> AgentContextWriteGuard:
        """原子开启一个新的 owner-bound 工作流世代，并撤销旧 latest。"""
        normalized_type = self._context_type(context_type, allow_download=False)
        owner_digest = self._owner_digest(owner)
        now = float(self._clock())
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._prune(conn, now=now)
            self._require_epoch_capacity(
                conn, owner_digest=owner_digest, context_type=normalized_type,
            )
            generation = self._advance_generation(
                conn, owner_digest=owner_digest, context_type=normalized_type, now=now,
            )
            conn.execute(
                "DELETE FROM agent_session_context "
                "WHERE owner_digest=? AND context_type=?",
                (owner_digest, normalized_type),
            )
        return AgentContextWriteGuard(generation=generation, revision=0)

    def begin_context_update(
        self, *, owner: str, context_type: str,
    ) -> tuple[PersistedAgentContext | None, AgentContextWriteGuard]:
        """开启 latest-wins 更新世代，同时保留并返回当前安全快照。"""
        normalized_type = self._context_type(context_type, allow_download=False)
        owner_digest = self._owner_digest(owner)
        now = float(self._clock())
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._prune(conn, now=now)
            self._require_epoch_capacity(
                conn, owner_digest=owner_digest, context_type=normalized_type,
            )
            row = conn.execute(
                "SELECT id,payload,expires_at,context_generation "
                "FROM agent_session_context "
                "WHERE owner_digest=? AND context_type=? AND expires_at>? "
                "ORDER BY id DESC LIMIT 1",
                (owner_digest, normalized_type, now),
            ).fetchone()
            current = self._decode_row(
                row, owner_digest=owner_digest, context_type=normalized_type
            )
            if row is not None and current is None:
                conn.execute(
                    "DELETE FROM agent_session_context "
                    "WHERE owner_digest=? AND context_type=?",
                    (owner_digest, normalized_type),
                )
            generation = self._advance_generation(
                conn, owner_digest=owner_digest, context_type=normalized_type, now=now,
            )
            revision = current.revision if current is not None else 0
            if current is not None:
                conn.execute(
                    "UPDATE agent_session_context SET context_generation=? "
                    "WHERE id=? AND owner_digest=? AND context_type=?",
                    (generation, revision, owner_digest, normalized_type),
                )
                current = PersistedAgentContext(
                    payload=deepcopy(current.payload),
                    expires_at=current.expires_at,
                    revision=revision,
                    generation=generation,
                )
        return current, AgentContextWriteGuard(
            generation=generation, revision=revision,
        )

    def replace_latest_guarded(
        self,
        *,
        owner: str,
        context_type: str,
        payload: dict[str, Any],
        expires_at: float,
        guard: AgentContextWriteGuard,
    ) -> PersistedAgentContext | None:
        """仅当工作流世代与 latest revision 均未变化时替换上下文。"""
        normalized_type = self._context_type(context_type, allow_download=False)
        owner_digest = self._owner_digest(owner)
        expiry = self._expiry(expires_at)
        generation = self._positive_int(guard.generation, "generation")
        revision = self._nonnegative_int(guard.revision, "revision")
        encoded = self._encode(
            owner_digest=owner_digest,
            context_type=normalized_type,
            payload=payload,
            expires_at=expiry,
        )
        now = float(self._clock())
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._prune(conn, now=now)
            epoch = conn.execute(
                "SELECT generation FROM agent_session_context_epochs "
                "WHERE owner_digest=? AND context_type=?",
                (owner_digest, normalized_type),
            ).fetchone()
            current = conn.execute(
                "SELECT id,context_generation FROM agent_session_context "
                "WHERE owner_digest=? AND context_type=? AND expires_at>? "
                "ORDER BY id DESC LIMIT 1",
                (owner_digest, normalized_type, now),
            ).fetchone()
            current_revision = int(current["id"]) if current is not None else 0
            if (
                epoch is None
                or int(epoch["generation"]) != generation
                or current_revision != revision
                or (
                    current is not None
                    and int(current["context_generation"] or 0) != generation
                )
            ):
                return None
            existing_count = self._context_row_count(
                conn, owner_digest=owner_digest, context_type=normalized_type,
            )
            try:
                self._require_row_capacity(conn, reclaimed_rows=existing_count)
            except RuntimeError:
                return None
            conn.execute(
                "DELETE FROM agent_session_context "
                "WHERE owner_digest=? AND context_type=?",
                (owner_digest, normalized_type),
            )
            cursor = conn.execute(
                "INSERT INTO agent_session_context("
                "owner_digest,context_type,payload,expires_at,context_generation,created_at"
                ") VALUES(?,?,?,?,?,?)",
                (
                    owner_digest, normalized_type, encoded, expiry,
                    generation, db.now(),
                ),
            )
            self._touch_generation(
                conn, owner_digest=owner_digest, context_type=normalized_type, now=now,
            )
            new_revision = int(cursor.lastrowid)
        return PersistedAgentContext(
            payload=deepcopy(payload), expires_at=expiry,
            revision=new_revision, generation=generation,
        )

    def consume_latest_guarded(
        self,
        *,
        owner: str,
        context_type: str,
        guard: AgentContextWriteGuard,
    ) -> bool:
        """原子消费指定 revision，防止跨 Worker 重复执行同一 continuation。"""
        normalized_type = self._context_type(context_type, allow_download=False)
        owner_digest = self._owner_digest(owner)
        generation = self._positive_int(guard.generation, "generation")
        revision = self._positive_int(guard.revision, "revision")
        now = float(self._clock())
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._prune(conn, now=now)
            epoch = conn.execute(
                "SELECT generation FROM agent_session_context_epochs "
                "WHERE owner_digest=? AND context_type=?",
                (owner_digest, normalized_type),
            ).fetchone()
            if epoch is None or int(epoch["generation"]) != generation:
                return False
            deleted = conn.execute(
                "DELETE FROM agent_session_context "
                "WHERE id=? AND owner_digest=? AND context_type=? "
                "AND context_generation=? AND expires_at>?",
                (
                    revision, owner_digest, normalized_type, generation, now,
                ),
            )
            if deleted.rowcount == 1:
                self._touch_generation(
                    conn, owner_digest=owner_digest,
                    context_type=normalized_type, now=now,
                )
                return True
            return False

    def invalidate_owner(
        self, *, owner: str, context_types: tuple[str, ...],
    ) -> int:
        """原子推进指定上下文世代并删除 owner 的全部持久化上下文。"""
        owner_digest = self._owner_digest(owner)
        normalized_types = tuple(dict.fromkeys(
            self._context_type(value, allow_download=False) for value in context_types
        ))
        now = float(self._clock())
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._prune(conn, now=now)
            for context_type in normalized_types:
                exists = conn.execute(
                    "SELECT 1 FROM agent_session_context_epochs "
                    "WHERE owner_digest=? AND context_type=?",
                    (owner_digest, context_type),
                ).fetchone()
                if exists is not None:
                    self._advance_generation(
                        conn,
                        owner_digest=owner_digest,
                        context_type=context_type,
                        now=now,
                    )
            deleted = conn.execute(
                "DELETE FROM agent_session_context WHERE owner_digest=?",
                (owner_digest,),
            )
        return max(0, int(deleted.rowcount or 0))

    def invalidate_context(self, *, owner: str, context_type: str) -> int:
        """推进单个上下文世代并仅删除该类型，阻止迟到写入复活。"""
        normalized_type = self._context_type(context_type, allow_download=False)
        owner_digest = self._owner_digest(owner)
        now = float(self._clock())
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._prune(conn, now=now)
            self._require_epoch_capacity(
                conn, owner_digest=owner_digest, context_type=normalized_type,
            )
            self._advance_generation(
                conn, owner_digest=owner_digest, context_type=normalized_type, now=now,
            )
            deleted = conn.execute(
                "DELETE FROM agent_session_context "
                "WHERE owner_digest=? AND context_type=?",
                (owner_digest, normalized_type),
            )
        return max(0, int(deleted.rowcount or 0))

    def mutate_latest(
        self,
        *,
        owner: str,
        context_type: str,
        updater: Callable[[dict[str, Any] | None], dict[str, Any]],
        expires_at: float,
    ) -> PersistedAgentContext:
        """在单个写事务中读取、更新并替换 owner 最新上下文。"""
        normalized_type = self._context_type(context_type, allow_download=False)
        owner_digest = self._owner_digest(owner)
        expiry = self._expiry(expires_at)
        now = float(self._clock())
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._prune(conn, now=now)
            row = conn.execute(
                "SELECT id,payload,expires_at,context_generation "
                "FROM agent_session_context "
                "WHERE owner_digest=? AND context_type=? AND expires_at>? "
                "ORDER BY id DESC LIMIT 1",
                (owner_digest, normalized_type, now),
            ).fetchone()
            current = self._decode_row(
                row, owner_digest=owner_digest, context_type=normalized_type
            )
            payload = updater(
                deepcopy(current.payload) if current is not None else None
            )
            encoded = self._encode(
                owner_digest=owner_digest,
                context_type=normalized_type,
                payload=payload,
                expires_at=expiry,
            )
            existing_count = self._context_row_count(
                conn, owner_digest=owner_digest, context_type=normalized_type,
            )
            self._require_row_capacity(conn, reclaimed_rows=existing_count)
            conn.execute(
                "DELETE FROM agent_session_context "
                "WHERE owner_digest=? AND context_type=?",
                (owner_digest, normalized_type),
            )
            conn.execute(
                "INSERT INTO agent_session_context("
                "owner_digest,context_type,payload,expires_at,created_at"
                ") VALUES(?,?,?,?,?)",
                (owner_digest, normalized_type, encoded, expiry, db.now()),
            )
        return PersistedAgentContext(payload=deepcopy(payload), expires_at=expiry)

    def append_download(
        self,
        *,
        owner: str,
        payload: dict[str, Any],
        expires_at: float,
        max_items: int,
    ) -> None:
        owner_digest = self._owner_digest(owner)
        expiry = self._expiry(expires_at)
        encoded = self._encode(
            owner_digest=owner_digest,
            context_type="download_submission",
            payload=payload,
            expires_at=expiry,
        )
        bounded_items = max(1, min(int(max_items), 32))
        now = float(self._clock())
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._prune(conn, now=now)
            existing_count = self._context_row_count(
                conn, owner_digest=owner_digest, context_type="download_submission",
            )
            reclaimed_rows = max(0, existing_count + 1 - bounded_items)
            self._require_row_capacity(conn, reclaimed_rows=reclaimed_rows)
            conn.execute(
                "INSERT INTO agent_session_context("
                "owner_digest,context_type,payload,expires_at,created_at"
                ") VALUES(?,?,?,?,?)",
                (owner_digest, "download_submission", encoded, expiry, db.now()),
            )
            conn.execute(
                "DELETE FROM agent_session_context "
                "WHERE owner_digest=? AND context_type='download_submission' AND id NOT IN ("
                "SELECT id FROM agent_session_context "
                "WHERE owner_digest=? AND context_type='download_submission' "
                "ORDER BY id DESC LIMIT ?"
                ")",
                (owner_digest, owner_digest, bounded_items),
            )

    def append_snapshot(
        self,
        *,
        owner: str,
        context_type: str,
        payload: dict[str, Any],
        expires_at: float,
        max_items: int,
    ) -> None:
        normalized_type = self._context_type(context_type, allow_download=False)
        owner_digest = self._owner_digest(owner)
        expiry = self._expiry(expires_at)
        encoded = self._encode(
            owner_digest=owner_digest,
            context_type=normalized_type,
            payload=payload,
            expires_at=expiry,
        )
        bounded_items = max(1, min(int(max_items), 16))
        now = float(self._clock())
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._prune(conn, now=now)
            existing_count = self._context_row_count(
                conn, owner_digest=owner_digest, context_type=normalized_type,
            )
            reclaimed_rows = max(0, existing_count + 1 - bounded_items)
            self._require_row_capacity(conn, reclaimed_rows=reclaimed_rows)
            conn.execute(
                "INSERT INTO agent_session_context("
                "owner_digest,context_type,payload,expires_at,created_at"
                ") VALUES(?,?,?,?,?)",
                (owner_digest, normalized_type, encoded, expiry, db.now()),
            )
            conn.execute(
                "DELETE FROM agent_session_context WHERE owner_digest=? AND context_type=? "
                "AND id NOT IN (SELECT id FROM agent_session_context "
                "WHERE owner_digest=? AND context_type=? ORDER BY id DESC LIMIT ?)",
                (owner_digest, normalized_type, owner_digest, normalized_type, bounded_items),
            )

    def get_latest(
        self,
        *,
        owner: str,
        context_type: str,
        now: float,
    ) -> PersistedAgentContext | None:
        normalized_type = self._context_type(context_type, allow_download=False)
        owner_digest = self._owner_digest(owner)
        current = self._finite_number(now, "now")
        with db.get_conn() as conn:
            self._prune(conn, now=current)
            row = conn.execute(
                "SELECT id,payload,expires_at,context_generation "
                "FROM agent_session_context "
                "WHERE owner_digest=? AND context_type=? AND expires_at>? "
                "ORDER BY id DESC LIMIT 1",
                (owner_digest, normalized_type, current),
            ).fetchone()
        return self._decode_row(
            row,
            owner_digest=owner_digest,
            context_type=normalized_type,
        )

    def list_downloads(
        self,
        *,
        owner: str,
        now: float,
        limit: int,
    ) -> tuple[PersistedAgentContext, ...]:
        owner_digest = self._owner_digest(owner)
        current = self._finite_number(now, "now")
        bounded_limit = max(1, min(int(limit), 32))
        with db.get_conn() as conn:
            self._prune(conn, now=current)
            rows = conn.execute(
                "SELECT payload,expires_at FROM agent_session_context "
                "WHERE owner_digest=? AND context_type='download_submission' "
                "AND expires_at>? ORDER BY id DESC LIMIT ?",
                (owner_digest, current, bounded_limit),
            ).fetchall()
        return tuple(
            decoded
            for row in rows
            if (
                decoded := self._decode_row(
                    row,
                    owner_digest=owner_digest,
                    context_type="download_submission",
                )
            ) is not None
        )

    def list_snapshots(
        self,
        *,
        owner: str,
        context_type: str,
        now: float,
        limit: int,
    ) -> tuple[PersistedAgentContext, ...]:
        normalized_type = self._context_type(context_type, allow_download=False)
        owner_digest = self._owner_digest(owner)
        current = self._finite_number(now, "now")
        bounded_limit = max(1, min(int(limit), 16))
        with db.get_conn() as conn:
            self._prune(conn, now=current)
            rows = conn.execute(
                "SELECT payload,expires_at FROM agent_session_context "
                "WHERE owner_digest=? AND context_type=? AND expires_at>? "
                "ORDER BY id DESC LIMIT ?",
                (owner_digest, normalized_type, current, bounded_limit),
            ).fetchall()
        return tuple(
            decoded
            for row in rows
            if (
                decoded := self._decode_row(
                    row,
                    owner_digest=owner_digest,
                    context_type=normalized_type,
                )
            ) is not None
        )

    def delete_latest(self, *, owner: str, context_type: str) -> int:
        """仅删除指定会话的一类 latest 上下文。"""
        normalized_type = self._context_type(context_type, allow_download=False)
        owner_digest = self._owner_digest(owner)
        with db.get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_session_context "
                "WHERE owner_digest=? AND context_type=?",
                (owner_digest, normalized_type),
            )
        return max(0, int(cursor.rowcount or 0))

    def delete_downloads(self, *, owner: str) -> int:
        """删除指定会话的全部最近下载提交上下文。"""
        owner_digest = self._owner_digest(owner)
        with db.get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_session_context "
                "WHERE owner_digest=? AND context_type='download_submission'",
                (owner_digest,),
            )
        return max(0, int(cursor.rowcount or 0))

    def delete_owner(self, *, owner: str) -> int:
        """删除指定会话的全部持久化上下文。"""
        owner_digest = self._owner_digest(owner)
        with db.get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_session_context WHERE owner_digest=?",
                (owner_digest,),
            )
        return max(0, int(cursor.rowcount or 0))

    def owner_digest_for_tests(self, owner: str) -> str:
        """仅供回归测试核验不落库原始 owner。"""
        return self._owner_digest(owner)

    def _owner_digest(self, owner: str) -> str:
        normalized = str(owner or "").strip()
        if not normalized or len(normalized) > 512:
            raise ValueError("Agent 会话 owner 无效")
        secret = str(self._secret_provider() or "")
        if not secret:
            raise ValueError("Agent 会话指纹密钥不可用")
        return hmac.new(
            secret.encode("utf-8"),
            b"mediaflux-agent-session-context:v1\0" + normalized.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _context_type(value: str, *, allow_download: bool) -> str:
        normalized = str(value or "").strip()
        allowed = _CONTEXT_TYPES if allow_download else _CONTEXT_TYPES - {"download_submission"}
        if normalized not in allowed:
            raise ValueError("Agent 会话上下文类型无效")
        return normalized

    def _encode(
        self,
        *,
        owner_digest: str,
        context_type: str,
        payload: dict[str, Any],
        expires_at: float,
    ) -> str:
        if not isinstance(payload, dict):
            raise ValueError("Agent 会话上下文必须是对象")
        auth_tag = self._auth_tag(
            owner_digest=owner_digest,
            context_type=context_type,
            payload=payload,
            expires_at=expires_at,
        )
        encoded = json.dumps(
            {"version": _SCHEMA_VERSION, "data": payload, "auth": auth_tag},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > self.max_payload_bytes:
            raise ValueError("Agent 会话上下文过大")
        return encoded

    def _decode_row(
        self,
        row: Any,
        *,
        owner_digest: str,
        context_type: str,
    ) -> PersistedAgentContext | None:
        if row is None:
            return None
        try:
            encoded = str(row["payload"] or "")
            if not encoded or len(encoded.encode("utf-8")) > self.max_payload_bytes:
                return None
            envelope = json.loads(encoded)
            expires_at = float(row["expires_at"])
            row_keys = set(row.keys()) if hasattr(row, "keys") else set()
            revision = int(row["id"] or 0) if "id" in row_keys else 0
            generation = (
                int(row["context_generation"] or 0)
                if "context_generation" in row_keys else 0
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"version", "data", "auth"}
            or envelope.get("version") != _SCHEMA_VERSION
            or not isinstance(envelope.get("data"), dict)
            or not isinstance(envelope.get("auth"), str)
            or not math.isfinite(expires_at)
        ):
            return None
        expected_auth = self._auth_tag(
            owner_digest=owner_digest,
            context_type=context_type,
            payload=envelope["data"],
            expires_at=expires_at,
        )
        if not hmac.compare_digest(envelope["auth"], expected_auth):
            return None
        return PersistedAgentContext(
            payload=envelope["data"], expires_at=expires_at,
            revision=max(0, revision), generation=max(0, generation),
        )

    def _auth_tag(
        self,
        *,
        owner_digest: str,
        context_type: str,
        payload: dict[str, Any],
        expires_at: float,
    ) -> str:
        secret = str(self._secret_provider() or "")
        if not secret:
            raise ValueError("Agent 会话完整性密钥不可用")
        canonical_payload = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        message = "\0".join((
            "mediaflux-agent-session-context-record:v1",
            owner_digest,
            context_type,
            repr(float(expires_at)),
            canonical_payload,
        )).encode("utf-8")
        return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()

    @staticmethod
    def _expiry(value: float) -> float:
        return SQLiteAgentSessionContextRepository._finite_number(value, "expires_at")

    @staticmethod
    def _finite_number(value: float, label: str) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Agent 会话上下文 {label} 无效") from exc
        if not math.isfinite(parsed) or parsed <= 0:
            raise ValueError(f"Agent 会话上下文 {label} 无效")
        return parsed

    @staticmethod
    def _positive_int(value: int, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"Agent 会话上下文 {label} 无效")
        return int(value)

    @staticmethod
    def _nonnegative_int(value: int, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Agent 会话上下文 {label} 无效")
        return int(value)

    @staticmethod
    def _advance_generation(
        conn: Any, *, owner_digest: str, context_type: str, now: float,
    ) -> int:
        cursor = conn.execute(
            "INSERT INTO agent_session_context_generation_sequence DEFAULT VALUES"
        )
        generation = int(cursor.lastrowid)
        conn.execute(
            "DELETE FROM agent_session_context_generation_sequence WHERE id=?",
            (generation,),
        )
        conn.execute(
            "INSERT INTO agent_session_context_epochs("
            "owner_digest,context_type,generation,touched_at,updated_at"
            ") VALUES(?,?,?,?,?) ON CONFLICT(owner_digest,context_type) DO UPDATE SET "
            "generation=excluded.generation,touched_at=excluded.touched_at,"
            "updated_at=excluded.updated_at",
            (owner_digest, context_type, generation, now, db.now()),
        )
        return generation

    @staticmethod
    def _touch_generation(
        conn: Any, *, owner_digest: str, context_type: str, now: float,
    ) -> None:
        conn.execute(
            "UPDATE agent_session_context_epochs SET touched_at=?,updated_at=? "
            "WHERE owner_digest=? AND context_type=?",
            (now, db.now(), owner_digest, context_type),
        )

    @staticmethod
    def _prune(conn: Any, *, now: float) -> None:
        conn.execute("DELETE FROM agent_session_context WHERE expires_at<=?", (now,))
        conn.execute(
            "DELETE FROM agent_session_context_epochs WHERE touched_at<=? AND "
            "NOT EXISTS(SELECT 1 FROM agent_session_context c "
            "WHERE c.owner_digest=agent_session_context_epochs.owner_digest "
            "AND c.context_type=agent_session_context_epochs.context_type)",
            (now - 3600.0,),
        )

    @staticmethod
    def _context_row_count(
        conn: Any, *, owner_digest: str, context_type: str,
    ) -> int:
        return int(conn.execute(
            "SELECT COUNT(*) FROM agent_session_context "
            "WHERE owner_digest=? AND context_type=?",
            (owner_digest, context_type),
        ).fetchone()[0] or 0)

    def _require_row_capacity(self, conn: Any, *, reclaimed_rows: int) -> None:
        total = int(conn.execute(
            "SELECT COUNT(*) FROM agent_session_context"
        ).fetchone()[0] or 0)
        if total - max(0, int(reclaimed_rows)) + 1 > self.max_rows:
            raise RuntimeError("Agent 会话上下文容量已满")

    def _require_epoch_capacity(
        self, conn: Any, *, owner_digest: str, context_type: str,
    ) -> None:
        exists = conn.execute(
            "SELECT 1 FROM agent_session_context_epochs "
            "WHERE owner_digest=? AND context_type=?",
            (owner_digest, context_type),
        ).fetchone()
        if exists is not None:
            return
        total = int(conn.execute(
            "SELECT COUNT(*) FROM agent_session_context_epochs"
        ).fetchone()[0] or 0)
        if total < self.max_epochs:
            return
        conn.execute(
            "DELETE FROM agent_session_context_epochs WHERE rowid IN ("
            "SELECT e.rowid FROM agent_session_context_epochs e "
            "WHERE NOT EXISTS(SELECT 1 FROM agent_session_context c "
            "WHERE c.owner_digest=e.owner_digest AND c.context_type=e.context_type) "
            "ORDER BY e.touched_at ASC LIMIT ?"
            ")",
            (total - self.max_epochs + 1,),
        )
        remaining = int(conn.execute(
            "SELECT COUNT(*) FROM agent_session_context_epochs"
        ).fetchone()[0] or 0)
        if remaining >= self.max_epochs:
            raise RuntimeError("Agent 会话上下文世代容量已满")
