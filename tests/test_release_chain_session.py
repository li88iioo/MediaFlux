"""确认终态的会话回写不能清除同 generation 的另一张票据。"""
from __future__ import annotations

from tests.support import assert_present

import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from app.agent.confirmation import ConfirmationStore
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import (
    CancellationToken,
    SessionBusyError,
    StalePublicationError,
    StateUpdate,
)
from app.agent.kernel.tool_calls import ToolCallContext
from tests.support import isolated_test_database


class ConfirmationSessionPointerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.ordinal = 0

    async def fixture(self, kind):
        self.ordinal += 1
        store = (InMemorySessionStateStore() if kind == "memory" else
                 SQLiteKernelStore(secret_provider=lambda: "isolated-session-secret"))
        lease, _ = await store.begin_turn(owner="owner", session_id=f"s{self.ordinal}", request_id="prepare")

        async def progress(_payload):
            pass

        context = ToolCallContext(owner=lease.owner, session_id=lease.session_id,
            request_id=lease.request_id, turn_id=lease.turn_id, lease=lease,
            cancellation=CancellationToken(), report_progress=progress)
        execute = Mock(return_value={"ok": True, "summary": "accepted"})
        catalog = ToolCatalog([KernelToolSpec(name="downloads.submit", domain="downloads",
            description="test write", input_schema={"type": "object", "properties": {}},
            effect=ToolEffect.WRITE,
            prepare=lambda *_: PreparedEffect(preview={"summary": "preview"}, snapshot_fingerprint="stable"),
            execute_confirmed=execute)])
        pipeline = ToolPipeline(catalog=catalog, state_store=store,
            effect_store=ConfirmationEffectPlanStore(ConfirmationStore()))
        session = AgentSession(model=Mock(), catalog=catalog,
             pipeline=pipeline, state_store=store)
        prepared = await pipeline.execute("downloads.submit", {}, context=context)
        assert prepared.effect_plan is not None
        return session, context, prepared.effect_plan.plan_id, execute

    async def test_failed_old_confirmation_retains_current_ticket_and_result_history(self):
        for kind in ("memory", "sqlite"):
            with self.subTest(store=kind):
                session, context, previous, execute = await self.fixture(kind)
                events = [e async for e in session.confirm(owner=context.owner,
                    session_id=context.session_id, plan_id=previous)]
                self.assertEqual(events[-1].type.value, "turn.completed")
                history = (await session.state_store.load(owner=context.owner,
                    session_id=context.session_id)).conversation
                self.assertTrue(history)  # 真实确认产生的已有可信回执，不用失败文本充数。
                execute.reset_mock()
                lease, _ = await session.state_store.begin_turn(owner=context.owner,
                    session_id=context.session_id, request_id="next-prepare")
                context = replace(context, lease=lease, turn_id=lease.turn_id, request_id=lease.request_id)
                old = await session.pipeline.execute("downloads.submit", {}, context=context)
                new = await session.pipeline.execute("downloads.submit", {}, context=context)
                events = [e async for e in session.confirm(owner=context.owner,
                    session_id=context.session_id, plan_id=assert_present(old.effect_plan).plan_id)]
                state = await session.state_store.load(owner=context.owner, session_id=context.session_id)
                assert new.effect_plan is not None
                self.assertEqual(state.pending_effect_plan_id, new.effect_plan.plan_id)
                self.assertEqual(events[-1].type.value, "turn.failed")
                self.assertEqual(state.conversation, history)
                execute.assert_not_called()
                events = [e async for e in session.confirm(owner=context.owner,
                    session_id=context.session_id, plan_id=new.effect_plan.plan_id)]
                self.assertEqual(events[-1].type.value, "turn.completed")
                self.assertEqual(execute.call_count, 1)
                state = await session.state_store.load(owner=context.owner, session_id=context.session_id)
                self.assertEqual(state.conversation[:len(history)], history)
                self.assertGreater(len(state.conversation), len(history))

    async def test_same_generation_replacement_at_terminal_write_is_not_erased(self):
        for kind in ("memory", "sqlite"):
            with self.subTest(store=kind):
                session, context, old, execute = await self.fixture(kind)
                original = session.state_store.commit
                replacement = []

                async def commit(lease, **kwargs):
                    if kwargs.get("conversation") and not replacement:
                        # A4 有意禁止旧读上下文在确认持锁期再预检。拒绝不能遮蔽成功回执。
                        with self.assertRaises(SessionBusyError):
                            await session.pipeline.execute("downloads.submit", {}, context=context)
                        # 单独注入已经持久化的新票据/指针，保留原子 clear_if_equals 的
                        # 同 generation 验收；不把拒绝的旧预检冒充成功替换。
                        new = session.pipeline.effect_store.freeze(owner=context.owner,
                            session_id=context.session_id, generation=lease.generation,
                            tool_name="downloads.submit", effect=ToolEffect.WRITE, arguments={},
                            prepared=PreparedEffect(preview={"summary": "next"}, snapshot_fingerprint="stable"))
                        await original(lease, updates=(StateUpdate("pending_effect_plan_id", new.plan_id),))
                        replacement.append(new.plan_id)
                    return await original(lease, **kwargs)

                with patch.object(session.state_store, "commit", side_effect=commit):
                    events = [e async for e in session.confirm(owner=context.owner,
                        session_id=context.session_id, plan_id=old)]
                state = await session.state_store.load(owner=context.owner, session_id=context.session_id)
                self.assertEqual(len(replacement), 1)
                self.assertEqual(state.pending_effect_plan_id, replacement[0])
                self.assertEqual(events[-1].type.value, "turn.completed")
                self.assertEqual(execute.call_count, 1)
                self.assertTrue(state.conversation)
                with self.assertRaises(StalePublicationError):
                    await original(context.lease, conversation=[], updates=(
                        StateUpdate("pending_effect_plan_id", old, mode="clear_if_equals"),))
                latest = await session.state_store.load(owner=context.owner, session_id=context.session_id)
                self.assertEqual(latest.conversation, state.conversation)
                self.assertEqual(latest.pending_effect_plan_id, replacement[0])
                claimed = session.pipeline.effect_store.claim(owner=context.owner,
                    session_id=context.session_id, generation=context.lease.generation, plan_id=replacement[0])
                self.assertEqual(claimed.plan_id, replacement[0])
