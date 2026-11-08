"""Tool-argument ingress contracts through the real HTTP adapter and pipeline."""
from __future__ import annotations
from typing import Any

import asyncio
import json
import socket

import httpx
import pytest

from app import database as db
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.model import ModelEventType, ModelRequest
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.provider_model import (
    ModelProviderError,
    OpenAICompatibleModelAdapter,
    ProviderSettings,
)
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput, CancellationToken
from app.clients.http import FixedHostHttpClient

PROTOCOLS = ("chat_completions", "responses", "anthropic_messages")
MISSING = object()
INVALID = [False, True, 0, 1, [], [1]]
INVALID_IDS = ["false", "true", "zero", "one", "empty-list", "list"]
SAFE = {"yes": True, "no": False, "nil": None, "text": "中文"}


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def field(name, value):
    return {} if value is MISSING else {name: value}


def frames(protocol, initial=MISSING, deltas=(), final=MISSING, *, index=0, done="item") -> list[dict[str, Any] | str]:
    """Full input and increments stay separate; only Responses done is authoritative."""
    cid = f"call-{index}"
    if protocol == "chat_completions":
        items = [{"index": index, "id": cid, "function": {"name": "lookup", **field("arguments", initial)}}]
        items.extend({"index": index, "function": field("arguments", value)} for value in deltas)
        return [{"choices": [{"index": 0, "delta": {"tool_calls": [item]}}]} for item in items] + [
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}, "[DONE]",
        ]
    if protocol == "anthropic_messages":
        blocks: list[dict[str, Any] | str] = [{"type": "content_block_start", "index": index, "content_block": {
            "type": "tool_use", "id": cid, "name": "lookup", **field("input", initial),
        }}]
        blocks.extend({"type": "content_block_delta", "index": index, "delta": {
            "type": "input_json_delta", **field("partial_json", value),
        }} for value in deltas)
        blocks.extend([
            {"type": "content_block_stop", "index": index},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        ])
        return blocks
    item = {"type": "function_call", "id": f"item-{index}", "call_id": cid, "name": "lookup"}
    result: list[dict[str, Any] | str] = [{"type": "response.output_item.added", "output_index": index,
               "item": {**item, **field("arguments", initial)}}]
    result.extend({"type": "response.function_call_arguments.delta", "output_index": index,
                   "item_id": item["id"], **field("delta", value)} for value in deltas)
    if done == "item":
        result.append({"type": "response.output_item.done", "output_index": index,
                       "item": {**item, **field("arguments", final)}})
    elif done == "arguments":
        result.append({"type": "response.function_call_arguments.done", "output_index": index,
                       "item_id": item["id"], **field("arguments", final)})
    result.append({"type": "response.completed", "response": {"status": "completed"}})
    return result


def wire(protocol, mode, value):
    if mode == "sse":
        return event_wire(frames(protocol, value, final=value))
    if protocol == "chat_completions":
        body = {"choices": [{"message": {"tool_calls": [{"type": "function", "id": "call-0",
                "function": {"name": "lookup", **field("arguments", value)}}]}, "finish_reason": "tool_calls"}]}
    elif protocol == "responses":
        body = {"status": "completed", "output": [{"type": "function_call", "id": "item-0",
                "call_id": "call-0", "name": "lookup", **field("arguments", value)}]}
    else:
        body = {"content": [{"type": "tool_use", "id": "call-0", "name": "lookup", **field("input", value)}],
                "stop_reason": "tool_use"}
    return encode(body).encode(), "application/json"


def event_wire(events):
    return "".join(f"data: {e if isinstance(e, str) else encode(e)}\n\n" for e in events).encode(), "text/event-stream"


class Body(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data
        self.closed = False

    async def __aiter__(self):
        for offset in range(0, len(self.data), 257):
            yield self.data[offset:offset + 257]

    async def aclose(self):
        self.closed = True


class Wire:
    def __init__(self, protocol, replies):
        self.replies = list(replies)
        self.requests = []
        self.clients = []
        self.bodies = []
        self.path = dict(zip(PROTOCOLS, ("/v1/chat/completions", "/v1/responses", "/v1/messages")))[protocol]
        self.adapter = OpenAICompatibleModelAdapter(
            ProviderSettings(api_url="https://api.example.com/v1", model="fixture", protocol=protocol),
            client_factory=self.factory,
        )

    async def handler(self, request):
        assert request.url.path == self.path
        self.requests.append(json.loads(request.content))
        assert self.replies, "invalid arguments must not trigger retry or fallback"
        data, content_type = self.replies.pop(0)
        body = Body(data)
        self.bodies.append(body)
        return httpx.Response(200, headers={"content-type": content_type}, stream=body)

    def factory(self, **kwargs):
        client = FixedHostHttpClient(
            **kwargs, transport=httpx.MockTransport(self.handler),
            resolver=lambda host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
        )
        self.clients.append(client)
        return client

    def verify(self, posts=1):
        assert len(self.requests) == posts
        assert self.clients and all(client._client.is_closed for client in self.clients)
        assert self.bodies and all(body.closed for body in self.bodies)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("real sockets and DNS are forbidden")
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    for name in ("getaddrinfo", "create_connection"):
        monkeypatch.setattr(socket, name, deny)


async def observe(protocol, reply):
    transport = Wire(protocol, [reply])
    seen = []
    error = None
    try:
        async for event in transport.adapter.stream(
            ModelRequest(system_prompt="isolated", messages=(), tools=()), cancellation=CancellationToken(),
        ):
            seen.append(event)
    except ModelProviderError as exc:
        error = exc
    transport.verify()
    return seen, error


def assert_rejected(protocol, reply, emitted=()):
    seen, error = asyncio.run(observe(protocol, reply))
    assert isinstance(error, ModelProviderError), "non-object ingress must fail closed"
    assert [e.tool_call.call_id for e in seen if e.tool_call] == list(emitted)
    assert not any(e.type is ModelEventType.FINISH for e in seen)


def assert_accepted(protocol, reply, value):
    seen, error = asyncio.run(observe(protocol, reply))
    assert error is None
    actual_calls = []
    for e in seen:
        if e.tool_call:
            assert e.tool_call.arguments is not None
            actual_calls.append(dict(e.tool_call.arguments))
    assert actual_calls == [value]
    assert seen[-1].type is ModelEventType.FINISH


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("mode", ("sse", "json"))
@pytest.mark.parametrize("value", INVALID, ids=INVALID_IDS)
def test_nonobject_full_input_never_emits_call(protocol, mode, value):
    assert_rejected(protocol, wire(protocol, mode, value))


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("value", INVALID, ids=INVALID_IDS)
@pytest.mark.parametrize("phase", ("initial", "initial-then-object", "delta", "before-object", "after-object", "inside-object"))
def test_invalid_raw_ingress_cannot_be_ignored_or_stringified(protocol, value, phase):
    initial = value if phase.startswith("initial") else "" if protocol != "anthropic_messages" else {}
    deltas = [] if phase == "initial" else [value]
    if phase == "initial-then-object":
        deltas = [encode(SAFE)]
    elif phase == "before-object":
        deltas = [value, encode(SAFE)]
    elif phase == "after-object":
        deltas = [encode(SAFE), value, None, ""]
    elif phase == "inside-object":
        deltas = ['{"raw":', value, "}"]
    assert_rejected(protocol, event_wire(frames(protocol, initial, deltas)))


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("placeholder", [MISSING, None, "", {}], ids=["missing", "null", "empty-string", "empty-object"])
@pytest.mark.parametrize("split", [False, True], ids=["empty-call", "split-object"])
def test_supported_start_placeholders_and_incremental_json(protocol, placeholder, split):
    text = encode(SAFE)
    deltas = [text[:7], MISSING, None, "", text[7:]] if split else []
    assert_accepted(protocol, event_wire(frames(protocol, placeholder, deltas)), SAFE if split else {})


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("mode", ["sse", "json"])
@pytest.mark.parametrize("value", [MISSING, None, "", "{}"], ids=["missing", "null", "empty", "object-json"])
def test_empty_full_inputs_are_compatible(protocol, mode, value):
    # Anthropic native full input is an object; use its legal empty object.
    incoming = {} if protocol == "anthropic_messages" and value == "{}" else value
    assert_accepted(protocol, wire(protocol, mode, incoming), {})


@pytest.mark.parametrize("value", [{}, SAFE])
def test_dictionary_full_input_retains_empty_delta_compatibility(value):
    protocol = "anthropic_messages"
    assert_accepted(protocol, event_wire(frames(protocol, value, [None, "", " "])), value)


@pytest.mark.parametrize("done", ["item", "arguments", "none"])
@pytest.mark.parametrize("placeholder", [MISSING, None, "", "  "], ids=["missing", "null", "empty", "whitespace"])
def test_responses_done_placeholder_falls_back_to_accumulated_json(done, placeholder):
    assert_accepted("responses", event_wire(frames("responses", "", [encode(SAFE)], placeholder, done=done)), SAFE)


@pytest.mark.parametrize("done", ["item", "arguments"])
@pytest.mark.parametrize("final", [{}, SAFE, "{}", encode(SAFE)])
@pytest.mark.parametrize("broken", ["{broken", False, 0, []], ids=["partial", "false", "zero", "list"])
@pytest.mark.parametrize("phase", ["added", "delta"])
def test_responses_authoritative_done_can_replace_invalid_accumulator(done, final, broken, phase):
    expected = json.loads(final) if isinstance(final, str) else final
    initial, deltas = (broken, []) if phase == "added" else ("", [broken])
    assert_accepted("responses", event_wire(frames("responses", initial, deltas, final, done=done)), expected)


@pytest.mark.parametrize("done", ["item", "arguments"])
@pytest.mark.parametrize("value", INVALID, ids=INVALID_IDS)
def test_responses_invalid_done_cannot_reuse_valid_delta(done, value):
    assert_rejected("responses", event_wire(frames("responses", "", [encode(SAFE)], value, done=done)))


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("text", ["false", "true", "null", "0", "[]", "[1]", '"string"'])
def test_json_text_must_decode_to_object_without_keyword_filtering(protocol, text):
    assert_rejected(protocol, event_wire(frames(protocol, "", [text[:1], text[1:]])))


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("bad_first", [False, True])
def test_failure_preserves_only_already_emitted_tool_boundaries(protocol, bad_first):
    valid = {} if protocol == "anthropic_messages" else "{}"
    first = frames(protocol, False if bad_first else valid, index=0)
    second = frames(protocol, valid if bad_first else False, index=1)
    trim = 2 if protocol == "chat_completions" else 2 if protocol == "anthropic_messages" else 1
    expected = [] if bad_first or protocol == "chat_completions" else ["call-0"]
    assert_rejected(protocol, event_wire(first[:-trim] + second), expected)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("mode", ["sse", "json"])
@pytest.mark.parametrize("size", [32768, 32769])
def test_unicode_character_limit_is_shared_across_full_and_streamed_arguments(protocol, mode, size):
    value = {"text": "界" * (size - len(encode({"text": ""})))}
    text = encode(value)
    assert len(text) == size
    assert len(text.encode()) > size
    incoming = value if protocol == "anthropic_messages" else text
    reply = wire(protocol, mode, incoming)
    if mode == "sse" and protocol != "anthropic_messages":
        reply = event_wire(frames(protocol, "", [text[:7000], text[7000:]]))
    if size == 32768:
        assert_accepted(protocol, reply, value)
    else:
        assert_rejected(protocol, reply)


def test_chat_identity_fragments_multi_index_and_choice_filter_are_unchanged():
    events = [
        {"choices": [{"index": 1, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": False}}]}},
                     {"index": 0, "delta": {"tool_calls": [
                         {"index": 0, "id": "first", "function": {"name": "look", "arguments": '{"no":'}},
                         {"index": 1, "id": "second", "function": {"name": "inspect", "arguments": "{"}},
                     ]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 1, "function": {"arguments": "}"}},
            {"index": 0, "function": {"name": "up", "arguments": "false}"}},
        ]}, "finish_reason": "tool_calls"}]}, "[DONE]",
    ]
    seen, error = asyncio.run(observe("chat_completions", event_wire(events)))
    assert error is None
    actual_calls = []
    for e in seen:
        if e.tool_call:
            assert e.tool_call.arguments is not None
            actual_calls.append((e.tool_call.call_id, e.tool_call.name, dict(e.tool_call.arguments)))
    assert actual_calls == [
        ("first", "lookup", {"no": False}), ("second", "inspect", {}),
    ]


@pytest.fixture
def store(tmp_path):
    previous = db.DB_PATH, bool(getattr(db, "_configured_test_mode", False))
    db.configure_database(tmp_path / "arguments.db", test_mode=True)
    db.init_db()
    try:
        yield SQLiteKernelStore(secret_provider=lambda: "offline-tool-argument-test")
    finally:
        db.configure_database(previous[0], test_mode=previous[1])


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("mode", ["sse", "json"])
@pytest.mark.parametrize("value", [False, 0, [], {}], ids=["false", "zero", "list", "valid-empty"])
def test_actual_session_pipeline_never_executes_nonobject(protocol, mode, value, store):
    async def run():
        executed = []
        def handler(arguments, context):
            executed.append(dict(arguments))
            return {"ok": True, "status": "success", "summary": "已核验", "data": {}}
        catalog = ToolCatalog([KernelToolSpec(name="lookup", domain="library", description="只读核验",
            input_schema={"type": "object", "properties": {}}, effect=ToolEffect.READ, read=handler)])
        incoming = "{}" if value == {} and protocol == "chat_completions" else value
        final = {
            "chat_completions": {"choices": [{"message": {"content": "已核验完成"}, "finish_reason": "stop"}]},
            "responses": {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "已核验完成"}]}]},
            "anthropic_messages": {"content": [{"type": "text", "text": "已核验完成"}], "stop_reason": "end_turn"},
        }[protocol]
        transport = Wire(protocol, [wire(protocol, mode, incoming), (encode(final).encode(), "application/json")])
        session = AgentSession(model=transport.adapter, catalog=catalog,

            pipeline=ToolPipeline(catalog=catalog, state_store=store), state_store=store)
        seen = [e async for e in session.run(AgentInput(message="只读核验", owner="owner", session_id="ingress"))]
        saved = await SQLiteKernelStore(secret_provider=lambda: "offline-tool-argument-test").load(
            owner="owner", session_id="ingress",
        )
        if value == {}:
            assert executed == [{}]
            assert seen[-1].payload.get("status") == "success"
            assert saved.conversation[-1]["content"] == "已核验完成"
            transport.verify(2)
        else:
            assert executed == [], "invalid raw type cannot become an empty business call"
            assert seen[-1].payload.get("status") != "success"
            assert seen[-1].payload.get("code") == "model_provider_error"
            assert not any(m.get("role") == "tool" for m in saved.conversation)
            transport.verify()
    asyncio.run(run())
