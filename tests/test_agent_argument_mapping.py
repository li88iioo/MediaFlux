"""Root Mapping compatibility without widening nested JSON admission."""
from __future__ import annotations
from typing import Any
from collections.abc import Mapping
from typing import cast

import asyncio
from collections import UserDict
from copy import deepcopy
import json
from types import MappingProxyType, SimpleNamespace

import pytest

from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from tests.support import isolated_test_database
from tests.test_agent_argument_fidelity import declaration, text_reply
from tests.test_agent_capacity_recovery import (
    PROTOCOLS, collect, make_wire, reopened, session,
)
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414


class MappingModel:
    def __init__(self, calls):
        self.calls = calls
        self.rounds = 0

    async def stream(self, request, *, cancellation):
        self.rounds += 1
        if self.rounds == 1:
            for call in self.calls:
                yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=call)
            yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")
        else:
            yield ModelEvent(ModelEventType.TEXT_DELTA, text="已核对")
            yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")


def assert_mapping_wire(protocol, payload, expected):
    # Injected ModelAdapter has no provider_state: IDs may be normalized.
    # Assert semantic arguments and actual call/result pairing, not raw IDs.
    if protocol == "chat_completions":
        rows = payload["messages"]
        calls = [(c["id"], c["function"]["name"], json.loads(c["function"]["arguments"]))
                 for row in rows for c in row.get("tool_calls", [])]
        results = [(row["tool_call_id"], row["content"])
                   for row in rows if row["role"] == "tool"]
    elif protocol == "responses":
        rows = payload["input"]
        calls = [(row["call_id"], row["name"], json.loads(row["arguments"]))
                 for row in rows if row.get("type") == "function_call"]
        results = [(row["call_id"], row["output"])
                   for row in rows if row.get("type") == "function_call_output"]
    else:
        blocks = [block for row in payload["messages"]
                  if isinstance(row.get("content"), list) for block in row["content"]]
        calls = [(block["id"], block["name"], block["input"])
                 for block in blocks if block.get("type") == "tool_use"]
        results = [(block["tool_use_id"], block["content"])
                   for block in blocks if block.get("type") == "tool_result"]
    assert [(name, args) for _, name, args in calls] == [("inspect_args", expected)]
    assert [cid for cid, _ in results] == [cid for cid, _, _ in calls]
    assert len(results) == 1
    assert "MAPPING_FACT" in json.dumps(results[0][1], ensure_ascii=False)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("factory", [dict, MappingProxyType, UserDict])
def test_public_root_mapping_survives_execution_cold_and_wire(agent_llm_enabled, protocol, factory):
    expected = {"must_match": ["动画"], "exclude_played": True,
                "zero": 0, "optional": None, "flag": False}

    async def exercise():
        handled, started = [], []

        async def handler(args, context):
            handled.append(deepcopy(args))
            started.append(await reopened())
            return {"data": {"fact": "MAPPING_FACT"}}

        catalog = ToolCatalog([declaration(handler)])
        model = MappingModel([ModelToolCall("map", "inspect_args", factory(expected))])
        events = await collect(session(catalog, SimpleNamespace(adapter=model)))
        assert handled == [expected]
        assert events[-1].payload["status"] == "success"
        cold = await reopened()
        assert len(started) == 1
        for state in [*started, cold]:
            assert [call["arguments"] for row in state.conversation
                    for call in row.get("tool_calls", [])] == [expected]
        assert "MAPPING_FACT" in repr(cold.conversation)
        follow = make_wire(protocol, [text_reply(protocol)])
        follow_events = await collect(session(catalog, follow), "mapping-follow")
        assert follow_events[-1].payload["status"] == "success"
        assert len(follow.requests) == 1
        assert_mapping_wire(protocol, follow.requests[0], expected)
        assert handled == [expected]

    with isolated_test_database():
        asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["root-list", "root-tuple", "nested-proxy",
                                   "nested-userdict", "nested-dto", "nested-tuple"])
def test_non_json_mapping_neighbors_reject_entire_batch(kind):
    conversions = []

    class DTO:
        def to_dict(self):
            conversions.append(True)
            return {"safe": "leaf"}

    args = {
        "root-list": [("safe", "leaf")],
        "root-tuple": (("safe", "leaf"),),
        "nested-proxy": {"nested": MappingProxyType({"safe": "leaf"})},
        "nested-userdict": {"nested": UserDict({"safe": "leaf"})},
        "nested-dto": {"nested": DTO()},
        "nested-tuple": {"nested": ("safe", "leaf")},
    }[kind]

    async def exercise():
        handled = []
        catalog = ToolCatalog([declaration(lambda a, c: handled.append(a) or {"data": {}})])
        model = MappingModel([
            ModelToolCall("good", "inspect_args", {"safe": "prefix"}),
            ModelToolCall("bad", "inspect_args", cast(Mapping[str, Any] | None, args)),
        ])
        events = await collect(session(catalog, SimpleNamespace(adapter=model)))
        assert handled == []
        assert conversions == []
        assert events[-1].payload.get("code") == "invalid_arguments"
        assert not any(row.get("tool_calls") for row in (await reopened()).conversation)

    with isolated_test_database():
        asyncio.run(exercise())
