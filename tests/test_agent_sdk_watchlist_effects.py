"""原生收藏写使用公共业务、真实 SQLite 确认账本；来源详情为离线替身。"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import config
from app.repositories import discovery as discovery_repo
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog, ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.events import AgentEventType as Event
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports import sdk_effects
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.ports.sdk_effects import watchlist_tools
from app.agent.kernel.scoped_trust import ScopedTrustPolicy
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput, CancellationToken, _PendingEffectTransition, _ScopedTrustTransition
from app.agent.kernel.tool_calls import ToolCallContext
from app.discovery.models import MediaCard
from app.discovery.registry import ProviderRegistry
from app.discovery.service import DiscoveryService
from tests.support import initialized_web_runtime
from tests.test_agent_kernel_core import ScriptedModel, collect

ADD = {"provider": "tmdb", "media_type": "movie", "external_id": "42"}
CARD = MediaCard(**ADD, title="预检标题", year="2026", poster_key="https://private.invalid/poster?api_key=secret")


@pytest.fixture
def backend(monkeypatch):
    with initialized_web_runtime():
        config.set_and_save({"DISCOVERY_ENABLED": "1"})
        provider = SimpleNamespace(name="tmdb", get_detail=Mock(return_value=CARD), close=Mock())
        service = DiscoveryService(registry=ProviderRegistry({"tmdb": provider}))
        monkeypatch.setattr(sdk_effects, "get_discovery_service", lambda: service)
        try:
            yield provider
        finally:
            service.shutdown(timeout_seconds=0.5)


async def context_for(store, *, owner="watchlist-owner"):
    lease, _ = await store.begin_turn(owner=owner, session_id="watchlist-session", request_id="watchlist-write")
    return ToolCallContext(owner=owner, session_id=lease.session_id, request_id=lease.request_id,
                           turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(),
                           report_progress=AsyncMock(), channel="web")


def executor(store, *, authorization=None):
    return EffectExecutor(
        catalog=ToolCatalog(watchlist_tools()), state_store=store, reference_store=store,
        effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()), authorization=authorization,
        effect_lifecycle=MediaFluxEffectLifecycle(),
    )


def test_add_freezes_metadata_and_confirms_once_after_restart(backend):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        prepared = await effects.prepare("watchlist.add", ADD, context=context)
        plan = prepared.effect_plan
        assert plan is not None and plan.tool_name == "watchlist.add"
        assert discovery_repo.get_media_watchlist("tmdb", "42", "movie") is None
        public = json.dumps(plan.public_dict(), ensure_ascii=False)
        assert "预检标题" in public
        assert all(secret not in public for secret in ("private.invalid", "api_key", "poster_key"))
        assert plan.public_dict()["trust_scope"] == {"provider": "tmdb", "media_type": "movie"}
        assert not await effects.scoped_trust.allows(plan, context=context)
        foreign = await context_for(store, owner="other-owner")
        with pytest.raises(ToolPipelineError):
            await effects.execute_confirmed(plan.plan_id, context=foreign)
        backend.get_detail.side_effect = AssertionError("确认不得再次查询来源")
        restarted = executor(SQLiteKernelStore())
        restored = await restarted.get_pending_plan(owner=context.owner, session_id=context.session_id)
        assert restored is not None and restored.plan_id == plan.plan_id
        completed = await restarted.execute_confirmed(plan.plan_id, context=context)
        assert completed.outcome.public_content["ok"] is True
        row = discovery_repo.get_media_watchlist("tmdb", "42", "movie")
        assert row is not None
        assert (row["title"], row["year"], row["poster_key"]) == (CARD.title, CARD.year, CARD.poster_key)
        assert completed.outcome.public_content["data"]["id"] == row["id"]
        assert "private.invalid" not in json.dumps(completed.outcome.public_content)
        backend.get_detail.assert_called_once_with("42", "movie")
        with pytest.raises(ToolPipelineError):
            await restarted.execute_confirmed(plan.plan_id, context=context)
        assert len(discovery_repo.list_media_watchlist()) == 1
    asyncio.run(check())


@pytest.mark.parametrize("operation", ["add", "remove"])
def test_no_changes_does_not_freeze_a_confirmation(backend, operation):
    async def check():
        if operation == "add":
            DiscoveryService.add_watchlist(CARD)
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        result = await effects.prepare(f"watchlist.{operation}",
            ADD if operation == "add" else {"watchlist_id": 12345}, context=context)
        assert result.effect_plan is None
        assert result.outcome.public_content["status"] == "no_changes"
        assert await effects.get_pending_plan(owner=context.owner, session_id=context.session_id) is None
        backend.get_detail.assert_not_called()
        assert len(discovery_repo.list_media_watchlist()) == (1 if operation == "add" else 0)
    asyncio.run(check())


@pytest.mark.parametrize("change", ["concurrent_add", "changed_snapshot", "recreated_row"])
def test_confirmation_conflicts_preserve_current_record(backend, change):
    async def check():
        number = DiscoveryService.add_watchlist(CARD) if change != "concurrent_add" else None
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        prepared = await effects.prepare("watchlist.add" if number is None else "watchlist.remove",
            ADD if number is None else {"watchlist_id": number}, context=context)
        assert prepared.effect_plan is not None
        if change == "recreated_row":
            assert DiscoveryService.remove_watchlist("tmdb", "movie", "42")
        DiscoveryService.add_watchlist(MediaCard(**ADD, title="并发标题", poster_key="changed-poster"))
        stored_row_1 = discovery_repo.get_media_watchlist("tmdb", "42", "movie")
        assert stored_row_1 is not None
        before = dict(stored_row_1)
        result = await effects.execute_confirmed(prepared.effect_plan.plan_id, context=context)
        assert result.outcome.public_content["ok"] is False
        assert result.outcome.public_content["status"] == "conflict"
        stored_row_2 = discovery_repo.get_media_watchlist("tmdb", "42", "movie")
        assert stored_row_2 is not None
        assert dict(stored_row_2) == before
    asyncio.run(check())


def test_remove_only_deletes_the_confirmed_record(backend):
    async def check():
        number = DiscoveryService.add_watchlist(CARD)
        other = DiscoveryService.add_watchlist(MediaCard(provider="douban", media_type="movie", external_id="42", title="另一来源"))
        assert number is not None and other is not None
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        prepared = await effects.prepare("watchlist.remove", {"watchlist_id": number}, context=context)
        assert prepared.effect_plan is not None
        assert discovery_repo.get_media_watchlist_by_id(number) is not None
        result = await effects.execute_confirmed(prepared.effect_plan.plan_id, context=context)
        assert result.outcome.public_content["data"]["id"] == number
        assert discovery_repo.get_media_watchlist_by_id(number) is None
        assert discovery_repo.get_media_watchlist_by_id(other) is not None
        backend.get_detail.assert_not_called()
    asyncio.run(check())


@pytest.mark.parametrize("operation,ending", [("add", "cancel_ticket"), ("remove", "cancel_execution")])
def test_cancel_never_writes(backend, operation, ending):
    async def check():
        number = DiscoveryService.add_watchlist(CARD) if operation == "remove" else None
        before = [dict(row) for row in discovery_repo.list_media_watchlist()]
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        prepared = await effects.prepare(f"watchlist.{operation}",
            ADD if operation == "add" else {"watchlist_id": number}, context=context)
        assert prepared.effect_plan is not None
        if ending == "cancel_ticket":
            assert await effects.cancel_effect(prepared.effect_plan.plan_id, lease=context.lease)
        else:
            context.cancellation.cancel()
        with pytest.raises(ToolPipelineError if ending == "cancel_ticket" else asyncio.CancelledError):
            await effects.execute_confirmed(prepared.effect_plan.plan_id, context=context)
        assert [dict(row) for row in discovery_repo.list_media_watchlist()] == before
    asyncio.run(check())


def test_cancel_during_source_lookup_does_not_publish_a_plan(backend):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        def cancelled_detail(*_args):
            context.cancellation.cancel()
            return CARD
        backend.get_detail.side_effect = cancelled_detail
        with pytest.raises(asyncio.CancelledError):
            await effects.prepare("watchlist.add", ADD, context=context)
        assert discovery_repo.list_media_watchlist() == []
        assert await effects.get_pending_plan(owner=context.owner, session_id=context.session_id) is None
    asyncio.run(check())


def test_confirmation_runtime_rejects_configuration_aba(backend):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store, owner="webk:v1:" + "a" * 64)
        effects = executor(store, authorization=MediaFluxAuthorizationPolicy())
        prepared = await effects.prepare("watchlist.add", ADD, context=context)
        assert prepared.effect_plan is not None
        assert prepared.effect_plan.snapshot_fingerprint.startswith("mfkr2:")
        config.set_and_save({"DISCOVERY_ENABLED": "0"})
        config.set_and_save({"DISCOVERY_ENABLED": "1"})
        with pytest.raises(ToolPipelineError) as error:
            await effects.execute_confirmed(prepared.effect_plan.plan_id, context=context)
        assert error.value.code == "confirmation_stale"
        assert error.value.execution_started is False
        assert discovery_repo.list_media_watchlist() == []
    asyncio.run(check())


@pytest.mark.parametrize("name,arguments", [
    ("watchlist.add", {"provider": "bangumi", "media_type": "movie", "external_id": "42"}),
    ("watchlist.remove", {"watchlist_id": True}),
])
def test_invalid_identity_never_prepares_a_write(backend, name, arguments):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        with pytest.raises(ToolPipelineError):
            await effects.prepare(name, arguments, context=context)
        assert await effects.get_pending_plan(owner=context.owner, session_id=context.session_id) is None
        backend.get_detail.assert_not_called()
        assert discovery_repo.list_media_watchlist() == []
    asyncio.run(check())


@pytest.mark.parametrize("arguments,scope", [
    (ADD, {"provider": "tmdb", "media_type": "movie"}),
    ({**ADD, "media_type": "tv"}, {"provider": "tmdb", "media_type": "tv"}),
    ({**ADD, "provider": "douban"}, None),
    ({**ADD, "external_id": "0"}, None),
    ({**ADD, "external_id": "1" * 19}, None),
    ({**ADD, "owner": "other"}, None),
])
def test_native_scope_keeps_existing_permission_boundaries(arguments, scope):
    native, remove = watchlist_tools()
    assert ScopedTrustPolicy.scope(native, arguments, native.metadata) == scope
    assert remove.approval_scope is None


def test_native_grant_reuses_scope_debits_once_and_never_authorizes_remove(backend):
    async def check():
        backend.get_detail.side_effect = lambda identity, media_type: MediaCard(
            provider="tmdb", external_id=identity, media_type=media_type, title=f"作品 {identity}",
        )
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        first = (await effects.prepare("watchlist.add", ADD, context=context)).effect_plan
        assert first is not None
        scope = {"provider": "tmdb", "media_type": "movie"}
        grant = await effects.scoped_trust.set(context=context, operation="grant", plan_id=first.plan_id,
            tool_name="watchlist.add", scope=scope, ttl_seconds=300, max_actions=5)
        other = (await effects.prepare("watchlist.add", {**ADD, "external_id": "43"}, context=context)).effect_plan
        assert other is not None and await effects.scoped_trust.allows(other, context=context)
        result = await effects.execute_confirmed(other.plan_id, context=context, scoped_trust=True)
        assert result.outcome.public_content["ok"] is True
        with pytest.raises(ToolPipelineError):
            await effects.execute_confirmed(other.plan_id, context=context, scoped_trust=True)
        state = await store.load(owner=context.owner, session_id=context.session_id)
        saved = dict(state.metadata["scoped_trust"])
        assert saved["remaining"] == 4 and saved["expires_at"] == grant["expires_at"]
        tv = (await effects.prepare("watchlist.add", {**ADD, "media_type": "tv"}, context=context)).effect_plan
        assert tv is not None and not await effects.scoped_trust.allows(tv, context=context)
        number = result.outcome.public_content["data"]["id"]
        remove = (await effects.prepare("watchlist.remove", {"watchlist_id": number}, context=context)).effect_plan
        assert remove is not None and not remove.public_dict().get("trust_eligible")
        with pytest.raises(ToolPipelineError):
            await effects.execute_confirmed(remove.plan_id, context=context, scoped_trust=True)
        with pytest.raises(ToolPipelineError) as denied:
            await effects.scoped_trust.set(context=context, operation="grant", plan_id=remove.plan_id,
                tool_name="watchlist.remove", scope=scope, ttl_seconds=300, max_actions=5)
        assert denied.value.code == "scoped_trust_denied"
        assert discovery_repo.get_media_watchlist_by_id(number) is not None
        assert (await store.load(owner=context.owner, session_id=context.session_id)).metadata["scoped_trust"] == saved
    asyncio.run(check())


def test_persisted_legacy_grant_migrates_with_the_existing_debit(backend):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        previous = {"owner": context.owner, "session_id": context.session_id,
                    "tool_name": "discovery.add_watchlist", "scope": {"provider": "tmdb", "media_type": "movie"},
                    "remaining": 3, "expires_at": time.time() + 120}
        await store.commit(context.lease, updates=(_ScopedTrustTransition("metadata.scoped_trust", previous),))
        restarted_store = SQLiteKernelStore()
        context = await context_for(restarted_store)
        native = executor(restarted_store)
        state = await restarted_store.load(owner=context.owner, session_id=context.session_id)
        assert native.scoped_trust.view(state)["tool_name"] == "watchlist.add"
        assert state.metadata["scoped_trust"] == previous
        current = (await native.prepare("watchlist.add", ADD, context=context)).effect_plan
        assert current is not None and await native.scoped_trust.allows(current, context=context)
        await native.execute_confirmed(current.plan_id, context=context, scoped_trust=True)
        saved = (await restarted_store.load(owner=context.owner, session_id=context.session_id)).metadata["scoped_trust"]
        assert saved == {**previous, "tool_name": "watchlist.add", "remaining": previous["remaining"] - 1}
        assert len(discovery_repo.list_media_watchlist()) == 1
    asyncio.run(check())


@pytest.mark.parametrize("retired", ["discovery.add_watchlist", "discovery.remove_watchlist"])
def test_unexpired_retired_ticket_never_executes_as_native(backend, retired):
    async def check():
        number = DiscoveryService.add_watchlist(CARD) if retired.endswith("remove_watchlist") else None
        before = [dict(row) for row in discovery_repo.list_media_watchlist()]
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        grant = {"owner": context.owner, "session_id": context.session_id,
                 "tool_name": "discovery.add_watchlist", "scope": {"provider": "tmdb", "media_type": "movie"},
                 "remaining": 3, "expires_at": time.time() + 120}
        await store.commit(context.lease, updates=(_ScopedTrustTransition("metadata.scoped_trust", grant),))
        ticket = effects.effect_store.freeze(owner=context.owner, session_id=context.session_id,
            generation=context.lease.generation, tool_name=retired, effect=ToolEffect.WRITE,
            arguments=ADD if number is None else {"watchlist_number": number},
            prepared=PreparedEffect(preview={"ok": True, "summary": "旧收藏确认卡"},
                                    snapshot_fingerprint="retired-watchlist-context", metadata={"risk": "low_write"}))
        await store.commit(context.lease, updates=(_PendingEffectTransition(
            "pending_effect_plan_id", ticket.plan_id, plan_generation=context.lease.generation,
            goal={"role": "user", "content": "旧收藏确认目标"},
        ),))
        restarted = executor(SQLiteKernelStore())
        pending = await restarted.get_pending_plan(owner=context.owner, session_id=context.session_id)
        assert pending is not None and pending.plan_id == ticket.plan_id and pending.expires_at > time.time()
        assert not await restarted.scoped_trust.allows(pending, context=context)
        with pytest.raises(ToolPipelineError):
            await restarted.execute_confirmed(ticket.plan_id, context=context, scoped_trust=True)
        with pytest.raises(ToolPipelineError) as failure:
            await restarted.execute_confirmed(ticket.plan_id, context=context)
        assert failure.value.code == "tool_not_found" and failure.value.execution_started is False
        assert await restarted.get_pending_plan(owner=context.owner, session_id=context.session_id) is None
        assert [dict(row) for row in discovery_repo.list_media_watchlist()] == before
        assert (await store.load(owner=context.owner, session_id=context.session_id)).metadata["scoped_trust"] == grant
        backend.get_detail.assert_not_called()
    asyncio.run(check())


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="真实 Linux root 工作区验收")
def test_run_code_nested_grant_confirms_without_replaying_script(backend, monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))
    backend.get_detail.side_effect = lambda identity, media_type: MediaCard(
        provider="tmdb", external_id=identity, media_type=media_type, title=f"作品 {identity}",
    )

    def script(label, identity):
        code = f'''from pathlib import Path
from mf import watchlist
with Path("runs.txt").open("a") as output:
    output.write("{label}\\n")
watchlist.add("tmdb", "tv", "{identity}")
Path("tail-{label}.txt").write_text("must not resume")
'''
        return [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                          tool_call=ModelToolCall(f"script-{label}", sandbox.RUN_CODE_TOOL, {"code": code})),
                ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]

    async def check():
        store = SQLiteKernelStore()
        catalog = ToolCatalog(watchlist_tools())
        pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store,
            effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()), effect_lifecycle=MediaFluxEffectLifecycle())
        catalog.register(sandbox.run_code_tool(pipeline))
        model = ScriptedModel([script("initial", "700"), script("auto", "701"), [
            ModelEvent(ModelEventType.TEXT_DELTA, text="第二部已加入探索收藏。"),
            ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
        ]])
        session = AgentSession(model=model, catalog=catalog,  pipeline=pipeline, state_store=store)
        owner, session_id = "native-script-owner", "native-script-session"
        first = await collect(session.run(AgentInput(owner=owner, session_id=session_id, message="收藏第一部电视剧")))
        plan = next(event.payload["plan"] for event in first if event.type is Event.EFFECT_APPROVAL_REQUIRED)
        assert plan["tool_name"] == "watchlist.add"
        await session.set_scoped_trust(owner=owner, session_id=session_id, operation="grant", plan_id=plan["plan_id"],
            tool_name="watchlist.add", scope={"provider": "tmdb", "media_type": "tv"}, ttl_seconds=300, max_actions=5)
        assert discovery_repo.list_media_watchlist() == []
        auto = await collect(session.run(AgentInput(owner=owner, session_id=session_id, message="改为收藏第二部电视剧")))
        assert not any(event.type is Event.EFFECT_APPROVAL_REQUIRED for event in auto)
        completed = [event for event in auto if event.type is Event.EFFECT_COMPLETED]
        assert len(completed) == 1 and completed[0].payload["scoped_trust"]["remaining"] == 4
        assert discovery_repo.get_media_watchlist("tmdb", "700", "tv") is None
        assert discovery_repo.get_media_watchlist("tmdb", "701", "tv") is not None
        root, _ = sandbox._workspace_location(owner, session_id)
        assert (root / "work/runs.txt").read_text() == "initial\nauto\n"
        assert not (root / "work/tail-initial.txt").exists()
        assert not (root / "work/tail-auto.txt").exists()
        assert backend.get_detail.call_count == 2 and len(model.requests) == 3
        assert (await store.load(owner=owner, session_id=session_id)).pending_effect_plan_id == ""
    asyncio.run(check())
