"""A 工作片：真实隔离落盘后的批量回执与历史恢复业务断言。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import database as db
from app.agent import indexer_actions
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.transports import QueryEnvelope
from app.agent.kernel.ux_selection import current_candidate_view
from app.agent.public_view import public_conversation_messages
from app.indexers.models import ResolvedDownload
from app.modules import download_dispatcher as dispatcher
from tests.support import isolated_test_database
from tests.test_agent_ux_backend import (
    OWNER,
    SECRET,
    SESSION,
    _events,
    _publish_candidates,
    _resources,
    _runtime,
    _selection,
)


@pytest.fixture
def isolated_chain():
    with isolated_test_database(), patch(
        "socket.socket.connect", side_effect=AssertionError("禁止外联")
    ):
        items = {item["result_id"]: SimpleNamespace(**item) for item in _resources().data["items"]}

        async def resolve(result_id, *, scope="manual"):
            digest = "a" if result_id.endswith("001") else "b"
            return ResolvedDownload("magnet", "magnet:?xt=urn:btih:" + digest * 40)

        service = SimpleNamespace(result_store=SimpleNamespace(get=items.__getitem__), get_result=lambda key, **_kw: items[key], resolve=resolve, enabled_site_ids=("demo",))
        with (
            patch.object(indexer_actions.config, "get_bool", return_value=True),
            patch.object(indexer_actions, "get_indexer_service", return_value=service),
            patch.object(indexer_actions, "download_target_readiness", return_value={"qb": True, "guangya": True}),
            patch.object(indexer_actions, "run_indexer_awaitable_sync", asyncio.run),
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
        assert f"下载请求 #{second_id}" in restored["last_result"]["text"]
        assert sum(bool(m.get("candidate_result_ref")) for m in messages) == 1
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


@pytest.mark.parametrize("count", [64, 650])
def test_provider_episode_inventory_preserves_declared_budget_and_truthful_completeness(isolated_chain, count):
    """正常/边界：真实媒体 transport 的完整清单不可在 Gateway 后静默只剩 32 集。"""
    from contextlib import nullcontext

    from app.agent.models import ToolContext
    from app.agent.provider_gateway import ProviderGateway
    from app.agent.provider_models import ProviderProfileView
    from app.agent.provider_operations import build_provider_catalog
    from app.agent.providers.media_server import MediaServerProviderTransport

    transport = MediaServerProviderTransport()
    gateway = ProviderGateway(catalog=build_provider_catalog(), transports=[transport])
    profile = SimpleNamespace(enabled=True, configured=True, label="测试媒体库", server_type="jellyfin")
    inventory = SimpleNamespace(episodes=[(1, n) for n in range(1, count + 1)], total=count,
                                truncated=False, ignored_specials=0, ignored_unknown=0)
    client = SimpleNamespace(list_series_episode_inventory=lambda *a, **kw: inventory)
    _, selected = gateway.artifacts.put(owner=OWNER, session_id=SESSION, provider="media",
        profile_ref="configured:media", operation="media.series.search",
        data={"series": [{"__object_id": "series-1", "__object_kind": "media_series", "name": "测试剧"}]})
    with (
        patch.object(transport, "profiles", return_value=[ProviderProfileView("configured:media", "media", "测试", "online")]),
        patch.object(transport, "_profile", return_value=profile),
        patch.object(transport, "_client", return_value=nullcontext(client)),
    ):
        result = gateway.query(profile_ref="configured:media", operation="media.series.episodes",
            arguments={"series_ref": selected["series"][0]["object_ref"], "max_episodes": count},
            context=ToolContext(owner=OWNER, session_id=SESSION))
    assert len(result.data["episodes"]) == min(count, 500)
    assert result.data["count"] == len(result.data["episodes"])
    assert result.data["truncated"] is (count > 500)
    assert result.data["total"] == count
    if count > 500:
        assert result.data["projection_truncated"] is True
        assert "截断" in result.summary


def test_provider_artifact_does_not_mint_invisible_objects_beyond_projection_budget(isolated_chain):
    """算法开销：默认只展示 32 项，不应给其余 968 项创建私有对象句柄。"""
    from app.agent.provider_artifacts import ProviderArtifactStore

    store = ProviderArtifactStore()
    with patch.object(store, "_ref", wraps=store._ref) as mint:
        _, result = store.put(owner=OWNER, session_id=SESSION, provider="demo",
            profile_ref="configured:demo", operation="demo.items",
            data={"items": [{"__object_id": str(n), "name": f"条目 {n}"} for n in range(1000)],
                  "count": 1000, "total": 1000, "truncated": False})
    assert len(result["items"]) == 32
    assert mint.call_count == 33, f"创建句柄次数：{mint.call_count}，但可见项目仅 32"
    assert result["truncated"] is True


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


def test_provider_extended_list_keeps_later_selection_resolvable(isolated_chain):
    """正常跨模块：目录允许 100 项时，第 50 项必须可见并能解析为后续操作对象。"""
    from dataclasses import replace

    from app.agent.models import ToolContext
    from app.agent.provider_catalog import ProviderCatalog
    from app.agent.provider_gateway import ProviderGateway
    from app.agent.provider_models import ProviderPayload
    from tests.test_agent_provider_gateway import _catalog, _FakeTransport

    catalog = ProviderCatalog()
    for spec in _catalog().operations():
        catalog.register(replace(spec, max_items=100))
    transport = _FakeTransport()
    gateway = ProviderGateway(catalog=catalog, transports=[transport])
    context = ToolContext(owner=OWNER, session_id=SESSION)
    payload = ProviderPayload(summary="50 项", source="demo", data={
        "items": [{"__object_id": f"raw-{n}", "__object_kind": "demo_item", "name": f"条目 {n}"} for n in range(50)],
        "count": 50, "total": 50, "truncated": False,
    })
    with patch.object(transport, "execute_read", return_value=payload):
        listed = gateway.query(profile_ref="configured:demo", operation="demo.items.list", arguments={}, context=context)
    assert len(listed.data["items"]) == listed.data["count"] == 50
    gateway.query(profile_ref="configured:demo", operation="demo.items.files",
        arguments={"item_ref": listed.data["items"][49]["object_ref"]}, context=context)
    assert transport.calls[-1][2] == {"item_ref": "raw-49"}
