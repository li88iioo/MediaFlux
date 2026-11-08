"""偏好 SDK：真实 SQLite 确认、事务证据、旧回退凭证与原生工作区。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from app import config, database as db, sdk
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports import sdk_effects
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.repositories.media_experience import default_media_preferences, get_media_preferences, set_media_preferences
from tests.support import initialized_web_runtime

OWNER = "webk:v1:" + "a" * 64
CHANGES = {"preferred_resolution": "2160p", "minimum_resolution": "1080p", "memory_notes": ["动漫库叫 Anime"]}


@pytest.fixture
def runtime():
    with initialized_web_runtime():
        yield


async def context_for(store, owner=OWNER):
    lease, _ = await store.begin_turn(owner=owner, session_id="preferences-session", request_id="preferences-request")
    return ToolCallContext(owner=owner, session_id=lease.session_id, request_id=lease.request_id,
        turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=AsyncMock())


def executor(store, *, cls=EffectExecutor, lifecycle=None, confirmation_store=None):
    return cls(catalog=ToolCatalog([*sdk_effects.preferences_tools(), sdk_effects.activity_undo_tool()]),
        state_store=store, reference_store=store, authorization=MediaFluxAuthorizationPolicy(),
        effect_store=ConfirmationEffectPlanStore(confirmation_store or SQLiteConfirmationStore()),
        effect_lifecycle=lifecycle or MediaFluxEffectLifecycle())


def test_sdk_uses_host_owner_and_preserves_explicit_fields(runtime):
    with pytest.raises(ValueError):
        sdk.preferences.get()
    with sdk.call_owner(OWNER):
        assert not sdk.preferences.get()["explicit"]
        assert sdk.preferences.update(**CHANGES)["ok"]
        assert sdk.preferences.update(min_rating=0, exclude_played=False, preferred_genres=[])["ok"]
        saved = sdk.preferences.get()
        assert all(saved[key] == value for key, value in CHANGES.items())
        assert "revision_token" not in saved and "revision" not in saved
        for invalid in ({"preferred_resolution": None}, {}, {"owner": "other"}, {"minimum_resolution": "8k"}):
            with pytest.raises((ValueError, TypeError)):
                sdk.METHODS["preferences.update"](**invalid)
        assert sdk.preferences.get() == saved
    with sdk.call_owner("other"):
        assert not sdk.preferences.get()["explicit"]
        assert not sdk.preferences.clear()["ok"]
    with sdk.call_owner(OWNER):
        assert sdk.preferences.clear()["ok"]
        assert not sdk.preferences.get()["explicit"]


def test_sdk_download_resolves_host_preference_only_during_preflight(runtime, monkeypatch):
    from app.indexers import downloads, runtime as indexer_runtime

    service = Mock()
    preview = Mock(side_effect=lambda service, ids, target, **kwargs: ({"ok": True}, {"target": target}))
    submit = AsyncMock(return_value={"ok": True, "status": "accepted"})
    monkeypatch.setattr(indexer_runtime, "get_indexer_service", lambda: service)
    monkeypatch.setattr(downloads, "preview_indexer_download_batch", preview)
    monkeypatch.setattr(downloads, "submit_indexer_download_batch", submit)
    with sdk.call_owner(OWNER):
        assert sdk.preferences.update(preferred_download_target="qb")["ok"]
    arguments: dict[str, Any]
    for owner, arguments, target in (
        (OWNER, {}, "qb"), ("other", {}, "guangya"),
        (OWNER, {"target": "preferred"}, "qb"), (OWNER, {"target": "both"}, "both"),
    ):
        with sdk.call_owner(owner):
            assert sdk.resources.download(["candidate"], **cast(Any, arguments))["status"] == "accepted"
        preview.assert_called_with(service, ["candidate"], target, scope="agent")
        submit.assert_awaited_with(service, ["candidate"], target, expected_snapshot={"target": target}, scope="agent",
                                  verification_targets=None, verification_owner="", origin_namespace="indexer")
    # 旧显式业务调用不新增 owner 要求，也不能读取偏好。
    monkeypatch.setattr("app.modules.media_preferences.resolve_download_target", Mock(side_effect=AssertionError("显式目标不查偏好")))
    assert sdk.resources.download(["candidate"], "qb")["ok"]


def test_cold_confirmation_emits_usable_once_only_undo(runtime):
    async def check():
        with sdk.call_owner(OWNER):
            before = sdk.preferences.get()
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        plan = (await effects.prepare("preferences.update", CHANGES, context=context)).effect_plan
        assert plan is not None
        digest = db.media_owner_digest(OWNER)
        assert not get_media_preferences(digest)["explicit"]
        foreign = await context_for(store, "webk:v1:" + "b" * 64)
        with pytest.raises(ToolPipelineError):
            await effects.execute_confirmed(plan.plan_id, context=foreign)
        rebuilt = executor(SQLiteKernelStore())
        restored = await rebuilt.get_pending_plan(owner=OWNER, session_id=context.session_id)
        assert restored is not None and restored.plan_id == plan.plan_id
        result = (await rebuilt.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
        assert result["ok"] and result["data"]["minimum_resolution"] == "1080p"
        assert all(key not in json.dumps(result) for key in ("compensation_after", "revision_token", digest))
        with pytest.raises(ToolPipelineError):
            await rebuilt.execute_confirmed(plan.plan_id, context=context)
        action = result["data"]["undo"]
        assert action == {"method": "activity.undo", "arguments": {"receipt_id": result["data"]["receipt_id"]}}
        saved = get_media_preferences(digest)
        undo = (await rebuilt.prepare(action["method"], action["arguments"], context=context)).effect_plan
        assert undo is not None and undo.tool_name == "activity.undo"
        assert get_media_preferences(digest) == saved
        assert (await rebuilt.execute_confirmed(undo.plan_id, context=context)).outcome.public_content["ok"]
        with sdk.call_owner(OWNER):
            assert sdk.preferences.get() == before
        with pytest.raises(ToolPipelineError):
            await rebuilt.execute_confirmed(undo.plan_id, context=context)
    asyncio.run(check())


def test_flat_confirmation_editing_reprepares_without_widening_fields(runtime):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store, confirmation_store=SQLiteConfirmationStore(clock=lambda: 2_000_000_000.0))
        with sdk.call_owner(OWNER):
            sdk.preferences.update(minimum_resolution="1080p", preferred_genres=["喜剧"], memory_notes=["旧备忘"])
            before = sdk.preferences.get()
        original = (await effects.prepare("preferences.update", {"preferred_resolution": "2160p"}, context=context)).effect_plan
        assert original is not None
        editing = original.public_dict()["editing"]
        assert {"preferred_resolution", "minimum_resolution", "memory_notes", "exclude_played"} <= editing["schema"]["properties"].keys()
        assert editing["values"] == {"preferred_resolution": "2160p"}
        edited = (await effects.edit_confirmation(original.plan_id,
            {"preferred_resolution": "720p", "memory_notes": [], "exclude_played": False}, context=context)).effect_plan
        assert edited is not None and edited.plan_id != original.plan_id
        assert edited.expires_at <= original.expires_at
        with pytest.raises(ToolPipelineError):
            await effects.execute_confirmed(original.plan_id, context=context)
        written = (await effects.execute_confirmed(edited.plan_id, context=context)).outcome.public_content
        assert written["ok"]
        with sdk.call_owner(OWNER):
            saved = sdk.preferences.get()
        assert saved["preferred_resolution"] == "720p" and saved["minimum_resolution"] == "1080p"
        assert saved["preferred_genres"] == ["喜剧"] and saved["memory_notes"] == [] and saved["exclude_played"] is False
        action = written["data"]["undo"]
        undo = (await effects.prepare(action["method"], action["arguments"], context=context)).effect_plan
        assert undo is not None and undo.tool_name == "activity.undo"
        with sdk.call_owner(OWNER):
            assert sdk.preferences.get() == saved
        assert (await effects.execute_confirmed(undo.plan_id, context=context)).outcome.public_content["ok"]
        with sdk.call_owner(OWNER):
            assert sdk.preferences.get() == before
        # 原严格 schema 继续约束编辑后的重预检，错误编辑撤销旧卡，不能留下可执行计划。
        pending = (await effects.prepare("preferences.update", {"min_rating": 8}, context=context)).effect_plan
        assert pending is not None
        with pytest.raises(ToolPipelineError) as error:
            await effects.edit_confirmation(pending.plan_id, {"exclude_played": "false"}, context=context)
        assert error.value.plan_invalidated
        assert await effects.get_pending_plan(owner=OWNER, session_id=context.session_id) is None
    asyncio.run(check())


@pytest.mark.parametrize("stage", ["forward", "undo"])
def test_confirmation_never_overwrites_a_concurrent_version(runtime, stage):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        plan = (await effects.prepare("preferences.update", CHANGES, context=context)).effect_plan
        assert plan is not None
        if stage == "undo":
            result = (await effects.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
            plan = (await effects.prepare("activity.undo", {"receipt_id": result["data"]["receipt_id"]}, context=context)).effect_plan
            assert plan is not None
        with sdk.call_owner(OWNER):
            assert sdk.preferences.update(preferred_download_target="qb")["ok"]
            current = sdk.preferences.get()
        with pytest.raises(ToolPipelineError) as error:
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert error.value.code == "confirmation_stale"
        with sdk.call_owner(OWNER):
            assert sdk.preferences.get() == current
    asyncio.run(check())


@pytest.mark.parametrize("operation", ["update", "clear"])
def test_effect_keeps_its_own_transaction_evidence_when_a_later_writer_commits(runtime, monkeypatch, operation):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        lifecycle = MediaFluxEffectLifecycle()
        lifecycle.completed = Mock(wraps=lifecycle.completed)
        effects = executor(store, lifecycle=lifecycle)
        digest = db.media_owner_digest(OWNER)
        set_media_preferences(digest, expected_revision=0, updates={"preferred_genres": ["A"]})
        arguments = {"preferred_download_target": "qb"} if operation == "update" else {}
        plan = (await effects.prepare(f"preferences.{operation}", arguments, context=context)).effect_plan
        assert plan is not None
        apply = sdk_effects.apply_media_preferences_change

        def interleaved(*args, **kwargs):
            own = apply(*args, **kwargs)
            current = get_media_preferences(digest)
            assert set_media_preferences(digest, expected_revision=current["revision"],
                updates={"preferred_genres": ["C"], "preferred_download_target": "both"}) is not None
            return own

        monkeypatch.setattr(sdk_effects, "apply_media_preferences_change", interleaved)
        completed = await effects.execute_confirmed(plan.plan_id, context=context)
        raw = lifecycle.completed.call_args.kwargs["value"]
        after = raw.effect_metadata["compensation_after"]
        assert get_media_preferences(digest)["preferred_genres"] == ["C"]
        assert not {"receipt_id", "undo"} & completed.outcome.public_content["data"].keys()
        assert "effect_metadata" not in raw.to_dict() and "compensation_after" not in raw.to_model_dict()
        if operation == "update":
            assert after["preferred_download_target"] == "qb" and after["revision"] == 2
            raw.data["preferred_genres"].append("display-only")
            assert after["preferred_genres"] == ["A"]
        else:
            assert after == {**default_media_preferences(), "revision": 0, "explicit": False}
    asyncio.run(check())


@pytest.mark.parametrize("change", ["cancel", "config_aba", "tg_revoked"])
def test_native_preferences_recheck_cancellation_and_host_authority(runtime, change):
    async def check():
        config.set_and_save({"TG_AGENT_ENABLED": "1", "TG_CHAT_ID": "-100123", "TG_AGENT_ALLOWED_USER_IDS": "8"})
        owner = "tg:v1:-100123\x1f8" if change == "tg_revoked" else OWNER
        store = SQLiteKernelStore()
        context = await context_for(store, owner)
        effects = executor(store)
        plan = (await effects.prepare("preferences.update", CHANGES, context=context)).effect_plan
        assert plan is not None
        assert plan.snapshot_fingerprint.startswith("mfkr2:")
        if change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "config_aba":
            config.set_and_save({"TG_AGENT_ALLOWED_USER_IDS": "9"})
            config.set_and_save({"TG_AGENT_ALLOWED_USER_IDS": "8"})
        else:
            config.set_and_save({"TG_AGENT_ALLOWED_USER_IDS": "9"})
        with pytest.raises(ToolPipelineError) as error:
            await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
        if change != "cancel":
            assert error.value.code == ("confirmation_stale" if change == "config_aba" else "authorization_denied")
        assert not get_media_preferences(db.media_owner_digest(owner))["explicit"]
    asyncio.run(check())


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="真实 Linux root 工作区验收")
@pytest.mark.parametrize("concurrent", [False, True])
def test_native_script_confirmation_returns_undo_without_replaying_side_effects(runtime, monkeypatch, tmp_path, concurrent):
    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))
    monkeypatch.setattr("app.services.search_library", lambda query, **kwargs: {"query": query, **kwargs})
    monkeypatch.setattr("app.services.batch_library_presence", lambda items, *, cancelled=None: {"items": items})

    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        pipeline = executor(store, cls=ToolPipeline)
        assert isinstance(pipeline, ToolPipeline)
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        if concurrent:
            with sdk.call_owner(OWNER):
                sdk.preferences.update(memory_notes=["旧备忘"])
        code = '''from pathlib import Path
from mf import library
from mf.preferences import get, update
assert library.search("defaults")["limit"] == 50
explicit = library.search(None, limit=0, count_only=False)
assert explicit["query"] is None and explicit["limit"] == 0 and explicit["count_only"] is False
assert library.presence([]) == {"items": []}
with Path("runs.txt").open("a") as output:
    output.write("once\\n")
assert not get()["explicit"]
update(preferred_resolution="2160p")
Path("after.txt").write_text("must not run")
'''
        if concurrent:
            code = code.replace('assert not get()["explicit"]', 'assert get()["memory_notes"] == ["旧备忘"]')
            code = code.replace('update(preferred_resolution="2160p")', '''import subprocess
subprocess.run(["mf", "preferences.update", "--preferred-resolution", "2160p",
                "--min-rating", "0", "--max-episode-size-gb", "1.5", "--no-exclude-played",
                "--memory-notes"], check=True)''')
        result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": code}, context=context)
        assert result.effect_plan is not None and result.effect_plan.tool_name == "preferences.update"
        root, _ = sandbox._workspace_location(OWNER, context.session_id)
        assert (root / "work/runs.txt").read_text() == "once\n"
        assert not (root / "work/after.txt").exists()
        rebuilt = executor(SQLiteKernelStore())
        done = (await rebuilt.execute_confirmed(result.effect_plan.plan_id, context=context)).outcome.public_content
        assert done["ok"]
        if concurrent:
            assert done["data"]["min_rating"] == 0 and done["data"]["max_episode_size_gb"] == 1.5
            assert done["data"]["exclude_played"] is False
            assert done["data"]["memory_notes"] == []
        action = done["data"]["undo"]
        undo = (await rebuilt.prepare(action["method"], action["arguments"], context=context)).effect_plan
        assert undo is not None
        if concurrent:
            with sdk.call_owner(OWNER):
                sdk.preferences.update(preferred_resolution="1080p")
            with pytest.raises(ToolPipelineError) as error:
                await rebuilt.execute_confirmed(undo.plan_id, context=context)
            assert error.value.code == "confirmation_stale"
        else:
            assert (await rebuilt.execute_confirmed(undo.plan_id, context=context)).outcome.public_content["ok"]
        with sdk.call_owner(OWNER):
            assert sdk.preferences.get()["preferred_resolution"] == ("1080p" if concurrent else "any")
        assert (root / "work/runs.txt").read_text() == "once\n"
        assert not (root / "work/after.txt").exists()
    asyncio.run(check())


@pytest.mark.parametrize("explicit", [False, True])
def test_persisted_old_compensation_metadata_becomes_ledger_receipt_on_cold_confirmation(runtime, explicit):
    """旧 setting 的来源是未执行计划 metadata；旧空账本行不补造 payload。"""
    from dataclasses import replace
    from app.repositories import agent_compensations

    class HistoricalPreferencesLifecycle(MediaFluxEffectLifecycle):
        def prepared(self, **kwargs):
            prepared = super().prepared(**kwargs)
            candidate = prepared.metadata["compensation"]
            historical = {"kind": "setting", "tool": "preferences.clear" if candidate["updates"] is None else "preferences.update",
                          "arguments": candidate["updates"] or {}}
            return replace(prepared, metadata={**prepared.metadata, "compensation": historical})

    async def check():
        with sdk.call_owner(OWNER):
            if explicit:
                assert sdk.preferences.update(preferred_download_target="guangya", memory_notes=["原库 /media/archive；原保存快照"])["ok"]
            before = sdk.preferences.get()
        store = SQLiteKernelStore()
        context = await context_for(store)
        old_executor = executor(store, lifecycle=HistoricalPreferencesLifecycle())
        plan = (await old_executor.prepare("preferences.update", {"preferred_download_target": "qb"}, context=context)).effect_plan
        assert plan is not None and plan.metadata["compensation"]["kind"] == "setting"
        cold = executor(SQLiteKernelStore())
        restored = await cold.get_pending_plan(owner=OWNER, session_id=context.session_id)
        assert restored is not None and restored.metadata["compensation"] == plan.metadata["compensation"]
        result = (await cold.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
        receipt_id = result["data"]["receipt_id"]
        stored = agent_compensations.get(receipt_id, db.media_owner_digest(OWNER))
        assert stored is not None and stored["payload"]["kind"] == "setting"
        assert stored["payload"]["tool"] == plan.metadata["compensation"]["tool"]
        undo = (await executor(SQLiteKernelStore()).prepare("activity.undo", {"receipt_id": receipt_id}, context=context)).effect_plan
        assert undo is not None
        assert (await executor(SQLiteKernelStore()).execute_confirmed(undo.plan_id, context=context)).outcome.public_content["ok"]
        with sdk.call_owner(OWNER):
            assert sdk.preferences.get() == before
    asyncio.run(check())
