"""Trusted receipts, not private layout copies, own ordinary assistant text."""

from __future__ import annotations
from typing import Callable
from app.clients.http import FixedHostHttpClient
from typing import cast
from typing import Any

import json

import pytest

from app.agent.kernel.adapters import consume_events
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.provider_model import (
    OpenAICompatibleModelAdapter,
    ProviderSettings,
)
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput
from app.agent.public_view import (
    format_public_result,
    public_conversation_messages,
    sanitize_confirmed_answer,
)
from tests import test_agent_provider_state as fixtures
from tests.test_agent_provider_state import REDACTED, SIGNATURE, THINKING, WireClient

FALSE_CLAIM = "文件已全部归档完成，可以删除源文件。"


@pytest.mark.parametrize("protocol", ["responses", "chat_completions", "anthropic_messages"])
def test_completion_receipt_is_input_not_an_assistant_prefill(protocol):
    from app.agent.kernel.model import ModelMessage, ModelToolCall
    from app.agent.kernel.provider_model import _history_for_protocol

    result = '{"ok":true,"status":"completed","summary":"移动并复制完成"}'
    messages = [
        ModelMessage("user", "移动并复制后重新核验"),
        ModelMessage("assistant", tool_calls=(ModelToolCall("write", "cloud.change", {}),)),
        ModelMessage("tool", result, tool_call_id="write", tool_name="cloud.change"),
        ModelMessage("user", "会删除吗？只解释，不执行"),
        ModelMessage("assistant", "不会删除，目前只是预览，尚未提交。"),
        ModelMessage("assistant", result, completion_receipt_id="confirmed-plan"),
    ]
    original = [row.to_dict() for row in messages]
    sent = _history_for_protocol(protocol, "按真实执行结果继续原任务", messages)
    assert sent[-1]["role"] == "user"
    assert result in str(sent[-1]["content"])
    assert "不是用户新指令或新增写入授权" in str(sent[-1]["content"])
    assert [row.to_dict() for row in messages] == original
    assert str(sent).count("cloud.change") == 1  # 没有新造调用或重放写工具。
    # 模型模仿回执文案不等于运行时回执，不凭正文升格输入身份。
    unmarked = _history_for_protocol(protocol, "", [ModelMessage("assistant", result)])
    assert unmarked[-1]["role"] == "assistant"


def native_reply(protocol, text="", *, call_name="", private=True, state_tag="") -> dict[str, Any]:
    if protocol == "anthropic_messages":
        blocks = (
            [
                {
                    "type": "thinking",
                    "thinking": THINKING + state_tag,
                    "signature": SIGNATURE + state_tag,
                },
                {"type": "redacted_thinking", "data": REDACTED + state_tag},
            ]
            if private
            else []
        )
        if text:
            blocks.append({"type": "text", "text": text})
        if call_name:
            blocks.append(
                {"type": "tool_use", "id": "submit-1", "name": call_name, "input": {}}
            )
        return {
            "content": blocks,
            "stop_reason": "tool_use" if call_name else "end_turn",
        }
    if protocol == "responses":
        output = (
            [
                {
                    "type": "reasoning",
                    "id": "rs_1" + state_tag,
                    "encrypted_content": REDACTED + state_tag,
                    "summary": [{"type": "summary_text", "text": "Checking the actual result." + state_tag}],
                    "content": [{"type": "reasoning_text", "text": THINKING + state_tag}],
                }
            ]
            if private
            else []
        )
        if text:
            output.append(
                {"type": "message", "content": [{"type": "output_text", "text": text}]}
            )
        if call_name:
            output.append(
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "submit-1",
                    "name": call_name,
                    "arguments": "{}",
                }
            )
        return {"status": "completed", "output": output}
    message = {"content": text}
    if private:
        message["reasoning_content"] = THINKING + state_tag
    if call_name:
        message["tool_calls"] = [
            {
                "id": "submit-1",
                "type": "function",
                "function": {"name": call_name, "arguments": "{}"},
            }
        ]
    return {
        "choices": [
            {"message": message, "finish_reason": "tool_calls" if call_name else "stop"}
        ]
    }


@pytest.mark.parametrize(
    "protocol", ["anthropic_messages", "responses", "chat_completions"]
)
def test_confirmed_receipt_is_authoritative_after_sqlite_restore(protocol):
    import asyncio

    async def scenario():
        fixture = fixtures.ProviderStateSessionTests()
        await fixture.asyncSetUp()
        try:
            executed = []
            receipt = {"ok": True, "status": "submitted", "summary": "请求 #276 已提交"}

            def execute(*_):
                executed.append("submit")
                return dict(receipt)

            write = KernelToolSpec(
                name="cloud.submit",
                domain="cloud",
                description="提交归档任务",
                input_schema={"type": "object", "properties": {}},
                effect=ToolEffect.WRITE,
                prepare=lambda *_: PreparedEffect(
                    preview={"summary": "归档预览"}, snapshot_fingerprint="snapshot"
                ),
                execute_confirmed=execute,
            )
            catalog = ToolCatalog([write])
            wire = WireClient(
                [
                    native_reply(protocol, call_name=write.model_name),
                    native_reply(protocol, FALSE_CLAIM, state_tag="_final"),
                    native_reply(protocol, "请核验后台状态", private=False),
                ]
            )

            def session():
                return AgentSession(
                    model=OpenAICompatibleModelAdapter(
                        ProviderSettings(
                            api_url="https://api.example.com/v1",
                            model="model-one",
                            protocol=protocol,
                        ),
                        client_factory=cast(Callable[..., FixedHostHttpClient], lambda **_: wire),
                    ),
                    catalog=catalog,

                    pipeline=ToolPipeline(catalog=catalog, state_store=fixture.store),
                    state_store=fixture.store,
                    journal=fixture.store,
                )

            active = session()
            preview = await consume_events(
                active.run(
                    AgentInput(
                        message="提交归档任务", owner="owner", session_id="session"
                    )
                )
            )
            assert preview.approval
            assert executed == []  # Native state is not write authorization.
            final = await consume_events(
                active.confirm(
                    owner="owner",
                    session_id="session",
                    plan_id=preview.approval.plan_id,
                )
            )
            assert executed == ["submit"]
            assert final.answer == format_public_result(receipt)
            assert FALSE_CLAIM not in final.answer
            state = await fixture.store.load(owner="owner", session_id="session")
            row = next(
                row for row in reversed(state.conversation) if row.get("provider_state")
            )
            assert row["content"] == final.answer
            assert FALSE_CLAIM not in row["content"]
            private_before = json.loads(row["provider_state"]["layout_json"])
            private_before = [
                b
                for b in private_before
                if b["type"]
                in {"thinking", "redacted_thinking", "reasoning", "reasoning_content"}
            ]

            fixture.store = SQLiteKernelStore(
                secret_provider=lambda: "synthetic-state-secret"
            )
            resumed = session()
            public = await consume_events(
                resumed.run(
                    AgentInput(
                        message="状态怎么样",
                        owner="owner",
                        session_id="session",
                        request_id="next",
                    )
                )
            )
            assert len(wire.requests) == 3
            outgoing = wire.requests[-1]
            assert FALSE_CLAIM not in json.dumps(outgoing, ensure_ascii=False)
            assert final.answer in json.dumps(outgoing, ensure_ascii=False).replace(
                "\\n", "\n"
            )
            history = outgoing["input" if protocol == "responses" else "messages"]
            if protocol == "responses":
                assert all(block in history for block in private_before)
                call = next(
                    item for item in history if item.get("type") == "function_call"
                )
                assert (call["id"], call["call_id"]) == ("fc_1", "submit-1")
                assert any(
                    item.get("type") == "function_call_output"
                    and item["call_id"] == "submit-1"
                    for item in history
                )
            elif protocol == "anthropic_messages":
                blocks = [
                    block
                    for item in history
                    if isinstance(item.get("content"), list)
                    for block in item["content"]
                ]
                assert all(block in blocks for block in private_before)
                assert any(
                    block.get("type") == "tool_use" and block["id"] == "submit-1"
                    for block in blocks
                )
                assert any(
                    block.get("type") == "tool_result"
                    and block["tool_use_id"] == "submit-1"
                    for block in blocks
                )
            else:
                assert any(
                    item.get("reasoning_content") == THINKING + "_final"
                    for item in history
                )
            public_text = (
                repr(public.to_dict())
                + repr(final.to_dict())
                + repr(public_conversation_messages(state.conversation))
            )
            for opaque in (THINKING, SIGNATURE, REDACTED):
                assert opaque not in public_text
            await consume_events(
                active.confirm(
                    owner="owner",
                    session_id="session",
                    plan_id=preview.approval.plan_id,
                )
            )
            assert executed == ["submit"]  # Confirmation remains single-use.
        finally:
            await fixture.asyncTearDown()

    asyncio.run(scenario())


def stream_reply(protocol, reply):
    """Encode complete synthetic native blocks as real SSE lifecycle events."""
    frames = []
    if protocol == "anthropic_messages":
        for index, block in enumerate(reply["content"]):
            frames.extend(
                [
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": block,
                    },
                    {"type": "content_block_stop", "index": index},
                ]
            )
        return frames + [
            {"type": "message_delta", "delta": {"stop_reason": reply["stop_reason"]}},
            {"type": "message_stop"},
        ]
    for index, item in enumerate(reply["output"]):
        if item["type"] == "message":
            for block in item["content"]:
                frames.append(
                    {
                        "type": "response.output_text.delta",
                        "output_index": index,
                        "delta": block["text"],
                    }
                )
        frames.append(
            {"type": "response.output_item.done", "output_index": index, "item": item}
        )
    return frames + [
        {"type": "response.completed", "response": {"status": "completed"}}
    ]


@pytest.mark.parametrize("protocol", ["anthropic_messages", "responses"])
@pytest.mark.parametrize("streamed", [False, True])
def test_unchanged_multiblock_tool_order_survives_sqlite_restore(protocol, streamed):
    import asyncio

    async def scenario():
        fixture = fixtures.ProviderStateSessionTests()
        await fixture.asyncSetUp()
        try:
            first = native_reply(protocol, " \n读取前", call_name="inspect")
            final = native_reply(protocol, "已核实", state_tag="_final")
            if protocol == "anthropic_messages":
                first["content"].append({"type": "text", "text": "读取后"})
                final["content"].append({"type": "text", "text": "，无需修改"})
                expected_tool, expected_final = first["content"], final["content"]
            else:
                first["output"].append(
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "读取后"}],
                    }
                )
                final["output"].append(
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "，无需修改"}],
                    }
                )

                def projected(items):
                    return [
                        {"role": "assistant", "content": item["content"]}
                        if item["type"] == "message"
                        else item
                        for item in items
                    ]

                expected_tool, expected_final = (
                    projected(first["output"]),
                    projected(final["output"]),
                )
            wire = WireClient(
                [
                    stream_reply(protocol, first) if streamed else first,
                    stream_reply(protocol, final) if streamed else final,
                    native_reply(protocol, "完成核验", private=False),
                ]
            )
            events = await fixture.run_session(fixture.session(wire, protocol))
            assert fixture.handled == ["inspect"]
            fixture.store = SQLiteKernelStore(
                secret_provider=lambda: "synthetic-state-secret"
            )
            events += await fixture.run_session(
                fixture.session(wire, protocol), "restored"
            )
            assert len(wire.requests) == 3
            if protocol == "anthropic_messages":
                assistants = [
                    row["content"]
                    for row in wire.requests[-1]["messages"]
                    if row["role"] == "assistant"
                ]
                assert assistants == [expected_tool, expected_final]
            else:
                history = wire.requests[-1]["input"]
                assert history[2 : 2 + len(expected_tool)] == expected_tool
                assert history[3 + len(expected_tool) : -1] == expected_final
            for opaque in (THINKING, SIGNATURE, REDACTED):
                assert opaque not in repr(events)
        finally:
            await fixture.asyncTearDown()

    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", ["anthropic_messages", "responses"])
@pytest.mark.parametrize("trusted", ["", "[已脱敏]"])
@pytest.mark.parametrize("original", [["obsolete-a", "obsolete-b"], []])
def test_final_text_projection_never_uses_a_stale_slot(protocol, trusted, original):
    from app.agent.kernel.model import ModelMessage, ProviderState
    from app.agent.kernel.provider_model import _history_for_protocol

    private = native_reply(protocol)[
        "content" if protocol == "anthropic_messages" else "output"
    ]
    layout = (
        [private[0]]
        + [{"type": "text", "text": text} for text in original]
        + private[1:]
    )
    state = ProviderState(protocol, "one", "https://example.com", json.dumps(layout))
    message = ModelMessage("assistant", trusted, provider_state=state)
    sent = _history_for_protocol(
        protocol, "", [message], model="one", endpoint=state.endpoint
    )
    blocks = (
        sent[-1]["content"]
        if protocol == "anthropic_messages"
        else [
            block
            for row in sent
            if row.get("role") == "assistant"
            for block in row["content"]
        ]
    )
    assert [
        block["text"] for block in blocks if block["type"] in {"text", "output_text"}
    ] == ([trusted] if trusted else [])
    assert state.layout() == layout
    for block in private:
        assert block in (blocks if protocol == "anthropic_messages" else sent)


@pytest.mark.parametrize("protocol", ["anthropic_messages", "responses"])
@pytest.mark.parametrize("correction", ["redaction", "submitted", "partial"])
def test_changed_native_tool_text_fails_closed_without_touching_opaque(protocol, correction):
    import asyncio

    async def scenario():
        fixture = fixtures.ProviderStateSessionTests()
        await fixture.asyncSetUp()
        try:
            wire = WireClient(
                [
                    native_reply(protocol, FALSE_CLAIM, call_name="inspect"),
                    native_reply(protocol, "读取完成", private=False),
                    native_reply(protocol, "不应发出此请求", private=False),
                ]
            )
            await fixture.run_session(fixture.session(wire, protocol))
            assert fixture.handled == ["inspect"]
            lease, stored = await fixture.store.begin_turn(
                owner="owner", session_id="session", request_id="redaction"
            )
            row = next(row for row in stored.conversation if row.get("tool_calls"))
            native_before = row["provider_state"]
            if correction == "redaction":
                row["content"] = "[已脱敏]"
            else:
                # Exercise the real receipt correction at the stored tool-turn
                # boundary, not an invented model continuation after confirmation.
                receipt = {"ok": True, "status": correction, "summary": "请核验任务回执"}
                row["content"] = sanitize_confirmed_answer(row["content"], receipt)
                assert row["content"] == format_public_result(receipt)
                assert FALSE_CLAIM not in row["content"]
            await fixture.store.commit(lease, conversation=stored.conversation)
            fixture.store = SQLiteKernelStore(
                secret_provider=lambda: "synthetic-state-secret"
            )
            events = await fixture.run_session(
                fixture.session(wire, protocol), "restored"
            )
            assert (
                len(wire.requests) == 2
            )  # No altered native tool group reaches transport.
            assert "model_provider_error" in repr(events)
            assert fixture.handled == ["inspect"]
            stored = await fixture.store.load(owner="owner", session_id="session")
            assert (
                next(
                    row["provider_state"]
                    for row in stored.conversation
                    if row.get("tool_calls")
                )
                == native_before
            )
            for opaque in (THINKING, SIGNATURE, REDACTED):
                assert opaque not in repr(events)
        finally:
            await fixture.asyncTearDown()

    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", ["anthropic_messages", "responses"])
@pytest.mark.parametrize("status", ["submitted", "partial"])
def test_legacy_checkpoint_replays_trusted_receipt_not_layout_copy(protocol, status):
    """Old-format checkpoint from a real adapter, not a hand-built private state.

    Partial confirmation currently short-circuits without calling the model.
    Exercise its trusted correction at the persisted-message boundary instead
    of altering that safety behavior to manufacture a follow-up model call.
    """
    import asyncio

    from app.agent.kernel.model import ModelEventType, ModelRequest
    from app.agent.kernel.state_types import CancellationToken

    async def scenario():
        fixture = fixtures.ProviderStateSessionTests()
        await fixture.asyncSetUp()
        try:
            wire = WireClient(
                [
                    native_reply(protocol, FALSE_CLAIM),
                    native_reply(protocol, "请核验状态", private=False),
                ]
            )
            session = fixture.session(wire, protocol)
            events = [
                event
                async for event in session.model.stream(
                    ModelRequest(system_prompt="", messages=(), tools=()),
                    cancellation=CancellationToken(),
                )
            ]
            state = next(
                event.provider_state
                for event in events
                if event.type is ModelEventType.FINISH
            )
            assert state is not None
            assert FALSE_CLAIM in state.layout_json
            text = "".join(
                event.text
                for event in events
                if event.type is ModelEventType.TEXT_DELTA
            )
            receipt = {
                "ok": True,
                "status": status,
                "summary": "请求 #276 已提交"
                if status == "submitted"
                else "仅归档 1 项，另 1 项失败",
            }
            trusted = sanitize_confirmed_answer(text, receipt)
            assert trusted == format_public_result(receipt)
            assert FALSE_CLAIM not in trusted
            lease, _ = await fixture.store.begin_turn(
                owner="owner", session_id="session", request_id="legacy"
            )
            await fixture.store.commit(
                lease,
                conversation=[
                    {"role": "user", "content": "归档任务"},
                    {
                        "role": "assistant",
                        "content": trusted,
                        "provider_state": state.to_dict(),
                    },
                ],
            )
            fixture.store = SQLiteKernelStore(
                secret_provider=lambda: "synthetic-state-secret"
            )
            stored = await fixture.store.load(owner="owner", session_id="session")
            assert stored.conversation[-1]["provider_state"] == state.to_dict()
            await fixture.run_session(
                fixture.session(wire, protocol), "restored-legacy"
            )
            assert len(wire.requests) == 2
            sent = wire.requests[-1]
            assert FALSE_CLAIM not in json.dumps(sent, ensure_ascii=False)
            assert trusted in json.dumps(sent, ensure_ascii=False).replace("\\n", "\n")
            if protocol == "anthropic_messages":
                blocks = [
                    block
                    for row in sent["messages"]
                    if isinstance(row.get("content"), list)
                    for block in row["content"]
                ]
            else:
                blocks = sent["input"]
            for block in state.layout():
                if block["type"] != "text":
                    assert block in blocks
        finally:
            await fixture.asyncTearDown()

    asyncio.run(scenario())
