"""媒体用户原生写：真实SQLite确认、冷恢复、取消及工作区，不接真实媒体。"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import database as db
from app import sdk
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.bootstrap import build_agent_kernel_runtime
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.ports.sdk_effects import media_user_tools
from app.agent.kernel.state_types import CancellationToken, SessionBusyError
from app.agent.kernel.tool_calls import ToolCallContext
from app.repositories.media_experience import (
    get_media_preferences,
    set_media_preferences,
)
from tests.test_agent_media_user_actions import (
    backend as backend,  # noqa: PLC0414 - shared isolated client/runtime
)

OWNER = "webk:v1:" + "d" * 64


async def context_for(store, *, owner=OWNER, session="media-user-native"):
    lease, _ = await store.begin_turn(owner=owner, session_id=session, request_id="media-user-write")
    return ToolCallContext(owner=owner, session_id=session, request_id=lease.request_id,
        turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=AsyncMock())


def executor(store):
    return EffectExecutor(catalog=ToolCatalog(media_user_tools()), state_store=store, reference_store=store,
        effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore(), record_actions=True),
        authorization=MediaFluxAuthorizationPolicy(), effect_lifecycle=MediaFluxEffectLifecycle())


@pytest.mark.parametrize("method,arguments", [
    ("set_played", {"item_id": "movie1", "played": True}),
    ("set_played", {"item_id": "movie1", "played": False}),
    ("set_favorite", {"item_id": "movie1", "favorite": True}),
    ("set_favorite", {"item_id": "movie1", "favorite": False}),
    ("create_playlist", {"name": "新片单", "item_ids": ["movie1"]}),
    ("add_to_playlist", {"playlist_id": "playlist1", "item_ids": ["movie2"]}),
    ("remove_from_playlist", {"playlist_id": "playlist1", "entry_ids": ["entry1"]}),
])
def test_native_write_cold_confirmation_and_replay_rejection_use_real_ids(backend, method, arguments):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        if method in {"set_played", "set_favorite"}:
            flag = "played" if method == "set_played" else "favorite"
            backend.client.items["movie1"]["UserData"]["Played" if flag == "played" else "IsFavorite"] = not arguments[flag]
        pending = await executor(store).prepare("library." + method, arguments, context=context)
        plan = pending.effect_plan
        assert plan is not None and not backend.client.calls
        assert plan.arguments == {**arguments, "server": "jellyfin"}
        restored = executor(SQLiteKernelStore())
        saved = await restored.get_pending_plan(owner=context.owner, session_id=context.session_id)
        assert saved is not None and saved.plan_id == plan.plan_id
        result = await restored.execute_confirmed(plan.plan_id, context=context)
        public = result.outcome.public_content
        assert public["ok"] and public["status"] == "completed", public
        assert public["data"]["verification"] == "已回读验证" and len(backend.client.calls) == 1
        assert public["data"]["scope"]["user_id"] == "viewer"
        with pytest.raises(ToolPipelineError):
            await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
        assert len(backend.client.calls) == 1

    asyncio.run(check())


@pytest.mark.parametrize("method,arguments", [
    ("set_played", {"item_id": "movie1", "played": 0}),
    ("set_favorite", {"item_id": "../movie1"}),
    ("create_playlist", {"name": "新片单", "item_ids": ["movie1", "movie1"]}),
    ("add_to_playlist", {"playlist_id": "playlist1", "item_ids": []}),
    ("remove_from_playlist", {"playlist_id": "playlist1", "entry_ids": [True]}),
    ("set_favorite", {"item_id": "movie1", "item_ref": "old-reference"}),
])
def test_native_illegal_parameters_fail_before_read_or_write(backend, method, arguments):
    async def check():
        store = SQLiteKernelStore()
        with pytest.raises(ToolPipelineError) as caught:
            await executor(store).prepare("library." + method, arguments, context=await context_for(store))
        assert caught.value.code == "invalid_arguments"
        assert not backend.client.read_calls and not backend.client.calls
    asyncio.run(check())


@pytest.mark.parametrize("change", ["owner", "session", "cancel_plan", "cancel_token", "snapshot"])
def test_pending_confirmation_cannot_cross_identity_or_ignore_cancellation(backend, change):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        if change == "snapshot":
            tool = effects.catalog.get("library.set_favorite")
            prepare = tool.prepare
            assert prepare is not None
            effects.catalog = ToolCatalog([replace(tool, prepare=lambda a, c: replace(prepare(a, c), metadata={}))])
        plan = (await effects.prepare("library.set_favorite", {"item_id": "movie1"}, context=context)).effect_plan
        assert plan is not None
        if change == "owner":
            context = await context_for(SQLiteKernelStore(), owner="webk:v1:" + "b" * 64)
        elif change == "session":
            context = await context_for(SQLiteKernelStore(), session="other")
        elif change == "cancel_plan":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "cancel_token":
            context.cancellation.cancel()
        with pytest.raises((ToolPipelineError, asyncio.CancelledError)):
            await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
        assert not backend.client.calls
    asyncio.run(check())


@pytest.mark.parametrize("change", ["state", "members", "profile", "default_user"])
def test_cold_confirmation_rechecks_original_business_snapshot_before_writing(backend, change):
    async def check():
        if change == "default_user":
            backend.profiles[0] = replace(backend.profiles[0], user_id="")
        store = SQLiteKernelStore()
        context = await context_for(store)
        name = "library.add_to_playlist" if change == "members" else "library.set_played"
        arguments = {"playlist_id": "playlist1", "item_ids": ["movie2"]} if change == "members" else {"item_id": "movie1"}
        plan = (await executor(store).prepare(name, arguments, context=context)).effect_plan
        assert plan is not None
        if change == "state":
            backend.client.items["movie1"]["UserData"]["PlayCount"] = 2
        elif change == "members":
            backend.client.members["playlist1"] = []
        elif change == "default_user":
            backend.client.user_id = "changed-user"
        else:
            backend.profiles[0] = replace(backend.profiles[0], credential="changed")
        with pytest.raises(ToolPipelineError) as caught:
            await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
        assert caught.value.code == "confirmation_stale" and caught.value.execution_started is False
        assert not backend.client.calls
    asyncio.run(check())


def test_owner_preference_is_resolved_once_before_freezing_and_not_at_confirmation(backend):
    async def check():
        backend.profiles.append(replace(backend.profiles[0], source="configured:emby", server_type="emby"))
        digest = db.media_owner_digest(OWNER)
        set_media_preferences(digest, expected_revision=0, updates={"preferred_server": "jellyfin"})
        store = SQLiteKernelStore()
        context = await context_for(store)
        plan = (await executor(store).prepare("library.set_favorite", {"item_id": "movie1", "favorite": False}, context=context)).effect_plan
        assert plan is not None and plan.arguments["server"] == "jellyfin" and plan.arguments["favorite"] is False
        set_media_preferences(digest, expected_revision=get_media_preferences(digest)["revision"], updates={"preferred_server": "emby"})
        result = await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
        assert result.outcome.public_content["data"]["server"] == "jellyfin"
        with sdk.call_owner("other-owner"), pytest.raises(ValueError):
            sdk.library.user_state("movie1")
        assert not backend.client.calls  # False原本就是目标值。
    asyncio.run(check())


@pytest.mark.parametrize("mode", ["unknown", "cancel_during_read", "cancel_after_write"])
def test_unknown_or_cancelled_confirmation_never_replays_media_mutation(backend, mode):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        plan = (await executor(store).prepare("library.set_favorite", {"item_id": "movie1"}, context=context)).effect_plan
        assert plan is not None
        if mode == "cancel_during_read":
            original = backend.client._request
            def read(*a, **kw):
                value = original(*a, **kw)
                context.cancellation.cancel()
                return value
            backend.client._request = read
        else:
            original = backend.client._session.request
            def write(*a, **kw):
                value = original(*a, **kw)
                if mode == "unknown":
                    raise RuntimeError("private-upstream-token")
                context.cancellation.cancel()
                return value
            backend.client._session.request = write
        try:
            result = await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
            assert result.outcome.public_content["status"] == "outcome_unknown"
            assert "private-upstream-token" not in json.dumps(result.outcome.public_content)
        except asyncio.CancelledError:
            assert mode.startswith("cancel")
        expected = 0 if mode == "cancel_during_read" else 1
        assert len(backend.client.calls) == expected
        context = replace(context, cancellation=CancellationToken())
        with pytest.raises(ToolPipelineError):
            await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
        assert len(backend.client.calls) == expected
    asyncio.run(check())


def test_concurrent_cold_confirmations_claim_one_sqlite_plan_once(backend):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        plan = (await executor(store).prepare("library.set_favorite", {"item_id": "movie1"}, context=context)).effect_plan
        assert plan is not None
        results = await asyncio.gather(*[
            executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context) for _ in range(2)
        ], return_exceptions=True)
        failures = [result for result in results if isinstance(result, BaseException)]
        successes = [result for result in results if not isinstance(result, BaseException)]
        assert len(failures) == len(successes) == 1
        assert isinstance(failures[0], (SessionBusyError, ToolPipelineError)), type(failures[0]).__name__
        assert successes[0].outcome.public_content["ok"] is True
        assert len(backend.client.calls) == 1 and backend.client.items["movie1"]["UserData"]["IsFavorite"] is True
    asyncio.run(check())


def test_native_catalog_removes_legacy_operations(backend):
    catalog = build_agent_kernel_runtime(model=SimpleNamespace()).session.catalog
    retired = {"media.user.inspect", "media.user.mark_played", "media.user.mark_unplayed", "media.user.favorite",
               "media.user.unfavorite", "media.playlists.list", "media.playlist.inspect", "media.playlist.create",
               "media.playlist.add_items", "media.playlist.remove_items"}
    for name in retired:
        assert not catalog.has(name) and not catalog.has(name.replace(".", "__"))
    for tool in media_user_tools():
        assert catalog.has(tool.name) and tool.name in sdk.WRITE_METHODS and tool.name in sdk.METHODS
    assert catalog.has("media.refresh")


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="需要可运行 Bubblewrap 的 Linux 隔离工作区")
def test_native_workspace_api_cli_file_readback_and_cold_confirmation_do_not_replay_script(backend, tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))
    backend.client.members["playlist1"] = [{"Id": f"m{i}", "Name": str(i), "PlaylistItemId": f"e{i}"} for i in range(40)]

    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        pipeline = ToolPipeline(catalog=ToolCatalog(media_user_tools()), state_store=store, reference_store=store,
            effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore(), record_actions=True),
            authorization=MediaFluxAuthorizationPolicy(), effect_lifecycle=MediaFluxEffectLifecycle())
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        read = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": '''import json, subprocess
from mf import library
state=library.user_state("movie1")
members=library.playlist("playlist1")
listed=json.loads(subprocess.check_output(["mf", "library.playlists", "--limit", "1"]))
json.dump({"state":state,"members":members,"listed":listed},open("before.json","w"))
assert len(members["items"])==40
print(state["item"]["id"],listed["items"][0]["id"])
'''}, context=context)
        assert read.outcome.public_content["data"]["exit_code"] == 0, read.outcome.public_content
        pending = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": '''from pathlib import Path
import json, subprocess
json.dump(["movie2"], open("playlist-ids.json", "w"))
subprocess.run(["mf", "library.add_to_playlist", "--playlist-id", "playlist1", "--item-ids", "@playlist-ids.json"], check=True)
Path("after-write").write_text("script replayed")
'''}, context=context)
        plan = pending.effect_plan
        assert plan is not None and not backend.client.calls
        assert plan.arguments["item_ids"] == ["movie2"]
        result = await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
        assert result.outcome.public_content["data"]["member_count"] == 41
        assert len(result.outcome.public_content["data"]["items"]) == 41
        after = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": '''import json
from pathlib import Path
from mf import library
saved=json.load(open("before.json"))
assert len(saved["members"]["items"])==40
assert saved["state"]["scope"]["user_id"]=="viewer"
assert not Path("after-write").exists()
members=library.playlist("playlist1")
assert len(members["items"])==41
assert members["items"][-1]["item_id"]=="movie2"
print(members["items"][-1]["entry_id"])
'''}, context=context)
        assert after.outcome.public_content["data"]["exit_code"] == 0, after.outcome.public_content
        assert after.outcome.public_content["data"]["stdout"].strip() == "added0"
        assert len(backend.client.calls) == 1
    try:
        asyncio.run(check())
    finally:
        sandbox.clear_workspace(owner=OWNER, session_id="media-user-native")
