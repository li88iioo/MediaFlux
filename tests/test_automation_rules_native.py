"""原生自动化：真实 SQLite/确认账本/RPC；所有元数据和通知均为替身。"""
from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from app import config, sdk
from app import database as db
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.ports.sdk_effects import automation_tools, subscription_tools
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.discovery.models import MediaCard
from app.modules import media_automation_rules as service
from app.modules import media_subscriptions as subscriptions
from app.repositories import media_automation_rules as rules
from app.repositories.agent_jobs import agent_job_owner_digest
from tests.support import initialized_web_runtime

OWNER = "webk:v1:" + "a" * 64
MEDIA = {"provider": "tmdb", "external_id": "123", "media_type": "tv", "check_interval_minutes": 10080, "season": 2, "action": "auto", "download_target": "qb", "sites": ["nyaa"]}


@pytest.fixture
def backend(monkeypatch):
    with initialized_web_runtime():
        config.set_and_save({"TG_CHAT_ID": "123"})
        monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("禁止真实网络")))
        monkeypatch.setattr(subscriptions, "get_discovery_service", lambda: SimpleNamespace(
            get_detail=lambda provider, kind, identity: MediaCard(provider=provider, media_type=kind, external_id=identity, title="完整作品")))
        monkeypatch.setattr(subscriptions, "_tmdb_detail", lambda *_: {"name": "完整作品", "first_air_date": "2026-01-01"})
        scheduler = Mock()
        monkeypatch.setattr("app.modules.media_subscription_scheduler.get_media_subscription_scheduler", lambda: scheduler)
        publish = Mock(return_value=SimpleNamespace(status="queued"))
        monkeypatch.setattr(service, "publish_notification_event", publish)
        yield SimpleNamespace(subscriptions=subscriptions.get_media_subscription_service(), scheduler=scheduler, publish=publish)


def executor():
    store = SQLiteKernelStore()
    return EffectExecutor(catalog=ToolCatalog((*automation_tools(), *subscription_tools())), state_store=store, reference_store=store,
        effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore(), record_actions=True),
        authorization=MediaFluxAuthorizationPolicy(), effect_lifecycle=MediaFluxEffectLifecycle())


async def context_for(effects, owner=OWNER, session="automation-native", route=None):
    lease, _ = await effects.state_store.begin_turn(owner=owner, session_id=session, request_id="automation")

    async def progress(_):
        pass

    return ToolCallContext(owner=owner, session_id=session, request_id=lease.request_id, turn_id=lease.turn_id,
        lease=lease, cancellation=CancellationToken(), report_progress=progress, delivery_route=route or {})


def saved_digest(**options):
    with sdk.call_owner(OWNER):
        return sdk.automation.set_digest(True, hour=21, minute=17, errors_only=True, send_empty=True, **options)["data"]


def test_sdk_complete_owner_scoped_ids_and_saved_corruption_errors(backend):
    owner = agent_job_owner_digest(OWNER)
    expected = []
    for _ in range(107):
        row = rules.save_rule(owner, kind="daily_summary", settings={"hour": 21, "minute": 17}, enabled=False,
                              next_run_at="2026-01-01T21:17:00+00:00")
        assert row is not None
        expected.append(row["id"])
    with db.get_conn() as conn:
        conn.execute("UPDATE media_automation_rules SET settings_json='null' WHERE id=?", (expected[-1],))
    with pytest.raises(ValueError):
        sdk.automation.digests()
    with sdk.call_owner(OWNER):
        rows = sdk.automation.digests()["data"]["items"]
        assert {item["rule_id"] for item in rows} == set(expected)
        broken = next(item for item in rows if item["rule_id"] == expected[-1])
        persisted = rules.get_rule(owner, expected[-1])
        assert persisted is not None and broken["settings_error"] == persisted["settings_error"]
        assert broken["settings"]["hour"] is None
        assert not {"owner_digest", "notification_chat_id", "lease_token"} & broken.keys()
        for injected in ({"chat_id": "999"}, {"delivery_route": {"chat_id": "999"}}, {"owner": "other"}):
            with pytest.raises(TypeError):
                sdk.METHODS["automation.set_digest"](True, hour=21, **injected)
    with sdk.call_owner("webk:v1:" + "b" * 64):
        assert sdk.automation.digests()["data"]["items"] == []
    backend.publish.assert_not_called()


def test_cold_digest_confirmation_owner_once_only_and_omitted_fields(backend):
    async def run():
        effects = executor()
        context = await context_for(effects)
        args = {"enabled": True, "hour": 21, "minute": 17, "errors_only": True, "send_empty": True}
        plan = (await effects.prepare("automation.set_digest", args, context=context)).effect_plan
        assert plan is not None and service.digests(owner=OWNER)["data"]["items"] == []
        assert "notification_chat_id" not in json.dumps(plan.public_dict())
        foreign = await context_for(effects, owner="webk:v1:" + "b" * 64)
        with pytest.raises(ToolPipelineError):
            await effects.execute_confirmed(plan.plan_id, context=foreign)
        cold = executor()
        pending = await cold.get_pending_plan(owner=OWNER, session_id=context.session_id)
        assert pending is not None and pending.plan_id == plan.plan_id
        saved = (await cold.execute_confirmed(plan.plan_id, context=context)).outcome.public_content["data"]
        with pytest.raises(ToolPipelineError):
            await executor().execute_confirmed(plan.plan_id, context=context)
        # 真实 ID 更新；省略 hour 不改变时间，停用无需现存目标。
        config.set_and_save({"TG_CHAT_ID": ""})
        stop = (await cold.prepare("automation.set_digest", {"rule_id": saved["rule_id"], "enabled": False}, context=context)).effect_plan
        assert stop is not None
        stopped = (await executor().execute_confirmed(stop.plan_id, context=context)).outcome.public_content["data"]
        assert stopped["rule_id"] == saved["rule_id"] and not stopped["enabled"]
        assert stopped["settings"] == saved["settings"] and stopped["revision"] == saved["revision"] + 1
        config.set_and_save({"TG_CHAT_ID": "123"})
        with sdk.call_owner(OWNER):
            reset = sdk.automation.set_digest(True, rule_id=saved["rule_id"], minute=0, errors_only=False, send_empty=False)["data"]
        assert reset["settings"] == {"hour": 21, "minute": 0, "errors_only": False, "send_empty": False}
        backend.publish.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["cancel", "session", "revision", "route", "disabled"])
def test_cancel_scope_revision_route_and_admission_never_overwrite(backend, change):
    saved = saved_digest()

    async def run():
        effects = executor()
        context = await context_for(effects)
        args = {"rule_id": saved["rule_id"], "enabled": True, "minute": 35}
        plan = (await effects.prepare("automation.set_digest", args, context=context)).effect_plan
        assert plan is not None
        if change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "session":
            context = await context_for(effects, session="other")
        elif change == "revision":
            with db.get_conn() as conn:
                conn.execute("UPDATE media_automation_rules SET revision=revision+1 WHERE id=?", (saved["rule_id"],))
        elif change == "route":
            config.set_and_save({"TG_CHAT_ID": "999"})
        else:
            config.set_and_save({"AGENT_ENABLED": "0"})
        with pytest.raises(ToolPipelineError):
            await executor().execute_confirmed(plan.plan_id, context=context)
        row = rules.get_rule(agent_job_owner_digest(OWNER), saved["rule_id"])
        assert row is not None
        assert row["settings"]["minute"] == 17
        backend.publish.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["owner", "session", "arguments", "snapshot"])
def test_native_effect_rejects_tampered_frozen_scope(backend, change):
    async def run():
        effects = executor()
        context = await context_for(effects)
        plan = (await effects.prepare("automation.set_digest", {"enabled": True, "hour": 21}, context=context)).effect_plan
        assert plan is not None
        altered: dict[str, Any] = {"owner": "foreign"} if change == "owner" else {"session_id": "foreign"} if change == "session" else {
            "arguments": {**plan.arguments, "hour": 9}} if change == "arguments" else {"metadata": {}}
        handler = effects.catalog.get(plan.tool_name).execute_confirmed
        assert handler is not None
        with pytest.raises(ToolPipelineError) as error:
            handler(dict(plan.arguments), plan.snapshot_fingerprint, replace(context, active_effect_plan=replace(plan, **altered)))
        assert error.value.execution_started is False
        assert service.digests(owner=OWNER)["data"]["items"] == []
    asyncio.run(run())


def test_digest_cas_rechecks_inside_existing_write_transaction(backend, monkeypatch):
    saved = saved_digest()
    original = rules.save_rule

    def raced(*args, **kwargs):
        with db.get_conn() as conn:
            conn.execute("UPDATE media_automation_rules SET revision=revision+1 WHERE id=?", (saved["rule_id"],))
        return original(*args, **kwargs)

    async def run():
        effects = executor()
        context = await context_for(effects)
        plan = (await effects.prepare("automation.set_digest", {"rule_id": saved["rule_id"], "enabled": False}, context=context)).effect_plan
        assert plan is not None
        monkeypatch.setattr(rules, "save_rule", raced)
        with pytest.raises(ToolPipelineError) as error:
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert error.value.execution_started is False
        row = rules.get_rule(agent_job_owner_digest(OWNER), saved["rule_id"])
        assert row is not None
        assert row["enabled"] and row["settings"]["minute"] == 17
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["digest", "media"])
def test_committed_unknown_is_consumed_and_never_replayed(backend, monkeypatch, kind):
    args = {"enabled": True, "hour": 21} if kind == "digest" else MEDIA
    method = "automation.set_digest" if kind == "digest" else "subscriptions.create"

    async def run():
        effects = executor()
        context = await context_for(effects)
        plan = (await effects.prepare(method, args, context=context)).effect_plan
        assert plan is not None
        if kind == "digest":
            original = rules.save_rule

            def lose(*args, **kwargs):
                original(*args, **kwargs)
                raise TimeoutError("committed result lost")

            hook = Mock(side_effect=lose)
            monkeypatch.setattr(rules, "save_rule", hook)
        else:
            original = backend.subscriptions.create_subscription

            async def lose(*args, **kwargs):
                await original(*args, **kwargs)
                raise TimeoutError("committed result lost")

            hook = Mock(side_effect=lose)
            monkeypatch.setattr(backend.subscriptions, "create_subscription", hook)
        with pytest.raises(ToolPipelineError) as error:
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert error.value.code == "outcome_unknown" and error.value.execution_started is True
        with pytest.raises(ToolPipelineError):
            await executor().execute_confirmed(plan.plan_id, context=context)
        assert hook.call_count == 1
        rows = rules.list_rules(agent_job_owner_digest(OWNER)) if kind == "digest" else backend.subscriptions.list_subscriptions()["items"]
        assert len(rows) == 1
        backend.publish.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("restored", [False, True])
def test_media_creation_and_restore_use_real_subscription_transaction(backend, restored):
    old_id = None
    if restored:
        old_id = db.add_media_subscription(provider="tmdb", external_id="123", tmdb_id="123", media_type="tv", title="旧作品", enabled=False)
        assert db.delete_media_subscription(old_id)

    async def run():
        effects = executor()
        context = await context_for(effects)
        plan = (await effects.prepare("subscriptions.create", MEDIA, context=context)).effect_plan
        assert plan is not None and backend.subscriptions.list_subscriptions()["items"] == []
        assert "持续自动提交" in plan.preview["data"]["effects"][1]
        backend.scheduler.reload.assert_not_called()
        saved = (await executor().execute_confirmed(plan.plan_id, context=context)).outcome.public_content["data"]
        row = backend.subscriptions.get_subscription(saved["subscription_number"])
        assert saved["id"] == row["id"] and (old_id is None or row["id"] == old_id)
        assert all(saved[key] == row[key] for key in row)
        assert row["seasons"] == [2] and row["monitor_mode"] == "selected" and row["action"] == "auto"
        assert row["download_target"] == "qb" and row["sites"] == ["nyaa"] and row["check_interval_minutes"] == 10080
        assert db.list_media_subscription_runs() == []
        backend.scheduler.reload.assert_called_once()
        with pytest.raises(ToolPipelineError):
            await effects.execute_confirmed(plan.plan_id, context=context)
        backend.publish.assert_not_called()
    asyncio.run(run())


def test_topic_route_is_host_bound_and_scheduled_notification_scope_is_unchanged(backend, monkeypatch):
    owner = "tg:v1:123\x1f456"
    route = {"chat_id": "123", "message_thread_id": 99}
    monkeypatch.setattr(service, "telegram_owner_route_is_currently_authorized", lambda *_args, **_kwargs: True)
    with sdk.call_owner(owner, delivery_route=route):
        saved = sdk.automation.set_digest(True, hour=21, send_empty=True)["data"]
    with sdk.call_owner(owner):
        sdk.automation.set_digest(False, rule_id=saved["rule_id"])
    row = rules.get_rule(agent_job_owner_digest(owner), saved["rule_id"])
    assert row is not None
    assert row["settings"]["notification_message_thread_id"] == 99
    with sdk.call_owner(owner, delivery_route=route):
        sdk.automation.set_digest(True, rule_id=saved["rule_id"])
    monkeypatch.setattr(service, "today_content_summary", dict)
    row = rules.get_rule(agent_job_owner_digest(owner), saved["rule_id"])
    assert row is not None
    from datetime import datetime
    assert service.drain_automation_rules(now=datetime.fromisoformat(row["next_run_at"])) == 1
    backend.publish.assert_called_once()
    assert backend.publish.call_args.kwargs["delivery_route"] == route
    assert not backend.publish.call_args.kwargs["deliver_now"]


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="需要可运行 Bubblewrap 的 Linux 隔离工作区")
@pytest.mark.parametrize("language", ["python", "shell"])
@pytest.mark.parametrize("kind", ["digest", "media"])
def test_native_rpc_reads_complete_file_then_cold_confirms_without_script_replay(backend, language, kind):
    saved = saved_digest()
    for _ in range(104):
        rules.save_rule(agent_job_owner_digest(OWNER), kind="daily_summary", settings={"hour": 8, "minute": 0},
                        enabled=False, next_run_at="2026-01-01T08:00:00+00:00")

    async def run():
        effects = executor()
        context = await context_for(effects, session=f"rpc-{language}-{kind}")
        pipeline = ToolPipeline(catalog=effects.catalog, state_store=effects.state_store, reference_store=effects.reference_store,
            effect_store=effects.effect_store, authorization=MediaFluxAuthorizationPolicy())
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        if language == "python":
            write = f'automation.set_digest(False, rule_id="{saved["rule_id"]}")' if kind == "digest" else 'subscriptions.create("tmdb", "123", "tv", action="auto", sites=["nyaa"], check_interval_minutes=10080)'
            code = ('import json\nfrom pathlib import Path\nfrom mf import automation, subscriptions\n'
                    'json.dump(automation.digests(), open("facts.json", "w"))\n'
                    f'Path("before").write_text("once")\n{write}\nPath("after").write_text("unexpected")')
        else:
            write = f'mf automation.set_digest --no-enabled --rule-id {saved["rule_id"]}' if kind == "digest" else 'mf subscriptions.create --provider tmdb --external-id 123 --media-type tv --action auto --sites nyaa --check-interval-minutes 10080'
            code = f'mf automation.digests > facts.json\necho once > before\n{write}\necho unexpected > after'
        try:
            result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": code, "language": language}, context=context)
            plan = result.effect_plan
            assert plan is not None, result.outcome.public_content
            expected = "automation.set_digest" if kind == "digest" else "subscriptions.create"
            assert plan.tool_name == expected
            root, _ = sandbox._workspace_location(context.owner, context.session_id)
            facts = json.loads((root / "work/facts.json").read_text())
            assert len(facts["data"]["items"]) == 105
            assert next(row for row in facts["data"]["items"] if row["rule_id"] == saved["rule_id"]) == saved
            assert (root / "work/before").read_text().strip() == "once" and not (root / "work/after").exists()
            row = rules.get_rule(agent_job_owner_digest(OWNER), saved["rule_id"])
            assert row is not None and row["enabled"]
            assert backend.subscriptions.list_subscriptions()["items"] == []
            pending = await executor().get_pending_plan(owner=OWNER, session_id=context.session_id)
            assert pending is not None and pending.plan_id == plan.plan_id
            assert (await executor().execute_confirmed(plan.plan_id, context=context)).outcome.public_content["ok"]
            assert not (root / "work/after").exists()
            with pytest.raises(ToolPipelineError):
                await executor().execute_confirmed(plan.plan_id, context=context)
            row = rules.get_rule(agent_job_owner_digest(OWNER), saved["rule_id"])
            assert row is not None and row["enabled"] is (kind == "media")
            assert len(backend.subscriptions.list_subscriptions()["items"]) == int(kind == "media")
            backend.publish.assert_not_called()
        finally:
            sandbox.clear_workspace(owner=context.owner, session_id=context.session_id)
    asyncio.run(run())
