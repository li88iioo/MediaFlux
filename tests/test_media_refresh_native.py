"""原生查询真实ID、分页及媒体刷新：离线客户端HTTP、持久确认和root工作区。"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import database as db
from app import sdk, services
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.bootstrap import build_agent_kernel_runtime
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.ports.sdk_effects import media_refresh_tools
from app.agent.kernel.tool_calls import PreparedEffect
from app.clients.emby import EmbyClient
from app.clients.jellyfin import JellyfinClient
from app.modules import media_library
from app.modules.media_server_profiles import MediaServerProfile
from tests.support import initialized_web_runtime
from tests.test_agent_sdk_media_user_effects import OWNER, context_for


@pytest.fixture(params=[JellyfinClient, EmbyClient])
def backend(request, monkeypatch):
    with initialized_web_runtime() as paths:
        client = request.param("http://media.invalid", "private-token")
        client._cached_user_id = "viewer"
        if isinstance(client, EmbyClient):
            client.product_kind = "emby"
        server = "emby" if isinstance(client, EmbyClient) else "jellyfin"
        profiles = [
            MediaServerProfile(
                "configured:" + server,
                server,
                server,
                client.url,
                "private-token",
                True,
                "viewer",
            )
        ]
        state = SimpleNamespace(
            client=client,
            profiles=profiles,
            paths=paths,
            server=server,
            item={
                "Id": "movie-49",
                "Name": "Example.Show.2026.mkv",
                "Type": "Movie",
                "ProductionYear": 2026,
                "ProviderIds": {"Tmdb": "123"},
            },
            total=651,
            known=True,
            reads=[],
            writes=[],
        )

        def read(path, params=None, **kwargs):
            state.reads.append((path, deepcopy(params)))
            if path.endswith("/Items/movie-49"):
                return deepcopy(state.item)
            assert path == "/Users/viewer/Items", path
            assert params is not None
            start = params.get("StartIndex", 0)
            limit = params.get("Limit", 50)
            rows = [
                {
                    "Id": f"movie-{i}",
                    "Name": f"Show.{i}.mkv",
                    "Type": "Series",
                    "ProviderIds": {"Tmdb": str(100 + i)},
                    "ParentIndexNumber": int(i != 0),
                    "IndexNumber": max(i, 1),
                }
                for i in range(start, min(start + limit, state.total))
            ]
            return {
                "Items": rows,
                **({"TotalRecordCount": state.total} if state.known else {}),
            }

        def post(url, **kwargs):
            state.writes.append((url, kwargs))
            return SimpleNamespace(raise_for_status=lambda: None)

        monkeypatch.setattr(client, "_request", read)
        monkeypatch.setattr(client._session, "post", post)
        monkeypatch.setattr(MediaServerProfile, "create_client", lambda _: client)
        monkeypatch.setattr(media_library, "list_configured_profiles", lambda: profiles)
        monkeypatch.setattr(
            services,
            "_configured_media_sources",
            lambda: [(server, server, client.url, client)],
        )
        services.clear_library_search_cache()
        yield state
        services.clear_library_search_cache()
        client.close()


def effects(store, catalog=None):
    return EffectExecutor(
        catalog=catalog or ToolCatalog(media_refresh_tools()),
        state_store=store,
        reference_store=store,
        effect_store=ConfirmationEffectPlanStore(
            SQLiteConfirmationStore(), record_actions=True
        ),
        authorization=MediaFluxAuthorizationPolicy(),
        effect_lifecycle=MediaFluxEffectLifecycle(),
    )


def test_native_search_full_pages_identity_and_later_selection(backend):
    backend.total = 137
    pages = [
        sdk.library.search(
            "Show", media_type="series", limit=50, offset=start, server=backend.server
        )["data"]
        for start in (0, 50, 100)
    ]
    assert [p["returned"] for p in pages] == [50, 50, 37]
    assert [p["next_offset"] for p in pages] == [50, 100, None]
    assert all(p["total"] == 137 for p in pages)
    items = [row for page in pages for row in page["sources"][0]["items"]]
    assert len({r["id"] for r in items}) == 137
    assert items[49]["id"] == "movie-49" and items[49]["tmdb_id"] == "149"
    assert all("ProviderIds" in params["Fields"] for _, params in backend.reads)
    assert "private-token" not in json.dumps(pages)
    prepared = media_library.prepare_refresh(backend.server, item_id=items[49]["id"])
    assert prepared["item"]["id"] == "movie-49" and not backend.writes


@pytest.mark.parametrize(
    "specials,known", [(False, True), (True, True), (False, False)]
)
def test_real_client_inventory_all_pages_no_projection_cap(backend, specials, known):
    backend.known = known
    result = sdk.library.episodes(
        series_id="series-1", server=backend.server, include_specials=specials
    )["data"]
    assert (
        len(result["episodes"]) == 651 if specials else len(result["episodes"]) == 650
    )
    assert result["truncated"] is False
    assert result["total"] == 651  # 共用客户端在短末页证明完整后得到实际总数
    assert result["inventory_complete"] is True
    assert result["ignored_specials"] == (0 if specials else 1)
    assert [params["StartIndex"] for _, params in backend.reads] == [0, 200, 400, 600]
    backend.reads.clear()
    limited = sdk.library.episodes(
        series_id="series-1", server=backend.server, max_episodes=500
    )["data"]
    assert limited["truncated"] and not limited["inventory_complete"]
    assert len(limited["episodes"]) == 499


def test_refresh_cold_sqlite_once_accepted_and_profile_capture(backend):
    async def check():
        store = SQLiteKernelStore()
        ctx = await context_for(store)
        plan = (
            await effects(store).prepare(
                "media.refresh",
                {"item_id": "movie-49", "server": backend.server},
                context=ctx,
            )
        ).effect_plan
        assert plan and not backend.writes
        cold = effects(SQLiteKernelStore())
        restored = await cold.get_pending_plan(
            owner=ctx.owner, session_id=ctx.session_id
        )
        assert restored and restored.arguments == {
            "item_id": "movie-49",
            "server": backend.server,
            "mode": "scan", "replace_images": False, "regenerate_trickplay": False,
        }
        result = (
            await cold.execute_confirmed(plan.plan_id, context=ctx)
        ).outcome.public_content
        assert result["ok"] and result["status"] == "accepted"
        assert (
            result["data"]["scope"] == "item"
            and result["data"]["global_refresh"] is False
        )
        assert backend.writes[0][0].endswith("/Items/movie-49/Refresh")
        assert {k.lower(): v for k, v in backend.writes[0][1]["params"].items()}[
            "replaceallmetadata"
        ] == "false"
        with pytest.raises(ToolPipelineError):
            await effects(SQLiteKernelStore()).execute_confirmed(
                plan.plan_id, context=ctx
            )
        assert len(backend.writes) == 1

    asyncio.run(check())


@pytest.mark.parametrize(
    "change",
    [
        "owner",
        "session",
        "cancel",
        "cancel_token",
        "profile",
        "target",
        "type",
        "runtime",
    ],
)
def test_refresh_scope_changes_and_cancellation_are_zero_write(
    backend, monkeypatch, change
):
    async def check():
        store = SQLiteKernelStore()
        ctx = await context_for(store)
        executor = effects(store)
        plan = (
            await executor.prepare(
                "media.refresh",
                {"item_id": "movie-49", "server": backend.server},
                context=ctx,
            )
        ).effect_plan
        assert plan
        if change == "owner":
            ctx = await context_for(SQLiteKernelStore(), owner="webk:v1:" + "b" * 64)
        elif change == "session":
            ctx = await context_for(SQLiteKernelStore(), session="other")
        elif change == "cancel":
            assert await executor.cancel_effect(plan.plan_id, lease=ctx.lease)
        elif change == "cancel_token":
            ctx.cancellation.cancel()
        elif change == "profile":
            backend.profiles[0] = replace(backend.profiles[0], credential="changed")
        elif change == "target":
            backend.item["Id"] = "replaced"
        elif change == "type":
            backend.item["Type"] = "Series"
        else:
            monkeypatch.setenv("AGENT_ENABLED", "false")
        with pytest.raises((ToolPipelineError, asyncio.CancelledError)):
            await effects(SQLiteKernelStore()).execute_confirmed(
                plan.plan_id, context=ctx
            )
        assert not backend.writes

    asyncio.run(check())


@pytest.mark.parametrize("failure", ["rejected", "timeout", "after_write"])
def test_write_uncertain_outcome_is_persisted_and_not_replayed(
    backend, monkeypatch, failure
):
    async def check():
        store = SQLiteKernelStore()
        ctx = await context_for(store)
        plan = (
            await effects(store).prepare(
                "media.refresh",
                {"item_id": "movie-49", "server": backend.server},
                context=ctx,
            )
        ).effect_plan
        assert plan

        def post(url, **kwargs):
            backend.writes.append((url, kwargs))
            if failure != "after_write":
                raise (
                    TimeoutError("lost reply")
                    if failure == "timeout"
                    else ValueError("rejected")
                )
            return SimpleNamespace(raise_for_status=lambda: None)

        monkeypatch.setattr(backend.client._session, "post", post)
        if failure == "after_write":
            original = backend.client.refresh_library

            def uncertain(identifier, **options):
                assert original(identifier, **options)
                raise RuntimeError("post-write failure")

            monkeypatch.setattr(backend.client, "refresh_library", uncertain)
        with pytest.raises(ToolPipelineError) as caught:
            await effects(SQLiteKernelStore()).execute_confirmed(
                plan.plan_id, context=ctx
            )
        assert caught.value.code == "outcome_unknown" and caught.value.execution_started
        with pytest.raises(ToolPipelineError):
            await effects(SQLiteKernelStore()).execute_confirmed(
                plan.plan_id, context=ctx
            )
        assert len(backend.writes) == 1
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT status FROM agent_action_history WHERE tool_name='media.refresh'"
            ).fetchone()
            assert row and row[0] == "outcome_unknown"

    asyncio.run(check())


@pytest.mark.parametrize(
    "arguments",
    [
        {"item_id": ""},
        {"item_id": "../movie"},
        {"item_id": "movie-49", "item_ref": "old"},
        {"library_id": "lib"},
    ],
)
def test_bad_item_identity_rejected_before_read(backend, arguments):
    async def check():
        store = SQLiteKernelStore()
        with pytest.raises(ToolPipelineError):
            await effects(store).prepare(
                "media.refresh", arguments, context=await context_for(store)
            )
        assert not backend.reads and not backend.writes

    asyncio.run(check())


@pytest.mark.parametrize("kind,mode,images", [
    ("library", "scan", False), ("library", "missing", True), ("Series", "replace", True),
    ("Season", "missing", False), ("Episode", "replace", False),
])
def test_refresh_mode_and_scope_are_frozen_and_submitted(backend, monkeypatch, kind, mode, images):
    monkeypatch.setattr(backend.client, "list_virtual_folders", lambda: [{"id": "library-1", "name": "动漫"}])
    backend.item["Type"] = kind
    arguments = dict(server=backend.server, mode=mode, replace_images=images,
                     **({"library_id": "library-1"} if kind == "library" else {"item_id": "movie-49"}))
    method = "library.refresh" if kind == "library" else "media.refresh"

    async def check():
        store = SQLiteKernelStore()
        ctx = await context_for(store)
        executor = effects(store)
        plan = (await executor.prepare(method, arguments, context=ctx)).effect_plan
        assert plan and not backend.writes
        assert plan.arguments["mode"] == mode and plan.arguments["replace_images"] is images
        result = (await executor.execute_confirmed(plan.plan_id, context=ctx)).outcome.public_content
        assert result["status"] == "accepted" and result["data"]["mode"] == mode
        assert result["data"]["target"]["id"] == ("library-1" if kind == "library" else "movie-49")
        params = {k.lower(): v for k, v in backend.writes[0][1]["params"].items()}
        assert params["metadatarefreshmode"] == ("Default" if mode == "scan" else "FullRefresh")
        assert params["replaceallmetadata"] == str(mode == "replace").lower()
        assert params["replaceallimages"] == str(images).lower()
    asyncio.run(check())


@pytest.mark.parametrize("options", [
    {"mode": "invalid"}, {"replace_images": "false"}, {"mode": "scan", "replace_images": True},
    {"mode": "scan", "regenerate_trickplay": True},
])
def test_refresh_invalid_options_are_zero_write(backend, options):
    with pytest.raises(media_library.MediaLibraryError):
        media_library.prepare_refresh(backend.server, item_id="movie-49", **options)
    assert not backend.writes and not backend.reads


def test_retired_catalog_and_old_pending_confirmation_cannot_execute(backend):
    catalog = build_agent_kernel_runtime(model=SimpleNamespace()).session.catalog
    for name in (
        "provider.capabilities",
        "provider.query",
        "provider.change.preview",
        "provider.change.execute",
        "provider.job.status",
    ):
        assert not catalog.has(name) and not catalog.has(name.replace(".", "__"))
    assert {"library.search", "library.episodes", "media.refresh"} <= sdk.METHODS.keys()
    assert catalog.has("downloads.pause") and catalog.has("cloud.change")

    async def check():
        store = SQLiteKernelStore()
        ctx = await context_for(store)
        old_write = Mock(side_effect=AssertionError("retired writer must never run"))
        old = KernelToolSpec(
            name="provider.change.execute",
            domain="provider",
            effect=ToolEffect.WRITE,
            description="旧持久票据夹具",
            input_schema={"type": "object", "properties": {}},
            prepare=lambda a, c: PreparedEffect(
                preview={
                    "ok": True,
                    "status": "confirmation_required",
                    "summary": "旧计划",
                },
                snapshot_fingerprint="old",
            ),
            execute_confirmed=old_write,
        )
        legacy = (
            await effects(store, ToolCatalog([old])).prepare(old.name, {}, context=ctx)
        ).effect_plan
        assert legacy
        cold = effects(SQLiteKernelStore())
        with pytest.raises(ToolPipelineError) as caught:
            await cold.execute_confirmed(legacy.plan_id, context=ctx)
        assert (
            caught.value.code == "tool_not_found" and not caught.value.execution_started
        )
        old_write.assert_not_called()
        with pytest.raises(ToolPipelineError):
            await cold.execute_confirmed(legacy.plan_id, context=ctx)
        assert not backend.writes

    asyncio.run(check())


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="需要可运行 Bubblewrap 的 Linux 隔离工作区")
def test_refresh_agent_turn_delivers_answer_without_approval(backend, tmp_path, monkeypatch):
    from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
    from app.agent.kernel.session import AgentSession
    from app.agent.kernel.state_types import AgentInput
    from tests.test_agent_kernel_core import ScriptedModel

    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))
    async def check():
        store = SQLiteKernelStore()
        catalog = ToolCatalog(media_refresh_tools())
        pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store,
                                effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))
        catalog.register(sandbox.run_code_tool(pipeline))
        model = ScriptedModel([
            [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("refresh", sandbox.RUN_CODE_TOOL, {
                "code": f'from mf import media\nprint(media.refresh("movie-49", server={backend.server!r}, mode="replace"))',
            })), ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
            [ModelEvent(ModelEventType.TEXT_DELTA, text="已提交覆盖元数据刷新。"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
        ])
        session = AgentSession(model=model, catalog=catalog, pipeline=pipeline, state_store=store)
        events = [event async for event in session.run(AgentInput(owner=OWNER, session_id="refresh-turn", message="覆盖这部电影的元数据"))]
        assert events[-1].payload["status"] == "success", events[-1].payload
        assert not any(event.type.value == "effect.approval_required" for event in events)
        assert len(model.requests) == 2 and len(backend.writes) == 1
        assert any('accepted' in message.content for message in model.requests[-1].messages if message.role == "tool")
    try:
        asyncio.run(check())
    finally:
        sandbox.clear_workspace(owner=OWNER, session_id="refresh-turn")


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="需要可运行 Bubblewrap 的 Linux 隔离工作区")
def test_workspace_python_cli_refresh_continues_without_confirmation(
    backend, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        sandbox,
        "get_runtime_paths",
        lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"),
    )

    async def check():
        store = SQLiteKernelStore()
        ctx = await context_for(store)
        pipe = ToolPipeline(
            catalog=ToolCatalog(media_refresh_tools()),
            state_store=store,
            reference_store=store,
            effect_store=ConfirmationEffectPlanStore(
                SQLiteConfirmationStore(), record_actions=True
            ),
            authorization=MediaFluxAuthorizationPolicy(),
            effect_lifecycle=MediaFluxEffectLifecycle(),
        )
        pipe.catalog.register(sandbox.run_code_tool(pipe))
        code = f"""import json,subprocess
from mf import library
page=library.search("Show",limit=50,server={backend.server!r})
episodes=library.episodes(series_id="series-1",server={backend.server!r},include_specials=True)
cli=json.loads(subprocess.check_output(["mf","library.search","--query","Show","--media-type","series","--server",{backend.server!r},"--offset","50"]))
assert page["data"]["sources"][0]["items"][49]["id"]=="movie-49"
assert len(episodes["data"]["episodes"])==651 and cli["data"]["offset"]==50
json.dump(episodes,open("inventory.json","w"))
print("full inventory saved")"""
        result = await pipe.execute(sandbox.RUN_CODE_TOOL, {"code": code}, context=ctx)
        assert result.outcome.public_content["data"]["exit_code"] == 0, (
            result.outcome.public_content
        )
        refreshed = await pipe.execute(
            sandbox.RUN_CODE_TOOL,
            {
                "code": f"""from mf import media
from pathlib import Path
result = media.refresh("movie-49",server={backend.server!r},mode="missing",replace_images=True)
assert result["status"] == "accepted"
Path("continued").write_text("refresh queued")"""
            },
            context=ctx,
        )
        assert not refreshed.effect_plan, refreshed.outcome.public_content
        assert refreshed.outcome.public_content["data"]["exit_code"] == 0, refreshed.outcome.public_content
        assert len(backend.writes) == 1
        params = {k.lower(): v for k, v in backend.writes[0][1]["params"].items()}
        assert params["metadatarefreshmode"] == "FullRefresh"
        assert params["replaceallmetadata"] == "false" and params["replaceallimages"] == "true"
        assert not await effects(SQLiteKernelStore()).get_pending_plan(owner=ctx.owner, session_id=ctx.session_id)
        assert isinstance(ctx.report_progress, AsyncMock)
        assert not any(call.args[0].get("activity", {}).get("status") == "approval_required"
                       for call in ctx.report_progress.call_args_list)
        after = await pipe.execute(
            sandbox.RUN_CODE_TOOL,
            {
                "code": """import json
from pathlib import Path
assert len(json.load(open("inventory.json"))["data"]["episodes"])==651
assert Path("continued").read_text() == "refresh queued"
print("receipt retained; script continued")"""
            },
            context=ctx,
        )
        assert after.outcome.public_content["data"]["exit_code"] == 0, (
            after.outcome.public_content
        )

    try:
        asyncio.run(check())
    finally:
        sandbox.clear_workspace(owner=OWNER, session_id="media-user-native")
