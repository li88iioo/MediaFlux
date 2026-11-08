"""参数越界：只读数量上限按上限执行；定位参数与写入参数照常报错并给出允许范围。"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import AgentInput
from app.agent.schema import normalize_read_arguments, validate_json_schema
from tests.test_agent_kernel_core import ScriptedModel, collect

SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
    "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
    "cast_limit": {"type": "integer", "minimum": 1, "maximum": 20},
    "page": {"type": "integer", "minimum": 1, "maximum": 5},
    "season": {"type": "integer", "minimum": 0, "maximum": 99},
    "mode": {"type": "string", "enum": ["title", "actor"]},
}}


def call(name, arguments, *, call_id="c1"):
    return [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(call_id, name, arguments)),
            ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]


ANSWER = [ModelEvent(ModelEventType.TEXT_DELTA, text="完成"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")]


def run_turn(tool, arguments):
    model = ScriptedModel([call(tool.name, arguments), ANSWER])
    state, catalog = InMemorySessionStateStore(), ToolCatalog([tool])
    session = AgentSession(model=model, catalog=catalog,
        pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state)
    events = asyncio.run(collect(session.run(AgentInput(message="查询", owner="o", session_id="s"))))
    tool_messages = [m for m in model.requests[-1].messages if m.role == "tool"]
    return events, json.loads(tool_messages[0].content)


@pytest.mark.parametrize("arguments,received", [
    ({"limit": 999}, {"limit": 50}),
    ({"max_results": 11, "cast_limit": 500}, {"max_results": 10, "cast_limit": 20}),
    ({"limit": 50}, {"limit": 50}),
    ({"limit": 3, "page": 2}, {"limit": 3, "page": 2}),
    # 真实 gemini 运行（T14）中模型按报错可选值仍写成大写 Movie 后转而穷举。
    ({"mode": "Actor"}, {"mode": "actor"}),
    ({"mode": " TITLE "}, {"mode": "title"}),
])
def test_read_quantity_caps_run_at_the_maximum(arguments, received):
    seen = []
    tool = KernelToolSpec(name="library.search", domain="library", description="搜索", input_schema=SCHEMA,
                          effect=ToolEffect.READ, read=lambda a, c: seen.append(dict(a)) or {"summary": "ok"})
    events, _ = run_turn(tool, arguments)
    assert seen == [received]
    assert events[-1].type.value == "turn.completed"


@pytest.mark.parametrize("arguments,message", [
    ({"page": 9}, "最大为 5"),
    ({"season": 120}, "最大为 99"),
    ({"limit": 0}, "最小为 1"),
    ({"mode": "genre"}, "可选：title、actor"),
])
def test_locators_and_lower_bounds_still_fail_with_allowed_range(arguments, message):
    seen = []
    tool = KernelToolSpec(name="library.search", domain="library", description="搜索", input_schema=SCHEMA,
                          effect=ToolEffect.READ, read=lambda a, c: seen.append(a) or {"summary": "ok"})
    events, result = run_turn(tool, arguments)
    assert seen == []
    assert result["code"] == "invalid_arguments" and message in result["error"]
    assert any(e.type.value == "tool.failed" for e in events)


def test_write_tool_quantities_are_not_clamped():
    prepared = []
    tool = KernelToolSpec(name="library.change", domain="library", description="变更", input_schema=SCHEMA,
        effect=ToolEffect.WRITE,
        prepare=lambda a, c: prepared.append(a) or PreparedEffect(preview={"summary": "预览"}, snapshot_fingerprint="s"),
        execute_confirmed=lambda *_: pytest.fail("未确认不能执行"))
    _, result = run_turn(tool, {"limit": 999})
    assert prepared == []
    assert result["code"] == "invalid_arguments" and "最大为 50" in result["error"]


def test_clamp_only_touches_integer_caps_above_maximum():
    raw = {"limit": True, "max_results": "99", "page": 9, "cast_limit": 5, "unknown_limit": 1000}
    assert normalize_read_arguments(raw, SCHEMA) == raw
    with pytest.raises(ToolPipelineError, match="太长，最多 3 个字符"):
        validate_json_schema("abcd", {"type": "string", "maxLength": 3})


def test_enum_case_folding_is_only_for_unambiguous_read_values():
    schema = {"type": "object", "properties": {"kind": {"type": "string", "enum": ["Movie", "movie", "series"]}}}
    assert normalize_read_arguments({"kind": "MOVIE"}, schema) == {"kind": "MOVIE"}
    assert normalize_read_arguments({"kind": "Series"}, schema) == {"kind": "series"}
    assert normalize_read_arguments({"kind": "movie"}, schema) == {"kind": "movie"}
    assert normalize_read_arguments({"kind": "film"}, schema) == {"kind": "film"}


def test_write_tool_enums_are_not_case_folded():
    prepared = []
    tool = KernelToolSpec(name="library.change", domain="library", description="变更", input_schema=SCHEMA,
        effect=ToolEffect.WRITE,
        prepare=lambda a, c: prepared.append(a) or PreparedEffect(preview={"summary": "预览"}, snapshot_fingerprint="s"),
        execute_confirmed=lambda *_: pytest.fail("未确认不能执行"))
    _, result = run_turn(tool, {"mode": "Actor"})
    assert prepared == [] and result["code"] == "invalid_arguments"


@pytest.mark.parametrize("raw,retry,expected,raises,recovered", [
    ({"media_type": "Movie"}, {"media_type": "movie"}, {"media_type": "movie"}, False, True),
    ({"media_type": " MOVIE "}, {"media_type": "movie"}, {"media_type": "movie"}, True, True),
    ({"limit": 999}, {"limit": 50}, {"limit": 50}, False, True),
    ({"query": "Title", "media_type": "Movie"}, {"query": "title", "media_type": "movie"},
     {"query": "Title", "media_type": "movie"}, False, False),
    ({"media_type": "Movie"}, {"media_type": "tv"}, {"media_type": "movie"}, True, False),
    ({"page": 1, "media_type": "Movie"}, {"page": 2, "media_type": "movie"},
     {"page": 1, "media_type": "movie"}, False, False),
], ids=["enum-result", "enum-exception", "clamped-limit", "different-title", "different-type", "different-page"])
def test_read_recovery_uses_execution_identity_without_rewriting_history(raw, retry, expected, raises, recovered):
    seen = []
    original = [dict(raw), dict(retry)]

    def read(arguments, _context):
        seen.append(dict(arguments))
        if len(seen) == 1:
            if raises:
                raise ToolPipelineError("临时超时", code="provider_timeout")
            return {"ok": False, "status": "provider_timeout", "summary": "临时超时"}
        return {"ok": True, "status": "success", "summary": "查询成功", "data": {"items": [{"title": "已核验的作品"}]}}

    tool = KernelToolSpec(name="library.search", domain="library", description="查询", effect=ToolEffect.READ,
        input_schema={**SCHEMA, "properties": {**SCHEMA["properties"], "query": {"type": "string"},
                                               "media_type": {"type": "string", "enum": ["movie", "tv"]}}}, read=read)
    model = ScriptedModel([call(tool.name, raw), call(tool.name, retry, call_id="retry"), ANSWER])
    state, catalog = InMemorySessionStateStore(), ToolCatalog([tool])
    session = AgentSession(model=model, catalog=catalog,
        pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state)
    events = asyncio.run(collect(session.run(AgentInput(message="查询", owner="o", session_id="s"))))
    saved = asyncio.run(state.load(owner="o", session_id="s"))

    assert seen == [expected, retry]
    assert [c["arguments"] for row in saved.conversation for c in row.get("tool_calls", [])] == original
    tool_messages = [row for row in saved.conversation if row["role"] == "tool"]
    assert len(tool_messages) == 2 and "临时超时" in tool_messages[0]["content"]
    final = events[-1].payload
    assert final["status"] == ("success" if recovered else "partial")
    assert final["answer"] == "完成"
    assert final["finish_reason"] == "stop"
