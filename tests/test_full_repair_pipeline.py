"""Property-count ingress and stale preview exclusion regressions."""

from typing import cast
from app.agent.kernel.effects import AuthorizationPolicy
import asyncio
from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session_guard import session_scope_guard
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import (
    CancellationToken,
    SessionBusyError,
    StalePublicationError,
)
from app.agent.kernel.tool_calls import ToolCallContext
from tests.support import isolated_test_database
from tests.test_agent_schema_composition import execute




@pytest.mark.parametrize("schema,value,valid", [
    ({"maxProperties": 0}, {}, True),
    ({"maxProperties": 0}, {"a": 1}, False),
    ({"minProperties": 1, "maxProperties": 1}, {}, False),
    ({"minProperties": 1, "maxProperties": 1}, {"a": 1}, True),
    ({"maxProperties": 1}, {"a": 1, "b": 2}, False),
    ({"maxProperties": 0}, "not an object", True),
])
def test_property_count_nested_ingress(schema, value, valid):
    trace = []
    root = {"type": "object", "properties": {"value": schema}}
    arguments = {"value": value}
    if valid:
        assert asyncio.run(execute(root, arguments, trace)).arguments == arguments
        assert trace == [("validate", arguments), ("read", arguments)]
    else:
        with pytest.raises(ToolPipelineError, match=r"arguments\.value") as error:
            asyncio.run(execute(root, arguments, trace))
        assert error.value.code == "invalid_arguments"
        assert trace == []


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("denied", [False, True])
def test_old_preview_never_runs_handler_or_writes(kind, held, denied):
    async def run():
        with isolated_test_database():
            store = (InMemorySessionStateStore() if kind == "memory" else
                     SQLiteKernelStore(secret_provider=lambda: "isolated-pipeline-secret"))
            lease, _ = await store.begin_turn(owner="owner", session_id="preview", request_id="old")

            async def progress(_):
                pass

            context = ToolCallContext(owner=lease.owner, session_id=lease.session_id,
                request_id=lease.request_id, turn_id=lease.turn_id, lease=lease,
                cancellation=CancellationToken(), report_progress=progress)
            prepare = Mock(return_value=PreparedEffect(preview={"summary": "preview"},
                                                      snapshot_fingerprint="stable"))
            confirmed = Mock(return_value={"ok": True})
            tool = KernelToolSpec(name="downloads.submit", domain="downloads", description="test",
                effect=ToolEffect.WRITE, input_schema={"type": "object"},
                prepare=prepare, execute_confirmed=confirmed)
            authorized = []

            class Authorization:
                async def authorize(self, spec, arguments, ctx):
                    authorized.append((spec.name, arguments, ctx.owner, ctx.session_id))
                    if denied:
                        raise ToolPipelineError("denied", code="forbidden")

            pipeline = ToolPipeline(catalog=ToolCatalog([tool]), state_store=store,
                                    authorization=cast(AuthorizationPolicy, Authorization()))
            await store.begin_turn(owner="owner", session_id="preview", request_id="new")
            before = await store.load(owner="owner", session_id="preview")
            error = ToolPipelineError if denied else SessionBusyError if held else StalePublicationError
            scope = session_scope_guard("owner", "preview", kind="effect") if held else nullcontext()
            with scope, pytest.raises(error) as caught:
                await pipeline.execute(tool.name, {}, context=context)
            if denied:
                assert isinstance(caught.value, ToolPipelineError)
                assert caught.value.code == "forbidden"
            assert authorized == [(tool.name, {}, "owner", "preview")]
            prepare.assert_not_called()
            confirmed.assert_not_called()
            assert await store.load(owner="owner", session_id="preview") == before

    asyncio.run(run())
