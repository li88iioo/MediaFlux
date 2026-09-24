from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from collections.abc import AsyncIterator

from app.agent.kernel.adapters import consume_events
from app.agent.kernel.capabilities import (
    CapabilityRetriever,
    KernelToolSpec,
    ToolCatalog,
    ToolEffect,
)
from app.agent.kernel.effects import PreparedEffect
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import (
    ModelEvent,
    ModelEventType,
    ModelMessage,
    ModelRequest,
    ModelToolCall,
)
from app.agent.kernel.pipeline import ToolCallContext, ToolPipeline, ToolPipelineError
from app.agent.kernel.projection import ReferenceValue, ToolOutcome
from app.agent.kernel.provider_model import ModelProviderError
from app.agent.kernel.session import AgentSession, SessionLimits, _provider_failure_message
from app.agent.kernel.state import (
    AgentInput,
    CancellationToken,
    InMemorySessionStateStore,
    StalePublicationError,
    PublicationLease,
    TurnCoordinator,
)
from app.agent.models import ToolReference, ToolResult
from app.agent.model_context_budget import bounded_model_messages, compact_tool_content


class ScriptedModel:
    def __init__(self, rounds: list[list[ModelEvent]]) -> None:
        self.rounds = list(rounds)
        self.requests: list[ModelRequest] = []

    async def stream(
        self, request: ModelRequest, *, cancellation
    ) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if not self.rounds:
            raise AssertionError("unexpected model round")
        for event in self.rounds.pop(0):
            cancellation.raise_if_cancelled()
            await asyncio.sleep(0)
            yield event


def read_tool(
    name: str,
    *,
    domain: str = "library",
    description: str = "读取状态",
    examples=(),
    handler=None,
):
    return KernelToolSpec(
        name=name,
        domain=domain,
        description=description,
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        effect=ToolEffect.READ,
        examples=tuple(examples),
        read=handler
        or (lambda _arguments, _context: {"summary": "读取完成", "data": {}}),
    )


async def _events_stream(events):
    for event in events:
        yield event


async def collect(stream) -> list:
    return [event async for event in stream]


def _run_thread(coroutine_factory, errors: list[BaseException]) -> None:
    try:
        asyncio.run(coroutine_factory(), debug=True)
    except BaseException as exc:  # pragma: no cover - 仅用于线程错误回传
        errors.append(exc)


class CapabilityRetrieverTests(unittest.TestCase):
    def test_retrieves_six_to_twelve_atomic_tools_without_deciding_intent(self) -> None:
        tools = [
            read_tool(
                "cloud.list_directory",
                domain="cloud",
                description="列出光鸭云盘目录中的文件夹与文件",
                examples=("看看光鸭云盘根目录",),
            ),
            read_tool(
                "cloud.inspect_directory",
                domain="cloud",
                description="检查光鸭目录内容和发布组文件",
            ),
        ]
        tools.extend(
            read_tool(f"library.tool_{index}", description=f"媒体库能力 {index}")
            for index in range(14)
        )
        catalog = ToolCatalog(tools)
        selection = CapabilityRetriever().retrieve(
            "帮我看看光鸭云盘根目录有哪些文件夹", catalog
        )
        self.assertGreaterEqual(len(selection.tools), 6)
        self.assertLessEqual(len(selection.tools), 12)
        self.assertEqual(selection.tools[0].name, "cloud.list_directory")
        self.assertIn("cloud.inspect_directory", selection.names)


class AgentKernelCrossLoopTests(unittest.TestCase):
    def test_turn_coordinator_survives_repeated_cross_loop_contention(self) -> None:
        coordinator = TurnCoordinator()
        errors: list[BaseException] = []

        for round_number in range(2):
            entered = threading.Event()
            release = threading.Event()
            waiter_finished = threading.Event()

            async def hold_lock() -> None:
                async with coordinator._lock:
                    entered.set()
                    while not release.is_set():
                        await asyncio.sleep(0.001)

            async def wait_through_public_api() -> None:
                lease = PublicationLease(
                    owner="owner-1",
                    session_id="session-1",
                    generation=round_number + 1,
                    turn_id=f"turn-{round_number}",
                    request_id=f"request-{round_number}",
                )
                token = await coordinator.begin(lease)
                await coordinator.finish(lease, token)
                waiter_finished.set()

            holder = threading.Thread(
                target=_run_thread,
                args=(hold_lock, errors),
            )
            waiter = threading.Thread(
                target=_run_thread,
                args=(wait_through_public_api, errors),
            )
            holder.start()
            self.assertTrue(entered.wait(timeout=1))
            waiter.start()
            time.sleep(0.03)
            self.assertFalse(waiter_finished.is_set())
            release.set()
            holder.join(timeout=1)
            waiter.join(timeout=1)

            self.assertFalse(holder.is_alive())
            self.assertFalse(waiter.is_alive())
            self.assertTrue(waiter_finished.is_set())

        self.assertEqual(errors, [])

    def test_agent_session_start_window_survives_repeated_cross_loop_contention(
        self,
    ) -> None:
        catalog = ToolCatalog([read_tool("agent.status", domain="agent")])
        state = InMemorySessionStateStore()
        model = ScriptedModel(
            [
                [
                    ModelEvent(ModelEventType.TEXT_DELTA, text="运行正常。"),
                    ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
                ],
                [
                    ModelEvent(ModelEventType.TEXT_DELTA, text="运行正常。"),
                    ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
                ],
            ]
        )
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(minimum=1, maximum=1),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
        )
        errors: list[BaseException] = []
        completed: list[list] = []

        for round_number in range(2):
            entered = threading.Event()
            release = threading.Event()

            async def hold_start_window() -> None:
                async with session._start_lock:
                    entered.set()
                    while not release.is_set():
                        await asyncio.sleep(0.001)

            async def run_session() -> None:
                completed.append(
                    await collect(
                        session.run(
                            AgentInput(
                                message="检查运行状态",
                                owner="owner-1",
                                session_id="session-1",
                                request_id=f"request-{round_number}",
                            )
                        )
                    )
                )

            holder = threading.Thread(
                target=_run_thread,
                args=(hold_start_window, errors),
            )
            waiter = threading.Thread(
                target=_run_thread,
                args=(run_session, errors),
            )
            holder.start()
            self.assertTrue(entered.wait(timeout=1))
            waiter.start()
            time.sleep(0.03)
            self.assertEqual(len(completed), round_number)
            release.set()
            holder.join(timeout=2)
            waiter.join(timeout=2)

            self.assertFalse(holder.is_alive())
            self.assertFalse(waiter.is_alive())
            self.assertEqual(len(completed), round_number + 1)
            self.assertEqual(completed[-1][-1].type, AgentEventType.TURN_COMPLETED)

        self.assertEqual(errors, [])


class AgentSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_confirmation_never_becomes_a_synthetic_user_query(self):
        model = ScriptedModel([])
        catalog, state = ToolCatalog([]), InMemorySessionStateStore()
        session = AgentSession(model=model, catalog=catalog, retriever=CapabilityRetriever(),
            pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state)
        view = await consume_events(session.confirm(owner="owner", session_id="session", plan_id=""))
        self.assertEqual(view.status, "failed")
        self.assertEqual(model.requests, [])
        self.assertEqual((await state.load(owner="owner", session_id="session")).conversation, [])

    async def test_confirm_resumes_original_task_and_stops_at_the_next_approval(self):
        await self._assert_confirmation_continues(InMemorySessionStateStore())

    async def test_persistent_confirm_resumes_and_claims_a_second_approval(self):
        from app.agent.confirmation import SQLiteConfirmationStore
        from app.agent.kernel.effects import ConfirmationEffectPlanStore
        from app.agent.kernel.persistence import SQLiteKernelStore
        from tests.support import isolated_test_database
        with isolated_test_database():
            await self._assert_confirmation_continues(SQLiteKernelStore(), ConfirmationEffectPlanStore(SQLiteConfirmationStore()))

    async def _assert_confirmation_continues(self, state, effect_store=None):
        writes = []
        tool = KernelToolSpec(
            name="cloud.change", domain="cloud", description="执行下一步云盘变更",
            input_schema={"type": "object", "properties": {"step": {"type": "integer"}}},
            effect=ToolEffect.WRITE,
            prepare=lambda a, _c: PreparedEffect(preview={"summary": f"步骤 {a['step']}"}, snapshot_fingerprint="snapshot"),
            execute_confirmed=lambda a, _s, _c: writes.append(a["step"]) or {"ok": True, "summary": f"步骤 {a['step']} 已完成"},
        )
        rounds = [[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(f"step-{step}", tool.name, {"step": step})),
                   ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")] for step in (1, 2)]
        rounds.append([ModelEvent(ModelEventType.TEXT_DELTA, text="两步任务均已完成。"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")])
        model = ScriptedModel(rounds)
        catalog = ToolCatalog([tool])
        session = AgentSession(model=model, catalog=catalog, retriever=CapabilityRetriever(),
                               pipeline=ToolPipeline(catalog=catalog, state_store=state, effect_store=effect_store), state_store=state)
        preview = await consume_events(session.run(AgentInput(message="先清洗再移动，完成后告诉我", owner="owner", session_id="session")))
        self.assertEqual(writes, [])
        events = await collect(session.confirm(owner="owner", session_id="session", plan_id=preview.approval.plan_id))
        continued = await consume_events(_events_stream(events))
        self.assertEqual(writes, [1], "后续写操作必须再次获得确认，不能自动执行")
        self.assertEqual(continued.status, "approval_required")
        self.assertIsNotNone(continued.approval)
        self.assertNotEqual(continued.approval.plan_id, preview.approval.plan_id)
        self.assertEqual(len({(e.turn_id, e.request_id) for e in events}), 1)
        self.assertEqual(sum(e.type is AgentEventType.TURN_COMPLETED for e in events), 1)
        current = await state.load(owner="owner", session_id="session")
        with self.assertRaises(StalePublicationError):
            await state.commit(PublicationLease("owner", "session", current.generation, preview.turn_id, "stale"),
                               conversation=[{"role": "assistant", "content": "旧查询不得覆盖确认后下一步"}])
        final = await consume_events(session.confirm(owner="owner", session_id="session", plan_id=continued.approval.plan_id))
        self.assertEqual(writes, [1, 2], final.to_dict())
        self.assertEqual(final.status, "success")
        self.assertEqual(final.answer, "两步任务均已完成。")
        saved = await state.load(owner="owner", session_id="session")
        self.assertEqual([r["content"] for r in saved.conversation if r["role"] == "user"], ["先清洗再移动，完成后告诉我"])
        self.assertEqual(saved.pending_effect_plan_id, "")
        marker = "当前回合是用户点击确认后的续行"
        self.assertNotIn(marker, model.requests[0].system_prompt)
        for request in model.requests[1:]:
            self.assertIn(marker, request.system_prompt)
            self.assertIn("已消费", request.system_prompt)
            executed = [row for row in request.messages if row.role == "tool" and row.tool_name == tool.name]
            self.assertTrue(executed)
            self.assertIn("已完成", executed[-1].content)
            self.assertNotIn('"status":"approval_required"', executed[-1].content)
            self.assertEqual([row.content for row in request.messages if row.role == "user"],
                             ["先清洗再移动，完成后告诉我"])
        # 授权语义只属于真实confirm回合，不能泄漏到后续普通请求。
        model.rounds.append([ModelEvent(ModelEventType.TEXT_DELTA, text="没有执行新操作。"),
                             ModelEvent(ModelEventType.FINISH, finish_reason="stop")])
        await consume_events(session.run(AgentInput(message="只看看状态，不操作", owner="owner", session_id="session")))
        self.assertNotIn(marker, model.requests[-1].system_prompt)
        self.assertEqual(writes, [1, 2])

    async def test_confirmation_resolves_only_the_bound_call_in_a_same_tool_batch(self):
        writes = []
        tool = KernelToolSpec(
            name="cloud.change", domain="cloud", description="修改文件",
            input_schema={"type": "object", "properties": {"step": {"type": "integer"}}},
            effect=ToolEffect.WRITE,
            prepare=lambda a, _c: PreparedEffect(preview={"summary": f"预览{a['step']}"}, snapshot_fingerprint="frozen"),
            execute_confirmed=lambda a, _s, _c: writes.append(a["step"]) or {"ok": True, "summary": f"已完成{a['step']}"},
        )
        model = ScriptedModel([
            [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(f"call-{step}", tool.model_name, {"step": step})) for step in (1, 2)],
            [ModelEvent(ModelEventType.TEXT_DELTA, text="第一步完成，第二步未执行。")],
        ])
        catalog, state = ToolCatalog([tool]), InMemorySessionStateStore()
        session = AgentSession(model=model, catalog=catalog, retriever=CapabilityRetriever(),
            pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state)
        preview = await consume_events(session.run(AgentInput(message="处理两步", owner="o", session_id="s")))
        before = await state.load(owner="o", session_id="s")
        pending = {row["tool_call_id"]: row for row in before.conversation if row["role"] == "tool"}
        self.assertEqual(pending["call-1"]["effect_plan_id"], preview.approval.plan_id)
        self.assertNotIn("effect_plan_id", pending["call-2"])
        final = await consume_events(session.confirm(owner="o", session_id="s", plan_id=preview.approval.plan_id))
        self.assertEqual(final.status, "success")
        self.assertEqual(writes, [1])
        for rows in (model.requests[-1].messages, session._restore_messages(await state.load(owner="o", session_id="s"))):
            by_id = {row.tool_call_id: row for row in rows if row.role == "tool"}
            self.assertIn("已完成1", by_id["call-1"].content)
            self.assertIn("not_executed_after_approval", by_id["call-2"].content)
        # 内部关联不是模型参数，不进入任一Provider的工具协议。
        from app.agent.kernel.provider_model import _history_for_protocol
        for protocol in ("chat_completions", "responses", "anthropic_messages"):
            self.assertNotIn("effect_plan_id", json.dumps(_history_for_protocol(protocol, "system", model.requests[-1].messages)))

    async def test_confirmed_internal_receipt_never_reaches_public_event_stream(self):
        tool = KernelToolSpec(
            name="local_media.retry_task",
            domain="local_media",
            description="修正季集映射",
            input_schema={"type": "object", "properties": {}},
            effect=ToolEffect.WRITE,
            prepare=lambda _a, _c: PreparedEffect(
                preview={"summary": "修正为 S02E12"},
                snapshot_fingerprint="snapshot",
            ),
            execute_confirmed=lambda _a, _s, _c: {
                "ok": True,
                "status": "accepted",
                "summary": "本地媒体任务 1 已修正为 S02E12 并重新排队",
                "data": {"operation": "remap_episode", "task_number": 1},
            },
        )
        internal = (
            "已确认操作的可信系统结果（不是待执行计划）：\n"
            '{"ok":true,"status":"accepted","data":{"private_id":99}}'
            "\n\n请求已提交，后台仍在归档；可以继续查询进度。"
        )
        model = ScriptedModel([
            [
                ModelEvent(
                    ModelEventType.TOOL_CALL_COMPLETED,
                    tool_call=ModelToolCall("write", tool.name, {}),
                ),
                ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
            ],
            [
                ModelEvent(ModelEventType.TEXT_DELTA, text=internal),
                ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
            ],
        ])
        catalog, state = ToolCatalog([tool]), InMemorySessionStateStore()
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
        )
        preview = await consume_events(session.run(AgentInput(
            message="把这个改成 S02E12", owner="owner", session_id="session",
        )))

        events = await collect(session.confirm(
            owner="owner", session_id="session", plan_id=preview.approval.plan_id,
        ))
        final = await consume_events(_events_stream(events))
        public_events = json.dumps(
            [event.to_dict() for event in events], ensure_ascii=False,
        )

        self.assertEqual(final.status, "success")
        self.assertIn("重新排队", final.answer)
        self.assertIn("后台任务尚未完成", final.answer)
        self.assertTrue(final.answer.startswith("📤 "))
        self.assertIn("可以继续查询进度", final.answer)
        self.assertNotIn("可信系统结果", public_events)
        self.assertNotIn("private_id", public_events)
        self.assertNotIn("处理完成", public_events)
        self.assertTrue(any(
            event.type is AgentEventType.MODEL_STARTED for event in events
        ))
        self.assertFalse(any(
            event.type is AgentEventType.MODEL_DELTA for event in events
        ))
        self.assertEqual(len(model.requests), 2, "已提交结果应继续交给 Agent 汇总")
        self.assertEqual(len(model.rounds), 0)
        stored = await state.load(owner="owner", session_id="session")
        internal_rows = [
            row for row in stored.conversation
            if "可信系统结果" in str(row.get("content") or "")
        ]
        self.assertEqual(len(internal_rows), 1)
        self.assertIn("public_content", internal_rows[0])
        self.assertNotIn(
            "可信系统结果", str(internal_rows[0].get("public_content") or "")
        )

    async def test_confirm_without_restored_user_returns_submitted_receipt_not_fake_completion(self):
        tool = KernelToolSpec(
            name="cloud.submit",
            domain="cloud",
            description="提交后台任务",
            input_schema={"type": "object", "properties": {}},
            effect=ToolEffect.WRITE,
            prepare=lambda _a, _c: PreparedEffect(
                preview={"summary": "提交后台任务"},
                snapshot_fingerprint="snapshot",
            ),
            execute_confirmed=lambda _a, _s, _c: ToolResult(
                True, "accepted", "后台任务已提交"
            ),
        )
        catalog, state = ToolCatalog([tool]), InMemorySessionStateStore()
        pipeline = ToolPipeline(catalog=catalog, state_store=state)
        lease, _ = await state.begin_turn(
            owner="owner", session_id="session", request_id="prepare"
        )
        preview = await pipeline.execute(
            tool.name,
            {},
            context=ToolCallContext(
                owner="owner",
                session_id="session",
                request_id="prepare",
                turn_id=lease.turn_id,
                lease=lease,
                cancellation=CancellationToken(),
                report_progress=lambda _payload: asyncio.sleep(0),
            ),
        )
        self.assertEqual(
            (await state.load(owner="owner", session_id="session")).conversation,
            [],
        )
        model = ScriptedModel([])
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(),
            pipeline=pipeline,
            state_store=state,
        )

        events = await collect(
            session.confirm(
                owner="owner",
                session_id="session",
                plan_id=preview.effect_plan.plan_id,
            )
        )
        final = await consume_events(_events_stream(events))

        self.assertEqual(final.status, "success")
        self.assertTrue(final.answer.startswith("📤 "))
        self.assertIn("后台任务尚未完成", final.answer)
        self.assertEqual(events[-1].payload["finish_reason"], "effect_submitted")
        self.assertNotEqual(events[-1].payload["status"], "effect_completed")
        self.assertEqual(model.requests, [])

    async def test_summary_failure_after_confirm_preserves_successful_write(self):
        writes = []
        tool = KernelToolSpec(name="cloud.change", domain="cloud", description="云盘变更",
            input_schema={"type": "object", "properties": {}}, effect=ToolEffect.WRITE,
            prepare=lambda _a, _c: PreparedEffect(preview={"summary": "变更"}, snapshot_fingerprint="snapshot"),
            execute_confirmed=lambda _a, _s, _c: writes.append(1) or {"ok": True, "summary": "目录移动已完成"})
        model = ScriptedModel([[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("write", tool.name, {})),
                                ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]])
        catalog, state = ToolCatalog([tool]), InMemorySessionStateStore()
        session = AgentSession(model=model, catalog=catalog, retriever=CapabilityRetriever(),
            pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state)
        preview = await consume_events(session.run(AgentInput(message="帮我移动目录", owner="owner", session_id="session")))
        final = await consume_events(session.confirm(owner="owner", session_id="session", plan_id=preview.approval.plan_id))
        self.assertEqual(writes, [1])
        self.assertEqual(final.status, "partial")
        self.assertTrue(final.effect_result["ok"])
        self.assertIn("目录移动已完成", final.answer)
        self.assertNotIn("执行失败", final.answer)

    async def test_receipt_failure_stops_followup_without_repeating_successful_write(self):
        writes = []
        tool = KernelToolSpec(name="cloud.change", domain="cloud", description="云盘变更",
            input_schema={"type": "object", "properties": {}}, effect=ToolEffect.WRITE,
            prepare=lambda _a, _c: PreparedEffect(preview={"summary": "变更"}, snapshot_fingerprint="snapshot"),
            execute_confirmed=lambda _a, _s, _c: writes.append(1) or {"ok": True, "summary": "目录移动已完成"})
        model = ScriptedModel([[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("write", tool.name, {})),
                                ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]])
        catalog, state = ToolCatalog([tool]), InMemorySessionStateStore()
        session = AgentSession(model=model, catalog=catalog, retriever=CapabilityRetriever(),
            pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state)
        preview = await consume_events(session.run(AgentInput(message="帮我移动目录", owner="owner", session_id="session")))
        from unittest.mock import patch
        commit = state.commit
        async def fail_receipt(lease, *, conversation=None, updates=()):
            if conversation and any("可信系统结果" in str(row.get("content")) for row in conversation):
                raise RuntimeError("receipt unavailable")
            return await commit(lease, conversation=conversation, updates=updates)
        with patch.object(state, "commit", side_effect=fail_receipt):
            final = await consume_events(session.confirm(owner="owner", session_id="session", plan_id=preview.approval.plan_id))
        self.assertEqual(writes, [1])
        self.assertEqual(final.status, "partial")
        self.assertTrue(final.effect_result["ok"])
        self.assertIn("目录移动已完成", final.answer)
        self.assertNotIn("执行失败", final.answer)
        self.assertIn("会话记录保存失败", final.answer)
        self.assertEqual(len(model.requests), 1, "没有可靠记录时不得继续模型规划")

    async def test_confirmation_initialization_failure_terminates_stream(self) -> None:
        class BrokenStateStore(InMemorySessionStateStore):
            async def load(self, *, owner, session_id):
                raise RuntimeError("database unavailable")

        catalog = ToolCatalog([read_tool("agent.status")])
        state = BrokenStateStore()
        session = AgentSession(
            model=ScriptedModel([]), catalog=catalog,
            retriever=CapabilityRetriever(minimum=1, maximum=1),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
        )
        reader = asyncio.create_task(collect(session.confirm(
            owner="owner-1", session_id="session-1", plan_id="plan-1"
        )))
        try:
            done, _ = await asyncio.wait([reader], timeout=0.3)
            self.assertIn(reader, done, "生产者出错后消费者仍在等待队列")
            events = await reader
            self.assertEqual(events[-1].type, AgentEventType.TURN_FAILED)
            self.assertEqual(events[-1].payload["code"], "internal_error")
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async def test_admission_failure_still_emits_a_terminal_event(self) -> None:
        class RejectingAdmission:
            async def begin(self, agent_input):
                del agent_input
                raise ToolPipelineError("Agent 身份无效", code="authorization_denied")

            async def is_current(self, token, agent_input):
                del token, agent_input
                return False

        catalog = ToolCatalog([read_tool("agent.status", domain="agent")])
        state = InMemorySessionStateStore()
        model = ScriptedModel([])
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(minimum=1, maximum=1),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
            turn_admission=RejectingAdmission(),
        )

        events = await collect(
            session.run(
                AgentInput(
                    message="你会做什么",
                    owner="invalid-owner",
                    session_id="session-1",
                )
            )
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, AgentEventType.TURN_FAILED)
        self.assertEqual(events[0].payload["code"], "authorization_denied")
        self.assertEqual(model.requests, [])

    def test_incomplete_provider_stream_has_explicit_error(self):
        for message in ("Provider 流在完成事件前中断", "Provider 回复被截断，未完整结束"):
            self.assertEqual(_provider_failure_message(ModelProviderError(message)),
                             "模型回复未完整生成，请重试；不要把截断内容视为完成结果。")

    async def test_provider_failure_has_specific_retryable_public_error(self) -> None:
        class FailingProviderModel:
            async def stream(self, request, *, cancellation):
                del request, cancellation
                raise ModelProviderError("Provider 请求失败（HTTP 503）")
                if False:  # pragma: no cover - async generator contract
                    yield None

        catalog = ToolCatalog([read_tool("agent.status", domain="agent")])
        state = InMemorySessionStateStore()
        session = AgentSession(
            model=FailingProviderModel(),
            catalog=catalog,
            retriever=CapabilityRetriever(minimum=1, maximum=1),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
        )

        events = await collect(
            session.run(
                AgentInput(
                    message="你会做什么",
                    owner="owner-1",
                    session_id="session-1",
                )
            )
        )

        self.assertEqual(events[-1].type, AgentEventType.TURN_FAILED)
        self.assertEqual(events[-1].payload["code"], "model_provider_error")
        self.assertEqual(
            events[-1].payload["message"], "模型服务暂时不可用，请稍后重试。"
        )

    async def test_single_read_uses_one_loop_and_streams_real_events(self) -> None:
        async def count(_arguments, _context):
            return {
                "ok": True,
                "status": "success",
                "summary": "媒体库中共有 37 集",
                "data": {"episodes": 37},
            }

        catalog = ToolCatalog(
            [
                read_tool(
                    "library.count_episodes",
                    description="查询媒体库指定剧集有多少集",
                    examples=("我的媒体库里有多少集",),
                    handler=count,
                )
            ]
        )
        state = InMemorySessionStateStore()
        model = ScriptedModel(
            [
                [
                    ModelEvent(
                        ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall("call-1", "library.count_episodes", {}),
                    ),
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
                ],
                [
                    ModelEvent(
                        ModelEventType.TEXT_DELTA, text="媒体库中目前共有 37 集。"
                    ),
                    ModelEvent(ModelEventType.USAGE, usage={"total_tokens": 42}),
                    ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
                ],
            ]
        )
        pipeline = ToolPipeline(catalog=catalog, state_store=state)
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(),
            pipeline=pipeline,
            state_store=state,
        )
        events = await collect(
            session.run(
                AgentInput(
                    message="我的媒体库里有多少集",
                    owner="owner-1",
                    session_id="session-1",
                )
            )
        )
        event_types = [event.type for event in events]
        self.assertEqual(event_types[0], AgentEventType.TURN_STARTED)
        self.assertIn(AgentEventType.MODEL_TOOL_CALL, event_types)
        self.assertIn(AgentEventType.TOOL_STARTED, event_types)
        self.assertIn(AgentEventType.TOOL_COMPLETED, event_types)
        self.assertIn(AgentEventType.MODEL_DELTA, event_types)
        self.assertEqual(event_types[-1], AgentEventType.TURN_COMPLETED)
        self.assertEqual(events[-1].payload["answer"], "媒体库中目前共有 37 集。")
        self.assertEqual(events[-1].payload["model_calls"], 2)
        self.assertEqual(events[-1].payload["tool_calls"], 1)
        self.assertEqual(
            [event.sequence for event in events], list(range(1, len(events) + 1))
        )
        self.assertIn("37", model.requests[1].messages[-1].content)

    async def test_exact_tool_budget_summarizes_success_and_preserves_facts_for_continue(
        self,
    ) -> None:
        observed: list[str] = []

        def read_one(_arguments, _context):
            observed.append("one")
            return {"summary": "第一项事实已读取", "data": {"item": "one"}}

        def read_two(_arguments, _context):
            observed.append("two")
            return {"summary": "第二项事实已读取", "data": {"item": "two"}}

        catalog = ToolCatalog(
            [
                read_tool(
                    "library.read_one",
                    description="读取第一项事实",
                    examples=("检查两项事实",),
                    handler=read_one,
                ),
                read_tool(
                    "library.read_two",
                    description="读取第二项事实",
                    examples=("检查两项事实",),
                    handler=read_two,
                ),
            ]
        )
        state = InMemorySessionStateStore()
        model = ScriptedModel(
            [
                [
                    ModelEvent(
                        ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall("read-1", "library.read_one", {}),
                    ),
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
                ],
                [
                    ModelEvent(
                        ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall("read-2", "library.read_two", {}),
                    ),
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
                ],
                [
                    ModelEvent(ModelEventType.TEXT_DELTA, text="两项事实均已保留。"),
                    ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
                ],
            ]
        )
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(minimum=2, maximum=2),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
            limits=SessionLimits(max_tool_calls=2),
        )

        events = await collect(
            session.run(
                AgentInput(
                    message="检查两项事实",
                    owner="owner-1",
                    session_id="session-1",
                )
            )
        )

        self.assertEqual(observed, ["one", "two"])
        self.assertEqual(tuple(model.requests[-1].tools), ())
        self.assertEqual(events[-1].type, AgentEventType.TURN_COMPLETED)
        self.assertEqual(events[-1].payload["status"], "success")
        self.assertEqual(events[-1].payload["finish_reason"], "stop")
        self.assertEqual(events[-1].payload["model_calls"], 3)
        self.assertEqual(events[-1].payload["tool_calls"], 2)

        stored = await state.load(owner="owner-1", session_id="session-1")
        self.assertEqual(stored.conversation[0]["content"], "检查两项事实")
        tool_messages = [
            item for item in stored.conversation if item.get("role") == "tool"
        ]
        self.assertEqual(
            {item.get("tool_call_id") for item in tool_messages}, {"read-1", "read-2"}
        )
        self.assertIn("第一项事实已读取", tool_messages[0]["content"])
        self.assertIn("第二项事实已读取", tool_messages[1]["content"])

        model.rounds.append(
            [
                ModelEvent(ModelEventType.TEXT_DELTA, text="可以基于保留事实继续。"),
                ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
            ]
        )
        continued = await collect(
            session.run(
                AgentInput(
                    message="继续",
                    owner="owner-1",
                    session_id="session-1",
                )
            )
        )
        self.assertEqual(continued[-1].type, AgentEventType.TURN_COMPLETED)
        self.assertTrue(
            any(
                item.role == "user" and item.content == "检查两项事实"
                for item in model.requests[-1].messages
            )
        )
        self.assertTrue(any("第一项事实已读取" in item.content for item in model.requests[-1].messages))

    async def test_over_budget_batch_with_write_is_rejected_as_a_whole(self) -> None:
        read_count = 0
        prepare_count = 0

        def read_handler(_arguments, _context):
            nonlocal read_count
            read_count += 1
            return {"summary": "预算前的读取事实"}

        def prepare(_arguments, _context):
            nonlocal prepare_count
            prepare_count += 1
            return PreparedEffect(
                preview={"summary": "不应生成写预览"},
                snapshot_fingerprint="snapshot:budget",
            )

        write = KernelToolSpec(
            name="download.pause",
            domain="download",
            description="暂停下载任务",
            examples=("检查两个能力",),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            effect=ToolEffect.WRITE,
            prepare=prepare,
            execute_confirmed=lambda _arguments, _snapshot, _context: {
                "summary": "不应执行"
            },
        )
        catalog = ToolCatalog(
            [
                read_tool(
                    "library.read_one",
                    description="读取预算前的事实",
                    examples=("检查两个能力",),
                    handler=read_handler,
                ),
                read_tool(
                    "library.read_two",
                    description="读取第二个事实",
                    examples=("检查两个能力",),
                ),
                write,
            ]
        )
        state = InMemorySessionStateStore()
        model = ScriptedModel(
            [
                [
                    ModelEvent(
                        ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall("read-1", "library.read_one", {}),
                    ),
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
                ],
                [
                    ModelEvent(
                        ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall("read-2", "library.read_two", {}),
                    ),
                    ModelEvent(
                        ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall("write-3", "download.pause", {}),
                    ),
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
                ],
                [
                    ModelEvent(ModelEventType.TEXT_DELTA, text="预算内事实已保留。"),
                    ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
                ],
            ]
        )
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(minimum=3, maximum=3),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
            limits=SessionLimits(max_tool_calls=2),
        )

        events = await collect(
            session.run(
                AgentInput(
                    message="检查两个能力",
                    owner="owner-1",
                    session_id="session-1",
                )
            )
        )

        self.assertEqual(read_count, 1)
        self.assertEqual(prepare_count, 0)
        self.assertEqual(events[-1].payload["status"], "partial")
        self.assertEqual(events[-1].payload["finish_reason"], "tool_budget_exceeded")
        self.assertEqual(tuple(model.requests[-1].tools), ())
        failed = {
            event.payload["call_id"]: event
            for event in events
            if event.type is AgentEventType.TOOL_FAILED
        }
        self.assertEqual(
            {failed["read-2"].payload["code"], failed["write-3"].payload["code"]},
            {"tool_budget_exceeded"},
        )
        self.assertFalse(
            any(
                event.type is AgentEventType.TOOL_STARTED
                and event.payload.get("call_id") in {"read-2", "write-3"}
                for event in events
            )
        )
        self.assertFalse(
            any(
                event.type is AgentEventType.EFFECT_PREVIEW_STARTED
                and event.payload.get("call_id") in {"read-2", "write-3"}
                for event in events
            )
        )

        stored = await state.load(owner="owner-1", session_id="session-1")
        assistant_calls = [
            call
            for item in stored.conversation
            if item.get("role") == "assistant"
            for call in item.get("tool_calls") or ()
        ]
        result_ids = {
            item.get("tool_call_id")
            for item in stored.conversation
            if item.get("role") == "tool"
        }
        self.assertEqual(
            {call["call_id"] for call in assistant_calls},
            {"read-1", "read-2", "write-3"},
        )
        self.assertEqual(result_ids, {"read-1", "read-2", "write-3"})
        self.assertTrue(
            all(
                "tool_budget_exceeded" in item["content"]
                for item in stored.conversation
                if item.get("tool_call_id") in {"read-2", "write-3"}
            )
        )

    async def test_single_model_round_preserves_reads_and_rejects_over_budget_batches(self) -> None:
        for requested, tool_limit, executed, reason in (
            (1, 2, 1, "model_round_budget_exceeded"),
            (2, 1, 0, "tool_budget_exceeded"),
        ):
            with self.subTest(requested=requested):
                observed = []
                catalog = ToolCatalog([read_tool("library.status", handler=lambda *_: observed.append("read") or {"summary": "读取事实"})])
                state = InMemorySessionStateStore()
                model = ScriptedModel([[
                    *[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(f"call-{i}", "library.status", {})) for i in range(requested)],
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
                ]])
                session = AgentSession(
                    model=model, catalog=catalog, retriever=CapabilityRetriever(minimum=1, maximum=1),
                    pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state,
                    limits=SessionLimits(max_model_rounds=1, max_tool_calls=tool_limit),
                )
                events = await collect(session.run(AgentInput(owner="owner", session_id="single", message="查询状态")))
                self.assertEqual(len(observed), executed)
                self.assertEqual(len(model.requests), 1)
                self.assertTrue(model.requests[0].tools)
                self.assertEqual(events[-1].type, AgentEventType.TURN_COMPLETED)
                self.assertEqual(events[-1].payload["status"], "partial")
                self.assertEqual(events[-1].payload["finish_reason"], reason)
                stored = await state.load(owner="owner", session_id="single")
                results = [item for item in stored.conversation if item["role"] == "tool"]
                self.assertEqual(len(results), requested)

    async def test_context_window_drops_oldest_complete_turns_only(self) -> None:
        catalog = ToolCatalog([read_tool("library.status")])
        state = InMemorySessionStateStore()
        model = ScriptedModel([])
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
            limits=SessionLimits(
                max_output_tokens=1_024,
                context_window_tokens=16_384,
            ),
        )
        messages = [
            ModelMessage(role="user", content="old:" + "a" * 100_000),
            ModelMessage(role="assistant", content="old answer"),
            ModelMessage(role="user", content="recent:" + "b" * 4_000),
            ModelMessage(role="assistant", content="recent answer"),
            ModelMessage(role="user", content="current question"),
        ]

        bounded = bounded_model_messages(
            messages,
            system_prompt=session.system_prompt,
            context_window_tokens=session.limits.context_window_tokens,
            output_tokens=session.limits.effective_output_tokens,
            history_end=4,
            tool_definitions=(catalog.get("library.status").model_definition(),),
        )

        self.assertEqual(bounded[-1].content, "current question")
        self.assertTrue(any(item.content.startswith("recent:") for item in bounded))
        self.assertNotIn("old answer", [item.content for item in bounded])
        self.assertEqual(bounded[0].role, "user")

    async def test_latest_oversized_history_turn_is_compacted_not_dropped(self) -> None:
        catalog = ToolCatalog([read_tool("library.status")])
        state = InMemorySessionStateStore()
        session = AgentSession(
            model=ScriptedModel([]),
            catalog=catalog,
            retriever=CapabilityRetriever(),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
            limits=SessionLimits(
                max_output_tokens=1_024,
                context_window_tokens=16_384,
            ),
        )
        messages = [
            ModelMessage(role="user", content="完整读取狐妖小红娘并给方案"),
            ModelMessage(
                role="assistant",
                tool_calls=(
                    ModelToolCall(
                        "large-call",
                        "library.status",
                        {"objects": ["OBJ" + "A" * 24] * 2_000},
                    ),
                ),
            ),
            ModelMessage(
                role="tool",
                tool_call_id="large-call",
                tool_name="library.status",
                content='{"ok":true,"status":"success","summary":"完整读取完成","data":"'
                + "x" * 100_000
                + '"}',
            ),
            ModelMessage(role="assistant", content="方案 A：按 TMDB 分季规整"),
            ModelMessage(role="user", content="方案 A"),
        ]

        bounded = bounded_model_messages(
            messages,
            system_prompt=session.system_prompt,
            context_window_tokens=session.limits.context_window_tokens,
            output_tokens=session.limits.effective_output_tokens,
            history_end=4,
            tool_definitions=(catalog.get("library.status").model_definition(),),
        )

        self.assertEqual(bounded[-1].content, "方案 A")
        self.assertTrue(any("完整读取狐妖小红娘" in item.content for item in bounded))
        self.assertTrue(any("方案 A：按 TMDB" in item.content for item in bounded))
        historical_call = next(item for item in bounded if item.tool_calls)
        self.assertEqual(dict(historical_call.tool_calls[0].arguments), {})

    async def test_compacted_failed_tool_keeps_failure_fact(self) -> None:
        compact = compact_tool_content(
            '{"ok":false,"status":"error","code":"precondition_failed",'
            '"error":"观察快照已过期"}',
            maximum=240,
        )

        self.assertIn('"ok":false', compact)
        self.assertIn("观察快照已过期", compact)
        self.assertIn("precondition_failed", compact)
        self.assertNotIn("工具执行完成", compact)

    async def test_final_model_round_is_synthesis_only_and_never_executes_calls(
        self,
    ) -> None:
        calls = 0

        def handler(_arguments, _context):
            nonlocal calls
            calls += 1
            return {"summary": "读取完成", "data": {"count": 200}}

        catalog = ToolCatalog([read_tool("library.status", handler=handler)])
        state = InMemorySessionStateStore()
        model = ScriptedModel(
            [
                [
                    ModelEvent(
                        ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall("read-1", "library.status", {}),
                    ),
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
                ],
                [
                    ModelEvent(
                        ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall("late-call", "library.status", {}),
                    ),
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
                ],
            ]
        )
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(minimum=1, maximum=1),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
            limits=SessionLimits(max_model_rounds=2),
        )

        events = await collect(
            session.run(
                AgentInput(
                    message="完整读取后汇总",
                    owner="owner-1",
                    session_id="session-1",
                )
            )
        )

        self.assertEqual(calls, 1)
        self.assertTrue(model.requests[0].tools)
        self.assertEqual(tuple(model.requests[1].tools), ())
        self.assertEqual(events[-1].type, AgentEventType.TURN_COMPLETED)
        self.assertEqual(events[-1].payload["status"], "partial")
        self.assertEqual(
            events[-1].payload["finish_reason"], "model_round_budget_exceeded"
        )
        failed = [event for event in events if event.type is AgentEventType.TOOL_FAILED]
        self.assertEqual(failed[-1].payload["code"], "not_executed_final_round")
        stored = await state.load(owner="owner-1", session_id="session-1")
        self.assertIn("部分完成", stored.conversation[-1]["content"])
        assistant_calls = [
            call
            for item in stored.conversation
            if item.get("role") == "assistant"
            for call in item.get("tool_calls") or ()
        ]
        self.assertEqual(
            {call["call_id"] for call in assistant_calls}, {"read-1", "late-call"}
        )
        self.assertEqual(
            {
                item.get("tool_call_id")
                for item in stored.conversation
                if item.get("role") == "tool"
            },
            {"read-1", "late-call"},
        )
        late_result = next(
            item for item in stored.conversation if item.get("tool_call_id") == "late-call"
        )
        self.assertIn("not_executed_final_round", late_result["content"])

    async def test_reply_context_is_available_to_model_but_not_persisted_as_chat_text(
        self,
    ) -> None:
        catalog = ToolCatalog([read_tool("library.status")])
        state = InMemorySessionStateStore()
        model = ScriptedModel(
            [
                [
                    ModelEvent(
                        ModelEventType.TEXT_DELTA, text="这是上一条提到的剧集。"
                    ),
                    ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
                ]
            ]
        )
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
        )
        await collect(
            session.run(
                AgentInput(
                    message="它有多少集？",
                    owner="owner",
                    session_id="session",
                    reply_context={"text": "光阴之外目前已入库 37 集"},
                )
            )
        )
        self.assertIn("光阴之外", model.requests[0].messages[-1].content)
        persisted = await state.load(owner="owner", session_id="session")
        user = next(item for item in persisted.conversation if item["role"] == "user")
        self.assertEqual(user["content"], "它有多少集？")

    async def test_tool_error_is_returned_to_model_for_self_correction(self) -> None:
        catalog = ToolCatalog([read_tool("library.status")])
        state = InMemorySessionStateStore()
        model = ScriptedModel(
            [
                [
                    ModelEvent(
                        ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall("bad", "cloud.not_selected", {}),
                    ),
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
                ],
                [
                    ModelEvent(
                        ModelEventType.TEXT_DELTA, text="当前没有可用的云盘读取能力。"
                    ),
                    ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
                ],
            ]
        )
        pipeline = ToolPipeline(catalog=catalog, state_store=state)
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(),
            pipeline=pipeline,
            state_store=state,
        )
        events = await collect(
            session.run(
                AgentInput(
                    message="看看云盘",
                    owner="owner-1",
                    session_id="session-1",
                )
            )
        )
        failed = [event for event in events if event.type is AgentEventType.TOOL_FAILED]
        self.assertEqual(failed[0].payload["code"], "tool_not_available")
        self.assertEqual(events[-1].type, AgentEventType.TURN_COMPLETED)
        self.assertIn("tool_not_available", model.requests[1].messages[-1].content)

    async def test_write_only_freezes_plan_and_confirm_continues_after_deterministic_execution(
        self,
    ) -> None:
        calls = {"execute": 0, "verify": 0}

        def prepare(arguments, _context):
            return PreparedEffect(
                preview={
                    "ok": True,
                    "status": "preview",
                    "summary": f"将暂停任务 {arguments['task_id']}",
                },
                snapshot_fingerprint="snapshot:v1",
            )

        def execute(arguments, expected_snapshot, _context):
            self.assertEqual(expected_snapshot, "snapshot:v1")
            calls["execute"] += 1
            return {
                "ok": True,
                "status": "success",
                "summary": f"已暂停任务 {arguments['task_id']}",
            }

        def verify(_arguments, value, _context):
            calls["verify"] += 1
            return value

        tool = KernelToolSpec(
            name="download.pause",
            domain="download",
            description="暂停下载任务",
            examples=("暂停这个下载",),
            input_schema={
                "type": "object",
                "required": ["task_id"],
                "properties": {"task_id": {"type": "string", "minLength": 1}},
                "additionalProperties": False,
            },
            effect=ToolEffect.WRITE,
            prepare=prepare,
            execute_confirmed=execute,
            verify=verify,
        )
        catalog = ToolCatalog([tool])
        state = InMemorySessionStateStore()
        model = ScriptedModel(
            [
                [
                    ModelEvent(
                        ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall(
                            "write-1", "download.pause", {"task_id": "job-7"}
                        ),
                    ),
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
                ],
                [
                    ModelEvent(
                        ModelEventType.TEXT_DELTA,
                        text="job-7 已暂停。",
                    ),
                    ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
                ],
            ]
        )
        pipeline = ToolPipeline(catalog=catalog, state_store=state)
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(),
            pipeline=pipeline,
            state_store=state,
            limits=SessionLimits(max_model_rounds=1),
        )
        preview_events = await collect(
            session.run(
                AgentInput(
                    message="暂停 job-7",
                    owner="owner-1",
                    session_id="session-1",
                )
            )
        )
        self.assertEqual(calls["execute"], 0)
        approval = next(
            event
            for event in preview_events
            if event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED
        )
        plan_id = approval.payload["plan"]["plan_id"]
        self.assertEqual(preview_events[-1].payload["status"], "approval_required")
        model_call_count = len(model.requests)

        confirmed_events = await collect(
            session.confirm(
                owner="owner-1",
                session_id="session-1",
                plan_id=plan_id,
            )
        )
        self.assertEqual(calls, {"execute": 1, "verify": 1})
        self.assertEqual(len(model.requests), model_call_count + 1)
        self.assertIn(
            AgentEventType.EFFECT_COMPLETED,
            [event.type for event in confirmed_events],
        )
        self.assertEqual(confirmed_events[-1].payload["status"], "success")
        stored = await state.load(owner="owner-1", session_id="session-1")
        confirmed_result = next(row for row in stored.conversation if "可信系统结果" in row.get("content", ""))
        self.assertIn("可信系统结果", confirmed_result["content"])
        self.assertEqual(confirmed_result["public_content"], "✅ 已暂停任务 job-7")

        model.rounds.append([ModelEvent(ModelEventType.TEXT_DELTA, text="job-7 已暂停。"),
                             ModelEvent(ModelEventType.FINISH, finish_reason="stop")])
        await collect(
            session.run(
                AgentInput(
                    message="它现在怎么样？",
                    owner="owner-1",
                    session_id="session-1",
                )
            )
        )
        next_turn_history = "\n".join(
            item.content for item in model.requests[-1].messages
        )
        self.assertIn("可信系统结果", next_turn_history)
        self.assertIn("已暂停任务 job-7", next_turn_history)

        replay = await collect(
            session.confirm(
                owner="owner-1",
                session_id="session-1",
                plan_id=plan_id,
            )
        )
        self.assertEqual(calls["execute"], 1)
        self.assertNotIn(AgentEventType.EFFECT_FAILED, [event.type for event in replay])
        self.assertNotIn(AgentEventType.TOOL_STARTED, [event.type for event in replay])
        self.assertEqual(replay[-1].type, AgentEventType.TURN_FAILED)

    async def test_claimed_execution_failure_keeps_cause_and_clears_pending_plan(self) -> None:
        # 同一个 confirmation_* 错误码既可能来自领票，也可能来自真实执行；
        # 已领票的领域失败必须作为终态交付，而不是被 TG 当成重复点击吞掉。
        for code in ("confirmation_stale", "confirmation_invalid", "precondition_failed"):
            with self.subTest(code=code):
                calls = []
                reason = "光鸭变更预览已过期，请重新生成"

                def execute(_arguments, _snapshot, _context):
                    calls.append("execute")
                    raise ToolPipelineError(reason, code=code)

                tool = KernelToolSpec(
                    name="cloud.relocate", domain="cloud", description="云盘文件移动",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    effect=ToolEffect.WRITE,
                    prepare=lambda _a, _c: PreparedEffect(
                        preview={"summary": "预览视频清洗与移动"},
                        snapshot_fingerprint="frozen-directory",
                    ),
                    execute_confirmed=execute,
                )
                catalog = ToolCatalog([tool])
                state = InMemorySessionStateStore()
                model = ScriptedModel([[
                    ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                               tool_call=ModelToolCall("move", tool.name, {})),
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
                ]])
                session = AgentSession(
                    model=model, catalog=catalog, retriever=CapabilityRetriever(),
                    pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state,
                )
                preview = await consume_events(session.run(AgentInput(
                    message="手动清洗云盘视频到目标目录", owner="owner", session_id="session",
                )))
                self.assertEqual(calls, [])
                self.assertIsNotNone(preview.approval)
                plan_id = preview.approval.plan_id
                confirmed = await consume_events(session.confirm(
                    owner="owner", session_id="session", plan_id=plan_id,
                ))
                self.assertEqual(calls, ["execute"])
                self.assertEqual(confirmed.status, "failed")
                self.assertEqual(confirmed.error_code, code)
                self.assertEqual(confirmed.error_message, reason)
                self.assertEqual(dict(confirmed.effect_result), {
                    "ok": False, "status": code, "summary": reason,
                })
                saved = await state.load(owner="owner", session_id="session")
                self.assertEqual(saved.pending_effect_plan_id, "")
                self.assertIn("光鸭变更预览已过期", saved.conversation[-1]["public_content"])
                self.assertIn("请重新生成", saved.conversation[-1]["public_content"])
                self.assertEqual(len(model.requests), 1, "确认结果不得再依赖模型")

                previous_conversation = saved.conversation
                replay = await consume_events(session.confirm(
                    owner="owner", session_id="session", plan_id=plan_id,
                ))
                self.assertEqual(calls, ["execute"], "失败重放不得二次执行")
                self.assertEqual(dict(replay.effect_result), {})
                self.assertEqual((await state.load(owner="owner", session_id="session")).conversation,
                                 previous_conversation, "重复点击不得覆盖已记录的失败原因")

    async def test_completed_read_survives_provider_failure_for_rebuilt_session(self) -> None:
        class FailsAfterRead(ScriptedModel):
            async def stream(self, request, *, cancellation):
                if self.requests:
                    self.requests.append(request)
                    raise ModelProviderError("simulated upstream timeout")
                async for event in super().stream(request, cancellation=cancellation):
                    yield event

        catalog = ToolCatalog([
            read_tool(
                "library.status",
                handler=lambda _arguments, _context: {
                    "summary": "第一部已检查",
                    "data": {"proof": "completed-fact-unique-7788"},
                },
            )
        ])
        state = InMemorySessionStateStore()
        model = FailsAfterRead([
            [
                ModelEvent(
                    ModelEventType.TOOL_CALL_COMPLETED,
                    tool_call=ModelToolCall("read-1", "library.status", {}),
                ),
                ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
            ]
        ])
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(minimum=1, maximum=1),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
        )

        events = await collect(
            session.run(
                AgentInput(
                    message="查询媒体库第一部",
                    owner="owner-1",
                    session_id="session-1",
                )
            )
        )
        self.assertEqual(events[-1].type, AgentEventType.TURN_FAILED)
        self.assertEqual(events[-1].payload["code"], "model_provider_error")

        saved = await state.load(owner="owner-1", session_id="session-1")
        assistant = next(item for item in saved.conversation if item.get("tool_calls"))
        results = [
            item for item in saved.conversation if item.get("role") == "tool"
        ]
        self.assertEqual(
            {call["call_id"] for call in assistant["tool_calls"]},
            {item["tool_call_id"] for item in results},
        )
        self.assertIn("completed-fact-unique-7788", results[0]["content"])

        rebuilt_model = ScriptedModel([
            [
                ModelEvent(ModelEventType.TEXT_DELTA, text="可以基于已保存事实继续。"),
                ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
            ]
        ])
        rebuilt = AgentSession(
            model=rebuilt_model,
            catalog=catalog,
            retriever=CapabilityRetriever(minimum=1, maximum=1),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
        )
        continued = await collect(
            rebuilt.run(
                AgentInput(
                    message="继续",
                    owner="owner-1",
                    session_id="session-1",
                )
            )
        )
        self.assertEqual(continued[-1].type, AgentEventType.TURN_COMPLETED)
        history = "\n".join(item.content for item in rebuilt_model.requests[0].messages)
        self.assertIn("completed-fact-unique-7788", history)

    async def test_cancelled_batch_preserves_outcome_and_closes_tool_protocol(self) -> None:
        second_started = asyncio.Event()

        async def second_read(_arguments, context):
            second_started.set()
            while not context.cancellation.cancelled:
                await asyncio.sleep(0)
            context.cancellation.raise_if_cancelled()

        catalog = ToolCatalog([
            read_tool(
                "library.first",
                description="读取第一项",
                examples=("检查一批",),
                handler=lambda _arguments, _context: {
                    "summary": "第一项完成",
                    "data": {"proof": "batch-first-7788"},
                },
            ),
            read_tool(
                "library.second",
                description="读取第二项",
                examples=("检查一批",),
                handler=second_read,
            ),
        ])
        state = InMemorySessionStateStore()
        model = ScriptedModel([
            [
                ModelEvent(
                    ModelEventType.TOOL_CALL_COMPLETED,
                    tool_call=ModelToolCall("first-call", "library.first", {}),
                ),
                ModelEvent(
                    ModelEventType.TOOL_CALL_COMPLETED,
                    tool_call=ModelToolCall("second-call", "library.second", {}),
                ),
                ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
            ]
        ])
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(minimum=2, maximum=2),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
        )
        task = asyncio.create_task(
            collect(
                session.run(
                    AgentInput(
                        message="检查一批",
                        owner="owner-1",
                        session_id="session-1",
                    )
                )
            )
        )
        await asyncio.wait_for(second_started.wait(), timeout=1)
        self.assertTrue(
            await session.cancel(owner="owner-1", session_id="session-1")
        )
        events = await asyncio.wait_for(task, timeout=1)
        self.assertEqual(events[-1].type, AgentEventType.TURN_CANCELLED)

        saved = await state.load(owner="owner-1", session_id="session-1")
        assistant = next(item for item in saved.conversation if item.get("tool_calls"))
        call_ids = {call["call_id"] for call in assistant["tool_calls"]}
        results = [
            item for item in saved.conversation if item.get("role") == "tool"
        ]
        self.assertEqual(call_ids, {item["tool_call_id"] for item in results})
        first_result = next(item for item in results if item["tool_call_id"] == "first-call")
        second_result = next(item for item in results if item["tool_call_id"] == "second-call")
        self.assertIn("batch-first-7788", first_result["content"])
        self.assertIn("result_unknown", second_result["content"])
        self.assertIn('"ok":false', second_result["content"])

    async def test_sensitive_input_cancelled_before_failure_is_not_persisted(self) -> None:
        secret = "api_key=sk-ThisIsAFakeCredential1234567890"
        cancelled: list[bool] = []

        class CancellingJournal:
            async def append(self, event, *, owner):
                del owner
                if event.type is AgentEventType.TURN_STARTED:
                    cancelled.append(
                        await session.cancel(
                            owner="owner-1", session_id="session-1"
                        )
                    )

        catalog = ToolCatalog([read_tool("agent.status", domain="agent")])
        state = InMemorySessionStateStore()
        model = ScriptedModel([])
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(minimum=1, maximum=1),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
            journal=CancellingJournal(),
        )

        events = await collect(
            session.run(
                AgentInput(
                    message=secret,
                    owner="owner-1",
                    session_id="session-1",
                )
            )
        )

        self.assertEqual(cancelled, [True])
        self.assertEqual(events[0].type, AgentEventType.TURN_STARTED)
        self.assertEqual(events[-1].type, AgentEventType.TURN_CANCELLED)
        self.assertEqual(model.requests, [])
        saved = await state.load(owner="owner-1", session_id="session-1")
        self.assertEqual(saved.conversation, [])
        self.assertNotIn(secret, str(events))

    async def test_context_hard_limit_fails_before_provider_call(self) -> None:
        catalog = ToolCatalog([read_tool("library.status")])
        state = InMemorySessionStateStore()
        model = ScriptedModel([])
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
            limits=SessionLimits(
                max_output_tokens=1_024,
                context_window_tokens=16_384,
            ),
            system_prompt="系统" * 3_000,
        )

        events = await collect(
            session.run(
                AgentInput(
                    message="界" * 12_000,
                    owner="owner-1",
                    session_id="session-1",
                )
            )
        )

        self.assertEqual(model.requests, [])
        self.assertEqual(events[-1].type, AgentEventType.TURN_FAILED)
        self.assertEqual(events[-1].payload["code"], "context_budget_exceeded")

    async def test_preview_publication_failure_discards_frozen_effect(self) -> None:
        class FailingCommitStateStore(InMemorySessionStateStore):
            async def commit(self, lease, *, conversation=None, updates=()):
                del lease, conversation, updates
                raise RuntimeError("commit failed")

        class RecordingLifecycle:
            def __init__(self):
                self.cancelled_plans = []

            def prepared(self, *, tool, arguments, prepared, context):
                del tool, arguments, context
                return prepared

            def prepare_failed(self, *, prepared, context):
                del prepared, context

            def completed(self, *, plan, value, elapsed_ms):
                del plan, value, elapsed_ms

            def failed(self, *, plan, code, elapsed_ms):
                del plan, code, elapsed_ms

            def interrupted(self, *, plan):
                del plan

            def cancelled(self, *, plan):
                self.cancelled_plans.append(plan)

        tool = KernelToolSpec(
            name="download.pause",
            domain="download",
            description="暂停下载任务",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            effect=ToolEffect.WRITE,
            prepare=lambda _arguments, _context: PreparedEffect(
                preview={"summary": "将暂停"},
                snapshot_fingerprint="snapshot:v1",
            ),
            execute_confirmed=lambda _arguments, _snapshot, _context: {
                "summary": "已暂停"
            },
        )
        catalog = ToolCatalog([tool])
        state = FailingCommitStateStore()
        lifecycle = RecordingLifecycle()
        pipeline = ToolPipeline(
            catalog=catalog,
            state_store=state,
            effect_lifecycle=lifecycle,
        )
        lease, _ = await state.begin_turn(
            owner="owner-1", session_id="session-1", request_id="request-1"
        )

        async def progress(_payload):
            return None

        context = ToolCallContext(
            owner="owner-1",
            session_id="session-1",
            request_id="request-1",
            turn_id=lease.turn_id,
            lease=lease,
            cancellation=CancellationToken(),
            report_progress=progress,
        )

        with self.assertRaisesRegex(RuntimeError, "commit failed"):
            await pipeline.execute("download.pause", {}, context=context)
        self.assertEqual(len(lifecycle.cancelled_plans), 1)
        self.assertEqual(lifecycle.cancelled_plans[0].tool_name, "download.pause")

    async def test_new_turn_discards_unconfirmed_effect_without_leaving_stale_state(
        self,
    ) -> None:
        tool = KernelToolSpec(
            name="download.pause",
            domain="download",
            description="暂停下载任务",
            examples=("暂停下载",),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            effect=ToolEffect.WRITE,
            prepare=lambda _arguments, _context: PreparedEffect(
                preview={"summary": "将暂停下载任务"},
                snapshot_fingerprint="snapshot:pending",
            ),
            execute_confirmed=lambda _arguments, _snapshot, _context: {
                "summary": "已暂停"
            },
        )
        catalog = ToolCatalog([tool])
        state = InMemorySessionStateStore()
        model = ScriptedModel(
            [
                [
                    ModelEvent(
                        ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall("write", "download.pause", {}),
                    ),
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
                ],
                [
                    ModelEvent(ModelEventType.TEXT_DELTA, text="当前没有其他问题。"),
                    ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
                ],
            ]
        )
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
        )
        preview = await collect(
            session.run(
                AgentInput(
                    message="暂停下载",
                    owner="owner",
                    session_id="session",
                )
            )
        )
        plan_id = next(
            event.payload["plan"]["plan_id"]
            for event in preview
            if event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED
        )
        before = await state.load(owner="owner", session_id="session")
        self.assertEqual(before.pending_effect_plan_id, plan_id)

        second = await collect(
            session.run(
                AgentInput(
                    message="算了，查看别的内容",
                    owner="owner",
                    session_id="session",
                )
            )
        )
        self.assertEqual(second[-1].payload["status"], "success")
        after = await state.load(owner="owner", session_id="session")
        self.assertEqual(after.pending_effect_plan_id, "")

        stale = await collect(
            session.confirm(
                owner="owner",
                session_id="session",
                plan_id=plan_id,
            )
        )
        self.assertNotIn(AgentEventType.EFFECT_FAILED, [event.type for event in stale])
        self.assertNotIn(AgentEventType.TOOL_STARTED, [event.type for event in stale])
        self.assertEqual(stale[-1].type, AgentEventType.TURN_FAILED)

    async def test_confirmed_effect_cannot_be_cancelled_or_superseded_by_new_chat(
        self,
    ) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = {"execute": 0}

        def prepare(_arguments, _context):
            return PreparedEffect(
                preview={"summary": "将执行写操作"},
                snapshot_fingerprint="snapshot:protected",
            )

        async def execute(_arguments, expected_snapshot, _context):
            self.assertEqual(expected_snapshot, "snapshot:protected")
            calls["execute"] += 1
            entered.set()
            await release.wait()
            return {"summary": "写操作完成"}

        tool = KernelToolSpec(
            name="download.pause",
            domain="download",
            description="暂停下载任务",
            examples=("暂停下载",),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            effect=ToolEffect.WRITE,
            prepare=prepare,
            execute_confirmed=execute,
        )
        catalog = ToolCatalog([tool])
        state = InMemorySessionStateStore()
        model = ScriptedModel(
            [
                [
                    ModelEvent(
                        ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall("write", "download.pause", {}),
                    ),
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
                ]
            ]
        )
        model.rounds.append([ModelEvent(ModelEventType.TEXT_DELTA, text="写操作完成。"),
                             ModelEvent(ModelEventType.FINISH, finish_reason="stop")])
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
        )
        preview = await collect(
            session.run(
                AgentInput(
                    message="暂停下载",
                    owner="owner",
                    session_id="session",
                )
            )
        )
        approval = next(
            event
            for event in preview
            if event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED
        )
        plan_id = approval.payload["plan"]["plan_id"]

        confirmation_task = asyncio.create_task(
            collect(
                session.confirm(
                    owner="owner",
                    session_id="session",
                    plan_id=plan_id,
                )
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        executing = await state.load(owner="owner", session_id="session")
        self.assertEqual(executing.pending_effect_plan_id, "")
        self.assertEqual(
            executing.metadata.get("confirmed_publication", {}).get("plan_id"),
            plan_id,
        )
        self.assertFalse(await session.cancel(owner="owner", session_id="session"))
        blocked = await collect(
            session.run(
                AgentInput(
                    message="执行期间再问一个问题",
                    owner="owner",
                    session_id="session",
                )
            )
        )
        self.assertEqual(blocked[-1].type, AgentEventType.TURN_FAILED)
        self.assertEqual(blocked[-1].payload["code"], "effect_in_progress")
        self.assertEqual(len(model.requests), 1)

        release.set()
        confirmed = await asyncio.wait_for(confirmation_task, timeout=1)
        self.assertEqual(calls["execute"], 1)
        self.assertEqual(confirmed[-1].payload["status"], "success")

    async def test_confirmed_sync_effect_survives_stream_consumer_disconnect(
        self,
    ) -> None:
        entered = threading.Event()
        release = threading.Event()
        executed = threading.Event()

        class RecordingLifecycle:
            def __init__(self) -> None:
                self.completed_plans = []
                self.interrupted_plans = []

            def prepared(self, *, tool, arguments, prepared, context):
                del tool, arguments, context
                return prepared

            def prepare_failed(self, *, prepared, context):
                del prepared, context

            def completed(self, *, plan, value, elapsed_ms):
                del value, elapsed_ms
                self.completed_plans.append(plan)

            def failed(self, *, plan, code, elapsed_ms):
                del plan, code, elapsed_ms

            def interrupted(self, *, plan):
                self.interrupted_plans.append(plan)

            def cancelled(self, *, plan):
                del plan

        def execute(_arguments, expected_snapshot, _context):
            self.assertEqual(expected_snapshot, "snapshot:disconnect-safe")
            entered.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test release timeout")
            executed.set()
            return {"summary": "断流后写操作仍完成"}

        tool = KernelToolSpec(
            name="download.pause",
            domain="download",
            description="暂停下载任务",
            examples=("暂停下载",),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            effect=ToolEffect.WRITE,
            prepare=lambda _arguments, _context: PreparedEffect(
                preview={"summary": "将暂停下载任务"},
                snapshot_fingerprint="snapshot:disconnect-safe",
            ),
            execute_confirmed=execute,
        )
        catalog = ToolCatalog([tool])
        state = InMemorySessionStateStore()
        lifecycle = RecordingLifecycle()
        session = AgentSession(
            model=ScriptedModel(
                [
                    [
                        ModelEvent(
                            ModelEventType.TOOL_CALL_COMPLETED,
                            tool_call=ModelToolCall(
                                "write", "download.pause", {}
                            ),
                        ),
                        ModelEvent(
                            ModelEventType.FINISH, finish_reason="tool_calls"
                        ),
                    ]
                ]
            ),
            catalog=catalog,
            retriever=CapabilityRetriever(minimum=1, maximum=1),
            pipeline=ToolPipeline(
                catalog=catalog,
                state_store=state,
                effect_lifecycle=lifecycle,
            ),
            state_store=state,
        )
        preview = await collect(
            session.run(
                AgentInput(
                    message="暂停下载",
                    owner="owner",
                    session_id="session",
                )
            )
        )
        plan_id = next(
            event.payload["plan"]["plan_id"]
            for event in preview
            if event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED
        )

        stream = session.confirm(
            owner="owner",
            session_id="session",
            plan_id=plan_id,
        )
        self.assertEqual((await anext(stream)).type, AgentEventType.TURN_STARTED)
        self.assertEqual((await anext(stream)).type, AgentEventType.TOOL_STARTED)
        self.assertTrue(
            await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), timeout=1.5)
        )
        await stream.aclose()
        release.set()
        self.assertTrue(
            await asyncio.wait_for(asyncio.to_thread(executed.wait, 1), timeout=1.5)
        )

        for _ in range(100):
            current = await state.load(owner="owner", session_id="session")
            if lifecycle.completed_plans and not session._detached_tasks:
                break
            await asyncio.sleep(0.01)
        current = await state.load(owner="owner", session_id="session")
        self.assertEqual(current.pending_effect_plan_id, "")
        self.assertEqual(len(lifecycle.completed_plans), 1)
        self.assertEqual(lifecycle.interrupted_plans, [])
        self.assertFalse(session._detached_tasks)
        self.assertIn("断流后写操作仍完成", current.conversation[-1]["content"])

    async def test_calls_after_write_preview_are_closed_without_execution(
        self,
    ) -> None:
        read_calls = 0

        def read_handler(_arguments, _context):
            nonlocal read_calls
            read_calls += 1
            return {"summary": "读取完成"}

        write_tool = KernelToolSpec(
            name="download.pause",
            domain="download",
            description="暂停下载任务",
            examples=("暂停并查看状态",),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            effect=ToolEffect.WRITE,
            prepare=lambda _arguments, _context: PreparedEffect(
                preview={"summary": "将暂停下载任务"},
                snapshot_fingerprint="snapshot:multi-call",
            ),
            execute_confirmed=lambda _arguments, _snapshot, _context: {
                "summary": "已暂停"
            },
        )
        read_status = read_tool(
            "download.status",
            domain="download",
            description="读取下载状态",
            examples=("暂停并查看状态",),
            handler=read_handler,
        )
        catalog = ToolCatalog([write_tool, read_status])
        state = InMemorySessionStateStore()
        session = AgentSession(
            model=ScriptedModel(
                [
                    [
                        ModelEvent(
                            ModelEventType.TOOL_CALL_COMPLETED,
                            tool_call=ModelToolCall(
                                "write-1", "download.pause", {}
                            ),
                        ),
                        ModelEvent(
                            ModelEventType.TOOL_CALL_COMPLETED,
                            tool_call=ModelToolCall(
                                "read-2", "download.status", {}
                            ),
                        ),
                        ModelEvent(
                            ModelEventType.FINISH, finish_reason="tool_calls"
                        ),
                    ]
                ]
            ),
            catalog=catalog,
            retriever=CapabilityRetriever(minimum=2, maximum=2),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
        )

        events = await collect(
            session.run(
                AgentInput(
                    message="暂停并查看状态",
                    owner="owner",
                    session_id="session",
                )
            )
        )
        self.assertEqual(read_calls, 0)
        self.assertEqual(events[-1].payload["status"], "approval_required")
        self.assertEqual(events[-1].payload["tool_calls"], 2)
        deferred = [
            event
            for event in events
            if event.type is AgentEventType.TOOL_FAILED
            and event.payload.get("call_id") == "read-2"
        ]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(
            deferred[0].payload["code"], "not_executed_after_approval"
        )

        current = await state.load(owner="owner", session_id="session")
        assistant = next(
            item
            for item in current.conversation
            if item.get("role") == "assistant" and item.get("tool_calls")
        )
        call_ids = {call["call_id"] for call in assistant["tool_calls"]}
        result_ids = {
            item.get("tool_call_id")
            for item in current.conversation
            if item.get("role") == "tool"
        }
        self.assertEqual(call_ids, {"write-1", "read-2"})
        self.assertEqual(result_ids, call_ids)
        deferred_result = next(
            item
            for item in current.conversation
            if item.get("tool_call_id") == "read-2"
        )
        self.assertIn("not_executed_after_approval", deferred_result["content"])

    async def test_provider_alias_is_projected_as_canonical_tool_name_in_events(
        self,
    ) -> None:
        tool = KernelToolSpec(
            name="agent.runtime_status",
            model_name="agent__runtime_status",
            domain="agent",
            description="读取状态",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            effect=ToolEffect.READ,
            read=lambda _arguments, _context: {"summary": "正常"},
        )
        catalog = ToolCatalog([tool])
        state = InMemorySessionStateStore()
        model = ScriptedModel(
            [
                [
                    ModelEvent(
                        ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall("alias-1", "agent__runtime_status", {}),
                    ),
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
                ],
                [
                    ModelEvent(ModelEventType.TEXT_DELTA, text="运行正常。"),
                    ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
                ],
            ]
        )
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(minimum=1, maximum=1),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
        )

        events = await collect(
            session.run(
                AgentInput(
                    message="检查 Agent 状态",
                    owner="owner",
                    session_id="session",
                )
            )
        )

        names = [
            event.payload.get("tool")
            for event in events
            if event.type
            in {
                AgentEventType.MODEL_TOOL_CALL,
                AgentEventType.TOOL_STARTED,
                AgentEventType.TOOL_COMPLETED,
            }
        ]
        self.assertEqual(names, ["agent.runtime_status"] * 3)
        self.assertNotIn(
            "agent__runtime_status", str([event.to_dict() for event in events])
        )

    async def test_tool_result_exposes_only_opaque_reference_to_model(self) -> None:
        def handler(_arguments, _context):
            return ToolOutcome(
                model_content='{"summary":"候选已找到"}',
                public_content={"summary": "候选已找到"},
                refs=(
                    ReferenceValue(
                        "resource", {"database_id": 99, "path": "/secret/path"}
                    ),
                ),
            )

        catalog = ToolCatalog(
            [read_tool("resource.search", domain="resource", handler=handler)]
        )
        state = InMemorySessionStateStore()
        model = ScriptedModel(
            [
                [
                    ModelEvent(
                        ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall("r1", "resource.search", {}),
                    ),
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
                ],
                [
                    ModelEvent(ModelEventType.TEXT_DELTA, text="已找到候选。"),
                    ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
                ],
            ]
        )
        pipeline = ToolPipeline(catalog=catalog, state_store=state)
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(),
            pipeline=pipeline,
            state_store=state,
        )
        events = await collect(
            session.run(
                AgentInput(
                    message="搜索资源",
                    owner="owner-1",
                    session_id="session-1",
                )
            )
        )
        completed = next(
            event for event in events if event.type is AgentEventType.TOOL_COMPLETED
        )
        reference = completed.payload["result"]["refs"][0]["ref"]
        self.assertTrue(reference.startswith("ref_"))
        tool_message = model.requests[1].messages[-1].content
        self.assertIn(reference, tool_message)
        self.assertNotIn("/secret/path", tool_message)
        self.assertNotIn("database_id", tool_message)

    async def test_confirmed_effect_reresolves_the_frozen_resource_reference(
        self,
    ) -> None:
        first_snapshot = {
            "search_id": "rs_1234567890abcdef",
            "search_status": "success",
            "candidates": [{"position": 1, "result_id": "first-resource-0001"}],
        }
        second_snapshot = {
            "search_id": "rs_fedcba0987654321",
            "search_status": "success",
            "candidates": [{"position": 1, "result_id": "second-resource-001"}],
        }
        snapshots = iter((first_snapshot, second_snapshot))
        executed: list[str] = []

        def search(_arguments, _context):
            snapshot = next(snapshots)
            return ToolResult(
                True,
                "success",
                "候选已找到",
                references=[ToolReference("resource_candidates", snapshot)],
            )

        def prepare(arguments, _context):
            snapshot = arguments["resource_candidates"]
            return PreparedEffect(
                preview={"summary": "将提交资源"},
                snapshot_fingerprint=snapshot["search_id"],
            )

        def execute(arguments, expected_snapshot, _context):
            snapshot = arguments["resource_candidates"]
            self.assertEqual(expected_snapshot, snapshot["search_id"])
            executed.append(snapshot["candidates"][0]["result_id"])
            return {"summary": "资源已提交"}

        search_tool = KernelToolSpec(
            name="resource.search",
            domain="resource",
            description="搜索资源并返回候选引用",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            effect=ToolEffect.READ,
            read=search,
        )
        submit_tool = KernelToolSpec(
            name="resource.submit",
            domain="resource",
            description="提交资源候选",
            input_schema={
                "type": "object",
                "required": ["resource_candidates_ref"],
                "properties": {
                    "resource_candidates_ref": {"type": "string"},
                },
                "additionalProperties": False,
            },
            effect=ToolEffect.WRITE,
            validator=lambda value: dict(value),
            prepare=prepare,
            execute_confirmed=execute,
        )
        catalog = ToolCatalog((search_tool, submit_tool))
        state = InMemorySessionStateStore()
        pipeline = ToolPipeline(catalog=catalog, state_store=state)
        lease, _state = await state.begin_turn(
            owner="owner-1", session_id="session-1", request_id="request-1"
        )
        token = CancellationToken()

        async def progress(_payload):
            return None

        context = ToolCallContext(
            owner="owner-1",
            session_id="session-1",
            request_id="request-1",
            turn_id=lease.turn_id,
            lease=lease,
            cancellation=token,
            report_progress=progress,
        )
        first = await pipeline.execute("resource.search", {}, context=context)
        first_ref = first.outcome.public_content["reference_arguments"][
            "resource_candidates_ref"
        ]
        preview = await pipeline.execute(
            "resource.submit",
            {"resource_candidates_ref": first_ref},
            context=context,
        )
        self.assertEqual(
            preview.effect_plan.arguments,
            {"resource_candidates_ref": first_ref},
        )

        newer = await pipeline.execute("resource.search", {}, context=context)
        self.assertNotEqual(
            newer.outcome.public_content["reference_arguments"][
                "resource_candidates_ref"
            ],
            first_ref,
        )
        await pipeline.execute_confirmed(preview.effect_plan.plan_id, context=context)
        self.assertEqual(executed, ["first-resource-0001"])

        foreign_lease, _foreign_state = await state.begin_turn(
            owner="owner-1", session_id="session-2", request_id="request-2"
        )
        foreign_token = CancellationToken()
        foreign_context = ToolCallContext(
            owner="owner-1",
            session_id="session-2",
            request_id="request-2",
            turn_id=foreign_lease.turn_id,
            lease=foreign_lease,
            cancellation=foreign_token,
            report_progress=progress,
        )
        with self.assertRaises(ToolPipelineError) as raised:
            await pipeline.execute(
                "resource.submit",
                {"resource_candidates_ref": first_ref},
                context=foreign_context,
            )
        self.assertEqual(raised.exception.code, "reference_invalid")

    async def test_default_projection_redacts_credentials_and_model_internal_paths(
        self,
    ) -> None:
        from app.agent.kernel.projection import DefaultProjector

        outcome = DefaultProjector().project(
            {
                "summary": "扫描完成 token=sk-secretsecretsecret1234",
                "data": {
                    "path": "/home/aio/private/media/file.mkv",
                    "database_id": 991,
                    "tmdb_id": 285993,
                    "source_url": "https://example.invalid/title/285993",
                },
            }
        )

        self.assertNotIn("sk-secret", str(outcome.public_content))
        self.assertIn("********", str(outcome.public_content))
        self.assertNotIn("/home/aio/private", outcome.model_content)
        self.assertNotIn('"database_id":991', outcome.model_content)
        self.assertIn('"tmdb_id":285993', outcome.model_content)
        self.assertIn("https://example.invalid/title/285993", outcome.model_content)

    async def test_projection_prefers_explicit_compact_model_dto(self) -> None:
        from app.agent.kernel.projection import DefaultProjector
        from app.agent.models import ToolResult

        outcome = DefaultProjector().project(
            ToolResult(
                True,
                "found",
                "找到 2 项",
                data={"entries": [{"name": "公开完整条目", "size": 123}]},
                model_data={"entries": [{"ref": "OBJ1", "name": "紧凑条目"}]},
            )
        )

        self.assertEqual(
            outcome.public_content["data"]["entries"][0]["name"],
            "公开完整条目",
        )
        self.assertIn("紧凑条目", outcome.model_content)
        self.assertNotIn("公开完整条目", outcome.model_content)


class LatestWinsTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_turn_cancels_old_turn_and_blocks_late_commit(self) -> None:
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        class ConcurrentModel:
            async def stream(self, request, *, cancellation):
                text = request.messages[-1].content
                if text == "first":
                    first_started.set()
                    await release_first.wait()
                    cancellation.raise_if_cancelled()
                    yield ModelEvent(ModelEventType.TEXT_DELTA, text="old")
                else:
                    yield ModelEvent(ModelEventType.TEXT_DELTA, text="new")
                yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")

        catalog = ToolCatalog([read_tool("agent.status", domain="agent")])
        state = InMemorySessionStateStore()
        pipeline = ToolPipeline(catalog=catalog, state_store=state)
        session = AgentSession(
            model=ConcurrentModel(),
            catalog=catalog,
            retriever=CapabilityRetriever(),
            pipeline=pipeline,
            state_store=state,
        )
        old_task = asyncio.create_task(
            collect(
                session.run(
                    AgentInput(message="first", owner="owner", session_id="same")
                )
            )
        )
        await asyncio.wait_for(first_started.wait(), timeout=1)
        new_events = await collect(
            session.run(AgentInput(message="second", owner="owner", session_id="same"))
        )
        release_first.set()
        old_events = await asyncio.wait_for(old_task, timeout=1)
        self.assertEqual(new_events[-1].payload["answer"], "new")
        self.assertEqual(old_events[-1].type, AgentEventType.TURN_CANCELLED)
        saved = await state.load(owner="owner", session_id="same")
        self.assertEqual(saved.conversation[-1]["content"], "new")

    async def test_followup_during_second_read_sees_the_completed_first_read(self):
        second_started = asyncio.Event()
        observed = []
        async def wait_read(_arguments, context):
            second_started.set()
            await context.cancellation.wait()
            context.cancellation.raise_if_cancelled()
        class Model:
            async def stream(self, request, *, cancellation):
                if request.messages[-1].content == "检查两个":
                    for name in ("library.first", "library.second"):
                        yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(name, name, {}))
                    yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")
                else:
                    observed.append(any("first-fact-5678" in message.content for message in request.messages))
                    yield ModelEvent(ModelEventType.TEXT_DELTA, text="继续处理")
                    yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")
        catalog = ToolCatalog([read_tool("library.first", handler=lambda *_: {"summary": "first-fact-5678"}),
                               read_tool("library.second", handler=wait_read)])
        state = InMemorySessionStateStore()
        session = AgentSession(model=Model(), catalog=catalog, retriever=CapabilityRetriever(minimum=2, maximum=2),
            pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state)
        previous = asyncio.create_task(collect(session.run(AgentInput(owner="owner", session_id="overlap", message="检查两个"))))
        try:
            await asyncio.wait_for(second_started.wait(), 2)
            await collect(session.run(AgentInput(owner="owner", session_id="overlap", message="继续")))
            await asyncio.wait_for(previous, 2)
            self.assertEqual(observed, [True])
        finally:
            if not previous.done():
                previous.cancel()

    async def test_late_read_checkpoint_cannot_replace_new_turn(self) -> None:
        checkpoint_started = asyncio.Event()
        release_checkpoint = asyncio.Event()

        class DelayedCheckpointStore(InMemorySessionStateStore):
            async def commit(self, lease, *, conversation=None, updates=()):
                if conversation and any(
                    item.get("tool_call_id") == "old-read"
                    for item in conversation
                ):
                    checkpoint_started.set()
                    await release_checkpoint.wait()
                return await super().commit(
                    lease, conversation=conversation, updates=updates
                )

        class ReplacingModel:
            async def stream(self, request, *, cancellation):
                del cancellation
                if request.messages[-1].content == "old":
                    yield ModelEvent(
                        ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall("old-read", "library.status", {}),
                    )
                    yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")
                else:
                    yield ModelEvent(ModelEventType.TEXT_DELTA, text="new")
                    yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")

        catalog = ToolCatalog([
            read_tool(
                "library.status",
                handler=lambda _arguments, _context: {
                    "summary": "旧回合读取完成",
                    "data": {"proof": "old-fact-must-not-win"},
                },
            )
        ])
        state = DelayedCheckpointStore()
        session = AgentSession(
            model=ReplacingModel(),
            catalog=catalog,
            retriever=CapabilityRetriever(minimum=1, maximum=1),
            pipeline=ToolPipeline(catalog=catalog, state_store=state),
            state_store=state,
        )
        old_task = asyncio.create_task(
            collect(
                session.run(
                    AgentInput(message="old", owner="owner-1", session_id="session-1")
                )
            )
        )
        await asyncio.wait_for(checkpoint_started.wait(), timeout=1)
        try:
            new_events = await collect(
                session.run(
                    AgentInput(message="new", owner="owner-1", session_id="session-1")
                )
            )
        finally:
            release_checkpoint.set()
        old_events = await asyncio.wait_for(old_task, timeout=1)

        self.assertEqual(new_events[-1].payload["answer"], "new")
        self.assertEqual(old_events[-1].type, AgentEventType.TURN_CANCELLED)
        saved = await state.load(owner="owner-1", session_id="session-1")
        self.assertEqual(saved.conversation[-1]["content"], "new")
        self.assertNotIn(
            "old-fact-must-not-win", json.dumps(saved.conversation, ensure_ascii=False)
        )
