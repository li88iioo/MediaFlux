"""Audit retention is independent of actual public event consumers."""
import asyncio
import copy
import json
from dataclasses import replace

import pytest

from app.agent.kernel.adapters import TurnViewBuilder, iter_ndjson
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput
from app.agent.models import ToolResult
from tests.test_agent_recovered_progress import reply
from tests.test_agent_returned_limit_progress import A, X, scenario, store  # noqa: F401
from tests.test_agent_tool_argument_ingress import Wire


@pytest.mark.parametrize("private", [False, True])
def test_session_cold_audit_and_public_consumers(store, private):  # noqa: F811 - imported pytest fixture injection
    async def exercise():
        detail = "/srv/private/audit-file" if private else "safe-detail"
        value = ToolResult(True, "success", detail, data={**({"internal_id": "private-row"} if private else {}), "title": "safe-title"})
        _, _, _, events = await scenario(store, [(A, X, value)])
        fresh = SQLiteKernelStore(secret_provider=lambda: "offline-returned-limit")
        cold = await fresh.list_events(owner="owner-a", session_id="returned-limit")
        completed = next(event for event in events if event.type is AgentEventType.TOOL_COMPLETED)
        assert completed.payload["result"] == value.to_dict()
        assert next(row for row in cold if row["type"] == "tool.completed")["payload"]["result"] == value.to_dict()
        before = copy.deepcopy([event.to_dict() for event in events])
        async def stream():
            for event in events:
                yield event
        public = b"".join([line async for line in iter_ndjson(stream())]).decode()
        assert "safe-title" in public
        if private:
            assert detail not in public and "private-row" not in public
        else:
            assert detail in public
        builder = TurnViewBuilder()
        for event in events:
            builder.apply(event)
        assert "/srv/private" not in json.dumps(builder.build().to_dict())
        assert [event.to_dict() for event in events] == before == cold
    asyncio.run(exercise())


def test_credentials_redacted_in_cold_audit_and_all_result_views(store):  # noqa: F811 - imported pytest fixture injection
    async def exercise():
        raw = {"ok": True, "status": "success", "summary": "safe-summary", "data": {
            "title": "safe-title", "path": "/srv/private/audit-file", "internal_id": "private-row",
            "token": "synthetic-audit-secret",
        }}
        catalog = ToolCatalog([KernelToolSpec(name=A, domain="library", description="read",
            effect=ToolEffect.READ, input_schema={"type": "object", "properties": {}}, read=lambda *_: raw)])
        wire = Wire("chat_completions", [reply("chat_completions", name=catalog.get(A).model_name,
            arguments={}, index=0), reply("chat_completions", text="safe-answer")])
        session = AgentSession(model=wire.adapter, catalog=catalog,

            pipeline=ToolPipeline(catalog=catalog, state_store=store), state_store=store, journal=store)
        events = [event async for event in session.run(AgentInput(message="read", owner="owner-a", session_id="audit-secret"))]
        wire.verify(posts=2)
        cold = await SQLiteKernelStore(secret_provider=lambda: "offline-returned-limit").list_events(owner="owner-a", session_id="audit-secret")
        completed = next(event for event in events if event.type is AgentEventType.TOOL_COMPLETED)
        audit = completed.payload["result"]
        assert audit["data"] == {"title": "safe-title", "path": "/srv/private/audit-file", "internal_id": "private-row", "token": "[已隐藏]"}
        assert "synthetic-audit-secret" not in json.dumps(cold)
        assert "synthetic-audit-secret" not in json.dumps(wire.requests)
        before = copy.deepcopy(completed.to_dict())
        # Replay the real retained result through each concrete result consumer.
        for kind in (AgentEventType.TOOL_COMPLETED, AgentEventType.EFFECT_APPROVAL_REQUIRED,
                     AgentEventType.EFFECT_COMPLETED, AgentEventType.EFFECT_FAILED):
            result = {**audit, "candidate_view": {"title": "safe-title", "path": "/srv/private/audit-file"}}
            event = replace(completed, type=kind, payload={"result": result,
                "plan": {"plan_id": "opaque-plan", "preview": {"summary": "safe-preview"}}, "call_id": "/opaque/native/id"})
            builder = TurnViewBuilder()
            builder.apply(event)
            view = builder.build().to_dict()
            encoded = json.dumps(view)
            assert "safe-title" in encoded
            assert "/srv/private" not in encoded and "private-row" not in encoded
            async def stream():
                yield event
            line = json.loads(b"".join([line async for line in iter_ndjson(stream())]))
            assert line["payload"]["call_id"] == "/opaque/native/id"
            assert "/srv/private" not in json.dumps(line) and "private-row" not in json.dumps(line)
        assert completed.to_dict() == before
        assert cold == [event.to_dict() for event in events]
        assert raw["data"]["token"] == "synthetic-audit-secret"
    asyncio.run(exercise())
