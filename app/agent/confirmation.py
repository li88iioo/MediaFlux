"""服务端一次性确认票据。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import secrets
import threading
import time
import unicodedata
from typing import Any, Callable

from app.agent.action_plan_id import normalize_action_plan_id
from app.agent.registry import AgentToolError
from app.modules.web_secret import get_web_secret


def confirmation_context_fingerprint(value: Any, *, domain: str) -> str:
    """生成部署密钥绑定的确认上下文指纹，避免敏感快照形成离线校验 oracle。"""
    if not isinstance(domain, str):
        raise ValueError("确认上下文指纹域无效")
    normalized_domain = domain.strip().casefold()
    if not normalized_domain or len(normalized_domain) > 80:
        raise ValueError("确认上下文指纹域无效")
    try:
        domain_bytes = normalized_domain.encode("ascii")
        payload_text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        payload = payload_text.encode("utf-8")
    except (
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
        UnicodeError,
    ) as exc:
        raise ValueError("确认上下文无法稳定序列化") from exc
    message = (
        b"mediaflux-agent-confirmation-context:v1\0"
        + domain_bytes
        + b"\0"
        + payload
    )
    return hmac.new(
        get_web_secret().encode("utf-8"), message, hashlib.sha256
    ).hexdigest()


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


def _normalize_expected_owner_generation(value: Any) -> int | None:
    """严格校验查询签发时携带的 owner epoch。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AgentToolError(
            "确认请求代次无效，请重新生成确认请求",
            code="confirmation_invalid",
        )
    return value


def _stored_owner_generation(value: Any) -> int:
    """读取 SQLite owner epoch；拒绝 SQLite 对文本/浮点的宽松强制转换。"""
    if type(value) is not int or value <= 0:
        raise ValueError("invalid stored owner generation")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _snapshot_json_object(
    value: Any,
    *,
    allow_none: bool = False,
) -> tuple[dict[str, Any], str]:
    """生成严格 JSON 对象快照，统一内存与 SQLite 的确认载荷语义。"""
    candidate = {} if value is None and allow_none else value
    if not isinstance(candidate, dict):
        raise AgentToolError(
            "确认请求内容无效，请重新生成确认请求",
            code="confirmation_invalid",
        )
    try:
        encoded = json.dumps(
            candidate,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        # sqlite3 绑定文本时同样要求合法 UTF-8；提前校验可保证失败发生在
        # 任何容量淘汰或 owner epoch 变更之前。
        encoded.encode("utf-8")
        decoded = json.loads(encoded, parse_constant=_reject_json_constant)
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError) as exc:
        raise AgentToolError(
            "确认请求内容无效，请重新生成确认请求",
            code="confirmation_invalid",
        ) from exc
    if not isinstance(decoded, dict):
        raise AgentToolError(
            "确认请求内容无效，请重新生成确认请求",
            code="confirmation_invalid",
        )
    return decoded, encoded


def _copy_ticket(
    ticket: ConfirmationTicket,
    *,
    owner_generation: int | None = None,
) -> ConfirmationTicket:
    """返回不与存储内部嵌套对象共享引用的票据快照。"""
    return ConfirmationTicket(
        confirmation_id=ticket.confirmation_id,
        owner=ticket.owner,
        tool_name=ticket.tool_name,
        arguments=deepcopy(ticket.arguments),
        context_fingerprint=ticket.context_fingerprint,
        expires_at=ticket.expires_at,
        owner_generation=(
            ticket.owner_generation
            if owner_generation is None
            else owner_generation
        ),
        followup_context=deepcopy(ticket.followup_context),
        confirmation_contract=deepcopy(ticket.confirmation_contract),
    )


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
        replace_active_ticket: bool = False,
    ) -> ConfirmationTicket:
        owner_key = str(owner or "").strip()
        if not owner_key:
            raise AgentToolError("当前会话无法创建确认请求", code="confirmation_invalid")
        normalized_tool_name = str(tool_name or "").strip()
        if not normalized_tool_name:
            raise AgentToolError(
                "确认请求内容无效，请重新生成确认请求",
                code="confirmation_invalid",
            )
        normalized_arguments, _arguments_json = _snapshot_json_object(arguments)
        normalized_followup, _followup_json = _snapshot_json_object(
            followup_context, allow_none=True
        )
        normalized_contract, _contract_json = _snapshot_json_object(
            confirmation_contract, allow_none=True
        )
        expected_generation = _normalize_expected_owner_generation(
            expected_owner_generation
        )
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            owner_generation = self._owner_generation_locked(owner_key, now=now, touch=True)
            if (
                expected_generation is not None
                and expected_generation != owner_generation
            ):
                raise AgentToolError(
                    "会话已重置，请重新生成确认请求",
                    code="confirmation_invalid",
                )
            # 先生成唯一 ID，再执行容量淘汰；令牌源异常时必须原样保留
            # 已存在票据，不能让一次失败的签发产生旁路状态变化。
            confirmation_id = self._new_unique_id_locked()
            active_ticket_ids = [
                key
                for key, ticket in self._tickets.items()
                if ticket.owner_generation == owner_generation
                and secrets.compare_digest(ticket.owner, owner_key)
            ]
            replaced_count = len(active_ticket_ids) if replace_active_ticket else 0
            protected_ticket_ids = (
                frozenset(active_ticket_ids) if replace_active_ticket else frozenset()
            )
            overflow = max(
                0, len(self._tickets) - replaced_count + 1 - self.max_entries
            )
            while overflow > 0:
                removable_ids = [
                    key for key in self._tickets if key not in protected_ticket_ids
                ]
                if not removable_ids:
                    break
                oldest_id = min(
                    removable_ids,
                    key=lambda key: self._tickets[key].expires_at,
                )
                self._tickets.pop(oldest_id, None)
                overflow -= 1
            if replace_active_ticket:
                for key in active_ticket_ids:
                    self._tickets.pop(key, None)
            ticket = ConfirmationTicket(
                confirmation_id=confirmation_id,
                owner=owner_key,
                tool_name=normalized_tool_name,
                arguments=normalized_arguments,
                context_fingerprint=str(context_fingerprint or ""),
                expires_at=now + self.ttl_seconds,
                owner_generation=owner_generation,
                followup_context=normalized_followup,
                confirmation_contract=normalized_contract,
            )
            self._tickets[confirmation_id] = ticket
            return _copy_ticket(ticket)

    def ticket_owner_match(
        self, *, owner: str, confirmation_id: str
    ) -> bool | None:
        """判断有效票据是否属于 owner；不存在/过期返回 ``None``，且不消费。"""
        owner_key = str(owner or "").strip()
        ticket_id = normalize_action_plan_id(confirmation_id)
        if not owner_key or not ticket_id:
            return None
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                return None
            current = self._owner_generations.get(ticket.owner)
            if current is None or ticket.owner_generation != current[0]:
                return None
            return secrets.compare_digest(ticket.owner, owner_key)

    def claim_and_rotate_owner(
        self, *, owner: str, confirmation_id: str, record_execution: bool = False,
        execution_risk_for: Callable[[str], Any] | None = None,
    ) -> ConfirmationTicket:
        """原子领取一张票据并推进 owner epoch，撤销同会话其余票据。"""
        owner_key = str(owner or "").strip()
        ticket_id = normalize_action_plan_id(confirmation_id)
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
            # 先确保 epoch 能推进，再写入执行审计；否则随机源异常会留下
            # “已领取”记录，但票据仍可重试。
            generation = self._new_owner_generation_locked()
            if record_execution:
                from app.agent.action_history import record_confirmation_claimed
                from app.agent.models import RiskLevel

                record_confirmation_claimed(
                    owner=owner_key,
                    confirmation_id=ticket.confirmation_id,
                    owner_generation=ticket.owner_generation,
                    tool_name=ticket.tool_name,
                    risk=(
                        execution_risk_for(ticket.tool_name)
                        if execution_risk_for is not None
                        else RiskLevel.WRITE
                    ),
                    confirmation_contract=ticket.confirmation_contract,
                )
            self._owner_generations[owner_key] = (generation, now)
            for key, active in list(self._tickets.items()):
                if secrets.compare_digest(active.owner, owner_key):
                    self._tickets.pop(key, None)
            return _copy_ticket(ticket)

    def list_active_tickets(self, *, owner: str) -> list[ConfirmationTicket]:
        """返回 owner 当前世代的有效票据快照，不消费票据。"""
        owner_key = str(owner or "").strip()
        if not owner_key:
            return []
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            current = self._owner_generations.get(owner_key)
            if current is None:
                return []
            generation, _touched_at = current
            self._owner_generations[owner_key] = (generation, now)
            tickets = [
                ticket
                for ticket in self._tickets.values()
                if ticket.owner_generation == generation
                and secrets.compare_digest(ticket.owner, owner_key)
                and ticket.expires_at > now
            ]
            tickets.sort(key=lambda item: (item.expires_at, item.confirmation_id))
            return [_copy_ticket(ticket) for ticket in tickets]

    def discard(self, *, owner: str, confirmation_id: str) -> bool:
        """撤销属于指定会话且尚未消费的确认票据。"""
        owner_key = str(owner or "").strip()
        ticket_id = normalize_action_plan_id(confirmation_id)
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            ticket = self._tickets.get(ticket_id)
            if ticket is None or not owner_key or not secrets.compare_digest(ticket.owner, owner_key):
                return False
            self._tickets.pop(ticket_id, None)
            return True

    def rotate_owner(
        self, *, owner: str, preserve_active: bool = False
    ) -> tuple[int, int]:
        """推进 owner epoch。默认撤销票据；查询抢占时可保留当前有效票据。"""
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
            if preserve_active:
                for key in ticket_ids:
                    self._tickets[key] = _copy_ticket(
                        self._tickets[key], owner_generation=generation
                    )
                return 0, generation
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
            token = normalize_action_plan_id(self._token_factory())
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
    def _load_json_object(value: Any) -> dict[str, Any]:
        if not isinstance(value, str):
            raise AgentToolError(
                "确认请求内容无效，请重新生成确认请求",
                code="confirmation_invalid",
            )
        try:
            value.encode("utf-8")
            decoded = json.loads(value, parse_constant=_reject_json_constant)
        except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
            raise AgentToolError(
                "确认请求内容无效，请重新生成确认请求",
                code="confirmation_invalid",
            ) from exc
        if not isinstance(decoded, dict):
            raise AgentToolError(
                "确认请求内容无效，请重新生成确认请求",
                code="confirmation_invalid",
            )
        return decoded

    @classmethod
    def _ticket_from_row(
        cls,
        row: Any,
        *,
        owner: str,
    ) -> ConfirmationTicket:
        try:
            raw_confirmation_id = row["confirmation_id"]
            confirmation_id = normalize_action_plan_id(raw_confirmation_id)
            tool_name = str(row["tool_name"] or "").strip()
            expires_at = float(row["expires_at"])
            owner_generation = _stored_owner_generation(row["owner_generation"])
        except (KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
            raise AgentToolError(
                "确认请求内容无效，请重新生成确认请求",
                code="confirmation_invalid",
            ) from exc
        if (
            not confirmation_id
            or confirmation_id != raw_confirmation_id
            or not tool_name
            or not math.isfinite(expires_at)
            or owner_generation <= 0
        ):
            raise AgentToolError(
                "确认请求内容无效，请重新生成确认请求",
                code="confirmation_invalid",
            )
        return ConfirmationTicket(
            confirmation_id=confirmation_id,
            owner=owner,
            tool_name=tool_name,
            arguments=cls._load_json_object(row["arguments_json"]),
            context_fingerprint=str(row["context_fingerprint"] or ""),
            expires_at=expires_at,
            owner_generation=owner_generation,
            followup_context=cls._load_json_object(row["followup_context_json"]),
            confirmation_contract=cls._load_json_object(
                row["confirmation_contract_json"]
            ),
        )

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
        replace_active_ticket: bool = False,
    ) -> ConfirmationTicket:
        from app import database as db
        import sqlite3

        owner_key = str(owner or "").strip()
        if not owner_key:
            raise AgentToolError("当前会话无法创建确认请求", code="confirmation_invalid")
        normalized_tool_name = str(tool_name or "").strip()
        if not normalized_tool_name:
            raise AgentToolError(
                "确认请求内容无效，请重新生成确认请求",
                code="confirmation_invalid",
            )
        normalized_arguments, arguments_json = _snapshot_json_object(arguments)
        normalized_followup, followup_json = _snapshot_json_object(
            followup_context, allow_none=True
        )
        normalized_contract, contract_json = _snapshot_json_object(
            confirmation_contract, allow_none=True
        )
        expected_generation = _normalize_expected_owner_generation(
            expected_owner_generation
        )
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
                expected_generation is not None
                and expected_generation != owner_generation
            ):
                raise AgentToolError(
                    "会话已重置，请重新生成确认请求",
                    code="confirmation_invalid",
                )
            count = int(conn.execute(
                "SELECT COUNT(*) FROM agent_confirmations"
            ).fetchone()[0] or 0)
            replaced_count = 0
            if replace_active_ticket:
                replaced_count = int(conn.execute(
                    "SELECT COUNT(*) FROM agent_confirmations "
                    "WHERE owner_digest=? AND owner_generation=?",
                    (owner_digest, owner_generation),
                ).fetchone()[0] or 0)
            overflow = max(
                0, count - replaced_count + 1 - self.max_entries
            )
            if overflow > 0:
                if replace_active_ticket:
                    conn.execute(
                        "DELETE FROM agent_confirmations WHERE confirmation_id IN ("
                        "SELECT confirmation_id FROM agent_confirmations "
                        "WHERE NOT (owner_digest=? AND owner_generation=?) "
                        "ORDER BY expires_at ASC, created_at ASC LIMIT ?)",
                        (owner_digest, owner_generation, overflow),
                    )
                else:
                    conn.execute(
                        "DELETE FROM agent_confirmations WHERE confirmation_id IN ("
                        "SELECT confirmation_id FROM agent_confirmations "
                        "ORDER BY expires_at ASC, created_at ASC LIMIT ?)",
                        (overflow,),
                    )
            confirmation_id = ""
            for _ in range(8):
                candidate = normalize_action_plan_id(self._token_factory())
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
                            normalized_tool_name,
                            arguments_json,
                            str(context_fingerprint or ""),
                            expires_at,
                            owner_generation,
                            followup_json,
                            contract_json,
                            self._timestamp(),
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                confirmation_id = candidate
                if replace_active_ticket:
                    conn.execute(
                        "DELETE FROM agent_confirmations "
                        "WHERE owner_digest=? AND owner_generation=? "
                        "AND confirmation_id<>?",
                        (owner_digest, owner_generation, confirmation_id),
                    )
                break
            if not confirmation_id:
                raise AgentToolError(
                    "暂时无法创建确认请求", code="confirmation_unavailable"
                )
        return ConfirmationTicket(
            confirmation_id=confirmation_id,
            owner=owner_key,
            tool_name=normalized_tool_name,
            arguments=normalized_arguments,
            context_fingerprint=str(context_fingerprint or ""),
            expires_at=expires_at,
            owner_generation=owner_generation,
            followup_context=normalized_followup,
            confirmation_contract=normalized_contract,
        )

    def ticket_owner_match(
        self, *, owner: str, confirmation_id: str
    ) -> bool | None:
        """跨 Worker 判断有效票据归属，不读取动作内容也不消费票据。"""
        from app import database as db

        owner_key = str(owner or "").strip()
        ticket_id = normalize_action_plan_id(confirmation_id)
        if not owner_key or not ticket_id:
            return None
        requested_digest = self._owner_digest(owner_key)
        now = self._clock()
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            self._prune(conn, now)
            row = conn.execute(
                "SELECT owner_digest,owner_generation,expires_at "
                "FROM agent_confirmations WHERE confirmation_id=?",
                (ticket_id,),
            ).fetchone()
            if row is None:
                return None
            try:
                ticket_digest = str(row["owner_digest"] or "")
                ticket_generation = _stored_owner_generation(
                    row["owner_generation"]
                )
                expires_at = float(row["expires_at"])
            except (KeyError, IndexError, TypeError, ValueError, OverflowError):
                return None
            epoch = conn.execute(
                "SELECT generation FROM agent_confirmation_epochs "
                "WHERE owner_digest=?",
                (ticket_digest,),
            ).fetchone()
            if epoch is None or expires_at <= now:
                return None
            try:
                epoch_generation = _stored_owner_generation(epoch["generation"])
            except (KeyError, IndexError, TypeError, ValueError, OverflowError):
                return None
            if ticket_generation != epoch_generation:
                return None
            return secrets.compare_digest(ticket_digest, requested_digest)

    def claim_and_rotate_owner(
        self, *, owner: str, confirmation_id: str, record_execution: bool = False,
        execution_risk_for: Callable[[str], Any] | None = None,
    ) -> ConfirmationTicket:
        """在同一 SQLite 事务中领取票据、推进 epoch 并撤销同 owner 票据。"""
        from app import database as db

        owner_key = str(owner or "").strip()
        ticket_id = normalize_action_plan_id(confirmation_id)
        if not owner_key or not ticket_id:
            raise AgentToolError("确认请求无效或已过期", code="confirmation_invalid")
        owner_digest = self._owner_digest(owner_key)
        now = self._clock()
        claimed_ticket: ConfirmationTicket | None = None
        invalid_payload = False
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
            if row is None or epoch is None:
                raise AgentToolError(
                    "确认请求无效或已过期", code="confirmation_invalid"
                )
            try:
                claimed_ticket = self._ticket_from_row(row, owner=owner_key)
                epoch_generation = _stored_owner_generation(epoch["generation"])
            except (AgentToolError, TypeError, ValueError, OverflowError):
                invalid_payload = True
            if not invalid_payload and claimed_ticket is not None and (
                claimed_ticket.expires_at <= now
                or claimed_ticket.owner_generation != epoch_generation
            ):
                raise AgentToolError(
                    "确认请求无效或已过期", code="confirmation_invalid"
                )
            if record_execution and claimed_ticket is not None and not invalid_payload:
                from app.agent.action_history import record_confirmation_claimed
                from app.agent.models import RiskLevel

                tool_name = claimed_ticket.tool_name
                record_confirmation_claimed(
                    owner=owner_key,
                    confirmation_id=claimed_ticket.confirmation_id,
                    owner_generation=claimed_ticket.owner_generation,
                    tool_name=tool_name,
                    risk=(
                        execution_risk_for(tool_name)
                        if execution_risk_for is not None
                        else RiskLevel.WRITE
                    ),
                    confirmation_contract=claimed_ticket.confirmation_contract,
                    connection=conn,
                )
            self._rotate_owner_state(
                conn,
                owner_digest=owner_digest,
                now=now,
                preserve_active=False,
            )
        if invalid_payload or claimed_ticket is None:
            raise AgentToolError(
                "确认请求内容无效，请重新生成确认请求",
                code="confirmation_invalid",
            )
        return claimed_ticket

    def list_active_tickets(self, *, owner: str) -> list[ConfirmationTicket]:
        """跨 Worker 查询 owner 当前世代的有效票据快照。"""
        from app import database as db

        owner_key = str(owner or "").strip()
        if not owner_key:
            return []
        owner_digest = self._owner_digest(owner_key)
        now = self._clock()
        tickets: list[ConfirmationTicket] = []
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
            try:
                epoch_generation = _stored_owner_generation(epoch["generation"])
            except (TypeError, ValueError, OverflowError):
                self._rotate_owner_state(
                    conn,
                    owner_digest=owner_digest,
                    now=now,
                    preserve_active=False,
                )
                return []
            rows = conn.execute(
                "SELECT confirmation_id,tool_name,arguments_json,context_fingerprint,"
                "expires_at,owner_generation,followup_context_json,"
                "confirmation_contract_json FROM agent_confirmations "
                "WHERE owner_digest=? AND owner_generation=? AND expires_at>? "
                "ORDER BY expires_at,confirmation_id",
                (owner_digest, epoch_generation, now),
            ).fetchall()
            invalid_payload = False
            for row in rows:
                try:
                    tickets.append(self._ticket_from_row(row, owner=owner_key))
                except AgentToolError:
                    invalid_payload = True
                    break
            if invalid_payload:
                self._rotate_owner_state(
                    conn,
                    owner_digest=owner_digest,
                    now=now,
                    preserve_active=False,
                )
                tickets.clear()
            else:
                conn.execute(
                    "UPDATE agent_confirmation_epochs SET touched_at=?,updated_at=? "
                    "WHERE owner_digest=?",
                    (now, self._timestamp(), owner_digest),
                )
        return tickets

    def discard(self, *, owner: str, confirmation_id: str) -> bool:
        from app import database as db

        owner_key = str(owner or "").strip()
        ticket_id = normalize_action_plan_id(confirmation_id)
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

    def rotate_owner(
        self, *, owner: str, preserve_active: bool = False
    ) -> tuple[int, int]:
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
            return self._rotate_owner_state(
                conn,
                owner_digest=owner_digest,
                now=now,
                preserve_active=preserve_active,
            )

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
        try:
            generation = _stored_owner_generation(row["generation"])
        except ValueError:
            _revoked, generation = self._rotate_owner_state(
                conn,
                owner_digest=owner_digest,
                now=now,
                preserve_active=False,
            )
            return generation
        if touch:
            conn.execute(
                "UPDATE agent_confirmation_epochs SET touched_at=?,updated_at=? "
                "WHERE owner_digest=?",
                (now, self._timestamp(), owner_digest),
            )
        return generation

    def _rotate_owner_state(
        self,
        conn: Any,
        *,
        owner_digest: str,
        now: float,
        preserve_active: bool,
    ) -> tuple[int, int]:
        """在当前事务内推进 owner epoch，并统一处理票据保留或撤销。"""
        generation = self._new_owner_generation(conn)
        conn.execute(
            "INSERT INTO agent_confirmation_epochs("
            "owner_digest,generation,touched_at,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(owner_digest) DO UPDATE SET "
            "generation=excluded.generation,touched_at=excluded.touched_at,"
            "updated_at=excluded.updated_at",
            (owner_digest, generation, now, self._timestamp()),
        )
        if preserve_active:
            conn.execute(
                "UPDATE agent_confirmations SET owner_generation=? "
                "WHERE owner_digest=? AND expires_at>?",
                (generation, owner_digest, now),
            )
            return 0, generation
        deleted = conn.execute(
            "DELETE FROM agent_confirmations WHERE owner_digest=?", (owner_digest,)
        )
        return max(0, int(deleted.rowcount)), generation

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
