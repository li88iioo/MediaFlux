"""One normalization owns public text and native replay, including after restart."""

from __future__ import annotations

import asyncio
import json

from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.public_view import public_conversation_messages
from tests import test_agent_provider_state as fixtures
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414
from tests.test_agent_provider_text_authority import native_reply, stream_reply


def test_literal_closing_tag_survives_private_sse_tool_loop_and_restart(agent_llm_enabled):
    async def scenario():
        fixture = fixtures.ProviderStateSessionTests()
        await fixture.asyncSetUp()
        try:
            text = "匹配结束标签 </think> 的文件"
            first = native_reply("anthropic_messages", text, call_name="inspect")
            wire = fixtures.WireClient(
                [
                    stream_reply("anthropic_messages", first),
                    fixtures.anthropic_answer(),
                    fixtures.anthropic_answer(),
                ]
            )
            events = await fixture.run_session(fixture.session(wire))
            stored = await fixture.store.load(owner="owner", session_id="session")
            row = next(row for row in stored.conversation if row.get("tool_calls"))
            layout = json.loads(row["provider_state"]["layout_json"])
            assert row["content"] == text
            assert "".join(b["text"] for b in layout if b["type"] == "text") == text
            assert len(wire.requests) == 2
            assert events[-1].to_dict()["payload"]["status"] == "success"
            fixture.store = SQLiteKernelStore(
                secret_provider=lambda: "synthetic-state-secret"
            )
            events += await fixture.run_session(fixture.session(wire), "restored")
            assert len(wire.requests) == 3
            assert events[-1].to_dict()["payload"]["status"] == "success"
            assert fixture.handled == ["inspect"]
            for request in wire.requests[1:]:
                assistant = next(
                    r for r in request["messages"] if r["role"] == "assistant"
                )
                assert assistant["content"] == first["content"]
            public = repr(events) + repr(
                public_conversation_messages(stored.conversation)
            )
            for opaque in (fixtures.THINKING, fixtures.SIGNATURE, fixtures.REDACTED):
                assert opaque not in public
        finally:
            await fixture.asyncTearDown()

    asyncio.run(scenario())


def test_empty_private_signature_is_a_provider_error_before_tool_execution(agent_llm_enabled):
    async def scenario():
        fixture = fixtures.ProviderStateSessionTests()
        await fixture.asyncSetUp()
        try:
            frames = fixtures.anthropic_tool_frames()
            for frame in frames:
                if frame.get("delta", {}).get("type") == "signature_delta":
                    frame["delta"]["signature"] = ""
            wire = fixtures.WireClient([frames])
            events = await fixture.run_session(fixture.session(wire))
            assert len(wire.requests) == 1
            assert fixture.handled == []
            for opaque in (fixtures.THINKING, fixtures.SIGNATURE, fixtures.REDACTED):
                assert opaque not in repr(events)
            assert events[-1].to_dict()["payload"]["code"] == "model_provider_error"
        finally:
            await fixture.asyncTearDown()

    asyncio.run(scenario())
