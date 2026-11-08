"""订阅原生纵切：真实 SQLite/确认账本/工作区，元数据与通知用既有替身。"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

from app import database as db, sdk
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.pipeline import ToolPipeline
from app.modules.media_subscriptions import _ExpectedMedia
from app.repositories import media_experience as repository
from tests import test_automation_rules_native as fixtures
from tests.test_automation_rules_native import OWNER, context_for, executor

# 复用已隔离的元数据与假通知夹具。
backend = fixtures.backend


def seed(**values):
    return db.add_media_subscription(provider="tmdb", external_id="123", tmdb_id="123", media_type="tv", title="完整追更", **values)


def arguments_for(operation, identifier):
    return {"subscription_id": identifier, **({"enabled": False} if operation == "update" else {"enabled": True, "notify_on_satisfied": True} if operation == "set_notification" else {})}


def test_full_pages_real_ids_and_saved_errors_have_no_old_summary_limits(backend):
    removed = seed()
    assert db.delete_media_subscription(removed)
    ids = []
    for index in range(203):
        ids.append(db.add_media_subscription(provider="tmdb", external_id=str(1000 + index), tmdb_id=str(1000 + index), media_type="tv", title=f"完整{index}"))
    saved_error = "已保存故障详情" * 100
    with db.get_conn() as conn:
        conn.execute("UPDATE media_subscriptions SET last_error=? WHERE id=?", (saved_error, ids[-1]))
    page = sdk.subscriptions.list(limit=17)
    collected = []
    while True:
        assert page["total"] == 203
        collected.extend(page["items"])
        if not page["has_more"]:
            assert page["next"] is None
            break
        page = sdk.subscriptions.list(**page["next"])
    assert {row["id"] for row in collected} == set(ids)
    detail = sdk.subscriptions.get(ids[-1])
    assert detail["last_error"] == saved_error and detail["revision"] == 1
    assert detail == next(row for row in collected if row["id"] == ids[-1])
    backend.publish.assert_not_called()


def test_check_updates_pages_complete_results_without_persisting_runs(backend, monkeypatch):
    empty = sdk.subscriptions.check_updates()
    assert empty["status"] == "not_configured" and empty["total"] == empty["returned"] == 0
    assert empty["summary"] == "尚未创建媒体追更订阅"
    ids = [db.add_media_subscription(provider="tmdb", external_id=str(100 + i), tmdb_id=str(100 + i), media_type="tv", title=f"追更{i}") for i in range(19)]
    before = [dict(row) for row in db.list_media_subscriptions(limit=200)]
    preview = AsyncMock(side_effect=lambda identifier, **_: {"subscription_number": identifier, "status": "missing", "missing_count": 15,
        "missing": [{"season": 1, "episode": i} for i in range(1, 16)], "resource_search": {"status": "empty", "items": []}})
    monkeypatch.setattr(backend.subscriptions, "preview_subscription_updates", preview)
    page = sdk.subscriptions.check_updates(limit=8)
    items = []
    while True:
        assert page["read_only"]
        assert page["status"] == "updates_available"
        assert page["updates_available_count"] == page["returned"]
        items.extend(page["items"])
        if not page["has_more"]:
            break
        page = sdk.subscriptions.check_updates(**page["next"])
    assert {row["subscription_number"] for row in items} == set(ids)
    assert all(len(row["missing"]) == 15 for row in items)
    last = db.get_media_subscription(items[-1]["subscription_number"])
    assert last is not None
    beyond = sdk.subscriptions.check_updates(before_updated_at=last["updated_at"], before_id=last["id"])
    assert beyond["ok"] and beyond["read_only"] and beyond["status"] == "empty"
    assert beyond["summary"] == "本页无媒体追更订阅" and beyond["total"] == 19 and beyond["returned"] == 0
    assert beyond["items"] == [] and not beyond["has_more"] and beyond["next"] is None
    assert preview.await_count == 19
    assert [dict(row) for row in db.list_media_subscriptions(limit=200)] == before
    assert db.list_media_subscription_runs() == []
    backend.publish.assert_not_called()


@pytest.mark.parametrize("local_count", [1, 35], ids=["missing", "satisfied"])
def test_real_read_preview_keeps_all_aired_missing_and_paused_saved_missing(backend, monkeypatch, local_count):
    identifier = seed()
    expected = [_ExpectedMedia(f"tmdb:123:tv:S01E{i:03d}", season=1, episode=i) for i in range(1, 36)]
    monkeypatch.setattr(backend.subscriptions, "_expected_tv", AsyncMock(return_value=(expected, 7, 2)))
    monkeypatch.setattr("app.modules.media_subscriptions.inspect_series_episode_sources", lambda *a, **kw: [{"server_type": "jellyfin", "status": "ready", "episodes": [[1, i] for i in range(1, local_count + 1)]}])
    monkeypatch.setattr(backend.subscriptions, "_preview_search_missing_tv", AsyncMock(return_value=backend.subscriptions._empty_preview_search("empty")))
    checked = sdk.subscriptions.check_updates(identifier)
    item = checked["items"][0]
    assert checked["ok"] and checked["status"] == ("updates_available" if local_count < 35 else "up_to_date")
    assert checked["updates_available_count"] == int(local_count < 35)
    assert checked["up_to_date_count"] == int(local_count == 35)
    assert item["expected_count"] == 35 and item["local_count"] == local_count and item["missing_count"] == 35 - local_count
    assert len(item["missing"]) == 35 - local_count and item["future_count"] == 7 and item["unknown_air_date_count"] == 2
    last_checked_at = "2026-09-04T12:00:00+08:00"
    with db.get_conn() as conn:
        conn.execute("UPDATE media_subscriptions SET enabled=0,expected_count=35,local_count=?,missing_count=?,missing_json=?,last_checked_at=? WHERE id=?",
                     (local_count, item["missing_count"], json.dumps(item["missing"]), last_checked_at, identifier))
    db.add_media_subscription(provider="tmdb", external_id="456", tmdb_id="456", media_type="movie", title="暂停电影", enabled=False)
    before = [dict(row) for row in db.list_media_subscriptions(limit=200)]
    network = Mock(side_effect=AssertionError("暂停订阅不得访问元数据、媒体库或资源站"))
    for name in ("_tmdb_detail", "inspect_series_episode_sources", "inspect_media_identity_sources", "get_indexer_service"):
        monkeypatch.setattr("app.modules.media_subscriptions." + name, network)
    for subscription_id in (identifier, None):
        page = sdk.subscriptions.check_updates(subscription_id)
        assert page["ok"] and page["status"] == "inconclusive" and page["read_only"]
        assert "暂停" in page["summary"] and "未核对" in page["summary"]
        assert page["paused_count"] == page["returned"] == (1 if subscription_id is not None else 2)
        assert page["up_to_date_count"] == page["updates_available_count"] == 0
        paused = next(row for row in page["items"] if row["subscription_number"] == identifier)
        assert paused["status"] == "paused" and paused["missing"] == item["missing"]
        assert (paused["expected_count"], paused["local_count"], paused["missing_count"]) == (35, local_count, 35 - local_count)
        assert "历史快照" in paused["summary"] and paused["last_checked_at"] == last_checked_at
        assert paused["checked_at"] != last_checked_at
        assert all(row["status"] == "paused" and not row["inventory_complete"] and row["sources"] == []
                   and row["resource_search"] == backend.subscriptions._empty_preview_search("not_run") for row in page["items"])
    network.assert_not_called()
    assert [dict(row) for row in db.list_media_subscriptions(limit=200)] == before
    assert db.list_media_subscription_runs() == []
    assert db.list_media_subscription_candidates(identifier, statuses=(), limit=50) == []
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_download_admissions").fetchone()[0] == 0
    backend.publish.assert_not_called()


@pytest.mark.parametrize("include_paused", [False, True], ids=["checked-only", "with-paused"])
@pytest.mark.parametrize("checked_status", ["satisfied", "missing", "timeout"])
def test_check_updates_preserves_checked_results_and_paused_coverage(backend, monkeypatch, include_paused, checked_status):
    identifier = seed()
    paused_id = db.add_media_subscription(provider="tmdb", external_id="456", tmdb_id="456", media_type="tv", title="暂停追更", enabled=False) if include_paused else None
    before = [dict(row) for row in db.list_media_subscriptions(limit=200)]
    result = {"subscription_number": identifier, "status": checked_status, "inventory_complete": True,
              "missing_count": int(checked_status == "missing"), "resource_search": backend.subscriptions._empty_preview_search("not_run")}
    preview = AsyncMock(side_effect=TimeoutError() if checked_status == "timeout" else None, return_value=result)
    monkeypatch.setattr(backend.subscriptions, "_preview_tv", preview)
    page = sdk.subscriptions.check_updates()
    assert page["status"] == ("partial" if include_paused else {"satisfied": "up_to_date", "missing": "updates_available", "timeout": "unavailable"}[checked_status])
    assert page["ok"] is (include_paused or checked_status != "timeout")
    assert page["up_to_date_count"] == int(checked_status == "satisfied")
    assert page["updates_available_count"] == int(checked_status == "missing")
    assert page["inconclusive_count"] == int(checked_status == "timeout")
    assert page["paused_count"] == int(include_paused) and page["returned"] == 1 + int(include_paused)
    checked = next(row for row in page["items"] if row["subscription_number"] == identifier)
    assert checked["status"] == checked_status
    if checked_status != "timeout":
        assert checked == result
    if include_paused:
        assert "暂停" in page["summary"] and "未核对" in page["summary"]
        paused = next(row for row in page["items"] if row["subscription_number"] == paused_id)
        assert paused["status"] == "paused" and not paused["inventory_complete"]
    preview.assert_awaited_once()
    assert [dict(row) for row in db.list_media_subscriptions(limit=200)] == before
    assert db.list_media_subscription_runs() == []
    backend.publish.assert_not_called()


def test_enabled_control_can_pause_a_legacy_invalid_policy_without_rewriting_it(backend):
    identifier = seed()
    with db.get_conn() as conn:
        conn.execute("UPDATE media_subscriptions SET monitor_mode='selected',seasons_json='[]' WHERE id=?", (identifier,))

    async def run():
        effects = executor()
        context = await context_for(effects)
        plan = (await effects.prepare("subscriptions.update", {"subscription_id": identifier, "enabled": False}, context=context)).effect_plan
        assert plan is not None
        assert (await executor().execute_confirmed(plan.plan_id, context=context)).outcome.public_content["ok"]
        saved = sdk.subscriptions.get(identifier)
        assert not saved["enabled"] and saved["monitor_mode"] == "selected" and saved["seasons"] == []
        backend.publish.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["update", "delete", "set_notification", "reset_notification"])
@pytest.mark.parametrize("ending", ["confirm", "cancel"])
def test_cold_native_writes_keep_owner_scope_cancellation_and_real_rule_versions(backend, operation, ending):
    identifier = seed()
    if operation == "reset_notification":
        sdk.subscriptions.set_notification(identifier, enabled=False)
    before = sdk.subscriptions.get(identifier)
    rule = sdk.subscriptions.notification(identifier)

    async def run():
        effects = executor()
        context = await context_for(effects, session=operation + ending)
        plan = (await effects.prepare("subscriptions." + operation, arguments_for(operation, identifier), context=context)).effect_plan
        assert plan is not None
        assert sdk.subscriptions.get(identifier) == before and sdk.subscriptions.notification(identifier) == rule
        other = await context_for(effects, owner="webk:v1:" + "b" * 64, session=operation + ending)
        with pytest.raises(ToolPipelineError):
            await effects.execute_confirmed(plan.plan_id, context=other)
        cold = executor()
        pending = await cold.get_pending_plan(owner=OWNER, session_id=context.session_id)
        assert pending is not None and pending.plan_id == plan.plan_id
        if ending == "cancel":
            assert await cold.cancel_effect(plan.plan_id, lease=context.lease)
            assert sdk.subscriptions.get(identifier) == before and sdk.subscriptions.notification(identifier) == rule
        else:
            result = (await cold.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
            assert result["ok"]
            if operation == "delete":
                assert db.get_media_subscription(identifier) is None
            elif operation == "update":
                assert not sdk.subscriptions.get(identifier)["enabled"]
            else:
                saved = sdk.subscriptions.notification(identifier)
                assert saved["explicit"] is (operation == "set_notification")
                assert saved["revision"] == (1 if operation == "set_notification" else 0)
        with pytest.raises(ToolPipelineError):
            await executor().execute_confirmed(plan.plan_id, context=context)
        backend.publish.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["update", "delete", "set_notification", "reset_notification"])
def test_unknown_after_real_commit_is_consumed_and_never_replayed(backend, monkeypatch, operation):
    identifier = seed()
    if operation == "reset_notification":
        sdk.subscriptions.set_notification(identifier, enabled=True)

    async def run():
        effects = executor()
        context = await context_for(effects)
        plan = (await effects.prepare("subscriptions." + operation, arguments_for(operation, identifier), context=context)).effect_plan
        assert plan is not None
        target, method = ((backend.subscriptions, "update_subscription") if operation == "update" else
                          (backend.subscriptions, "delete_subscription") if operation == "delete" else
                          (repository, "set_notification_rule") if operation == "set_notification" else (repository, "reset_notification_rule"))
        original = getattr(target, method)

        def committed_then_lost(*args, **kwargs):
            original(*args, **kwargs)
            raise TimeoutError("committed acknowledgement lost")

        hook = Mock(side_effect=committed_then_lost)
        monkeypatch.setattr(target, method, hook)
        with pytest.raises(ToolPipelineError) as error:
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert error.value.code == "outcome_unknown" and error.value.execution_started is True
        with pytest.raises(ToolPipelineError):
            await executor().execute_confirmed(plan.plan_id, context=context)
        hook.assert_called_once()
        if operation == "delete":
            assert db.get_media_subscription(identifier) is None
        elif operation == "update":
            assert not sdk.subscriptions.get(identifier)["enabled"]
        else:
            assert sdk.subscriptions.notification(identifier)["explicit"] is (operation == "set_notification")
        backend.publish.assert_not_called()
    asyncio.run(run())


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="需要指定离线root镜像执行真实工作区")
@pytest.mark.parametrize("language", ["python", "shell"])
@pytest.mark.parametrize("operation", ["update", "delete", "set_notification", "reset_notification"])
def test_python_shell_real_rpc_reads_full_record_then_stops_at_cold_confirmation(backend, language, operation):
    identifier = seed()
    if operation == "reset_notification":
        sdk.subscriptions.set_notification(identifier, enabled=True)
    before = sdk.subscriptions.get(identifier)
    args = arguments_for(operation, identifier)

    async def run():
        effects = executor()
        context = await context_for(effects, session=language + operation)
        pipeline = ToolPipeline(catalog=effects.catalog, state_store=effects.state_store, reference_store=effects.reference_store, effect_store=effects.effect_store)
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        if language == "python":
            code = ('import json\nfrom pathlib import Path\nfrom mf import subscriptions\n'
                    f'json.dump(subscriptions.get({identifier}), open("facts.json", "w"))\n'
                    f'Path("before").write_text("once")\nsubscriptions.{operation}(**{args!r})\nPath("after").write_text("replayed")')
        else:
            options = " --no-enabled" if operation == "update" else " --enabled --notify-on-satisfied" if operation == "set_notification" else ""
            code = (f'mf subscriptions.get --subscription-id {identifier} > facts.json\necho once > before\n'
                    f'mf subscriptions.{operation} --subscription-id {identifier}{options}\necho replayed > after')
        try:
            result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": code, "language": language}, context=context)
            plan = result.effect_plan
            assert plan is not None and plan.tool_name == "subscriptions." + operation, result.outcome.public_content
            root, _ = sandbox._workspace_location(context.owner, context.session_id)
            assert json.loads((root / "work/facts.json").read_text()) == before
            assert (root / "work/before").read_text().strip() == "once" and not (root / "work/after").exists()
            assert sdk.subscriptions.get(identifier) == before
            assert (await executor().execute_confirmed(plan.plan_id, context=context)).outcome.public_content["ok"]
            assert not (root / "work/after").exists()
            with pytest.raises(ToolPipelineError):
                await executor().execute_confirmed(plan.plan_id, context=context)
            backend.publish.assert_not_called()
        finally:
            sandbox.clear_workspace(owner=context.owner, session_id=context.session_id)
    asyncio.run(run())
