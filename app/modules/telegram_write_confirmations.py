"""Telegram 旧命令写操作的一次性、会话绑定确认票据。"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable


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


_store = TelegramWriteConfirmationStore()


def get_telegram_write_confirmation_store() -> TelegramWriteConfirmationStore:
    return _store


def reset_telegram_write_confirmation_store_for_tests() -> None:
    global _store
    _store = TelegramWriteConfirmationStore()
