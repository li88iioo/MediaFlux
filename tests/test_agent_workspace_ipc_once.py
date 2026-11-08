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

from app.agent.kernel import sandbox
from app.agent.kernel.effects import EffectExecutor
from app.agent.kernel.references import InMemoryReferenceStore
from app.agent.kernel.state import InMemorySessionStateStore
from tests.test_agent_code_sandbox import make_context


@asynccontextmanager
async def _bridge(root, effects):
    (root / "run").mkdir()
    (root / "output").mkdir()
    progress = AsyncMock()
    context = replace(await make_context(InMemorySessionStateStore()), report_progress=progress)
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
                for index in range(5):
                    _publish(rpc, index, "library.episodes", {"query": str(index)})
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
