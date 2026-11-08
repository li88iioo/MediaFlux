"""READ 续行在真实进程退出后安全收束；存活执行者不受 claim TTL 影响。"""
from __future__ import annotations

import asyncio
import multiprocessing
import os
from dataclasses import replace

import pytest

from app import config
from app import database as db
from app.agent.kernel.bootstrap import (
    _capture_creation_binding,
)
from app.agent.kernel.model import ModelEvent, ModelEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.state_types import PublicationLease
from tests.support import isolated_test_database
from tests.test_agent_read_wait_lifecycle import build_wait_runtime
from tests.test_agent_read_wait_lifecycle import OWNER, parked_runtime

SESSION = "read-process-recovery"


def _probe(kind):
    with db.get_conn() as conn:
        conn.execute("INSERT INTO read_process_probe(kind) VALUES(?)", (kind,))


def _counts():
    with db.get_conn() as conn:
        return dict(conn.execute("SELECT kind,COUNT(*) FROM read_process_probe GROUP BY kind").fetchall())


def _worker(db_path, candidate, binding, phase, termination, ready, release):
    db.configure_database(db_path, test_mode=True)

    async def pause():
        ready.set()
        await asyncio.to_thread(release.wait)
        if termination == "exit":
            os._exit(23)

    class HeldModel:
        async def stream(self, request, *, cancellation):
            _probe("model")
            await pause()
            yield ModelEvent(ModelEventType.TEXT_DELTA, text="已保留结果")
            yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")

    class Limiter:
        async def acquire(self, *, tool_name, **kwargs):
            if phase == "acquire" and tool_name == "fixture.lookup":
                await pause()

    async def run():
        runtime = build_wait_runtime(model=HeldModel(), store=SQLiteKernelStore(clock=lambda: 110))
        runtime.session.continuation_binding = binding
        runtime.session.pipeline.rate_limiter = Limiter()

        async def read(arguments, context):
            _probe("read")
            if phase == "handler":
                await pause()
            return {"ok": True, "summary": "PROCESS_READ_COMMITTED", "data": {"fact": "saved"}}

        async def write(*args, **kwargs):
            _probe("write")
            raise AssertionError("READ continuation must not execute a write")

        catalog = runtime.session.catalog
        tool = catalog.get("fixture.lookup")
        catalog._tools[tool.name] = replace(tool, read=read)
        for tool in tuple(catalog._tools.values()):
            if tool.execute_confirmed is not None:
                catalog._tools[tool.name] = replace(tool, execute_confirmed=write)
        resumed = await runtime.session.resume_wait(candidate=candidate)
        if not ready.is_set():
            state = await runtime.store.load(owner=candidate["owner"], session_id=candidate["session_id"])
            events = await runtime.store.list_events(owner=candidate["owner"], session_id=candidate["session_id"])
            raise AssertionError((resumed, state.metadata.get("read_wait", {}).get("terminal_reason"),
                [event["payload"] for event in events[-1:]]))

    with config.use_runtime_config_snapshot(binding.snapshot):
        asyncio.run(run())


def _start_worker(path, candidate, phase, termination):
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    binding = _capture_creation_binding()
    # tests 包为 spawn 子进程创建另一套目录；使用同一部署的显式配置快照。
    binding = replace(binding, snapshot=replace(binding.snapshot,
        file_values=dict(binding.snapshot.file_values), startup_overrides=dict(binding.snapshot.startup_overrides)))
    worker = context.Process(target=_worker, args=(str(path), candidate, binding, phase, termination, ready, release))
    worker.start()
    if not ready.wait(15):
        if worker.is_alive():
            worker.kill()
        worker.join(10)
        raise AssertionError(f"continuation did not reach {phase}: exit={worker.exitcode}")
    return worker, release


async def _park():
    _, candidate = await parked_runtime([100], session_id=SESSION)
    with db.get_conn() as conn:
        conn.execute("CREATE TABLE read_process_probe(kind TEXT NOT NULL)")
    return candidate


@pytest.mark.parametrize("phase", ["acquire", "handler", "model"])
@pytest.mark.parametrize("termination", ["kill", "exit"])
def test_dead_read_worker_is_retired_without_replaying_started_work(phase, termination):
    with isolated_test_database() as path:
        candidate = asyncio.run(_park())
        worker, release = _start_worker(path, candidate, phase, termination)
        cold = SQLiteKernelStore(clock=lambda: 151)
        try:
            before = asyncio.run(cold.load(owner=OWNER, session_id=SESSION))
            record = before.metadata["read_wait"]
            assert record["state"] == "running" and record["claim_until"] == 140
            # The real process still owns the execution lock after the 30s claim TTL.
            assert asyncio.run(cold.due_continuations()) == []
            assert asyncio.run(cold.load(owner=OWNER, session_id=SESSION)) == before
            expected = {"read": 1, "model": 1} if phase == "model" else ({"read": 1} if phase == "handler" else {})
            assert _counts() == expected
            if phase == "model":
                assert "PROCESS_READ_COMMITTED" in str(before.conversation)

            if termination == "kill":
                worker.kill()
            else:
                release.set()
            worker.join(10)
            assert not worker.is_alive()
            if termination == "exit":
                assert worker.exitcode == 23
            else:
                assert worker.exitcode != 0

            async def verify():
                other = SQLiteKernelStore(clock=lambda: 151)
                assert await asyncio.gather(cold.due_continuations(), other.due_continuations()) == [[], []]
                after = await cold.load(owner=OWNER, session_id=SESSION)
                assert after.generation == before.generation
                assert after.conversation == before.conversation
                assert after.metadata["task"] == before.metadata["task"]
                recovered = after.metadata["read_wait"]
                assert recovered["state"] == "terminal"
                assert recovered["terminal_reason"] == "interrupted"
                assert recovered["budget"] == record["budget"]
                assert after.metadata["active_publication"]["status"] == "finished"
                lease = PublicationLease(OWNER, SESSION, before.generation, record["resume_turn_id"],
                    record["source_request_id"], record["wait_id"], record["claim_id"])
                assert not await cold.is_current(lease)
                assert await cold.claim_continuation(candidate, _capture_creation_binding()) is None
                events = await cold.list_events(owner=OWNER, session_id=SESSION)
                assert events[-1]["payload"]["code"] == "continuation_interrupted"
                assert events[-1]["turn_id"] == record["resume_turn_id"]
                assert await cold.due_continuations() == []
                assert await cold.list_events(owner=OWNER, session_id=SESSION) == events
                assert await cold.load(owner=OWNER, session_id=SESSION) == after

            asyncio.run(verify())
            assert _counts() == expected
        finally:
            if worker.is_alive():
                worker.kill()
            worker.join(10)


def test_cold_cancel_revokes_live_worker_without_waiting_for_its_execution_lock():
    with isolated_test_database() as path:
        candidate = asyncio.run(_park())
        worker, release = _start_worker(path, candidate, "handler", "kill")
        cold = SQLiteKernelStore(clock=lambda: 151)
        try:
            assert asyncio.run(cold.cancel_publication(owner=OWNER, session_id=SESSION, request_id="source"))
            cancelled = asyncio.run(cold.load(owner=OWNER, session_id=SESSION))
            assert cancelled.metadata["read_wait"]["state"] == "cancelled"
            assert worker.is_alive()
            assert asyncio.run(cold.due_continuations()) == []
            worker.kill()
            worker.join(10)
            assert asyncio.run(cold.due_continuations()) == []
            assert asyncio.run(cold.load(owner=OWNER, session_id=SESSION)) == cancelled
            assert _counts() == {"read": 1}
        finally:
            if worker.is_alive():
                worker.kill()
            worker.join(10)
