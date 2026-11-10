"""A 工作片：真实隔离落盘后的批量回执与历史恢复业务断言。"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app import database as db
from app.agent.confirmation import SQLiteConfirmationStore
from app import config as indexer_config
from app.agent.domains.resources.selection import current_candidate_view
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.public_view import public_conversation_messages
from app.agent.transports import QueryEnvelope
from app.indexers.models import ResolvedDownload
from app.modules import download_dispatcher as dispatcher
from tests.support import isolated_test_database, isolated_indexer_results
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
def isolated_chain():
    with (
        isolated_test_database(),
        isolated_indexer_results() as service,
        patch("socket.socket.connect", side_effect=AssertionError("禁止实际网络连接")),
    ):
        async def resolve(result_id, *, scope="manual"):
            digest = "a" if result_id.endswith("001") else "b"
            return ResolvedDownload("magnet", "magnet:?xt=urn:btih:" + digest * 40)

        service.resolve = resolve
        service.enabled_site_ids = ("demo",)
        with (
            patch.object(indexer_config, "get_bool", return_value=True),
            patch("app.indexers.downloads.download_target_readiness", return_value={"qb": True, "guangya": True}),
            patch("app.indexers.downloads.guangya_download_credential_generation", return_value=1),
            patch.object(dispatcher, "_submit_guangya", return_value={"ok": True, "task_id": "fake-gy"}) as gy,
            patch.object(dispatcher, "_submit_qb", return_value={"ok": True, "task_id": "fake-qb"}) as qb,
        ):
            yield gy, qb


def persistent_runtime():
    store = SQLiteKernelStore(secret_provider=lambda: SECRET)
    session, pipeline, _ = _runtime(store)
    pipeline.effect_store = ConfirmationEffectPlanStore(SQLiteConfirmationStore())
    return session, pipeline, store


async def prepare_selection(session, view, positions, target="guangya"):
    events = await _events(session.run(QueryEnvelope(
        owner=OWNER, session_id=SESSION, message="提交所选资源",
        selection=_selection(view, positions, target),
    ).to_agent_input()))
    plans = [e.payload["plan"]["plan_id"] for e in events if e.type is AgentEventType.EFFECT_APPROVAL_REQUIRED]
    assert len(plans) == 1, [(e.type, e.payload) for e in events]
    return plans[0]


async def confirm(session, plan):
    events = await _events(session.confirm(owner=OWNER, session_id=SESSION, plan_id=plan))
    results = [e.payload["result"] for e in events if e.type is AgentEventType.EFFECT_COMPLETED]
    assert len(results) == 1, [(e.type, e.payload) for e in events]
    return results[0]


def test_history_two_confirmations_keep_earlier_download_receipt_after_restart(isolated_chain):
    """历史恢复：两次真实提交后只折叠候选卡中已展示的最后一条回执。"""
    async def exercise():
        session, pipeline, store = persistent_runtime()
        view, _ = await _publish_candidates(pipeline, store)
        plan1 = await prepare_selection(session, view, [1])
        first = await confirm(session, plan1)
        plan2 = await prepare_selection(session, view, [2])
        second = await confirm(session, plan2)
        with db.get_conn() as conn:
            rows = conn.execute("SELECT id, status FROM download_requests ORDER BY id").fetchall()
        assert len(rows) == 2
        assert all(row["status"] == "submitted" for row in rows)
        first_id = first["data"]["items"][0]["request_id"]
        second_id = second["data"]["items"][0]["request_id"]
        assert first_id != second_id
        _, _, reloaded = persistent_runtime()
        state = await reloaded.load(owner=OWNER, session_id=SESSION)
        restored = await current_candidate_view(state=state, store=reloaded)
        messages = public_conversation_messages(state.conversation, candidate_view=restored)
        # 与 Web 恢复端一致：带当前 candidate_result_ref 的消息由候选卡取代。
        visible = [m["content"] for m in messages if not m.get("candidate_result_ref")]
        assert any(f"下载请求 #{first_id}" in text for text in visible), messages
        assert restored is not None
        assert f"下载请求 #{second_id}" in restored["last_result"]["text"]
        assert sum(bool(m.get("candidate_result_ref")) for m in messages) == 1
        folded = next(m for m in messages if m.get("candidate_result_ref"))
        assert "部分完成" in folded["candidate_followup"]
        assert "未完成的查询" in folded["candidate_followup"]
        assert f"下载请求 #{second_id}" not in folded["candidate_followup"]
    asyncio.run(exercise())


@pytest.mark.parametrize("same_text", [False, True])
def test_historical_candidate_results_fold_only_last_matching_receipt(same_text):
    """历史兼容：旧记录没有 plan ID；相同文本也只折叠一次。"""
    earlier = "回执甲" if not same_text else "回执乙"
    conversation = [
        {"role": "assistant", "tool_name": "ingest.submit", "content": text,
         "public_content": text, "candidate_result_ref": "ref_same"}
        for text in (earlier, "回执乙")
    ]
    messages = public_conversation_messages(conversation, candidate_view={
        "ref": "ref_same", "last_result": {"text": "回执乙"},
    })
    assert "candidate_result_ref" not in messages[0]
    assert messages[1]["candidate_result_ref"] == "ref_same"


def test_duplicate_confirmation_worker_cannot_overwrite_committed_success(isolated_chain):
    """重复确认：先获 scope 的 worker 执行，竞争者和迟到重放都不得改成功历史。"""
    import threading

    async def exercise():
        winner, pipeline, store = persistent_runtime()
        view, _ = await _publish_candidates(pipeline, store)
        plan = await prepare_selection(winner, view, [1])
        loser, _, _ = persistent_runtime()
        claim_entered, continue_claim = threading.Event(), threading.Event()
        real_claim = pipeline.effect_store.claim

        def delayed_claim(**kwargs):
            claim_entered.set()
            assert continue_claim.wait(5), "竞争确认未返回"
            return real_claim(**kwargs)

        with patch.object(pipeline.effect_store, "claim", side_effect=delayed_claim):
            task = asyncio.create_task(_events(winner.confirm(owner=OWNER, session_id=SESSION, plan_id=plan)))
            try:
                assert await asyncio.to_thread(claim_entered.wait, 5)
                rejected = await _events(loser.confirm(owner=OWNER, session_id=SESSION, plan_id=plan))
                assert any(e.payload.get("code") == "effect_in_progress" for e in rejected)
            finally:
                continue_claim.set()
                completed = await task
        result = next(e.payload["result"] for e in completed if e.type is AgentEventType.EFFECT_COMPLETED)
        rejected = await _events(loser.confirm(owner=OWNER, session_id=SESSION, plan_id=plan))
        assert any(e.payload.get("code") == "confirmation_invalid" for e in rejected)
        state = await store.load(owner=OWNER, session_id=SESSION)
        request_id = result["data"]["items"][0]["request_id"]
        assert any(f"下载请求 #{request_id}" in row.get("public_content", "") for row in state.conversation)
        assert not any(row.get("tool_name") == "confirmed_effect" for row in state.conversation)
        with db.get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0] == 1
        assert isolated_chain[0].call_count == 1
    asyncio.run(exercise())


def test_normal_preview_persists_confirmation_without_creating_download(isolated_chain):
    """正常：已落盘的待确认计划不能提前产生下载请求。"""
    async def exercise():
        session, pipeline, store = persistent_runtime()
        view, _ = await _publish_candidates(pipeline, store)
        plan = await prepare_selection(session, view, [1, 2], "both")
        _, _, restored = persistent_runtime()
        assert (await restored.load(owner=OWNER, session_id=SESSION)).pending_effect_plan_id == plan
        with db.get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0] == 0
        assert all(mock.call_count == 0 for mock in isolated_chain)
    asyncio.run(exercise())


@pytest.mark.parametrize("cloud_ok", [True, False])
def test_batch_both_targets_survives_preconfirm_restart_and_duplicate_replay(isolated_chain, cloud_ok):
    """批量跨模块/重启：两候选两目标，重建 runtime 确认一次，重复不新增请求。"""
    from app.agent.public_view import format_public_result

    gy, qb = isolated_chain
    gy.return_value = {"ok": cloud_ok, "task_id": "fake-gy"} if cloud_ok else {"ok": False, "error": "目标拒绝"}

    async def exercise():
        session, pipeline, store = persistent_runtime()
        view, _ = await _publish_candidates(pipeline, store)
        plan = await prepare_selection(session, view, [2, 1], "both")
        restarted, _, _ = persistent_runtime()
        result = await confirm(restarted, plan)
        assert result["status"] == ("accepted" if cloud_ok else "partial")
        items = result["data"]["items"]
        assert [item["position"] for item in items] == [1, 2]
        assert all(set(item["succeeded"]) == ({"qb", "guangya"} if cloud_ok else {"qb"}) for item in items)
        assert all(item["failed"] == ([] if cloud_ok else ["guangya"]) for item in items)
        text = format_public_result(result)
        assert all(f"下载请求 #{item['request_id']}" in text for item in items)
        if not cloud_ok:
            assert "部分目标成功" in text and "失败目标：光鸭云盘" in text
        again, _, _ = persistent_runtime()
        events = await _events(again.confirm(owner=OWNER, session_id=SESSION, plan_id=plan))
        assert any(e.payload.get("code") == "confirmation_invalid" for e in events)
        with db.get_conn() as conn:
            rows = conn.execute("SELECT status, qb_status, gy_status FROM download_requests").fetchall()
        assert len(rows) == 2
        # 请求主状态 submitted 表示至少一个目标受理；部分成功由目标列投影。
        assert all(row["status"] == "submitted" and row["qb_status"] == "submitted" for row in rows)
        assert all(row["gy_status"] == ("submitted" if cloud_ok else "failed") for row in rows)
        assert gy.call_count == qb.call_count == 2
    asyncio.run(exercise())
