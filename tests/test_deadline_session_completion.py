"""Session discovery budgets at the real native request boundary."""

from typing import cast
from collections.abc import Callable
from app.clients.http import FixedHostHttpClient
import asyncio
import json
from contextlib import asynccontextmanager


from app.agent.kernel.events import AgentEventType
from app.agent.kernel.provider_model import (
    OpenAICompatibleModelAdapter,
    ProviderSettings,
)
from app.agent.kernel.session import SessionLimits
from tests.agent_model_tools_test_harness import request, session_for
from tests.test_agent_kernel_core import collect, read_tool


def run_native(batches, *, rounds=6, max_calls=1):
    async def exercise():
        seen, executed = [], []
        session, _ = session_for([read_tool(
            "alpha.inspect", handler=lambda a, c: executed.append("read") or {"summary": "verified"},
        )], [])
        session.limits = SessionLimits(max_tool_calls=max_calls, max_model_rounds=rounds)

        class Response:
            status_code = 200

            def __init__(self, body):
                self.body = body
                self.headers = {"content-type": "text/event-stream"}

            async def aiter_bytes(self):
                yield ("data: " + json.dumps(self.body) + "\n\ndata: [DONE]\n\n").encode()

        class Client:
            @asynccontextmanager
            async def stream_post_json(self, url, **kwargs):
                seen.append(kwargs["json"])
                index = len(seen) - 1
                calls = batches[index] if index < len(batches) else []
                delta = {"tool_calls": [
                    {"index": i, "id": f"c{index}-{i}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(args),
                    }} for i, (name, args) in enumerate(calls)
                ]} if calls else {"content": "已依据真实读取完成。"}
                yield Response({"choices": [{"delta": delta, "finish_reason": "tool_calls" if calls else "stop"}]})

            async def aclose(self):
                pass

        session.model = OpenAICompatibleModelAdapter(
            ProviderSettings(api_url="https://offline.invalid/v1", model="fixture", protocol="chat_completions"),
            client_factory=cast(Callable[..., FixedHostHttpClient], lambda **_: Client()),
        )
        events = await collect(session.run(request()))
        return seen, executed, events
    return asyncio.run(exercise())


READ = ("alpha__inspect", {})






def test_explicit_business_tool_budget_rejects_crossing_batch_atomically():
    seen, executed, events = run_native([[READ, READ]], max_calls=1)
    assert len(seen) == 1 and executed == []
    failed = [e for e in events if e.type is AgentEventType.TOOL_FAILED]
    assert len(failed) == 2
    assert all(e.payload["code"] == "tool_budget_exceeded" for e in failed)
    assert events[-1].payload["tool_calls"] == 0
    assert events[-1].payload["status"] == "partial"
