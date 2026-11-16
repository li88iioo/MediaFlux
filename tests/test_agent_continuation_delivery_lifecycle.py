"""续行已交接 outbox 后，实际发送仍须遵守会话生命周期。"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from unittest.mock import patch

import httpx
import pytest

from app import config, database as db
from app.agent.kernel.bootstrap import build_agent_kernel_runtime
from app.agent.kernel.continuation import deliver_continuation_result
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.model_catalog import provider_channel_fingerprint
from app.agent.transports import QueryEnvelope
from app.bot.agent_adapter import (
    prepare_agent_continuation_delivery,
    publish_agent_continuation_delivery,
)
from app.modules import telegram_notification_center as center
from tests.test_agent_continuation_telegram import NativeBotFixture
from tests.test_agent_read_wait_contract_repair import resumed
from tests.test_agent_read_wait_entry_exclusions import tg_config as tg_config
from tests.test_agent_read_wait_lifecycle import WaitScript
from tests.test_agent_read_wait_lifecycle import isolated_database as isolated_database
from tests.test_agent_waiting_delivery import OWNER, ROUTE, SID, park
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414


class DeliveryScript(WaitScript):
    def __init__(self, answer="已取得真实查询结果。"):
        super().__init__(True)
        self.answer = answer

    async def respond(self, request):
        response = await super().respond(request)
        body = response.json()
        body["choices"][0]["message"]["content"] = self.answer
        return httpx.Response(200, json=body)


def _bind_telegram(script):
    script.adapter.settings = replace(script.adapter.settings, telegram_channel=provider_channel_fingerprint(
        config.get_many(("AGENT_LLM_API_URL", "AGENT_LLM_PROTOCOL", "AGENT_LLM_API_KEY"))))
    return script


@pytest.fixture
def bot(tg_config) -> Iterator[NativeBotFixture]:
    bot = NativeBotFixture()
    was_stopped = center._dispatch_stop.is_set()
    center._dispatch_stop.clear()
    try:
        with (
            patch.object(center, "wake_telegram_notification_dispatcher", return_value=False),
            patch("app.notifier.get_bot", return_value=bot),
        ):
            yield bot
    finally:
        if was_stopped:
            center._dispatch_stop.set()


def _rows():
    with db.get_conn() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM telegram_notification_outbox "
            "WHERE json_extract(event_json,'$._agent_continuation_delivery') IS NOT NULL ORDER BY id"
        )]


async def _enqueue(clock, answer="已取得真实查询结果。"):
    candidate = await park(clock, ROUTE)
    clock[0] += 10
    runtime, script, reads = resumed(clock, _bind_telegram(DeliveryScript(answer)))
    assert await runtime.session.resume_wait(candidate=candidate)
    delivery, = await SQLiteKernelStore(clock=lambda: clock[0]).due_continuations()
    assert delivery["delivery_view"]["status"] == "success"
    assert delivery["delivery_view"]["answer"] == answer
    assert await deliver_continuation_result(runtime.store, delivery,
        prepare_delivery=prepare_agent_continuation_delivery,
        publish_delivery=publish_agent_continuation_delivery)
    assert len(reads) == len(script.requests) == 1
    assert await runtime.store.due_continuations() == []
    return runtime, script, reads


@pytest.mark.parametrize("operation", ["reset", "delete"])
def test_queued_continuation_is_suppressed_after_real_session_lifecycle(agent_llm_enabled, bot, operation):
    async def exercise():
        clock = [100]
        runtime, script, reads = await _enqueue(clock)
        assert not bot.sent and len(_rows()) == 1
        await getattr(runtime.lifecycle, operation)(owner=OWNER, session_id=SID)
        after = await runtime.store.load(owner=OWNER, session_id=SID)
        assert not after.conversation and not after.metadata
        await asyncio.to_thread(center.drain_telegram_notifications, limit=16)
        assert len(bot.sent) == len(bot.attempts) == 0, "成功重置或删除后，旧生命周期的续行不得再触达 Bot"
        rows = _rows()
        assert [row["status"] for row in rows] == ["suppressed"]
        assert rows[0]["last_error"]
        assert len(reads) == len(script.requests) == 1

    asyncio.run(exercise())


def test_deleted_session_recreation_does_not_rebind_old_queued_result(agent_llm_enabled, bot):
    async def exercise():
        clock = [100]
        first, old_script, old_reads = await _enqueue(clock, "旧生命周期事实：原查询已完成。")
        old_key = _rows()[0]["event_key"]
        assert await first.lifecycle.delete(owner=OWNER, session_id=SID)
        # 同一个 owner/session 通过真实新请求重新创建；旧 outbox 仍持久存在。
        clock[0] = 100
        second, new_script, new_reads = await _enqueue(clock, "新生命周期事实：新的查询已完成。")
        assert second is not first
        assert len(_rows()) == 2 and not bot.sent
        await asyncio.to_thread(center.drain_telegram_notifications, limit=16)
        assert len(bot.sent) == len(bot.attempts) == 1, "同名会话重建不能把旧通知重新绑定为新会话事实"
        assert "新生命周期事实" in bot.sent[0][1] and "旧生命周期事实" not in bot.sent[0][1]
        statuses = {row["event_key"]: row["status"] for row in _rows()}
        assert statuses.pop(old_key) == "suppressed"
        assert list(statuses.values()) == ["sent"]
        assert len(old_reads) == len(new_reads) == len(old_script.requests) == len(new_script.requests) == 1

    asyncio.run(exercise())


def test_ordinary_new_turn_keeps_completed_fact_deliverable(agent_llm_enabled, bot):
    async def exercise():
        clock = [100]
        runtime, old_script, reads = await _enqueue(clock, "前一轮已核验的事实。")
        before = await runtime.store.load(owner=OWNER, session_id=SID)
        script = _bind_telegram(DeliveryScript("新的问题已回答。"))
        foreground = build_agent_kernel_runtime(model=script.adapter, store=SQLiteKernelStore(clock=lambda: clock[0]))
        events = [event async for event in foreground.session.run(QueryEnvelope(
            message="我另问一句：你好。", owner=OWNER, session_id=SID, request_id="new-user-turn",
            channel="telegram", delivery_route=ROUTE,
        ).to_agent_input())]
        assert events[-1].payload["status"] == "success"
        after = await foreground.store.load(owner=OWNER, session_id=SID)
        assert after.generation > before.generation
        assert not bot.sent
        await asyncio.to_thread(center.drain_telegram_notifications, limit=16)
        assert len(bot.sent) == len(bot.attempts) == 1 and "前一轮已核验的事实" in bot.sent[0][1]
        assert [row["status"] for row in _rows()] == ["sent"]
        assert len(reads) == len(old_script.requests) == len(script.requests) == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["reset", "delete"])
def test_multipart_sent_prefix_survives_and_all_unsent_parts_are_suppressed(agent_llm_enabled, bot, operation):
    async def exercise():
        clock = [100]
        answer = "\n".join(["已经核验的媒体事实，等待续行结果。"] * 700)
        runtime, script, reads = await _enqueue(clock, answer)
        queued = _rows()
        assert len(queued) >= 3 and all(row["status"] == "pending" for row in queued)
        assert await asyncio.to_thread(center.drain_telegram_notifications, limit=1)
        sent = _rows()[0]
        assert len(bot.sent) == 1 and sent["status"] == "sent" and sent["message_id"] > 0
        await getattr(runtime.lifecycle, operation)(owner=OWNER, session_id=SID)
        await asyncio.to_thread(center.drain_telegram_notifications, limit=16)
        assert len(bot.sent) == len(bot.attempts) == 1, "重置后的分段尾部不得继续发送"
        rows = _rows()
        assert rows[0]["status"] == "sent" and rows[0]["message_id"] == sent["message_id"]
        assert all(row["status"] == "suppressed" and row["last_error"] for row in rows[1:]), (
            "失效必须终结整组未发送段，不能留下被前缀检查永久阻挡的 pending/retry_wait"
        )
        assert len(rows) == len(queued)
        assert not await asyncio.to_thread(center.drain_telegram_notifications, limit=16)
        assert len(bot.sent) == 1 and len(reads) == len(script.requests) == 1

    asyncio.run(exercise())
