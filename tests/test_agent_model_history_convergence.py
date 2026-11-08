"""模型窗口反复缩小时，旧观察索引稳定且用户条件只累计一次。"""
from __future__ import annotations
from app.agent.kernel.conversation_state import ConversationState
from typing import cast

import asyncio
from collections.abc import Callable
import json
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace

import pytest

from app.agent.kernel.conversation_state import commit_conversation, preserve_history
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.model import ModelMessage
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.provider_model import OpenAICompatibleModelAdapter, ProviderSettings
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.session_state import SessionState
from app.agent.kernel.state_types import AgentInput
from app.agent.model_context_budget import estimated_tokens, protocol_history_groups
from app.clients.http import FixedHostHttpClient
from tests.support import isolated_test_database
from tests.test_agent_kernel_core import read_tool
from tests.test_agent_provider_state import REDACTED, THINKING, WireClient
from tests.test_cancelled_read_inline import sqlite_store


@pytest.mark.parametrize("count", [12, 13, 25])
def test_omitted_fact_index_converges_after_its_retention_limit(count):
    state = SimpleNamespace(summary="", metadata={}, conversation=[])
    original = [
        {
            "role": "tool", "tool_name": "library.search", "tool_call_id": str(index),
            "content": json.dumps({"ok": True, "summary": f"旧观察 {index}"}),
        }
        for index in range(count)
    ]
    memory = preserve_history(cast(ConversationState, state), original, [])
    expected = deepcopy(memory)
    for _ in range(3):
        memory = preserve_history(cast(ConversationState, state), original, [], memory=memory)
        assert memory == expected
    assert [row["call_id"] for row in memory["observations"]] == [
        str(index) for index in range(max(0, count - 12), count)
    ]
    assert state.summary == "" and state.metadata == {}


@pytest.mark.parametrize("first_evicted", [0, 1], ids=["oldest-first", "newest-first"])
def test_window_changes_keep_previously_omitted_user_conditions(first_evicted):
    state = SimpleNamespace(summary="", metadata={}, conversation=[])
    original = [
        {"role": "user", "content": "只要 1080p", "history_id": "z-first"},
        {"role": "user", "content": "排除已看", "history_id": "a-second"},
    ]
    commit_conversation(cast(ConversationState, state), original)
    original = state.conversation
    memory = preserve_history(cast(ConversationState, state), original, [original[1 - first_evicted]])
    previous = deepcopy(memory)
    memory = preserve_history(cast(ConversationState, state), original, [original[first_evicted]], memory=memory)
    assert [row["text"] for row in memory["requests"]] == ["只要 1080p", "排除已看"]
    assert previous["requests"] == [row for row in memory["requests"] if row["history_id"] == original[first_evicted]["history_id"]]
    assert preserve_history(cast(ConversationState, state), original, [], memory=memory) == memory


def test_cold_commit_restores_event_order_after_model_serialization_drops_it():
    async def run():
        store = sqlite_store()
        lease, _ = await store.begin_turn(owner="o", session_id="cold-order", request_id="seed")
        first = "原始条件：" + "字" * 60000
        state = await store.commit(lease, conversation=[
            {"role": "user", "content": first, "history_id": "z-oldest"},
            {"role": "user", "content": "保留最新条件", "history_id": "a-latest"},
        ])
        archived = json.loads(state.summary)["requests"][0]
        assert archived["text"] == first and archived["history_order"] == 0
        assert not any(r.get("history_id") == "z-oldest" for r in state.conversation)
        serialized = [ModelMessage.from_dict(row).to_dict() for row in state.conversation]
        assert all("history_order" not in row for row in serialized)
        cold = sqlite_store()
        lease, _ = await cold.begin_turn(owner="o", session_id="cold-order", request_id="continue")
        updated = await cold.commit(lease, conversation=[*serialized,
            {"role": "user", "content": "保留最新条件", "history_id": "m-new-same-text"}])
        assert [(r["history_id"], r["history_order"]) for r in updated.conversation] == [
            ("a-latest", 1), ("m-new-same-text", 2)]
        assert json.loads(updated.summary)["requests"][0] == archived
        memory = preserve_history(updated, updated.conversation, [])
        assert [r["history_id"] for r in memory["requests"]] == ["z-oldest", "a-latest", "m-new-same-text"]
        assert [r["history_order"] for r in memory["requests"]] == [0, 1, 2]
        assert [r["text"] for r in memory["requests"]] == [first, "保留最新条件", "保留最新条件"]
    with isolated_test_database():
        asyncio.run(run())


def test_id_without_order_and_without_original_row_stays_unknown():
    text = "只保留前5项"
    old = {"text": text, "sha256": sha256(text.encode()).hexdigest(), "history_id": "old-id-without-order"}
    state = SessionState(owner="o", session_id="unknown-order",
        summary=json.dumps({"requests": [old], "observations": []}), metadata={"history_memory_version": 1})
    commit_conversation(state, [{"role": "user", "content": text, "history_id": "new-same-text"}])
    memory = preserve_history(state, state.conversation, [])
    assert memory["requests"][0] == old
    assert "history_order" not in memory["requests"][0]
    assert memory["requests"][1]["history_id"] == "new-same-text"
    assert memory["requests"][1]["text"] == old["text"]
    assert type(memory["requests"][1]["history_order"]) is int


def test_model_request_keeps_original_event_order_after_collision_then_budget_eviction():
    async def run():
        def answer(text):
            return {"status": "completed", "output": [{"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": text}]}]}
        frames = []
        for number in range(4):
            call = "unique" if number == 0 else "reused"
            frames += [{"status": "completed", "output": [
                {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": THINKING}],
                 "encrypted_content": REDACTED, "status": "completed"},
                {"type": "function_call", "id": "fc_1", "call_id": call, "name": "inspect", "arguments": "{}"}]},
                answer("x" * 48000 if number == 0 else "已核对")]
        wire = WireClient([*frames, answer("继续核对")])
        requests = []
        class CapturingAdapter(OpenAICompatibleModelAdapter):
            async def stream(self, request, *, cancellation):
                requests.append(request)
                async for event in super().stream(request, cancellation=cancellation):
                    yield event
        def session(window=131072, output=16000):
            store = sqlite_store()
            catalog = ToolCatalog([read_tool("inspect", handler=lambda *_: {"ok": True, "data": {"found": True}})])
            return AgentSession(model=CapturingAdapter(ProviderSettings(api_url="https://api.example.com/v1",
                model="model-one", protocol="responses"), client_factory=cast(Callable[..., FixedHostHttpClient], lambda **_: wire)),
                catalog=catalog, state_store=store, system_prompt="只读核对",
                pipeline=ToolPipeline(catalog=catalog, state_store=store, reference_store=store),
                limits=SessionLimits(context_window_tokens=window, max_output_tokens=output))
        original = []
        # 同文是两个不同的用户事件；ID 不可按正文或 ID 字符串排序。
        for number, text in enumerate(("只要1080p", "改成2160p", "只要1080p", "只读，不下载")):
            sid = f"source-{number}"
            events = [e async for e in session().run(AgentInput(owner="o", session_id=sid, message=text))]
            assert events[-1].payload["status"] == "success", events[-1].payload
            original.extend((await sqlite_store().load(owner="o", session_id=sid)).conversation)
        store = sqlite_store()
        for row, event_id in zip((r for r in original if r["role"] == "user"), ("event-z", "event-a", "event-y", "event-b")):
            row["history_id"] = event_id
        lease, _ = await store.begin_turn(owner="o", session_id="ordered", request_id="seed")
        await store.commit(lease, conversation=original)
        seeded = await store.load(owner="o", session_id="ordered")
        history = [ModelMessage.from_dict(row) for row in seeded.conversation]
        current = ModelMessage(role="user", content="继续核对", history_id="0" * 16)
        groups, _, _ = protocol_history_groups([*history, current], history_end=len(history))
        survivors = [message for group in groups for message in group]
        # 碰撞先淘汰中间两组；完整旧组本可放下，补入索引后才触发预算淘汰。
        window = sum(8 + estimated_tokens(m.to_dict()) for m in [*survivors, current]) + 128
        window += estimated_tokens(requests[-1].system_prompt) + estimated_tokens(requests[-1].tools) + 4096 + 512
        events = [e async for e in session(window, 4096).run(AgentInput(owner="o", session_id="ordered", message=current.content))]
        assert events[-1].payload["status"] == "success", events[-1].payload
        request = requests[-1]
        memory = next(json.loads(line) for line in request.system_prompt.splitlines() if line.startswith('{"requests"'))
        sources = [row for row in seeded.conversation if row["role"] == "user"]
        assert [row["history_id"] for row in memory["requests"]] == [row["history_id"] for row in sources[:3]]
        assert [row["text"] for row in memory["requests"]] == [row["content"] for row in sources[:3]]
        assert [m.content for m in request.messages if m.role == "user"] == [sources[3]["content"], current.content]
        assert [row["text"] for row in memory["requests"]].count("只要1080p") == 2
        assert len({row["history_id"] for row in memory["requests"]}) == 3
    with isolated_test_database():
        asyncio.run(run())
