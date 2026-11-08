"""原生组合的完整参数、真实 SQLite 收藏写入、CLI 单RPC与批次边界。"""
import asyncio
import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from app import config
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.sdk_effects import watchlist_tools
from app.discovery.models import MediaCard
from app.discovery.service import DiscoveryService
from tests import test_agent_session_result as results
from tests.support import isolated_test_database
from tests.test_agent_sdk_effects import context_for

native_workspace = results.native_workspace


@pytest.mark.parametrize("count,duplicate,language", [(1, False, "python"), (6, False, "shell"), (6, True, "python")])
def test_native_real_ids_repeat_method_and_full_scope(native_workspace, count, duplicate, language):
    async def run():
        service = DiscoveryService()
        store = SQLiteKernelStore(secret_provider=lambda: "batch-native")
        ctx = await context_for(store)
        catalog = ToolCatalog(watchlist_tools())
        pipe = ToolPipeline(catalog=catalog, state_store=store, reference_store=store,
                            effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))
        catalog.register(sandbox.run_code_tool(pipe))
        steps = [{"method": "watchlist.add", "arguments": {"provider": "tmdb", "media_type": "movie",
                  "external_id": "0" if duplicate else str(n)}} for n in range(count)]
        script = ('import mf\nmf.session.batch('+repr(steps)+')' if language == "python" else
                  "mf session.batch --steps '" + json.dumps(steps) + "'")
        with patch.object(config, "get_bool", return_value=True), patch("app.agent.kernel.ports.sdk_effects.get_discovery_service", return_value=service), patch.object(service, "get_detail", side_effect=lambda p,m,i: MediaCard(p,i,m,"标题-"+i)):
            blocked = await pipe.execute("agent.run_code", {"code": script, "language": language}, context=replace(ctx, read_only=True))
            assert "read_scope_required" in blocked.outcome.model_content
            assert await pipe.get_pending_plan(owner=ctx.owner, session_id=ctx.session_id) is None
            preview = await pipe.execute("agent.run_code", {"code": script, "language": language}, context=ctx)
            plan = preview.effect_plan
            assert plan is not None and plan.tool_name == "session.batch"
            assert len(plan.arguments["steps"]) == count
            assert DiscoveryService.watchlist_statistics()["total"] == 0
            cold = SQLiteKernelStore(secret_provider=lambda: "batch-native")
            executor = ToolPipeline(catalog=ToolCatalog(watchlist_tools()), state_store=cold, reference_store=cold,
                                    effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))
            restored = await executor.get_pending_plan(owner=ctx.owner, session_id=ctx.session_id)
            assert restored is not None and restored.arguments == plan.arguments
            result = await executor.execute_confirmed(plan.plan_id, context=ctx)
            data = result.outcome.public_content["data"]
            assert data["total"] == count and len(data["steps"]) == count
            expected = 1 if duplicate else count
            assert data["completed"] == expected
            assert DiscoveryService.watchlist_statistics()["total"] == expected
            if duplicate:
                assert data["steps"][1]["result"]["status"] == "conflict"
                assert all(step["result"]["status"] == "not_started" for step in data["steps"][2:])
    with isolated_test_database():
        asyncio.run(run())
