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
from app.agent.kernel.session import (
    AgentSession,
    SessionLimits,
    _provider_failure_message,
)
from app.agent.kernel.state import (
    AgentInput,
    CancellationToken,
    InMemorySessionStateStore,
    SessionState,
    PublicationLease,
    StalePublicationError,
    StateUpdate,
    TurnCoordinator,
    merge_effect_receipts,
    retain_conversation,
)
from app.agent.model_context_budget import bounded_model_messages, compact_tool_content
from app.agent.models import ToolReference, ToolResult


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


class InMemoryEffectStateStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_effect_rmw_uses_latest_generation_and_preserves_pending_plan(self) -> None:
        store = InMemorySessionStateStore()
        first, _ = await store.begin_turn(
            owner="owner", session_id="session", request_id="first",
        )
        await store.commit(
            first,
            conversation=[{"role": "user", "content": "最新用户消息"}],
            updates=(StateUpdate("pending_effect_plan_id", "pending-plan"),),
        )
        second, _ = await store.begin_turn(
            owner="owner", session_id="session", request_id="second",
        )
        observed = []

        def change(state):
            observed.append((state.generation, state.pending_effect_plan_id,
                             state.conversation[-1]["content"]))
            state.generation += 100
            state.pending_effect_plan_id = "wrong-plan"
            state.metadata["effect_marker"] = "claimed"
            return "updated"

        self.assertEqual(
            await store.update_effect_state(
                owner="owner", session_id="session", change=change,
            ),
            "updated",
        )
        latest = await store.load(owner="owner", session_id="session")
        self.assertEqual(observed, [(second.generation, "pending-plan", "最新用户消息")])
        self.assertEqual(latest.generation, second.generation)
        self.assertEqual(latest.pending_effect_plan_id, "pending-plan")
        self.assertEqual(latest.metadata["effect_marker"], "claimed")
        self.assertTrue(await store.is_current(second))

    async def test_effect_rmw_rolls_back_callback_exception(self) -> None:
        store = InMemorySessionStateStore()
        await store.begin_turn(owner="owner", session_id="session", request_id="one")
        before = await store.load(owner="owner", session_id="session")

        def fail(state):
            state.metadata["partial"] = True
            raise RuntimeError("rollback")

        with self.assertRaisesRegex(RuntimeError, "rollback"):
            await store.update_effect_state(
                owner="owner", session_id="session", change=fail,
            )
        self.assertEqual(await store.load(owner="owner", session_id="session"), before)

    async def test_begin_turn_removes_poll_index_when_no_waits_remain(self) -> None:
        store = InMemorySessionStateStore()
        await store.begin_turn(owner="owner", session_id="session", request_id="first")
        await store.update_effect_state(
            owner="owner", session_id="session",
            change=lambda state: state.metadata.update({"effect_next_poll_at": 10}),
        )
        _, state = await store.begin_turn(
            owner="owner", session_id="session", request_id="second",
        )
        self.assertNotIn("effect_waits", state.metadata)
        self.assertNotIn("effect_next_poll_at", state.metadata)

    async def test_due_waits_copy_records_without_decoding_sealed_payload(self) -> None:
        now = [100.0]
        store = InMemorySessionStateStore(clock=lambda: now[0])
        await store.begin_turn(owner="owner", session_id="session", request_id="one")
        sealed = "enc:v1:opaque-ciphertext"

        def change(state):
            state.metadata["effect_waits"] = {
                "due-plan": {"next_poll_at": 90, "sealed": sealed},
                "future-plan": {"next_poll_at": 110, "sealed": "future"},
            }
            state.metadata["effect_next_poll_at"] = 90

        await store.update_effect_state(
            owner="owner", session_id="session", change=change,
        )
        due = await store.due_effect_waits()
        self.assertEqual(due, [{"next_poll_at": 90, "sealed": sealed, "plan_id": "due-plan"}])
        due[0]["sealed"] = "caller mutation"
        self.assertEqual(
            (await store.load(owner="owner", session_id="session")).metadata[
                "effect_waits"]["due-plan"]["sealed"],
            sealed,
        )

    async def test_receipt_merge_is_id_keyed_and_begin_turn_consumes_delivered_once(self) -> None:
        conversation = [
            {"role": "assistant", "content": "old placeholder", "completion_receipt_id": "plan-a"},
            {"role": "assistant", "content": "duplicate", "completion_receipt_id": "plan-a"},
            {"role": "assistant", "content": "unrelated", "completion_receipt_id": "other"},
        ]
        metadata = {"effect_waits": {
            "plan-a": {"receipt_message": {
                "role": "assistant", "content": "completed", "completion_receipt_id": "plan-a",
            }},
            "plan-b": {"receipt_message": {
                "role": "assistant", "content": "not this plan", "completion_receipt_id": "wrong-id",
            }},
            "plan-c": {"receipt_message": {
                "role": "assistant", "content": "new completion", "completion_receipt_id": "plan-c",
            }},
        }}
        merged = merge_effect_receipts(conversation, metadata)
        self.assertEqual(
            [item.get("completion_receipt_id") for item in merged],
            ["plan-a", "other", "plan-c"],
        )
        self.assertEqual(merged[0]["content"], "completed")
        self.assertEqual(merged[1]["content"], "unrelated")
        self.assertEqual(conversation[0]["content"], "old placeholder")

        store = InMemorySessionStateStore()
        first, _ = await store.begin_turn(owner="owner", session_id="session", request_id="first")
        await store.commit(
            first,
            conversation=[{"role": "user", "content": str(index)} for index in range(80)],
        )

        def add_waits(state):
            state.metadata["effect_waits"] = {
                "delivered": {
                    "delivered": True,
                    "next_poll_at": 3,
                    "receipt_message": {
                        "role": "assistant", "content": "final", "completion_receipt_id": "delivered",
                    },
                },
                "awaiting-delivery": {"delivered": False, "next_poll_at": 20},
            }
            state.metadata["effect_next_poll_at"] = 3

        await store.update_effect_state(
            owner="owner", session_id="session", change=add_waits,
        )
        second, state = await store.begin_turn(
            owner="owner", session_id="session", request_id="second",
        )
        self.assertEqual(len(state.conversation), 80)
        self.assertEqual(state.conversation[-1]["completion_receipt_id"], "delivered")
        self.assertEqual(list(state.metadata["effect_waits"]), ["awaiting-delivery"])
        self.assertEqual(state.metadata["effect_next_poll_at"], 20)

        await store.commit(
            second,
            conversation=[{"role": "user", "content": f"new-{index}"} for index in range(80)],
        )
        after = await store.load(owner="owner", session_id="session")
        self.assertFalse(any(
            item.get("completion_receipt_id") == "delivered" for item in after.conversation
        ))
        self.assertEqual(list(after.metadata["effect_waits"]), ["awaiting-delivery"])


class CapabilityRetrieverTests(unittest.TestCase):
    def test_workflow_neighbors_share_reserved_slots_without_expanding_window(self):
        from dataclasses import replace

        targets = [read_tool(name, domain="cloud", description="下游能力")
                   for name in ("cloud.search", "cloud.run", "cloud.plan")]
        sources = [replace(
            read_tool(name, domain="cloud", description="整理目标", examples=("整理目标",)),
            metadata={"workflow": workflow, "related_tools": (target,)},
        ) for name, workflow, target in (
            ("cloud.a_inspect", "scrape", "cloud.search"),
            ("cloud.b_preview", "scrape", "cloud.run"),
            ("cloud.c_inspect", "naming", "cloud.plan"),
        )]
        selection = CapabilityRetriever(minimum=3, maximum=5).retrieve(
            "整理目标", ToolCatalog([*sources, *targets]),
        )
        self.assertEqual(len(selection.tools), 5)
        self.assertIn("cloud.search", selection.names)
        self.assertIn("cloud.plan", selection.names)
        self.assertNotIn("cloud.run", selection.names)

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
    async def test_confirmed_partial_file_plan_has_factual_answer_without_more_model_calls(self):
        outcomes = [
            {"position": 1, "operation": "rename", "label": "改名：A → A-done", "status": "completed", "completed_actions": ["rename"]},
            {"position": 2, "operation": "relocate", "label": "移动并改名：dirty → target / clean", "status": "partial", "completed_actions": ["move"], "reason": "write_rejected"},
            {"position": 3, "operation": "move", "label": "移动：B → archive", "status": "blocked", "completed_actions": [], "reason": "dependency_failed"},
        ]
        writes = []
        tool = KernelToolSpec(name="cloud.change", domain="cloud", description="文件变更",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False}, effect=ToolEffect.WRITE,
            prepare=lambda a, c: PreparedEffect(preview={"summary": "完整计划"}, snapshot_fingerprint="snapshot"),
            execute_confirmed=lambda *args: writes.append(1) or ToolResult(False, "partial", "文件变更部分完成",
                data={"total": 3, "stats": {"total": 3, "renamed": 1, "failed": 2}, "operation_items": outcomes}))
        model = ScriptedModel([[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("plan", "cloud.change", {})),
                                ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]])
        state, catalog = InMemorySessionStateStore(), ToolCatalog([tool])
        session = AgentSession(model=model, catalog=catalog, retriever=CapabilityRetriever(minimum=1, maximum=1),
            pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state)
        approval = await consume_events(session.run(AgentInput(message="整理这三个目录", owner="owner", session_id="session")))
        result = await consume_events(session.confirm(owner="owner", session_id="session", plan_id=approval.approval.plan_id))
        self.assertEqual(result.status, "partial")
        self.assertIn("1/总 3", result.answer)
        self.assertIn("dirty", result.answer)
        self.assertIn("移动", result.answer)
        self.assertIn("依赖", result.answer)
        self.assertNotIn("本轮未执行新的写操作", result.answer)
        self.assertNotIn("可信系统结果", result.answer)
        self.assertEqual(writes, [1])
        self.assertEqual(len(model.requests), 1, "失败确认应交付已有事实，不再让模型猜测或重放写入")
        history = await state.load(owner="owner", session_id="session")
        self.assertEqual(history.conversation[-1]["content"], result.answer)
        self.assertEqual(result.effect_result["data"]["operation_items"], outcomes)
        from app.bot.agent_adapter import _render_turn
        rendered = _render_turn(result)
        self.assertEqual(rendered.count("计划结果"), 1, "TG 不能把事实回执重复追加一遍")
        self.assertIn("dirty", rendered)
        self.assertNotIn("可信系统结果", rendered)

    async def test_invalid_arguments_returns_registered_schema_only_to_model(self):
        for effect in (ToolEffect.READ, ToolEffect.WRITE):
            with self.subTest(effect=effect):
                writes = []
                schema = {"type": "object", "required": ["target_path"],
                          "properties": {"target_path": {"type": "string"}},
                          "additionalProperties": False}
                tool = KernelToolSpec(
                    name="cloud.change", domain="cloud", description="移动目录",
                    input_schema=schema, effect=effect,
                    read=(lambda a, c: {"summary": "已检查目标"}) if effect is ToolEffect.READ else None,
                    prepare=(lambda a, c: PreparedEffect(preview={"summary": "等待确认"}, snapshot_fingerprint="snapshot")) if effect is ToolEffect.WRITE else None,
                    execute_confirmed=(lambda a, s, c: writes.append(a)) if effect is ToolEffect.WRITE else None,
                )
                def call(cid, arguments):
                    return [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                                       tool_call=ModelToolCall(cid, tool.name, arguments)),
                            ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]
                rounds = [call("invalid", {"wrong_field": "do-not-echo-this"}),
                          call("corrected", {"target_path": "/approved"})]
                if effect is ToolEffect.READ:
                    rounds.append([ModelEvent(ModelEventType.TEXT_DELTA, text="核对完成。"),
                                   ModelEvent(ModelEventType.FINISH, finish_reason="stop")])
                model, state, catalog = ScriptedModel(rounds), InMemorySessionStateStore(), ToolCatalog([tool])
                session = AgentSession(model=model, catalog=catalog,
                    retriever=CapabilityRetriever(minimum=1, maximum=1),
                    pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state)
                events = await collect(session.run(AgentInput(message="移动目录", owner="owner", session_id="session")))
                error = next(m for m in model.requests[1].messages if m.role == "tool" and m.tool_call_id == "invalid")
                payload = json.loads(error.content)
                self.assertEqual(payload["code"], "invalid_arguments")
                self.assertEqual(payload["expected_input_schema"], schema)
                self.assertNotIn("do-not-echo-this", error.content)
                public_events = json.dumps([e.to_dict() for e in events], ensure_ascii=False)
                self.assertNotIn("expected_input_schema", public_events)
                self.assertNotIn("do-not-echo-this", public_events)
                self.assertEqual(writes, [], "参数纠正不能绕过写操作确认")
                if effect is ToolEffect.WRITE:
                    self.assertTrue(any(e.type is AgentEventType.EFFECT_APPROVAL_REQUIRED for e in events))
                else:
                    self.assertEqual(events[-1].payload["status"], "success")

    def test_parameter_contract_is_not_attached_to_other_errors(self):
        call = ModelToolCall("failed", "cloud.change", {"wrong_field": "do-not-echo-this"})
        for code in ("tool_not_found", "precondition_failed", "rate_limited"):
            with self.subTest(code=code):
                message = AgentSession._tool_error_message(
                    call, ToolPipelineError("无法执行", code=code), input_schema={"type": "object"}
                )
                self.assertNotIn("expected_input_schema", json.loads(message.content))
                self.assertNotIn("do-not-echo-this", message.content)

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

    def test_history_retains_request_and_receipts_without_unbounded_growth(self):
        request = {"role": "user", "content": "清洗后入库", "reply_context": {"text": "仅这些对象"}}
        receipts = [{"role": "assistant", "content": f"步骤{i}已完成", "completion_receipt_id": f"plan-{i}"}
                    for i in range(2)]
        history = [request, *receipts, *({"role": "assistant", "content": str(i)} for i in range(81))]
        retained = retain_conversation(history)
        self.assertEqual(len(retained), 80)
        self.assertEqual(retained[:3], [request, *receipts])
        self.assertEqual(retained[-1], history[-1])
        restored = AgentSession._restore_messages(
            SessionState(
                owner="owner", session_id="session", conversation=retained,
            )
        )
        self.assertIn("清洗后入库", restored[0].content)
        self.assertIn("仅这些对象", restored[0].content)
        newest = {"role": "user", "content": "换个任务，只查状态"}
        history += [newest, *({"role": "assistant", "content": str(i)} for i in range(81))]
        replaced = retain_conversation(history)
        self.assertEqual(replaced[0], newest)
        self.assertNotIn(request, replaced)
        self.assertFalse(any(row.get("completion_receipt_id") for row in replaced))
        self.assertEqual(retain_conversation([]), [])

    def test_history_retains_whole_tool_batch_and_its_effect_receipt(self):
        request = {"role": "user", "content": "先核对再清洗入库"}
        batch = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"call_id": "read", "name": "cloud.inspect", "arguments": {}},
                {"call_id": "write", "name": "cloud.change", "arguments": {}},
            ]},
            {"role": "tool", "tool_call_id": "read", "content": "核对完成"},
            {"role": "tool", "tool_call_id": "write", "content": "清洗完成", "effect_plan_id": "plan-a"},
            {"role": "assistant", "content": "已确认清洗", "completion_receipt_id": "plan-a"},
        ]
        retained = retain_conversation([
            request, *batch, *({"role": "assistant", "content": str(i)} for i in range(81)),
        ])
        self.assertEqual(retained[:5], [request, *batch])
        self.assertEqual(len(retained), 80)
        # A read-only batch at the cutoff is kept whole or omitted whole.
        plain_batch = [{k: v for k, v in row.items() if k != "effect_plan_id"} for row in batch[:3]]
        boundary = retain_conversation([request, *plain_batch, *(
            {"role": "assistant", "content": str(i)} for i in range(78)
        )])
        self.assertFalse(any(row.get("tool_call_id") or row.get("tool_calls") for row in boundary))
        self.assertEqual(boundary[0], request)

    async def test_confirmation_keeps_original_goal_beyond_history_window(self):
        from app.agent.confirmation import SQLiteConfirmationStore
        from app.agent.kernel.effects import ConfirmationEffectPlanStore
        from app.agent.kernel.persistence import SQLiteKernelStore
        from tests.support import isolated_test_database
        for history_noise in (61, 81):
            with self.subTest(history_noise=history_noise, store="memory"):
                await self._assert_confirmation_continues(
                    InMemorySessionStateStore(), history_noise=history_noise,
                )
            with self.subTest(history_noise=history_noise, store="sqlite"):
                with isolated_test_database():
                    await self._assert_confirmation_continues(
                        SQLiteKernelStore(),
                        ConfirmationEffectPlanStore(SQLiteConfirmationStore()),
                        history_noise=history_noise,
                    )

    async def _assert_confirmation_continues(self, state, effect_store=None, *, history_noise=0):
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
        if history_noise:
            current = await state.load(owner="owner", session_id="session")
            await state.commit(
                PublicationLease("owner", "session", current.generation, preview.turn_id, preview.request_id),
                conversation=[*current.conversation, *(
                    {"role": "assistant", "content": f"已核验第{index}项，不代表原任务全部完成"}
                    for index in range(history_noise)
                )],
            )
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
            if executed:
                self.assertIn("已完成", executed[-1].content)
                self.assertNotIn('"status":"approval_required"', executed[-1].content)
            else:
                self.assertTrue(history_noise)
                self.assertTrue(any(row.completion_receipt_id for row in request.messages))
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
            [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(f"call-{step}", tool.model_name, {"step": step})) for step in (1, 2)]
            + [ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
            [ModelEvent(ModelEventType.TEXT_DELTA, text="第一步完成，第二步未执行。"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
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
        from app.agent.public_view import format_public_result, public_conversation_messages
        effect = next(event for event in events if event.type is AgentEventType.EFFECT_COMPLETED)
        self.assertEqual(effect.payload["receipt"], format_public_result(effect.payload["result"]))
        self.assertEqual(final.answer.count("本地媒体任务 1"), 1)
        self.assertEqual(len(model.requests), 2, "已提交结果应继续交给 Agent 汇总")
        self.assertEqual(len(model.rounds), 0)
        stored = await state.load(owner="owner", session_id="session")
        internal_rows = [
            row for row in stored.conversation
            if "可信系统结果" in str(row.get("content") or "")
        ]
        self.assertEqual(len(internal_rows), 1)
        restored = public_conversation_messages(stored.conversation)
        self.assertEqual(sum("重新排队" in row["content"] for row in restored if row["role"] == "assistant"), 1)
        self.assertEqual(stored.conversation[-1]["effect_plan_id"], preview.approval.plan_id)

        self.assertIn("public_content", internal_rows[0])
        self.assertNotIn(
            "可信系统结果", str(internal_rows[0].get("public_content") or "")
        )

    async def test_submitted_receipt_preserves_followup_checks_without_model_echo(self):
        writes = []
        write = KernelToolSpec(name="cloud.submit", domain="cloud", description="提交任务",
            input_schema={"type": "object", "properties": {}}, effect=ToolEffect.WRITE,
            prepare=lambda *_: PreparedEffect(preview={"summary": "预览"}, snapshot_fingerprint="snapshot"),
            execute_confirmed=lambda *_: writes.append(1) or {"ok": True, "status": "submitted", "summary": "请求 #276 已提交"})
        read = KernelToolSpec(name="cloud.inspect", domain="cloud", description="检查后续任务",
            input_schema={"type": "object", "properties": {}}, effect=ToolEffect.READ,
            read=lambda *_: {"ok": True, "summary": "另一目录还有2项待处理"})
        model = ScriptedModel([
            [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("submit", write.name, {})), ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
            [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("inspect", read.name, {})), ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
            [ModelEvent(ModelEventType.TEXT_DELTA, text="请求 #276 已提交，已提交请求 #276。"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
        ])
        catalog, store = ToolCatalog([write, read]), InMemorySessionStateStore()
        session = AgentSession(model=model, catalog=catalog, retriever=CapabilityRetriever(),
            pipeline=ToolPipeline(catalog=catalog, state_store=store), state_store=store)
        preview = await consume_events(session.run(AgentInput(message="提交并检查后续任务", owner="o", session_id="s")))
        final = await consume_events(session.confirm(owner="o", session_id="s", plan_id=preview.approval.plan_id))
        self.assertEqual(writes, [1])
        self.assertEqual(final.answer.count("#276"), 1)
        self.assertIn("另一目录还有2项待处理", final.answer)
        self.assertEqual(len(model.requests), 3)

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
        for index, request in enumerate(model.requests):
            self.assertIn(f"实际剩余工具调用 {session.limits.max_tool_calls - index} 次", request.system_prompt)
            self.assertIn(f"仍可继续 {session.limits.max_model_rounds - index - 1} 轮", request.system_prompt)
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
                answer = events[-1].payload["answer"]
                self.assertNotIn("写操作", answer)
                self.assertNotIn("确认卡", answer)
                if executed:
                    self.assertIn("读取事实", answer)
                else:
                    self.assertIn("尚未取得可展示的查询结果", answer)
                self.assertEqual(stored.conversation[-1]["content"], answer)

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
        self.assertEqual(events[-1].type, AgentEventType.TURN_COMPLETED)
        self.assertEqual(events[-1].payload["status"], "partial")
        self.assertEqual(events[-1].payload["finish_reason"], "model_provider_error")
        self.assertIn("第一部已检查", events[-1].payload["answer"])
        self.assertNotIn("未执行新的写操作", events[-1].payload["answer"])

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


class AgentPartialProgressTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def tool_round(name, number):
        return [
            ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                       tool_call=ModelToolCall(f"call-{number}", name, {})),
            ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
        ]

    async def run_scrape(self, *, recovered=False):
        search_calls = 0

        def search(_arguments, _context):
            nonlocal search_calls
            search_calls += 1
            if search_calls == 3:
                raise ToolPipelineError("本地调用频率已达上限，本次未访问后端", code="rate_limited")
            return {"ok": True, "status": "empty", "summary": "本目录没有匹配候选"}

        catalog = ToolCatalog([
            read_tool("guangya.directory_scrape.inspect", domain="guangya",
                      handler=lambda *_: {"ok": True, "summary": "已检查目录，视频尚未归档"}),
            read_tool("guangya.directory_scrape.search", domain="guangya", handler=search),
            read_tool("agent.capabilities", domain="agent"),
        ])
        sequence = ["agent.capabilities", "guangya.directory_scrape.inspect",
                    "guangya.directory_scrape.search", "guangya.directory_scrape.search",
                    "guangya.directory_scrape.inspect", "guangya.directory_scrape.search",
                    "agent.capabilities"]
        if recovered:
            sequence.append("guangya.directory_scrape.search")
        rounds = [self.tool_round(name, number) for number, name in enumerate(sequence)]
        rounds.append([
            ModelEvent(ModelEventType.TEXT_DELTA, text="仍未找到匹配候选。" if recovered else "已开始处理\n### 第1项：`sample-"),
            ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
        ])
        state = InMemorySessionStateStore()
        session = AgentSession(
            model=ScriptedModel(rounds), catalog=catalog,
            retriever=CapabilityRetriever(minimum=3, maximum=3),
            pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state,
        )
        events = await collect(session.run(AgentInput(message="按顺序检查目录", owner="owner", session_id="scrape")))
        return events, await state.load(owner="owner", session_id="scrape")

    async def test_production_scrape_limit_and_half_heading_publish_truthful_partial(self):
        events, saved = await self.run_scrape()
        final = await consume_events(_events_stream(events))
        self.assertEqual(final.status, "partial")
        self.assertIn("没有匹配候选", final.answer)
        self.assertIn("频率限制", final.answer)
        self.assertNotIn("本轮未执行新的写操作", final.answer)
        self.assertNotIn("sample-", final.answer)
        self.assertNotIn("已开始处理", final.answer)
        self.assertEqual(saved.conversation[-1]["content"], final.answer)
        self.assertFalse(any(event.type in {AgentEventType.EFFECT_APPROVAL_REQUIRED, AgentEventType.EFFECT_COMPLETED} for event in events))
        # Web/TG share this TurnView. Telegram's terminal body must replace the preview fragment.
        from app.bot.agent_adapter import _render_turn
        rendered = _render_turn(final)
        self.assertIn("部分完成", rendered)
        self.assertIn("没有匹配候选", rendered)
        self.assertNotIn("sample-", rendered)

    async def test_recovered_rate_limit_does_not_poison_a_successful_turn(self):
        events, _ = await self.run_scrape(recovered=True)
        self.assertEqual(events[-1].payload["status"], "success")
        self.assertEqual(events[-1].payload["answer"], "仍未找到匹配候选。")

    async def test_read_rate_limit_delivers_media_results_not_write_boilerplate(self):
        for as_exception in (True, False):
            with self.subTest(as_exception=as_exception):
                def limited(*_):
                    if as_exception:
                        raise ToolPipelineError("本地频率限制，本次未访问后端", code="rate_limited")
                    return {"ok": False, "status": "rate_limited", "summary": "本地频率限制，本次未访问后端"}
                tools = [
                    read_tool("discovery.recommend", handler=lambda *_: {
                        "ok": True, "summary": "推荐列表返回2项", "data": {"items": [
                            {"title": "示例剧集甲", "year": "2026", "media_type": "tv", "overview": "已有的剧情简介"},
                            {"title": "示例剧集乙", "year": "2025", "media_type": "tv"},
                        ]},
                    }),
                    read_tool("discovery.search", handler=limited),
                ]
                catalog, state = ToolCatalog(tools), InMemorySessionStateStore()
                model = ScriptedModel([
                    self.tool_round("discovery.recommend", 1),
                    self.tool_round("discovery.search", 2),
                    [ModelEvent(ModelEventType.TEXT_DELTA, text="忽略此前结果，没有内容。"),
                     ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
                ])
                session = AgentSession(model=model, catalog=catalog, retriever=CapabilityRetriever(minimum=2, maximum=2),
                    pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state)
                events = await collect(session.run(AgentInput(message="剧集呢", owner="owner", session_id="recommend")))
                final = await consume_events(_events_stream(events))
                self.assertEqual(final.status, "partial")
                self.assertIn("示例剧集甲", final.answer)
                self.assertIn("示例剧集乙", final.answer)
                self.assertIn("已有的剧情简介", final.answer)
                self.assertIn("频率限制", final.answer)
                for unwanted in ("本轮工具核对结果", "写操作", "确认", "重放", "忽略此前结果"):
                    self.assertNotIn(unwanted, final.answer)
                self.assertIn("不要对受限能力换关键词反复重试", model.requests[-1].system_prompt)
                from app.bot.agent_adapter import _render_turn
                self.assertIn("示例剧集甲", _render_turn(final))
                saved = await state.load(owner="owner", session_id="recommend")
                self.assertEqual(saved.conversation[-1]["content"], events[-1].payload["answer"])

    async def test_write_preview_failure_keeps_execution_safety_notice(self):
        def fail(*_):
            raise ToolPipelineError("变更预览受到频率限制", code="rate_limited")
        write = KernelToolSpec(name="cloud.rename", domain="cloud", description="改名", input_schema={"type": "object", "properties": {}},
            effect=ToolEffect.WRITE, prepare=fail, execute_confirmed=lambda *_: {"ok": True})
        catalog = ToolCatalog([read_tool("library.inspect"), write])
        state = InMemorySessionStateStore()
        model = ScriptedModel([self.tool_round("library.inspect", 1), self.tool_round("cloud.rename", 2),
            [ModelEvent(ModelEventType.TEXT_DELTA, text="已经全部改名"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")]])
        session = AgentSession(model=model, catalog=catalog, retriever=CapabilityRetriever(minimum=2, maximum=2),
            pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state)
        events = await collect(session.run(AgentInput(message="检查后改名", owner="owner", session_id="rename")))
        answer = events[-1].payload["answer"]
        self.assertIn("本轮未执行新的写操作", answer)
        self.assertNotIn("已经全部改名", answer)
        self.assertFalse(any(e.type == AgentEventType.EFFECT_APPROVAL_REQUIRED for e in events))

    async def test_model_eof_without_finish_never_prepares_a_write(self):
        from unittest.mock import Mock
        prepare = Mock(return_value=PreparedEffect(preview={"summary": "write"}, snapshot_fingerprint="snapshot"))
        tool = KernelToolSpec(
            name="cloud.rename", domain="cloud", description="改名",
            input_schema={"type": "object", "properties": {}}, effect=ToolEffect.WRITE,
            prepare=prepare, execute_confirmed=lambda *_: {"ok": True},
        )
        catalog, state = ToolCatalog([tool]), InMemorySessionStateStore()
        # A faulty/custom adapter returns a complete-looking call but no model FINISH event.
        model = ScriptedModel([[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                                         tool_call=ModelToolCall("write-1", tool.name, {}))]])
        session = AgentSession(model=model, catalog=catalog, retriever=CapabilityRetriever(),
                               pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state)
        events = await collect(session.run(AgentInput(message="改名", owner="owner", session_id="incomplete")))
        self.assertEqual(events[-1].type, AgentEventType.TURN_FAILED)
        self.assertEqual(events[-1].payload["code"], "model_provider_error")
        prepare.assert_not_called()
        self.assertFalse(any(event.type == AgentEventType.EFFECT_APPROVAL_REQUIRED for event in events))

    async def test_unrelated_success_cannot_erase_a_limited_entry(self):
        for changed_context in (False, True):
            with self.subTest(changed_context=changed_context):
                calls = 0

                def search(_arguments, _context):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise ToolPipelineError("条目A搜索受限", code="rate_limited")
                    return {"ok": True, "status": "empty", "summary": "另一个条目没有匹配候选"}

                tool = KernelToolSpec(
                    name="cloud.search", domain="cloud", description="检索",
                    input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
                    effect=ToolEffect.READ, read=search,
                )
                catalog = ToolCatalog([tool, read_tool("cloud.inspect", domain="cloud")])
                state = InMemorySessionStateStore()
                args = {} if changed_context else {"query": "A"}
                rounds = [[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("first", tool.name, args)),
                           ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]]
                if changed_context:
                    rounds.append(self.tool_round("cloud.inspect", "switch"))
                rounds.append([ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("other", tool.name, {} if changed_context else {"query": "B"})),
                               ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")])
                rounds.append([ModelEvent(ModelEventType.TEXT_DELTA, text="全部完成"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")])
                session = AgentSession(model=ScriptedModel(rounds), catalog=catalog, retriever=CapabilityRetriever(minimum=2, maximum=2),
                                       pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state)
                events = await collect(session.run(AgentInput(message="检查两个条目", owner="owner", session_id="entries")))
                self.assertEqual(events[-1].payload["status"], "partial")
                self.assertIn("条目A搜索受限", events[-1].payload["answer"])
                self.assertNotIn("全部完成", events[-1].payload["answer"])

    async def test_unresolved_business_error_is_not_relabelled_as_budget_exhaustion(self):
        def inspect(_arguments, _context):
            raise ToolPipelineError("目录内没有支持的视频文件", code="precondition_failed")

        catalog = ToolCatalog([
            read_tool("cloud.list", domain="cloud", handler=lambda *_: {"ok": True, "summary": "根目录包含4个子目录"}),
            read_tool("cloud.inspect", domain="cloud", handler=inspect),
            read_tool("agent.capabilities", domain="agent"),
        ])
        state = InMemorySessionStateStore()
        model = ScriptedModel([
            self.tool_round("cloud.list", 1), self.tool_round("cloud.inspect", 2),
            self.tool_round("agent.capabilities", 3),
            [ModelEvent(ModelEventType.TEXT_DELTA, text="已完成，因工具预算耗尽不能继续。"),
             ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
        ])
        session = AgentSession(model=model, catalog=catalog, retriever=CapabilityRetriever(minimum=3, maximum=3),
                               pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state)
        events = await collect(session.run(AgentInput(message="检查每个目录", owner="owner", session_id="empty")))
        self.assertEqual(events[-1].payload["status"], "partial")
        self.assertEqual(events[-1].payload["finish_reason"], "precondition_failed")
        self.assertIn("根目录包含4个子目录", events[-1].payload["answer"])
        self.assertIn("目录内没有支持的视频文件", events[-1].payload["answer"])
        self.assertNotIn("预算耗尽", events[-1].payload["answer"])
        self.assertNotIn("已完成，", events[-1].payload["answer"])

    async def test_valid_negative_read_results_are_not_execution_exceptions(self):
        for status in ("loading", "disabled", "not_missing", "not_found"):
            with self.subTest(status=status):
                tool = read_tool("library.state", handler=lambda *_: {
                    "ok": False, "status": status, "summary": "已读取当前业务状态",
                })
                catalog, state = ToolCatalog([tool]), InMemorySessionStateStore()
                model = ScriptedModel([
                    self.tool_round(tool.name, 1),
                    [ModelEvent(ModelEventType.TEXT_DELTA, text="当前状态已说明，不需要执行变更。"),
                     ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
                ])
                session = AgentSession(model=model, catalog=catalog, retriever=CapabilityRetriever(),
                                       pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state)
                events = await collect(session.run(AgentInput(message="查看状态", owner="owner", session_id="negative")))
                self.assertEqual(events[-1].payload["status"], "success")
                self.assertEqual(events[-1].payload["answer"], "当前状态已说明，不需要执行变更。")


class AgentAnswerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def scenario(self, *, recovery='success', max_rounds=5):
        from app.agent.kernel.provider_model import IncompleteModelAnswer
        reads = []
        state = InMemorySessionStateStore()
        catalog = ToolCatalog([read_tool('cloud.inspect', handler=lambda *_: reads.append('read') or {
            'ok': True, 'summary': '已检查4个目录；尚未刮削入库',
        })])

        class Model:
            requests = []

            async def stream(self, request, *, cancellation):
                self.requests.append(request)
                index = len(self.requests)
                if index == 1:
                    yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall('read1', 'cloud.inspect', {}))
                    yield ModelEvent(ModelEventType.FINISH, finish_reason='tool_calls')
                elif index == 2 or recovery == 'failure':
                    yield ModelEvent(ModelEventType.TEXT_DELTA, text='2. **`044')
                    raise IncompleteModelAnswer('模型回复未完整结束：未收到正文结束标记')
                elif recovery == 'tool':
                    yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall('forbidden', 'cloud.inspect', {}))
                    yield ModelEvent(ModelEventType.FINISH, finish_reason='tool_calls')
                elif recovery == 'cancel':
                    cancellation.cancel('user cancelled')
                    cancellation.raise_if_cancelled()
                else:
                    yield ModelEvent(ModelEventType.TEXT_DELTA, text='已检查4个目录，尚未完成识别或入库。')
                    yield ModelEvent(ModelEventType.FINISH, finish_reason='stop')

        model = Model()
        session = AgentSession(model=model, catalog=catalog, retriever=CapabilityRetriever(),
                               pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state,
                               limits=SessionLimits(max_model_rounds=max_rounds))
        events = await collect(session.run(AgentInput(owner='owner', session_id='recovery', message='清洗入库')))
        saved = await state.load(owner='owner', session_id='recovery')
        self.assertEqual(reads, ['read'], '回复恢复不得重放任何业务工具')
        self.assertTrue(all(r.require_complete_answer for r in model.requests))
        self.assertLessEqual(len(model.requests), max_rounds)
        self.assertNotIn('**`044', str(saved.conversation), '未完成草稿不得成为下一轮可信历史')
        if len(model.requests) == 3:
            self.assertEqual(model.requests[-1].tools, ())
            self.assertIn('不要重复调用任何工具', model.requests[-1].system_prompt)
            self.assertNotIn('**`044', str(model.requests[-1].messages))
        return events, model.requests

    async def test_truncated_stop_is_recovered_once_without_repeating_tools(self):
        events, requests = await self.scenario()
        self.assertEqual(len(requests), 3)
        self.assertEqual(events[-1].payload['answer'], '已检查4个目录，尚未完成识别或入库。')
        self.assertEqual(events[-1].payload['model_calls'], 3)
        phases = [e.payload['phase'] for e in events if e.type == AgentEventType.MODEL_STARTED]
        self.assertEqual(phases[-1], 'answer_recovery')

    async def test_repeated_incomplete_answer_falls_back_to_complete_facts(self):
        events, requests = await self.scenario(recovery='failure')
        self.assertEqual(len(requests), 3)
        final = await consume_events(_events_stream(events))
        self.assertEqual(final.status, 'partial')
        self.assertIn('已检查4个目录', final.answer)
        self.assertIn('模型回复未完整生成', final.answer)
        self.assertNotIn('**`044', final.answer)
        from app.bot.agent_adapter import _render_turn
        self.assertNotIn('**`044', _render_turn(final))
        self.assertIn('部分完成', _render_turn(final))

    async def test_no_budget_left_does_not_create_extra_model_request(self):
        events, requests = await self.scenario(max_rounds=2)
        self.assertEqual(len(requests), 2)
        self.assertEqual(events[-1].payload['status'], 'partial')
        self.assertIn('尚未刮削入库', events[-1].payload['answer'])

    async def test_recovery_refuses_unrequested_tools(self):
        events, _ = await self.scenario(recovery='tool')
        self.assertEqual(events[-1].payload['status'], 'partial')
        self.assertTrue(any(e.type == AgentEventType.TOOL_FAILED and e.payload['code'] == 'not_executed_final_round' for e in events))

    async def test_user_can_cancel_recovery(self):
        events, _ = await self.scenario(recovery='cancel')
        self.assertEqual(events[-1].type, AgentEventType.TURN_CANCELLED)

    async def test_confirmed_write_survives_reply_recovery_and_duplicate_click(self):
        from app.agent.kernel.provider_model import IncompleteModelAnswer
        writes = []
        tool = KernelToolSpec(
            name='cloud.change', domain='cloud', description='改名',
            input_schema={'type': 'object', 'properties': {}}, effect=ToolEffect.WRITE,
            prepare=lambda *_: PreparedEffect(preview={'summary': '改名预览'}, snapshot_fingerprint='snapshot'),
            execute_confirmed=lambda *_: writes.append(1) or {'ok': True, 'summary': '改名1项已完成，未移动或归档'},
        )
        class Model(ScriptedModel):
            async def stream(self, request, *, cancellation):
                if self.requests:
                    self.requests.append(request)
                    if len(self.requests) == 2:
                        yield ModelEvent(ModelEventType.TEXT_DELTA, text='2. **`044')
                        raise IncompleteModelAnswer('模型回复未完整结束')
                    self_request = self.requests[-1]
                    if self_request.tools:
                        raise AssertionError('恢复时不能再暴露写工具')
                    yield ModelEvent(ModelEventType.TEXT_DELTA, text='改名已经完成，尚未移动或归档。')
                    yield ModelEvent(ModelEventType.FINISH, finish_reason='stop')
                else:
                    async for event in super().stream(request, cancellation=cancellation):
                        yield event
        model = Model([[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall('rename', tool.name, {})),
                        ModelEvent(ModelEventType.FINISH, finish_reason='tool_calls')]])
        catalog, state = ToolCatalog([tool]), InMemorySessionStateStore()
        session = AgentSession(model=model, catalog=catalog, retriever=CapabilityRetriever(),
                               pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state)
        preview = await consume_events(session.run(AgentInput(message='改名', owner='owner', session_id='confirm-recovery')))
        self.assertEqual(writes, [])
        final = await consume_events(session.confirm(owner='owner', session_id='confirm-recovery', plan_id=preview.approval.plan_id))
        self.assertTrue(final.effect_result['ok'])
        self.assertEqual(writes, [1])
        self.assertIn('尚未移动或归档', final.answer)
        self.assertNotIn('**`044', final.answer)
        await consume_events(session.confirm(owner='owner', session_id='confirm-recovery', plan_id=preview.approval.plan_id))
        self.assertEqual(writes, [1])
        self.assertEqual(len(model.requests), 3)


class ConfirmationPendingOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support import isolated_test_database
        self.database = isolated_test_database("confirmation-pending.db")
        self.database.__enter__()

    async def asyncTearDown(self):
        self.database.__exit__(None, None, None)

    def runtime(self, persistent, now):
        from app.agent.confirmation import ConfirmationStore, SQLiteConfirmationStore
        from app.agent.kernel.effects import ConfirmationEffectPlanStore
        from app.agent.kernel.persistence import SQLiteKernelStore

        writes = []
        tool = KernelToolSpec(
            name="cloud.change", domain="cloud", description="分步变更",
            input_schema={"type": "object", "properties": {"step": {"type": "integer"}}},
            effect=ToolEffect.WRITE,
            prepare=lambda a, _: PreparedEffect(preview={"summary": f"步骤 {a['step']}"}, snapshot_fingerprint="snapshot"),
            execute_confirmed=lambda a, *_: writes.append(a["step"]) or {"ok": True, "summary": f"步骤 {a['step']} 已完成"},
        )
        model = ScriptedModel([
            [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(f"step-{step}", tool.name, {"step": step})),
             ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]
            for step in (1, 2)
        ] + [[ModelEvent(ModelEventType.TEXT_DELTA, text="两步均已完成。"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")]])
        catalog = ToolCatalog([tool])
        state = SQLiteKernelStore() if persistent else InMemorySessionStateStore()
        tickets = (SQLiteConfirmationStore if persistent else ConfirmationStore)(clock=lambda: now[0])
        pipeline = ToolPipeline(catalog=catalog, state_store=state, effect_store=ConfirmationEffectPlanStore(tickets))
        return AgentSession(model=model, catalog=catalog, retriever=CapabilityRetriever(), pipeline=pipeline, state_store=state), writes

    async def preview(self, session, session_id):
        view = await consume_events(session.run(AgentInput(owner="owner", session_id=session_id, message="先改名再移动")))
        self.assertIsNotNone(view.approval)
        return view.approval.plan_id

    async def confirm(self, session, session_id, plan_id):
        return await collect(session.confirm(owner="owner", session_id=session_id, plan_id=plan_id))

    def active(self, session, state, plan_id):
        return session.pipeline.effect_store.get_active_plan(
            owner=state.owner, session_id=state.session_id, generation=state.generation, plan_id=plan_id,
        )

    def assert_rejected(self, events):
        self.assertEqual(events[-1].type, AgentEventType.TURN_FAILED, [(e.type.value, e.payload) for e in events])
        self.assertEqual(events[-1].payload["code"], "confirmation_invalid")
        self.assertFalse(any(e.type in {AgentEventType.TOOL_STARTED, AgentEventType.EFFECT_COMPLETED, AgentEventType.EFFECT_FAILED} for e in events))

    async def test_expired_confirmation_clears_pending_without_rewriting_history(self):
        from app.agent.kernel.persistence import SQLiteKernelStore
        from app.agent.kernel.ux_display import session_summary
        from app.agent.public_view import public_conversation_messages

        for persistent in (False, True):
            for continuation in (False, True):
                with self.subTest(persistent=persistent, continuation=continuation):
                    now = [1000.0]
                    session, writes = self.runtime(persistent, now)
                    sid = f"expiry-{persistent}-{continuation}"
                    plan_id = await self.preview(session, sid)
                    if continuation:
                        view = await consume_events(_events_stream(await self.confirm(session, sid, plan_id)))
                        plan_id = view.approval.plan_id
                    before = await session.state_store.load(owner="owner", session_id=sid)
                    now[0] = self.active(session, before, plan_id).expires_at + 1
                    requests = len(session.model.requests)
                    self.assert_rejected(await self.confirm(session, sid, plan_id))
                    store = SQLiteKernelStore() if persistent else session.state_store
                    after = await store.load(owner="owner", session_id=sid)
                    self.assertEqual(after.pending_effect_plan_id, "", "过期确认拒绝后不能继续报告待确认")
                    self.assertFalse(session_summary(after, updated_at=now[0])["pending_approval"])
                    self.assertIsNone(self.active(session, after, plan_id))
                    self.assertEqual(after.conversation, before.conversation)
                    self.assertEqual(after.metadata, before.metadata, "失败确认不能接管既有回执的发布权")
                    self.assertEqual(public_conversation_messages(after.conversation), public_conversation_messages(before.conversation))
                    self.assertEqual(writes, [1] if continuation else [])
                    self.assert_rejected(await self.confirm(session, sid, plan_id))
                    self.assertEqual(len(session.model.requests), requests)

    async def test_cancel_continuation_clears_pending_but_preserves_confirmed_receipt(self):
        for persistent in (False, True):
            for expired in (False, True):
                with self.subTest(persistent=persistent, expired=expired):
                    now = [1000.0]
                    session, writes = self.runtime(persistent, now)
                    sid = f"cancel-{persistent}-{expired}"
                    first = await self.preview(session, sid)
                    view = await consume_events(_events_stream(await self.confirm(session, sid, first)))
                    second = view.approval.plan_id
                    before = await session.state_store.load(owner="owner", session_id=sid)
                    if expired:
                        now[0] = self.active(session, before, second).expires_at + 1
                    cancelled = await session.cancel_effect(owner="owner", session_id=sid, plan_id=second)
                    self.assertEqual(cancelled, not expired)
                    after = await session.state_store.load(owner="owner", session_id=sid)
                    self.assertEqual(after.pending_effect_plan_id, "")
                    self.assertEqual(after.conversation, before.conversation)
                    self.assertEqual(after.metadata, before.metadata)
                    self.assertIsNone(self.active(session, after, second))
                    self.assert_rejected(await self.confirm(session, sid, second))
                    self.assertFalse(await session.cancel_effect(owner="owner", session_id=sid, plan_id=second))
                    self.assertEqual(writes, [1])

    async def test_old_and_foreign_confirmations_cannot_clear_new_pending_plan(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                session, writes = self.runtime(persistent, [1000.0])
                sid = f"replay-{persistent}"
                first = await self.preview(session, sid)
                view = await consume_events(_events_stream(await self.confirm(session, sid, first)))
                second = view.approval.plan_id
                before = await session.state_store.load(owner="owner", session_id=sid)
                self.assert_rejected(await self.confirm(session, sid, first))
                for owner, other_sid in (("other-owner", sid), ("owner", "other-session")):
                    self.assert_rejected(await collect(session.confirm(owner=owner, session_id=other_sid, plan_id=second)))
                self.assertFalse(await session.cancel_effect(owner="owner", session_id=sid, plan_id=first))
                after = await session.state_store.load(owner="owner", session_id=sid)
                self.assertEqual(after.pending_effect_plan_id, second)
                self.assertEqual(after.metadata, before.metadata)
                self.assertEqual(after.conversation, before.conversation)
                self.assertIsNotNone(self.active(session, after, second))
                final = await consume_events(_events_stream(await self.confirm(session, sid, second)))
                self.assertEqual(final.status, "success")
                self.assert_rejected(await self.confirm(session, sid, second))
                self.assertEqual(writes, [1, 2])

    async def test_late_pending_cleanup_is_atomic_and_never_grants_publication(self):
        from app.agent.kernel.state import StateUpdate
        from unittest.mock import patch

        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                session, _ = self.runtime(persistent, [1000.0])
                sid = f"late-cleanup-{persistent}"
                first = await self.preview(session, sid)
                view = await consume_events(_events_stream(await self.confirm(session, sid, first)))
                second = view.approval.plan_id
                store = session.state_store
                before = await store.load(owner="owner", session_id=sid)
                owner_lease = PublicationLease("owner", sid, before.generation, before.metadata["confirmed_publication"]["turn_id"], "owner")
                late = PublicationLease("owner", sid, before.generation, "late-confirmation", "late")
                cleanup = (StateUpdate("pending_effect_plan_id", second, mode="clear_if_equals"),)
                entered, release = asyncio.Event(), asyncio.Event()
                commit = store.commit

                async def delayed_commit(lease, *, conversation=None, updates=()):
                    if lease == late:
                        entered.set()
                        await release.wait()
                    return await commit(lease, conversation=conversation, updates=updates)

                with patch.object(store, "commit", side_effect=delayed_commit):
                    task = asyncio.create_task(store.commit(late, updates=cleanup))
                    try:
                        await asyncio.wait_for(entered.wait(), 1)
                        await store.commit(owner_lease, updates=(StateUpdate("pending_effect_plan_id", "new-plan"),))
                    finally:
                        release.set()
                    await task
                after = await store.load(owner="owner", session_id=sid)
                self.assertEqual(after.pending_effect_plan_id, "new-plan", "晚到 CAS 不能按旧快照清除新计划")
                self.assertEqual(after.metadata, before.metadata)
                self.assertEqual(after.conversation, before.conversation)
                self.assertFalse(await store.is_current(late))
                self.assertTrue(await store.is_current(owner_lease))
                for conversation, updates in (
                    ([], cleanup),
                    (None, cleanup + (StateUpdate("summary", "late"),)),
                    (None, (StateUpdate("pending_effect_plan_id", ""),)),
                    (None, (StateUpdate("metadata.confirmed_publication", {}),)),
                ):
                    with self.assertRaises(StalePublicationError):
                        await store.commit(late, conversation=conversation, updates=updates)
                fresh, _ = await store.begin_turn(owner="owner", session_id=sid, request_id="new-generation")
                await store.commit(fresh, updates=(StateUpdate("pending_effect_plan_id", second),))
                with self.assertRaises(StalePublicationError):
                    await store.commit(late, updates=cleanup)
                self.assertEqual((await store.load(owner="owner", session_id=sid)).pending_effect_plan_id, second)

    async def test_duplicate_confirm_does_not_cancel_running_continuation(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                session, writes = self.runtime(persistent, [1000.0])
                sid = f"duplicate-running-{persistent}"
                first = await self.preview(session, sid)
                entered, release = asyncio.Event(), asyncio.Event()
                model_stream = session.model.stream

                async def delayed_stream(request, *, cancellation):
                    entered.set()
                    await release.wait()
                    async for event in model_stream(request, cancellation=cancellation):
                        yield event

                session.model.stream = delayed_stream
                task = asyncio.create_task(self.confirm(session, sid, first))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    self.assert_rejected(await self.confirm(session, sid, first))
                finally:
                    release.set()
                events = await asyncio.wait_for(task, 1)
                view = await consume_events(_events_stream(events))
                self.assertEqual(view.status, "approval_required", "重复确认不能取消已执行操作的后续规划")
                after = await session.state_store.load(owner="owner", session_id=sid)
                self.assertEqual(after.pending_effect_plan_id, view.approval.plan_id)
                self.assertEqual(writes, [1])

    async def test_transient_claim_failure_keeps_active_pending_plan(self):
        from unittest.mock import patch

        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                session, writes = self.runtime(persistent, [1000.0])
                sid = f"claim-unavailable-{persistent}"
                plan_id = await self.preview(session, sid)
                before = await session.state_store.load(owner="owner", session_id=sid)
                with patch.object(session.pipeline.effect_store, "claim", side_effect=RuntimeError("storage unavailable")):
                    self.assert_rejected(await self.confirm(session, sid, plan_id))
                after = await session.state_store.load(owner="owner", session_id=sid)
                self.assertEqual(after.pending_effect_plan_id, plan_id)
                self.assertIsNotNone(self.active(session, after, plan_id))
                self.assertEqual(after.conversation, before.conversation)
                self.assertEqual(writes, [])

    async def test_cancel_cannot_revoke_ticket_while_confirmation_is_claiming_it(self):
        from app.agent.kernel.state import SessionBusyError
        from unittest.mock import patch

        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                session, writes = self.runtime(persistent, [1000.0])
                sid = f"claim-cancel-{persistent}"
                plan_id = await self.preview(session, sid)
                before = await session.state_store.load(owner="owner", session_id=sid)
                tickets = session.pipeline.effect_store.store
                claim = tickets.claim_and_rotate_owner
                entered, release = threading.Event(), threading.Event()

                def delayed_claim(**kwargs):
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError("confirmation claim was not released")
                    return claim(**kwargs)

                with patch.object(tickets, "claim_and_rotate_owner", side_effect=delayed_claim):
                    task = asyncio.create_task(self.confirm(session, sid, plan_id))
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                        with self.assertRaises(SessionBusyError):
                            await session.cancel_effect(owner="owner", session_id=sid, plan_id=plan_id)
                        self.assertIsNotNone(self.active(session, before, plan_id), "取消被拒绝时不得已先撤销票据")
                    finally:
                        release.set()
                        events = await asyncio.wait_for(task, 3)
                view = await consume_events(_events_stream(events))
                self.assertEqual(view.status, "approval_required")
                self.assertEqual(writes, [1])


class ConfirmedEffectReplayTests(unittest.IsolatedAsyncioTestCase):
    """已确认的同轮重复调用读回原回执，而非产生第二张确认卡。"""

    @staticmethod
    def call(name, arguments, call_id):
        return [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                           tool_call=ModelToolCall(call_id, name, arguments)),
                ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]

    def runtime(self, tail, *, failed=False):
        prepares, writes, reads = [], [], []
        tool = KernelToolSpec(
            name="cloud.change", domain="cloud", description="变更",
            input_schema={"type": "object", "properties": {"step": {"type": "integer"}},
                          "required": ["step"], "additionalProperties": False},
            effect=ToolEffect.WRITE,
            prepare=lambda a, _: prepares.append(dict(a)) or PreparedEffect(
                preview={"summary": "变更预览"}, snapshot_fingerprint="snapshot"),
            execute_confirmed=lambda a, *_: writes.append(dict(a)) or ToolOutcome(
                model_content='{"ok":true,"summary":"变更已完成"}',
                public_content={"ok": not failed, "status": "failed" if failed else "success",
                                "summary": "变更失败" if failed else "变更已完成"},
                state_updates=(StateUpdate("metadata.marker", "original"),)),
        )
        read = read_tool("cloud.inspect", domain="cloud", handler=lambda *_: reads.append(1) or ToolOutcome(
            model_content="已查询", public_content={"ok": True, "summary": "已查询"},
            state_updates=(StateUpdate("metadata.marker", "read"),)))
        model = ScriptedModel([self.call(tool.name, {"step": 1}, "initial")] + tail)
        catalog = ToolCatalog([tool, read])
        state = InMemorySessionStateStore()
        session = AgentSession(model=model, catalog=catalog, retriever=CapabilityRetriever(),
                               pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state)
        return session, model, prepares, writes, reads

    async def confirm(self, session):
        preview = await consume_events(session.run(AgentInput(owner="owner", session_id="replay", message="变更")))
        self.assertIsNotNone(preview.approval)
        return await collect(session.confirm(owner="owner", session_id="replay", plan_id=preview.approval.plan_id))

    async def test_same_write_after_live_read_reuses_receipt_without_replaying_updates(self):
        for repair in (False, True):
            with self.subTest(repair=repair):
                tail = [self.call("cloud.inspect", {}, "read")]
                if repair:
                    tail.append(self.call("cloud.change", {}, "invalid"))
                tail += [self.call("cloud.change", {"step": 1}, "repeat"),
                         [ModelEvent(ModelEventType.TEXT_DELTA, text="变更已完成。"),
                          ModelEvent(ModelEventType.FINISH, finish_reason="stop")]]
                session, model, prepares, writes, reads = self.runtime(tail)
                events = await self.confirm(session)
                final = await consume_events(_events_stream(events))
                self.assertEqual(final.status, "success")
                self.assertIsNone(final.approval)
                self.assertEqual(prepares, [{"step": 1}])
                self.assertEqual(writes, [{"step": 1}])
                self.assertEqual(reads, [1])
                self.assertFalse(any(e.type == AgentEventType.EFFECT_APPROVAL_REQUIRED for e in events))
                current = await session.state_store.load(owner="owner", session_id="replay")
                self.assertEqual(current.metadata["marker"], "read")
                repeated = [m for m in model.requests[-1].messages if m.tool_call_id == "repeat"]
                self.assertEqual(len(repeated), 1)
                self.assertIn("不是再次执行", repeated[0].content)
                if repair:
                    self.assertTrue(any(e.type == AgentEventType.TOOL_FAILED and e.payload["code"] == "invalid_arguments" for e in events))

    async def test_changed_arguments_still_require_a_new_confirmation(self):
        session, _, prepares, writes, _ = self.runtime([self.call("cloud.change", {"step": 2}, "next")])
        final = await consume_events(_events_stream(await self.confirm(session)))
        self.assertEqual(final.status, "approval_required")
        self.assertIsNotNone(final.approval)
        self.assertEqual(prepares, [{"step": 1}, {"step": 2}])
        self.assertEqual(writes, [{"step": 1}])

    async def test_new_user_turn_does_not_reuse_previous_confirmation(self):
        session, model, prepares, writes, _ = self.runtime([
            [ModelEvent(ModelEventType.TEXT_DELTA, text="已完成。"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")]])
        await self.confirm(session)
        model.rounds.append(self.call("cloud.change", {"step": 1}, "new-user"))
        final = await consume_events(session.run(AgentInput(owner="owner", session_id="replay", message="重新执行一次变更")))
        self.assertEqual(final.status, "approval_required")
        self.assertEqual(prepares, [{"step": 1}, {"step": 1}])
        self.assertEqual(writes, [{"step": 1}])

    async def test_failed_effect_cannot_enter_successful_replay_continuation(self):
        session, model, prepares, writes, _ = self.runtime([], failed=True)
        final = await consume_events(_events_stream(await self.confirm(session)))
        self.assertFalse(final.effect_result["ok"])
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(prepares, [{"step": 1}])
        self.assertEqual(writes, [{"step": 1}])

    async def test_receipt_reuse_still_checks_authorization_schema_cancellation_and_scope(self):
        from dataclasses import replace
        from unittest.mock import AsyncMock, patch

        session, _, prepares, writes, _ = self.runtime([])
        pipeline, store = session.pipeline, session.state_store
        lease, _ = await store.begin_turn(owner="owner", session_id="replay", request_id="initial")
        context = ToolCallContext(owner="owner", session_id="replay", request_id="initial",
                                  turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(),
                                  report_progress=AsyncMock())
        preview = await pipeline.execute("cloud.change", {"step": 1}, context=context)
        completed = await pipeline.execute_confirmed(preview.effect_plan.plan_id, context=context)
        continuation = replace(context, confirmed_effect=completed)
        replay = await pipeline.execute("cloud.change", {"step": 1}, context=continuation)
        self.assertIsNone(replay.effect_plan)
        self.assertIsNone(replay.outcome.effect_plan)
        self.assertEqual(replay.outcome.refs, ())
        self.assertEqual(replay.outcome.state_updates, ())
        self.assertEqual(replay.elapsed_ms, 0)
        self.assertEqual(replay.outcome.public_content, completed.outcome.public_content)
        for arguments in ({}, {"step": True}, {"step": 1, "unexpected": True}):
            with self.assertRaises(ToolPipelineError) as invalid:
                await pipeline.execute("cloud.change", arguments, context=continuation)
            self.assertEqual(invalid.exception.code, "invalid_arguments")
        with patch.object(pipeline.authorization, "authorize", new=AsyncMock(
            side_effect=ToolPipelineError("已撤销授权", code="authorization_denied"))):
            with self.assertRaises(ToolPipelineError) as denied:
                await pipeline.execute("cloud.change", {"step": 1}, context=continuation)
            self.assertEqual(denied.exception.code, "authorization_denied")
        cancelled = CancellationToken()
        cancelled.cancel("测试取消")
        with self.assertRaises(asyncio.CancelledError):
            await pipeline.execute("cloud.change", {"step": 1}, context=replace(continuation, cancellation=cancelled))
        self.assertEqual(len(prepares), 1)
        self.assertEqual(writes, [{"step": 1}])
        for owner, sid in (("other-owner", "replay"), ("owner", "other-session"), ("owner", "replay")):
            fresh, _ = await store.begin_turn(owner=owner, session_id=sid, request_id="fresh")
            foreign = replace(continuation, owner=owner, session_id=sid, lease=fresh, turn_id=fresh.turn_id)
            next_preview = await pipeline.execute("cloud.change", {"step": 1}, context=foreign)
            self.assertIsNotNone(next_preview.effect_plan, "旧回执不能跨身份、会话或代次复用")
            self.assertEqual(writes, [{"step": 1}])
        with self.assertRaises(StalePublicationError):
            await pipeline.execute("cloud.change", {"step": 1}, context=continuation)
