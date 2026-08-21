"""服务端一次性确认票据。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import secrets
import threading
import time
import unicodedata
from typing import Any, Callable

from app.agent.registry import AgentToolError


@dataclass(frozen=True)
class ConfirmationTicket:
    confirmation_id: str
    owner: str
    tool_name: str
    arguments: dict[str, Any]
    context_fingerprint: str
    expires_at: float
    owner_generation: int = 0
    followup_context: dict[str, Any] = field(default_factory=dict)
    confirmation_contract: dict[str, Any] = field(default_factory=dict)


_CONFIRMATION_REPLY_PHRASES = frozenset({
    "确认", "确认执行", "确定", "确定执行", "同意", "执行", "开始执行",
    "好的", "好的执行", "好的帮我执行", "好，执行", "好,执行",
    "ok", "yes", "confirm",
})
_CANCELLATION_REPLY_PHRASES = frozenset({
    "取消", "取消执行", "算了", "不要了", "不执行", "放弃",
    "cancel", "no",
})


def confirmation_reply_intent(value: Any) -> str | None:
    """只识别无附加条件的明确确认/取消短句，避免自然语言误触发写操作。"""
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    normalized = normalized.rstrip("。.!！")
    normalized = " ".join(normalized.split())
    if normalized in _CONFIRMATION_REPLY_PHRASES:
        return "confirm"
    if normalized in _CANCELLATION_REPLY_PHRASES:
        return "cancel"
    return None


class ConfirmationStore:
    """线程安全、会话绑定、短期且只能消费一次的确认票据存储。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = 60,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(24))
        self._lock = threading.RLock()
        self._tickets: dict[str, ConfirmationTicket] = {}
        self._owner_generations: dict[str, tuple[int, float]] = {}

    def issue(
        self,
        *,
        owner: str,
        tool_name: str,
        arguments: dict[str, Any],
        context_fingerprint: str = "",
        followup_context: dict[str, Any] | None = None,
        confirmation_contract: dict[str, Any] | None = None,
        expected_owner_generation: int | None = None,
    ) -> ConfirmationTicket:
        owner_key = str(owner or "").strip()
        if not owner_key:
            raise AgentToolError("当前会话无法创建确认请求", code="confirmation_invalid")
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            owner_generation = self._owner_generation_locked(owner_key, now=now, touch=True)
            if (
                expected_owner_generation is not None
                and int(expected_owner_generation) != owner_generation
            ):
                raise AgentToolError(
                    "会话已重置，请重新生成确认请求",
                    code="confirmation_invalid",
                )
            while len(self._tickets) >= self.max_entries:
                oldest_id = min(self._tickets, key=lambda key: self._tickets[key].expires_at)
                self._tickets.pop(oldest_id, None)
            confirmation_id = self._new_unique_id_locked()
            ticket = ConfirmationTicket(
                confirmation_id=confirmation_id,
                owner=owner_key,
                tool_name=str(tool_name or "").strip(),
                arguments=deepcopy(arguments),
                context_fingerprint=str(context_fingerprint or ""),
                expires_at=now + self.ttl_seconds,
                owner_generation=owner_generation,
                followup_context=deepcopy(followup_context or {}),
                confirmation_contract=deepcopy(confirmation_contract or {}),
            )
            self._tickets[confirmation_id] = ticket
            return ticket

    def claim(self, *, owner: str, confirmation_id: str) -> ConfirmationTicket:
        owner_key = str(owner or "").strip()
        ticket_id = str(confirmation_id or "").strip()
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            ticket = self._tickets.get(ticket_id)
            if (
                ticket is None
                or not owner_key
                or not secrets.compare_digest(ticket.owner, owner_key)
                or ticket.owner_generation != self._owner_generation_locked(
                    owner_key, now=now, touch=True
                )
            ):
                raise AgentToolError("确认请求无效或已过期", code="confirmation_invalid")
            self._tickets.pop(ticket_id, None)
            return ConfirmationTicket(
                confirmation_id=ticket.confirmation_id,
                owner=ticket.owner,
                tool_name=ticket.tool_name,
                arguments=deepcopy(ticket.arguments),
                context_fingerprint=ticket.context_fingerprint,
                expires_at=ticket.expires_at,
                owner_generation=ticket.owner_generation,
                followup_context=deepcopy(ticket.followup_context),
                confirmation_contract=deepcopy(ticket.confirmation_contract),
            )

    def list_active_tickets(self, *, owner: str) -> list[ConfirmationTicket]:
        """返回 owner 当前世代的有效票据快照，不消费票据。"""
        owner_key = str(owner or "").strip()
        if not owner_key:
            return []
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            generation = self._owner_generation_locked(owner_key, now=now, touch=True)
            tickets = [
                ticket
                for ticket in self._tickets.values()
                if ticket.owner_generation == generation
                and secrets.compare_digest(ticket.owner, owner_key)
                and ticket.expires_at > now
            ]
            tickets.sort(key=lambda item: (item.expires_at, item.confirmation_id))
            return [
                ConfirmationTicket(
                    confirmation_id=ticket.confirmation_id,
                    owner=ticket.owner,
                    tool_name=ticket.tool_name,
                    arguments=deepcopy(ticket.arguments),
                    context_fingerprint=ticket.context_fingerprint,
                    expires_at=ticket.expires_at,
                    owner_generation=ticket.owner_generation,
                    followup_context=deepcopy(ticket.followup_context),
                    confirmation_contract=deepcopy(ticket.confirmation_contract),
                )
                for ticket in tickets
            ]

    def discard(self, *, owner: str, confirmation_id: str) -> bool:
        """撤销属于指定会话且尚未消费的确认票据。"""
        owner_key = str(owner or "").strip()
        ticket_id = str(confirmation_id or "").strip()
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            ticket = self._tickets.get(ticket_id)
            if ticket is None or not owner_key or not secrets.compare_digest(ticket.owner, owner_key):
                return False
            self._tickets.pop(ticket_id, None)
            return True

    def rotate_owner(self, *, owner: str) -> tuple[int, int]:
        """推进 owner epoch 并撤销其票据，返回 ``(数量, 新 epoch)``。"""
        owner_key = str(owner or "").strip()
        if not owner_key:
            raise AgentToolError("当前会话无法创建确认请求", code="confirmation_invalid")
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            generation = self._new_owner_generation_locked()
            self._owner_generations[owner_key] = (generation, now)
            ticket_ids = [
                key
                for key, ticket in self._tickets.items()
                if secrets.compare_digest(ticket.owner, owner_key)
            ]
            for key in ticket_ids:
                self._tickets.pop(key, None)
            return len(ticket_ids), generation

    def revoke_owner(self, *, owner: str) -> int:
        """撤销某个会话仍然有效的全部确认票据。"""
        owner_key = str(owner or "").strip()
        if not owner_key:
            return 0
        revoked, _generation = self.rotate_owner(owner=owner_key)
        return revoked

    def owner_generation(self, *, owner: str) -> int:
        owner_key = str(owner or "").strip()
        if not owner_key:
            raise AgentToolError("当前会话无法创建确认请求", code="confirmation_invalid")
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            return self._owner_generation_locked(owner_key, now=now, touch=True)

    def reset(self) -> None:
        with self._lock:
            self._tickets.clear()
            self._owner_generations.clear()

    def _prune_locked(self, now: float) -> None:
        expired = [key for key, ticket in self._tickets.items() if ticket.expires_at <= now]
        for key in expired:
            self._tickets.pop(key, None)
        active_owners = {ticket.owner for ticket in self._tickets.values()}
        generation_cutoff = now - (self.ttl_seconds * 2)
        stale_owners = [
            owner
            for owner, (_generation, touched_at) in self._owner_generations.items()
            if touched_at <= generation_cutoff and owner not in active_owners
        ]
        for owner in stale_owners:
            self._owner_generations.pop(owner, None)
        max_owner_generations = max(32, self.max_entries * 4)
        while len(self._owner_generations) > max_owner_generations:
            removable = [
                owner for owner in self._owner_generations if owner not in active_owners
            ]
            if not removable:
                break
            oldest_owner = min(
                removable,
                key=lambda owner: self._owner_generations[owner][1],
            )
            self._owner_generations.pop(oldest_owner, None)

    def _owner_generation_locked(self, owner: str, *, now: float, touch: bool) -> int:
        current = self._owner_generations.get(owner)
        if current is None:
            generation = self._new_owner_generation_locked()
            touched_at = now
        else:
            generation, touched_at = current
        if touch or current is None:
            self._owner_generations[owner] = (generation, now)
        else:
            self._owner_generations[owner] = (generation, touched_at)
        return generation

    def _new_owner_generation_locked(self) -> int:
        # 使用不可预测且不复用的 epoch；即使旧 tombstone 被清理，慢 prepare
        # 也无法借由 generation 回退为 0 而在 reset 后重新签发票据。
        active_generations = {generation for generation, _ in self._owner_generations.values()}
        for _ in range(8):
            generation = secrets.randbits(63) or 1
            if generation not in active_generations:
                return generation
        raise AgentToolError("暂时无法创建确认请求", code="confirmation_unavailable")

    def _new_unique_id_locked(self) -> str:
        for _ in range(8):
            token = str(self._token_factory() or "").strip()
            if token and token not in self._tickets:
                return token
        raise AgentToolError("暂时无法创建确认请求", code="confirmation_unavailable")

class SQLiteConfirmationStore(ConfirmationStore):
    """SQLite-backed confirmation tickets shared by restarts and Web workers."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 60,
        max_entries: int = 256,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(24))

    @staticmethod
    def _ensure_schema(conn: Any) -> None:
        # Web worker/CLI may construct the Agent service before the application-wide
        # init hook runs. Keep this repository independently idempotent.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_confirmation_epochs("
            "owner_digest TEXT PRIMARY KEY,generation INTEGER NOT NULL "
            "CHECK(generation>0),touched_at REAL NOT NULL,updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_confirmation_epochs_touched "
            "ON agent_confirmation_epochs(touched_at)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_confirmations("
            "confirmation_id TEXT PRIMARY KEY,owner_digest TEXT NOT NULL,"
            "tool_name TEXT NOT NULL,arguments_json TEXT NOT NULL DEFAULT '{}',"
            "context_fingerprint TEXT NOT NULL DEFAULT '',expires_at REAL NOT NULL,"
            "owner_generation INTEGER NOT NULL CHECK(owner_generation>0),"
            "followup_context_json TEXT NOT NULL DEFAULT '{}',"
            "confirmation_contract_json TEXT NOT NULL DEFAULT '{}',"
            "created_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_confirmations_owner_expiry "
            "ON agent_confirmations(owner_digest,expires_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_confirmations_expiry "
            "ON agent_confirmations(expires_at)"
        )

    @staticmethod
    def _owner_digest(owner: str) -> str:
        import hashlib
        import hmac

        from app.modules.web_secret import get_web_secret

        return hmac.new(
            get_web_secret().encode("utf-8"),
            b"mediaflux-agent-confirmation:v1\0" + owner.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _timestamp() -> str:
        from datetime import datetime

        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _json_object(value: dict[str, Any] | None) -> str:
        import json

        return json.dumps(deepcopy(value or {}), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _load_json_object(value: Any) -> dict[str, Any]:
        import json

        try:
            decoded = json.loads(str(value or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return deepcopy(decoded) if isinstance(decoded, dict) else {}

    def issue(
        self,
        *,
        owner: str,
        tool_name: str,
        arguments: dict[str, Any],
        context_fingerprint: str = "",
        followup_context: dict[str, Any] | None = None,
        confirmation_contract: dict[str, Any] | None = None,
        expected_owner_generation: int | None = None,
    ) -> ConfirmationTicket:
        from app import database as db
        import sqlite3

        owner_key = str(owner or "").strip()
        if not owner_key:
            raise AgentToolError("当前会话无法创建确认请求", code="confirmation_invalid")
        owner_digest = self._owner_digest(owner_key)
        now = self._clock()
        expires_at = now + self.ttl_seconds
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            self._prune(conn, now)
            owner_generation = self._owner_generation(
                conn, owner_digest, now=now, touch=True
            )
            if (
                expected_owner_generation is not None
                and int(expected_owner_generation) != owner_generation
            ):
                raise AgentToolError(
                    "会话已重置，请重新生成确认请求",
                    code="confirmation_invalid",
                )
            count = int(conn.execute(
                "SELECT COUNT(*) FROM agent_confirmations"
            ).fetchone()[0] or 0)
            overflow = max(0, count - (self.max_entries - 1))
            if overflow > 0:
                conn.execute(
                    "DELETE FROM agent_confirmations WHERE confirmation_id IN ("
                    "SELECT confirmation_id FROM agent_confirmations "
                    "ORDER BY expires_at ASC, created_at ASC LIMIT ?)",
                    (overflow,),
                )
            confirmation_id = ""
            for _ in range(8):
                candidate = str(self._token_factory() or "").strip()
                if not candidate:
                    continue
                try:
                    conn.execute(
                        "INSERT INTO agent_confirmations("
                        "confirmation_id,owner_digest,tool_name,arguments_json,"
                        "context_fingerprint,expires_at,owner_generation,"
                        "followup_context_json,confirmation_contract_json,created_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            candidate,
                            owner_digest,
                            str(tool_name or "").strip(),
                            self._json_object(arguments),
                            str(context_fingerprint or ""),
                            expires_at,
                            owner_generation,
                            self._json_object(followup_context),
                            self._json_object(confirmation_contract),
                            self._timestamp(),
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                confirmation_id = candidate
                break
            if not confirmation_id:
                raise AgentToolError(
                    "暂时无法创建确认请求", code="confirmation_unavailable"
                )
        return ConfirmationTicket(
            confirmation_id=confirmation_id,
            owner=owner_key,
            tool_name=str(tool_name or "").strip(),
            arguments=deepcopy(arguments),
            context_fingerprint=str(context_fingerprint or ""),
            expires_at=expires_at,
            owner_generation=owner_generation,
            followup_context=deepcopy(followup_context or {}),
            confirmation_contract=deepcopy(confirmation_contract or {}),
        )

    def claim(self, *, owner: str, confirmation_id: str) -> ConfirmationTicket:
        from app import database as db

        owner_key = str(owner or "").strip()
        ticket_id = str(confirmation_id or "").strip()
        if not owner_key or not ticket_id:
            raise AgentToolError("确认请求无效或已过期", code="confirmation_invalid")
        owner_digest = self._owner_digest(owner_key)
        now = self._clock()
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            self._prune(conn, now)
            row = conn.execute(
                "SELECT confirmation_id,tool_name,arguments_json,context_fingerprint,"
                "expires_at,owner_generation,followup_context_json,"
                "confirmation_contract_json FROM agent_confirmations "
                "WHERE confirmation_id=? AND owner_digest=?",
                (ticket_id, owner_digest),
            ).fetchone()
            epoch = conn.execute(
                "SELECT generation FROM agent_confirmation_epochs WHERE owner_digest=?",
                (owner_digest,),
            ).fetchone()
            if (
                row is None
                or epoch is None
                or float(row["expires_at"]) <= now
                or int(row["owner_generation"]) != int(epoch["generation"])
            ):
                raise AgentToolError(
                    "确认请求无效或已过期", code="confirmation_invalid"
                )
            deleted = conn.execute(
                "DELETE FROM agent_confirmations WHERE confirmation_id=? AND owner_digest=?",
                (ticket_id, owner_digest),
            )
            if deleted.rowcount != 1:
                raise AgentToolError(
                    "确认请求无效或已过期", code="confirmation_invalid"
                )
            conn.execute(
                "UPDATE agent_confirmation_epochs SET touched_at=?,updated_at=? "
                "WHERE owner_digest=?",
                (now, self._timestamp(), owner_digest),
            )
        return ConfirmationTicket(
            confirmation_id=str(row["confirmation_id"]),
            owner=owner_key,
            tool_name=str(row["tool_name"]),
            arguments=self._load_json_object(row["arguments_json"]),
            context_fingerprint=str(row["context_fingerprint"] or ""),
            expires_at=float(row["expires_at"]),
            owner_generation=int(row["owner_generation"]),
            followup_context=self._load_json_object(row["followup_context_json"]),
            confirmation_contract=self._load_json_object(
                row["confirmation_contract_json"]
            ),
        )

    def list_active_tickets(self, *, owner: str) -> list[ConfirmationTicket]:
        """跨 Worker 查询 owner 当前世代的有效票据快照。"""
        from app import database as db

        owner_key = str(owner or "").strip()
        if not owner_key:
            return []
        owner_digest = self._owner_digest(owner_key)
        now = self._clock()
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            self._prune(conn, now)
            epoch = conn.execute(
                "SELECT generation FROM agent_confirmation_epochs WHERE owner_digest=?",
                (owner_digest,),
            ).fetchone()
            if epoch is None:
                return []
            rows = conn.execute(
                "SELECT confirmation_id,tool_name,arguments_json,context_fingerprint,"
                "expires_at,owner_generation,followup_context_json,"
                "confirmation_contract_json FROM agent_confirmations "
                "WHERE owner_digest=? AND owner_generation=? AND expires_at>? "
                "ORDER BY expires_at,confirmation_id",
                (owner_digest, int(epoch["generation"]), now),
            ).fetchall()
            conn.execute(
                "UPDATE agent_confirmation_epochs SET touched_at=?,updated_at=? "
                "WHERE owner_digest=?",
                (now, self._timestamp(), owner_digest),
            )
        return [
            ConfirmationTicket(
                confirmation_id=str(row["confirmation_id"]),
                owner=owner_key,
                tool_name=str(row["tool_name"]),
                arguments=self._load_json_object(row["arguments_json"]),
                context_fingerprint=str(row["context_fingerprint"] or ""),
                expires_at=float(row["expires_at"]),
                owner_generation=int(row["owner_generation"]),
                followup_context=self._load_json_object(row["followup_context_json"]),
                confirmation_contract=self._load_json_object(
                    row["confirmation_contract_json"]
                ),
            )
            for row in rows
        ]

    def discard(self, *, owner: str, confirmation_id: str) -> bool:
        from app import database as db

        owner_key = str(owner or "").strip()
        ticket_id = str(confirmation_id or "").strip()
        if not owner_key or not ticket_id:
            return False
        owner_digest = self._owner_digest(owner_key)
        now = self._clock()
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            self._prune(conn, now)
            deleted = conn.execute(
                "DELETE FROM agent_confirmations WHERE confirmation_id=? AND owner_digest=?",
                (ticket_id, owner_digest),
            )
            return deleted.rowcount == 1

    def rotate_owner(self, *, owner: str) -> tuple[int, int]:
        from app import database as db

        owner_key = str(owner or "").strip()
        if not owner_key:
            raise AgentToolError("当前会话无法创建确认请求", code="confirmation_invalid")
        owner_digest = self._owner_digest(owner_key)
        now = self._clock()
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            self._prune(conn, now)
            generation = self._new_owner_generation(conn)
            conn.execute(
                "INSERT INTO agent_confirmation_epochs("
                "owner_digest,generation,touched_at,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(owner_digest) DO UPDATE SET "
                "generation=excluded.generation,touched_at=excluded.touched_at,"
                "updated_at=excluded.updated_at",
                (owner_digest, generation, now, self._timestamp()),
            )
            deleted = conn.execute(
                "DELETE FROM agent_confirmations WHERE owner_digest=?", (owner_digest,)
            )
            return max(0, int(deleted.rowcount)), generation

    def revoke_owner(self, *, owner: str) -> int:
        owner_key = str(owner or "").strip()
        if not owner_key:
            return 0
        revoked, _generation = self.rotate_owner(owner=owner_key)
        return revoked

    def owner_generation(self, *, owner: str) -> int:
        from app import database as db

        owner_key = str(owner or "").strip()
        if not owner_key:
            raise AgentToolError("当前会话无法创建确认请求", code="confirmation_invalid")
        owner_digest = self._owner_digest(owner_key)
        now = self._clock()
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            self._prune(conn, now)
            return self._owner_generation(conn, owner_digest, now=now, touch=True)

    def reset(self) -> None:
        from app import database as db

        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            conn.execute("DELETE FROM agent_confirmations")
            conn.execute("DELETE FROM agent_confirmation_epochs")

    def _prune(self, conn: Any, now: float) -> None:
        conn.execute("DELETE FROM agent_confirmations WHERE expires_at<=?", (now,))
        cutoff = now - (self.ttl_seconds * 2)
        conn.execute(
            "DELETE FROM agent_confirmation_epochs WHERE touched_at<=? AND "
            "NOT EXISTS(SELECT 1 FROM agent_confirmations c "
            "WHERE c.owner_digest=agent_confirmation_epochs.owner_digest)",
            (cutoff,),
        )
        max_epochs = max(32, self.max_entries * 4)
        count = int(conn.execute(
            "SELECT COUNT(*) FROM agent_confirmation_epochs"
        ).fetchone()[0] or 0)
        overflow = count - max_epochs
        if overflow > 0:
            conn.execute(
                "DELETE FROM agent_confirmation_epochs WHERE owner_digest IN ("
                "SELECT e.owner_digest FROM agent_confirmation_epochs e "
                "WHERE NOT EXISTS(SELECT 1 FROM agent_confirmations c "
                "WHERE c.owner_digest=e.owner_digest) "
                "ORDER BY e.touched_at ASC LIMIT ?)",
                (overflow,),
            )

    def _owner_generation(
        self, conn: Any, owner_digest: str, *, now: float, touch: bool
    ) -> int:
        row = conn.execute(
            "SELECT generation,touched_at FROM agent_confirmation_epochs "
            "WHERE owner_digest=?",
            (owner_digest,),
        ).fetchone()
        if row is None:
            generation = self._new_owner_generation(conn)
            conn.execute(
                "INSERT INTO agent_confirmation_epochs("
                "owner_digest,generation,touched_at,updated_at) VALUES(?,?,?,?)",
                (owner_digest, generation, now, self._timestamp()),
            )
            return generation
        generation = int(row["generation"])
        if touch:
            conn.execute(
                "UPDATE agent_confirmation_epochs SET touched_at=?,updated_at=? "
                "WHERE owner_digest=?",
                (now, self._timestamp(), owner_digest),
            )
        return generation

    @staticmethod
    def _new_owner_generation(conn: Any) -> int:
        for _ in range(8):
            generation = secrets.randbits(63) or 1
            exists = conn.execute(
                "SELECT 1 FROM agent_confirmation_epochs WHERE generation=? LIMIT 1",
                (generation,),
            ).fetchone()
            if exists is None:
                return generation
        raise AgentToolError(
            "暂时无法创建确认请求", code="confirmation_unavailable"
        )
