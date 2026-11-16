"""真实确认任务完成后，两类 Telegram 回执共用实时授权规则。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import config, database as db
from app.agent.owner_routes import telegram_owner_route_is_currently_authorized
from app.bot import agent_adapter as adapter
from app.modules import telegram_notification_center as center
from tests.test_agent_continuation_delivery_handoff import (
    OWNER,
    ROUTE,
    SID,
    _completed_effect,
    _handoff,
)
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414


class BotFixture:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=100 + len(self.sent))


def _row(kind):
    prefix = "agent-effect:" if kind == "effect" else "agent-continuation:"
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM telegram_notification_outbox WHERE event_key LIKE ? ORDER BY id",
            (f"tg:event:agent:{prefix}%",),
        ).fetchall()
    assert len(rows) == 1
    return dict(rows[0])


@pytest.mark.parametrize("kind", ["effect", "continuation"])
@pytest.mark.parametrize("change", [None, "TG_AGENT_ALLOWED_USER_IDS", "TG_CHAT_ID"])
def test_completed_native_receipt_rechecks_current_authorization_before_bot(agent_llm_enabled, tmp_path, kind, change):
    async def run():
        bot = BotFixture()
        with patch.object(center, "wake_telegram_notification_dispatcher", return_value=False), patch(
            "app.notifier.get_bot", return_value=bot,
        ):
            async with _completed_effect(tmp_path, [100]) as (store, script, reads):
                if kind == "continuation":
                    candidate, = await store.due_continuations()
                    assert await _handoff(store, candidate)
                queued = _row(kind)
                state = await store.load(owner=OWNER, session_id=SID)
                current_get = config.get
                changed_value = "8" if change == "TG_AGENT_ALLOWED_USER_IDS" else "-100999"
                with patch.object(config, "get", side_effect=lambda key, default="": (
                    changed_value if change is not None and key == change else current_get(key, default)
                )):
                    assert telegram_owner_route_is_currently_authorized(OWNER) is (change is None)
                    result = await asyncio.to_thread(center.drain_telegram_notifications, event_key=queued["event_key"])
                    saved = center.get_notification(queued["event_key"])
                    assert saved is not None
                    if change is None:
                        assert result and saved["status"] == "sent" and len(bot.sent) == 1
                        chat, _, options = bot.sent[0]
                        assert chat == ROUTE["chat_id"] and options["message_thread_id"] == ROUTE["message_thread_id"]
                    else:
                        assert not result and saved["status"] == "failed"
                        assert saved["last_error"] == "AgentDeliveryUnauthorized"
                        assert bot.sent == [], "撤权后的完成事实不能再发送到旧用户/群"
                        await asyncio.to_thread(center.drain_telegram_notifications, event_key=queued["event_key"])
                        assert bot.sent == []
                assert (await store.load(owner=OWNER, session_id=SID)).lifecycle_id == state.lifecycle_id
                assert reads == ["admitted.mp4"] and len(script.requests) == 2
                with db.get_conn() as conn:
                    assert conn.execute("SELECT COUNT(*) FROM organize_operation_jobs").fetchone()[0] == 1
    asyncio.run(run())


@pytest.mark.parametrize("setting,value,status", [
    ("TG_AGENT_ALLOWED_USER_IDS", "8", 403),
    ("TG_CHAT_ID", "-100999", 403),
    ("TG_AGENT_ENABLED", "0", 503),
    ("AGENT_ENABLED", "0", 503),
])
def test_direct_native_continuation_sender_retains_live_authorization_contract(agent_llm_enabled, tmp_path, setting, value, status):
    async def run():
        bot = BotFixture()
        with patch.object(center, "wake_telegram_notification_dispatcher", return_value=False):
            async with _completed_effect(tmp_path, [100]) as (store, _, _):
                candidate, = await store.due_continuations()
                assert await _handoff(store, candidate)
                payload = json.loads(_row("continuation")["event_json"])["_agent_continuation_delivery"]
                current_get = config.get
                with patch.object(config, "get", side_effect=lambda key, default="": (
                    value if key == setting else current_get(key, default)
                )), patch("app.notifier.get_bot", return_value=bot):
                    result = await asyncio.to_thread(adapter._send_agent_continuation_part, payload)
                assert not result.ok and result.status_code == status and bot.sent == []
    asyncio.run(run())


def test_control_permission_can_enable_agent_while_delivery_remains_disabled():
    values = {"TG_CHAT_ID": ROUTE["chat_id"], "TG_AGENT_ALLOWED_USER_IDS": "7",
              "AGENT_ENABLED": "0", "TG_AGENT_ENABLED": "0"}
    with patch.object(config, "get", side_effect=lambda key, default="": values.get(key, default)):
        assert adapter.telegram_agent_control_access(ROUTE["chat_id"], "7") == "allowed"
        assert adapter.telegram_agent_access(ROUTE["chat_id"], "7") == "disabled"
        assert not telegram_owner_route_is_currently_authorized(OWNER)
