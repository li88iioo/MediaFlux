"""真实映射身份沿原生 SDK 和持久确认账本流转，不再依赖候选序号上下文。"""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app import config, database as db, sdk
from app.repositories import discovery as discovery_repo
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.bootstrap import build_agent_kernel_runtime
from app.agent.kernel.capabilities import ToolCatalog, ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports import sdk_effects
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.state_types import _PendingEffectTransition
from app.agent.kernel.tool_calls import PreparedEffect
from app.discovery import service as discovery_service
from app.discovery.models import MediaCard
from app.discovery.registry import ProviderRegistry
from app.discovery.service import DiscoveryService
from tests.support import initialized_web_runtime
from tests.test_agent_kernel_core import ScriptedModel
from tests.test_agent_kernel_existing_ports import context_for

OWNER = "webk:v1:" + "a" * 64
ARGS = {"provider": "douban", "external_id": "101", "media_type": "movie", "tmdb_id": "90101"}


def source(provider="douban", external_id="101", media_type="movie"):
    return MediaCard(provider=provider, external_id=external_id, media_type=media_type,
                     title=f"来源影片-{external_id}", year="2026", original_title="Source title",
                     overview="完整来源详情", origin_country=("CN",), genre_ids=(16, 35))


def match(tmdb_id, media_type):
    return SimpleNamespace(tmdb_id=tmdb_id, title=f"已核验-{tmdb_id}", year="2026",
                           media_type=media_type, need_confirm=False)


@pytest.fixture
def backend(monkeypatch):
    with initialized_web_runtime():
        config.set_and_save({"DISCOVERY_ENABLED": "1"})
        detail = Mock(side_effect=lambda identity, media_type: source(external_id=identity, media_type=media_type))
        provider = SimpleNamespace(name="douban", get_detail=detail, close=Mock())
        scraper = SimpleNamespace(
            search_candidates=Mock(return_value=[SimpleNamespace(
                tmdb_id=str(90101 + i), title=f"候选-{i}", year="2026", media_type="movie", score=0.99,
            ) for i in range(8)]),
            match_from_tmdb=Mock(side_effect=match),
        )
        providers = {"douban": provider}
        for name in ("bangumi", "tmdb"):
            providers[name] = SimpleNamespace(name=name, close=Mock(), get_detail=Mock(
                side_effect=lambda identity, media_type, provider=name: source(provider, identity, media_type)))
        service = DiscoveryService(registry=ProviderRegistry(providers), scraper_factory=lambda: scraper)
        monkeypatch.setattr(sdk_effects, "get_discovery_service", lambda: service)
        monkeypatch.setattr(discovery_service, "get_discovery_service", lambda: service)
        try:
            yield SimpleNamespace(service=service, detail=detail, scraper=scraper)
        finally:
            service.shutdown(timeout_seconds=0.5)


def stored_mapping(external_id="101", provider="douban", media_type="movie"):
    row = discovery_repo.get_media_external_id(provider, external_id, media_type)
    assert row is not None
    return dict(row)


def executor(store):
    return EffectExecutor(catalog=ToolCatalog([sdk_effects.media_mapping_tool()]),
        state_store=store, reference_store=store,
        effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()),
        authorization=MediaFluxAuthorizationPolicy(), effect_lifecycle=MediaFluxEffectLifecycle())


def runtime():
    return build_agent_kernel_runtime(model=ScriptedModel([]), store=SQLiteKernelStore())


async def prepare(effects, context, arguments=None):
    result = await effects.prepare("media.confirm_mapping", arguments or ARGS, context=context)
    assert result.effect_plan is not None
    return result.effect_plan


def test_resolve_returns_every_real_candidate_without_saving(backend):
    detail = sdk.media.detail("douban", "movie", "101")["item"]
    assert detail["original_title"] == "Source title" and detail["mapping_confirmed"] is False
    result = sdk.media.resolve("douban", "101", "movie", detail["title"], detail["year"])
    assert len(result["candidates"]) == 8
    assert result["candidates"][-1] == {"tmdb_id": "90108", "title": "候选-7", "year": "2026", "media_type": "movie", "score": 0.99}
    assert not result["confirmed"] and discovery_repo.get_media_external_id("douban", "101", "movie") is None
    backend.scraper.match_from_tmdb.assert_not_called()
    with pytest.raises(ValueError, match="EffectExecutor"):
        sdk.media.confirm_mapping(**ARGS)
    assert discovery_repo.get_media_external_id("douban", "101", "movie") is None


def test_real_id_plans_stay_in_their_owner_and_session_after_restart(backend):
    async def exercise():
        store = SQLiteKernelStore()
        effects = executor(store)
        first = await context_for(store, owner=OWNER, session="session-a")
        second = await context_for(store, owner=OWNER, session="session-b")
        first_plan = await prepare(effects, first)
        second_args = {**ARGS, "external_id": "202", "tmdb_id": "90202"}
        second_plan = await prepare(effects, second, second_args)
        assert first_plan.snapshot_fingerprint.startswith("mfkr2:")
        public = first_plan.public_dict()
        assert dict(first_plan.arguments) == ARGS
        assert "已核验-90101" in json.dumps(public, ensure_ascii=False)
        assert discovery_repo.get_media_external_id("douban", "101", "movie") is None
        cold = executor(SQLiteKernelStore())
        for owner, session in [(OWNER, "other-session"), ("webk:v1:" + "b" * 64, "session-a")]:
            foreign = await context_for(store, owner=owner, session=session)
            with pytest.raises(ToolPipelineError):
                await cold.execute_confirmed(first_plan.plan_id, context=foreign)
        for context, plan, arguments in [(first, first_plan, ARGS), (second, second_plan, second_args)]:
            result = await cold.execute_confirmed(plan.plan_id, context=context)
            assert result.outcome.public_content["data"]["tmdb_id"] == arguments["tmdb_id"]
            row = discovery_repo.get_media_external_id("douban", arguments["external_id"], "movie")
            assert row is not None and row["tmdb_id"] == arguments["tmdb_id"] and row["confirmed"]
            before = dict(row)
            with pytest.raises(ToolPipelineError):
                await cold.execute_confirmed(plan.plan_id, context=context)
            assert stored_mapping(arguments["external_id"]) == before
        assert backend.scraper.search_candidates.call_count == 0
    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["reset", "delete"])
@pytest.mark.parametrize("worker", ["warm", "cold"])
def test_lifecycle_invalidates_only_its_native_mapping_plan(backend, operation, worker):
    async def exercise():
        first, controller = runtime(), runtime()
        a = await context_for(first.store, owner=OWNER, session="session-a")
        b = await context_for(first.store, owner=OWNER, session="session-b")
        plan_b = await prepare(first.session.pipeline, b, {**ARGS, "external_id": "202", "tmdb_id": "90202"})
        plan_a = await prepare(first.session.pipeline, a)
        await getattr(controller.lifecycle, operation)(owner=OWNER, session_id="session-a")
        receiver = first if worker == "warm" else runtime()
        fresh = await context_for(receiver.store, owner=OWNER, session="session-a")
        with pytest.raises(ToolPipelineError):
            await receiver.session.pipeline.execute_confirmed(plan_a.plan_id, context=fresh)
        result = await first.session.pipeline.execute_confirmed(plan_b.plan_id, context=b)
        assert result.outcome.public_content["ok"]
        assert discovery_repo.get_media_external_id("douban", "101", "movie") is None
        assert stored_mapping("202")["tmdb_id"] == "90202"
    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["reset", "delete"])
def test_lifecycle_surfaces_confirmation_revocation_failure(backend, operation):
    async def exercise():
        native = runtime()
        context = await context_for(native.store, owner=OWNER, session="mapping-session")
        plan = await prepare(native.session.pipeline, context)
        with patch.object(native.lifecycle.effect_store, "revoke_session", side_effect=OSError("confirmation storage failed")):
            with patch.object(native.store, operation + "_session", wraps=getattr(native.store, operation + "_session")) as change:
                with pytest.raises(OSError, match="confirmation storage failed"):
                    await getattr(native.lifecycle, operation)(owner=OWNER, session_id=context.session_id)
            change.assert_not_called()
        pending = await native.session.pipeline.get_pending_plan(owner=OWNER, session_id=context.session_id)
        assert pending is not None and pending.plan_id == plan.plan_id
        assert discovery_repo.get_media_external_id("douban", "101", "movie") is None
        await getattr(native.lifecycle, operation)(owner=OWNER, session_id=context.session_id)
        assert await native.session.pipeline.get_pending_plan(owner=OWNER, session_id=context.session_id) is None
    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["reset", "delete"])
def test_lifecycle_rejects_late_native_preview_without_harming_other_session(backend, operation):
    started, release = threading.Event(), threading.Event()

    def detail(identity, media_type):
        if identity == "101":
            started.set()
            assert release.wait(timeout=5)
        return source(external_id=identity, media_type=media_type)

    async def exercise():
        first, controller = runtime(), runtime()
        a = await context_for(first.store, owner=OWNER, session="session-a")
        b = await context_for(first.store, owner=OWNER, session="session-b")
        plan_b = await prepare(first.session.pipeline, b, {**ARGS, "external_id": "202", "tmdb_id": "90202"})
        backend.detail.side_effect = detail
        late = asyncio.create_task(first.session.pipeline.prepare("media.confirm_mapping", ARGS, context=a))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            await getattr(controller.lifecycle, operation)(owner=OWNER, session_id="session-a")
        finally:
            release.set()
        outcome = (await asyncio.gather(late, return_exceptions=True))[0]
        assert isinstance(outcome, Exception)
        assert await first.session.pipeline.get_pending_plan(owner=OWNER, session_id="session-a") is None
        assert (await first.session.pipeline.execute_confirmed(plan_b.plan_id, context=b)).outcome.public_content["ok"]
        assert discovery_repo.get_media_external_id("douban", "101", "movie") is None
    asyncio.run(exercise())


@pytest.mark.parametrize("field,value", [
    ("provider", "bangumi"), ("external_id", "999"), ("media_type", "tv"),
    ("title", "来源改名"), ("year", "2025"), ("missing", None),
])
def test_source_changes_never_save_the_frozen_mapping(backend, field, value):
    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        effects = executor(store)
        plan = await prepare(effects, context)
        backend.detail.side_effect = None
        backend.detail.return_value = None if field == "missing" else replace(source(), **{field: value})
        with pytest.raises(ToolPipelineError) as error:
            await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
        assert error.value.code == "confirmation_stale" and error.value.execution_started is False
        assert discovery_repo.get_media_external_id("douban", "101", "movie") is None
    asyncio.run(exercise())


@pytest.mark.parametrize("field,value", [
    ("tmdb_id", "999"), ("media_type", "tv"), ("title", "目标改名"), ("year", "2025"), ("need_confirm", True),
])
def test_reverified_target_must_equal_the_frozen_id_type_and_detail(backend, field, value):
    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        plan = await prepare(executor(store), context)
        changed = match(ARGS["tmdb_id"], ARGS["media_type"])
        setattr(changed, field, value)
        backend.scraper.match_from_tmdb.side_effect = None
        backend.scraper.match_from_tmdb.return_value = changed
        with pytest.raises(ToolPipelineError) as error:
            await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
        assert error.value.code == "confirmation_stale" and error.value.execution_started is False
        assert discovery_repo.get_media_external_id("douban", "101", "movie") is None
    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["insert", "replace", "aba", "delete"])
def test_mapping_cas_preserves_concurrent_changes_even_with_the_same_timestamp(backend, change):
    async def exercise():
        if change != "insert":
            discovery_repo.upsert_media_external_id("douban", "101", "movie", "100", "旧候选", "2025", 0.8, False)
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        plan = await prepare(executor(store), context)
        before = discovery_repo.get_media_external_id("douban", "101", "movie")
        with patch("app.database.now", return_value=str(before["updated_at"]) if before else "fixed-timestamp"):
            if change == "delete":
                with db.get_conn() as conn:
                    conn.execute("DELETE FROM media_external_ids WHERE provider='douban' AND external_id='101'")
            else:
                discovery_repo.upsert_media_external_id("douban", "101", "movie", "200", "并发确认", "2026", 1.0, True)
                if change == "aba":
                    discovery_repo.upsert_media_external_id("douban", "101", "movie", "100", "旧候选", "2025", 0.8, False)
            current = discovery_repo.get_media_external_id("douban", "101", "movie")
            expected = dict(current) if current is not None else None
            with pytest.raises(ToolPipelineError) as error:
                await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
        assert error.value.code == "confirmation_stale" and error.value.execution_started is False
        saved = discovery_repo.get_media_external_id("douban", "101", "movie")
        assert (dict(saved) if saved is not None else None) == expected
    asyncio.run(exercise())


@pytest.mark.parametrize("phase", ["prepare_source", "prepare_target", "confirm_source", "confirm_target", "cancel_ticket", "cancel_before_confirm"])
def test_cancellation_at_each_mapping_boundary_never_writes(backend, phase):
    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        effects = executor(store)

        def cancel_source(identity, media_type):
            context.cancellation.cancel()
            return source(external_id=identity, media_type=media_type)

        def cancel_target(tmdb_id, media_type):
            context.cancellation.cancel()
            return match(tmdb_id, media_type)

        if phase.startswith("prepare"):
            (backend.detail if phase.endswith("source") else backend.scraper.match_from_tmdb).side_effect = cancel_source if phase.endswith("source") else cancel_target
            with pytest.raises(asyncio.CancelledError):
                await effects.prepare("media.confirm_mapping", ARGS, context=context)
            assert await effects.get_pending_plan(owner=OWNER, session_id=context.session_id) is None
        else:
            plan = await prepare(effects, context)
            if phase == "cancel_ticket":
                assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
            elif phase == "cancel_before_confirm":
                context.cancellation.cancel()
            else:
                (backend.detail if phase.endswith("source") else backend.scraper.match_from_tmdb).side_effect = cancel_source if phase.endswith("source") else cancel_target
            with pytest.raises(ToolPipelineError if phase == "cancel_ticket" else asyncio.CancelledError):
                await effects.execute_confirmed(plan.plan_id, context=context)
        assert discovery_repo.get_media_external_id("douban", "101", "movie") is None
    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["config_aba", "agent_disabled"])
def test_runtime_and_authority_are_rechecked_before_mapping_write(backend, change):
    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        effects = executor(store)
        plan = await prepare(effects, context)
        if change == "config_aba":
            config.set_and_save({"DISCOVERY_ENABLED": "0"})
            config.set_and_save({"DISCOVERY_ENABLED": "1"})
        else:
            config.set_and_save({"AGENT_ENABLED": "0"})
        with pytest.raises(ToolPipelineError) as error:
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert error.value.code == ("confirmation_stale" if change == "config_aba" else "agent_disabled")
        assert error.value.execution_started is False
        assert discovery_repo.get_media_external_id("douban", "101", "movie") is None
    asyncio.run(exercise())


@pytest.mark.parametrize("arguments", [
    {**ARGS, "provider": "unknown"}, {**ARGS, "provider": "bangumi"}, {**ARGS, "media_type": "all"},
    {**ARGS, "external_id": ""}, {**ARGS, "external_id": True}, {**ARGS, "tmdb_id": True},
    {**ARGS, "tmdb_id": "١٢٣"}, {**ARGS, "tmdb_id": ""}, {**ARGS, "tmdb_id": "1" * 11},
])
def test_invalid_identity_never_publishes_a_confirmation(backend, arguments):
    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        effects = executor(store)
        with pytest.raises(ToolPipelineError):
            await effects.prepare("media.confirm_mapping", arguments, context=context)
        assert await effects.get_pending_plan(owner=OWNER, session_id=context.session_id) is None
        backend.detail.assert_not_called()
        backend.scraper.match_from_tmdb.assert_not_called()
    asyncio.run(exercise())


def test_existing_identical_confirmation_does_not_write_or_publish_a_plan(backend):
    discovery_repo.upsert_media_external_id("douban", "101", "movie", "90101", "已确认", "2026", 1.0, True)
    before = stored_mapping()

    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        effects = executor(store)
        result = await effects.prepare("media.confirm_mapping", ARGS, context=context)
        assert result.effect_plan is None and result.outcome.public_content["status"] == "no_changes"
        assert stored_mapping() == before
    asyncio.run(exercise())


def test_retired_candidate_number_ticket_never_executes_as_native(backend):
    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        effects = executor(store)
        ticket = effects.effect_store.freeze(owner=OWNER, session_id=context.session_id,
            generation=context.lease.generation, tool_name="discovery.confirm_mapping", effect=ToolEffect.WRITE,
            arguments={"candidate_number": 1}, prepared=PreparedEffect(
                preview={"ok": True, "summary": "旧映射确认"}, snapshot_fingerprint="retired-mapping", metadata={"risk": "low_write"}))
        await store.commit(context.lease, updates=(_PendingEffectTransition(
            "pending_effect_plan_id", ticket.plan_id, plan_generation=context.lease.generation,
            goal={"role": "user", "content": "旧映射确认目标"}),))
        cold = executor(SQLiteKernelStore())
        with pytest.raises(ToolPipelineError) as error:
            await cold.execute_confirmed(ticket.plan_id, context=context)
        assert error.value.code == "tool_not_found" and error.value.execution_started is False
        assert await cold.get_pending_plan(owner=OWNER, session_id=context.session_id) is None
        backend.detail.assert_not_called()
        assert discovery_repo.get_media_external_id("douban", "101", "movie") is None
    asyncio.run(exercise())


def test_unknown_result_is_never_replayed(backend):
    original = backend.service.confirm_tmdb_mapping_if_unchanged

    def reply_lost(*args, **kwargs):
        assert original(*args, **kwargs) is not None
        raise RuntimeError("lost reply after local CAS")

    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        effects = executor(store)
        plan = await prepare(effects, context)
        with patch.object(backend.service, "confirm_tmdb_mapping_if_unchanged", side_effect=reply_lost) as submit:
            with pytest.raises(ToolPipelineError) as error:
                await effects.execute_confirmed(plan.plan_id, context=context)
            assert error.value.code == "tool_execution_failed" and error.value.execution_started is True
            before = stored_mapping()
            with pytest.raises(ToolPipelineError):
                await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
            submit.assert_called_once()
            assert stored_mapping() == before
    asyncio.run(exercise())


@pytest.mark.parametrize("provider,media_type", [("douban", "tv"), ("bangumi", "tv"), ("tmdb", "movie")])
def test_real_source_provider_and_media_type_are_preserved(backend, provider, media_type):
    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        effects = executor(store)
        arguments = {"provider": provider, "external_id": "101", "media_type": media_type,
                     "tmdb_id": "101" if provider == "tmdb" else "90101"}
        result = await effects.prepare("media.confirm_mapping", arguments, context=context)
        if provider == "tmdb":
            assert result.effect_plan is None and result.outcome.public_content["status"] == "no_changes"
            assert sdk.media.resolve(provider, "101", media_type, "原生 TMDB 来源")["tmdb_id"] == "101"
            assert discovery_repo.get_media_external_id(provider, "101", media_type) is None
        else:
            assert result.effect_plan is not None
            completed = await effects.execute_confirmed(result.effect_plan.plan_id, context=context)
            assert completed.outcome.public_content["data"]["media_type"] == media_type
            assert stored_mapping("101", provider, media_type)["tmdb_id"] == "90101"
            assert discovery_repo.get_media_external_id(provider, "101", "movie") is None
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["source_mismatch", "missing_source", "target_mismatch", "tmdb_source_mismatch", "disabled"])
def test_unverifiable_mapping_never_creates_a_live_plan(backend, failure):
    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        effects = executor(store)
        arguments = ARGS
        if failure in {"source_mismatch", "missing_source"}:
            backend.detail.side_effect = None
            backend.detail.return_value = None if failure == "missing_source" else replace(source(), external_id="999")
        elif failure == "target_mismatch":
            backend.scraper.match_from_tmdb.side_effect = lambda *_args: match("999", "movie")
        elif failure == "tmdb_source_mismatch":
            arguments = {**ARGS, "provider": "tmdb"}
        else:
            config.set_and_save({"DISCOVERY_ENABLED": "0"})
        with pytest.raises(ToolPipelineError) as error:
            await effects.prepare("media.confirm_mapping", arguments, context=context)
        assert error.value.code == ("not_found" if failure == "missing_source" else "disabled" if failure == "disabled" else "precondition_failed")
        assert await effects.get_pending_plan(owner=OWNER, session_id=context.session_id) is None
        assert discovery_repo.get_media_external_id("douban", "101", "movie") is None
    asyncio.run(exercise())


def test_mapping_rate_budget_is_shared_across_workers_and_separates_confirmation(backend):
    from app.agent.kernel.ports.mediaflux_policy import MediaFluxToolRateLimiter

    async def exercise():
        for _ in range(4):
            await MediaFluxToolRateLimiter().acquire(owner=OWNER, tool_name="media.confirm_mapping", cost=1, arguments=ARGS)
        with pytest.raises(ToolPipelineError) as error:
            await MediaFluxToolRateLimiter().acquire(owner=OWNER, tool_name="media.confirm_mapping", cost=1, arguments=ARGS)
        assert error.value.code == "rate_limited"
        await MediaFluxToolRateLimiter().acquire(owner=OWNER, tool_name="confirm:media.confirm_mapping", cost=1, arguments=ARGS)
        assert discovery_repo.get_media_external_id("douban", "101", "movie") is None
    asyncio.run(exercise())


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="真实 Linux root 工作区验收")
def test_real_workspace_resolves_full_candidates_and_cold_confirms_without_script_replay(backend, monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))

    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        catalog = ToolCatalog([sdk_effects.media_mapping_tool()])
        pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store,
            effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()), authorization=MediaFluxAuthorizationPolicy())
        catalog.register(sandbox.run_code_tool(pipeline))
        result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": '''
from pathlib import Path
import json
from mf import media
with Path("runs.txt").open("a") as output:
    output.write("run\\n")
detail = media.detail("douban", "movie", "101")["item"]
resolved = media.resolve("douban", "101", "movie", detail["title"], detail["year"])
Path("candidates.json").write_text(json.dumps(resolved))
media.confirm_mapping("douban", "101", "movie", resolved["candidates"][-1]["tmdb_id"])
Path("tail.txt").write_text("must not resume")
'''}, context=context)
        plan = result.effect_plan
        assert plan is not None and plan.tool_name == "media.confirm_mapping"
        assert plan.arguments["tmdb_id"] == "90108"
        assert discovery_repo.get_media_external_id("douban", "101", "movie") is None
        root, _ = sandbox._workspace_location(context.owner, context.session_id)
        assert len(json.loads((root / "work/candidates.json").read_text())["candidates"]) == 8
        cold = executor(SQLiteKernelStore())
        assert (await cold.execute_confirmed(plan.plan_id, context=context)).outcome.public_content["ok"]
        before = stored_mapping()
        assert before["tmdb_id"] == "90108" and before["confirmed"]
        with pytest.raises(ToolPipelineError):
            await cold.execute_confirmed(plan.plan_id, context=context)
        assert stored_mapping() == before
        assert (root / "work/runs.txt").read_text() == "run\n"
        assert not (root / "work/tail.txt").exists()
        backend.scraper.search_candidates.assert_called_once()
        assert backend.detail.call_count == 3 and backend.scraper.match_from_tmdb.call_count == 2
    asyncio.run(exercise())
