"""待确认卡普通对话回归：脚本模型、最小订阅工具、真实 Kernel 和 SQLite。

不访问真实模型或 RSS 网络服务；业务写入落到隔离数据库的 rss_items。
"""
from __future__ import annotations
from app.agent.kernel.effects import ConfirmationEffectPlanStore, DefaultAuthorizationPolicy, NoopEffectLifecycle

import unittest
import json
from app.agent.kernel import sandbox
from unittest.mock import patch

from app import database as db
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.events import AgentEventType as E
from app.agent.kernel.model import ModelEvent, ModelToolCall
from app.agent.kernel.model import ModelEventType as M
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput
from tests.support import isolated_test_database

OWNER, SID = "dialogue-owner", "pending-dialogue"
WRITE, READ, CONTROL = "rss.create_subscription", "rss.list_subscriptions", "agent.run_code"


class ScriptedModel:
    """只按测试脚本发工具调用，不模拟自然语言理解。"""

    def __init__(self):
        self.calls = []
        self.requests = []

    async def stream(self, request, *, cancellation):
        self.requests.append(request)
        cancellation.raise_if_cancelled()
        if self.calls:
            name, arguments = self.calls.pop(0)
            if name == CONTROL:
                arguments = {"code": (
                    "import json\nfrom mf import session, Error\n"
                    f"payload={arguments!r}\n"
                    "try:\n    result=session.pending_plan(**payload)\n"
                    "except (Error, TypeError) as exc:\n"
                    "    result={'sdk_error': getattr(exc, 'code', type(exc).__name__)}\n"
                    "print(json.dumps(result, ensure_ascii=False))"
                )}
            call = ModelToolCall(f"call-{len(self.requests)}", name, arguments)
            yield ModelEvent(M.TOOL_CALL_COMPLETED, tool_call=call)
            yield ModelEvent(M.FINISH, finish_reason="tool_calls")
        else:
            yield ModelEvent(M.TEXT_DELTA, text="仅讨论，业务是否执行以服务端回执为准。")
            yield ModelEvent(M.FINISH, finish_reason="stop")


class PendingDialogueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(isolated_test_database("pending-dialogue.db"))
        self.now = [1_800_000_000.0]
        self.trace = []
        self.denied = False
        self.session = self.runtime()

    def rows(self):
        with db.get_conn() as conn:
            return [row[0] for row in conn.execute("SELECT name FROM rss_items ORDER BY id")]

    def runtime(self):
        store = SQLiteKernelStore(clock=lambda: self.now[0])
        tickets = SQLiteConfirmationStore(ttl_seconds=60, clock=lambda: self.now[0])
        trace = self.trace
        fixture = self

        class Policy(DefaultAuthorizationPolicy):
            async def authorize(self, tool, arguments, context):
                await super().authorize(tool, arguments, context)
                if tool.name == WRITE and fixture.denied:
                    raise ToolPipelineError("测试授权拒绝", code="authorization_denied")

        class Lifecycle(NoopEffectLifecycle):
            def cancelled(self, *, plan):
                trace.append(("cancel", plan.plan_id, dict(plan.arguments)))

        async def prepare(arguments, context):
            state = await store.load(owner=context.owner, session_id=context.session_id)
            trace.append(("prepare", arguments["name"], state.pending_effect_plan_id or None))
            return PreparedEffect(arguments=dict(arguments), snapshot_fingerprint=arguments["name"],
                                  preview={"summary": f"创建订阅 {arguments['name']}"})

        def write(arguments, _snapshot, _context):
            db.add_rss_subscription(name=arguments["name"], urls="https://example.invalid/feed")
            return {"ok": True, "summary": f"已创建订阅 {arguments['name']}"}

        def read(arguments, _context):
            if arguments.get("fail"):
                raise ToolPipelineError("脚本只读失败", code="read_failed")
            return {"ok": True, "summary": "订阅列表", "data": {"names": self.rows()}}

        catalog = ToolCatalog([
            KernelToolSpec(name=WRITE, domain="rss", description="创建 RSS 订阅确认卡",
                           input_schema={"type": "object", "required": ["name"],
                                         "properties": {"name": {"type": "string", "minLength": 1}},
                                         "additionalProperties": False},
                           effect=ToolEffect.WRITE, prepare=prepare, execute_confirmed=write),
            KernelToolSpec(name=READ, domain="rss", description="只读查询 RSS 订阅",
                           input_schema={"type": "object", "properties": {"fail": {"type": "boolean"}},
                                         "additionalProperties": False},
                           effect=ToolEffect.READ, read=read),
        ])
        pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store,
                                effect_store=ConfirmationEffectPlanStore(tickets),
                                authorization=Policy(), effect_lifecycle=Lifecycle())
        catalog.register(sandbox.run_code_tool(pipeline))
        return AgentSession(model=ScriptedModel(), catalog=catalog,
                            pipeline=pipeline, state_store=store, journal=store)

    async def run_turn(self, *calls, session=None, owner=OWNER, sid=SID,
                       message="讨论 RSS 订阅和当前待确认计划，按工具回执答复"):
        session = session or self.session
        assert isinstance(session.model, ScriptedModel)
        session.model.calls = list(calls)
        events = [event async for event in session.run(AgentInput(
            owner=owner, session_id=sid, message=message))]
        assert isinstance(session.model, ScriptedModel)
        self.assertFalse(session.model.calls, "脚本工具调用未被消费")
        return events

    async def pending(self, session=None, owner=OWNER, sid=SID):
        return await (session or self.session).pipeline.get_pending_plan(owner=owner, session_id=sid)

    async def preview(self, name="original", **scope):
        events = await self.run_turn((WRITE, {"name": name}), **scope)
        self.assertTrue(any(e.type == E.EFFECT_APPROVAL_REQUIRED for e in events), self.summary(events))
        plan = await self.pending(**scope)
        assert (plan) is not None
        return plan

    async def confirm(self, plan, *, session=None, owner=OWNER, sid=SID):
        return [event async for event in (session or self.session).confirm(
            owner=owner, session_id=sid, plan_id=plan.plan_id)]

    @staticmethod
    def summary(events):
        return [(e.type.value, e.payload.get("tool"), e.payload.get("code")) for e in events]

    def rejected(self, events):
        self.assertEqual(events[-1].type, E.TURN_FAILED, self.summary(events))
        self.assertEqual(events[-1].payload.get("code"), "confirmation_invalid")
        self.assertFalse(any(e.type in {E.TOOL_STARTED, E.EFFECT_COMPLETED, E.EFFECT_FAILED} for e in events))

    def result(self, events, name):
        matches = [e.payload["result"] for e in events
                   if e.type == E.TOOL_COMPLETED and e.payload.get("tool") == name]
        self.assertEqual(len(matches), 1, self.summary(events))
        result = matches[0]
        if name == CONTROL:
            self.assertEqual(result["data"]["exit_code"], 0, result)
            return json.loads(result["data"]["stdout"])
        return result

    def control_visible(self, session):
        alias = session.catalog.get(CONTROL).model_name
        return any(tool["name"] == alias for tool in session.model.requests[-1].tools)

    async def empty_controls(self, hidden_plan, **scope):
        for operation in ("inspect", "cancel"):
            events = await self.run_turn((CONTROL, {"operation": operation}), **scope)
            result = self.result(events, CONTROL)
            self.assertEqual(result["data"], {"pending": False, "cancelled": False})
            for secret in (hidden_plan.plan_id, hidden_plan.arguments["name"]):
                self.assertNotIn(secret, str(result))
            self.assertIsNone(await self.pending(**scope))
            self.assertEqual(self.rows(), [])
            self.assertFalse(any(e.type == E.EFFECT_COMPLETED for e in events))

    async def discussion(self, cold):
        original = await self.preview()
        session = self.runtime() if cold else self.session
        if cold:
            self.assertIsNot(session.state_store, self.session.state_store)
            assert isinstance(self.session.pipeline.effect_store, ConfirmationEffectPlanStore)
            assert isinstance(session.pipeline.effect_store, ConfirmationEffectPlanStore)
            self.assertIsNot(session.pipeline.effect_store.store, self.session.pipeline.effect_store.store)
        self.now[0] += 5
        await self.run_turn(session=session)
        self.assertEqual(await self.pending(session), original)
        self.now[0] += 5
        events = await self.run_turn((READ, {}), session=session)
        self.result(events, READ)
        self.assertEqual(await self.pending(session), original, "讨论不得改变票据、冻结参数或 TTL")
        self.assertEqual(self.trace, [("prepare", "original", None)])
        self.assertEqual(self.rows(), [])
        confirmed = await self.confirm(original, session=session)
        self.assertEqual(sum(e.type == E.EFFECT_COMPLETED for e in confirmed), 1, confirmed)
        self.assertEqual(self.rows(), ["original"])
        self.assertIsNone(await self.pending(session))
        self.rejected(await self.confirm(original, session=self.runtime()))
        self.assertEqual(self.rows(), ["original"])

    async def test_ordinary_discussion_preserves_ticket_and_confirms_once(self):
        await self.discussion(cold=False)

    async def test_cold_recovery_discussion_preserves_ticket_and_confirms_once(self):
        await self.discussion(cold=True)

    async def test_modification_cancels_before_prepare_and_rejects_old_card(self):
        original = await self.preview()
        self.trace.clear()
        replacement = await self.preview("replacement")
        self.assertNotEqual(replacement.plan_id, original.plan_id)
        self.assertEqual(dict(replacement.arguments), {"name": "replacement"})
        self.assertEqual(self.trace, [("cancel", original.plan_id, {"name": "original"}),
                                     ("prepare", "replacement", None)])
        self.assertEqual(self.rows(), [])
        cold = self.runtime()
        self.rejected(await self.confirm(original, session=cold))
        self.assertEqual(await self.pending(cold), replacement)
        events = await self.confirm(replacement, session=cold)
        self.assertEqual(sum(e.type == E.EFFECT_COMPLETED for e in events), 1, events)
        self.rejected(await self.confirm(replacement, session=self.runtime()))
        self.assertEqual(self.rows(), ["replacement"])

    async def test_native_inspect_preserves_pending_card(self):
        original = await self.preview()
        cold = self.runtime()
        events = await self.run_turn((CONTROL, {"operation": "inspect"}), session=cold)
        self.assertTrue(self.control_visible(cold), "原生工作区应保持可用")
        inspected = self.result(events, CONTROL)
        self.assertTrue(inspected["data"]["pending"])
        self.assertIn("original", str(inspected["data"]["preview"]))
        self.assertEqual(await self.pending(cold), original)
        self.assertEqual(self.trace, [("prepare", "original", None)])
        self.assertEqual(self.rows(), [])

    async def test_hello_keeps_native_entry_without_registering_pending_tool(self):
        await self.run_turn(message="hello")
        self.assertTrue(self.control_visible(self.session))
        self.assertFalse(self.session.catalog.has("agent.pending_plan"))
        self.assertIsNone(await self.pending())
        original = await self.preview()
        await self.run_turn(message="hello")
        self.assertTrue(self.control_visible(self.session))
        self.assertEqual(await self.pending(), original)
        self.assertEqual(self.rows(), [])

    async def test_model_cancel_uses_original_ticket_chain_with_zero_business_writes(self):
        original = await self.preview()
        cold = self.runtime()
        effect_store = cold.pipeline.effect_store
        with patch.object(effect_store, "cancel", wraps=effect_store.cancel) as cancel:
            events = await self.run_turn((CONTROL, {"operation": "cancel"}), session=cold)
            self.assertTrue(self.result(events, CONTROL)["data"]["cancelled"])
            cancel.assert_called_once_with(owner=OWNER, session_id=SID,
                                           generation=original.generation, plan_id=original.plan_id)
        self.assertIsNone(await self.pending(cold))
        self.assertEqual(self.trace[-1], ("cancel", original.plan_id, {"name": "original"}))
        self.rejected(await self.confirm(original, session=self.runtime()))
        self.assertEqual(self.rows(), [])
        self.assertFalse(any(e.type == E.EFFECT_COMPLETED for e in events))

    async def test_argument_authorization_and_read_failures_keep_card(self):
        original = await self.preview()
        cases = [
            (WRITE, {"name": ""}, False, "invalid_arguments"),
            (WRITE, {"name": "replacement"}, True, "authorization_denied"),
            (READ, {"fail": True}, False, "read_failed"),
            (CONTROL, {"operation": "confirm"}, False, "invalid_arguments"),
            (CONTROL, {"operation": "cancel", "plan_id": "foreign-card"}, False, "TypeError"),
        ]
        for name, arguments, denied, code in cases:
            with self.subTest(tool=name, arguments=arguments):
                self.denied = denied
                events = await self.run_turn((name, arguments))
                failures = [e for e in events if e.type == E.TOOL_FAILED]
                if name == CONTROL:
                    self.assertEqual(self.result(events, name)["sdk_error"], code)
                else:
                    self.assertTrue(any(e.payload.get("code") == code for e in failures), self.summary(events))
                self.assertEqual(await self.pending(), original)
                self.assertEqual(self.rows(), [])
                self.assertEqual(self.trace, [("prepare", "original", None)])
        self.denied = False
        await self.confirm(original)
        self.assertEqual(self.rows(), ["original"])

    async def test_expired_card_cannot_revive_through_discussion_or_cold_confirmation(self):
        original = await self.preview()
        self.now[0] = original.expires_at + 1
        cold = self.runtime()
        events = await self.run_turn((READ, {}), session=cold)
        self.result(events, READ)
        await self.empty_controls(original, session=cold)
        self.assertIsNone(await self.pending(cold))
        self.rejected(await self.confirm(original, session=self.runtime()))
        await self.run_turn(session=cold)
        self.assertIsNone(await self.pending(cold))
        self.assertEqual(self.rows(), [])
        self.assertEqual([item for item in self.trace if item[0] == "prepare"],
                         [("prepare", "original", None)])

    async def test_owner_and_session_cannot_inspect_cancel_or_confirm_another_card(self):
        original = await self.preview("owner-a")
        for owner, sid in (("other-owner", SID), (OWNER, "other-session")):
            with self.subTest(owner=owner, sid=sid):
                cold = self.runtime()
                await self.empty_controls(original, session=cold, owner=owner, sid=sid)
                self.assertEqual(await self.pending(), original)
                isolated = await self.preview("isolated", session=cold, owner=owner, sid=sid)
                self.rejected(await self.confirm(original, session=cold, owner=owner, sid=sid))
                self.assertEqual(await self.pending(cold, owner=owner, sid=sid), isolated)
                events = await self.run_turn((CONTROL, {"operation": "inspect"}),
                                             session=cold, owner=owner, sid=sid)
                inspected = self.result(events, CONTROL)
                self.assertTrue(inspected["data"]["pending"])
                self.assertNotIn("owner-a", str(inspected))
                events = await self.run_turn((CONTROL, {"operation": "cancel"}),
                                             session=cold, owner=owner, sid=sid)
                self.assertTrue(self.result(events, CONTROL)["data"]["cancelled"])
                self.assertIsNone(await self.pending(cold, owner=owner, sid=sid))
                self.assertEqual(await self.pending(), original)
                self.assertEqual(self.rows(), [])
        await self.confirm(original, session=self.runtime())
        self.assertEqual(self.rows(), ["owner-a"])
