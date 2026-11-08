"""Behavioral controls for R0; these pass before the deduplication.

capture_session_contract is also used by the offline, fixed-base differential
probe. No test constructs a second runner or requires Git/history at runtime.
"""
from __future__ import annotations
from app.agent.kernel.state_ports import SessionStateStore
from typing import cast

import asyncio
import json
import time
from copy import deepcopy
from dataclasses import asdict
from itertools import count
from typing import Any
from unittest.mock import patch

import pytest

from app.agent.kernel.ports.resource_presentation import ResourceSelection
from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.session_state import SessionState
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import AgentInput, StateUpdate
from tests.test_agent_kernel_core import ScriptedModel

SESSION_CASES = (
    "read_alias", "unavailable", "invalid_alias", "pipeline_failure",
    "approval_siblings", "over_budget", "selection",
)


def _normalize(value: Any) -> Any:
    """Ignore only wall-clock/duration values, never field presence or order."""
    if isinstance(value, dict):
        return {
            key: (f"<{type(child).__name__}>" if key in {"elapsed_ms", "expires_at"}
                  else _normalize(child))
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize(child) for child in value]
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, str) and value.startswith("{"):
        try:
            return _normalize(json.loads(value))
        except ValueError:
            pass
    return value


async def capture_session_contract(case):
    timeline: list[list[object]] = []
    executions = []

    class RecordingStore(InMemorySessionStateStore):
        async def commit(self, lease, *, conversation=None, updates=(), task_patch=None):
            result = await super().commit(lease, conversation=conversation, updates=updates, task_patch=task_patch)
            timeline.append(["commit", asdict(result)])
            return result

    class Journal:
        async def append_for_turn(self, event, lease):
            await self.append(event, owner=lease.owner)

        async def append(self, event, *, owner):
            current = await store.load(owner=owner, session_id=event.session_id)
            timeline.append([event.type.value, deepcopy(dict(event.payload)),
                             deepcopy(current.conversation)])

    def read(arguments, context):
        executions.append(["read", dict(arguments)])
        if case == "pipeline_failure":
            raise ToolPipelineError("查询失败", code="lookup_failed")
        return {"ok": True, "status": "success", "summary": "读取完成", "data": {"count": 3}}

    def prepare(arguments, context):
        executions.append(["prepare", dict(arguments)])
        return PreparedEffect(
            preview={"summary": "准备写入"}, snapshot_fingerprint="r0-snapshot",
            metadata={"purpose": "compatibility"},
        )

    def execute(arguments, snapshot, context):
        executions.append(["execute", dict(arguments)])
        raise AssertionError("No scenario authorizes writes")

    schema = {"type": "object", "properties": {"query": {"type": "string"}},
              "required": ["query"], "additionalProperties": False}
    reader = KernelToolSpec(
        name="library.inspect", model_name="inspect_alias", domain="library",
        description="读取状态", input_schema=schema, effect=ToolEffect.READ, read=read,
    )
    writer = KernelToolSpec(
        name="resources.download", domain="resources", description="准备写入",
        input_schema={"type": "object", "required": ["result_ids", "target"], "properties": {"result_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}}, "target": {"type": "string", "enum": ["guangya"]}}, "additionalProperties": False}, effect=ToolEffect.WRITE,
        prepare=prepare, execute_confirmed=execute,
    )
    catalog = ToolCatalog([reader, writer], selection=ResourceSelection())
    store = RecordingStore()
    calls = [ModelToolCall("call-1", "inspect_alias", {"query": "fixture"})]
    if case == "unavailable":
        calls = [ModelToolCall("call-1", "not_selected", {})]
    elif case == "invalid_alias":
        calls = [ModelToolCall("call-1", "inspect_alias", {})]
    elif case in {"approval_siblings", "over_budget"}:
        calls = [ModelToolCall("call-1", "resources__download", {"result_ids": ["fixture-result-1"], "target": "guangya"}),
                 ModelToolCall("call-2", "inspect_alias", {"query": "fixture"})]
    model = ScriptedModel([
        [*[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=call) for call in calls],
         ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
        [ModelEvent(ModelEventType.TEXT_DELTA, text="已核对。"),
         ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
    ])
    pipeline = ToolPipeline(catalog=catalog, state_store=cast(SessionStateStore, store))
    session = AgentSession(
        model=model, catalog=catalog, pipeline=pipeline, state_store=cast(SessionStateStore, store),
        journal=Journal(),  system_prompt="R0 compatibility fixture",
        limits=SessionLimits(max_model_rounds=2, max_tool_calls=1 if case == "over_budget" else 4),
    )
    metadata = {}
    serial = count(1)
    with patch("secrets.token_urlsafe", side_effect=lambda *args: f"fixture_{next(serial):024d}"):
        if case == "selection":
            from tests.resource_presentation_test_harness import safe_resource_snapshot
            from app.agent.models import ToolResult
            from app.agent.domains.resources.selection import RESOURCE_KIND, SELECTION_KIND
            snapshot = safe_resource_snapshot(ToolResult(True, "found", "候选", data={"items": [{
                "result_id": "fixture-result-1", "title": "fixture.mkv", "download_state": "ready",
                "download_kinds": ["magnet"],
            }]}), search_id="rs_fixture_search_123456")
            assert len(snapshot["candidates"]) == 1
            reference = await pipeline.reference_store.put(owner="owner", session_id="session",
                kind=RESOURCE_KIND, value=snapshot, ttl_seconds=900)
            view = {"generation": 1, "ref": reference.ref, "expires_at": time.time() + 900,
                    "items": snapshot["candidates"]}
            selection = await pipeline.reference_store.put(owner="owner", session_id="session",
                kind=SELECTION_KIND, value={key: view[key] for key in ("ref", "generation", "expires_at")},
                ttl_seconds=900)
            view["selection_ref"] = selection.ref
            lease, _ = await store.begin_turn(owner="owner", session_id="session", request_id="seed")
            await store.commit(lease, updates=(StateUpdate("metadata.ux_candidate_view", view),))
            timeline.clear()
            metadata["selection"] = {"ref": selection.ref, "positions": [1], "target": "guangya"}
        events = [event async for event in session.run(AgentInput(
            message="读取状态并准备写入", owner="owner", session_id="session", request_id="r0",
            metadata=metadata,
        ))]
    state = await store.load(owner="owner", session_id="session")
    return _normalize({
        "events": [{"type": event.type.value, "sequence": event.sequence,
                    "payload": dict(event.payload)} for event in events],
        "requests": [asdict(request) for request in model.requests],
        "state": asdict(state), "timeline": timeline, "executions": executions,
    })


def _assert_paired(messages):
    calls = [call["call_id"] for row in messages for call in row.get("tool_calls", [])]
    results = [row["tool_call_id"] for row in messages if row["role"] == "tool"]
    assert results == calls
    assert len(set(results)) == len(results)


@pytest.mark.parametrize("case", SESSION_CASES)
def test_session_closure_preserves_protocol_events_and_persistence(case):
    result = asyncio.run(capture_session_contract(case))
    events = result["events"]
    tool_events = [event for event in events if event["type"].startswith(("tool.", "effect."))]
    expected = {
        "read_alias": ["tool.started", "tool.completed"],
        "unavailable": ["tool.failed"],
        "invalid_alias": ["tool.started", "tool.failed"],
        "pipeline_failure": ["tool.started", "tool.failed"],
        "approval_siblings": ["effect.preview_started", "tool.started", "effect.approval_required", "tool.failed"],
        "over_budget": ["tool.failed", "tool.failed"],
        "selection": ["tool.started", "effect.approval_required"],
    }
    assert [event["type"] for event in tool_events] == expected[case]
    assert events[-1]["type"] in {"turn.completed", "turn.failed"}
    assert [item[0] for item in result["executions"]] == {
        "read_alias": ["read"], "pipeline_failure": ["read"],
        "approval_siblings": ["prepare"], "selection": ["prepare"],
    }.get(case, [])
    _assert_paired(result["state"]["conversation"])
    for request in result["requests"]:
        _assert_paired(request["messages"])
    for entry in result["timeline"]:
        _assert_paired(entry[1]["conversation"] if entry[0] == "commit" else entry[2])
    for event in tool_events:
        payload = event["payload"]
        assert {"call_id", "tool", "label"} <= payload.keys()
        if event["type"] == "effect.approval_required":
            assert "plan" in payload and "elapsed_ms" not in payload
        if event["type"] == "tool.completed":
            assert "elapsed_ms" in payload and "plan" not in payload
            published = next(row for row in result["timeline"] if row[0] == "tool.completed")
            assert any(row.get("tool_call_id") == payload["call_id"] for row in published[2])
    if case == "read_alias":
        assert all(event["payload"]["tool"] == "library.inspect" for event in tool_events)
        assert result["requests"][1]["messages"][-1]["tool_name"] == "inspect_alias"
    if case == "invalid_alias":
        payload = result["requests"][1]["messages"][-1]["content"]
        assert payload["expected_input_schema"]["required"] == ["query"]
        assert tool_events[-1]["payload"]["tool"] == "library.inspect"
        assert "expected_input_schema" not in tool_events[-1]["payload"]
    if case == "approval_siblings":
        assert tool_events[-1]["payload"]["code"] == "not_executed_after_approval"
        assert tool_events[-1]["payload"]["tool"] == "inspect_alias"
    if case == "over_budget":
        assert {event["payload"]["code"] for event in tool_events} == {"tool_budget_exceeded"}
        assert events[-1]["payload"]["tool_calls"] == 0
    if case == "selection":
        assert result["executions"][0][1]["result_ids"] == ["fixture-result-1"]
        assert "source_type" not in result["executions"][0][1]
        assert result["requests"] == []
        assert events[-1]["payload"]["model_calls"] == 0
        assert events[-1]["payload"]["tool_calls"] == 1
        assert events[-1]["payload"]["answer"] == ""




@pytest.mark.parametrize("mutate_clone", [True, False])
def test_state_clone_keeps_all_fields_and_isolates_nested_mutations(mutate_clone):
    original = SessionState(
        owner="owner", session_id="session", generation=7, summary="summary",
        pending_effect_plan_id="plan", recent_refs=["ref_a"], ref_kinds={"resource"},
        conversation=[{"role": "tool", "content": "fact", "nested": {"items": [1]}}],
        metadata={"nested": {"items": [{"values": {"old"}}]}},
    )
    baseline = deepcopy(original)
    cloned = original.clone()
    assert cloned == original and cloned is not original
    changed, untouched = (cloned, original) if mutate_clone else (original, cloned)
    changed.conversation[0]["nested"]["items"].append(2)
    changed.metadata["nested"]["items"][0]["values"].add("new")
    changed.recent_refs.append("ref_b")
    changed.ref_kinds.add("result")
    changed.generation += 1
    changed.pending_effect_plan_id = "new-plan"
    assert untouched == baseline
    assert changed != baseline
