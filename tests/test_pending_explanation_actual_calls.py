import asyncio

import pytest

from app.agent.kernel.adapters import consume_events
from app.agent.kernel.state_types import AgentInput
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414
from tests.support import isolated_test_database
from tests.test_pending_explanation_transports import NativeRuntime


@pytest.mark.parametrize("name", [None, "tools_search", "refs_read", "task_update"])
@pytest.mark.parametrize("arguments", ['{"step":1}', '{"step":'])
def test_any_actual_call_is_rejected_without_repair_or_dispatch(agent_llm_enabled, name, arguments):
    async def run():
        runtime = NativeRuntime()
        preview = await consume_events(
            runtime.session.run(
                AgentInput(
                    message="改名一个对象", owner="owner", session_id="hard-reject"
                )
            )
        )
        frozen = await runtime.session.pipeline.get_pending_plan(
            owner="owner", session_id="hard-reject"
        )
        runtime.force_call = True
        runtime.call_name = name
        runtime.call_arguments = arguments
        assert preview.approval is not None
        view = await consume_events(
            runtime.session.explain(
                owner="owner",
                session_id="hard-reject",
                plan_id=preview.approval.plan_id,
                message="只解释，不能调用任何工具",
            )
        )
        assert view.error_code == "explanation_tools_forbidden"
        assert len(runtime.wires) == 2 and not runtime.wires[-1].get("tools")
        assert runtime.writes == [] and len(runtime.prepares) == 1
        assert (
            await runtime.session.pipeline.get_pending_plan(
                owner="owner", session_id="hard-reject"
            )
            == frozen
        )

    with isolated_test_database("explain-hard-reject.db"):
        asyncio.run(run())
