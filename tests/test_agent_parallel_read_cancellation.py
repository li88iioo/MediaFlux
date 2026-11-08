"""并发 READ 取消回归：脚本模型和受控 async handlers，非实机 LLM、不读写实际媒体。"""
from app.agent.kernel.references import InMemoryReferenceStore

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.conversation_codec import pending_content
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import AgentInput, _CancelledReadInline, _exact_value
from tests.support import isolated_test_database

OWNER = "parallel-cancel-test-owner"
SESSION = "parallel-cancel-test-session"
READ_TOOL = "library.parallel_inspect"
OLD_REQUEST = "old-read-turn"
NEW_REQUEST = "new-user-turn"
TIMEOUT = 10


class _ScriptedModel:
    """只发出测试指定的原生 calls / 文本；没有 provider、网络请求或真实 LLM。"""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.requests = []
        self.producers = []

    async def stream(self, request, *, cancellation):
        self.requests.append(request)
        self.producers.append(asyncio.current_task())
        assert self.rounds, "unexpected model round"
        for event in self.rounds.pop(0):
            cancellation.raise_if_cancelled()
            yield event


def _call_round(call_ids):
    return [
        *[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                     tool_call=ModelToolCall(call_id, READ_TOOL, {"label": call_id}))
          for call_id in call_ids],
        ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
    ]


def _payload(call_id, *, large=True):
    return {
        "ok": True, "status": "partial", "summary": "隔离测试只读数据",
        "data": {
            "fact": f"OLD_READ_{call_id}_CANARY",
            "typed": [True, 1, 1.0, None, "中文", {"nested": [False, 0]}],
            "long": "x" * (32_000 if large else 0) + f"{call_id}_EXACT_TAIL",
        },
    }


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _sqlite_store():
    return SQLiteKernelStore(secret_provider=lambda: "parallel-cancel-isolated-test-secret")


@pytest.fixture(params=["memory", "sqlite"])
def stores(request):
    # 即使内存状态后端使用真实 journal，也只访问该临时 DB，退出时恢复原配置。
    with isolated_test_database():
        store = _sqlite_store() if request.param == "sqlite" else InMemorySessionStateStore()
        cold = _sqlite_store() if request.param == "sqlite" else store
        yield store, cold


def _session(store, read, model, *, journal=None):
    catalog = ToolCatalog([KernelToolSpec(
        name=READ_TOOL, domain="library", description="独立只读检查（隔离测试）",
        effect=ToolEffect.READ, input_schema={
            "type": "object", "properties": {"label": {"type": "string"}},
            "required": ["label"], "additionalProperties": False,
        }, read=read, observe=None, metadata={"parallel_read": True},
    )])
    pipeline = ToolPipeline(catalog=catalog, state_store=store,
                            reference_store=store if isinstance(store, SQLiteKernelStore) else None)
    return AgentSession(model=model, catalog=catalog,
                        pipeline=pipeline, state_store=store, journal=journal)


async def _load(store):
    return await store.load(owner=OWNER, session_id=SESSION)


async def _collect(session, request_id=OLD_REQUEST, message="library 独立只读检查"):
    return [event async for event in session.run(AgentInput(
        owner=OWNER, session_id=SESSION, request_id=request_id, message=message,
    ))]


async def _revoke_and_cancel_producer(session, model):
    # 工具阶段为协作停止；撤销真实发布权后原生取消实际 producer，而非改 token 或生产代码。
    assert await session.cancel(owner=OWNER, session_id=SESSION, request_id=OLD_REQUEST)
    producer, = model.producers
    assert not producer.done()
    producer.cancel()


async def _wait(event):
    # 超时只用于发现死锁；所有时序由 Event barrier 决定，不靠 sleep。
    await asyncio.wait_for(event.wait(), TIMEOUT)


async def _cleanup(*tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _rows(state, call_ids):
    batches = [row for row in state.conversation if row.get("tool_calls")]
    assert len(batches) == 1
    assert batches[0]["tool_calls"] == [
        {"call_id": call_id, "name": READ_TOOL, "arguments": {"label": call_id}}
        for call_id in call_ids
    ]
    rows = [row for row in state.conversation if row["role"] == "tool"]
    assert [row["tool_call_id"] for row in rows] == list(call_ids)
    assert all(row["tool_name"] == READ_TOOL for row in rows)
    return rows


def _assert_unknown(row):
    assert row["content"] == pending_content(True)
    assert json.loads(row["content"])["code"] == "result_unknown"


def _assert_cancelled(events, *, settlement):
    assert events[-1].type is AgentEventType.TURN_CANCELLED, [(e.type, e.payload) for e in events]
    assert events[-1].payload["read_settlement"] == settlement
    assert not any(event.type in {AgentEventType.TURN_COMPLETED, AgentEventType.TURN_FAILED,
                                  AgentEventType.TOOL_FAILED} for event in events)


class _MaterializeBarrier:
    """只暂停真实 pipeline 的引用阶段；释放后仍调用原 _materialize_refs。"""

    def __init__(self, pipeline, call_ids):
        self.original = pipeline._materialize_refs
        self.call_ids = set(call_ids)
        self.outcomes = {}
        self.cancelled = set()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, outcome, *, context, **options):
        if context.call_id in self.call_ids:
            self.outcomes[context.call_id] = outcome
            # 引用发布现在有序排队；第一项即可占住发布阶段，其余已返回数据等锁。
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.add(context.call_id)
                raise
        return await self.original(outcome, context=context, **options)


class _SettlementObserver:
    """记录真实批量事务前后状态，不创建 receipt、不替换业务结果或 CAS。"""

    def __init__(self, store, cold, *, paused=False):
        self.original = store.settle_cancelled_reads
        self.cold = cold
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not paused:
            self.release.set()

    async def __call__(self, receipts):
        assert type(receipts) is tuple
        assert all(type(receipt) is _CancelledReadInline for receipt in receipts)
        observation = SimpleNamespace(receipts=receipts, before=None, after=None, settled=None)
        self.calls.append(observation)
        self.entered.set()
        await self.release.wait()
        observation.before = await _load(self.cold)
        observation.settled = await self.original(receipts)
        observation.after = await _load(self.cold)
        return observation.settled


def _assert_settled_only(observer, expected_payloads):
    assert len(observer.calls) == 1, "returned reads must settle in one atomic batch"
    call = observer.calls[0]
    assert call.settled is True
    assert [call.before.conversation[r.tool_index]["tool_call_id"] for r in call.receipts] == list(expected_payloads)
    expected = call.before.clone()
    for receipt, payload in zip(call.receipts, expected_payloads.values(), strict=True):
        assert receipt.expected == _exact_value(call.before.conversation)
        _assert_unknown(call.before.conversation[receipt.tool_index])
        assert receipt._content == _json(payload), "full typed JSON and its tail must survive"
        expected.conversation[receipt.tool_index]["content"] = _json(payload)
    assert call.after == expected, "settlement may only replace returned READ slots, never authority or metadata"
    return call


@pytest.mark.parametrize("pending_read", [False, True], ids=["both-returned", "third-still-unknown"])
def test_producer_interrupt_during_materialization_settles_returned_reads_atomically(stores, pending_read):
    async def run():
        store, cold = stores
        returned_ids = ("first", "second")
        call_ids = (*returned_ids, "waiting") if pending_read else returned_ids
        payloads = {call_id: _payload(call_id) for call_id in returned_ids}
        entered, return_gate, pending_gate = asyncio.Event(), asyncio.Event(), asyncio.Event()
        invoked, returned, handler_cancelled = [], [], []
        all_returned = asyncio.Event()

        async def read(arguments, context):
            call_id = arguments["label"]
            assert call_id == context.call_id
            invoked.append(call_id)
            if len(invoked) == len(call_ids):
                entered.set()
            try:
                await return_gate.wait()
                if call_id == "waiting":
                    await pending_gate.wait()
                    raise AssertionError("the pending READ must never return")
                returned.append(call_id)
                if len(returned) == len(returned_ids):
                    all_returned.set()
                return deepcopy(payloads[call_id])
            except asyncio.CancelledError:
                handler_cancelled.append(call_id)
                raise

        model = _ScriptedModel([_call_round(call_ids)])
        session = _session(store, read, model)
        materialize = _MaterializeBarrier(session.pipeline, returned_ids)
        observer = _SettlementObserver(store, cold)
        with patch.object(session.pipeline, "_materialize_refs", materialize), patch.object(
            store, "settle_cancelled_reads", observer,
        ):
            task = asyncio.create_task(_collect(session))
            try:
                await _wait(entered)
                started = await _load(cold)
                for row in _rows(started, call_ids):
                    _assert_unknown(row)
                return_gate.set()
                await _wait(materialize.entered)
                await _wait(all_returned)
                assert returned == list(returned_ids)
                for call_id, outcome in materialize.outcomes.items():
                    assert outcome.full_model_content
                    assert json.loads(outcome.full_model_content) == payloads[call_id]
                assert await _load(cold) == started
                await _revoke_and_cancel_producer(session, model)
                events = await asyncio.wait_for(task, TIMEOUT)
            finally:
                observer.release.set()
                await _cleanup(task)

        _assert_cancelled(events, settlement="settled")
        assert materialize.cancelled == {returned_ids[0]}, "第一项的发布被中断；另一已返回结果在等发布锁，同样必须保留"
        assert handler_cancelled == (["waiting"] if pending_read else [])
        assert invoked == list(call_ids), "no cancelled READ may be retried"
        assert [e.payload["call_id"] for e in events if e.type is AgentEventType.TOOL_STARTED] == list(call_ids)
        assert not any(e.type is AgentEventType.TOOL_COMPLETED for e in events)
        settled = _assert_settled_only(observer, payloads)
        final = await _load(cold)
        assert final == settled.after
        rows = _rows(final, call_ids)
        for row, payload in zip(rows[:2], payloads.values(), strict=True):
            assert row["content"] == _json(payload)
        if pending_read:
            _assert_unknown(rows[2])
        assert final.generation == started.generation + 1
        assert final.metadata["active_publication"] == {
            "generation": started.generation, "turn_id": settled.receipts[0].lease.turn_id,
            "request_id": OLD_REQUEST, "status": "cancelled",
        }
        assert not await store.is_current(settled.receipts[0].lease)
        assert len(model.requests) == 1
        if isinstance(store, SQLiteKernelStore):
            from app import database as db
            with db.get_conn() as conn:
                assert conn.execute("SELECT COUNT(*) FROM agent_kernel_refs").fetchone()[0] == 0
        else:
            assert isinstance(session.pipeline.reference_store, InMemoryReferenceStore)
            assert not session.pipeline.reference_store._records

    asyncio.run(run())


def test_cancel_after_first_result_cas_keeps_unconsumed_second_result(stores):
    async def run():
        store, cold = stores
        call_ids = ("first", "second")
        payloads = {"first": _payload("first", large=False), "second": _payload("second")}
        entered, return_gate = asyncio.Event(), asyncio.Event()
        journal_entered, journal_release, journal_cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        invoked, returned, started_checkpoints = [], [], []

        async def read(arguments, context):
            invoked.append(context.call_id)
            started_checkpoints.append(await _load(cold))
            if len(invoked) == len(call_ids):
                entered.set()
            await return_gate.wait()
            returned.append(context.call_id)
            return deepcopy(payloads[arguments["label"]])

        # 内存状态没有 journal 方法；事件仍委托真实的隔离 SQLite journal，不伪造完成事件。
        durable_journal = store if isinstance(store, SQLiteKernelStore) else _sqlite_store()
        async def append_for_turn(event, lease):
            await durable_journal.append(event, owner=lease.owner)
        journal = durable_journal if isinstance(store, SQLiteKernelStore) else SimpleNamespace(
            append=durable_journal.append, append_for_turn=append_for_turn)
        method = "append_for_turn"
        original_append = getattr(journal, method)

        async def blocked_append(event, *args, **kwargs):
            if event.type is AgentEventType.TOOL_COMPLETED and event.payload["call_id"] == "first":
                journal_entered.set()
                try:
                    await journal_release.wait()
                except asyncio.CancelledError:
                    journal_cancelled.set()
                    raise
            return await original_append(event, *args, **kwargs)

        model = _ScriptedModel([_call_round(call_ids)])
        session = _session(store, read, model, journal=journal)
        observer = _SettlementObserver(store, cold)
        with patch.object(journal, method, blocked_append), patch.object(store, "settle_cancelled_reads", observer):
            task = asyncio.create_task(_collect(session))
            try:
                await _wait(entered)
                return_gate.set()
                await _wait(journal_entered)
                assert returned == list(call_ids), "both real pipeline executions must have returned before cancellation"
                checkpoint = await _load(cold)
                rows = _rows(checkpoint, call_ids)
                assert rows[0]["content"] == _json(payloads["first"])
                _assert_unknown(rows[1])
                assert all(_exact_value(state.conversation) != _exact_value(checkpoint.conversation)
                           for state in started_checkpoints)
                await _revoke_and_cancel_producer(session, model)
                events = await asyncio.wait_for(task, TIMEOUT)
            finally:
                observer.release.set()
                await _cleanup(task)

        _assert_cancelled(events, settlement="settled")
        assert journal_cancelled.is_set(), "cancel must interrupt journal I/O, not wait for a synthetic completion"
        assert invoked == list(call_ids)
        settled = _assert_settled_only(observer, {"second": payloads["second"]})
        assert settled.receipts[0].expected == _exact_value(checkpoint.conversation), "rebind to the executor's last successful CAS"
        final = await _load(cold)
        assert final == settled.after
        assert final.conversation[:-1] == checkpoint.conversation[:-1], "first committed result must remain byte-for-byte intact"
        assert _rows(final, call_ids)[1]["content"] == _json(payloads["second"])
        assert final.generation == checkpoint.generation + 1
        assert not await store.is_current(settled.receipts[0].lease)
        assert not any(e.type is AgentEventType.TOOL_COMPLETED for e in events)
        persisted = await durable_journal.list_events(owner=OWNER, session_id=SESSION)
        assert persisted[-1]["type"] == AgentEventType.TURN_CANCELLED
        assert not any(e["type"] == AgentEventType.TOOL_COMPLETED for e in persisted)
        assert len(model.requests) == 1

    asyncio.run(run())


def test_new_user_supersedes_returned_parallel_reads_without_polluting_new_turn(stores):
    async def run():
        store, cold = stores
        call_ids = ("first", "second")
        payloads = {call_id: _payload(call_id) for call_id in call_ids}
        invoked, returned = [], []
        both_started, all_returned = asyncio.Event(), asyncio.Event()

        async def read(arguments, context):
            invoked.append(context.call_id)
            if len(invoked) == len(call_ids):
                both_started.set()
            await both_started.wait()
            returned.append(context.call_id)
            if len(returned) == len(call_ids):
                all_returned.set()
            return deepcopy(payloads[arguments["label"]])

        new_message, new_answer = "新用户问题：只回答当前回合", "这是新回合的回答"
        model = _ScriptedModel([
            _call_round(call_ids),
            [ModelEvent(ModelEventType.TEXT_DELTA, text=new_answer),
             ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
        ])
        session = _session(store, read, model)
        materialize = _MaterializeBarrier(session.pipeline, call_ids)
        # 让新用户回合先完整落盘，再尝试旧结果的真实批量 CAS，证明不能覆盖新历史。
        observer = _SettlementObserver(store, cold, paused=True)
        with patch.object(session.pipeline, "_materialize_refs", materialize), patch.object(
            store, "settle_cancelled_reads", observer,
        ):
            old_task = asyncio.create_task(_collect(session))
            tasks = [old_task]
            try:
                await _wait(materialize.entered)
                await _wait(all_returned)
                started = await _load(cold)
                for row in _rows(started, call_ids):
                    _assert_unknown(row)
                new_task = asyncio.create_task(_collect(session, NEW_REQUEST, new_message))
                tasks.append(new_task)
                new_events = await asyncio.wait_for(new_task, TIMEOUT)
                assert new_events[-1].type is AgentEventType.TURN_COMPLETED, [(e.type, e.payload) for e in new_events]
                assert new_events[-1].payload["status"] == "success"
                assert all(e.request_id == NEW_REQUEST for e in new_events)
                new_state = await _load(cold)
                assert new_state.generation == started.generation + 1
                assert [row["content"] for row in new_state.conversation if row["role"] == "user"][-1] == new_message
                assert new_state.conversation[-1]["role"] == "assistant"
                assert new_state.conversation[-1]["content"] == new_answer
                assert len(model.requests) == 2
                new_history = [message.to_dict() for message in model.requests[1].messages]
                assert new_history[-1]["role"] == "user"
                assert new_history[-1]["content"] == new_message
                for row in [message for message in new_history if message["role"] == "tool"]:
                    _assert_unknown(row)
                assert len([row for row in new_history if row["role"] == "tool"]) == len(call_ids)
                for payload in payloads.values():
                    assert payload["data"]["fact"] not in _json(new_history)
                    assert payload["data"]["fact"] not in _json(new_state.to_payload())
                materialize.release.set()
                await _wait(observer.entered)
                observer.release.set()
                old_events = await asyncio.wait_for(old_task, TIMEOUT)
            finally:
                observer.release.set()
                materialize.release.set()
                await _cleanup(*tasks)

        _assert_cancelled(old_events, settlement="rejected")
        assert all(e.request_id == OLD_REQUEST for e in old_events)
        assert not any(e.type is AgentEventType.TOOL_COMPLETED for e in old_events)
        assert not materialize.cancelled, "superseded pipelines must resume through the real stale-authority check"
        assert invoked == list(call_ids), "superseding must never replay old handlers"
        assert len(observer.calls) == 1
        call = observer.calls[0]
        assert len(call.receipts) == len(call_ids), "both returned receipts must be rejected, not silently forgotten"
        for receipt, payload in zip(call.receipts, payloads.values(), strict=True):
            assert receipt.lease.request_id == OLD_REQUEST
            assert receipt.lease.generation == started.generation
            assert receipt.expected == _exact_value(started.conversation), "never rebind an old receipt to a new user's checkpoint"
            assert receipt._content == _json(payload)
        assert call.settled is False
        assert call.before == call.after == new_state
        assert await _load(cold) == new_state, "old cancellation cleanup cannot mutate the new turn"
        for row in _rows(new_state, call_ids):
            _assert_unknown(row)
        assert not await store.is_current(call.receipts[0].lease)

    asyncio.run(run())


def test_cooperative_session_cancel_then_real_io_return_keeps_both_facts(stores):
    """仅调用Session.cancel，不对producer调用Task.cancel；IO正常返回后走真实代次检查。"""
    async def run():
        store, cold = stores
        call_ids = ("first", "second")
        payloads = {call_id: _payload(call_id) for call_id in call_ids}
        invoked, returned = [], []
        both_started, all_returned = asyncio.Event(), asyncio.Event()

        async def read(arguments, context):
            invoked.append(context.call_id)
            if len(invoked) == len(call_ids):
                both_started.set()
            await both_started.wait()
            returned.append(context.call_id)
            if len(returned) == len(call_ids):
                all_returned.set()
            return deepcopy(payloads[arguments["label"]])

        model = _ScriptedModel([_call_round(call_ids)])
        session = _session(store, read, model)
        barrier = _MaterializeBarrier(session.pipeline, call_ids)
        observer = _SettlementObserver(store, cold)
        with patch.object(session.pipeline, "_materialize_refs", barrier), patch.object(
            store, "settle_cancelled_reads", observer,
        ):
            task = asyncio.create_task(_collect(session))
            try:
                await _wait(barrier.entered)
                await _wait(all_returned)
                assert await session.cancel(owner=OWNER, session_id=SESSION, request_id=OLD_REQUEST)
                barrier.release.set()
                events = await asyncio.wait_for(task, TIMEOUT)
            finally:
                barrier.release.set()
                observer.release.set()
                await _cleanup(task)
        _assert_cancelled(events, settlement="settled")
        assert not barrier.cancelled
        assert invoked == list(call_ids)
        settled = _assert_settled_only(observer, payloads)
        final = await _load(cold)
        assert final == settled.after
        assert [json.loads(row["content"]) for row in _rows(final, call_ids)] == list(payloads.values())
        assert len(model.requests) == 1
    asyncio.run(run())
