"""真实 Kernel 与 SQLite 中，领域预览只属于创建它的会话生命周期。"""
from __future__ import annotations

import asyncio

import pytest

from app.agent.models import ToolContext
from app.agent.errors import ToolPipelineError
from app.agent.kernel.bootstrap import build_agent_kernel_runtime
from app.agent.kernel.effects import InMemoryRateLimiter
from tests.support import initialized_web_runtime
from tests.test_agent_effect_admission import _OfflineClient, _offline_runtime, _domain_arguments, _paused_manager
from tests.test_agent_kernel_core import ScriptedModel
from tests.test_agent_kernel_existing_ports import context_for

OWNER = "webk:v1:" + "a" * 64


def runtime_for_test():
    runtime = build_agent_kernel_runtime(model=ScriptedModel([]))
    runtime.session.pipeline.rate_limiter = InMemoryRateLimiter(limit=100)
    return runtime


async def preview(runtime, kind, session_id):
    context = await context_for(runtime.store, owner=OWNER, session=session_id)
    return context


@pytest.mark.parametrize("kind", ["scrape"])
@pytest.mark.parametrize("operation", ["reset", "delete"])
@pytest.mark.parametrize("cold", [False, True], ids=["warm", "cold"])
def test_previews_cannot_cross_sessions_or_survive_lifecycle_change(tmp_path, kind, operation, cold):
    async def run():
        client = _OfflineClient()
        with initialized_web_runtime(), _offline_runtime(tmp_path, client) as stack, _paused_manager():
            arguments = (_domain_arguments("scrape", client, ToolContext(owner=OWNER), stack) if kind == "scrape" else {})
            runtime = runtime_for_test()
            tool = "cloud.scrape"
            origin = await preview(runtime, kind, "session-a")
            initial = await runtime.session.pipeline.execute(tool, arguments, context=origin)
            assert initial.effect_plan is not None

            other = await context_for(runtime.store, owner=OWNER, session="session-b")
            with pytest.raises(ToolPipelineError) as rejected:
                if kind == "scrape":
                    await runtime.session.pipeline.execute_confirmed(initial.effect_plan.plan_id, context=other)
                else:
                    await runtime.session.pipeline.execute(tool, arguments, context=other)
            assert rejected.value.code == ("confirmation_invalid" if kind == "scrape" else "precondition_failed")
            assert not (await runtime.store.load(owner=OWNER, session_id="session-b")).pending_effect_plan_id

            other_plan = (await runtime.session.pipeline.execute(tool, arguments, context=other)
                          if kind == "scrape" else None)
            await preview(runtime, kind, "session-b")
            await getattr(runtime.lifecycle, operation)(owner=OWNER, session_id="session-a")
            if cold:
                runtime = runtime_for_test()
            after = await context_for(runtime.store, owner=OWNER, session="session-a")
            with pytest.raises(ToolPipelineError) as rejected:
                if kind == "scrape":
                    await runtime.session.pipeline.execute_confirmed(initial.effect_plan.plan_id, context=after)
                else:
                    await runtime.session.pipeline.execute(tool, arguments, context=after)
            assert rejected.value.code == ("confirmation_invalid" if kind == "scrape" else "precondition_failed")
            assert not (await runtime.store.load(owner=OWNER, session_id="session-a")).pending_effect_plan_id

            # 清空 A 不能误清同一 owner 的 B；冷重建也必须保持这一事实。
            if other_plan is not None:
                assert other_plan.effect_plan is not None
                state_b = await runtime.store.load(owner=OWNER, session_id="session-b")
                assert state_b.pending_effect_plan_id == other_plan.effect_plan.plan_id
            other = await context_for(runtime.store, owner=OWNER, session="session-b")
            valid = await runtime.session.pipeline.execute(tool, arguments, context=other)
            assert valid.effect_plan is not None
            assert valid.effect_plan.session_id == "session-b"
            assert valid.effect_plan.plan_id != initial.effect_plan.plan_id
    asyncio.run(run())
