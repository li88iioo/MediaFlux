"""Web/TG→Kernel→下载请求→回执的本轮业务验收，生产边界全部 fake。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import database as db
from app.agent import indexer_actions
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.transports import (
    EffectEnvelope,
    QueryEnvelope,
    TelegramKernelTransport,
    WebKernelTransport,
)
from app.agent.kernel.ux_selection import current_candidate_view
from app.agent.public_view import public_conversation_messages
from app.indexers.models import ResolvedDownload
from app.modules import download_dispatcher as dispatcher
from tests.support import isolated_test_database
from tests.test_agent_ux_backend import (
    OWNER,
    SECRET,
    SESSION,
    _resources,
    _runtime,
    _selection,
)


class SearchModel:
    """只给模型边界提供预设输出，实际路由、校验、落盘由 Kernel 完成。"""

    async def stream(self, request, *, cancellation):
        cancellation.raise_if_cancelled()
        if any("已确认操作的可信系统结果" in message.content for message in request.messages):
            yield ModelEvent(ModelEventType.TEXT_DELTA, text="两项下载请求已提交，回执已记录。")
            yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")
            return
        if request.round_index == 0:
            yield ModelEvent(
                ModelEventType.TOOL_CALL_COMPLETED,
                tool_call=ModelToolCall(
                    "deep-search",
                    "indexer.search_resources",
                    {"title": "Example"},
                ),
            )
            yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")
        elif request.round_index == 1:
            evidence = next(
                message.content
                for message in reversed(request.messages)
                if message.role == "tool"
            )
            refs = json.loads(evidence)["reference_arguments"]
            yield ModelEvent(
                ModelEventType.TOOL_CALL_COMPLETED,
                tool_call=ModelToolCall(
                    "deep-present",
                    "indexer.present_candidates",
                    {
                        "resource_candidates_ref": refs["resource_candidates_ref"],
                        "positions": [1, 2],
                    },
                ),
            )
            yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")
        else:
            yield ModelEvent(
                ModelEventType.TEXT_DELTA, text="已找到两项候选，尚未下载。"
            )
            yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")


@pytest.fixture
def offline_chain():
    with (
        isolated_test_database(),
        patch("socket.socket.connect", side_effect=AssertionError("禁止实际网络连接")),
    ):
        resources = {
            item["result_id"]: SimpleNamespace(**item)
            for item in _resources().data["items"]
        }

        async def resolve(result_id, *, scope="manual"):
            digest = "a" if result_id.endswith("001") else "b"
            return ResolvedDownload("magnet", "magnet:?xt=urn:btih:" + digest * 40)

        service = SimpleNamespace(
            result_store=SimpleNamespace(get=resources.__getitem__),
            get_result=lambda key, **_kw: resources[key],
            resolve=resolve,
            enabled_site_ids=("demo",),
        )
        with (
            patch.object(indexer_actions.config, "get_bool", return_value=True),
            patch.object(indexer_actions, "get_indexer_service", return_value=service),
            patch.object(
                indexer_actions,
                "download_target_readiness",
                return_value={"qb": True, "guangya": True},
            ),
            patch.object(indexer_actions, "run_indexer_awaitable_sync", asyncio.run),
            patch.object(
                dispatcher,
                "_submit_guangya",
                return_value={"ok": True, "task_id": "fake-gy"},
            ) as cloud,
            patch.object(
                dispatcher,
                "_submit_qb",
                return_value={"ok": True, "task_id": "fake-qb"},
            ) as qb,
        ):
            yield cloud, qb


def runtime():
    store = SQLiteKernelStore(secret_provider=lambda: SECRET)
    session, pipeline, _ = _runtime(store, model=SearchModel())
    pipeline.effect_store = ConfirmationEffectPlanStore(SQLiteConfirmationStore())
    session.journal = store
    return session, store


async def search(session, *, telegram=False):
    request = QueryEnvelope(
        owner=OWNER,
        session_id=SESSION,
        message="搜索 Example，挑选两项资源供预览，先不要下载",
        channel="web",
    )
    if telegram:
        view = await TelegramKernelTransport(session).query(request)
    else:
        view = await WebKernelTransport(session).query_view(request)
    assert view.status == "success" and not view.error_code, view.to_dict()
    assert view.approval is None
    assert view.candidate_view is not None, view.to_dict()
    assert view.candidate_view["explicit_selection"] is True
    assert len(view.candidate_view["items"]) == 2
    assert [item["position"] for item in view.candidate_view["items"]] == [1, 2]
    return view.candidate_view


async def prepare(session, candidates):
    view = await WebKernelTransport(session).query_view(
        QueryEnvelope(
            owner=OWNER,
            session_id=SESSION,
            message="提交两项资源到两个目标",
            channel="web",
            selection=_selection(candidates, [2, 1], "both"),
        )
    )
    assert view.status == "approval_required" and view.approval
    return view.approval.plan_id


@pytest.mark.parametrize("telegram", [False, True])
def test_normal_web_and_telegram_share_search_facts_without_downloading(
    offline_chain, telegram
):
    async def exercise():
        session, store = runtime()
        candidates = await search(session, telegram=telegram)
        state = await store.load(owner=OWNER, session_id=SESSION)
        restored = await current_candidate_view(state=state, store=store)
        assert restored["ref"] == candidates["ref"]
        assert len(restored["items"]) == 2
        events = await store.list_events(owner=OWNER, session_id=SESSION)
        assert events[-1]["type"] == "turn.completed"
        completed = [
            event["payload"] for event in events if event["type"] == "tool.completed"
        ]
        assert [payload["tool"] for payload in completed] == [
            "indexer.search_resources",
            "indexer.present_candidates",
        ]
        assert completed[0]["result"]["candidate_view"] is None
        assert completed[1]["result"]["candidate_view"] == candidates
        with db.get_conn() as conn:
            assert (
                conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0]
                == 0
            )
        for transport in offline_chain:
            transport.assert_not_called()

    asyncio.run(exercise())


def test_batch_cross_module_web_selection_telegram_confirmation_has_per_target_receipts(
    offline_chain,
):
    async def exercise():
        session, _store = runtime()
        plan = await prepare(session, await search(session))
        # 浏览器刷新后新 runtime，从持久确认/引用恢复，再使用同一事件适配的 TG 回执。
        restarted, store = runtime()
        view = await TelegramKernelTransport(restarted).confirm(
            EffectEnvelope(
                owner=OWNER,
                session_id=SESSION,
                plan_id=plan,
            )
        )
        assert view.status == "success" and not view.error_code
        items = view.effect_result["data"]["items"]
        assert [item["position"] for item in items] == [1, 2]
        assert all(
            set(item["succeeded"]) == {"qb", "guangya"} and not item["failed"]
            for item in items
        )
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT id,status,qb_status,gy_status FROM download_requests ORDER BY id"
            ).fetchall()
        assert len(rows) == 2
        assert {row["id"] for row in rows} == {item["request_id"] for item in items}
        assert all(
            row["status"] == row["qb_status"] == row["gy_status"] == "submitted"
            for row in rows
        )
        state = await store.load(owner=OWNER, session_id=SESSION)
        restored = await current_candidate_view(state=state, store=store)
        assert restored["last_result"]["handled_positions"] == [1, 2]
        assert restored["last_result"]["target"] == "both"
        assert all(
            f"下载请求 #{item['request_id']}" in restored["last_result"]["text"]
            for item in items
        )
        assert all(transport.call_count == 2 for transport in offline_chain)

    asyncio.run(exercise())


def test_restart_repeat_of_confirmation_preserves_completed_receipt(offline_chain):
    async def exercise():
        session, _store = runtime()
        plan = await prepare(session, await search(session))
        request = EffectEnvelope(owner=OWNER, session_id=SESSION, plan_id=plan)
        accepted = await WebKernelTransport(session).confirm_view(request)
        assert accepted.status == "success"
        restarted, store = runtime()
        original = await store.load(owner=OWNER, session_id=SESSION)
        for _ in range(2):
            replay = await TelegramKernelTransport(restarted).confirm(request)
            assert (
                replay.status == "failed"
                and replay.error_code == "confirmation_invalid"
            )
        restored = await store.load(owner=OWNER, session_id=SESSION)
        assert restored.conversation == original.conversation
        assert restored.pending_effect_plan_id == ""
        assert all(transport.call_count == 2 for transport in offline_chain)

    asyncio.run(exercise())


def test_history_restores_confirmed_result_after_candidate_expiry(offline_chain):
    async def exercise():
        session, store = runtime()
        plan = await prepare(session, await search(session))
        view = await WebKernelTransport(session).confirm_view(
            EffectEnvelope(owner=OWNER, session_id=SESSION, plan_id=plan)
        )
        items = view.effect_result["data"]["items"]
        # 只让短期引用过期；用户已完成的下载回执必须长期可读。
        with db.get_conn() as conn:
            conn.execute("UPDATE agent_kernel_refs SET expires_at=0")
        _, reloaded = runtime()
        state = await reloaded.load(owner=OWNER, session_id=SESSION)
        candidates = await current_candidate_view(state=state, store=reloaded)
        assert candidates is None
        messages = public_conversation_messages(
            state.conversation, candidate_view=candidates
        )
        assert not any(message.get("candidate_view") for message in messages)
        visible = "\n".join(message["content"] for message in messages)
        assert all(f"下载请求 #{item['request_id']}" in visible for item in items)
        assert all(transport.call_count == 2 for transport in offline_chain)

    asyncio.run(exercise())
