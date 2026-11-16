"""SDK IPC 的同请求单次受理；poll 由队列推进，不依赖 sleep 竞态。"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from app.agent import rate_limit as rates
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.ports.mediaflux_policy import MediaFluxToolRateLimiter
from app.agent.kernel.references import InMemoryReferenceStore
from app.agent.kernel.state import InMemorySessionStateStore
from tests.support import isolated_test_database
from tests.test_agent_code_sandbox import make_context


@asynccontextmanager
async def _bridge(root, effects, *, context=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / "run").mkdir()
    (root / "output").mkdir()
    progress = AsyncMock()
    context = replace(context or await make_context(InMemorySessionStateStore()), report_progress=progress)
    if isinstance(effects, SimpleNamespace):
        effects.admit_call = AsyncMock()
        effects.reference_store = InMemoryReferenceStore()
    rpc = sandbox._SDKRequests(context, root, cast(EffectExecutor, effects))
    rpc.uid = os.getuid()
    try:
        yield rpc, progress
    finally:
        await rpc.close()


@asynccontextmanager
async def _controlled_poll(rpc):
    scanned, advance = asyncio.Queue(), asyncio.Queue()

    async def pause(_seconds):
        scanned.put_nowait(None)
        await advance.get()

    async def next_scan():
        advance.put_nowait(None)
        await asyncio.wait_for(scanned.get(), 2)

    with patch.object(sandbox.asyncio, "sleep", new=pause):
        task = asyncio.create_task(rpc.poll())
        try:
            await asyncio.wait_for(scanned.get(), 2)
            yield next_scan
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _publish(rpc, number, method="resources.download", arguments=None):
    name = f"{number:032x}.json"
    path = rpc.root / "run/mf-requests" / name
    path.write_text(json.dumps({"method": method, "arguments": arguments or {}}))
    return name


@pytest.fixture
def sqlite_admission():
    with (
        isolated_test_database("sdk-ipc-admission.db"),
        patch.dict(rates._TOOL_RATE_LIMITS, {"library.episodes": 4}),
        patch.object(rates, "agent_rate_limiter", rates.AgentRateLimiter(shared=True, wall_clock=lambda: 120.0)),
    ):
        yield


@asynccontextmanager
async def _sqlite_bridge(root, *, owner="quota-owner", session="quota-session"):
    store = SQLiteKernelStore(secret_provider=lambda: "ipc-admission-test-secret")
    effects = EffectExecutor(
        catalog=ToolCatalog(), state_store=store, reference_store=store,
        rate_limiter=MediaFluxToolRateLimiter(),
    )
    context = await make_context(store, owner=owner, session=session)
    async with _bridge(root, effects, context=context) as (rpc, _):
        yield rpc, effects


def test_exhausted_sqlite_read_quota_blocks_provider_and_keeps_other_owner_independent(tmp_path, sqlite_admission):
    async def run():
        invoked = []

        def read(query: str):
            invoked.append(query)
            return {"query": query}

        for _ in range(4):
            assert rates.consume_agent_tool("quota-owner", "library.episodes").allowed
        decision = rates.consume_agent_tool("quota-owner", "library.episodes")
        assert not decision.allowed
        expected = {key: getattr(decision, key) for key in (
            "limit", "cost", "remaining", "retry_after_seconds", "retry_at_epoch", "reason",
        )}
        with patch.dict(sandbox.METHODS, {"library.episodes": read}):
            async with _sqlite_bridge(tmp_path / "exhausted") as (rpc, _):
                name = _publish(rpc, 1, "library.episodes", {"query": "blocked"})
                await rpc.dispatch(name)
                reply = json.loads((rpc.root / "run/mf-responses" / name).read_text())
                assert invoked == []
                assert reply["error"]["code"] == "rate_limited"
                assert reply["error"]["execution_started"] is False
                assert reply["error"]["rate_limit"] == expected
            async with _sqlite_bridge(tmp_path / "other-owner", owner="other-owner") as (rpc, _):
                name = _publish(rpc, 1, "library.episodes", {"query": "independent"})
                await rpc.dispatch(name)
                assert json.loads((rpc.root / "run/mf-responses" / name).read_text())["result"] == {"query": "independent"}
                assert invoked == ["independent"]

    asyncio.run(run())


def test_concurrent_sqlite_read_admission_shares_four_permits_across_bridges(tmp_path, sqlite_admission):
    async def run():
        invoked = []

        def read(query: str):
            invoked.append(query)
            return {"query": query}

        with patch.dict(sandbox.METHODS, {"library.episodes": read}):
            async with (
                _sqlite_bridge(tmp_path / "first", session="first") as (first, _),
                _sqlite_bridge(tmp_path / "second", session="second") as (second, _),
            ):
                requests = [
                    (rpc, _publish(rpc, index, "library.episodes", {"query": str(index)}))
                    for index, rpc in enumerate((first, second) * 3)
                ]
                await asyncio.gather(*(rpc.dispatch(name) for rpc, name in requests))
                replies = [json.loads((rpc.root / "run/mf-responses" / name).read_text()) for rpc, name in requests]
                assert len(invoked) == 4 and len(set(invoked)) == 4
                assert sum("result" in reply for reply in replies) == 4
                rejected = [reply["error"] for reply in replies if "error" in reply]
                assert len(rejected) == 2
                assert all(error["code"] == "rate_limited" and error["execution_started"] is False for error in rejected)
                assert all(error["rate_limit"]["remaining"] == 0 for error in rejected)

    asyncio.run(run())


@pytest.mark.parametrize("ending", ("cancel", "revoked"))
def test_read_admission_rechecks_cancellation_and_lease_after_await(tmp_path, sqlite_admission, ending):
    async def run():
        invoked = []

        def read(query: str):
            invoked.append(query)
            return {"query": query}

        with patch.dict(sandbox.METHODS, {"library.episodes": read}):
            async with _sqlite_bridge(tmp_path) as (rpc, effects):
                entered, release = asyncio.Event(), asyncio.Event()
                acquire = effects.rate_limiter.acquire

                async def delayed_acquire(**kwargs):
                    await acquire(**kwargs)
                    entered.set()
                    await release.wait()

                with patch.object(effects.rate_limiter, "acquire", new=delayed_acquire):
                    name = _publish(rpc, 1, "library.episodes", {"query": "blocked"})
                    dispatch = asyncio.create_task(rpc.dispatch(name))
                    try:
                        await asyncio.wait_for(entered.wait(), 2)
                        if ending == "cancel":
                            rpc.context.cancellation.cancel()
                        else:
                            await effects.state_store.begin_turn(
                                owner=rpc.context.owner, session_id=rpc.context.session_id, request_id="replacement",
                            )
                    finally:
                        release.set()
                        await asyncio.gather(dispatch, return_exceptions=True)
                assert invoked == []
                if ending == "cancel":
                    assert dispatch.cancelled()
                    assert not (rpc.root / "run/mf-responses" / name).exists()
                else:
                    reply = json.loads((rpc.root / "run/mf-responses" / name).read_text())
                    assert "error" in reply and "result" not in reply

    asyncio.run(run())


def test_approval_request_is_not_dispatched_again_after_task_completion(tmp_path):
    async def run():
        effects = SimpleNamespace(
            prepare=AsyncMock(return_value=SimpleNamespace(
                effect_plan=object(), tool=SimpleNamespace(metadata={}),
            ))
        )
        with patch.dict(sandbox.METHODS, {"resources.download": lambda: None}):
            async with _bridge(tmp_path, effects) as (rpc, progress):
                name = _publish(rpc, 1)
                async with _controlled_poll(rpc) as next_scan:
                    await asyncio.gather(*rpc.pending.values())
                    assert rpc.proposal is not None and rpc.approval_ready.is_set()
                    assert not (rpc.root / "run/mf-responses" / name).exists()
                    # 第一轮dispatch已经return，第二轮poll必须能释放槽位而不重派该request。
                    await next_scan()
                    await asyncio.gather(*rpc.pending.values())
                    assert rpc.calls == [
                        {
                            "method": "resources.download",
                            "ok": True,
                            "status": "approval_required",
                        }
                    ]
                    effects.prepare.assert_awaited_once()
                    effects.admit_call.assert_not_awaited()
                    activities = [call.args[0]["activity"] for call in progress.await_args_list]
                    assert [item["status"] for item in activities] == ["running", "approval_required"]
                    assert len({item["id"] for item in activities}) == 1

    asyncio.run(run())


def test_distinct_requests_use_four_slots_and_release_completed_slots(tmp_path):
    async def run():
        import threading

        loop, entered = asyncio.get_running_loop(), asyncio.Queue()
        release, lock = threading.Event(), threading.Lock()
        active = maximum = 0
        seen = []

        def read(query: str):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                seen.append(query)
            loop.call_soon_threadsafe(entered.put_nowait, query)
            try:
                release.wait()
                return {"query": query}
            finally:
                with lock:
                    active -= 1

        effects = SimpleNamespace(prepare=AsyncMock())
        with patch.dict(sandbox.METHODS, {"library.episodes": read}):
            async with _bridge(tmp_path, effects) as (rpc, progress):
                names = {_publish(rpc, index, "library.episodes", {"query": str(index)}) for index in range(5)}
                try:
                    async with _controlled_poll(rpc) as next_scan:
                        for _ in range(4):
                            await asyncio.wait_for(entered.get(), 2)
                        assert len(rpc.pending) == 4 and len(seen) == maximum == 4
                        await next_scan()
                        assert len(seen) == 4
                        release.set()
                        await asyncio.gather(*rpc.pending.values())
                        await next_scan()
                        await asyncio.gather(*rpc.pending.values())
                        assert sorted(seen) == [str(index) for index in range(5)]
                        assert maximum == 4 and len(rpc.calls) == 5
                        assert all(call["ok"] for call in rpc.calls)
                        effects.prepare.assert_not_awaited()
                        activities = [call.args[0]["activity"] for call in progress.await_args_list]
                        by_id = {}
                        for item in activities:
                            by_id.setdefault(item["id"], []).append(item)
                        assert len(by_id) == 5
                        assert set(by_id) == names
                        assert all([item["status"] for item in pair] == ["running", "success"] for pair in by_id.values())
                        assert all(pair[0]["label"] == "查询媒体库季集" for pair in by_id.values())
                finally:
                    release.set()

    asyncio.run(run())


def test_read_error_can_be_retried_with_new_request_id(tmp_path):
    async def run():
        invoked = []

        def read(query: str):
            invoked.append(query)
            if query == "bad":
                raise ValueError("correct the query")
            return {"items": [query]}

        effects = SimpleNamespace(prepare=AsyncMock())
        with patch.dict(sandbox.METHODS, {"library.episodes": read}):
            async with _bridge(tmp_path, effects) as (rpc, progress):
                failed = _publish(rpc, 1, "library.episodes", {"query": "bad"})
                async with _controlled_poll(rpc) as next_scan:
                    await asyncio.gather(*rpc.pending.values())
                    responses = rpc.root / "run/mf-responses"
                    assert (
                        json.loads((responses / failed).read_text())["error"]["code"]
                        == "ValueError"
                    )
                    # 客户端清理旧请求；更正参数的正常SDK重试会生成新ID。
                    (rpc.root / "run/mf-requests" / failed).unlink()
                    fresh = _publish(rpc, 2, "library.episodes", {"query": "good"})
                    await next_scan()
                    await asyncio.gather(*rpc.pending.values())
                    assert json.loads((responses / fresh).read_text())["result"] == {
                        "items": ["good"]
                    }
                    assert invoked == ["bad", "good"]
                    assert [call["ok"] for call in rpc.calls] == [False, True]
                    effects.prepare.assert_not_awaited()
                    activities = [call.args[0]["activity"] for call in progress.await_args_list]
                    assert [item["status"] for item in activities] == ["running", "failed", "running", "success"]
                    assert "correct the query" in activities[1]["summary"]
                    assert "本次返回 1 项" in activities[-1]["summary"]

    asyncio.run(run())


def test_unknown_write_is_not_replayed_when_response_disappears(tmp_path):
    async def run():
        effects = SimpleNamespace(
            prepare=AsyncMock(
                return_value=SimpleNamespace(
                    effect_plan=None,
                    outcome=SimpleNamespace(
                        public_content={"ok": False, "status": "outcome_unknown"}
                    ),
                )
            )
        )
        with patch.dict(sandbox.METHODS, {"resources.download": lambda: None}):
            async with _bridge(tmp_path, effects) as (rpc, _):
                name = _publish(rpc, 1)
                async with _controlled_poll(rpc) as next_scan:
                    await asyncio.gather(*rpc.pending.values())
                    response = rpc.root / "run/mf-responses" / name
                    reply = response.read_bytes()
                    assert json.loads(reply)["result"]["status"] == "outcome_unknown"
                    # 响应不再可见不是“未执行”的证据，不能靠响应存在性防重放。
                    response.unlink()
                    await next_scan()
                    await asyncio.gather(*rpc.pending.values())
                    assert rpc.calls == [
                        {
                            "method": "resources.download",
                            "ok": False,
                            "status": "outcome_unknown",
                        }
                    ]
                    effects.prepare.assert_awaited_once()
                    effects.admit_call.assert_not_awaited()
                    assert not response.exists()
                    response.write_bytes(reply)

    asyncio.run(run())


def test_cancellation_stops_inflight_dispatch_without_late_response(tmp_path):
    async def run():
        import threading

        loop, entered = asyncio.get_running_loop(), asyncio.Queue()
        release, finished = threading.Event(), threading.Event()

        def read():
            loop.call_soon_threadsafe(entered.put_nowait, None)
            release.wait()
            finished.set()
            return {"late": True}

        effects = SimpleNamespace(prepare=AsyncMock())
        try:
            with patch.dict(sandbox.METHODS, {"library.episodes": read}):
                async with _bridge(tmp_path, effects) as (rpc, _):
                    _publish(rpc, 1, "library.episodes")
                    async with _controlled_poll(rpc):
                        await asyncio.wait_for(entered.get(), 2)
                        rpc.context.cancellation.cancel()
                # close已经取消dispatch并关闭协议目录句柄，宿主读函数才允许返回。
                assert all(task.cancelled() for task in rpc.pending.values())
                release.set()
                assert await asyncio.to_thread(finished.wait, 2)
                assert not rpc.calls and not list(
                    (tmp_path / "run/mf-responses").iterdir()
                )
                effects.prepare.assert_not_awaited()
        finally:
            release.set()

    asyncio.run(run())
