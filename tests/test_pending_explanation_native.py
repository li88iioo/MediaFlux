
from typing import cast
from collections.abc import Callable
from app.clients.http import FixedHostHttpClient
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel.adapters import consume_events
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.provider_model import (
    OpenAICompatibleModelAdapter,
    ProviderSettings,
)
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput
from tests.support import isolated_test_database


@pytest.mark.parametrize(
    "mode",
    ["explain", "direct", "cancel", "scope", "expiry", "permission", "replacement"],
)
def test_lifecycle(mode):
    import asyncio

    asyncio.run(lifecycle(mode))


async def lifecycle(mode):
    with isolated_test_database("id17-" + mode + ".db"):
        wires = []
        writes = []
        now = [1000.0]
        denied = [False]
        tool = KernelToolSpec(
            name="cloud.change",
            domain="cloud",
            description="改名",
            input_schema={
                "type": "object",
                "properties": {"step": {"type": "integer"}},
                "required": ["step"],
            },
            effect=ToolEffect.WRITE,
            prepare=lambda a, c: PreparedEffect(
                preview={"summary": "改名一个对象"}, snapshot_fingerprint="frozen-1"
            ),
            execute_confirmed=lambda a, s, c: (
                writes.append({"arguments": dict(a), "snapshot": s})
                or {"ok": True, "summary": "已改名"}
            ),
        )

        business_round = [0]

        def handler(request):
            body = json.loads(request.content)
            wires.append(body)
            n = business_round[0] + 1
            business_round[0] += 1
            if n == 1 or (mode in ("cancel", "replacement") and n == 2):
                cancelling = mode == "cancel" and n == 2
                delta = {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"native-{n}",
                            "type": "function",
                            "function": {
                                "name": (
                                    catalog.get("agent.run_code").model_name
                                    if cancelling
                                    else tool.model_name
                                ),
                                "arguments": json.dumps(
                                    {"code": "from mf import session\nprint(session.pending_plan(operation='cancel'))"} if cancelling else {"step": n}
                                ),
                            },
                        }
                    ]
                }
                finish = "tool_calls"
            else:
                delta = {"content": "这是改名预览，不会在解释时执行。"}
                finish = "stop"
            raw = (
                "data: "
                + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]})
                + "\n\ndata: [DONE]\n\n"
            )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=raw.encode()
            )

        class Client:
            def __init__(self):
                self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

            @asynccontextmanager
            async def stream_post_json(self, url, **kwargs):
                async with self.http.stream(
                    "POST", url, json=kwargs["json"], headers=kwargs.get("headers")
                ) as response:
                    yield response

            async def aclose(self):
                await self.http.aclose()

        class Authorization:
            async def authorize(self, *a, **kw):
                if denied[0]:
                    raise ToolPipelineError("权限已撤销", code="authorization_denied")

        catalog = ToolCatalog([tool])
        state = SQLiteKernelStore()
        tickets = SQLiteConfirmationStore(clock=lambda: now[0])
        effects = ConfirmationEffectPlanStore(tickets)

        def session():
            st = SQLiteKernelStore()
            from app.agent.kernel import sandbox
            pipeline = ToolPipeline(catalog=catalog, state_store=st, effect_store=effects, authorization=Authorization())
            if not catalog.has("agent.run_code"):
                catalog.register(sandbox.run_code_tool(pipeline))
            return AgentSession(
                model=OpenAICompatibleModelAdapter(
                    ProviderSettings(
                        api_url="https://example.invalid/v1",
                        model="fixture",
                        protocol="chat_completions",
                    ),
                    client_factory=cast(Callable[..., FixedHostHttpClient], lambda **_: Client()),
                ),
                catalog=catalog,

                pipeline=pipeline,
                state_store=st,
            )

        s = session()
        v = await consume_events(
            s.run(AgentInput(message="改名一个对象", owner="owner", session_id=mode))
        )
        assert v.approval is not None, v
        plan = v.approval.plan_id
        before = await state.load(owner="owner", session_id=mode)
        frozen = effects.get_active_plan(
            owner="owner", session_id=mode, generation=before.generation, plan_id=plan
        )
        assert frozen is not None and writes == []
        if mode in ("explain", "cancel", "scope", "replacement"):
            text = {
                "explain": "这张卡有什么影响？只解释，不修改计划。",
                "cancel": "算了，放弃原任务。",
                "scope": "先看看另外两个对象，不修改原计划。",
                "replacement": "改成第二个对象。",
            }[mode]
            if mode == "explain":
                follow = await consume_events(
                    session().explain(
                        message=text, owner="owner", session_id=mode, plan_id=plan
                    )
                )
                assert follow.status == "success", follow
                assert not wires[-1].get("tools")
            else:
                follow = await consume_events(
                    session().run(
                        AgentInput(message=text, owner="owner", session_id=mode)
                    )
                )
            if mode in ("explain", "scope"):
                assert follow.status == "success", follow
                assert follow.tool_calls == ()
                restored = await session().pipeline.get_pending_plan(
                    owner="owner", session_id=mode
                )
                assert restored == frozen
            elif mode == "cancel":
                assert follow.status == "success", follow
                assert follow.tool_calls == ("agent.run_code",)
            assert writes == []
        if mode == "expiry":
            now[0] = frozen.expires_at + 1
        if mode == "permission":
            denied[0] = True
        after = await SQLiteKernelStore().load(owner="owner", session_id=mode)
        result = await consume_events(
            session().confirm(owner="owner", session_id=mode, plan_id=plan)
        )
        replay = await consume_events(
            session().confirm(owner="owner", session_id=mode, plan_id=plan)
        )
        cold = await SQLiteKernelStore().load(owner="owner", session_id=mode)
        record = {
            "mode": mode,
            "before_generation": before.generation,
            "after_generation": after.generation,
            "original_plan": plan,
            "pending_after_followup": after.pending_effect_plan_id,
            "status": result.status,
            "error_code": result.error_code,
            "writes": writes,
            "wire_count": len(wires),
            "native_call_replayed": any("native-1" in json.dumps(x) for x in wires[1:]),
            "cold_pending": cold.pending_effect_plan_id,
        }
        assert replay.status == "failed", replay
        assert replay.error_code == "confirmation_invalid", replay
        if mode in ("explain", "scope"):
            assert after.generation == before.generation + 1
            assert after.pending_effect_plan_id == plan
            assert after.metadata["pending_effect_binding"] == {
                "plan_id": plan,
                "plan_generation": before.generation,
            }
        if mode in ("direct", "explain", "scope"):
            assert writes == [{"arguments": {"step": 1}, "snapshot": "frozen-1"}], (
                record
            )
            assert result.status == "success"
        else:
            assert writes == [], record
            assert result.status == "failed", record
            assert result.error_code == (
                "authorization_denied" if mode == "permission" else "confirmation_invalid"
            ), record
        if mode == "cancel":
            assert after.pending_effect_plan_id == "", record
        if mode != "replacement":
            assert cold.pending_effect_plan_id == "", record
        if mode == "replacement":
            assert after.pending_effect_plan_id not in ("", plan)
            assert cold.pending_effect_plan_id == after.pending_effect_plan_id
