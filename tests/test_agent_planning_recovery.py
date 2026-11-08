"""#24：真实 Session/native adapter 在完整历史上有界重建截断规划回合。"""

from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest

from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.provider_model import (
    OpenAICompatibleModelAdapter,
    ProviderSettings,
)
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.state_types import AgentInput
from app.clients.http import FixedHostHttpClient
from tests.support import isolated_test_database
from tests.test_agent_provider_state import (
    REDACTED,
    SIGNATURE,
    THINKING,
    anthropic_tool_frames,
)

PROTOCOLS = ("responses", "chat_completions", "anthropic_messages")
OWNER, SESSION = "planning-recovery-owner", "planning-recovery"
DRAFT = "未完成规划残稿_SENTINEL_不能当答案"
ANSWER = "核验完成：目标一的事实已确认。"
SECRET = "planning-recovery-test-secret"


@pytest.fixture(autouse=True)
def isolated_offline(monkeypatch):
    def deny_network(*_args, **_kwargs):
        raise AssertionError("本文件只能使用 MockTransport，禁止真实网络")

    for target, names in (
        (socket.socket, ("connect", "connect_ex", "sendto")),
        (socket, ("create_connection", "getaddrinfo")),
    ):
        for name in names:
            monkeypatch.setattr(target, name, deny_network)
    with isolated_test_database("agent-planning-recovery.db"):
        yield


def sse(*events):
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        content="".join("data: " + json.dumps(e, ensure_ascii=False) + "\n\n" for e in events).encode(),
    )


def truncated_text(protocol):
    if protocol == "responses":
        return sse({"type": "response.output_text.delta", "delta": DRAFT})
    if protocol == "chat_completions":
        return sse({"choices": [{"index": 0, "delta": {"content": DRAFT}}]})
    return sse(
        {"type": "message_start", "message": {"role": "assistant", "content": []}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": DRAFT}},
    )


def reply(protocol, *, text="", call_id="", name="library__inspect", arguments=None):
    """只构造 Provider 原生响应，不替换模型适配器/Session 行为。"""
    arguments = {} if arguments is None else arguments
    if protocol == "responses":
        output = []
        if text:
            output.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
        if call_id:
            output.append({"type": "function_call", "id": "item-" + call_id,
                           "call_id": call_id, "name": name, "arguments": json.dumps(arguments)})
        body = {"status": "completed", "output": output}
    elif protocol == "chat_completions":
        message = {"role": "assistant", "content": text}
        if call_id:
            message["tool_calls"] = [{"id": call_id, "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments)}}]
        body = {"choices": [{"message": message, "finish_reason": "tool_calls" if call_id else "stop"}]}
    else:
        content = [{"type": "text", "text": text}] if text else []
        if call_id:
            content.append({"type": "tool_use", "id": call_id, "name": name, "input": arguments})
        body = {"role": "assistant", "content": content, "stop_reason": "tool_use" if call_id else "end_turn"}
    return httpx.Response(200, json=body)


class NativeWire:
    """真实 HTTP 客户端仅替换传输与 DNS；严格限制脚本请求次数。"""

    def __init__(self, protocol, responses):
        self.protocol = protocol
        self.responses = list(responses)
        self.requests = []
        self.paths = []
        self.model = OpenAICompatibleModelAdapter(
            ProviderSettings(api_url="https://api.example.com/v1", model="offline-fixture", protocol=protocol),
            client_factory=self.client,
        )

    async def handle(self, request):
        self.requests.append(json.loads(request.content))
        self.paths.append(request.url.path)
        assert self.responses, "不应新增脚本之外的 Provider 请求"
        return self.responses.pop(0)

    def client(self, **kwargs):
        return FixedHostHttpClient(
            **kwargs, transport=httpx.MockTransport(self.handle),
            resolver=lambda _host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
        )


def read_tool(reads):
    def inspect(arguments, _context):
        reads.append(arguments["target"])
        return {"ok": True, "status": "ready", "summary": f"目标{arguments['target']}核验完成",
                "data": {"fact": "FACT-" + str(arguments["target"])}}

    return KernelToolSpec(
        name="library.inspect", domain="library", description="只读核验目标状态",
        input_schema={"type": "object", "properties": {"target": {"type": "integer"}},
                      "required": ["target"], "additionalProperties": False},
        effect=ToolEffect.READ, read=inspect,
    )


def make_session(wire, tools, *, limits=None):
    catalog = ToolCatalog(tools)
    store = SQLiteKernelStore(secret_provider=lambda: SECRET)
    return AgentSession(
        model=wire.model, catalog=catalog,
        pipeline=ToolPipeline(catalog=catalog, state_store=store, reference_store=store),
        state_store=store, limits=limits,
    )


async def run_turn(session):
    return [event async for event in session.run(AgentInput(
        owner=OWNER, session_id=SESSION, message="请用 library.inspect 只读核验目标一；必要时继续核验目标二。",
    ))]


def history(protocol, request):
    return request["input" if protocol == "responses" else "messages"]


def phases(events):
    return [e.payload["phase"] for e in events if e.type is AgentEventType.MODEL_STARTED]


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_first_planning_text_truncation_keeps_read_tools_and_completes(protocol):
    async def scenario():
        reads = []
        wire = NativeWire(protocol, [
            truncated_text(protocol),
            reply(protocol, call_id="read-1", arguments={"target": 1}),
            reply(protocol, text=ANSWER),
        ])
        session = make_session(wire, [read_tool(reads)])
        events = await run_turn(session)
        # 优先验证 bug 的直接原因：重建规划回合不能强制变成 tools=()。
        assert wire.requests[1].get("tools") == wire.requests[0].get("tools")
        assert wire.requests[1].get("tools")
        assert reads == [1]
        assert len(wire.requests) == 3 and not wire.responses
        assert phases(events) == ["planning", "answer_recovery", "planning"]
        assert len({e.turn_id for e in events}) == 1
        assert sum(e.type is AgentEventType.TURN_STARTED for e in events) == 1
        assert events[-1].type is AgentEventType.TURN_COMPLETED
        assert events[-1].payload["status"] == "success"
        assert events[-1].payload["answer"] == ANSWER
        assert events[-1].payload["model_calls"] == 3
        assert events[-1].payload["tool_calls"] == 1
        assert "FACT-1" in json.dumps(history(protocol, wire.requests[2]))
        assert DRAFT not in json.dumps(history(protocol, wire.requests[1]), ensure_ascii=False)
        cold = await SQLiteKernelStore(secret_provider=lambda: SECRET).load(owner=OWNER, session_id=SESSION)
        assert DRAFT not in json.dumps(cold.conversation, ensure_ascii=False)
        assert [r["tool_call_id"] for r in cold.conversation if r["role"] == "tool"] == ["read-1"]

    asyncio.run(scenario())


def truncated_call(protocol, *, complete_arguments=False):
    raw = '{"target":1}' if complete_arguments else '{"target":'
    # 先输出规划文字，确保 emitted=True；否则 adapter 的首输出前重试可能
    # 把 Session 的回合恢复缺陷掩盖掉。所有断言仍走真实原生事件链路。
    if protocol == "responses":
        frames = [
            {"type": "response.output_text.delta", "delta": DRAFT},
            {"type": "response.output_item.added", "output_index": 0, "item": {
                "type": "function_call", "id": "item-discard", "call_id": "discard-call",
                "name": "library__inspect", "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "output_index": 0,
             "item_id": "item-discard", "delta": raw},
        ]
        if complete_arguments:
            frames.append({"type": "response.function_call_arguments.done", "output_index": 0,
                           "item_id": "item-discard", "arguments": raw})
        return sse(*frames)
    if protocol == "chat_completions":
        return sse({"choices": [{"index": 0, "delta": {"content": DRAFT, "tool_calls": [
            {"index": 0, "id": "discard-call", "type": "function", "function": {
                "name": "library__inspect", "arguments": raw}},
        ]}}]})
    return sse(
        {"type": "message_start", "message": {"role": "assistant", "content": []}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": DRAFT}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {
            "type": "tool_use", "id": "discard-call", "name": "library__inspect", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": raw}},
    )

@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_partial_native_arguments_are_not_replayed_or_persisted(protocol):
    async def scenario():
        reads = []
        wire = NativeWire(protocol, [
            truncated_call(protocol),
            reply(protocol, call_id="rebuilt-call", arguments={"target": 2}),
            reply(protocol, text="目标二核验完成。"),
        ])
        session = make_session(wire, [read_tool(reads)])
        events = await run_turn(session)
        assert reads == [2]
        assert len(wire.requests) == 3
        assert phases(events) == ["planning", "answer_recovery", "planning"]
        assert events[-1].payload["status"] == "success"
        assert not any(e.type is AgentEventType.MODEL_TOOL_CALL and e.payload["call_id"] == "discard-call" for e in events)
        for request in wire.requests[1:]:
            assert "discard-call" not in json.dumps(history(protocol, request))
            assert DRAFT not in json.dumps(history(protocol, request), ensure_ascii=False)
        cold = await SQLiteKernelStore(secret_provider=lambda: SECRET).load(owner=OWNER, session_id=SESSION)
        calls = [c for row in cold.conversation for c in row.get("tool_calls", ())]
        assert [(c["call_id"], c["arguments"]) for c in calls] == [("rebuilt-call", {"target": 2})]
        assert "discard-call" not in json.dumps(cold.conversation)
        assert DRAFT not in json.dumps(cold.conversation, ensure_ascii=False)

    asyncio.run(scenario())


def test_responses_completed_call_event_before_eof_is_not_executed_or_charged():
    async def scenario():
        reads = []
        wire = NativeWire("responses", [
            truncated_call("responses", complete_arguments=True),
            reply("responses", call_id="rebuilt-call", arguments={"target": 1}),
            reply("responses", text=ANSWER),
        ])
        session = make_session(wire, [read_tool(reads)], limits=SessionLimits(max_tool_calls=1))
        events = await run_turn(session)
        assert [e.payload["call_id"] for e in events if e.type is AgentEventType.MODEL_TOOL_CALL] == [
            "discard-call", "rebuilt-call",
        ], "夹具必须真的先产出完整工具事件，再截断整个回合"
        assert [e.payload["call_id"] for e in events if e.type is AgentEventType.TOOL_STARTED] == ["rebuilt-call"]
        assert [e.payload["call_id"] for e in events if e.type is AgentEventType.TOOL_COMPLETED] == ["rebuilt-call"]
        assert reads == [1]
        assert events[-1].payload["status"] == "success"
        assert events[-1].payload["tool_calls"] == events[-1].payload["business_tool_calls"] == 1
        assert wire.requests[1].get("tools") == wire.requests[0].get("tools")
        assert wire.requests[2].get("tools"), "达到调用额度后仍可自然回答；工具执行由同一预算拒绝"
        assert "discard-call" not in json.dumps(history("responses", wire.requests[1]))
        cold = await session.state_store.load(owner=OWNER, session_id=SESSION)
        assert "discard-call" not in json.dumps(cold.conversation)
        assert [row["tool_call_id"] for row in cold.conversation if row["role"] == "tool"] == ["rebuilt-call"]

    asyncio.run(scenario())


def private_read_reply(protocol):
    """复用已有 opaque/signature 原生夹具；私有状态必须由 adapter 生成。"""
    if protocol == "anthropic_messages":
        frames = anthropic_tool_frames()
        for frame in frames:
            if frame.get("content_block", {}).get("type") == "tool_use":
                frame["content_block"]["name"] = "library__inspect"
            if frame.get("delta", {}).get("type") == "input_json_delta":
                frame["delta"]["partial_json"] = '{"target":1}'
        return sse(*frames)
    body = json.loads(reply(protocol, call_id="call-1", arguments={"target": 1}).content)
    if protocol == "responses":
        body["output"].insert(0, {
            "type": "reasoning", "id": "rs_1", "status": "completed",
            "summary": [{"type": "summary_text", "text": "Checking the actual result."}], "encrypted_content": REDACTED,
            "content": [{"type": "reasoning_text", "text": THINKING}],
        })
    else:
        body["choices"][0]["message"]["reasoning_content"] = THINKING
    return httpx.Response(200, json=body)


BROKEN_PRIVATE = "当前截断私有组_SENTINEL"
RECOVERY_HINT = "上一轮模型回复未完整结束"


def truncated_private_text(protocol):
    if protocol == "responses":
        return sse(
            {"type": "response.output_item.added", "output_index": 0, "item": {
                "type": "reasoning", "id": "rs_discard", "status": "in_progress",
                "summary": [], "encrypted_content": BROKEN_PRIVATE}},
            {"type": "response.output_text.delta", "delta": DRAFT},
        )
    if protocol == "chat_completions":
        return sse({"choices": [{"index": 0, "delta": {
            "reasoning_content": BROKEN_PRIVATE, "content": DRAFT}}]})
    return sse(
        {"type": "message_start", "message": {"role": "assistant", "content": []}},
        {"type": "content_block_start", "index": 0, "content_block": {
            "type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {
            "type": "thinking_delta", "thinking": BROKEN_PRIVATE}},
        # 没有 signature_delta/content_block_stop/message_stop，不能补造一个完成组。
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": DRAFT}},
    )


def private_blocks(protocol, request):
    native = history(protocol, request)
    if protocol == "responses":
        return [row for row in native if row.get("type") == "reasoning"]
    if protocol == "chat_completions":
        return [row["reasoning_content"] for row in native if "reasoning_content" in row]
    return [block for row in native if isinstance(row.get("content"), list)
            for block in row["content"] if block.get("type") in {"thinking", "redacted_thinking"}]


def system_text(protocol, request):
    if protocol == "anthropic_messages":
        return "".join(block["text"] for block in request["system"])
    return json.dumps([row for row in history(protocol, request) if row.get("role") == "system"], ensure_ascii=False)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_completed_read_and_native_private_group_survive_but_current_truncated_group_does_not(protocol):
    async def scenario():
        reads = []
        wire = NativeWire(protocol, [
            private_read_reply(protocol),
            truncated_private_text(protocol),
            reply(protocol, call_id="read-2", arguments={"target": 2}),
            reply(protocol, text="核验完成：目标一和目标二的事实均已确认。"),
        ])
        session = make_session(wire, [read_tool(reads)], limits=SessionLimits(max_model_rounds=4, max_tool_calls=2))
        events = await run_turn(session)
        assert reads == [1, 2], "第一次 READ 不可重放，剩余第二次 READ 仍可执行"
        assert len(wire.requests) == 4 and not wire.responses
        assert events[-1].payload["status"] == "success"
        assert events[-1].payload["model_calls"] == 4
        assert events[-1].payload["business_tool_calls"] == 2
        assert phases(events) == ["planning", "planning", "answer_recovery", "planning"]
        assert [e.payload["round"] for e in events if e.type is AgentEventType.MODEL_STARTED] == [1, 2, 3, 4]
        assert RECOVERY_HINT not in system_text(protocol, wire.requests[1])
        assert RECOVERY_HINT in system_text(protocol, wire.requests[2])
        assert RECOVERY_HINT not in system_text(protocol, wire.requests[3])
        assert "实际剩余工具调用" not in system_text(protocol, wire.requests[2])
        assert "剩余规划轮次" not in system_text(protocol, wire.requests[2])
        assert "实际剩余工具调用" not in system_text(protocol, wire.requests[3])
        assert "剩余规划轮次" not in system_text(protocol, wire.requests[3])
        assert wire.requests[2].get("tools") == wire.requests[1].get("tools")
        assert wire.requests[3].get("tools") == wire.requests[2].get("tools")
        original = private_blocks(protocol, wire.requests[1])
        assert original, "第一次工具回合必须真正携带完整原生 private 状态"
        assert private_blocks(protocol, wire.requests[2]) == original
        assert private_blocks(protocol, wire.requests[3]) == original
        if protocol == "responses":
            assert original == [{"type": "reasoning", "id": "rs_1", "status": "completed",
                                 "summary": [{"type": "summary_text", "text": "Checking the actual result."}], "encrypted_content": REDACTED,
                                 "content": [{"type": "reasoning_text", "text": THINKING}]}]
        elif protocol == "chat_completions":
            assert original == [THINKING]
        else:
            assert original == [{"type": "thinking", "thinking": THINKING, "signature": SIGNATURE},
                                {"type": "redacted_thinking", "data": REDACTED}]
        assert "FACT-1" in json.dumps(history(protocol, wire.requests[2]))
        assert "FACT-2" in json.dumps(history(protocol, wire.requests[3]))
        for request in wire.requests[2:]:
            sent = json.dumps(history(protocol, request), ensure_ascii=False)
            assert BROKEN_PRIVATE not in sent and DRAFT not in sent
        cold = await SQLiteKernelStore(secret_provider=lambda: SECRET).load(owner=OWNER, session_id=SESSION)
        serialized = json.dumps(cold.conversation, ensure_ascii=False)
        assert BROKEN_PRIVATE not in serialized and DRAFT not in serialized
        assert [row["tool_call_id"] for row in cold.conversation if row["role"] == "tool"] == ["call-1", "read-2"]
        complete = next(row for row in cold.conversation if row.get("tool_calls"))
        assert THINKING in complete["provider_state"]["layout_json"]
        assert complete["provider_state"]["protocol"] == protocol
        # 私有内容不是给用户展示的答案/事件；仅在绑定的原生请求和存储中透传。
        assert THINKING not in json.dumps([dict(e.payload) for e in events], ensure_ascii=False)
        assert BROKEN_PRIVATE not in json.dumps([dict(e.payload) for e in events], ensure_ascii=False)

    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("recovery_read_succeeds", [False, True], ids=["recovery-itself-truncates", "later-round-truncates"])
def test_only_one_reconstruction_is_allowed_per_turn(protocol, recovery_read_succeeds):
    async def scenario():
        reads = []
        responses = [truncated_text(protocol)]
        if recovery_read_succeeds:
            responses.append(reply(protocol, call_id="read-1", arguments={"target": 1}))
        responses.extend([truncated_text(protocol), reply(protocol, text="不应到达的第二次恢复")])
        wire = NativeWire(protocol, responses)
        session = make_session(wire, [read_tool(reads)])
        events = await run_turn(session)
        assert len(wire.requests) == (3 if recovery_read_succeeds else 2)
        assert len(wire.responses) == 1, "不能每个普通回合都重新获得一次恢复机会"
        assert phases(events).count("answer_recovery") == 1
        assert reads == ([1] if recovery_read_succeeds else [])
        if recovery_read_succeeds:
            assert phases(events) == ["planning", "answer_recovery", "planning"]
            assert RECOVERY_HINT not in system_text(protocol, wire.requests[2])
            assert events[-1].type is AgentEventType.TURN_COMPLETED
            assert events[-1].payload["status"] == "partial"
            assert events[-1].payload["finish_reason"] == "model_provider_error"
            assert "目标1核验完成" in events[-1].payload["answer"]
        else:
            assert events[-1].type is AgentEventType.TURN_FAILED
            assert events[-1].payload["code"] == "model_provider_error"
        cold = await session.state_store.load(owner=OWNER, session_id=SESSION)
        assert DRAFT not in json.dumps(cold.conversation, ensure_ascii=False)
        assert [row["tool_call_id"] for row in cold.conversation if row["role"] == "tool"] == (
            ["read-1"] if recovery_read_succeeds else []
        )

    asyncio.run(scenario())


class BlockedRecoveryBody(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data
        self.waiting = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield self.data
        self.waiting.set()
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_user_can_cancel_during_native_reconstruction_without_a_third_request(protocol):
    async def scenario():
        reads = []
        body = BlockedRecoveryBody(truncated_text(protocol).content)
        wire = NativeWire(protocol, [
            truncated_text(protocol),
            httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body),
            reply(protocol, call_id="never-read", arguments={"target": 1}),
        ])
        session = make_session(wire, [read_tool(reads)])
        task = asyncio.create_task(run_turn(session))
        try:
            await asyncio.wait_for(body.waiting.wait(), timeout=3)
            assert await session.cancel(owner=OWNER, session_id=SESSION)
            await session.cancel(owner=OWNER, session_id=SESSION)
            events = await asyncio.wait_for(task, timeout=3)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert events[-1].type is AgentEventType.TURN_CANCELLED
        assert phases(events) == ["planning", "answer_recovery"]
        assert reads == [] and len(wire.requests) == 2 and len(wire.responses) == 1
        assert body.closed, "取消必须关闭当前 Provider 响应流"
        assert not any(e.type in {AgentEventType.TURN_FAILED, AgentEventType.TURN_COMPLETED} for e in events)
        cold = await session.state_store.load(owner=OWNER, session_id=SESSION)
        assert DRAFT not in json.dumps(cold.conversation, ensure_ascii=False)
        assert not any(row.get("tool_calls") or row.get("provider_state") for row in cold.conversation)

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["no-model-round", "last-read-round", "last-round-keeps-tools", "tool-quota"])
def test_reconstruction_obeys_remaining_model_and_business_tool_budgets(boundary):
    async def scenario():
        protocol = "responses"
        reads = []
        if boundary == "no-model-round":
            limits = SessionLimits(max_model_rounds=1)
            responses = [truncated_text(protocol)]
        elif boundary == "last-read-round":
            limits = SessionLimits(max_model_rounds=2)
            responses = [truncated_text(protocol), reply(protocol, call_id="read-1", arguments={"target": 1})]
        else:
            limits = SessionLimits(max_model_rounds=3 if boundary == "last-round-keeps-tools" else 5,
                                   max_tool_calls=1 if boundary == "tool-quota" else 2)
            responses = [reply(protocol, call_id="read-1", arguments={"target": 1}),
                         truncated_text(protocol), reply(protocol, call_id="forbidden-read-2", arguments={"target": 2})]
        expected_requests = len(responses)
        # 即使后端还有完整回答，耗尽预算时也不得发出额外 Provider 请求。
        wire = NativeWire(protocol, responses + [reply(protocol, text="不应请求的额外额度")])
        session = make_session(wire, [read_tool(reads)], limits=limits)
        events = await run_turn(session)
        assert len(wire.requests) == expected_requests and len(wire.responses) == 1
        assert limits.max_model_rounds is not None
        assert len(wire.requests) <= limits.max_model_rounds
        expected_reads = [] if boundary == "no-model-round" else [1, 2] if boundary == "last-round-keeps-tools" else [1]
        assert reads == expected_reads
        if boundary == "no-model-round":
            assert phases(events) == ["planning"]
            assert events[-1].type is AgentEventType.TURN_FAILED
            assert events[-1].payload["code"] == "model_provider_error"
        else:
            assert events[-1].type is AgentEventType.TURN_COMPLETED
            assert events[-1].payload["status"] == "partial"
            assert events[-1].payload["finish_reason"] == (
                "tool_budget_exceeded" if boundary == "tool-quota" else "model_round_budget_exceeded"
            )
            assert events[-1].payload["model_calls"] == expected_requests
            assert events[-1].payload["business_tool_calls"] == len(expected_reads)
        if boundary == "last-read-round":
            assert wire.requests[1].get("tools") == wire.requests[0].get("tools")
        elif boundary in {"last-round-keeps-tools", "tool-quota"}:
            assert wire.requests[2].get("tools")
            rejected = [e for e in events if e.type is AgentEventType.TOOL_FAILED]
            assert [(e.payload["call_id"], e.payload["code"]) for e in rejected] == (
                [("forbidden-read-2", "tool_budget_exceeded")] if boundary == "tool-quota" else [])
        cold = await session.state_store.load(owner=OWNER, session_id=SESSION)
        assert DRAFT not in json.dumps(cold.conversation, ensure_ascii=False)
        assert [row["tool_call_id"] for row in cold.conversation if row["role"] == "tool" and json.loads(row["content"]).get("ok")] == (
            [] if boundary == "no-model-round" else ["read-1", "forbidden-read-2"] if boundary == "last-round-keeps-tools" else ["read-1"]
        )

    asyncio.run(scenario())


def test_auto_protocol_fallback_preserves_incomplete_turn_reconstruction():
    async def scenario():
        def unsupported():
            return httpx.Response(404, json={'error': 'responses api is not supported'})

        reads = []
        wire = NativeWire('auto', [
            unsupported(), truncated_text('chat_completions'),
            unsupported(), reply('chat_completions', call_id='rebuilt', arguments={'target': 1}),
            unsupported(), reply('chat_completions', text=ANSWER),
        ])
        session = make_session(wire, [read_tool(reads)])
        events = await run_turn(session)
        assert reads == [1]
        assert events[-1].payload['status'] == 'success'
        assert phases(events) == ['planning', 'answer_recovery', 'planning']
        assert wire.paths == ['/v1/responses', '/v1/chat/completions'] * 3
        assert wire.requests[3]['tools'] == wire.requests[1]['tools']
        assert DRAFT not in json.dumps(wire.requests[3]['messages'], ensure_ascii=False)
        cold = await session.state_store.load(owner=OWNER, session_id=SESSION)
        assert [r['tool_call_id'] for r in cold.conversation if r['role'] == 'tool'] == ['rebuilt']

    asyncio.run(scenario())
