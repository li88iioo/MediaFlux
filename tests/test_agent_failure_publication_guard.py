"""Cold cancellation fences executor failures at journal and delivery boundaries."""
import asyncio
import threading
from contextlib import suppress
from dataclasses import replace
from unittest.mock import patch

import pytest

from app.agent.errors import ToolPipelineError
from app.agent.kernel.bootstrap import (
    _capture_creation_binding,
)
from app.agent.kernel.persistence import SQLiteKernelStore
from tests.test_agent_read_wait_lifecycle import build_wait_runtime
from tests.test_agent_read_wait_lifecycle import (
    OWNER,
    WaitScript,
    parked_runtime,
)
from tests.test_agent_read_wait_lifecycle import (
    isolated_database as isolated_database,
)


@pytest.mark.parametrize("phase", ["before_append", "after_append", "not_cancelled"])
@pytest.mark.parametrize("terminal", ["turn.failed", "turn.completed"])
def test_cold_cancel_blocks_late_terminal_failure(phase, terminal):
    async def run():
        clock = [100]
        _, candidate = await parked_runtime(clock)
        clock[0] = 110
        script = WaitScript(True)
        store = SQLiteKernelStore(clock=lambda: clock[0])
        runtime = build_wait_runtime(model=script.adapter, store=store)
        runtime.session.continuation_binding = _capture_creation_binding()
        async def read(arguments, context):
            if terminal == "turn.failed":
                raise ToolPipelineError("fixture READ failure", code="fixture_read_error")
            return {"ok": True, "summary": "fixture READ success"}
        tool = runtime.session.catalog.get('fixture.lookup')
        runtime.session.catalog._tools['fixture.lookup'] = replace(tool, read=read)
        entered, release = threading.Event(), threading.Event()
        append = store._append_event_sync
        delivered = []
        background = runtime.session._run_background

        async def observe(*args, **kwargs):
            async for event in background(*args, **kwargs):
                delivered.append(event.type.value)
                yield event

        def gate(event, owner, *args):
            if event.type.value != terminal:
                return append(event, owner, *args)
            if phase == "after_append":
                append(event, owner, *args)
            entered.set()
            assert release.wait(5), "failure journal release timeout"
            if phase != "after_append":
                return append(event, owner, *args)

        with patch.object(store, "_append_event_sync", side_effect=gate), patch.object(
            runtime.session, "_run_background", side_effect=observe,
        ):
            worker = asyncio.create_task(runtime.session.resume_wait(candidate=candidate))
            try:
                assert await asyncio.to_thread(entered.wait, 5), "failure journal entry timeout"
                cold = build_wait_runtime(
                    model=WaitScript(True).adapter,
                    store=SQLiteKernelStore(clock=lambda: clock[0]),
                )
                if phase != "not_cancelled":
                    assert await cold.session.cancel(owner=OWNER, session_id="wait-control", request_id="source")
                release.set()
                await asyncio.wait_for(worker, 5)
                events = await cold.store.list_events(owner=OWNER, session_id="wait-control")
                state = await cold.store.load(owner=OWNER, session_id="wait-control")
                terminals = [e for e in events if e["type"] == terminal]
                if terminal == "turn.completed":
                    # 初始 waiting 也是 completed 事件；仅成功用例排除该历史轮次。
                    terminals = [e for e in terminals if e["turn_id"] == state.metadata["read_wait"]["resume_turn_id"]]
                assert len(terminals) == (0 if phase == "before_append" else 1)
                assert (terminal in delivered) is (phase == "not_cancelled")
                assert "turn.started" in delivered
                assert state.metadata["active_publication"]["status"] == (
                    "finished" if phase == "not_cancelled" else "cancelled"
                )
            finally:
                release.set()
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(worker, 5)
    asyncio.run(run())
