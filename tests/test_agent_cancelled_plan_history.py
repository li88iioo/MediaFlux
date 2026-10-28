"""取消观察回归：真实 Kernel/SQLite，原生 mock 与脚本模型，非真实 LLM。

只验证服务端事实、冷历史和模型输入，不以预设模型答句冒充语义验收。
NativeRuntime 的 HTTP 请求全部经过 MockTransport；测试禁止外网连接。
"""

import asyncio
import json
import socket
import threading
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

import pytest

from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel.adapters import consume_events
from app.agent.kernel.capabilities import (
    CapabilityRetriever,
    KernelToolSpec,
    ToolCatalog,
    ToolEffect,
)
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.events import AgentEventType as E
from app.agent.kernel.model import ModelEvent, ModelToolCall
from app.agent.kernel.model import ModelEventType as M
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.state_types import AgentInput, SessionBusyError
from tests.support import isolated_test_database
from tests.test_pending_explanation_transports import NativeRuntime

OWNER, SID = "cancel-history-owner", "cancel-history-session"


@pytest.fixture
def runtime(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("本测试仅用 mock/脚本模型，禁止真实网络连接")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    with isolated_test_database("cancelled-plan-history.db"):
        runtime = NativeRuntime()
        original = runtime.catalog.get("cloud.change")

        def prepare(arguments, context):
            assert original.prepare is not None
            prepared = original.prepare(arguments, context)
            return replace(prepared, preview={**prepared.preview, "status": "confirmation_required"})

        runtime.catalog = ToolCatalog([replace(original, prepare=prepare)])
        runtime.reopen()
        yield runtime


class ScriptedModel:
    """明确按脚本发调用；不模拟 LLM 的理解或语言能力。"""

    def __init__(self, batches):
        self.batches = list(batches)

    async def stream(self, request, *, cancellation):
        cancellation.raise_if_cancelled()
        if self.batches:
            for call in self.batches.pop(0):
                yield ModelEvent(M.TOOL_CALL_COMPLETED, tool_call=call)
            yield ModelEvent(M.FINISH, finish_reason="tool_calls")
        else:
            yield ModelEvent(M.TEXT_DELTA, text="脚本模型：执行事实以服务端观察为准。")
            yield ModelEvent(M.FINISH, finish_reason="stop")


async def preview(runtime, step=1, *, owner=OWNER):
    runtime.once_call = True
    runtime.call_name = None
    runtime.call_arguments = json.dumps({"step": step})
    view = await consume_events(runtime.session.run(AgentInput(
        message=f"改名一个对象，方案 {step}", owner=owner, session_id=SID,
    )))
    assert view.approval is not None, view
    return view.approval.plan_id


async def cold_state(runtime, *, owner=OWNER):
    return await SQLiteKernelStore().load(owner=owner, session_id=SID)


def cold_reopen(runtime):
    runtime.effects = ConfirmationEffectPlanStore(
        SQLiteConfirmationStore(clock=lambda: runtime.now)
    )
    runtime.reopen()


def plan_row(state, plan_id):
    rows = [row for row in state.conversation
            if row.get("role") == "tool" and row.get("effect_plan_id") == plan_id]
    assert len(rows) == 1, rows
    return rows[0]


def assert_cancelled(state, plan_id):
    row = plan_row(state, plan_id)
    assert row.get("effect_cancelled") is True, row
    content = json.loads(row["content"])
    assert content["status"] == "cancelled", content
    assert content["data"]["plan_id"] == plan_id
    assert content["data"]["preview_created"] is True
    assert content["data"]["executed"] is False
    return row


def assert_not_cancelled(state, plan_id):
    row = plan_row(state, plan_id)
    assert row.get("effect_cancelled") is not True, row
    assert json.loads(row["content"])["status"] != "cancelled", row


def wire_result(runtime):
    messages = runtime.wires[-1]["messages"]
    # 原生出站历史会重编号 ID；按实际出站调用关联结果，不假设内部 ID 原样发送。
    call_ids = {call["id"] for row in messages for call in row.get("tool_calls") or ()
                if call["function"]["name"] == "cloud__change"
                and json.loads(call["function"]["arguments"]) == {"step": 1}}
    rows = [row for row in messages
            if row.get("role") == "tool" and row.get("tool_call_id") in call_ids]
    assert len(call_ids) == 1, call_ids
    assert len(rows) == 1, rows
    return json.loads(rows[0]["content"])


async def followup(runtime):
    cold_reopen(runtime)
    view = await consume_events(runtime.session.run(AgentInput(
        message="刚才的确认卡是否已取消？只读核对历史，不执行。",
        owner=OWNER, session_id=SID,
    )))
    assert view.status == "success" and view.approval is None, view
    observed = wire_result(runtime)
    assert observed["status"] == "cancelled", observed
    assert observed["data"]["preview_created"] is True
    assert observed["data"]["executed"] is False
    assert runtime.writes == []


def test_button_cancellation_is_in_cold_history_and_native_model_input(runtime):
    async def run():
        plan_id = await preview(runtime)
        before = await cold_state(runtime)
        original = plan_row(before, plan_id)
        assert json.loads(original["content"])["status"] == "confirmation_required"
        assert await runtime.session.cancel_effect(
            owner=OWNER, session_id=SID, plan_id=plan_id,
        )
        after = await cold_state(runtime)
        cancelled = assert_cancelled(after, plan_id)
        assert cancelled["tool_call_id"] == original["tool_call_id"]
        assert after.pending_effect_plan_id == ""
        assert after.generation == before.generation
        for old, new in zip(before.conversation, after.conversation, strict=True):
            if old is not original:
                assert new == old
        assert runtime.writes == [] and len(runtime.prepares) == 1
        await followup(runtime)
        assert_cancelled(await cold_state(runtime), plan_id)

    asyncio.run(run())


def test_interrupted_ticket_cancellation_keeps_lock_until_cold_history_is_settled(runtime):
    async def run():
        plan_id = await preview(runtime)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        cancel = runtime.effects.cancel

        def held_cancel(**kwargs):
            entered.set()
            try:
                assert release.wait(5), "取消票据线程未释放"
                return cancel(**kwargs)
            finally:
                finished.set()

        with patch.object(runtime.effects, "cancel", side_effect=held_cancel):
            task = asyncio.create_task(runtime.session.cancel_effect(
                owner=OWNER, session_id=SID, plan_id=plan_id,
            ))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                for _ in range(3):
                    task.cancel()
                    delivered = asyncio.Event()
                    asyncio.get_running_loop().call_soon(delivered.set)
                    await delivered.wait()
                    assert not task.done(), "撤票工作项结束前不能释放会话或丢弃真实结果"
                    with pytest.raises(SessionBusyError):
                        await SQLiteKernelStore().begin_turn(
                            owner=OWNER, session_id=SID, request_id="peer-during-cancel",
                        )
            finally:
                release.set()
                outcome = (await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5))[0]
                assert await asyncio.to_thread(finished.wait, 5)
        assert outcome is True
        cold_reopen(runtime)
        state = await cold_state(runtime)
        assert state.pending_effect_plan_id == ""
        assert_cancelled(state, plan_id)
        assert runtime.writes == []
        await followup(runtime)

    asyncio.run(run())


def test_repeated_cancel_and_confirmation_never_replay_cancelled_ticket(runtime):
    async def run():
        plan_id = await preview(runtime)
        assert await runtime.session.cancel_effect(
            owner=OWNER, session_id=SID, plan_id=plan_id,
        )
        cold_reopen(runtime)
        before = await cold_state(runtime)
        calls = len(runtime.wires)
        for _ in range(2):
            assert not await runtime.session.cancel_effect(
                owner=OWNER, session_id=SID, plan_id=plan_id,
            )
            result = await consume_events(runtime.session.confirm(
                owner=OWNER, session_id=SID, plan_id=plan_id,
            ))
            assert result.status == "failed"
            assert result.error_code == "confirmation_invalid"
        after = await cold_state(runtime)
        assert_cancelled(after, plan_id)
        assert after.conversation == before.conversation
        assert len(runtime.wires) == calls
        assert runtime.writes == [] and len(runtime.prepares) == 1

    asyncio.run(run())


def test_late_ordinary_read_checkpoint_keeps_same_generation_cancellation(runtime):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        reads = []

        async def read(arguments, context):
            reads.append(dict(arguments))
            entered.set()
            await release.wait()
            return {"ok": True, "status": "success", "summary": "普通 READ 完成",
                    "data": {"checkpoint": "late-read"}}

        tool = KernelToolSpec(
            name="cloud.inspect", domain="cloud", description="只读查看对象状态",
            input_schema={"type": "object", "properties": {}},
            effect=ToolEffect.READ, read=read,
        )
        runtime.catalog.register(tool)
        runtime.session.retriever = CapabilityRetriever(minimum=2, maximum=2)
        plan_id = await preview(runtime)
        runtime.once_call = True
        runtime.call_name, runtime.call_arguments = tool.model_name, "{}"
        task = asyncio.create_task(consume_events(runtime.session.run(AgentInput(
            message="cloud.inspect 只读查看对象状态，不执行待确认卡。",
            owner=OWNER, session_id=SID,
        ))))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            waiting = await cold_state(runtime)
            assert json.loads(plan_row(waiting, plan_id)["content"])["status"] == "confirmation_required"
            assert wire_result(runtime)["status"] == "confirmation_required"
            assert await runtime.session.cancel_effect(
                owner=OWNER, session_id=SID, plan_id=plan_id,
            )
            assert (await cold_state(runtime)).generation == waiting.generation
        finally:
            release.set()
            result = await asyncio.wait_for(task, 5)
        assert result.status == "success", result
        state = await cold_state(runtime)
        assert state.generation == waiting.generation and state.pending_effect_plan_id == ""
        assert_cancelled(state, plan_id)
        assert any(row.get("role") == "tool"
                   and json.loads(row["content"]).get("data", {}).get("checkpoint") == "late-read"
                   for row in state.conversation)
        assert reads == [{}] and runtime.writes == []
        await followup(runtime)

    asyncio.run(run())


@pytest.mark.parametrize("reuse_call_id", [False, True], ids=["distinct-call", "reused-call"])
def test_replacement_cancels_only_a_and_b_can_be_confirmed(runtime, reuse_call_id):
    async def run():
        call_a = "same-call" if reuse_call_id else "proposal-a"
        call_b = "same-call" if reuse_call_id else "proposal-b"
        runtime.model = ScriptedModel([
            [ModelToolCall(call_a, "cloud.change", {"step": 1})],
            [ModelToolCall(call_b, "cloud.change", {"step": 2})],
        ])
        runtime.reopen()
        a = await preview(runtime, 1)
        b = await preview(runtime, 2)
        assert a != b
        state = await cold_state(runtime)
        assert_cancelled(state, a)
        assert_not_cancelled(state, b)
        assert state.pending_effect_plan_id == b
        b_before = deepcopy(plan_row(state, b))
        assert json.loads(b_before["content"])["status"] == "confirmation_required"
        assert not await runtime.session.cancel_effect(owner=OWNER, session_id=SID, plan_id=a)
        rejected = await consume_events(runtime.session.confirm(owner=OWNER, session_id=SID, plan_id=a))
        assert rejected.error_code == "confirmation_invalid"
        assert plan_row(await cold_state(runtime), b) == b_before
        assert runtime.writes == []
        cold_reopen(runtime)
        confirmed = await consume_events(runtime.session.confirm(owner=OWNER, session_id=SID, plan_id=b))
        assert confirmed.status == "success", confirmed
        replay = await consume_events(runtime.session.confirm(owner=OWNER, session_id=SID, plan_id=b))
        assert replay.error_code == "confirmation_invalid"
        assert runtime.writes == [({"step": 2}, "frozen-1")]
        assert len(runtime.prepares) == 2
        state = await cold_state(runtime)
        assert_cancelled(state, a)
        assert_not_cancelled(state, b)

    asyncio.run(run())


def test_wrong_owner_cannot_cancel_or_rewrite_either_owners_observation(runtime):
    async def run():
        a = await preview(runtime)
        b = await preview(runtime, 2, owner="other-owner")
        before = await cold_state(runtime)
        other = await cold_state(runtime, owner="other-owner")
        assert not await runtime.session.cancel_effect(owner="other-owner", session_id=SID, plan_id=a)
        rejected = await consume_events(runtime.session.confirm(owner="other-owner", session_id=SID, plan_id=a))
        assert rejected.error_code == "confirmation_invalid"
        assert (await cold_state(runtime)).conversation == before.conversation
        unchanged = await cold_state(runtime, owner="other-owner")
        assert unchanged.conversation == other.conversation and unchanged.pending_effect_plan_id == b
        assert_not_cancelled(await cold_state(runtime), a)
        assert_not_cancelled(unchanged, b)
        assert runtime.writes == []

    asyncio.run(run())


def test_expired_ticket_does_not_fabricate_successful_cancellation(runtime):
    async def run():
        plan_id = await preview(runtime)
        frozen = await runtime.session.pipeline.get_pending_plan(owner=OWNER, session_id=SID)
        before = await cold_state(runtime)
        runtime.now = frozen.expires_at + 1
        assert not await runtime.session.cancel_effect(owner=OWNER, session_id=SID, plan_id=plan_id)
        cold_reopen(runtime)
        assert (await cold_state(runtime)).conversation == before.conversation
        assert_not_cancelled(await cold_state(runtime), plan_id)
        rejected = await consume_events(runtime.session.confirm(owner=OWNER, session_id=SID, plan_id=plan_id))
        assert rejected.error_code == "confirmation_invalid"
        assert (await cold_state(runtime)).conversation == before.conversation
        assert runtime.writes == []

    asyncio.run(run())


def test_confirmation_permission_failure_is_not_a_cancellation_receipt(runtime):
    async def run():
        plan_id = await preview(runtime)

        class Denied:
            async def authorize(self, tool, arguments, context):
                raise ToolPipelineError("脚本授权拒绝", code="authorization_denied")

        runtime.session.pipeline.authorization = Denied()
        rejected = await consume_events(runtime.session.confirm(owner=OWNER, session_id=SID, plan_id=plan_id))
        assert rejected.status == "failed" and rejected.error_code == "authorization_denied"
        assert runtime.writes == []
        assert not await runtime.session.cancel_effect(owner=OWNER, session_id=SID, plan_id=plan_id)
        cold_reopen(runtime)
        state = await cold_state(runtime)
        assert_not_cancelled(state, plan_id)
        assert any(json.loads(row["content"]).get("status") == "authorization_denied"
                   for row in state.conversation if row.get("role") == "tool")
        replay = await consume_events(runtime.session.confirm(owner=OWNER, session_id=SID, plan_id=plan_id))
        assert replay.error_code == "confirmation_invalid" and runtime.writes == []

    asyncio.run(run())


@pytest.mark.parametrize("parallel_tail", [False, True], ids=["single-call", "parallel-tail"])
def test_immediate_approval_cancel_has_no_waiting_cold_history(runtime, monkeypatch, parallel_tail):
    async def run():
        calls = [ModelToolCall("approval-call", "cloud.change", {"step": 1})]
        if parallel_tail:
            calls.append(ModelToolCall("deferred-call", "cloud.change", {"step": 2}))
        runtime.model = ScriptedModel([calls])
        runtime.reopen()
        published, release = asyncio.Event(), asyncio.Event()
        late_commits = []
        original_append, original_commit = runtime.store.append_for_turn, runtime.store.commit

        async def append(event, lease):
            await original_append(event, lease)
            if event.type == E.EFFECT_APPROVAL_REQUIRED:
                published.set()

        async def commit(lease, *, conversation=None, updates=(), **kwargs):
            if published.is_set() and conversation is not None:
                late_commits.append(deepcopy(conversation))
                # 固定窗口：事件已发布后的 checkpoint 等收到事件的消费者取消完。
                await release.wait()
            return await original_commit(lease, conversation=conversation, updates=updates, **kwargs)

        monkeypatch.setattr(runtime.store, "append_for_turn", append)
        monkeypatch.setattr(runtime.store, "commit", commit)
        stream = runtime.session.run(AgentInput(message="改名一个对象", owner=OWNER, session_id=SID))
        plan_id = None
        try:
            async for event in stream:
                if event.type == E.EFFECT_APPROVAL_REQUIRED:
                    assert plan_id is None
                    plan_id = event.payload["plan"]["plan_id"]
                    at_approval = await cold_state(runtime)
                    assert json.loads(plan_row(at_approval, plan_id)["content"])["status"] == "confirmation_required"
                    assert await runtime.session.cancel_effect(owner=OWNER, session_id=SID, plan_id=plan_id)
                    release.set()
        finally:
            release.set()
            await stream.aclose()
        assert plan_id is not None
        cold_reopen(runtime)
        state = await cold_state(runtime)
        assert_cancelled(state, plan_id)
        assert state.pending_effect_plan_id == "" and runtime.writes == []
        assert len(runtime.prepares) == 1
        assert len(late_commits) == int(parallel_tail)
        if parallel_tail:
            deferred = [row for row in state.conversation
                        if row.get("role") == "tool" and row.get("tool_call_id") == "deferred-call"]
            assert len(deferred) == 1
            rejected = json.loads(deferred[0]["content"])
            assert rejected["status"] == "error" and rejected["code"] == "not_executed_after_approval"
        rejected = await consume_events(runtime.session.confirm(owner=OWNER, session_id=SID, plan_id=plan_id))
        assert rejected.error_code == "confirmation_invalid" and runtime.writes == []

    asyncio.run(run())
