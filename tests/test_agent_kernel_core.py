from __future__ import annotations

import asyncio
import unittest
from collections.abc import AsyncIterator

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
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.state import (
    AgentInput,
    CancellationToken,
    InMemorySessionStateStore,
)
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


async def collect(stream) -> list:
    return [event async for event in stream]


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


class AgentSessionTests(unittest.IsolatedAsyncioTestCase):
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

        bounded = session._bounded_model_messages(
            messages,
            history_end=4,
            tool_definitions=(catalog.get("library.status").model_definition(),),
        )

        self.assertEqual(bounded[-1].content, "current question")
        self.assertTrue(any(item.content.startswith("recent:") for item in bounded))
        self.assertNotIn("old answer", [item.content for item in bounded])
        self.assertEqual(bounded[0].role, "user")

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

    async def test_write_only_freezes_plan_and_confirmation_never_calls_model(
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
        self.assertEqual(len(model.requests), model_call_count)
        self.assertIn(
            AgentEventType.EFFECT_COMPLETED,
            [event.type for event in confirmed_events],
        )
        self.assertEqual(confirmed_events[-1].payload["status"], "effect_completed")

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
        self.assertIn(AgentEventType.EFFECT_FAILED, [event.type for event in replay])

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
        self.assertIn(AgentEventType.EFFECT_FAILED, [event.type for event in stale])

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
        self.assertEqual(confirmed[-1].payload["status"], "effect_completed")

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
