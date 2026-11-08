"""资源批选：单执行器、零模型按钮、消息绑定、部分结果和跨进程 UI CAS。"""
from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.agent.domains.resources.selection import (
    _recommend,
    candidate_item,
    current_candidate_view,
    normalize_selection,
)
from app.agent.kernel.conversation_context import persisted_conversation
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.references import ReferenceError
from app.agent.kernel.state_types import AgentInput, SelectionInvalidError
from app.agent.public_view import (
    format_public_result,
    public_conversation_messages,
)
from app.agent.transports import QueryEnvelope, TelegramKernelTransport
from app.bot import agent_adapter, agent_candidates
from tests import test_agent_ux_backend as ux
from tests.test_agent_kernel_telegram_adapter import TELEBOT, Call, FakeBot, Message
from tests.test_agent_ux_backend import (
    OWNER,
    SECRET,
    SESSION,
    _events,
    _publish_candidates,
    _runtime,
    _selection,
)


@pytest.fixture
def store(tmp_path):
    yield from inspect.unwrap(ux.store)(tmp_path)


from tests.support import _prepare_batch


def _batch_result():
    return {"ok": True, "status": "partial", "summary": "批量处理结果", "error": "部分提交结果待核对", "target": "both",
            "total": 2, "succeeded": 1, "failed": 0, "review_required": 1, "items": [
        {"result_id": "ux-resource-result-001", "status": "submitted", "request_id": 81, "succeeded": ["qb", "guangya"], "failed": []},
        {"result_id": "ux-resource-result-002", "status": "manual_review", "request_id": 82, "succeeded": ["qb"], "failed": []},
    ]}


@pytest.mark.parametrize("value", [
    {"ref": "ref_" + "x" * 24, "position": 1},
    _selection({"selection_ref": "ref_" + "x" * 24}, [1, 1]),
    _selection({"selection_ref": "ref_" + "x" * 24}, [True]),
    _selection({"selection_ref": "ref_" + "x" * 24}, [51]),
    _selection({"selection_ref": "ref_" + "x" * 24}, [1], "preferred"),
    {"ref": "ref_" + "x" * 24, "positions": [], "target": "qb"},
])
def test_batch_input_rejects_legacy_duplicate_unbounded_or_implicit_selection(value):
    with pytest.raises(SelectionInvalidError):
        normalize_selection(value)


def test_recommendation_only_combines_verified_complementary_ranges():
    items = [candidate_item({"title": title, "match": "episode_pack", "_verification_context": {
        "title": "Example", "season": 1, "episode": 5 if pos <= 2 else 1,
    }}, pos) for pos, title in enumerate([
        "Example.S01E05-06.2160p.SDR", "Example.S01E05-06.2160p.HDR",
        "Example.S01E01-04.2160p.SDR", "Example.S01E01-04.2160p.HDR",
    ], 1)]
    assert _recommend(items) == [1, 3]
    text, markup = agent_candidates.render(TELEBOT, {"items": items, "recommended_positions": [1, 3]}, {
        "handle": "ref_" + "a" * 24, "positions": [1, 3], "expanded": False, "target": "guangya",
    })
    assert "资源推荐与批选" in text
    assert any(button.text == "使用推荐组合" for button in markup.buttons)
    assert _recommend([candidate_item({"title": "Movie.2026.2160p"}, 1), candidate_item({"title": "Movie.2026.1080p"}, 2)]) == []
    assert _recommend([items[0], candidate_item({"title": "Other.S01E01-04"}, 2)]) == [1]


@pytest.mark.parametrize("natural", [False, True])
def test_both_intent_inputs_share_one_batch_plan_and_settle_after_confirm(store, natural):
    class Model:
        def __init__(self):
            self.calls = 0
            self.intent = {}

        async def stream(self, request, *, cancellation):
            self.calls += 1
            if any("已确认操作的可信系统结果" in row.content for row in request.messages):
                yield ModelEvent(ModelEventType.TEXT_DELTA, text="已提交明确接受的任务，其余结果未知，请核验。")
                yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")
                return
            yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("batch", "resources.download", self.intent))
            yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")

    async def exercise():
        model = Model()
        session, pipeline, states = _runtime(store, model=model)
        view, _ = await _publish_candidates(pipeline, states)
        model.intent = await ux._download_intent(store, view, [1, 2], "both")
        message = "把1和2下载到两个目标"
        user = AgentInput(owner=OWNER, session_id=SESSION, message=message) if natural else QueryEnvelope(
            owner=OWNER, session_id=SESSION, message=message, selection=_selection(view, [2, 1], "both"),
        ).to_agent_input()
        with patch("app.indexers.downloads.preview_indexer_download_batch", side_effect=_prepare_batch) as prepare, \
             patch("app.indexers.downloads.submit_indexer_download_batch", side_effect=lambda *a, **kw: _batch_result()) as execute:
            events = await _events(session.run(user))
            approvals = [e for e in events if e.type is AgentEventType.EFFECT_APPROVAL_REQUIRED]
            assert len(approvals) == 1
            assert model.calls == int(natural)
            assert events[-1].payload["model_calls"] == int(natural)
            prepare.assert_called_once()
            assert prepare.call_args.args[1] == ["ux-resource-result-001", "ux-resource-result-002"]
            assert prepare.call_args.args[2] == "both"
            execute.assert_not_called()
            plan = approvals[0].payload["plan"]["plan_id"]
            result_events = await _events(session.confirm(owner=OWNER, session_id=SESSION, plan_id=plan))
            result = next(e.payload["result"] for e in result_events if e.type is AgentEventType.EFFECT_COMPLETED)
            assert (result["data"]["total"], result["data"]["succeeded"], result["data"]["review_required"]) == (2, 1, 1)
            assert [(item["position"], item["status"]) for item in result["data"]["items"]] == [(1, "submitted"), (2, "manual_review")]
            assert result_events[-1].payload["status"] == "partial"
            text = format_public_result(result)
            assert "#1" in text and "下载请求 #81" in text and "结果未知，请先核验" in text
            await _events(session.confirm(owner=OWNER, session_id=SESSION, plan_id=plan))
            execute.assert_called_once()
            assert await current_candidate_view(state=await states.load(owner=OWNER, session_id=SESSION), store=store)
            events = await _events(session.run(QueryEnvelope(owner=OWNER, session_id=SESSION, message="仍只做预检", selection=_selection(view, [1, 2], "both")).to_agent_input()))
            assert sum(e.type is AgentEventType.EFFECT_APPROVAL_REQUIRED for e in events) == 1
            execute.assert_called_once()
            assert model.calls == int(natural) + 1, "部分完成后仅续接解释，重复确认不能再次调用模型"
    asyncio.run(exercise())


def test_recovered_candidates_attach_to_exact_search_not_latest_message(store):
    async def exercise():
        _, pipeline, states = _runtime(store)
        view, _ = await _publish_candidates(pipeline, states)
        messages = public_conversation_messages([
            {"role": "user", "content": "搜索"},
            {"role": "tool", "tool_name": "indexer.search_resources", "content": "opaque_refs=" + json.dumps([{"ref": view["ref"], "kind": "resource_candidates"}])},
            {"role": "assistant", "content": "找到两个版本"},
            {"role": "user", "content": "下载目标是什么"},
            {"role": "assistant", "content": "目标为光鸭"},
        ], candidate_view=view)
        assert messages[1]["candidate_view"] == view
        assert "candidate_view" not in messages[-1]
        unbound = public_conversation_messages([{"role": "assistant", "content": "无关历史"}], candidate_view=view)
        assert "candidate_view" not in unbound[0]
    asyncio.run(exercise())


@pytest.mark.parametrize("invalidation", ["directory_changed", "retired_single_snapshot"])
def test_receiving_directory_change_invalidates_frozen_plan_without_any_download(store, invalidation):
    from app.indexers.downloads import preview_indexer_download_batch
    from app.modules.offline import OfflineRules
    from dataclasses import replace
    from tests.test_agent_indexer_actions import FakeDownloadService, _resource_item

    async def exercise():
        session, pipeline, states = _runtime(store)
        view, _ = await _publish_candidates(pipeline, states)
        rules = replace(OfflineRules.from_config(), target_dir_id="a", target_dir_name="接收A", secondary_enabled=False)
        def preview(*args, **kwargs):
            result, snapshot = preview_indexer_download_batch(*args, **kwargs)
            # 种入旧单项计划的真实形状；不会为旧票据重新预检或换一份执行快照。
            return result, snapshot["resources"][0] if invalidation == "retired_single_snapshot" else snapshot
        with patch("app.agent.kernel.ports.sdk_effects.get_indexer_service", return_value=FakeDownloadService(_resource_item())), \
             patch("app.indexers.downloads.preview_indexer_download_batch", side_effect=preview), \
             patch("app.indexers.downloads.download_target_readiness", return_value={"qb": True, "guangya": True}), \
             patch("app.modules.missing_resources.config.get_bool", return_value=True), \
             patch("app.modules.offline.OfflineRules.from_config", return_value=rules) as config, \
             patch("app.indexers.downloads.submit_download_input") as execute:
            positions = [1] if invalidation == "retired_single_snapshot" else [1, 2]
            events = await _events(session.run(QueryEnvelope(owner=OWNER, session_id=SESSION, message="预检", selection=_selection(view, positions, "both")).to_agent_input()))
            approval = next(e for e in events if e.type is AgentEventType.EFFECT_APPROVAL_REQUIRED)
            assert "光鸭：接收A / 每任务隔离目录" in approval.payload["plan"]["preview"]["data"]["receiving_folders"]
            if invalidation == "directory_changed":
                config.return_value = replace(rules, target_dir_id="b", target_dir_name="接收B")
            events = await _events(session.confirm(owner=OWNER, session_id=SESSION, plan_id=approval.payload["plan"]["plan_id"]))
            assert any(e.type is AgentEventType.EFFECT_FAILED for e in events)
            assert not (await states.load(owner=OWNER, session_id=SESSION)).pending_effect_plan_id
            execute.assert_not_called()
    asyncio.run(exercise())


def _target_options(_owner):
    return {"target": "guangya", "target_source": "saved_preference", "targets": [
        {"value": "qb", "label": "qBittorrent", "available": True},
        {"value": "guangya", "label": "光鸭", "available": True},
        {"value": "both", "label": "两个目标", "available": True},
    ]}


@pytest.mark.parametrize("terminal_action", ["c", "x"])
def test_telegram_in_place_multiselect_previews_and_finishes_once(store, monkeypatch, terminal_action):
    owner = agent_adapter.telegram_agent_owner(-100, 7)
    session_id = agent_adapter.telegram_agent_session_id(-100, 7)
    monkeypatch.setattr("app.agent.domains.resources.selection._target_options", _target_options)
    from tests.test_agent_kernel_core import ScriptedModel
    model = ScriptedModel([[ModelEvent(ModelEventType.TEXT_DELTA, text="已接受任务请见回执；未知结果请先核验。"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")]])
    session, pipeline, states = _runtime(store, model=model)
    runtime = SimpleNamespace(store=store, telegram=TelegramKernelTransport(session))
    monkeypatch.setattr("app.agent.kernel.bootstrap.get_agent_kernel_runtime", lambda: runtime)
    monkeypatch.setattr(agent_adapter, "get_agent_kernel_runtime", lambda: runtime)
    monkeypatch.setattr(agent_adapter, "telegram_agent_access", lambda *a: "allowed")
    from app import config
    config.set_and_save({"AGENT_ENABLED": "1", "TG_AGENT_ENABLED": "1",
                         "TG_CHAT_ID": "-100", "TG_AGENT_ALLOWED_USER_IDS": "7"})
    monkeypatch.setattr(agent_adapter.agent_rate_limiter, "allow", lambda *a, **k: True)
    view, _ = asyncio.run(_publish_candidates(pipeline, states, owner=owner, session_id=session_id))
    draft = asyncio.run(agent_candidates.start_draft(runtime, owner=owner, session_id=session_id, view=view))
    bot = FakeBot()
    message = Message(message_id=111)

    def click(action, handle=None):
        nonlocal draft
        call = Call(f"agk:s:{handle or draft['handle']}:{action}", message)
        agent_adapter.handle_agent_callback(bot, call, TELEBOT)
        draft = asyncio.run(states.load(owner=owner, session_id=session_id)).metadata["ux_candidate_draft"]

    assert draft["positions"] == []
    text, markup = agent_candidates.render(TELEBOT, view, draft)
    assert "请选择需要的版本" in text and "资源推荐与批选" not in text
    assert not any(button.text == "使用推荐组合" for button in markup.buttons)
    stale = draft["handle"]
    click("e")
    click("i1")
    click("i2")
    click("tboth")
    assert draft["positions"] == [1, 2] and draft["target"] == "both"
    assert model.requests == [] and bot.sent == [] and len(bot.edits) == 4
    edit_count = len(bot.edits)
    click("r")  # 旧消息中遗留的推荐按钮不能清掉用户的手动选择。
    assert len(bot.edits) == edit_count and draft["positions"] == [1, 2]
    assert "当前没有推荐组合" in bot.answers[-1][1]
    click("i2", stale)
    assert len(bot.edits) == edit_count and draft["positions"] == [1, 2]
    assert bot.answers[-1][2]["show_alert"] is True
    for edit in bot.edits:
        assert edit[2] == 111
        for button in edit[3]["reply_markup"].buttons:
            assert len(button.callback_data.encode()) <= 64
            assert "http" not in button.callback_data
    with patch("app.indexers.downloads.preview_indexer_download_batch", side_effect=_prepare_batch), \
         patch("app.indexers.downloads.submit_indexer_download_batch", side_effect=lambda *a, **kw: _batch_result()) as execute:
        click("p")
        assert draft["phase"] == "approval" and model.requests == []
        execute.assert_not_called()
        plan = draft["plan_id"]
        agent_adapter.handle_agent_callback(bot, Call(f"agk:{terminal_action}:{plan}", message), TELEBOT)
        assert execute.call_count == (1 if terminal_action == "c" else 0)
        if terminal_action == "c":
            assert "结果未知" in bot.edits[-1][0] and "#81" in bot.edits[-1][0]
        else:
            assert "已取消" in bot.edits[-1][0]
        assert bot.edits[-1][3]["reply_markup"] is None
        assert "继续挑选本批资源" not in bot.edits[-1][0]
        ended = asyncio.run(states.load(owner=owner, session_id=session_id)).metadata["ux_candidate_draft"]
        assert ended["phase"] == "result" and ended["positions"] == [] and ended["plan_id"] == ""
        assert agent_candidates.render(TELEBOT, view, ended)[1] is None
        assert bot.sent == []
        assert len(model.requests) == (1 if terminal_action == "c" else 0)
        count = len(bot.edits)
        agent_adapter.handle_agent_callback(bot, Call(f"agk:c:{plan}", message), TELEBOT)
        assert len(bot.edits) == count
        assert execute.call_count == (1 if terminal_action == "c" else 0)
        click("b", ended["handle"])
        assert len(bot.edits) == count
        assert draft["phase"] == "result"


@pytest.mark.parametrize("final_text", ["已完成", "已取消，本次未执行"])
def test_telegram_followup_approval_transfers_then_closes_the_same_draft(store, monkeypatch, final_text):
    monkeypatch.setattr("app.agent.domains.resources.selection._target_options", _target_options)

    async def exercise():
        _, pipeline, states = _runtime(store)
        view, _ = await _publish_candidates(pipeline, states)
        runtime = SimpleNamespace(store=store)
        draft = await agent_candidates.start_draft(runtime, owner=OWNER, session_id=SESSION, view=view)
        await agent_candidates.save_draft(runtime, owner=OWNER, session_id=SESSION, view=view,
            expected=draft["handle"], draft={**draft, "phase": "approval", "plan_id": "first-plan", "positions": [1, 2]})
        await agent_candidates.settle_draft(runtime, owner=OWNER, session_id=SESSION,
            plan_id="first-plan", result_html="第一步已完成", next_plan_id="next-plan")
        following = (await states.load(owner=OWNER, session_id=SESSION)).metadata["ux_candidate_draft"]
        assert following["phase"] == "approval" and following["plan_id"] == "next-plan"
        assert following["positions"] == [1, 2]
        await agent_candidates.settle_draft(runtime, owner=OWNER, session_id=SESSION,
            plan_id="first-plan", result_html="过时结果不得覆盖下一步")
        assert (await states.load(owner=OWNER, session_id=SESSION)).metadata["ux_candidate_draft"] == following
        await agent_candidates.settle_draft(runtime, owner=OWNER, session_id=SESSION,
            plan_id="next-plan", result_html=final_text)
        ended = (await states.load(owner=OWNER, session_id=SESSION)).metadata["ux_candidate_draft"]
        assert ended["phase"] == "result" and ended["plan_id"] == "" and ended["positions"] == []
        assert agent_candidates.render(TELEBOT, view, ended) == (final_text, None)

    asyncio.run(exercise())


def test_telegram_ui_cas_survives_restart_rejects_concurrent_and_cross_owner(store, monkeypatch):
    monkeypatch.setattr("app.agent.domains.resources.selection._target_options", _target_options)
    async def exercise():
        _, pipeline, states = _runtime(store)
        view, _ = await _publish_candidates(pipeline, states)
        runtime = SimpleNamespace(store=store)
        draft = await agent_candidates.start_draft(runtime, owner=OWNER, session_id=SESSION, view=view)
        reloaded = SimpleNamespace(store=SQLiteKernelStore(secret_provider=lambda: SECRET))
        assert (await agent_candidates.load_draft(reloaded, owner=OWNER, session_id=SESSION, handle=draft["handle"], message_id=111))[1] == draft
        next_drafts = await asyncio.gather(*[
            agent_candidates.save_draft(local, owner=OWNER, session_id=SESSION, view=view, draft={**draft, "positions": [position]}, expected=draft["handle"])
            for local, position in [(runtime, 1), (reloaded, 2)]
        ], return_exceptions=True)
        assert sum(isinstance(item, dict) for item in next_drafts) == 1
        assert sum(isinstance(item, SelectionInvalidError) for item in next_drafts) == 1
        with pytest.raises(ReferenceError):
            await agent_candidates.load_draft(reloaded, owner="foreign", session_id=SESSION, handle=draft["handle"], message_id=111)
        await _publish_candidates(pipeline, states)
        winning = next(item for item in next_drafts if isinstance(item, dict))
        with pytest.raises(SelectionInvalidError):
            await agent_candidates.load_draft(reloaded, owner=OWNER, session_id=SESSION, handle=winning["handle"], message_id=111)
    asyncio.run(exercise())


@pytest.mark.parametrize("title", ["/private/secret/movie.mkv", "C:\\private\\movie.mkv", "https://example.invalid/private", "password=SECRET"])
def test_resource_titles_never_reveal_paths_or_credentials(title):
    from app.agent.public_safety import sanitize_resource_title
    assert not sanitize_resource_title(title) or sanitize_resource_title(title) == "[网址]"


def test_resource_titles_preserve_filename_not_tool_names():
    from app.agent.public_safety import sanitize_resource_title
    assert sanitize_resource_title("Example.S01E05-06.2160p.HDR") == "Example.S01E05-06.2160p.HDR"
    assert candidate_item({"title": "Example.S01E05-06.2160p.HDR"}, 1)["coverage"] == [1, 5, 6]


def test_telegram_reply_binding_uses_signed_batch_and_rejects_replaced_search(store, monkeypatch):
    monkeypatch.setattr("app.agent.domains.resources.selection._target_options", _target_options)
    async def exercise():
        _, pipeline, states = _runtime(store)
        view, _ = await _publish_candidates(pipeline, states)
        runtime = SimpleNamespace(store=store)
        draft = await agent_candidates.start_draft(runtime, owner=OWNER, session_id=SESSION, view=view)
        message = Message()
        message.reply_to_message = SimpleNamespace(reply_markup=SimpleNamespace(keyboard=[[{"callback_data": f"agk:s:{draft['handle']}:p"}]]))
        assert await agent_candidates.reply_selection_ref(runtime, owner=OWNER, session_id=SESSION, message=message) == view["selection_ref"]
        await _publish_candidates(pipeline, states)
        with pytest.raises(SelectionInvalidError):
            await agent_candidates.reply_selection_ref(runtime, owner=OWNER, session_id=SESSION, message=message)
    asyncio.run(exercise())


def test_candidate_result_history_metadata_survives_model_round_without_entering_model():
    from app.agent.kernel.model import ModelMessage
    prior = [{"role": "assistant", "content": "可信结果", "tool_name": "resources.download", "public_content": "已提交", "candidate_result_ref": "ref_example"}]
    messages = [ModelMessage.from_dict(prior[0]), ModelMessage(role="user", content="后续提问")]
    assert "candidate_result_ref" not in messages[0].to_dict()
    stored = persisted_conversation(messages, current_user_index=1, original_message="后续提问", prior_conversation=prior)
    assert stored[0]["candidate_result_ref"] == "ref_example"
    assert stored[0]["public_content"] == "已提交"


@pytest.mark.parametrize("scoped", [False, True])
def test_telegram_real_kernel_keeps_empty_update_answer_without_a_zero_selection_card(store, monkeypatch, scoped):
    class Model:
        calls = 0

        async def stream(self, request, *, cancellation):
            self.calls += 1
            if self.calls == 1:
                yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(
                    "search-updates", "fixture.resource_read", {"title": "仙逆"},
                ))
                yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")
            else:
                yield ModelEvent(ModelEventType.TEXT_DELTA, text="目前没有可确认覆盖目标缺集的4K推荐，不能据此断言没有更新。")
                yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")

    resources = ux._episode_candidates_result() if scoped else ux._resources()
    monkeypatch.setattr(ux, "_resources", lambda **_: resources)
    monkeypatch.setattr("app.agent.domains.resources.selection._target_options", _target_options)
    model = Model()
    session, _, _ = _runtime(store, model=model)
    runtime = SimpleNamespace(store=store, telegram=TelegramKernelTransport(session))
    monkeypatch.setattr(agent_adapter, "get_agent_kernel_runtime", lambda: runtime)
    monkeypatch.setattr(agent_adapter, "telegram_agent_access", lambda *args: "allowed")
    monkeypatch.setattr(agent_adapter.agent_rate_limiter, "allow", lambda *args, **kwargs: True)
    bot = FakeBot()
    source = Message("仙逆完美世界有更新吗？4K排除1080")
    view = agent_adapter._execute_query(bot, TELEBOT, source, chat_id="-100", user_id="7", text=source.text)
    assert model.calls == 2
    assert view.answer == "目前没有可确认覆盖目标缺集的4K推荐，不能据此断言没有更新。"
    assert view.candidate_view is None
    assert view.approval is None
    messages = [(text, kwargs) for text, _, _, kwargs in bot.edits] + [(text, kwargs) for _, text, kwargs in bot.sent]
    assert any(view.answer in text for text, _ in messages)
    assert not any(kwargs.get("reply_markup") for _, kwargs in messages)
    assert not any("资源搜索与批选" in text or "预览下载 0 项" in text for text, _ in messages)
    state = asyncio.run(store.load(owner=agent_adapter.telegram_agent_owner(-100, 7),
                                  session_id=agent_adapter.telegram_agent_session_id(-100, 7)))
    assert state.recent_refs, "隐藏自动批选卡不应删除资源引用或阻断后续自然语言预检"
