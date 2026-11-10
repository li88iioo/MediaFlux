"""SDK 写入共用确认账本；测试真实预检/持久化，下载后端用替身。"""
from __future__ import annotations

from app.agent.kernel.ports.sdk_effects import cloud_organize_tools

import asyncio
import json
import os
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app import config, sdk
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.sdk_effects import indexer_configuration_tool, library_audit_job_tools, playback_tools, media_user_tools, library_patrol_tool, media_mapping_tool, release_format_save_tool, media_refresh_tools, activity_reorganize_tool, activity_undo_tool, activity_follow_tools, schedule_policy_tools, strm_run_tool, strm_failure_tools, share_transfer_tool, strm_metadata_tools, directory_scrape_tool, cloud_change_tool, cloud_recycle_tools, cloud_share_tools, download_submit_tool, download_retry_tool, preferences_tools, qb_control_tools, resource_download_tool, watchlist_tools
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.ports.sdk_effects import subscription_tools, notification_test_tool, policy_config_tool, feature_config_tool, local_task_tools, automation_tools, rss_delivery_tools, recognition_rule_tool, rss_configuration_tools, local_source_tools, recognition_knowledge_tools, media_path_mapping_tools
from app.agent.kernel import sandbox
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.clients.http import HttpSecurityError
from app.indexers.downloads import DownloadSnapshotChanged
from app.indexers.errors import IndexerResultExpired, IndexerResultNotFound
from app.indexers.models import IndexerItem
from app.indexers.providers.nyaa import NyaaAdapter
from app.indexers.registry import IndexerRegistry
from app.indexers.result_store import IndexerResultStore
from app.indexers.service import IndexerService
from app.sdk import WRITE_METHODS
from app.modules.episode_audit import EpisodeAuditResult
from tests.support import initialized_web_runtime, isolated_test_database


@pytest.fixture
def resources_backend(monkeypatch):
    from app.indexers.downloads import submit_indexer_download_batch

    monkeypatch.setenv("INDEXER_SEARCH_ENABLED", "true")
    service = IndexerService(
        registry=IndexerRegistry({site: NyaaAdapter(
            site_id=site, site_name=site, base_url=f"https://{site}.invalid",
            http=SimpleNamespace(), default_enabled=True,
        ) for site in ("nyaa", "sukebei")}),
        result_store=IndexerResultStore(), sukebei_authorized=True,
    )
    for number, identity in enumerate([f"candidate-{i:016d}" for i in range(20)] + ["candidate-000000000001"]):
        service.result_store.restore(identity, IndexerItem(
            title=f"作品 {identity}", site_id="nyaa", site_name="Nyaa",
            download_state="ready", download_kinds=("magnet",),
            magnet=f"magnet:?xt=urn:btih:{number:040x}",
        ))

    async def submit(service, result_ids, target, **kwargs):
        return {"ok": True, "status": "accepted", "summary": "整批请求已提交", "error": "",
                "total": len(result_ids), "target": target,
                "items": [{"result_id": identifier, "status": "submitted"} for identifier in result_ids]}

    with (isolated_test_database(),
          patch("app.agent.kernel.ports.sdk_effects.get_indexer_service", return_value=service),
          patch("app.indexers.downloads.guangya_download_credential_generation", return_value=1),
          patch("app.indexers.downloads.download_target_readiness", side_effect=lambda target: {
              name: True for name in ("qb", "guangya") if target in (name, "both")}),
          patch("app.indexers.downloads.submit_indexer_download_batch", new=AsyncMock(side_effect=submit)) as backend):
        backend.service = service
        backend.original_submit = submit_indexer_download_batch
        yield backend


async def context_for(store, owner="sdk-owner", session="sdk-session"):
    lease, _ = await store.begin_turn(owner=owner, session_id=session, request_id="sdk-write")

    async def progress(_):
        pass

    return ToolCallContext(owner=owner, session_id=session, request_id="sdk-write", turn_id=lease.turn_id,
                           lease=lease, cancellation=CancellationToken(), report_progress=progress)


def executor(store, *, authorization=None):
    catalog = ToolCatalog([*subscription_tools(), notification_test_tool(), policy_config_tool(), *cloud_organize_tools(), feature_config_tool(), *local_task_tools(), *automation_tools(), indexer_configuration_tool(), *library_audit_job_tools(), recognition_rule_tool(), *rss_configuration_tools(), *rss_delivery_tools(), *local_source_tools(), *media_path_mapping_tools(), *playback_tools(), *recognition_knowledge_tools(), *media_user_tools(), library_patrol_tool(), media_mapping_tool(), release_format_save_tool(), *media_refresh_tools(), activity_undo_tool(), activity_reorganize_tool(), *activity_follow_tools(), *schedule_policy_tools(), strm_run_tool(), *strm_failure_tools(), share_transfer_tool(), *strm_metadata_tools(), directory_scrape_tool(), resource_download_tool(), download_submit_tool(), download_retry_tool(), cloud_change_tool(), *qb_control_tools(), *cloud_recycle_tools(), *cloud_share_tools(), *watchlist_tools(), *preferences_tools()])
    return EffectExecutor(catalog=catalog, state_store=store, reference_store=store,
                          effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()), authorization=authorization)


@pytest.fixture
def activity_request():
    from app import database as db
    from app.repositories import activity as repository

    with isolated_test_database(), patch.object(repository, "notification_route_settings", side_effect=lambda owner, **_: {
        "notification_owner": owner, "notification_chat_id": "123",
    }), patch("socket.socket.connect", side_effect=AssertionError("no external service")):
        identifier, _ = db.create_download_request("native-follow", "magnet", title="关注示例")
        db.update_download_request(identifier, status="downloading", gy_status="downloading")
        yield identifier


def test_activity_follow_rpc_cold_confirmation_once_and_unfollow_nochange(activity_request, tmp_path):
    from app import database as db
    from app.repositories import media_automation_rules as rules
    from app.repositories.agent_jobs import agent_job_owner_digest

    async def check():
        context = await context_for(SQLiteKernelStore())
        (tmp_path / "run").mkdir()
        bridge = sandbox._SDKRequests(context, tmp_path, executor(SQLiteKernelStore()))
        bridge.uid = os.getuid()
        arguments = {"kind": "download", "identifier": activity_request, "hours": 2}
        request = tmp_path / "run/mf-requests/follow.json"
        request.write_text(json.dumps({"method": "activity.follow", "arguments": arguments}))
        try:
            await bridge.dispatch(request.name)
            assert bridge.proposal is not None and bridge.proposal.effect_plan is not None
            plan = bridge.proposal.effect_plan
            assert plan.arguments == arguments
            assert "notification_chat_id" not in json.dumps(plan.public_dict())
            with sdk.call_owner(context.owner):
                assert sdk.activity.follows()["data"]["items"] == []
        finally:
            await bridge.close()
        restored = executor(SQLiteKernelStore())
        pending = await restored.get_pending_plan(owner=context.owner, session_id=context.session_id)
        assert pending is not None and pending.plan_id == plan.plan_id
        result = await restored.execute_confirmed(plan.plan_id, context=context)
        saved = result.outcome.public_content["data"]
        assert saved["enabled"]
        with pytest.raises(ToolPipelineError):
            await restored.execute_confirmed(plan.plan_id, context=context)
        assert len(rules.list_rules(agent_job_owner_digest(context.owner), kind="activity_follow")) == 1
        with sdk.call_owner("other-owner"):
            assert sdk.activity.follows()["data"]["items"] == []
        request_row = db.get_download_request(activity_request)
        assert request_row is not None and request_row["status"] == "downloading"
        stop = (await restored.prepare("activity.unfollow", {"rule_id": saved["rule_id"]}, context=context)).effect_plan
        assert stop is not None
        await executor(SQLiteKernelStore()).execute_confirmed(stop.plan_id, context=context)
        with patch.object(rules, "save_rule", side_effect=AssertionError("nochange must not write")):
            unchanged = await restored.prepare("activity.unfollow", {"rule_id": saved["rule_id"]}, context=context)
            assert unchanged.effect_plan is None and unchanged.outcome.public_content["status"] == "no_changes"
            with sdk.call_owner(context.owner):
                assert sdk.activity.unfollow(saved["rule_id"])["status"] == "no_changes"

    asyncio.run(check())


@pytest.mark.parametrize("change", ["cancel", "cancellation", "foreign_owner", "foreign_session", "legacy_snapshot"])
def test_native_activity_follow_rejects_cancelled_or_wrong_frozen_scope(activity_request, change):
    from app.repositories import media_automation_rules as rules
    from app.repositories.agent_jobs import agent_job_owner_digest

    async def check():
        context = await context_for(SQLiteKernelStore())
        effects = executor(SQLiteKernelStore())
        if change == "legacy_snapshot":
            tool = effects.catalog.get("activity.follow")
            prepare = tool.prepare
            assert prepare is not None
            def legacy(arguments, context):
                prepared = prepare(arguments, context)
                return replace(prepared, metadata={})
            effects.catalog = ToolCatalog([indexer_configuration_tool(), replace(tool, prepare=legacy)])
        plan = (await effects.prepare("activity.follow", {
            "kind": "download", "identifier": activity_request,
        }, context=context)).effect_plan
        assert plan is not None
        if change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "cancellation":
            context.cancellation.cancel()
        elif change == "foreign_owner":
            context = await context_for(SQLiteKernelStore(), owner="other-owner")
        elif change == "foreign_session":
            context = await context_for(SQLiteKernelStore(), session="other-session")
        with pytest.raises((ToolPipelineError, asyncio.CancelledError)) as error:
            await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
        if change == "legacy_snapshot":
            assert isinstance(error.value, ToolPipelineError)
            assert error.value.code == "confirmation_stale" and error.value.execution_started is False
        assert rules.list_rules(agent_job_owner_digest("sdk-owner"), kind="activity_follow") == []

    asyncio.run(check())


def test_native_activity_follow_script_stops_at_card_and_is_not_replayed(activity_request, tmp_path, monkeypatch):
    if not sandbox.sandbox_supported():
        pytest.skip("需要Linux root工作区模板")
    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))

    async def check():
        context = await context_for(SQLiteKernelStore())
        effects = executor(SQLiteKernelStore())
        pipeline = ToolPipeline(catalog=effects.catalog, state_store=effects.state_store, effect_store=effects.effect_store)
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        code = ('from pathlib import Path\nfrom mf import activity\n'
                'Path("before").write_text("once")\n'
                f'activity.follow("download", {activity_request}, hours=2)\n'
                'Path("after").write_text("should not execute")')
        try:
            result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": code}, context=context)
            assert result.effect_plan is not None and result.effect_plan.tool_name == "activity.follow"
            root, _ = sandbox._workspace_location(context.owner, context.session_id)
            assert (root / "work/before").read_text() == "once" and not (root / "work/after").exists()
            await executor(SQLiteKernelStore()).execute_confirmed(result.effect_plan.plan_id, context=context)
            assert not (root / "work/after").exists()
            read = await pipeline.execute(sandbox.RUN_CODE_TOOL, {
                "code": 'from mf import activity\nrows=activity.follows()["data"]["items"]\nassert len(rows)==1 and rows[0]["enabled"]\nprint("follow persisted")',
            }, context=context)
            assert read.outcome.public_content["data"]["exit_code"] == 0
        finally:
            sandbox.clear_workspace(owner=context.owner, session_id=context.session_id)

    asyncio.run(check())


@pytest.fixture
def retry_backend(monkeypatch):
    from app import database as db
    from app.modules import download_dispatcher as dispatcher

    monkeypatch.setenv("QB_URL", "http://qb.invalid")
    with isolated_test_database(), patch.object(dispatcher, "_submit_qb", return_value={
        "ok": True, "task_id": "a" * 40,
    }) as backend, patch.object(dispatcher, "_submit_guangya", side_effect=AssertionError("unselected target")), \
         patch("socket.socket.connect", side_effect=AssertionError("no real download")):
        request_id, _ = db.create_download_request("retry-native", "magnet", title="Private.Show",
            source_value="magnet:?xt=urn:btih:" + "a" * 40, origin="agent")
        db.update_download_request(request_id, targets="qb", status="failed", qb_status="failed")
        backend.request_id = request_id
        yield backend


def test_native_retry_rpc_freezes_plan_and_confirmation_survives_restart_once(retry_backend, tmp_path):
    async def check():
        from app.agent.kernel.bootstrap import build_tool_catalog
        from app.agent.kernel.capabilities import ToolEffect

        assert "downloads.retry_submission" not in {tool.name for tool in build_tool_catalog().visible()}
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        assert effects.catalog.get("downloads.retry").effect is ToolEffect.DANGER
        (tmp_path / "run").mkdir()
        bridge = sandbox._SDKRequests(context, tmp_path, effects)
        bridge.uid = os.getuid()
        request = tmp_path / "run/mf-requests/retry.json"
        request.write_text(json.dumps({"method": "downloads.retry", "arguments": {
            "request_id": retry_backend.request_id, "target": "qb",
        }}))
        try:
            await bridge.dispatch(request.name)
            assert bridge.proposal is not None
            plan = bridge.proposal.effect_plan
            assert plan is not None
            assert plan.arguments == {"request_id": retry_backend.request_id, "target": "qb"}
            assert "magnet:" not in json.dumps(plan.public_dict())
            retry_backend.assert_not_called()
        finally:
            await bridge.close()
        rebuilt = executor(SQLiteKernelStore())
        restored = await rebuilt.get_pending_plan(owner=context.owner, session_id=context.session_id)
        assert restored is not None and restored.plan_id == plan.plan_id
        completed = await rebuilt.execute_confirmed(plan.plan_id, context=context)
        public = completed.outcome.public_content
        assert public["status"] == "submitted"
        assert public["data"]["source_request_id"] == retry_backend.request_id
        assert public["data"]["request_id"] != retry_backend.request_id
        assert public["data"]["succeeded"] == ["qb"]
        retry_backend.assert_called_once()
        with pytest.raises(ToolPipelineError):
            await rebuilt.execute_confirmed(plan.plan_id, context=context)
        retry_backend.assert_called_once()

    asyncio.run(check())


@pytest.mark.parametrize("change", ["cancel", "cancellation", "source", "config", "foreign_owner"])
def test_native_retry_cancel_or_frozen_drift_never_writes(retry_backend, monkeypatch, change):
    async def check():
        from app import database as db

        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        plan = (await effects.prepare("downloads.retry", {
            "request_id": retry_backend.request_id, "target": "qb",
        }, context=context)).effect_plan
        assert plan is not None
        if change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "cancellation":
            context.cancellation.cancel()
        elif change == "source":
            db.update_download_request(retry_backend.request_id, title="changed")
        elif change == "config":
            monkeypatch.setenv("TG_QB_SAVE_PATH", "/changed")
        else:
            context = await context_for(store, owner="other-owner")
        with pytest.raises((ToolPipelineError, asyncio.CancelledError)) as error:
            await effects.execute_confirmed(plan.plan_id, context=context)
        if change in {"source", "config"}:
            assert isinstance(error.value, ToolPipelineError)
            assert error.value.code == "confirmation_stale"
        retry_backend.assert_not_called()
        row = db.get_download_request(retry_backend.request_id)
        assert row is not None and row["qb_status"] == "failed"

    asyncio.run(check())


@pytest.mark.parametrize("requested,expected", [(None, "guangya"), ("preferred", "qb"), ("qb", "qb"), ("both", "both")])
@pytest.mark.parametrize("cache_state", ["empty", "expired"])
def test_entire_batch_survives_restart_and_confirmation_is_once_only(resources_backend, monkeypatch, requested, expected, cache_state):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        first = executor(store)
        service = resources_backend.service
        clock = [datetime.now(timezone.utc)]
        monkeypatch.setattr(service.result_store, "_clock", lambda: clock[0])
        assert isinstance(first.effect_store, ConfirmationEffectPlanStore)
        monkeypatch.setattr(first.effect_store.store, "_clock", lambda: clock[0].timestamp())
        # batch 是仅供 SDK 使用的执行端点，不进入模型可见工具目录。
        registered_writes = {tool.name for tool in first.catalog.visible() if tool.effect.value != "read"}
        registered_writes.add(first.catalog.get("session.batch").name)
        assert registered_writes == WRITE_METHODS
        ids = [f"candidate-{i:016d}" for i in range(20)]
        if requested is not None:
            with sdk.call_owner(context.owner):
                assert sdk.preferences.update(preferred_download_target="qb")["ok"]
        arguments = {"result_ids": ids, **({"target": requested} if requested is not None else {})}
        clock[0] += timedelta(seconds=300)
        prepared = await first.prepare("resources.download", arguments, context=context)
        resources_backend.assert_not_called()
        plan = prepared.effect_plan
        assert plan is not None
        assert len(plan.preview["data"]["resources"]) == 20
        assert plan.arguments == {"result_ids": ids, "target": expected}
        assert plan.preview["data"]["target"] == expected
        assert "magnet:?" not in json.dumps(plan.public_dict())
        with sdk.call_owner(context.owner):
            assert sdk.preferences.update(preferred_download_target="guangya" if expected != "guangya" else "qb")["ok"]
        original = [service.get_result(identity, scope="agent") for identity in ids]
        clock[0] += timedelta(seconds=301)
        if cache_state == "empty":
            service.result_store = IndexerResultStore(clock=lambda: clock[0])
        with pytest.raises(IndexerResultNotFound if cache_state == "empty" else IndexerResultExpired):
            service.get_result(ids[0], scope="agent")
        rebuilt = executor(SQLiteKernelStore())
        assert isinstance(rebuilt.effect_store, ConfirmationEffectPlanStore)
        monkeypatch.setattr(rebuilt.effect_store.store, "_clock", lambda: clock[0].timestamp())
        restored = await rebuilt.get_pending_plan(owner=context.owner, session_id=context.session_id)
        assert restored is not None and restored.plan_id == plan.plan_id
        assert restored.expires_at > clock[0].timestamp()
        with patch("app.modules.media_preferences.repository.get_media_preferences", side_effect=AssertionError("确认不得重读偏好")):
            completed = await rebuilt.execute_confirmed(plan.plan_id, context=context)
        assert completed.outcome.public_content["status"] == "accepted"
        resources_backend.assert_awaited_once()
        assert resources_backend.call_args.args[1:3] == (ids, expected)
        assert [service.get_result(identity, scope="agent") for identity in ids] == original
        with pytest.raises(ToolPipelineError):
            await rebuilt.execute_confirmed(plan.plan_id, context=context)
        resources_backend.assert_awaited_once()

    asyncio.run(check())


def test_native_download_freezes_every_episode_of_one_pack(resources_backend):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        identity = "candidate-0000000000000000"
        intents = [{"result_id": identity, "query": "用户别名", "tmdb_id": "123", "season": 1,
                    "episode": episode, "as_of": "2026-08-03"} for episode in (2, 3)]
        audit = EpisodeAuditResult(True, "updates_available", "缺集", {
            "title": "服务端标题", "tmdb_id": "123", "season": 1, "as_of": "2026-08-03",
            "missing_sample_truncated": False,
            "missing_sample": [{"season": 1, "episode": episode} for episode in (2, 3)],
        })
        with patch("app.indexers.downloads.episode_audit.audit_series_episodes", return_value=audit) as audited:
            prepared = await effects.prepare("resources.download", {
                "result_ids": [identity], "target": "qb", "verification_targets": intents,
            }, context=context)
        audited.assert_called_once()
        plan = prepared.effect_plan
        assert plan is not None
        assert plan.preview["data"]["verification_count"] == 2
        frozen = plan.metadata["verification_targets"]
        assert [item["episode"] for item in frozen[identity]] == [2, 3]
        assert all(item["title"] == "服务端标题" for item in frozen[identity])
        resources_backend.assert_not_called()
        rebuilt = executor(SQLiteKernelStore())
        with patch("app.indexers.downloads.episode_audit.audit_series_episodes", side_effect=AssertionError("确认使用冻结意图")):
            await rebuilt.execute_confirmed(plan.plan_id, context=context)
        resources_backend.assert_awaited_once()
        assert resources_backend.call_args.kwargs["verification_targets"] == frozen
        assert resources_backend.call_args.kwargs["verification_owner"] == context.owner

    asyncio.run(check())


@pytest.mark.parametrize("target", [None, "preferred"])
def test_cold_confirmation_rejects_unfrozen_target(resources_backend, target):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        tool = resource_download_tool()
        prepare = tool.prepare
        assert prepare is not None

        def unfrozen(arguments, context):
            prepared = prepare(arguments, context)
            return replace(prepared, arguments={"result_ids": arguments["result_ids"],
                                                **({"target": target} if target is not None else {})})

        effects.catalog = ToolCatalog([indexer_configuration_tool(), replace(tool, prepare=unfrozen)])
        plan = (await effects.prepare("resources.download", {
            "result_ids": ["candidate-000000000001"], "target": "qb",
        }, context=context)).effect_plan
        assert plan is not None
        with pytest.raises(ToolPipelineError) as error:
            await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
        assert error.value.code == "confirmation_stale"
        resources_backend.assert_not_called()

    asyncio.run(check())


@pytest.mark.parametrize("ending", ["cancel", "expired", "target_changed", "foreign_owner", "source_revoked", "candidate_changed"])
def test_unapproved_or_changed_snapshot_never_submits(resources_backend, monkeypatch, ending):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        service = resources_backend.service
        identity = "candidate-000000000001"
        if ending == "source_revoked":
            service.result_store.restore(identity, replace(service.result_store.get(identity), site_id="sukebei", site_name="Sukebei"))
        prepared = await effects.prepare("resources.download", {
            "result_ids": ["candidate-000000000001"], "target": "qb",
        }, context=context)
        plan = prepared.effect_plan
        assert plan is not None
        if ending == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif ending == "expired":
            service.result_store = IndexerResultStore()
            assert isinstance(effects.effect_store, ConfirmationEffectPlanStore)
            monkeypatch.setattr(effects.effect_store.store, "_clock", lambda: plan.expires_at + 1)
        elif ending == "target_changed":
            service.result_store = IndexerResultStore()
            monkeypatch.setenv("TG_QB_SAVE_PATH", "/different-destination")
        elif ending == "source_revoked":
            service.result_store = IndexerResultStore()
            service.sukebei_authorized = False
        elif ending == "candidate_changed":
            service.result_store.restore(identity, replace(service.result_store.get(identity), title="候选已变更"))
        else:
            context = await context_for(store, owner="other-owner")
        with pytest.raises(ToolPipelineError) as error:
            await effects.execute_confirmed(plan.plan_id, context=context)
        if ending in {"target_changed", "candidate_changed"}:
            assert error.value.code == "confirmation_stale"
        elif ending == "expired":
            assert error.value.code == "confirmation_invalid"
            with pytest.raises(IndexerResultNotFound):
                service.result_store.get(identity)
        elif ending == "source_revoked":
            assert error.value.code == HttpSecurityError.code
        if ending == "candidate_changed":
            assert service.result_store.get(identity).title == "候选已变更"
        resources_backend.assert_not_called()

    asyncio.run(check())


def test_schema_error_can_be_corrected_without_a_repair_ticket(resources_backend):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        for invalid in (
            {"result_ids": "candidate-000000000001", "target": "qb"},
            {"result_ids": ["candidate-000000000001"], "target": None},
            {"result_ids": ["candidate-000000000001"], "target": ""},
        ):
            with pytest.raises(ToolPipelineError):
                await effects.prepare("resources.download", invalid, context=context)
        assert await effects.get_pending_plan(owner=context.owner, session_id=context.session_id) is None
        prepared = await effects.prepare("resources.download", {
            "result_ids": ["candidate-000000000001"], "target": "qb",
        }, context=context)
        assert prepared.effect_plan is not None
        resources_backend.assert_not_called()

    asyncio.run(check())


def test_submit_revalidation_preserves_stale_error(resources_backend):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        prepared = await effects.prepare("resources.download", {
            "result_ids": ["candidate-000000000001"], "target": "qb",
        }, context=context)
        assert prepared.effect_plan is not None
        resources_backend.side_effect = DownloadSnapshotChanged()
        with pytest.raises(ToolPipelineError) as error:
            await effects.execute_confirmed(prepared.effect_plan.plan_id, context=context)
        assert error.value.code == "confirmation_stale"
        assert error.value.execution_started is False
        resources_backend.assert_awaited_once()

    asyncio.run(check())


def test_native_download_cancellation_during_resolution_never_submits(resources_backend, monkeypatch):
    from app import database as db
    from app.indexers import downloads
    from app.modules import download_dispatcher

    monkeypatch.setenv("QB_URL", "http://qb.invalid")

    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        service = resources_backend.service
        entered, release = threading.Event(), threading.Event()
        original = service.resolve

        async def resolve(*args, **kwargs):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
            return await original(*args, **kwargs)

        with patch.object(downloads, "submit_indexer_download_batch", new=resources_backend.original_submit), \
             patch.object(service, "resolve", new=resolve), \
             patch.object(download_dispatcher, "_submit_qb", return_value={"ok": True}) as backend, \
             patch("socket.socket.connect", side_effect=AssertionError("禁止外联")):
            plan = (await effects.prepare("resources.download", {
                "result_ids": ["candidate-000000000001"], "target": "qb",
            }, context=context)).effect_plan
            assert plan is not None
            task = asyncio.create_task(effects.execute_confirmed(plan.plan_id, context=context))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                backend.assert_not_called()
                context.cancellation.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
            backend.assert_not_called()
            with db.get_conn() as conn:
                assert conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0] == 0

    asyncio.run(check())


@pytest.mark.parametrize("method", ["downloads.submit", "downloads.retry"])
def test_native_download_cancel_during_shared_preflight_never_writes(retry_backend, method):
    from app import database as db
    from app.indexers import downloads
    from app.modules import download_dispatcher

    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        arguments = ({"url": "magnet:?xt=urn:btih:" + "d" * 40, "target": "qb"}
                     if method == "downloads.submit" else {"request_id": retry_backend.request_id, "target": "qb"})
        plan = (await effects.prepare(method, arguments, context=context)).effect_plan
        assert plan is not None
        target, name = ((downloads, "preview_download_url") if method == "downloads.submit"
                        else (download_dispatcher, "_download_retry_snapshot"))
        original = getattr(target, name)
        calls = 0

        def cancelled_preflight(*args, **kwargs):
            nonlocal calls
            result = original(*args, **kwargs)
            calls += 1
            # URL 的第二次复验位于共享提交服务，已越过 SDK 外层取消检查。
            if method == "downloads.retry" or calls == 2:
                context.cancellation.cancel()
            return result

        with patch.object(target, name, side_effect=cancelled_preflight):
            with pytest.raises(asyncio.CancelledError):
                await effects.execute_confirmed(plan.plan_id, context=context)
        retry_backend.assert_not_called()
        with db.get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0] == 1
        row = db.get_download_request(retry_backend.request_id)
        assert row is not None and row["qb_status"] == "failed"

    asyncio.run(check())


@pytest.mark.parametrize("owner,change", [
    ("webk:v1:" + "a" * 64, "unchanged"),
    ("webk:v1:" + "a" * 64, "config_aba"),
    ("tg:v1:-100123\x1f8", "unchanged"),
    ("tg:v1:-100123\x1f8", "revoked"),
])
@pytest.mark.parametrize("method", ["resources.download", "downloads.submit"])
def test_native_confirmation_rechecks_production_authority(resources_backend, owner, change, method):
    async def check():
        with initialized_web_runtime(), patch("app.modules.download_dispatcher._submit_qb", return_value={"ok": True}) as url_backend:
            config.set_and_save({"TG_AGENT_ENABLED": "1", "TG_CHAT_ID": "-100123",
                                 "TG_AGENT_ALLOWED_USER_IDS": "8", "TG_QB_SAVE_PATH": "/frozen-target", "QB_URL": "http://qb.invalid"})
            store = SQLiteKernelStore()
            context = await context_for(store, owner=owner)
            effects = executor(store, authorization=MediaFluxAuthorizationPolicy())
            arguments = ({"result_ids": ["candidate-000000000001"]} if method == "resources.download" else
                         {"url": "magnet:?xt=urn:btih:" + "b" * 40})
            prepared = await effects.prepare(method, {**arguments, "target": "qb"}, context=context)
            assert prepared.effect_plan is not None
            assert prepared.effect_plan.snapshot_fingerprint.startswith("mfkr2:")
            resources_backend.service.result_store = IndexerResultStore()
            if change == "config_aba":
                config.set_and_save({"TG_QB_SAVE_PATH": "/another-target"})
                config.set_and_save({"TG_QB_SAVE_PATH": "/frozen-target"})
            elif change == "revoked":
                config.set_and_save({"TG_AGENT_ALLOWED_USER_IDS": "9"})
            rebuilt = executor(SQLiteKernelStore(), authorization=MediaFluxAuthorizationPolicy())
            if change == "unchanged":
                result = await rebuilt.execute_confirmed(prepared.effect_plan.plan_id, context=context)
                assert result.outcome.public_content["status"] == "accepted"
                if method == "resources.download":
                    resources_backend.assert_awaited_once()
                else:
                    url_backend.assert_called_once()
            else:
                with pytest.raises(ToolPipelineError) as error:
                    await rebuilt.execute_confirmed(prepared.effect_plan.plan_id, context=context)
                assert error.value.code == ("confirmation_stale" if change == "config_aba" else "authorization_denied")
                resources_backend.assert_not_called()
                url_backend.assert_not_called()

    asyncio.run(check())


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="真实 Linux root 工作区容器验收")
@pytest.mark.parametrize("method", ["resources.download", "downloads.submit"])
def test_script_stops_at_approval_and_confirm_does_not_replay_script(resources_backend, monkeypatch, tmp_path, method):
    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))

    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        catalog = ToolCatalog([indexer_configuration_tool(), resource_download_tool(), download_submit_tool()])
        pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store,
                                effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))
        catalog.register(sandbox.run_code_tool(pipeline))
        invocation = ('resources.download([f"candidate-{i:016d}" for i in range(20)])' if method == "resources.download" else
                      'downloads.submit("magnet:?xt=urn:btih:" + "b" * 40, "qb")')
        result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": f'''
from pathlib import Path
from mf import resources, downloads
Path("before.txt").write_text("read completed")
{invocation}
Path("after.txt").write_text("must not run")
'''}, context=context)
        resources_backend.assert_not_called()
        assert result.effect_plan is not None
        assert result.effect_plan.tool_name == method
        assert result.effect_plan.arguments["target"] == ("guangya" if method == "resources.download" else "qb")
        assert result.outcome.public_content["status"] == "approval_required"
        root, _ = sandbox._workspace_location(context.owner, context.session_id)
        assert (root / "work/before.txt").read_text() == "read completed"
        assert not (root / "work/after.txt").exists()
        # 重建执行器不需要保存解释器/重放脚本，原冻结卡即可执行整批。
        rebuilt = executor(SQLiteKernelStore())
        await rebuilt.execute_confirmed(result.effect_plan.plan_id, context=context)
        if method == "resources.download":
            resources_backend.assert_awaited_once()
            assert len(resources_backend.call_args.args[1]) == 20
            assert resources_backend.call_args.args[2] == "guangya"
        else:
            url_backend.assert_called_once()
        assert not (root / "work/after.txt").exists()
        resumed = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code":
            'from pathlib import Path\nprint(Path("before.txt").read_text())'}, context=context)
        assert resumed.outcome.public_content["data"]["stdout"].strip() == "read completed"
        if method == "resources.download":
            resources_backend.assert_awaited_once()
        else:
            url_backend.assert_called_once()

    monkeypatch.setenv("QB_URL", "http://qb.invalid")
    with patch("app.modules.download_dispatcher._submit_qb", return_value={"ok": True}) as url_backend:
        asyncio.run(check())


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="真实 Linux root 工作区容器验收")
def test_parallel_write_requests_produce_one_approval(resources_backend, monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))

    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        catalog = ToolCatalog([resource_download_tool()])
        pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store,
                                effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))
        catalog.register(sandbox.run_code_tool(pipeline))
        result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": '''
from concurrent.futures import ThreadPoolExecutor
from mf import resources
with ThreadPoolExecutor(2) as pool:
    list(pool.map(lambda n: resources.download([f"candidate-{n:016d}"], "qb"), range(2)))
'''}, context=context)
        resources_backend.assert_not_called()
        assert result.effect_plan is not None
        pending = await pipeline.get_pending_plan(owner=context.owner, session_id=context.session_id)
        assert pending is not None
        assert pending.plan_id == result.effect_plan.plan_id
        assert len(pending.arguments["result_ids"]) == 1
        assert await pipeline.cancel_effect(pending.plan_id, lease=context.lease)
        assert await pipeline.get_pending_plan(owner=context.owner, session_id=context.session_id) is None
        resources_backend.assert_not_called()

    asyncio.run(check())


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="真实 Linux root 工作区容器验收")
def test_session_emits_confirmation_and_resumes_with_authoritative_receipt(resources_backend, monkeypatch, tmp_path):

    from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
    from app.agent.kernel.session import AgentSession
    from app.agent.kernel.state_types import AgentInput
    from tests.test_agent_kernel_core import ScriptedModel

    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))

    async def check():
        store = SQLiteKernelStore()
        catalog = ToolCatalog([resource_download_tool()])
        pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store,
                                effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))
        catalog.register(sandbox.run_code_tool(pipeline))
        model = ScriptedModel([
            [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("native-write", sandbox.RUN_CODE_TOOL, {
                "code": 'from mf import resources\nresources.download([f"candidate-{i:016d}" for i in range(20)], "qb")',
            })), ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
            [ModelEvent(ModelEventType.TEXT_DELTA, text="20 项下载已提交。"),
             ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
        ])
        session = AgentSession(model=model, catalog=catalog,
                               pipeline=pipeline, state_store=store)
        events = [event async for event in session.run(AgentInput(
            owner="sdk-owner", session_id="sdk-session", message="把这20个资源下载到qB"))]
        approvals = [event.payload for event in events if event.type.value == "effect.approval_required"]
        assert len(approvals) == 1
        assert approvals[0]["plan"]["tool_name"] == "resources.download"
        assert len(approvals[0]["plan"]["preview"]["data"]["resources"]) == 20
        assert events[-1].payload["status"] == "approval_required"
        resources_backend.assert_not_called()
        confirmed = [event async for event in session.confirm(
            owner="sdk-owner", session_id="sdk-session", plan_id=approvals[0]["plan"]["plan_id"])]
        assert confirmed[-1].payload["status"] == "success", confirmed[-1].payload
        resources_backend.assert_awaited_once()
        assert len(model.requests) == 2
        assert any('"accepted"' in message.content for message in model.requests[-1].messages if message.role == "tool")

    asyncio.run(check())


@pytest.mark.parametrize('url,kind', [
    ('magnet:?xt=urn:btih:' + 'b' * 40 + '&dn=Frozen.Show', 'magnet'),
    ('ed2k://|file|Frozen.Show.mkv|1234|' + 'b' * 32 + '|/', 'ed2k'),
    ('https://fixture.invalid/download/Frozen.Show.torrent?passkey=secret', 'http'),
])
def test_native_url_rpc_freezes_default_and_cold_confirm_is_once_only(retry_backend, tmp_path, url, kind):
    from app import database as db

    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        with sdk.call_owner(context.owner):
            sdk.preferences.update(preferred_download_target='qb')
        effects = executor(store)
        (tmp_path / 'run').mkdir()
        bridge = sandbox._SDKRequests(context, tmp_path, effects)
        bridge.uid = os.getuid()
        request = tmp_path / 'run/mf-requests/url.json'
        request.write_text(json.dumps({'method': 'downloads.submit', 'arguments': {'url': url}}))
        try:
            await bridge.dispatch(request.name)
            assert bridge.proposal is not None
            plan = bridge.proposal.effect_plan
            assert plan is not None
            assert plan.arguments == {'url': url, 'target': 'qb'}
            assert plan.preview['data']['kind'] == kind
            assert url not in json.dumps(plan.public_dict())
            assert 'passkey' not in json.dumps(plan.public_dict())
            retry_backend.assert_not_called()
            with db.get_conn() as conn:
                assert conn.execute('SELECT COUNT(*) FROM download_requests').fetchone()[0] == 1
        finally:
            await bridge.close()
        with sdk.call_owner(context.owner):
            sdk.preferences.update(preferred_download_target='guangya')
        rebuilt = executor(SQLiteKernelStore())
        restored = await rebuilt.get_pending_plan(owner=context.owner, session_id=context.session_id)
        assert restored is not None and restored.plan_id == plan.plan_id
        result = await rebuilt.execute_confirmed(plan.plan_id, context=context)
        assert result.outcome.public_content['status'] == 'accepted'
        data = result.outcome.public_content['data']
        assert data['target'] == 'qb' and data['kind'] == kind and data['created']
        row = db.get_download_request(data['request_id'])
        assert row is not None
        assert row['source_value'] == url and row['kind'] == kind
        # 保留旧 direct 行为：HTTP 原链接交由既有 dispatcher，不提前拉 torrent 或改变去重身份。
        assert row['torrent_data'] is None
        retry_backend.assert_called_once()
        with pytest.raises(ToolPipelineError):
            await rebuilt.execute_confirmed(plan.plan_id, context=context)
        retry_backend.assert_called_once()

    asyncio.run(check())


@pytest.mark.parametrize('change', ['cancel', 'cancellation', 'foreign_owner', 'unfrozen'])
def test_native_url_cancel_and_frozen_target_required(retry_backend, change):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        if change == 'unfrozen':
            tool = effects.catalog.get('downloads.submit')
            prepare = tool.prepare
            assert prepare is not None
            def unfrozen(arguments, context):
                prepared = prepare(arguments, context)
                return replace(prepared, arguments={**prepared.arguments, 'target': 'preferred'})
            effects.catalog = ToolCatalog([indexer_configuration_tool(), replace(tool, prepare=unfrozen)])
        plan = (await effects.prepare('downloads.submit', {
            'url': 'magnet:?xt=urn:btih:' + 'b' * 40, 'target': 'qb',
        }, context=context)).effect_plan
        assert plan is not None
        if change == 'cancel':
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == 'cancellation':
            context.cancellation.cancel()
        elif change == 'foreign_owner':
            context = await context_for(store, owner='other-owner')
        with pytest.raises((ToolPipelineError, asyncio.CancelledError)) as error:
            await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
        if change == 'unfrozen':
            assert isinstance(error.value, ToolPipelineError)
            assert error.value.code == 'confirmation_stale'
        retry_backend.assert_not_called()

    asyncio.run(check())


def test_native_url_unknown_acceptance_is_recorded_and_not_retried(retry_backend):
    from app import database as db

    async def check():
        retry_backend.side_effect = TimeoutError('unknown backend acceptance')
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        arguments = {'url': 'magnet:?xt=urn:btih:' + 'b' * 40, 'target': 'qb'}
        plan = (await effects.prepare('downloads.submit', arguments, context=context)).effect_plan
        assert plan is not None
        first = await effects.execute_confirmed(plan.plan_id, context=context)
        assert first.outcome.public_content['status'] == 'outcome_unknown'
        request_id = first.outcome.public_content['data']['request_id']
        row = db.get_download_request(request_id)
        assert row is not None and row['qb_status'] == 'outcome_unknown'
        rebuilt = executor(SQLiteKernelStore())
        second = (await rebuilt.prepare('downloads.submit', arguments, context=context)).effect_plan
        assert second is not None
        duplicate = await rebuilt.execute_confirmed(second.plan_id, context=context)
        assert duplicate.outcome.public_content['data']['duplicate']
        assert duplicate.outcome.public_content['data']['request_id'] == request_id
        row = db.get_download_request(request_id)
        assert row is not None and row['qb_status'] == 'outcome_unknown'
        retry_backend.assert_called_once()

    asyncio.run(check())


def test_native_url_edit_freezes_target_and_never_exposes_or_edits_url(retry_backend):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        url = 'https://fixture.invalid/download/Show.torrent?passkey=private'
        with (patch('app.indexers.downloads.download_target_readiness', side_effect=lambda target: {target: True}),
              patch('app.indexers.downloads.guangya_download_credential_generation', return_value=1)):
            original = (await effects.prepare('downloads.submit', {'url': url}, context=context)).effect_plan
            assert original is not None and original.arguments['target'] == 'guangya'
            editing = original.public_dict()['editing']
            assert set(editing['schema']['properties']) == {'target'}
            assert url not in json.dumps(editing)
            edited = (await effects.edit_confirmation(original.plan_id, {'target': 'qb'}, context=context)).effect_plan
            assert edited is not None and edited.arguments == {'url': url, 'target': 'qb'}
            with pytest.raises(ToolPipelineError):
                await effects.execute_confirmed(original.plan_id, context=context)
            result = await effects.execute_confirmed(edited.plan_id, context=context)
            assert result.outcome.public_content['data']['target'] == 'qb'
            pending = (await effects.prepare('downloads.submit', {'url': url}, context=context)).effect_plan
            assert pending is not None
            with pytest.raises(ToolPipelineError) as error:
                await effects.edit_confirmation(pending.plan_id, {'url': url + '-changed'}, context=context)
            assert error.value.code == 'confirmation_edit_invalid'
            with pytest.raises(ToolPipelineError):
                await effects.execute_confirmed(pending.plan_id, context=context)
        retry_backend.assert_called_once()

    asyncio.run(check())


@pytest.fixture
def native_library_backend(monkeypatch):
    from unittest.mock import MagicMock
    from app.modules import media_library
    from app.modules.media_server_profiles import MediaServerProfile

    profile = MediaServerProfile('configured:jellyfin', 'jellyfin', 'Jellyfin',
                                 'http://media.invalid', 'private-test-token', True, 'viewer')
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.list_virtual_folders.return_value = [{'id': 'anime', 'name': '动漫', 'collection_type': 'tvshows'}]
    client.refresh_library.return_value = True
    monkeypatch.setattr(media_library, 'list_configured_profiles', lambda: [profile])
    monkeypatch.setattr(MediaServerProfile, 'create_client', lambda _: client)
    with isolated_test_database():
        yield profile, client


def test_native_library_refresh_rpc_direct_with_durable_receipt(native_library_backend, tmp_path):
    _, client = native_library_backend

    async def check():
        context = await context_for(SQLiteKernelStore())
        (tmp_path / 'run').mkdir()
        bridge = sandbox._SDKRequests(context, tmp_path, executor(SQLiteKernelStore()))
        bridge.uid = os.getuid()
        request = tmp_path / 'run/mf-requests/refresh.json'
        request.write_text(json.dumps({'method': 'library.refresh', 'arguments': {'server': 'jellyfin', 'library_id': 'anime'}}))
        try:
            await bridge.dispatch(request.name)
            assert bridge.proposal is None and not bridge.approval_ready.is_set()
            response = json.loads((tmp_path / 'run/mf-responses/refresh.json').read_text())
            assert response['result']['status'] == 'accepted'
            assert response['result']['data']['library_id'] == 'anime'
            assert 'private-test-token' not in json.dumps(response)
        finally:
            await bridge.close()
        cold = executor(SQLiteKernelStore())
        pending = await cold.get_pending_plan(owner=context.owner, session_id=context.session_id)
        assert pending is None
        state = await cold.state_store.load(owner=context.owner, session_id=context.session_id)
        assert not state.metadata['active_publication'].get('protected')
        plan_id = state.metadata['confirmed_publication']['plan_id']
        with pytest.raises(ToolPipelineError):
            await cold.execute_confirmed(plan_id, context=context)
        client.refresh_library.assert_called_once_with('anime', mode='scan', replace_images=False, regenerate_trickplay=False)
        client.refresh_library_precise.assert_not_called()
        client.refresh.assert_not_called()
        assert await cold.state_store.cancel_publication(owner=context.owner, session_id=context.session_id,
                                                       request_id=context.request_id)
    asyncio.run(check())


@pytest.mark.parametrize('change', ['cancel', 'cancel_token', 'cancel_during_read', 'owner', 'session', 'target', 'profile', 'unknown'])
def test_native_library_refresh_frozen_scope_and_unknown_no_retry(native_library_backend, monkeypatch, change):
    from app.modules import media_library
    profile, client = native_library_backend

    async def check():
        context = await context_for(SQLiteKernelStore())
        effects = executor(SQLiteKernelStore())
        plan = (await effects.prepare('library.refresh', {'server': 'jellyfin', 'library_id': 'anime'}, context=context)).effect_plan
        assert plan is not None
        if change == 'cancel':
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == 'cancel_token':
            context.cancellation.cancel()
        elif change == 'cancel_during_read':
            def cancel_read():
                context.cancellation.cancel()
                return [{'id': 'anime', 'name': '动漫', 'collection_type': 'tvshows'}]
            client.list_virtual_folders.side_effect = cancel_read
        elif change == 'owner':
            context = await context_for(SQLiteKernelStore(), owner='other')
        elif change == 'session':
            context = await context_for(SQLiteKernelStore(), session='other')
        elif change == 'target':
            client.list_virtual_folders.return_value = []
        elif change == 'profile':
            monkeypatch.setattr(media_library, 'list_configured_profiles', lambda: [replace(profile, credential='changed')])
        else:
            client.refresh_library.side_effect = TimeoutError('private-upstream-token')
        with pytest.raises((ToolPipelineError, asyncio.CancelledError)) as caught:
            await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
        if change == 'unknown':
            assert isinstance(caught.value, ToolPipelineError)
            assert caught.value.execution_started is True
            assert 'private-upstream-token' not in str(caught.value)
            with pytest.raises(ToolPipelineError):
                await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
            client.refresh_library.assert_called_once_with('anime', mode='scan', replace_images=False, regenerate_trickplay=False)
        else:
            client.refresh_library.assert_not_called()
    asyncio.run(check())


def test_native_quality_wrapper_keeps_page_and_error_code(native_library_backend):
    from app.modules.library_quality import LibraryQualityError
    _, client = native_library_backend
    client._request.return_value = {'Items': [{'Id': f'item-{i}', 'Name': f'影片{i}', 'Type': 'Movie'} for i in range(31)], 'TotalRecordCount': 40}
    result = sdk.library.quality('jellyfin', 'anime', limit=31)
    assert len(result['data']['items']) == 31
    assert result['data']['items'][-1]['id'] == 'item-30'
    assert result['data']['has_more'] and result['data']['next_start_index'] == 31
    assert not result['data']['complete']
    client._request.return_value['TotalRecordCount'] = 20
    with pytest.raises(LibraryQualityError) as caught:
        sdk.library.quality('jellyfin', 'anime', limit=31)
    assert caught.value.code == 'provider_snapshot_changed'
    client.refresh_library.assert_not_called()


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason='真实 Linux root 工作区容器验收')
def test_library_native_workspace_reads_complete_pages_without_provider_refs(native_library_backend, tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))
    _, client = native_library_backend
    client._request.return_value = {'Items': [{'Id': f'item-{i}', 'Name': f'影片{i}', 'Type': 'Movie'} for i in range(31)], 'TotalRecordCount': 31}
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        catalog = ToolCatalog()
        pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store)
        catalog.register(sandbox.run_code_tool(pipeline))
        try:
            result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {'code': '''import json
from pathlib import Path
import mf
folders = mf.library.libraries(server="jellyfin")
result = mf.library.quality(server="jellyfin", library_id=folders["libraries"][0]["id"], limit=31)
Path("quality.json").write_text(json.dumps(result))
print(len(result["data"]["items"]), result["data"]["items"][-1]["id"])
'''}, context=context)
            assert result.effect_plan is None
            assert result.outcome.public_content['data']['exit_code'] == 0
            assert '31 item-30' in result.outcome.public_content['data']['stdout']
            again = await pipeline.execute(sandbox.RUN_CODE_TOOL, {'code': 'import json; from pathlib import Path; print(json.loads(Path("quality.json").read_text())["data"]["complete"])'}, context=context)
            assert again.outcome.public_content['data']['exit_code'] == 0
            assert 'True' in again.outcome.public_content['data']['stdout']
            client.refresh_library.assert_not_called()
        finally:
            sandbox.clear_workspace(owner=context.owner, session_id=context.session_id)
    asyncio.run(check())
