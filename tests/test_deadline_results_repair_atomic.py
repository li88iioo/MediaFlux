"""Already-projected atomic facts remain native and cold-readable."""
import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.projection import ToolOutcome
from app.agent.kernel.session_service import SessionService
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from tests.support import isolated_test_database


def test_atomic_unknown_metadata_keeps_native_fact_and_cold_source():
    async def exercise():
        source = {"ok": False, "status": "unknown", "code": "UNRESOLVED" * 4000}
        native = json.dumps(source)
        calls = []

        def read(*_):
            calls.append("read")
            return ToolOutcome(model_content=native, public_content={
                "ok": False, "status": "unknown", "summary": "Outcome not known",
            })

        store = SQLiteKernelStore(secret_provider=lambda: "repair-atomic-source")
        lease, _ = await store.begin_turn(owner="owner", session_id="atomic", request_id="observe")
        context = ToolCallContext(owner="owner", session_id="atomic", request_id=lease.request_id,
            turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=AsyncMock())
        catalog = ToolCatalog([KernelToolSpec(name="inspect_atomic", domain="library", description="Inspect",
            effect=ToolEffect.READ, input_schema={"type": "object", "properties": {}}, read=read)])
        pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store)
        result = await pipeline.execute("inspect_atomic", {}, context=context)
        assert result.outcome.model_content == native
        assert result.outcome.public_content["status"] == "unknown"
        assert result.outcome.public_content["ok"] is False
        handle = result.outcome.public_content["result_handle"]
        assert not result.outcome.full_model_content
        reopened = SQLiteKernelStore(secret_provider=lambda: "repair-atomic-source")
        assert reopened.max_state_bytes == 262144
        manifest = await reopened.resolve(handle, owner="owner", session_id="atomic", expected_kind="tool_result")
        parts = [await reopened.resolve(ref, owner="owner", session_id="atomic", expected_kind="tool_result_part")
                 for ref in manifest["parts"]]
        assert json.loads("".join(parts)) == source
        cold = ToolPipeline(catalog=ToolCatalog([]), state_store=reopened, reference_store=reopened)
        content = await SessionService(cold, context).result(handle)
        assert content["result_handle"] == handle
        assert content["data"] == source  # unknown、错误码与原事实全部保留
        assert (await SessionService(cold, context).result(handle, "/code"))["data"][-100:] == source["code"][-100:]
        with pytest.raises(ToolPipelineError) as error:
            await SessionService(cold, replace(context, owner="other")).result(handle, "/code")
        assert error.value.code == "reference_invalid"
        assert calls == ["read"]

    with isolated_test_database():
        asyncio.run(exercise())
