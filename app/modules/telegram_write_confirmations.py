"""Telegram 旧命令写操作的一次性、会话绑定确认票据。"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from app import database as db
from app.modules.web_secret import get_web_secret


class TelegramWriteConfirmationError(ValueError):
    """确认票据无效、过期或 owner 不匹配。"""


@dataclass(frozen=True)
class _Action:
    action_id: str
    group_id: str
    chat_id: str
    user_id: str
    decision: str
    operation: str
    value: dict[str, Any]
    expires_at: float


class TelegramWriteConfirmationStore:
    def __init__(
        self,
        *,
        ttl_seconds: int = 300,
        max_actions: int = 1024,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if ttl_seconds <= 0 or max_actions <= 1:
            raise ValueError("confirmation store limits must be positive")
        self.ttl_seconds = int(ttl_seconds)
        self.max_actions = int(max_actions)
        self._clock = clock or time.monotonic
        self._actions: dict[str, _Action] = {}
        self._lock = threading.RLock()

    def create_group(
        self,
        *,
        chat_id: str,
        user_id: str,
        operation: str,
        actions: list[tuple[str, dict[str, Any]]],
    ) -> tuple[str, ...]:
        """创建同组互斥票据；任一票据被消费后整组立即失效。"""
        normalized = [
            (str(decision), dict(value))
            for decision, value in actions
        ]
        if not normalized:
            raise ValueError("confirmation group must contain at least one action")
        if len(normalized) > self.max_actions:
            raise ValueError("confirmation group exceeds store capacity")

        now = self._clock()
        with self._lock:
            self._prune(now)
            while len(self._actions) + len(normalized) > self.max_actions:
                oldest = min(
                    self._actions.values(),
                    key=lambda action: action.expires_at,
                )
                self._remove_group(oldest.group_id)
            group_id = self._token()
            action_ids: list[str] = []
            for decision, value in normalized:
                action_id = self._token()
                self._actions[action_id] = _Action(
                    action_id=action_id,
                    group_id=group_id,
                    chat_id=str(chat_id),
                    user_id=str(user_id),
                    decision=decision,
                    operation=str(operation),
                    value=value,
                    expires_at=now + self.ttl_seconds,
                )
                action_ids.append(action_id)
            return tuple(action_ids)

    def create_pair(
        self,
        *,
        chat_id: str,
        user_id: str,
        operation: str,
        value: dict[str, Any],
    ) -> tuple[str, str]:
        confirm_id, cancel_id = self.create_group(
            chat_id=chat_id,
            user_id=user_id,
            operation=operation,
            actions=[("confirm", value), ("cancel", value)],
        )
        return confirm_id, cancel_id

    def claim(self, action_id: str, *, chat_id: str, user_id: str) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._prune(now)
            action = self._actions.get(str(action_id))
            if action is None:
                raise TelegramWriteConfirmationError("确认已过期或已处理，请重新发起")
            if action.chat_id != str(chat_id) or action.user_id != str(user_id):
                raise TelegramWriteConfirmationError("该确认不属于当前会话")
            self._remove_group(action.group_id)
            return {
                "decision": action.decision,
                "operation": action.operation,
                "value": dict(action.value),
            }

    def _remove_group(self, group_id: str) -> None:
        for action_id, action in tuple(self._actions.items()):
            if action.group_id == group_id:
                self._actions.pop(action_id, None)

    def _prune(self, now: float) -> None:
        expired_groups = {
            action.group_id
            for action in self._actions.values()
            if action.expires_at <= now
        }
        for group_id in expired_groups:
            self._remove_group(group_id)

    @staticmethod
    def _token() -> str:
        return secrets.token_urlsafe(9).rstrip("=")


class SQLiteTelegramWriteConfirmationStore(TelegramWriteConfirmationStore):
    """SQLite-backed Telegram 旧命令确认票据，支持跨进程原子消费。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = 300,
        max_actions: int = 1024,
        clock: Callable[[], float] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(
            ttl_seconds=ttl_seconds,
            max_actions=max_actions,
            clock=clock or time.time,
        )
        self._token_factory = token_factory or (
            lambda: secrets.token_urlsafe(9).rstrip("=")
        )

    @staticmethod
    def _ensure_schema(conn: Any) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS telegram_write_confirmations("
            "action_id TEXT PRIMARY KEY,group_id TEXT NOT NULL,"
            "owner_digest TEXT NOT NULL,decision TEXT NOT NULL,"
            "operation TEXT NOT NULL,value_json TEXT NOT NULL DEFAULT '{}',"
            "expires_at REAL NOT NULL,created_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telegram_write_confirmations_group "
            "ON telegram_write_confirmations(group_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telegram_write_confirmations_owner_expiry "
            "ON telegram_write_confirmations(owner_digest,expires_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telegram_write_confirmations_expiry "
            "ON telegram_write_confirmations(expires_at)"
        )

    @staticmethod
    def _owner_digest(chat_id: str, user_id: str) -> str:
        owner = f"{chat_id}\x1f{user_id}"
        return hmac.new(
            get_web_secret().encode("utf-8"),
            b"mediaflux-telegram-write-confirmation:v1\0" + owner.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _prune_rows(conn: Any, now: float) -> int:
        cursor = conn.execute(
            "DELETE FROM telegram_write_confirmations WHERE expires_at<=?", (now,)
        )
        return max(0, int(cursor.rowcount or 0))

    def _next_token(self, conn: Any, *, column: str = "action_id") -> str:
        for _ in range(8):
            token = str(self._token_factory() or "").strip()
            if not token or ":" in token or len(token) > 64:
                continue
            if column == "group_id":
                return token
            if conn.execute(
                "SELECT 1 FROM telegram_write_confirmations WHERE action_id=?",
                (token,),
            ).fetchone() is None:
                return token
        raise RuntimeError("无法生成 Telegram 确认标识")

    def create_group(
        self,
        *,
        chat_id: str,
        user_id: str,
        operation: str,
        actions: list[tuple[str, dict[str, Any]]],
    ) -> tuple[str, ...]:
        normalized: list[tuple[str, str]] = []
        operation_name = str(operation or "").strip()
        if not operation_name or len(operation_name) > 80:
            raise ValueError("confirmation operation is invalid")
        for decision, value in actions:
            decision_name = str(decision or "").strip()
            if not decision_name or len(decision_name) > 40 or not isinstance(value, dict):
                raise ValueError("confirmation action is invalid")
            try:
                value_json = json.dumps(
                    dict(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True
                )
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("confirmation action is invalid") from exc
            if len(value_json.encode("utf-8")) > 16 * 1024:
                raise ValueError("confirmation action is too large")
            normalized.append((decision_name, value_json))
        if not normalized:
            raise ValueError("confirmation group must contain at least one action")
        if len(normalized) > self.max_actions:
            raise ValueError("confirmation group exceeds store capacity")

        now = self._clock()
        owner_digest = self._owner_digest(str(chat_id), str(user_id))
        with self._lock, db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            self._prune_rows(conn, now)
            while int(conn.execute(
                "SELECT COUNT(*) FROM telegram_write_confirmations"
            ).fetchone()[0] or 0) + len(normalized) > self.max_actions:
                oldest = conn.execute(
                    "SELECT group_id FROM telegram_write_confirmations "
                    "GROUP BY group_id ORDER BY MIN(expires_at) ASC,MIN(rowid) ASC LIMIT 1"
                ).fetchone()
                if oldest is None:
                    break
                conn.execute(
                    "DELETE FROM telegram_write_confirmations WHERE group_id=?",
                    (oldest["group_id"],),
                )

            group_id = self._next_token(conn, column="group_id")
            action_ids: list[str] = []
            expires_at = now + self.ttl_seconds
            for decision, value_json in normalized:
                inserted = False
                for _ in range(8):
                    action_id = self._next_token(conn)
                    try:
                        conn.execute(
                            "INSERT INTO telegram_write_confirmations("
                            "action_id,group_id,owner_digest,decision,operation,"
                            "value_json,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                            (
                                action_id,
                                group_id,
                                owner_digest,
                                decision,
                                operation_name,
                                value_json,
                                expires_at,
                                db.now(),
                            ),
                        )
                    except sqlite3.IntegrityError:
                        continue
                    action_ids.append(action_id)
                    inserted = True
                    break
                if not inserted:
                    raise RuntimeError("无法生成 Telegram 确认标识")
            return tuple(action_ids)

    def claim(
        self, action_id: str, *, chat_id: str, user_id: str
    ) -> dict[str, Any]:
        key = str(action_id or "").strip()
        now = self._clock()
        expected_owner = self._owner_digest(str(chat_id), str(user_id))
        with self._lock, db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            self._prune_rows(conn, now)
            row = conn.execute(
                "SELECT group_id,owner_digest,decision,operation,value_json,expires_at "
                "FROM telegram_write_confirmations WHERE action_id=?",
                (key,),
            ).fetchone()
            if row is None or float(row["expires_at"]) <= now:
                raise TelegramWriteConfirmationError(
                    "确认已过期或已处理，请重新发起"
                )
            if not secrets.compare_digest(str(row["owner_digest"]), expected_owner):
                raise TelegramWriteConfirmationError("该确认不属于当前会话")
            try:
                value = json.loads(str(row["value_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise TelegramWriteConfirmationError(
                    "确认已过期或已处理，请重新发起"
                ) from exc
            if not isinstance(value, dict):
                raise TelegramWriteConfirmationError(
                    "确认已过期或已处理，请重新发起"
                )
            deleted = conn.execute(
                "DELETE FROM telegram_write_confirmations WHERE group_id=?",
                (str(row["group_id"] or ""),),
            )
            if deleted.rowcount < 1:
                raise TelegramWriteConfirmationError(
                    "确认已过期或已处理，请重新发起"
                )
            return {
                "decision": str(row["decision"] or ""),
                "operation": str(row["operation"] or ""),
                "value": value,
            }


_store: TelegramWriteConfirmationStore = SQLiteTelegramWriteConfirmationStore()


def get_telegram_write_confirmation_store() -> TelegramWriteConfirmationStore:
    return _store


def reset_telegram_write_confirmation_store_for_tests() -> None:
    global _store
    # 测试重置显式回退到进程内实现，避免未隔离的旧测试连接开发数据库。
    _store = TelegramWriteConfirmationStore()
