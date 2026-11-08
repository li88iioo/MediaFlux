"""跨模型轮次复用 call_id 时，SQLite READ 等待仍绑定原生工具组。"""
import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from app import database as db
from app.agent.errors import ToolRateLimitError
from app.agent.feature_gate import initialize_runtime_fence
from app.agent.kernel.bootstrap import (
    _capture_creation_binding,
)
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.state_types import AgentInput
from app.agent.rate_limit import RateLimitDecision
from tests.agent_task_native_helpers import task_call
from tests.test_agent_read_wait_lifecycle import build_wait_runtime
from tests.test_agent_read_wait_lifecycle import isolated_database as isolated_database
from tests.test_agent_task_first_slice import GOAL, OWNER, NativeScript, declaration


class ReusedCallScript(NativeScript):
    def __init__(self, *, repeated=False, resume=False):
        super().__init__("tmdb")
        self.repeated = repeated
        self.steps = ["finish"] if resume else [
            "discover", "create", "read-first", "read-second", "finish",
        ]

    async def respond(self, request):
        self.requests.append(json.loads(request.content))
        step = self.steps.pop(0)
        if step == "finish":
            message = {"content": "仅报告已经取得的实际查询结果。"}
        else:
            name, arguments = {
                "discover": ("agent__run_code", {"language": "shell", "code": "mf --help"}),
                "create": task_call(declaration(), wire=True),
                "read-first": ("fixture__lookup", {"query": "first"}),
                "read-second": ("fixture__lookup", {"query": "second"}),
            }[step]
            call_id = "read-id" if self.repeated and step.startswith("read-") else step
            message = {"tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }]}
        return httpx.Response(200, json={"choices": [{
            "message": message,
            "finish_reason": "stop" if step == "finish" else "tool_calls",
        }]})


def read_results(conversation):
    call_ids = {call["call_id"] for row in conversation
                for call in row.get("tool_calls", ()) if call["name"] in {"fixture.lookup", "fixture__lookup"}}
    return [row for row in conversation if row.get("role") == "tool"
            and row.get("tool_call_id") in call_ids]


@pytest.mark.parametrize("repeated", [False, True], ids=["unique-ids", "reused-id"])
def test_cross_round_read_id_parks_and_cold_resume_uses_original_second_query(repeated):
    async def run():
        db.init_db()
        initialize_runtime_fence()
        timer = [100]
        script = ReusedCallScript(repeated=repeated)
        store = SQLiteKernelStore(clock=lambda: timer[0])
        runtime = build_wait_runtime(model=script.adapter, store=store)
        runtime.session.continuation_binding = _capture_creation_binding()
        invoked = []

        async def read(arguments, context):
            invoked.append(dict(arguments))
            return {"ok": True, "status": "success", "summary": arguments["query"] + " complete"}

        class SecondReadQuota:
            count = 0

            async def acquire(self, *, tool_name, **kwargs):
                if tool_name == "fixture.lookup":
                    self.count += 1
                    if self.count == 2:
                        raise ToolRateLimitError(RateLimitDecision(
                            False, 6, 1, 0, retry_after_seconds=10,
                            retry_at_epoch=110, reason="quota",
                        ))

        spec = runtime.session.catalog.get("fixture.lookup")
        runtime.session.catalog._tools[spec.name] = replace(spec, read=read)
        runtime.session.pipeline.rate_limiter = SecondReadQuota()
        events = [event async for event in runtime.session.run(AgentInput(
            message=GOAL, owner=OWNER, session_id="read-id-reuse", request_id="source",
        ))]
        assert events[-1].payload.get("status") == "waiting"
        assert len(script.requests) == 4
        first_arguments = {"query": "first"}
        second_arguments = {"query": "second"}
        assert invoked == [first_arguments]

        cold = SQLiteKernelStore(clock=lambda: timer[0])
        parked = await cold.load(owner=OWNER, session_id="read-id-reuse")
        wait = parked.metadata["read_wait"]
        assert wait["state"] == "pending" and wait["due_at"] == 110
        assert wait["budget"]["model_spent"] == 4
        assert wait["budget"]["tool_spent"] == 4
        assert wait["budget"]["wait_segments"] == 1
        first_id, second_id = ("read-id", "read-id") if repeated else ("read-first", "read-second")
        history = read_results(parked.conversation)
        assert [row["tool_call_id"] for row in history] == [first_id, second_id]
        assert "first complete" in history[0]["content"]
        assert "rate_limited" in history[1]["content"]

        timer[0] = 110
        candidates = await cold.due_continuations()
        assert len(candidates) == 1
        resumed_script = ReusedCallScript(resume=True)
        fresh = build_wait_runtime(model=resumed_script.adapter, store=cold)
        fresh.session.continuation_binding = _capture_creation_binding()

        async def resumed_read(arguments, context):
            assert not resumed_script.requests, "原参数 READ 应先于模型合成执行"
            return await read(arguments, context)

        spec = fresh.session.catalog.get("fixture.lookup")
        fresh.session.catalog._tools[spec.name] = replace(spec, read=resumed_read)
        assert await fresh.session.resume_wait(candidate=candidates[0])
        assert invoked == [first_arguments, second_arguments]
        assert len(resumed_script.requests) == 1
        state = await cold.load(owner=OWNER, session_id="read-id-reuse")
        terminal = state.metadata["read_wait"]
        assert terminal["state"] == "terminal"
        assert terminal["terminal_reason"] == "completed"
        assert terminal["budget"]["model_spent"] == 5
        assert terminal["budget"]["tool_spent"] == 5
        assert terminal["budget"]["wait_segments"] == 1
        assert terminal["budget"]["wait_seconds_spent"] == 10
        final_history = read_results(state.conversation)
        assert final_history[:2] == history
        assert len(final_history) == 3 and "second complete" in final_history[2]["content"]
        assert final_history[2]["tool_call_id"] not in {first_id, second_id}
        wire_history = [row for row in resumed_script.requests[0]["messages"]
                        if row.get("role") == "tool" and row.get("tool_call_id") in {first_id, second_id}]
        assert len(wire_history) == 2
        assert "first complete" in wire_history[0]["content"]
        assert "rate_limited" in wire_history[1]["content"]
        assert not await fresh.session.resume_wait(candidate=candidates[0])
        assert invoked == [first_arguments, second_arguments]
        assert await cold.due_continuations() == []

    asyncio.run(run())
