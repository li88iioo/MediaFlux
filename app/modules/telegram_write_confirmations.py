"""Telegram 写操作对统一 Agent 一次性确认票据的传输适配。"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Callable
from typing import Any

from app.agent.action_plan_id import normalize_action_plan_id
from app.agent.confirmation import ConfirmationStore, SQLiteConfirmationStore
from app.agent.errors import AgentToolError

_TELEGRAM_WRITE_TOOL = "telegram.write_action"
_CALLBACK_ID_RE = re.compile(
    r"(?P<ticket>[A-Za-z0-9_-]{16,56})\.(?P<index>[0-9]{1,4})\Z"
)


class TelegramWriteConfirmationError(ValueError):
    """确认票据无效、过期或 owner 不匹配。"""


def _telegram_owner(chat_id: str, user_id: str) -> str:
    # 延迟导入避免 Telegram handler/adapter 初始化时形成模块环。
    from app.bot.agent_adapter import telegram_agent_owner

    try:
        return telegram_agent_owner(chat_id, user_id)
    except ValueError as exc:
        raise TelegramWriteConfirmationError("当前 Telegram 会话身份无效") from exc


def _bounded_ticket_factory(factory: Callable[[], str]) -> Callable[[], str]:
    """限制 callback ticket 长度，保证 ``tgc:`` 数据不超过 Telegram 64 字节。"""

    def next_token() -> str:
        token = normalize_action_plan_id(factory())
        return token if 16 <= len(token) <= 56 else ""

    return next_token


class TelegramWriteConfirmationStore:
    """把 Telegram 按钮组映射到统一 ConfirmationStore。

    一个按钮组只签发一张 canonical ticket，按钮仅携带该 ticket 的选择索引。
    新组会替换同一 Telegram owner 的旧确认，领取任一按钮后整组以及同会话旧票据
    一并失效，与 Agent 文本确认保持完全相同的一次性/世代语义。
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = 300,
        max_actions: int = 1024,
        clock: Callable[[], float] | None = None,
        token_factory: Callable[[], str] | None = None,
        _backend: ConfirmationStore | None = None,
    ) -> None:
        if ttl_seconds <= 0 or max_actions <= 1:
            raise ValueError("confirmation store limits must be positive")
        self.ttl_seconds = int(ttl_seconds)
        self.max_actions = int(max_actions)
        source = token_factory or (lambda: secrets.token_urlsafe(18))
        self._store = _backend or ConfirmationStore(
            ttl_seconds=self.ttl_seconds,
            max_entries=self.max_actions,
            clock=clock or time.monotonic,
            token_factory=_bounded_ticket_factory(source),
        )

    @staticmethod
    def _normalize_actions(
        operation: str,
        actions: list[tuple[str, dict[str, Any]]],
        *,
        max_actions: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        operation_name = str(operation or "").strip()
        if not operation_name or len(operation_name) > 80:
            raise ValueError("confirmation operation is invalid")
        if not actions:
            raise ValueError("confirmation group must contain at least one action")
        if len(actions) > max_actions:
            raise ValueError("confirmation group exceeds store capacity")

        normalized: list[dict[str, Any]] = []
        for decision, value in actions:
            decision_name = str(decision or "").strip()
            if (
                not decision_name
                or len(decision_name) > 40
                or not isinstance(value, dict)
            ):
                raise ValueError("confirmation action is invalid")
            snapshot = {"decision": decision_name, "value": dict(value)}
            try:
                encoded = json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                    allow_nan=False,
                )
            except (TypeError, ValueError, OverflowError, RecursionError) as exc:
                raise ValueError("confirmation action is invalid") from exc
            if len(encoded.encode("utf-8")) > 16 * 1024:
                raise ValueError("confirmation action is too large")
            normalized.append(snapshot)
        return operation_name, normalized

    def create_group(
        self,
        *,
        chat_id: str,
        user_id: str,
        operation: str,
        actions: list[tuple[str, dict[str, Any]]],
    ) -> tuple[str, ...]:
        operation_name, normalized = self._normalize_actions(
            operation,
            actions,
            max_actions=self.max_actions,
        )
        ticket = self._store.issue(
            owner=_telegram_owner(str(chat_id), str(user_id)),
            tool_name=_TELEGRAM_WRITE_TOOL,
            arguments={"operation": operation_name, "actions": normalized},
            replace_active_ticket=True,
        )
        return tuple(
            f"{ticket.confirmation_id}.{index}" for index in range(len(normalized))
        )

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

    def claim(
        self,
        action_id: str,
        *,
        chat_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        match = _CALLBACK_ID_RE.fullmatch(str(action_id or "").strip())
        if match is None:
            raise TelegramWriteConfirmationError("确认已过期或已处理，请重新发起")
        ticket_id = match.group("ticket")
        choice_index = int(match.group("index"))
        owner = _telegram_owner(str(chat_id), str(user_id))
        owner_match = self._store.ticket_owner_match(
            owner=owner,
            confirmation_id=ticket_id,
        )
        if owner_match is False:
            raise TelegramWriteConfirmationError("该确认不属于当前用户")
        if owner_match is None:
            raise TelegramWriteConfirmationError("确认已过期或已处理，请重新发起")
        try:
            ticket = self._store.claim_and_rotate_owner(
                owner=owner,
                confirmation_id=ticket_id,
            )
        except AgentToolError as exc:
            raise TelegramWriteConfirmationError(
                "确认已过期或已处理，请重新发起"
            ) from exc
        if ticket.tool_name != _TELEGRAM_WRITE_TOOL:
            raise TelegramWriteConfirmationError("确认内容无效，请重新发起")
        operation = ticket.arguments.get("operation")
        actions = ticket.arguments.get("actions")
        if (
            not isinstance(operation, str)
            or not operation
            or not isinstance(actions, list)
            or choice_index >= len(actions)
        ):
            raise TelegramWriteConfirmationError("确认内容无效，请重新发起")
        selected = actions[choice_index]
        if not isinstance(selected, dict):
            raise TelegramWriteConfirmationError("确认内容无效，请重新发起")
        decision = selected.get("decision")
        value = selected.get("value")
        if not isinstance(decision, str) or not decision or not isinstance(value, dict):
            raise TelegramWriteConfirmationError("确认内容无效，请重新发起")
        return {
            "decision": decision,
            "operation": operation,
            "value": dict(value),
        }


class SQLiteTelegramWriteConfirmationStore(TelegramWriteConfirmationStore):
    """使用统一 SQLiteConfirmationStore 的 Telegram 传输适配。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = 300,
        max_actions: int = 1024,
        clock: Callable[[], float] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        source = token_factory or (lambda: secrets.token_urlsafe(18))
        backend = SQLiteConfirmationStore(
            ttl_seconds=ttl_seconds,
            max_entries=max_actions,
            clock=clock or time.time,
            token_factory=_bounded_ticket_factory(source),
        )
        super().__init__(
            ttl_seconds=ttl_seconds,
            max_actions=max_actions,
            _backend=backend,
        )


_store: TelegramWriteConfirmationStore = SQLiteTelegramWriteConfirmationStore()


def get_telegram_write_confirmation_store() -> TelegramWriteConfirmationStore:
    return _store


def reset_telegram_write_confirmation_store_for_tests() -> None:
    global _store
    _store = TelegramWriteConfirmationStore()
