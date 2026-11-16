"""Preserve each wire's public semantics; private replay consumes the same text."""

from __future__ import annotations
from typing import Any

import asyncio
import copy

import pytest

from app.agent.kernel.model import ModelEventType, ModelRequest
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.provider_model import ModelProviderError
from app.agent.kernel.state_types import CancellationToken
from app.agent.public_view import public_conversation_messages
from tests import test_agent_provider_state as fixtures
from tests.test_agent_provider_text_authority import native_reply
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414


def anthropic_frames(reply, delta_width) -> list[dict[str, Any]]:
    """Full lifecycle; vary text-delta boundaries independently of wire bytes."""
    frames: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_normalization",
                "type": "message",
                "role": "assistant",
                "model": "model-one",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        }
    ]
    for index, original in enumerate(reply["content"]):
        block = dict(original)
        text = block.get("text", "") if block["type"] == "text" else ""
        if delta_width and text:
            block["text"] = ""
        frames.append(
            {"type": "content_block_start", "index": index, "content_block": block}
        )
        if delta_width:
            frames.extend(
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {
                        "type": "text_delta",
                        "text": text[offset : offset + delta_width],
                    },
                }
                for offset in range(0, len(text), delta_width)
            )
        frames.append({"type": "content_block_stop", "index": index})
    return frames + [
        {
            "type": "message_delta",
            "delta": {"stop_reason": reply["stop_reason"], "stop_sequence": None},
            "usage": {"output_tokens": 50},
        },
        {"type": "message_stop"},
    ]


# Explicit legacy public semantics, NOT an oracle that runs the filters under
# test. JSON intentionally removes orphan closes while SSE retains them.
TEXT_CASES = [
    pytest.param(
        ["匹配结束标签 </think> 的文件"],
        ["匹配结束标签 </think> 的文件"],
        ["匹配结束标签  的文件"],
        id="orphan-close",
    ),
    pytest.param(
        ["甲</THINK >乙</think>丙"],
        ["甲</THINK >乙</think>丙"],
        ["甲乙丙"],
        id="repeated-close",
    ),
    pytest.param(
        ["甲<think>x</think>乙<think>y</think>丙"],
        ["甲乙丙"],
        ["甲乙丙"],
        id="repeated-pairs",
    ),
    pytest.param(
        ["前<think>隐一<think>隐二</think>余</think>后"],
        ["前余</think>后"],
        ["前余后"],
        id="nested",
    ),
    pytest.param(["前<think>未闭合"], ["前"], ["前"], id="unclosed"),
    pytest.param(
        ["<thi<think>x</think>nk>尾"],
        ["<think>尾"],
        [""],
        id="filter-output-forms-new-marker",
    ),
    pytest.param(
        [" \t中文 <file> & < 𝛼🙂\n "],
        [" \t中文 <file> & < 𝛼🙂\n "],
        [" \t中文 <file> & < 𝛼🙂\n "],
        id="ordinary-unicode-whitespace",
    ),
    pytest.param(
        ["甲</think> ", "乙<think>隐</think>尾<", "丙<thi", "丁>"],
        ["甲</think> ", "乙尾<", "丙<thi", "丁>"],
        ["甲 ", "乙尾<", "丙<thi", "丁>"],
        id="multiple-native-blocks",
    ),
    pytest.param(
        ["字<thi", "nk>常规</think>尾"],
        ["字<thi", "nk>常规</think>尾"],
        ["字<thi", "nk>常规尾"],
        id="prefix-does-not-cross-native-blocks",
    ),
    pytest.param(
        ["", " \t\n", "后<"],
        ["", " \t\n", "后<"],
        ["", " \t\n", "后<"],
        id="empty-and-whitespace-blocks",
    ),
]


@pytest.mark.parametrize("raw,sse_text,json_text", TEXT_CASES)
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize(
    "mode,delta_width,wire_width",
    [
        ("json", 0, 19),
        ("sse", 0, 4096),
        ("sse", 1, 1),
        ("sse", 3, 7),
        ("sse", 7, 19),
    ],
)
def test_normalization_matrix_tool_loop_and_sqlite_restore(agent_llm_enabled,
    raw,
    sse_text,
    json_text,
    private,
    mode,
    delta_width,
    wire_width,
):
    async def scenario():
        fixture = fixtures.ProviderStateSessionTests()
        await fixture.asyncSetUp()
        try:
            reply = native_reply(
                "anthropic_messages", call_name="inspect", private=private
            )
            # Exercise original block ownership on both sides of a native call.
            call = reply["content"].pop()
            reply["content"].append({"type": "text", "text": raw[0]})
            reply["content"].append(call)
            reply["content"].extend({"type": "text", "text": text} for text in raw[1:])
            original = copy.deepcopy(reply)
            payload = reply if mode == "json" else anthropic_frames(reply, delta_width)
            texts = json_text if mode == "json" else sse_text
            expected = "".join(texts)
            parse_wire = fixtures.WireClient([payload], split=wire_width)
            parsed = [
                event
                async for event in fixture.session(parse_wire).model.stream(
                    ModelRequest(system_prompt="", messages=(), tools=()),
                    cancellation=CancellationToken(),
                )
            ]
            assert (
                "".join(e.text for e in parsed if e.type is ModelEventType.TEXT_DELTA)
                == expected
            )
            state = next(
                e.provider_state for e in parsed if e.type is ModelEventType.FINISH
            )
            if private:
                assert state is not None
                assert [b["text"] for b in state.layout() if b["type"] == "text"] == [
                    t for t in texts if t
                ]
            else:
                assert state is None
            assert reply == original  # JSON normalization must not mutate caller input.

            wire = fixtures.WireClient(
                [
                    payload,
                    fixtures.anthropic_answer(),
                    fixtures.anthropic_answer(),
                ],
                split=wire_width,
            )
            events = await fixture.run_session(fixture.session(wire))
            assert events[-1].to_dict()["payload"]["status"] == "success"
            assert len(wire.requests) == 2
            saved = await fixture.store.load(owner="owner", session_id="session")
            row = next(row for row in saved.conversation if row.get("tool_calls"))
            assert row["content"] == expected.strip()
            if private:
                assert state is not None
                assert row["provider_state"] == state.to_dict()
            else:
                assert not row.get("provider_state")

            fixture.store = SQLiteKernelStore(
                secret_provider=lambda: "synthetic-state-secret"
            )
            events += await fixture.run_session(fixture.session(wire), "restored")
            assert events[-1].to_dict()["payload"]["status"] == "success"
            assert len(wire.requests) == 3
            assert fixture.handled == ["inspect"]
            for request in wire.requests[1:]:
                messages = request["messages"]
                assistant = next(r for r in messages if r["role"] == "assistant")
                blocks = assistant["content"]
                if private:
                    expected_blocks = []
                    text_iter = iter(texts)
                    for block in original["content"]:
                        if block["type"] == "text":
                            text = next(text_iter)
                            if text:
                                expected_blocks.append({"type": "text", "text": text})
                        else:
                            expected_blocks.append(block)
                    assert blocks == expected_blocks
                else:
                    assert (
                        "".join(b["text"] for b in blocks if b["type"] == "text")
                        == expected.strip()
                    )
                    assert not any(
                        b["type"] in {"thinking", "redacted_thinking"} for b in blocks
                    )
                sent_call = next(b for b in blocks if b["type"] == "tool_use")
                assert sent_call["name"] == "inspect" and sent_call["input"] == {}
                assert any(
                    b.get("type") == "tool_result"
                    and b["tool_use_id"] == sent_call["id"]
                    for r in messages
                    if isinstance(r.get("content"), list)
                    for b in r["content"]
                )
            public = repr(events) + repr(
                public_conversation_messages(saved.conversation)
            )
            for opaque in (fixtures.THINKING, fixtures.SIGNATURE, fixtures.REDACTED):
                assert opaque not in public
        finally:
            await fixture.asyncTearDown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,invalid",
    [
        ("signature", "missing"),
        ("signature", ""),
        ("signature", None),
        ("signature", 123),
        ("signature", [fixtures.SIGNATURE]),
        ("signature", {"private": fixtures.SIGNATURE}),
        ("data", "missing"),
        ("data", ""),
        ("data", None),
        ("data", {"private": fixtures.REDACTED}),
        ("thinking", "missing"),
        ("thinking", [fixtures.THINKING]),
    ],
)
@pytest.mark.parametrize("mode", ["json", "sse-start", "sse-delta"])
def test_invalid_private_fields_stay_provider_errors_without_tools(agent_llm_enabled,
    field, invalid, mode, caplog
):
    async def scenario():
        fixture = fixtures.ProviderStateSessionTests()
        await fixture.asyncSetUp()
        try:
            reply = native_reply("anthropic_messages", "读取前", call_name="inspect")
            index = 1 if field == "data" else 0
            block = reply["content"][index]
            if invalid == "missing":
                block.pop(field)
            else:
                block[field] = invalid
            payload = reply if mode == "json" else anthropic_frames(reply, 1)
            if mode == "sse-delta" and field != "data":
                assert isinstance(payload, list)
                # Malformed delta fields must retain the existing early error path.
                block = payload[1]["content_block"]
                block.pop(field, None)
                delta: dict[str, Any] = {"type": field + "_delta"}
                if invalid != "missing":
                    delta[field] = invalid
                payload.insert(
                    2, {"type": "content_block_delta", "index": 0, "delta": delta}
                )
            wire = fixtures.WireClient([payload, payload])
            session = fixture.session(wire)
            with pytest.raises(ModelProviderError) as caught:
                async for _ in session.model.stream(
                    ModelRequest(system_prompt="", messages=(), tools=()),
                    cancellation=CancellationToken(),
                ):
                    pass
            events = await fixture.run_session(session)
            assert len(wire.requests) == 2
            assert fixture.handled == []
            assert events[-1].to_dict()["payload"]["code"] == "model_provider_error"
            saved = await fixture.store.load(owner="owner", session_id="session")
            assert not any(
                r.get("tool_calls") or r.get("provider_state")
                for r in saved.conversation
            )
            public = repr(events) + str(caught.value) + caplog.text
            for opaque in (fixtures.THINKING, fixtures.SIGNATURE, fixtures.REDACTED):
                assert opaque not in public
        finally:
            await fixture.asyncTearDown()

    asyncio.run(scenario())
