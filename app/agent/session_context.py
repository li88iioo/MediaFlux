"""Agent 短期会话上下文的安全 SQLite 持久化。"""
from __future__ import annotations

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
})
_SCHEMA_VERSION = 1
_DEFAULT_MAX_PAYLOAD_BYTES = 32 * 1024
_DEFAULT_MAX_ROWS = 4096


@dataclass(frozen=True)
class PersistedAgentContext:
    """已通过版本与基础结构校验的持久化上下文。"""

    payload: dict[str, Any]
    expires_at: float


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

    def append_download(
        self,
        *,
        owner: str,
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

    def delete_latest(self, *, owner: str, context_type: str) -> int: ...

    def delete_owner(self, *, owner: str) -> int: ...


class SQLiteAgentSessionContextRepository:
    """只保存安全投影、按会话指纹隔离且自动过期的 SQLite 仓储。"""

    def __init__(
        self,
        *,
        secret_provider: Callable[[], str] = get_web_secret,
        clock: Callable[[], float] = time.time,
        max_payload_bytes: int = _DEFAULT_MAX_PAYLOAD_BYTES,
        max_rows: int = _DEFAULT_MAX_ROWS,
    ) -> None:
        self._secret_provider = secret_provider
        self._clock = clock
        self.max_payload_bytes = max(1024, int(max_payload_bytes))
        self.max_rows = max(128, int(max_rows))

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
            self._prune(conn, now=now)
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
            self._bound_rows(conn)

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
            self._prune(conn, now=now)
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
            self._bound_rows(conn)

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
                "SELECT payload,expires_at FROM agent_session_context "
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
        return PersistedAgentContext(payload=envelope["data"], expires_at=expires_at)

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
    def _prune(conn: Any, *, now: float) -> None:
        conn.execute("DELETE FROM agent_session_context WHERE expires_at<=?", (now,))

    def _bound_rows(self, conn: Any) -> None:
        conn.execute(
            "DELETE FROM agent_session_context WHERE id NOT IN ("
            "SELECT id FROM agent_session_context ORDER BY id DESC LIMIT ?"
            ")",
            (self.max_rows,),
        )
