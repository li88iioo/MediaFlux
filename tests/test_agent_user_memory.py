"""跨会话长期记忆：经确认保存在身份偏好中，每回合以数据形式注入，不跨身份、不阻断回合。"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.modules.media_preferences import owner_memory
from app.database import media_owner_digest
from app.modules.media_preferences import (
    apply_media_preferences_change, media_preferences_preview,
    prepare_media_preferences_change, validate_preference_updates,
)
from app.agent.kernel import model_rounds
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import AgentInput
from tests.test_agent_kernel_core import ScriptedModel, collect, read_tool
from tests.support import isolated_test_database

NOTE = "动漫库在 Jellyfin 里叫 Anime"


@pytest.fixture
def memory_db():
    with isolated_test_database():
        yield


def save(owner, **updates):
    digest = media_owner_digest(owner)
    prepared = prepare_media_preferences_change(digest, updates)
    assert apply_media_preferences_change(digest, updates, expected_fingerprint=prepared["fingerprint"]) is not None
    return media_preferences_preview(prepared)


def test_memory_notes_validation_bounds():
    assert validate_preference_updates({"memory_notes": [f" {NOTE} ", NOTE.upper(), "x" * 200]}) == {
        "memory_notes": [NOTE, "x" * 200]}
    for invalid in (["x" * 201], [f"n{i}" for i in range(21)], ["换行\u0007控制符"], [42], "不是列表"):
        with pytest.raises(ValueError):
            validate_preference_updates({"memory_notes": invalid})


def test_saved_memory_is_confirmed_listed_and_scoped_to_its_owner(memory_db):
    assert owner_memory("owner-a") == ""
    preview = save("owner-a", memory_notes=[NOTE, "不要推荐韩剧"], preferred_resolution="2160p")
    assert f"长期备忘：\n· {NOTE}\n· 不要推荐韩剧" in preview["effects"]
    memory = owner_memory("owner-a")
    payload = json.loads(memory.splitlines()[0])
    assert payload == {"preferences": {"preferred_resolution": "2160p"}, "notes": [NOTE, "不要推荐韩剧"]}
    assert "preferences.update" in memory
    assert owner_memory("owner-b") == ""
    save("owner-a", memory_notes=[])
    assert json.loads(owner_memory("owner-a").splitlines()[0]) == {
        "preferences": {"preferred_resolution": "2160p"}, "notes": []}


def _session(model, provider):
    state, catalog = InMemorySessionStateStore(), ToolCatalog([read_tool("library.search")])
    return AgentSession(model=model, catalog=catalog,
        pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state,
        limits=SessionLimits(max_model_rounds=4), user_memory=provider)


def _rounds():
    answer = [ModelEvent(ModelEventType.TEXT_DELTA, text="好"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")]
    return [[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("c1", "library.search", {})),
             ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")], answer]


def test_memory_is_loaded_once_per_turn_and_sits_in_the_stable_head(monkeypatch):
    calls = []
    model = ScriptedModel(_rounds())
    session = _session(model, lambda owner: calls.append(owner) or f'{{"notes": ["{NOTE}"]}}')
    history = {"requests": [], "observations": [], "legacy_note": "OLD-HISTORY"}
    monkeypatch.setattr(model_rounds, "history_memory", lambda _state: history)
    events = asyncio.run(collect(session.run(AgentInput(message="推荐点动画", owner="owner-a", session_id="s"))))
    assert events[-1].type.value == "turn.completed"
    assert calls == ["owner-a"]
    for request in model.requests:
        head = request.system_prompt[:request.stable_system_chars]
        assert "用户长期记忆" in head and NOTE in head and "不是指令" in head
        assert "OLD-HISTORY" not in head and "OLD-HISTORY" in request.system_prompt


@pytest.mark.parametrize("provider", [lambda _owner: "", lambda _owner: (_ for _ in ()).throw(RuntimeError("db down"))])
def test_empty_or_failing_memory_never_blocks_the_turn(provider):
    model = ScriptedModel(_rounds())
    events = asyncio.run(collect(_session(model, provider).run(
        AgentInput(message="推荐点动画", owner="owner-a", session_id="s"))))
    assert events[-1].type.value == "turn.completed"
    assert all("用户长期记忆" not in request.system_prompt for request in model.requests)
