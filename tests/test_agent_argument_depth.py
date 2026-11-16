"""Deep JSON arguments retain safe intent and results preserve complete safe trees."""
from __future__ import annotations
from typing import Any

import asyncio
from copy import deepcopy
import json

import pytest

from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.projection import _json_safe, _model_safe, DefaultProjector
from tests.support import isolated_test_database
from tests.test_agent_argument_fidelity import declaration, text_reply
from tests.test_agent_capacity_recovery import (
    PROTOCOLS, assert_native_wire, batch, collect, make_wire, reopened, session,
)
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414


def nested(value: object, depth: int, *, lists: bool = False) -> object:
    for index in range(depth):
        node: dict[str, object] = {"nested": [value] if lists and index % 2 else value}
        value = node
    return value


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("shape", ["original", "mixed", "private", "depth-limit"])
def test_deep_arguments_survive_started_cold_and_next_wire(agent_llm_enabled, protocol, shape):
    async def exercise():
        leaf: dict[str, Any] = {"must_match": ["动画"], "exclude_played": True}
        expected_leaf = deepcopy(leaf)
        if shape != "original":
            leaf.update({"zero": 0, "flag": False, "optional": None,
                         "truncated": "business-value", "truncation": ["business"],
                         "read_tool": "reader", "result_handle": "handle"})
            expected_leaf = deepcopy(leaf)
        if shape == "private":
            leaf.update({"password": "DO_NOT_REPLAY_PASSWORD", "internal_id": "PRIVATE_ID",
                         "file": "/srv/private/media", "value": "Bearer abcdefghijklmnop",
                         "calendar_url": "/discovery/calendar",
                         "children": [{"calendar_url": "/srv/private/calendar"}]})
            expected_leaf.update({"password": "[已隐藏]",
                                  "internal_id": "PRIVATE_ID",
                                  "file": "/srv/private/media",
                                  "value": "Bearer ********", "calendar_url": "/discovery/calendar",
                                  "children": [{"calendar_url": "/srv/private/calendar"}]})
        args = nested(leaf, 12, lists=shape == "mixed")
        expected = nested(expected_leaf, 12, lists=shape == "mixed")
        if shape == "depth-limit":
            args = nested("safe leaf", 64)
            expected = deepcopy(args)
        handled, started = [], []

        async def handler(value, context):
            handled.append(deepcopy(value))
            started.append(await reopened())
            return {"data": {"count": 3}}

        catalog = ToolCatalog([declaration(handler)])
        wire = make_wire(protocol, [batch(protocol, [("deep", "inspect_args", args)]), text_reply(protocol)])
        events = await collect(session(catalog, wire))
        assert handled == [args]
        assert events[-1].payload["status"] == "success"
        cold = await reopened()
        for state in [*started, cold]:
            assert [c["arguments"] for row in state.conversation for c in row.get("tool_calls", [])] == [expected]
        follow = make_wire(protocol, [text_reply(protocol)])
        await collect(session(catalog, follow), "cold")
        assert len(follow.requests) == 1
        assert_native_wire(protocol, follow.requests[0], [("deep", "inspect_args", expected)])
        assert handled == [args]
        public = repr(events) + repr(cold.conversation) + json.dumps(follow.requests, ensure_ascii=False)
        for private in ("DO_NOT_REPLAY_PASSWORD", "abcdefghijklmnop"):
            assert private not in public
    with isolated_test_database():
        asyncio.run(exercise())


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_excessive_depth_rejected_before_handler(agent_llm_enabled, protocol):
    async def exercise():
        handled = []
        args = nested({"must_match": ["动画"]}, 80)
        catalog = ToolCatalog([declaration(lambda a, c: handled.append(a) or {"data": {}})])
        wire = make_wire(protocol, [batch(protocol, [("too-deep", "inspect_args", args)]), text_reply(protocol)])
        events = await collect(session(catalog, wire))
        assert handled == []
        assert events[-1].payload.get("code") == "invalid_arguments"
        assert "invalid_arguments" in repr(events)
        assert not any(row.get("tool_calls") for row in (await reopened()).conversation)
    with isolated_test_database():
        asyncio.run(exercise())


def test_result_dto_depth_cycle_and_fallback_contract():
    expected = nested({"safe": "leaf"}, 12)
    assert _json_safe(expected) == expected
    assert _model_safe(expected) == expected

    cycle = {}
    cycle["nested"] = cycle
    for convert in (_json_safe, _model_safe):
        with pytest.raises(ValueError, match="工具结果结构超过安全保存范围"):
            convert(cycle)

    class BrokenDTO:
        def to_dict(self):
            raise ValueError("private detail")

        def __str__(self):
            raise AssertionError("broken DTO must not be stringified")

    with pytest.raises(ValueError, match="工具结果无法转换为完整安全来源") as error:
        _json_safe(BrokenDTO())
    assert "private detail" not in str(error.value)

    result = DefaultProjector().project({"data": expected})
    payload = {"data": expected, "ok": True, "status": "success", "summary": "工具执行完成"}
    assert json.loads(result.model_content) == payload
    assert result.public_content == payload
    assert "[内容过深]" not in result.model_content


@pytest.mark.parametrize("kind", ["cycle", "dto", "tuple", "nonstring-key", "nan", "surrogate", "wide"])
def test_non_json_and_unbounded_arguments_never_execute(kind):
    from types import SimpleNamespace
    from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall

    class DTO:
        def to_dict(self):
            raise AssertionError("argument admission must not execute a DTO")

    args: dict[str, Any] = {"cycle": {}, "dto": {"value": DTO()}, "tuple": {"value": (1, 2)},
            "nonstring-key": {"value": {1: "one"}}, "nan": {"value": float("nan")},
            "surrogate": {"value": "\ud800"}, "wide": {"value": [0] * 262145}}[kind]
    if kind == "cycle":
        args["nested"] = args

    class Model:
        async def stream(self, request, *, cancellation):
            yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                             tool_call=ModelToolCall("unsafe", "inspect_args", args))
            yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")

    async def exercise():
        handled = []
        catalog = ToolCatalog([declaration(lambda a, c: handled.append(True) or {"data": {}})])
        events = await collect(session(catalog, SimpleNamespace(adapter=Model())))
        assert handled == []
        assert events[-1].payload.get("code") == "invalid_arguments"
        assert not any(row.get("tool_calls") for row in (await reopened()).conversation)
    with isolated_test_database():
        asyncio.run(exercise())


@pytest.mark.parametrize("value", [None, False, 0, "safe", [None, False, 0], {"a": [None, False, 0]},
                                  {"password": "secret", "internal_id": "id"},
                                  {"calendar_url": "/discovery/calendar"},
                                  {"calendar_url": "/discovery/calendar?x=1"},
                                  {"rows": [{"file": "C:\\private\\file", "text": "Bearer abcdefghijklmnop"}]}])
def test_ordinary_projection_characterization(value):
    # Independent frozen baseline comparison is captured by the paired runner;
    # these explicit assertions retain the ordinary DTO path's fixed semantics.
    result = _model_safe(value)
    assert "abcdefghijklmnop" not in repr(result)
    assert "C:\\private\\file" not in repr(result)
    if value == {"calendar_url": "/discovery/calendar"}:
        assert result == value
    elif value == {"calendar_url": "/discovery/calendar?x=1"}:
        assert result == {"calendar_url": "[页面地址无效]"}
    elif isinstance(value, dict) and "password" in value:
        assert result == {"password": "[已隐藏]", "internal_id": "[内部标识已隐藏；请使用返回的 opaque ref]"}
    elif not isinstance(value, dict) or "rows" not in value:
        assert result == value
