import asyncio

import pytest

from app.agent.kernel.adapters import consume_events
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.state_types import StateUpdate
from tests.support import isolated_test_database
from tests.test_pending_explanation_transports import NativeRuntime


@pytest.mark.parametrize("cross_generation", [False, True])
def test_legacy_ticket_adopts_binding_only_in_issuing_generation(cross_generation):
    async def run():
        runtime = NativeRuntime()
        lease, _ = await runtime.store.begin_turn(
            owner="owner", session_id="legacy", request_id="legacy"
        )
        tool = runtime.catalog.get("cloud.change")
        plan = runtime.effects.freeze(
            owner="owner",
            session_id="legacy",
            generation=lease.generation,
            tool_name=tool.name,
            effect=tool.effect,
            arguments={"step": 1},
            prepared=PreparedEffect(
                preview={"summary": "旧计划"}, snapshot_fingerprint="frozen-1"
            ),
        )
        await runtime.store.commit(
            lease,
            conversation=[{"role": "user", "content": "改名一个对象"}],
            updates=(StateUpdate("pending_effect_plan_id", plan.plan_id),),
        )
        if cross_generation:
            await runtime.store.begin_turn(
                owner="owner", session_id="legacy", request_id="legacy-next"
            )
        before = await runtime.store.load(owner="owner", session_id="legacy")
        view = await consume_events(
            runtime.session.explain(
                owner="owner", session_id="legacy", plan_id=plan.plan_id, message="解释"
            )
        )
        after = await runtime.store.load(owner="owner", session_id="legacy")
        if cross_generation:
            assert view.error_code == "confirmation_invalid"
            assert after == before and runtime.wires == []
        else:
            # Native fixture intentionally asks for a tool on its first request;
            # admission succeeds but execution is hard-rejected, without refresh.
            assert view.error_code == "explanation_tools_forbidden"
            assert after.generation == before.generation + 1
            assert after.metadata["pending_effect_binding"] == {
                "plan_id": plan.plan_id,
                "plan_generation": plan.generation,
            }
            assert (
                await runtime.session.pipeline.get_pending_plan(
                    owner="owner", session_id="legacy"
                )
                == plan
            )
        assert runtime.writes == [] and runtime.prepares == []

    with isolated_test_database("explain-legacy.db"):
        asyncio.run(run())
