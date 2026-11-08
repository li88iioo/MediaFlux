"""Session 公开入口的冷恢复、事件终态与一次性确认契约。"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.kernel.ports.resource_presentation import ResourceSelection
from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.events import TERMINAL_EVENT_TYPES, AgentEventType
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput
from app.agent.public_view import public_conversation_messages
from tests.support import isolated_test_database
from tests.test_agent_kernel_core import ScriptedModel, collect


@pytest.mark.parametrize("read_access", ["allowed", "revoked", "unavailable", "write"])
def test_confirm_readback_is_loaded_without_discovery_but_never_grants_permission(tmp_path, read_access):
    """离线模型脚本；真实SQLite冷恢复与临时文件改名/读取，不是付费模型实测。"""
    async def scenario():
        source, target = tmp_path / "source.txt", tmp_path / "renamed.txt"
        source.write_bytes(b"readback-regression")
        writes, reads = [], []
        authorized = True

        def execute(*_):
            source.rename(target)
            writes.append("rename")
            return {"ok": True, "status": "completed", "summary": "文件改名完成"}

        def inspect(*_):
            reads.append(target.read_bytes())
            return {"ok": True, "status": "success", "summary": "已重新读取文件，内容一致"}

        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        write = KernelToolSpec(
            name="fixture.rename", domain="files", description="文件改名", input_schema=schema,
            effect=ToolEffect.WRITE, metadata={"related_tools": ("fixture.inspect", "removed.tool")},
            prepare=lambda *_: PreparedEffect(preview={"summary": "改名预览"}, snapshot_fingerprint="file"),
            execute_confirmed=execute,
        )
        read = KernelToolSpec(
            name="fixture.inspect", domain="files", description="核验已改名文件", input_schema=schema,
            effect=ToolEffect.READ,
            authorize=lambda _: authorized,
            availability=lambda _: read_access != "unavailable", read=inspect,
        )
        unrelated = replace(read, name="fixture.unrelated", model_name="", effect=ToolEffect.READ,
                            authorize=None, availability=None)
        if read_access == "write":
            read = replace(read, effect=ToolEffect.WRITE, read=None,
                prepare=lambda *_: pytest.fail("不能自动加载相关写工具并为其生成新卡"),
                execute_confirmed=lambda *_: pytest.fail("不能执行未确认写入"))
        catalog = ToolCatalog([write, read, unrelated])

        def call(name, call_id):
            return [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                               tool_call=ModelToolCall(call_id, name, {})),
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]

        def answer(text):
            return [ModelEvent(ModelEventType.TEXT_DELTA, text=text),
                    ModelEvent(ModelEventType.FINISH, finish_reason="stop")]

        model = ScriptedModel([call(write.name, "rename"), answer("只解释预览，尚未执行。"),
                               call(read.name, "readback"), answer("改名和重新读取全部完成。")])

        def cold_session():
            store = SQLiteKernelStore(secret_provider=lambda: "readback-test-only")
            return AgentSession(
                model=model, catalog=catalog, state_store=store,

                model_tools=[write.name, read.name] if read_access != "write" else [write.name],
                pipeline=ToolPipeline(catalog=catalog, state_store=store, reference_store=store,
                    effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore())),
            )

        preview = await collect(cold_session().run(AgentInput(
            message="改名后重新读取核对", owner="readback-owner", session_id="readback-session")))
        plan_id = preview[-1].payload["plan_id"]
        options = dict(owner="readback-owner", session_id="readback-session", plan_id=plan_id)
        await collect(cold_session().explain(**options, message="只解释，不执行"))
        assert model.requests[-1].tools == () and writes == [] and reads == []
        authorized = read_access != "revoked"
        events = await collect(cold_session().confirm(**options))
        names = {tool["name"] for tool in model.requests[2].tools}
        assert (read.model_name in names) == (read_access == "allowed")
        assert len(names) <= 2  # 不扩大业务窗口。
        assert writes == ["rename"] and not source.exists()
        assert target.read_bytes() == b"readback-regression"
        assert reads == ([b"readback-regression"] if read_access == "allowed" else [])
        failures = [e.payload["code"] for e in events if e.type is AgentEventType.TOOL_FAILED]
        assert failures == ([] if read_access == "allowed" else ["tool_not_available"])
        assert len(model.requests) == 4  # 确认后直接读取+回答，不再发现/重试。
        terminal = events[-1].payload
        assert terminal["status"] == ("success" if read_access == "allowed" else "partial")
        if read_access != "allowed":
            assert "改名和重新读取全部完成。" not in terminal["answer"]
            assert "文件改名完成" in terminal["answer"]
        cold = await cold_session().state_store.load(owner="readback-owner", session_id="readback-session")
        assert public_conversation_messages(cold.conversation)[-1]["content"] == terminal["answer"]
        await collect(cold_session().confirm(**options))
        assert len(model.requests) == 4 and writes == ["rename"]

    with isolated_test_database("confirm-readback-contract.db"):
        asyncio.run(scenario())


def test_cold_explain_confirm_keep_event_and_plan_contract() -> None:
    async def scenario() -> None:
        prepared: list[str] = []
        writes: list[str] = []
        goal = "确认后修改示例目录并报告结果"
        tool = KernelToolSpec(
            name="fixture.change", domain="library", description="隔离的目录变更",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect=ToolEffect.WRITE,
            prepare=lambda *_: prepared.append("preview") or PreparedEffect(
                preview={"summary": "确认后修改示例目录"}, snapshot_fingerprint="fixture"),
            execute_confirmed=lambda *_: writes.append("done") or {
                "ok": True, "status": "success", "summary": "示例目录修改完成"},
        )
        catalog = ToolCatalog([tool])
        model = ScriptedModel([
            [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall("change", tool.name, {})),
             ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
            [ModelEvent(ModelEventType.TEXT_DELTA, text="这是目录变更预览，尚未执行。"),
             ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
            [ModelEvent(ModelEventType.TEXT_DELTA, text="示例目录修改完成。"),
             ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
        ])
        plan_id = ""
        turn_ids: list[str] = []
        for entry in ("run", "explain", "confirm", "duplicate"):
            # 每次从同一隔离 SQLite 重新构造，不依赖旧 Session 的内存状态。
            store = SQLiteKernelStore(secret_provider=lambda: "session-flow-test-secret")
            session = AgentSession(
                model=model, catalog=catalog,  state_store=store,
                pipeline=ToolPipeline(
                    catalog=catalog, state_store=store, reference_store=store,
                    effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore())),
            )
            request_id = f"contract-{entry}"
            if entry == "run":
                stream = session.run(AgentInput(
                    owner="contract-owner", session_id="contract-session",
                    message=goal, request_id=request_id))
            elif entry == "explain":
                stream = session.explain(
                    owner="contract-owner", session_id="contract-session", plan_id=plan_id,
                    message="只解释这个计划", request_id=request_id)
            else:
                stream = session.confirm(
                    owner="contract-owner", session_id="contract-session", plan_id=plan_id,
                    request_id=request_id)
            events = [event async for event in stream]
            types = [event.type for event in events]
            assert [event.sequence for event in events] == list(range(1, len(events) + 1))
            assert {event.request_id for event in events} == {request_id}
            assert {event.session_id for event in events} == {"contract-session"}
            assert len({event.turn_id for event in events}) == 1
            assert [event for event in events if event.type in TERMINAL_EVENT_TYPES] == [events[-1]]
            turn_ids.append(events[-1].turn_id)
            saved = await SQLiteKernelStore(
                secret_provider=lambda: "session-flow-test-secret").load(
                    owner="contract-owner", session_id="contract-session")
            if entry == "run":
                plan_id = saved.pending_effect_plan_id
                assert plan_id and events[-1].payload["status"] == "approval_required"
                assert types.index(AgentEventType.EFFECT_PREVIEW_STARTED) < types.index(
                    AgentEventType.EFFECT_APPROVAL_REQUIRED) < len(events) - 1
            elif entry == "explain":
                assert events[-1].payload["status"] == "success"
                assert model.requests[-1].tools == ()
                assert not any(kind in types for kind in (
                    AgentEventType.TOOL_STARTED, AgentEventType.EFFECT_APPROVAL_REQUIRED,
                    AgentEventType.EFFECT_COMPLETED))
            elif entry == "confirm":
                assert events[-1].payload["status"] == "success"
                assert types.index(AgentEventType.EFFECT_COMPLETED) < len(events) - 1
                assert any(row.role == "user" and row.content == goal
                           for row in model.requests[-1].messages)
            else:
                assert events[-1].type is AgentEventType.TURN_FAILED
                assert events[-1].payload["code"] == "confirmation_invalid"
            if entry in {"run", "explain"}:
                assert saved.pending_effect_plan_id == plan_id
                assert writes == []
            else:
                assert not saved.pending_effect_plan_id
                assert writes == ["done"]
                results = [row for row in saved.conversation if row.get("tool_call_id") == "change"]
                assert len(results) == 1 and json.loads(results[0]["content"])["ok"] is True
                assert len([row for row in saved.conversation
                            if row.get("completion_receipt_id") == plan_id]) == 1
            assert prepared == ["preview"]
        assert len(set(turn_ids)) == len(turn_ids)
        assert len(model.requests) == 3

    with isolated_test_database("session-flow-contract.db"):
        asyncio.run(asyncio.wait_for(scenario(), timeout=10))


@pytest.mark.parametrize("cancel_first", [False, True], ids=["complete", "cancel"])
def test_concurrent_public_runs_keep_different_sessions_isolated(cancel_first: bool) -> None:
    async def scenario() -> None:
        entered = {name: asyncio.Event() for name in ("A", "B")}
        release = {name: asyncio.Event() for name in ("A", "B")}
        observed: list[tuple[str, str]] = []

        async def read(arguments, context):
            name = arguments["selector"]
            observed.append((context.session_id, name))
            entered[name].set()
            await release[name].wait()
            return {"ok": True, "data": {"fact": f"fact-{name}"}}

        tool = KernelToolSpec(
            name="fixture.inspect", domain="library", description="独立读取示例对象",
            input_schema={"type": "object", "properties": {"selector": {"type": "string"}},
                          "required": ["selector"], "additionalProperties": False},
            effect=ToolEffect.READ, read=read,
        )
        catalog = ToolCatalog([tool])
        rounds = [[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                              tool_call=ModelToolCall("same-call", tool.name, {"selector": name})),
                   ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]
                  for name in ("A", "B")]
        rounds.extend([[ModelEvent(ModelEventType.TEXT_DELTA, text=f"已核对{name}。"),
                        ModelEvent(ModelEventType.FINISH, finish_reason="stop")]
                       for name in (("B",) if cancel_first else ("A", "B"))])
        model = ScriptedModel(rounds)
        store = SQLiteKernelStore(secret_provider=lambda: "session-flow-test-secret")
        session = AgentSession(
            model=model, catalog=catalog,  state_store=store,
            pipeline=ToolPipeline(catalog=catalog, state_store=store, reference_store=store),
        )
        tasks = []
        try:
            for name in ("A", "B"):
                tasks.append(asyncio.create_task(collect(session.run(AgentInput(
                    owner="concurrent-owner", session_id=f"session-{name}",
                    request_id=f"request-{name}", message=f"读取{name}")))))
                await asyncio.wait_for(entered[name].wait(), timeout=2)
            assert observed == [("session-A", "A"), ("session-B", "B")]
            if cancel_first:
                assert await session.cancel(
                    owner="concurrent-owner", session_id="session-A", request_id="request-A")
            release["A"].set()
            first = await asyncio.wait_for(tasks[0], timeout=2)
            assert first[-1].type is (
                AgentEventType.TURN_CANCELLED if cancel_first else AgentEventType.TURN_COMPLETED)
            assert not tasks[1].done(), "A 的结束或取消不能结束 B 的流"
            release["B"].set()
            second = await asyncio.wait_for(tasks[1], timeout=2)
            assert second[-1].payload["status"] == "success"
            for name, events in zip(("A", "B"), (first, second), strict=True):
                assert {event.session_id for event in events} == {f"session-{name}"}
                assert {event.request_id for event in events} == {f"request-{name}"}
                assert len({event.turn_id for event in events}) == 1
                assert [event.sequence for event in events] == list(range(1, len(events) + 1))
                assert [event for event in events if event.type in TERMINAL_EVENT_TYPES] == [events[-1]]
                cold = await SQLiteKernelStore(
                    secret_provider=lambda: "session-flow-test-secret").load(
                        owner="concurrent-owner", session_id=f"session-{name}")
                assert [row["content"] for row in cold.conversation if row["role"] == "user"] == [f"读取{name}"]
                other = "B" if name == "A" else "A"
                assert f"fact-{other}" not in json.dumps(cold.conversation)
            assert first[-1].turn_id != second[-1].turn_id
            assert [row.content for row in model.requests[-1].messages if row.role == "user"] == ["读取B"]
            results = [row for row in model.requests[-1].messages if row.role == "tool"]
            assert [row.tool_call_id for row in results] == ["same-call"]
            assert json.loads(results[0].content)["data"]["fact"] == "fact-B"
            assert len(model.requests) == (3 if cancel_first else 4)
        finally:
            for event in release.values():
                event.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    with isolated_test_database("session-concurrent-contract.db"):
        asyncio.run(asyncio.wait_for(scenario(), timeout=10))


@pytest.mark.parametrize("failure", ["selection", "admission"])
def test_early_rejection_does_not_publish_a_late_terminal_or_poison_next_run(
    monkeypatch, failure: str,
) -> None:
    async def scenario() -> None:
        store = SQLiteKernelStore(secret_provider=lambda: "session-flow-test-secret")
        catalog = ToolCatalog([], selection=ResourceSelection())
        model = ScriptedModel([[ModelEvent(ModelEventType.TEXT_DELTA, text="正常后续回答。"),
                                ModelEvent(ModelEventType.FINISH, finish_reason="stop")]])
        session = AgentSession(
            model=model, catalog=catalog,  state_store=store,
            pipeline=ToolPipeline(catalog=catalog, state_store=store, reference_store=store),
            journal=store,
        )
        before = await store.load(owner="reject-owner", session_id="reject-session")
        metadata = {"selection": {"ref": "ref_" + "z" * 24, "positions": [1], "target": "guangya"}}
        with monkeypatch.context() as patch:
            if failure == "admission":
                patch.setattr(session, "turn_admission", Mock(
                    begin=AsyncMock(side_effect=ToolPipelineError(
                        "隔离测试拒绝准入", code="authorization_denied")),
                    is_current=AsyncMock(return_value=False)))
            rejected = await collect(session.run(AgentInput(
                owner="reject-owner", session_id="reject-session", request_id="rejected",
                message="拒绝这个请求", metadata=metadata if failure == "selection" else {})))
        assert len(rejected) == 1 and rejected[0].type is AgentEventType.TURN_FAILED
        assert rejected[0].payload["code"] == (
            "selection_invalid" if failure == "selection" else "authorization_denied")
        assert model.requests == []
        assert await store.load(owner="reject-owner", session_id="reject-session") == before
        journal = await store.list_events(owner="reject-owner", session_id="reject-session")
        following = await collect(session.run(AgentInput(
            owner="reject-owner", session_id="reject-session", request_id="following", message="正常对话")))
        assert following[-1].payload["status"] == "success"
        assert {event.request_id for event in following} == {"following"}
        assert [event for event in following if event.type in TERMINAL_EVENT_TYPES] == [following[-1]]
        later = await store.list_events(owner="reject-owner", session_id="reject-session")
        assert [event for event in later if event["request_id"] == "rejected"] == journal
        assert len(model.requests) == 1
        assert [row.content for row in model.requests[0].messages if row.role == "user"] == ["正常对话"]

    with isolated_test_database("session-early-rejection-contract.db"):
        asyncio.run(asyncio.wait_for(scenario(), timeout=10))
