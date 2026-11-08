import asyncio
import threading

import pytest

from app.agent.kernel.adapters import consume_events
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.state import TurnCoordinator
from app.agent.kernel.state_types import AgentInput
from app.agent.kernel.turn import AgentTurn
from tests.support import isolated_test_database
from tests.test_pending_explanation_transports import NativeRuntime


@pytest.mark.parametrize("action", ["stop", "read", "cancel_plan", "expire"])
def test_explanation_does_not_own_or_refresh_plan(action):
    async def run():
        runtime = NativeRuntime()
        owner, sid = "owner", "explain-race"
        preview = await consume_events(
            runtime.session.run(
                AgentInput(message="改名一个对象", owner=owner, session_id=sid)
            )
        )
        assert preview.approval is not None
        plan_id = preview.approval.plan_id
        frozen = await runtime.session.pipeline.get_pending_plan(
            owner=owner, session_id=sid
        )
        entered, release = asyncio.Event(), asyncio.Event()
        runtime.gate = entered, release
        task = asyncio.create_task(
            consume_events(
                runtime.session.explain(
                    owner=owner,
                    session_id=sid,
                    plan_id=plan_id,
                    message="解释影响",
                    request_id="explain",
                )
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        if action == "stop":
            assert await runtime.session.cancel(
                owner=owner, session_id=sid, request_id="explain"
            )
        elif action == "read":
            read = await consume_events(
                runtime.session.run(
                    AgentInput(message="查看别的内容", owner=owner, session_id=sid)
                )
            )
            assert read.status == "success", read
            assert read.tool_calls == () and read.approval is None
        elif action == "cancel_plan":
            assert await runtime.session.cancel_effect(
                owner=owner, session_id=sid, plan_id=plan_id
            )
        elif action == "expire":
            assert frozen is not None
            runtime.now = frozen.expires_at + 1
        release.set()
        await asyncio.wait_for(task, 2)
        state = await SQLiteKernelStore().load(owner=owner, session_id=sid)
        assert runtime.writes == [] and len(runtime.prepares) == 1
        if action in ("stop", "read"):
            assert state.pending_effect_plan_id == plan_id
            assert (
                await runtime.session.pipeline.get_pending_plan(
                    owner=owner, session_id=sid
                )
                == frozen
            )
        else:
            assert (
                await runtime.session.pipeline.get_pending_plan(
                    owner=owner, session_id=sid
                )
                is None
            )
        result = await consume_events(
            runtime.session.confirm(owner=owner, session_id=sid, plan_id=plan_id)
        )
        replay = await consume_events(
            runtime.session.confirm(owner=owner, session_id=sid, plan_id=plan_id)
        )
        assert replay.status == "failed" and replay.error_code == "confirmation_invalid"
        assert runtime.writes == (
            [({"step": 1}, "frozen-1")] if action in ("stop", "read") else []
        )
        assert result.status == ("success" if action in ("stop", "read") else "failed")

    with isolated_test_database("explain-race.db"):
        asyncio.run(run())


@pytest.mark.parametrize("cleanup", ["checkpoint", "finish", "ownership_check"])
def test_superseded_explanation_cleanup_cannot_cancel_current_read(monkeypatch, cleanup):
    """旧收尾与新回答提交重叠；暂停真实提交worker，不替换CAS或放宽原断言。"""
    new_commit_ready, old_ready = asyncio.Event(), asyncio.Event()
    new_committed, old_finished = asyncio.Event(), asyncio.Event()
    checked_while_current, new_admitted = asyncio.Event(), asyncio.Event()
    release_worker = threading.Event()
    new_turn = None
    admitting = False
    checked = False
    worker_paused = False
    loop = None
    original_admit = AgentTurn.admit
    original_commit = SQLiteKernelStore.commit
    original_commit_sync = SQLiteKernelStore._commit_sync
    original_finish = TurnCoordinator.finish

    async def admit(turn):
        nonlocal new_turn, admitting, loop
        if turn.agent_input.message == "查看别的内容":
            new_turn, admitting = turn, True
            loop = asyncio.get_running_loop()
            if cleanup == "ownership_check":
                # 模拟断连的本地取消；此时持久发布权仍属于explain。
                assert await turn.session.coordinator.cancel(
                    owner=turn.agent_input.owner, session_id=turn.agent_input.session_id,
                    request_id="explain", reason="disconnected")
                await asyncio.wait_for(checked_while_current.wait(), 5)
        await original_admit(turn)
        if turn is new_turn:
            admitting = False
            new_admitted.set()

    async def commit(store, lease, **kwargs):
        nonlocal checked
        if new_turn is not None and lease.request_id == "explain":
            if cleanup == "ownership_check" and not checked_while_current.is_set():
                assert await store.is_current(lease)
                checked_while_current.set()
                await asyncio.wait_for(new_admitted.wait(), 5)
            await asyncio.wait_for(new_commit_ready.wait(), 5)
        if (new_turn is None or admitting or lease != new_turn.lease or checked):
            return await original_commit(store, lease, **kwargs)
        checked = True
        new_commit_ready.set()
        await asyncio.wait_for(old_ready.wait(), 5)
        assert await store.is_current(lease)
        assert not await new_turn.session.coordinator.has_protected_turn(
            owner=lease.owner, session_id=lease.session_id)
        try:
            return await original_commit(store, lease, **kwargs)
        finally:
            release_worker.set()
            new_committed.set()
            await asyncio.wait_for(old_finished.wait(), 5)

    def commit_sync(store, lease, conversation, updates, *args):
        nonlocal worker_paused
        if (new_turn is not None and lease.request_id == "explain" and not worker_paused
                and (conversation is not None) == (cleanup != "finish")):
            worker_paused = True
            assert not store._is_current_sync(lease)
            assert loop is not None
            loop.call_soon_threadsafe(old_ready.set)
            assert release_worker.wait(5), "new commit did not release old worker"
        return original_commit_sync(store, lease, conversation, updates, *args)

    async def finish(coordinator, lease, token):
        if new_turn is not None and lease.request_id == "explain":
            # 新提交不必等待旧协程完全退出；旧finish也不得撤掉新回合协调状态。
            old_ready.set()
            await asyncio.wait_for(new_committed.wait(), 5)
            await original_finish(coordinator, lease, token)
            old_finished.set()
        else:
            await original_finish(coordinator, lease, token)

    monkeypatch.setattr(AgentTurn, "admit", admit)
    monkeypatch.setattr(SQLiteKernelStore, "commit", commit)
    monkeypatch.setattr(SQLiteKernelStore, "_commit_sync", commit_sync)
    monkeypatch.setattr(TurnCoordinator, "finish", finish)
    try:
        # 原测试继续检查冻结计划不变、无提前写、冷SQLite读取与确认仅一次。
        test_explanation_does_not_own_or_refresh_plan("read")
        assert checked
    finally:
        release_worker.set()


def test_superseded_read_settlement_cannot_cancel_current_commit(monkeypatch):
    from app.agent.kernel.conversation_codec import pending_content
    from app.agent.kernel.state_types import StalePublicationError, _CancelledReadInline
    from tests.test_cancelled_read_inline import READ_TOOL, sqlite_store

    async def run():
        store = sqlite_store()
        old, _ = await store.begin_turn(owner="o", session_id="s", request_id="old")
        checkpoint = await store.commit(old, conversation=[
            {"role": "assistant", "content": "", "tool_calls": [
                {"call_id": "read", "name": READ_TOOL.name, "arguments": {}}]},
            {"role": "tool", "tool_call_id": "read", "tool_name": READ_TOOL.name,
             "content": pending_content(True)},
        ])
        receipt = _CancelledReadInline.capture(old, checkpoint.conversation, 0, tool=READ_TOOL)
        receipt.put_once('{"ok":true,"data":"returned-before-stop"}')
        assert await store.cancel_publication(owner="o", session_id="s", request_id="old")
        current, _ = await store.begin_turn(owner="o", session_id="s", request_id="new")
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        settle = store._settle_cancelled_reads_sync

        def blocked_settle(receipts):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            return settle(receipts)

        monkeypatch.setattr(store, "_settle_cancelled_reads_sync", blocked_settle)
        task = asyncio.create_task(store.settle_cancelled_reads((receipt,)))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            saved = await store.commit(current, conversation=[{"role": "user", "content": "new question"}])
        finally:
            release.set()
            assert not await asyncio.wait_for(task, 5)
        assert await sqlite_store().load(owner="o", session_id="s") == saved
        with pytest.raises(StalePublicationError):
            await store.commit(old, conversation=[])

    with isolated_test_database():
        asyncio.run(run())


def test_confirmed_parent_can_commit_but_inherited_child_cannot():
    from dataclasses import replace
    from app.agent.kernel.capabilities import ToolCatalog
    from app.agent.kernel.state_types import StalePublicationError, StateUpdate

    async def run():
        runtime = NativeRuntime()
        native = runtime.catalog.get("cloud.change")

        async def execute(arguments, snapshot, context):
            await runtime.store.commit(context.lease, updates=(StateUpdate("summary", "parent-before"),))
            child = asyncio.create_task(runtime.store.commit(
                context.lease, updates=(StateUpdate("summary", "unauthorized-child"),)))
            with pytest.raises(StalePublicationError):
                await asyncio.wait_for(child, 2)
            assert (await runtime.store.load(owner=context.owner, session_id=context.session_id)).summary == "parent-before"
            await runtime.store.commit(context.lease, updates=(StateUpdate("summary", "parent-after"),))
            assert native.execute_confirmed is not None
            return native.execute_confirmed(arguments, snapshot, context)

        runtime.catalog = ToolCatalog([replace(native, execute_confirmed=execute)])
        runtime.reopen()
        preview = await consume_events(runtime.session.run(AgentInput(owner="o", session_id="s", message="改名")))
        assert preview.approval is not None
        result = await consume_events(runtime.session.confirm(owner="o", session_id="s", plan_id=preview.approval.plan_id))
        assert result.status == "success", result
        assert runtime.writes == [({"step": 1}, "frozen-1")]
        assert (await SQLiteKernelStore().load(owner="o", session_id="s")).summary == "parent-after"

    with isolated_test_database():
        asyncio.run(run())


def test_database_first_commit_rolls_back_when_guard_first_admission_wins(monkeypatch):
    from app.agent.kernel.session_guard import session_scope_guard
    from app.agent.kernel.state_types import StalePublicationError, StateUpdate
    from tests.test_cancelled_read_inline import sqlite_store

    async def run():
        store, next_store = sqlite_store(), sqlite_store()
        lease, _ = await store.begin_turn(owner="o", session_id="s", request_id="old")
        database_locked, guard_locked = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        load = store._load_row

        def blocked_load(conn, owner, session_id):
            if conn.in_transaction:
                loop.call_soon_threadsafe(database_locked.set)
                assert guard_locked.wait(5)
            return load(conn, owner, session_id)

        monkeypatch.setattr(store, "_load_row", blocked_load)

        async def old_commit():
            with pytest.raises(StalePublicationError):
                await store.commit(lease, updates=(StateUpdate("summary", "must-rollback"),))

        async def admit():
            await asyncio.wait_for(database_locked.wait(), 5)
            with session_scope_guard("o", "s"):
                guard_locked.set()
                current, state = await next_store.begin_turn(owner="o", session_id="s", request_id="new")
            assert await next_store.is_current(current)
            assert state.summary != "must-rollback"

        await asyncio.wait_for(asyncio.gather(old_commit(), admit()), 5)
        assert not await next_store.is_current(lease)

    with isolated_test_database():
        asyncio.run(run())


def test_store_precheck_cannot_authorize_commit_after_new_admission(monkeypatch):
    from app.agent.kernel.state_types import StalePublicationError, StateUpdate
    from tests.test_cancelled_read_inline import sqlite_store

    async def run():
        store, current_store = sqlite_store(), sqlite_store()
        old, _ = await store.begin_turn(owner="o", session_id="s", request_id="old")
        checked, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        load = store._load_row
        paused = False

        def checked_load(conn, owner, session_id):
            nonlocal paused
            state = load(conn, owner, session_id)
            if not conn.in_transaction and not paused:
                paused = True
                assert state.generation == old.generation
                loop.call_soon_threadsafe(checked.set)
                assert release.wait(5)
            return state

        monkeypatch.setattr(store, "_load_row", checked_load)
        task = asyncio.create_task(store.commit(old, updates=(StateUpdate("summary", "stale-write"),)))
        try:
            await asyncio.wait_for(checked.wait(), 5)
            current, _ = await current_store.begin_turn(owner="o", session_id="s", request_id="new")
            saved = await current_store.commit(current, updates=(StateUpdate("summary", "new-answer"),))
        finally:
            release.set()
            with pytest.raises(StalePublicationError):
                await asyncio.wait_for(task, 5)
        assert await sqlite_store().load(owner="o", session_id="s") == saved

    with isolated_test_database():
        asyncio.run(run())
