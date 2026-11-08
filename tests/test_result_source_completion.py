"""Complete safe DTO sources survive projection and cold scoped reads."""
import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.projection import DefaultProjector
from app.agent.kernel.session_service import SessionService
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from tests.support import isolated_test_database


class DTO:
    def __init__(self, value):
        self.value = value

    def to_dict(self):
        return self.value


@pytest.mark.parametrize("sequence", [False, True])
def test_complete_dto_cold_reader(sequence):
    async def exercise():
        leaf = {"tail": "last-fact" * 80, "token": "never-store-secret", "path": "/private/media/file.mkv"}
        expected_leaf = {"tail": "last-fact" * 80, "token": "[已隐藏]", "path": "[内部路径已隐藏；请使用返回的 opaque ref]"}
        raw: dict[str, object] | list[object] = dict(leaf)
        expected: dict[str, object] | list[object] = dict(expected_leaf)
        for _ in range(18):
            raw = {"next": raw}
            expected = {"next": expected}
        raw = [raw, "final-item"] if sequence else {"tree": raw}
        if sequence:
            expected = [expected, "final-item"]
            source = {"ok": True, "status": "success", "summary": "工具执行完成", "data": expected}
        else:
            expected = {"tree": expected}
            source = dict(expected, ok=True, status="success", summary="工具执行完成")
        store = SQLiteKernelStore(secret_provider=lambda: "dto-source-test")
        lease, _ = await store.begin_turn(owner="o", session_id="dto", request_id="read")
        context = ToolCallContext(owner="o", session_id="dto", request_id=lease.request_id, turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=AsyncMock())
        calls = []
        def read(*_):
            calls.append(1)
            return DTO(raw)
        pipeline = ToolPipeline(catalog=ToolCatalog([KernelToolSpec(name="inspect_dto", domain="library", description="Inspect", effect=ToolEffect.READ, input_schema={"type": "object", "properties": {}}, read=read)]), state_store=store, reference_store=store)
        result = await pipeline.execute("inspect_dto", {}, context=context)
        assert "[内容过深]" not in result.outcome.model_content
        assert "result_handle" in result.outcome.public_content
        handle = result.outcome.public_content["result_handle"]
        cold_store = SQLiteKernelStore(secret_provider=lambda: "dto-source-test")
        manifest = await cold_store.resolve(handle, owner="o", session_id="dto", expected_kind="tool_result")
        parts = [await cold_store.resolve(p, owner="o", session_id="dto", expected_kind="tool_result_part") for p in manifest["parts"]]
        assert json.loads("".join(parts)) == source
        assert "never-store-secret" not in "".join(parts)
        assert "/private/media" not in json.dumps(result.outcome.public_content)
        cold = ToolPipeline(catalog=ToolCatalog([]), state_store=cold_store, reference_store=cold_store)
        root = await SessionService(cold, context).result(handle)
        assert root["data"] == source
        path = ("/data/0" if sequence else "/tree") + "/next" * 18 + "/tail"
        tail = await SessionService(cold, context).result(handle, path)
        assert tail["data"] == expected_leaf["tail"]
        assert tail["data"][700:800] == expected_leaf["tail"][700:800]
        for wrong in (replace(context, owner="other"), replace(context, session_id="other")):
            with pytest.raises(ToolPipelineError):
                await SessionService(cold, wrong).result(handle)
        assert calls == [1]
    with isolated_test_database():
        asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["unsupported", "broken", "cycle", "depth"])
def test_unrecoverable_source_rejected(kind):
    class Broken:
        def to_dict(self):
            raise RuntimeError("not safe to stringify")
    value: object = object() if kind == "unsupported" else Broken()
    if kind == "cycle":
        value = []
        value.append(value)
    if kind == "depth":
        value = "tail"
        for _ in range(66):
            level: list[object] = [value]
            value = level
    with pytest.raises((ValueError, TypeError)):
        DefaultProjector().project(value)


def test_model_converter_privacy_and_incomplete_coverage():
    from app.agent.models import ToolResult
    value = ToolResult(ok=True, status="success", summary="Inspect", data={"items": ["private-business-detail", "tail"]}, model_data={"items": ["safe-view"]})
    result = DefaultProjector().project(value)
    model = json.loads(result.full_model_content or result.model_content)
    assert model["data"] == {"items": ["safe-view"]}
    assert "private-business-detail" not in (result.full_model_content or result.model_content)
    assert model["source_coverage"]["complete"] is False
    assert model["source_coverage"]["original_total"] is None
    assert model["source_coverage"]["scope"] == "model_projection"
    assert result.public_content["source_coverage"] == model["source_coverage"]


def test_broken_model_converter_does_not_fall_back_to_public():
    class PrivateDTO(DTO):
        def to_model_dict(self):
            raise RuntimeError("private-business-detail")
    with pytest.raises(ValueError, match="模型结果无法安全转换"):
        DefaultProjector().project(PrivateDTO({"data": {"private": "business-detail"}}))


def test_nonstring_keys_and_atomic_budget_are_explicit_rejections():
    with pytest.raises(TypeError):
        DefaultProjector().project({1: "lost", "1": "kept"})
    with pytest.raises(ToolPipelineError) as error:
        DefaultProjector(max_model_chars=2000).project({"status": "x" * 3000})
    assert error.value.code == "context_budget_exceeded"
