"""真实SQLite会话文件锁争用控制：网络/模型为脚本，不伪造引用写入结果。"""
import asyncio
import json
import threading
from unittest.mock import patch

from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.session_guard import _held_scope
from app.agent.kernel.state_types import AgentInput, _CancelledReadInline
from tests.support import isolated_test_database
from tests.test_agent_kernel_core import ScriptedModel
from tests.test_agent_parallel_reads import query_tool


def test_parallel_reads_queue_real_reference_transactions_without_false_busy():
    async def scenario():
        loop = asyncio.get_running_loop()
        lock_held, both_started, second_captured = asyncio.Event(), asyncio.Event(), asyncio.Event()
        release = threading.Event()
        blocked = False
        observed = []
        store = SQLiteKernelStore(secret_provider=lambda: "parallel-publication-isolated-key")
        original_put = store._put_ref_sync
        original_capture = _CancelledReadInline.put_once

        def blocked_put(*args, **kwargs):
            nonlocal blocked
            if not blocked:
                held = _held_scope.get()
                assert held and held.active and held.kind == "commit", "必须真实持有会话文件锁"
                blocked = True
                loop.call_soon_threadsafe(lock_held.set)
                assert release.wait(10), "控制门未释放"
            return original_put(*args, **kwargs)

        def observe_capture(receipt, content):
            original_capture(receipt, content)
            if receipt.call_index == 1:
                second_captured.set()

        async def read(arguments, context):
            observed.append(context.call_id)
            if len(observed) == 2:
                both_started.set()
            await both_started.wait()
            if arguments["query"] == "second":
                await lock_held.wait()
            return {"ok": True, "data": {"query": arguments["query"], "detail": "fact" * 400}}

        tool = query_tool(read)
        catalog = ToolCatalog([tool])
        model = ScriptedModel([
            [*[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(name, tool.name, {"query": name}))
               for name in ("first", "second")], ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
            [ModelEvent(ModelEventType.TEXT_DELTA, text="两项查询均已核对。"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
        ])
        pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store)
        session = AgentSession(model=model, catalog=catalog,  pipeline=pipeline, state_store=store)

        async def collect():
            return [event async for event in session.run(AgentInput(owner="owner", session_id="publication", message="查询两项媒体信息"))]

        with patch.object(store, "_put_ref_sync", blocked_put), patch.object(_CancelledReadInline, "put_once", observe_capture):
            running = asyncio.create_task(collect())
            try:
                await asyncio.wait_for(second_captured.wait(), timeout=10)
                # 第二handler确已返回，但第一份引用仍真实占有文件锁；不使用sleep碰运气。
                assert blocked and not release.is_set()
                release.set()
                events = await asyncio.wait_for(running, timeout=10)
            finally:
                release.set()
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)
        assert events[-1].payload["status"] == "success", events[-1].payload
        assert observed == ["first", "second"]
        results = [json.loads(message.content) for message in model.requests[-1].messages if message.role == "tool"]
        assert [result["data"]["query"] for result in results] == ["first", "second"]
        for result in results:
            assert result["result_handle"]
            assert await store.resolve(result["result_handle"], owner="owner", session_id="publication", expected_kind="tool_result")
    with isolated_test_database():
        asyncio.run(scenario())


def test_confirmed_receipt_releases_guard_before_parallel_followup_reads():
    """真实确认与SQLite保护域；写handler仅记录调用，不操作实际媒体。"""
    async def scenario():
        from app.agent.kernel.capabilities import KernelToolSpec, ToolEffect
        from app.agent.kernel.tool_calls import PreparedEffect
        store = SQLiteKernelStore(secret_provider=lambda: "confirmed-followup-isolated-key")
        executed, observed_tasks = [], []
        both_reads = asyncio.Event()

        async def write(*_args):
            held = _held_scope.get()
            assert held and held.active and held.kind == "effect" and held.task is asyncio.current_task()
            executed.append(asyncio.current_task())
            return {"ok": True, "status": "completed", "summary": "受控变更已完成"}

        async def read(arguments, _context):
            held = _held_scope.get()
            assert held is None or not held.active
            state = await store.load(owner="owner", session_id="confirmed")
            assert not state.pending_effect_plan_id
            assert any(row.get("completion_receipt_id") == plan for row in state.conversation)
            observed_tasks.append(asyncio.current_task())
            if len(observed_tasks) == 2:
                both_reads.set()
            await asyncio.wait_for(both_reads.wait(), timeout=2)
            return {"ok": True, "data": {"query": arguments["query"], "detail": "fact" * 400}}

        read_spec = query_tool(read)
        write_spec = KernelToolSpec(name="test.change", domain="test", description="受控确认测试",
            effect=ToolEffect.WRITE, input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            prepare=lambda *args: PreparedEffect(preview={"summary": "确认后执行"}, snapshot_fingerprint="controlled"),
            execute_confirmed=write)
        catalog = ToolCatalog([read_spec, write_spec])
        model = ScriptedModel([
            [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("prepare", write_spec.name, {})),
             ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
            [*[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(q, read_spec.name, {"query": q}))
               for q in ("first", "second")], ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
            [ModelEvent(ModelEventType.TEXT_DELTA, text="已完成后续只读核验。"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
        ])
        session = AgentSession(model=model, catalog=catalog,
            pipeline=ToolPipeline(catalog=catalog, state_store=store, reference_store=store), state_store=store)
        first = [event async for event in session.run(AgentInput(owner="owner", session_id="confirmed", message="准备受控变更，确认后核验两项"))]
        assert first[-1].payload["status"] == "approval_required"
        plan = (await store.load(owner="owner", session_id="confirmed")).pending_effect_plan_id
        final = [event async for event in session.confirm(owner="owner", session_id="confirmed", plan_id=plan)]
        assert final[-1].payload["status"] == "success", final[-1].payload
        assert len(executed) == 1 and len(set(observed_tasks)) == 2
        assert all(task is not executed[0] for task in observed_tasks)
        assert model.requests[1].allow_parallel_tool_calls is True
    with isolated_test_database():
        asyncio.run(scenario())
