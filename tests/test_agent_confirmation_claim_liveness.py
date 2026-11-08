"""待续行作业不能抢占无待确认卡、但仍在执行的直接确认尾段。"""
from __future__ import annotations

import asyncio

from app.agent.kernel.capabilities import KernelToolSpec, ToolEffect
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.model import ModelEvent, ModelEventType as M, ModelToolCall
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.state_types import AgentInput
from tests.test_agent_effect_continuation import FollowScript, OWNER, SID, _ready_case, _session
from tests.test_agent_kernel_core import collect
from tests.test_agent_recovered_progress import reply
from tests.test_agent_tool_argument_ingress import Wire


def test_pending_effect_wait_cannot_claim_a_live_direct_confirmation_tail(tmp_path):
    async def scenario():
        async with _ready_case(tmp_path) as (store, original, client, reads, stack, _users):
            held, release = asyncio.Event(), asyncio.Event()
            writes = []
            target = tmp_path / "confirmed.txt"

            def execute(*_):
                target.write_text("confirmed-once")
                writes.append("write")
                return {"ok": True, "status": "completed", "summary": "独立文件已保存"}

            tool = KernelToolSpec(
                name="fixture.save", domain="fixture", description="保存独立文件", effect=ToolEffect.WRITE,
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                prepare=lambda *_: PreparedEffect(preview={"ok": True, "summary": "保存文件预览"}, snapshot_fingerprint="file"),
                execute_confirmed=execute,
            )

            class HeldSynthesis:
                calls = 0

                async def stream(self, request, *, cancellation):
                    self.calls += 1
                    if self.calls == 1:
                        yield ModelEvent(M.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("save-file", tool.name, {}))
                        yield ModelEvent(M.FINISH, finish_reason="tool_calls")
                    else:
                        held.set()
                        await release.wait()
                        yield ModelEvent(M.TEXT_DELTA, text="独立文件已保存，以实际回执为准。")
                        yield ModelEvent(M.FINISH, finish_reason="stop")

            current = _session(store, FollowScript(resume=True), client, reads, stack=stack)
            current.catalog.register(tool)
            current.model = HeldSynthesis()
            preview = await collect(current.run(AgentInput(owner=OWNER, session_id=SID, message="保存独立文件")))
            assert preview[-1].payload["status"] == "approval_required"
            plan_id = preview[-1].payload["plan_id"]
            running = asyncio.create_task(collect(current.confirm(owner=OWNER, session_id=SID, plan_id=plan_id)))
            cold = SQLiteKernelStore()
            follow = _session(cold, FollowScript(resume=True), client, reads, stack=stack)
            readback = Wire("chat_completions", [
                reply("chat_completions", name="guangya__inspect_after", arguments={}),
                reply("chat_completions", text="已重新读取文件名 admitted.mp4。"),
            ])
            follow.model = readback.adapter
            try:
                await asyncio.wait_for(held.wait(), 8)
                assert writes == ["write"] and target.read_text() == "confirmed-once"
                before = await cold.load(owner=OWNER, session_id=SID)
                assert not before.pending_effect_plan_id
                assert before.metadata["active_publication"]["status"] == "running"
                candidates = await cold.due_continuations()
                assert len(candidates) == 1 and candidates[0]["wait_id"] == original["wait_id"]
                assert not await follow.resume_wait(candidate=candidates[0])
                after = await cold.load(owner=OWNER, session_id=SID)
                assert after.generation == before.generation
                assert after.metadata["effect_waits"][original["wait_id"]]["continuation"]["state"] == "pending"
                assert reads == [] and not readback.requests and not running.done()
            finally:
                release.set()
                await asyncio.wait_for(running, 8)
            candidates = await cold.due_continuations()
            assert len(candidates) == 1
            assert await follow.resume_wait(candidate=candidates[0])
            assert reads == ["admitted.mp4"] and writes == ["write"]
            readback.verify(2)
            assert await cold.due_continuations() == []

    asyncio.run(scenario())
