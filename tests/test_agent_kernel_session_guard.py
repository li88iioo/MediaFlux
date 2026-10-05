"""A4：跨 worker 会话写保护、反向竞争和旧 publication 的隔离业务回归。"""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import patch

import pytest

from app import database as db
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.lifecycle import AgentSessionLifecycle
from app.agent.kernel.state import PublicationLease, SessionBusyError, StalePublicationError, StateUpdate
from app.agent.kernel.transports import QueryEnvelope
from tests import test_agent_full_chain_audit as chain
from tests.test_agent_full_chain_audit import persistent_runtime
from tests.test_agent_ux_backend import OWNER, SESSION, _events, _publish_candidates, _selection


@pytest.fixture
def isolated_chain():
    yield from chain.isolated_chain.__wrapped__()


async def prepare(session, pipeline, store, *, owner=OWNER, session_id=SESSION):
    view, _ = await _publish_candidates(pipeline, store, owner=owner, session_id=session_id)
    events = await _events(session.run(QueryEnvelope(owner=owner, session_id=session_id,
        message="确认一个资源", selection=_selection(view, [1])).to_agent_input()))
    approval = next(e for e in events if e.type is AgentEventType.EFFECT_APPROVAL_REQUIRED)
    state = await store.load(owner=owner, session_id=session_id)
    return approval.payload["plan"]["plan_id"], PublicationLease(
        owner, session_id, state.generation, events[0].turn_id, "old-read")


def lifecycle_for(session, pipeline, store):
    return AgentSessionLifecycle(session=session, store=store, effect_store=pipeline.effect_store,
                                 clear_provider_state=lambda **kw: {})


async def confirmation(session, plan, *, owner=OWNER, session_id=SESSION):
    return await _events(session.confirm(owner=owner, session_id=session_id, plan_id=plan))


async def mutate(operation, lifecycle, store):
    if operation in {"reset", "delete"}:
        return await getattr(lifecycle, operation)(owner=OWNER, session_id=SESSION)
    if operation in {"raw_reset", "raw_delete"}:
        return await getattr(store, operation.removeprefix("raw_") + "_session")(owner=OWNER, session_id=SESSION)
    return await store.begin_turn(owner=OWNER, session_id=SESSION, request_id="new-query")


def assert_completed(events):
    results = [e.payload["result"] for e in events if e.type is AgentEventType.EFFECT_COMPLETED]
    assert len(results) == 1, [(e.type.value, e.payload) for e in events]
    return results[0]


@pytest.mark.parametrize("operation", ["reset", "delete", "raw_reset", "raw_delete", "begin_turn", "query"])
@pytest.mark.parametrize("phase", ["remote", "receipt"])
def test_confirmed_write_blocks_cross_worker_mutation_until_receipt_persisted(isolated_chain, operation, phase):
    async def exercise():
        writer, pipeline, store = persistent_runtime()
        plan, old = await prepare(writer, pipeline, store)
        other, other_pipeline, other_store = persistent_runtime()
        lifecycle = lifecycle_for(other, other_pipeline, other_store)
        remote_entered, remote_release = threading.Event(), threading.Event()
        receipt_entered, receipt_release = asyncio.Event(), asyncio.Event()
        real_commit = store.commit

        def remote_write(*args, **kwargs):
            remote_entered.set()
            assert remote_release.wait(5)
            return {"ok": True, "task_id": "fake-gy"}

        async def commit(lease, *, conversation=None, updates=()):
            if conversation and any("下载请求 #" in item.get("public_content", "") for item in conversation):
                receipt_entered.set()
                await receipt_release.wait()
            return await real_commit(lease, conversation=conversation, updates=updates)

        if phase == "remote":
            receipt_release.set()
        else:
            remote_release.set()
        isolated_chain[0].side_effect = remote_write
        with patch.object(store, "commit", side_effect=commit):
            task = asyncio.create_task(confirmation(writer, plan))
            try:
                if phase == "remote":
                    assert await asyncio.to_thread(remote_entered.wait, 5)
                else:
                    await asyncio.wait_for(receipt_entered.wait(), 5)
                if operation == "query":
                    events = await _events(other.run(QueryEnvelope(
                        owner=OWNER, session_id=SESSION, message="另一 worker 的新问题",
                    ).to_agent_input()))
                    assert any(e.payload.get("code") == "effect_in_progress" for e in events)
                    assert other.model.requests == []
                else:
                    with pytest.raises(SessionBusyError):
                        await mutate(operation, lifecycle, other_store)
                with pytest.raises(StalePublicationError):
                    await other_store.commit(old, conversation=[{"role": "assistant", "content": "迟到覆盖"}])
            finally:
                remote_release.set()
                receipt_release.set()
                events = await task
        result = assert_completed(events)
        state = await other_store.load(owner=OWNER, session_id=SESSION)
        request_id = result["data"]["items"][0]["request_id"]
        assert any(f"下载请求 #{request_id}" in item.get("public_content", "") for item in state.conversation)
        assert db.get_download_request(request_id)["status"] == "submitted"
        assert isolated_chain[0].call_count == 1
    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["reset", "delete"])
def test_lifecycle_wins_before_confirmation_never_enters_external_write(isolated_chain, operation):
    async def exercise():
        writer, pipeline, store = persistent_runtime()
        plan, _ = await prepare(writer, pipeline, store)
        other, other_pipeline, other_store = persistent_runtime()
        lifecycle = lifecycle_for(other, other_pipeline, other_store)
        entered, release = asyncio.Event(), asyncio.Event()
        real_invalidate = lifecycle._invalidate

        async def delayed_invalidation(**kwargs):
            entered.set()
            await release.wait()
            await real_invalidate(**kwargs)

        with patch.object(lifecycle, "_invalidate", side_effect=delayed_invalidation):
            task = asyncio.create_task(mutate(operation, lifecycle, other_store))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                events = await confirmation(writer, plan)
                assert any(e.payload.get("code") == "effect_in_progress" for e in events), events
                assert isolated_chain[0].call_count == 0
            finally:
                release.set()
                await task
        replay = await confirmation(writer, plan)
        assert not any(e.type is AgentEventType.EFFECT_COMPLETED for e in replay)
        assert isolated_chain[0].call_count == 0
        with db.get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0] == 0
    asyncio.run(exercise())


def test_old_same_generation_publication_cannot_overwrite_confirmed_receipt_after_unlock(isolated_chain):
    async def exercise():
        writer, pipeline, store = persistent_runtime()
        plan, old = await prepare(writer, pipeline, store)
        assert await store.is_current(old)
        assert_completed(await confirmation(writer, plan))
        _, _, restarted = persistent_runtime()
        state = await restarted.load(owner=OWNER, session_id=SESSION)
        assert state.generation == old.generation, "确认不能破坏冻结票据的 generation 契约"
        assert not await restarted.is_current(old)
        with pytest.raises(StalePublicationError):
            await restarted.commit(old, conversation=[{"role": "assistant", "content": "迟到覆盖"}],
                                   updates=(StateUpdate("pending_effect_plan_id", "old-plan"),))
        assert (await restarted.load(owner=OWNER, session_id=SESSION)).conversation == state.conversation
    asyncio.run(exercise())


@pytest.mark.parametrize("other_owner,other_session", [(OWNER, "session_other_12345"), ("owner-other", SESSION)])
def test_independent_owner_session_can_confirm_while_first_scope_is_writing(isolated_chain, other_owner, other_session):
    async def exercise():
        writer, pipeline, store = persistent_runtime()
        plan, _ = await prepare(writer, pipeline, store)
        other, other_pipeline, other_store = persistent_runtime()
        plan2, _ = await prepare(other, other_pipeline, other_store, owner=other_owner, session_id=other_session)
        entered, release = threading.Event(), threading.Event()

        def remote_write(row, **kwargs):
            if int(row["id"]) == 1:
                entered.set()
                assert release.wait(5)
            return {"ok": True, "task_id": "fake-gy"}

        isolated_chain[0].side_effect = remote_write
        task = asyncio.create_task(confirmation(writer, plan))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            # 第二个候选换成另一磁力，避免下载层正确的全局资源幂等影响 scope 验证。
            with patch("app.agent.indexer_actions.get_indexer_service") as get_service:
                # 恢复外层隔离 fixture 的 service，不改候选身份和冻结摘要。
                from types import SimpleNamespace
                from app.indexers.models import ResolvedDownload
                from tests.test_agent_ux_backend import _resources
                items = {item["result_id"]: SimpleNamespace(**item) for item in _resources().data["items"]}
                async def resolve(_, *, scope="manual"):
                    return ResolvedDownload("magnet", "magnet:?xt=urn:btih:" + "c" * 40)
                get_service.return_value = SimpleNamespace(result_store=SimpleNamespace(get=items.__getitem__), get_result=lambda key, **_kw: items[key],
                                                           enabled_site_ids=("demo",), resolve=resolve)
                assert_completed(await confirmation(other, plan2, owner=other_owner, session_id=other_session))
            await lifecycle_for(other, other_pipeline, other_store).reset(owner=other_owner, session_id=other_session)
        finally:
            release.set()
            events = await task
        assert_completed(events)
        assert isolated_chain[0].call_count == 2
    asyncio.run(exercise())


def _process_confirm(db_path, plan, pipe):
    """spawn worker 使用父进程的测试 DB；只替换远端，不初始化/恢复运行中的数据库。"""
    from types import SimpleNamespace
    from app.agent import indexer_actions
    from app.indexers.models import ResolvedDownload
    from app.modules import download_dispatcher as dispatcher
    from tests.test_agent_ux_backend import SECRET, _resources

    db.configure_database(db_path, test_mode=True)
    items = {item["result_id"]: SimpleNamespace(**item) for item in _resources().data["items"]}

    async def resolve(_, *, scope="manual"):
        return ResolvedDownload("magnet", "magnet:?xt=urn:btih:" + "a" * 40)

    def remote_write(row, **kwargs):
        pipe.send(("writing", row["id"]))
        assert pipe.recv() == "finish"
        return {"ok": True, "task_id": "fake-gy"}

    service = SimpleNamespace(result_store=SimpleNamespace(get=items.__getitem__), get_result=lambda key, **_kw: items[key],
                              enabled_site_ids=("demo",), resolve=resolve)
    try:
        with (
            patch("socket.socket.connect", side_effect=AssertionError("禁止外联")),
            patch("app.modules.web_secret.get_web_secret", return_value=SECRET),
            patch.object(indexer_actions.config, "get_bool", return_value=True),
            patch.object(indexer_actions, "get_indexer_service", return_value=service),
            patch.object(indexer_actions, "download_target_readiness", return_value={"qb": True, "guangya": True}),
            patch.object(indexer_actions, "run_indexer_awaitable_sync", asyncio.run),
            patch.object(dispatcher, "_submit_guangya", side_effect=remote_write),
        ):
            session, _, _ = persistent_runtime()
            events = asyncio.run(confirmation(session, plan))
            pipe.send(("finished", any(e.type is AgentEventType.EFFECT_COMPLETED for e in events)))
    finally:
        pipe.close()


@pytest.mark.parametrize("exit_kind", ["normal", "killed"])
def test_process_exit_releases_scope_and_restart_never_reexecutes_claimed_plan(isolated_chain, exit_kind):
    import multiprocessing
    from tests.test_agent_ux_backend import SECRET

    async def exercise():
        writer, pipeline, store = persistent_runtime()
        plan, old = await prepare(writer, pipeline, store)
        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe()
        process = ctx.Process(target=_process_confirm, args=(str(db.DB_PATH), plan, child))
        process.start()
        child.close()
        try:
            assert await asyncio.to_thread(parent.poll, 10), "子进程未进入确认执行"
            stage, request_id = parent.recv()
            assert stage == "writing"
            with pytest.raises(SessionBusyError):
                await store.reset_session(owner=OWNER, session_id=SESSION)
            if exit_kind == "normal":
                parent.send("finish")
                assert await asyncio.to_thread(parent.poll, 10)
                assert parent.recv() == ("finished", True)
            else:
                process.kill()
            await asyncio.to_thread(process.join, 10)
            assert not process.is_alive()
            assert (process.exitcode == 0) is (exit_kind == "normal")
            restarted, re_pipeline, re_store = persistent_runtime()
            state = await re_store.load(owner=OWNER, session_id=SESSION)
            assert not await re_store.is_current(old)
            with pytest.raises(StalePublicationError):
                await re_store.commit(old, conversation=[])
            if exit_kind == "normal":
                assert any(f"下载请求 #{request_id}" in item.get("public_content", "") for item in state.conversation)
            replay = await confirmation(restarted, plan)
            assert any(e.payload.get("code") == "confirmation_invalid" for e in replay)
            assert isolated_chain[0].call_count == 0, "已认领票据在重启后不能重发"
            # 不等待 TTL；OS 已释放文件锁，新会话操作立即恢复。
            await lifecycle_for(restarted, re_pipeline, re_store).reset(owner=OWNER, session_id=SESSION)
        finally:
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 10)
            parent.close()
            process.close()
    with patch("app.modules.web_secret.get_web_secret", return_value=SECRET):
        asyncio.run(exercise())


def test_cancelled_state_transaction_keeps_guard_until_worker_commits(isolated_chain):
    """协程被取消不等于线程已结束；不能提前把正在提交的状态事务放行给确认。"""
    async def exercise():
        writer, pipeline, store = persistent_runtime()
        plan, old = await prepare(writer, pipeline, store)
        entered, release = threading.Event(), threading.Event()
        real_write = store._write_state

        def delayed_write(conn, state):
            entered.set()
            assert release.wait(5)
            return real_write(conn, state)

        with patch.object(store, "_write_state", side_effect=delayed_write):
            transaction = asyncio.create_task(store.commit(old, updates=(StateUpdate("summary", "旧读先提交"),)))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                transaction.cancel()
                events = await confirmation(writer, plan)
                assert any(e.payload.get("code") == "effect_in_progress" for e in events)
                assert isolated_chain[0].call_count == 0
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await transaction
        assert_completed(await confirmation(writer, plan))
        with pytest.raises(StalePublicationError):
            await store.commit(old, conversation=[])
    asyncio.run(exercise())


def test_disconnected_confirmation_keeps_cross_worker_guard_until_receipt(isolated_chain):
    async def exercise():
        writer, pipeline, store = persistent_runtime()
        plan, _ = await prepare(writer, pipeline, store)
        other, op, other_store = persistent_runtime()
        entered, release = threading.Event(), threading.Event()
        saved = asyncio.Event()
        real_commit = store.commit

        def remote_write(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return {"ok": True, "task_id": "fake-gy"}

        async def commit(lease, *, conversation=None, updates=()):
            result = await real_commit(lease, conversation=conversation, updates=updates)
            if conversation and any("下载请求 #" in item.get("public_content", "") for item in conversation):
                saved.set()
            return result

        isolated_chain[0].side_effect = remote_write
        with patch.object(store, "commit", side_effect=commit):
            stream = writer.confirm(owner=OWNER, session_id=SESSION, plan_id=plan)
            assert (await anext(stream)).type is AgentEventType.TURN_STARTED
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                await stream.aclose()
                with pytest.raises(SessionBusyError):
                    await lifecycle_for(other, op, other_store).reset(owner=OWNER, session_id=SESSION)
            finally:
                release.set()
                await asyncio.wait_for(saved.wait(), 5)
            # 等待 detached producer 的收尾，避免测试关闭事件循环代替生产者完成。
            pending = tuple(writer._detached_tasks)
            if pending:
                await asyncio.gather(*pending)
        state = await other_store.load(owner=OWNER, session_id=SESSION)
        assert any("下载请求 #" in item.get("public_content", "") for item in state.conversation)
        assert isolated_chain[0].call_count == 1
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", [False, OSError("test lock unavailable")])
def test_unavailable_scope_guard_fails_closed_before_ticket_claim(isolated_chain, failure):
    from app.modules.process_lock import CrossProcessLock

    async def exercise():
        writer, pipeline, store = persistent_runtime()
        plan, _ = await prepare(writer, pipeline, store)
        before = await store.load(owner=OWNER, session_id=SESSION)
        with patch.object(CrossProcessLock, "acquire", side_effect=failure if isinstance(failure, OSError) else None,
                          return_value=False):
            events = await confirmation(writer, plan)
        assert any(e.payload.get("code") == "effect_in_progress" for e in events)
        assert isolated_chain[0].call_count == 0
        assert (await store.load(owner=OWNER, session_id=SESSION)).conversation == before.conversation
        assert_completed(await confirmation(writer, plan))
    asyncio.run(exercise())


def test_new_turn_wins_before_confirmation_rejects_old_ticket_without_external_write(isolated_chain):
    async def exercise():
        writer, pipeline, store = persistent_runtime()
        plan, old = await prepare(writer, pipeline, store)
        _, _, other = persistent_runtime()
        new, _ = await other.begin_turn(owner=OWNER, session_id=SESSION, request_id="new-wins")
        events = await confirmation(writer, plan)
        assert any(e.payload.get("code") == "confirmation_invalid" for e in events)
        assert isolated_chain[0].call_count == 0
        assert await other.is_current(new)
        with pytest.raises(StalePublicationError):
            await other.commit(old, conversation=[])
        with db.get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0] == 0
    asyncio.run(exercise())


def test_cancelled_producer_drains_external_thread_and_persists_receipt(isolated_chain):
    """生产者取消也不能把仍在同步线程内执行的已确认副作用提前放锁。"""
    async def exercise():
        writer, pipeline, store = persistent_runtime()
        plan, old = await prepare(writer, pipeline, store)
        other, op, other_store = persistent_runtime()
        entered, release = threading.Event(), threading.Event()
        queue = asyncio.Queue()

        def remote_write(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return {"ok": True, "task_id": "fake-gy"}

        isolated_chain[0].side_effect = remote_write
        task = asyncio.create_task(writer._drive(
            QueryEnvelope(owner=OWNER, session_id=SESSION, message="继续已确认任务", request_id="cancel-producer", channel="api").to_agent_input(),
            queue, plan_id=plan,
        ))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            delivered = asyncio.Event()
            asyncio.get_running_loop().call_soon(delivered.set)
            await delivered.wait()
            with pytest.raises(SessionBusyError):
                await lifecycle_for(other, op, other_store).reset(owner=OWNER, session_id=SESSION)
        finally:
            release.set()
            await task
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        result = assert_completed(events)
        request_id = result["data"]["items"][0]["request_id"]
        state = await other_store.load(owner=OWNER, session_id=SESSION)
        assert any(f"下载请求 #{request_id}" in item.get("public_content", "") for item in state.conversation)
        assert not await other_store.is_current(old)
        assert isolated_chain[0].call_count == 1
    asyncio.run(exercise())


def _cancelled_work_process(mode, pipe):
    """R1 在独立子进程验收，回归成忙等时不能把整个 pytest 事件循环锁死。"""
    from app.agent.kernel.session_guard import guarded_state_call, session_scope_guard

    def cancelled_worker(*args, **kwargs):
        raise asyncio.CancelledError("work item cancelled itself")

    async def exercise(mocks):
        if mode == "confirmation":
            session, pipeline, store = persistent_runtime()
            plan, _ = await prepare(session, pipeline, store)
            mocks[0].side_effect = cancelled_worker
            events = await confirmation(session, plan)
            assert any(e.type is AgentEventType.TURN_CANCELLED for e in events)
            assert not any(e.type is AgentEventType.EFFECT_COMPLETED for e in events)
            replay = await confirmation(session, plan)
            assert any(e.payload.get("code") == "confirmation_invalid" for e in replay)
            assert mocks[0].call_count == 1
        else:
            with pytest.raises(asyncio.CancelledError):
                await guarded_state_call(OWNER, SESSION, cancelled_worker,
                    kind=mode, complete_on_cancel=(mode == "effect"))
        with session_scope_guard(OWNER, SESSION):
            pass
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await heartbeat.wait()
        pipe.send({"cancel_propagated": True, "guard_released": True, "heartbeat": True})

    fixture = isolated_chain.__wrapped__()
    try:
        asyncio.run(exercise(next(fixture)))
    finally:
        fixture.close()
        pipe.close()


@pytest.mark.parametrize("mode", ["mutation", "effect", "confirmation"])
def test_worker_owned_cancellation_never_spins_or_leaks_scope(mode):
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_cancelled_work_process, args=(mode, child))
    process.start()
    child.close()
    try:
        process.join(10)
        assert not process.is_alive(), "工作项自身取消导致忙等或持锁未收束"
        assert process.exitcode == 0
        assert parent.poll(1)
        assert parent.recv() == {"cancel_propagated": True, "guard_released": True, "heartbeat": True}
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        parent.close()
        process.close()


@pytest.mark.parametrize("phase", ["receipt", "reference", "journal"])
def test_cancellation_during_confirmed_finalization_keeps_receipt_and_terminal_fact(isolated_chain, phase):
    """R2：写后任一持久化等待点取消，不能留下 submitted 但无成功回执。"""
    from app.agent.models import ToolReference

    async def exercise():
        writer, pipeline, store = persistent_runtime()
        plan, old = await prepare(writer, pipeline, store)
        writer.journal = store
        other, op, other_store = persistent_runtime()
        entered, release = threading.Event(), threading.Event()
        queue = asyncio.Queue()
        method = "_put_ref_sync" if phase == "reference" else "_append_event_sync" if phase == "journal" else "_write_state"
        original = getattr(store, method)

        def persistence(*args, **kwargs):
            if phase == "reference":
                matched = args[2] == "download_receipt"
            elif phase == "journal":
                matched = args[0].type is AgentEventType.EFFECT_COMPLETED
            else:
                state = args[1]
                receipt = any("下载请求 #" in item.get("public_content", "") for item in state.conversation)
                matched = bool(state.metadata.get("confirmed_publication")) and not state.pending_effect_plan_id
                matched = matched and (receipt if phase == "receipt" else not receipt)
            if matched and not entered.is_set():
                entered.set()
                assert release.wait(5)
            return original(*args, **kwargs)

        def attach_reference(*, plan, value, elapsed_ms):
            value.references.append(ToolReference("download_receipt", {"request_id": value.data["request_id"]}))

        with patch.object(store, method, side_effect=persistence), \
             patch.object(pipeline.effect_lifecycle, "completed", side_effect=attach_reference):
            task = asyncio.create_task(writer._drive(QueryEnvelope(owner=OWNER, session_id=SESSION,
                message="继续已确认任务", request_id="finalization-cancel", channel="api").to_agent_input(), queue, plan_id=plan))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                assert isolated_chain[0].call_count == 1
                task.cancel()
                delivered = asyncio.Event()
                asyncio.get_running_loop().call_soon(delivered.set)
                await delivered.wait()
                with pytest.raises(SessionBusyError):
                    await lifecycle_for(other, op, other_store).reset(owner=OWNER, session_id=SESSION)
                with pytest.raises(StalePublicationError):
                    await other_store.commit(old, conversation=[])
            finally:
                release.set()
                await asyncio.wait_for(task, 5)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        result = assert_completed(events)
        assert events[-1].type is AgentEventType.TURN_COMPLETED
        assert not any(e.type is AgentEventType.TURN_CANCELLED for e in events)
        request_id = result["data"]["items"][0]["request_id"]
        state = await other_store.load(owner=OWNER, session_id=SESSION)
        assert not state.pending_effect_plan_id
        assert any(f"下载请求 #{request_id}" in item.get("public_content", "") for item in state.conversation)
        assert db.get_download_request(request_id)["status"] == "submitted"
        assert not await other_store.is_current(old)
        replay = await confirmation(other, plan)
        assert any(e.payload.get("code") == "confirmation_invalid" for e in replay)
        assert isolated_chain[0].call_count == 1
    asyncio.run(exercise())
