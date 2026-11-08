"""本地任务：真实ID、完整事实、原事务CAS、原生Python/CLI及冷确认。"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app import database as db, sdk
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.kernel.bootstrap import build_tool_catalog
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.ports.sdk_effects import local_task_tools
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.clients.base import SeriesCandidate, SeriesSearchResult, SeriesEpisodeInventory
from app.modules import local_media_tasks as tasks, local_media_library as library
from app.modules.local_media_scan_runs import record_local_media_scan
from app.modules.local_media_service import LocalMediaServiceError
from app.modules.media_server_path_mapping import MediaServerPathMapping
from app.repositories import local_media as repository
from tests.test_local_sources_native import Backend, backend as backend, source

OWNER = "webk:v1:" + "f" * 64
OLD_NAMES = {"local_media." + name for name in ("diagnose", "task_summaries", "inspect_task", "preview_task", "retry_task",
                                               "refresh_task_library", "verify_task_library_visibility")}


def current_task(task_id: int):
    current = db.get_local_media_task(task_id)
    assert current is not None
    return current


def task(backend: Backend, *, name: str = "one", status: str = "failed", source_id: int | None = None) -> int:
    identifier = source(backend, name) if source_id is None else source_id
    current_source = db.get_local_media_source(identifier)
    assert current_source is not None
    video = Path(current_source.local_root) / (name + ".S02E24.mkv")
    video.write_bytes(b"temporary-media")
    task_id = db.create_local_media_task(identifier, "qb-" + name, str(video), trigger="qb_completed")
    db.update_local_media_task(task_id, status=status, title=name, tmdb_id="42", media_type="tv",
        season_override=2, episode_override=24, snapshot_digest="source-digest", error="完整错误" * 1200 + " password=private-password")
    return task_id


def executor() -> EffectExecutor:
    store = SQLiteKernelStore()
    return EffectExecutor(catalog=ToolCatalog(local_task_tools()), state_store=store, reference_store=store,
        effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore(), record_actions=True), authorization=MediaFluxAuthorizationPolicy())


async def context_for(effects: EffectExecutor, *, owner: str = OWNER, session: str = "local-tasks") -> ToolCallContext:
    lease, _ = await effects.state_store.begin_turn(owner=owner, session_id=session, request_id="local-task")
    async def progress(_):
        pass
    return ToolCallContext(owner=owner, session_id=session, request_id="local-task", turn_id=lease.turn_id,
        lease=lease, cancellation=CancellationToken(), report_progress=progress)


def test_complete_error_files_real_ids_and_no_domain_context(backend):
    removed = task(backend, name="removed")
    db.update_local_media_task(removed, status="completed")
    db.delete_local_media_tasks([removed])
    selected = task(backend)
    for index in range(65):
        db.add_local_media_task_item(selected, f"/temporary/source/{index}.mkv", f"/temporary/target/{index}.mkv", role="video", action="skip")
    detail = sdk.local.task(selected)
    assert detail["task"]["task_id"] == selected and selected != 1
    assert len(detail["items"]) == 65 and len(detail["task"]["file_names"]) == 65
    assert not detail["task"]["file_names_truncated"]
    assert detail["task"]["error"].startswith("完整错误" * 1200)
    assert "private-password" not in json.dumps(detail)
    assert "task_number" not in detail["task"] and "inspection_number" not in detail["task"]
    assert not OLD_NAMES & {spec.name for spec in build_tool_catalog().visible()}
    for name in ("task_actions", "task_arguments", "task_context", "common", "task_library", "task_queries", "queries", "tools"):
        assert importlib.util.find_spec("app.agent.domains.local_media." + name) is None
    assert {spec.name for spec in local_task_tools()} == {"local." + name for name in ("tasks", "task", "preview", "retry", "refresh_library", "verify_library", "diagnose")}


def test_pages_have_real_totals_filter_before_limit_and_scope_never_widens(backend):
    source_id = source(backend)
    identifiers = [task(backend, name=f"task-{i}", source_id=source_id, status="requires_manual" if i % 2 else "failed") for i in range(127)]
    unrelated = task(backend, name="unrelated", status="completed")
    first = sdk.local.tasks(scope="attention", source_id=source_id, limit=80)
    second = sdk.local.tasks(scope="attention", source_id=source_id, limit=80, offset=first["next_offset"])
    assert (first["total"], first["returned"], second["total"], second["returned"]) == (127, 80, 127, 47)
    assert {row["task_id"] for row in first["items"] + second["items"]} == set(identifiers)
    assert not second["has_more"] and second["next_offset"] is None
    scan_ref = record_local_media_scan({"task_ids": identifiers[:3]})
    batch = sdk.local.tasks(scan_ref=scan_ref)
    assert batch["total"] == 3 and unrelated not in {row["task_id"] for row in batch["items"]}
    assert sdk.local.tasks(scan_ref="LM999999")["status"] == "not_recorded"


class PreviewService:
    def __init__(self):
        self.inspections = Mock()
        self.inspected = []
        self.previews = []
        self.digest = "source-digest"

    def inspect_source(self, owner, source_id, path):
        self.inspected.append((owner, source_id, path))
        return {"inspection_id": "business-inspection", "digest": self.digest}

    def inspect_task(self, owner, task_id):
        current = current_task(task_id)
        assert current is not None and current.status == "requires_manual"
        return {**self.inspect_source(owner, current.source_id, current.content_path),
                "files": [{"name": f"file-{i}.mkv", "relative_path": f"nested/file-{i}.mkv", "role": "video"} for i in range(71)]}

    def preview(self, owner, inspection_id, **changes):
        self.previews.append((owner, inspection_id, changes))
        return {"status": "planned", "preview_digest": "preview:" + self.digest + ":" + str(changes["season_override"]) + str(changes["episode_override"]),
                "rules_snapshot": "{}", "candidates": [{"tmdb_id": str(i), "title": f"title-{i}"} for i in range(9)],
                "plans": [{"source_path": f"nested/source-{i}.mkv", "target_name": f"target-{i}.mkv", "action": "move"} for i in range(71)]}


def test_inspect_preview_and_recognition_override_keep_all_facts_without_ttl_protocol(backend, monkeypatch):
    selected = task(backend, status="requires_manual")
    preview_service = PreviewService()
    monkeypatch.setattr(tasks, "get_local_media_service", lambda: preview_service)
    detail = sdk.local.task(selected, inspect=True)
    assert len(detail["inspection"]["files"]) == 71 and "inspection_id" not in detail["inspection"]
    preview = sdk.local.preview(selected, tmdb_id="84", media_type="tv", season=2, episode=12, numbering_mode="standard")
    assert len(preview["plans"]) == 71 and len(preview["candidates"]) == 9
    assert current_task(selected).status == "requires_manual"
    result = sdk.local.retry(selected, tmdb_id="84", media_type="tv", season=2, episode=12, numbering_mode="standard")
    assert result["status"] == "accepted" and result["data"]["task_id"] == selected
    current = current_task(selected)
    assert current is not None and (current.tmdb_id, current.season_override, current.episode_override, current.numbering_mode) == ("84", 2, 12, "standard")
    assert all(call[0] == "admin" for call in preview_service.inspected)
    assert all(Path(call[2]).name == "one.S02E24.mkv" for call in preview_service.inspected)
    assert preview_service.inspections.discard.call_count == 4


def test_cold_sqlite_retry_preserves_identity_qb_and_is_once_only(backend):
    selected, unrelated = task(backend), task(backend, name="unrelated")
    before = current_task(selected)
    assert before is not None
    async def check():
        effects = executor()
        context = await context_for(effects)
        plan = (await effects.prepare("local.retry", {"task_id": selected}, context=context)).effect_plan
        assert plan is not None and current_task(selected) == before
        cold = executor()
        pending = await cold.get_pending_plan(owner=OWNER, session_id=context.session_id)
        assert pending is not None and pending.plan_id == plan.plan_id
        result = await cold.execute_confirmed(plan.plan_id, context=context)
        assert result.outcome.public_content["status"] == "accepted"
        current = current_task(selected)
        assert current is not None and current.id == before.id and current.status == "waiting_stable"
        assert (current.tmdb_id, current.media_type, current.season_override, current.episode_override, current.qb_hash) == (
            before.tmdb_id, before.media_type, before.season_override, before.episode_override, before.qb_hash)
        assert current.operation_token != before.operation_token and current.version == before.version + 1
        assert current_task(unrelated).status == "failed"
        with pytest.raises(ToolPipelineError):
            await executor().execute_confirmed(plan.plan_id, context=context)
    asyncio.run(check())


@pytest.mark.parametrize("change", ["cancel", "token", "transaction_cancel", "task", "targets", "owner", "session"])
def test_cold_confirmation_cancellation_drift_and_scope_do_not_write(backend, change):
    selected = task(backend)
    async def check():
        effects = executor()
        context = await context_for(effects)
        plan = (await effects.prepare("local.retry", {"task_id": selected}, context=context)).effect_plan
        assert plan is not None
        if change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "token":
            context.cancellation.cancel()
        elif change == "task":
            db.update_local_media_task(selected, error="different error")
        elif change == "targets":
            current = current_task(selected)
            assert current is not None
            db.upsert_local_library_target(current.source_id, "movie", str(backend.target))
        elif change == "owner":
            context = await context_for(executor(), owner="webk:v1:" + "a" * 64)
        elif change == "session":
            context = await context_for(executor(), session="different-session")
        if change == "transaction_cancel":
            writer = repository.reset_local_media_task
            def cancel(*args, **kwargs):
                context.cancellation.cancel()
                return writer(*args, **kwargs)
            with patch.object(repository, "reset_local_media_task", side_effect=cancel):
                result = await executor().execute_confirmed(plan.plan_id, context=context)
            assert result.outcome.public_content["status"] == "cancelled"
            with pytest.raises(ToolPipelineError):
                await executor().execute_confirmed(plan.plan_id, context=replace(context, cancellation=CancellationToken()))
        else:
            with pytest.raises((ToolPipelineError, asyncio.CancelledError)):
                await executor().execute_confirmed(plan.plan_id, context=context)
        assert current_task(selected).status == "failed"
    asyncio.run(check())


@pytest.mark.parametrize("change", ["error_without_version", "source", "targets"])
def test_retry_transaction_checks_frozen_facts_after_service_recheck(backend, change):
    selected = task(backend)
    prepared = tasks.prepare_local_task_retry(selected)
    writer = repository.reset_local_media_task
    before = current_task(selected)
    assert before is not None
    def race(*args, **kwargs):
        if change == "error_without_version":
            with db.get_conn() as conn:
                conn.execute("UPDATE local_media_tasks SET error='raced' WHERE id=?", (selected,))
        elif change == "source":
            db.update_local_media_source(before.source_id, enabled=False)
        else:
            db.upsert_local_library_target(before.source_id, "movie", str(backend.target))
        return writer(*args, **kwargs)
    with patch.object(repository, "reset_local_media_task", side_effect=race), pytest.raises(repository.LocalMediaTaskChanged):
        tasks.retry_local_task(selected, expected_fingerprint=prepared["fingerprint"])
    after = current_task(selected)
    assert after is not None and after.status == "failed" and after.version == before.version
    assert after.operation_token == before.operation_token


@pytest.mark.parametrize("error_type", [TimeoutError, ValueError, LocalMediaServiceError])
def test_unknown_after_retry_commit_is_consumed_never_replayed(backend, error_type):
    selected = task(backend)
    writer = repository.reset_local_media_task
    def uncertain(*args, **kwargs):
        writer(*args, **kwargs)
        raise error_type("private unknown receipt")
    async def check():
        effects = executor()
        context = await context_for(effects)
        plan = (await effects.prepare("local.retry", {"task_id": selected}, context=context)).effect_plan
        assert plan is not None
        with patch.object(repository, "reset_local_media_task", side_effect=uncertain) as called:
            with pytest.raises(ToolPipelineError) as caught:
                await executor().execute_confirmed(plan.plan_id, context=context)
            assert caught.value.execution_started is True and "private" not in str(caught.value)
            with pytest.raises(ToolPipelineError):
                await executor().execute_confirmed(plan.plan_id, context=context)
            assert called.call_count == 1
        assert current_task(selected).status == "waiting_stable"
    asyncio.run(check())


@pytest.fixture
def media_server(backend, monkeypatch):
    selected = task(backend, status="completed")
    db.add_local_media_task_item(selected, str(backend.directory / "one/one.S02E24.mkv"), str(backend.target / "Show/Season 2/E24.mkv"), role="video", action="move")
    client = Mock()
    client.list_virtual_folders.return_value = [{"id": "real-library-id", "name": "剧集"}]
    client.refresh_for_paths.return_value = {"ok": True, "scope": "item", "matched": 1}
    profile = SimpleNamespace(server_type="jellyfin", enabled=True, configured=True, label="测试服务", url="https://media.invalid", credential="private-key")
    options = {"path_mappings": (), "allow_global_refresh_fallback": True}
    monkeypatch.setattr(library, "list_configured_profiles", lambda: [profile])
    monkeypatch.setattr(library, "configured_media_server_refresh_options", lambda _: options)
    monkeypatch.setattr(library, "_client_for_provider", lambda *args, **kwargs: client)
    return selected, client, profile, options


@pytest.mark.parametrize("change", ["none", "endpoint", "credential", "mapping", "target", "cancel"])
def test_refresh_cold_confirmation_freezes_binding_config_and_paths(backend, media_server, change):
    selected, client, profile, options = media_server
    async def check():
        effects = executor()
        context = await context_for(effects)
        plan = (await effects.prepare("local.refresh_library", {"task_id": selected}, context=context)).effect_plan
        assert plan is not None and "private-key" not in json.dumps(plan.public_dict())
        client.refresh_for_paths.assert_not_called()
        if change == "endpoint":
            profile.url = "https://changed.invalid"
        elif change == "credential":
            profile.credential = "changed-private-key"
        elif change == "mapping":
            options["path_mappings"] = (MediaServerPathMapping("/nas", "/changed"),)
        elif change == "target":
            current = current_task(selected)
            assert current is not None
            db.upsert_local_library_target(current.source_id, "tv", str(backend.target), provider="jellyfin", library_id="real-library-id", library_name="剧集", server_path="/changed")
        elif change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        if change == "none":
            result = await executor().execute_confirmed(plan.plan_id, context=context)
            assert result.outcome.public_content["status"] == "completed"
            client.refresh_for_paths.assert_called_once_with(["/nas/Shows/Show/Season 2"], allowed_library_ids=("real-library-id",), allow_library_fallback=False)
        else:
            with pytest.raises(ToolPipelineError):
                await executor().execute_confirmed(plan.plan_id, context=context)
            client.refresh_for_paths.assert_not_called()
    asyncio.run(check())


def test_refresh_two_owners_use_original_atomic_lease_and_unknown_not_replay(backend, media_server):
    selected, client, _, _ = media_server
    entered, release = threading.Event(), threading.Event()
    def refresh(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        raise TimeoutError("refresh result lost password=private")
    client.refresh_for_paths.side_effect = refresh
    async def check():
        first, second = executor(), executor()
        a, b = await context_for(first), await context_for(second, owner="webk:v1:" + "b" * 64)
        one = (await first.prepare("local.refresh_library", {"task_id": selected}, context=a)).effect_plan
        two = (await second.prepare("local.refresh_library", {"task_id": selected}, context=b)).effect_plan
        assert one is not None and two is not None
        pending = asyncio.create_task(first.execute_confirmed(one.plan_id, context=a))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            conflict = await second.execute_confirmed(two.plan_id, context=b)
            assert conflict.outcome.public_content["status"] == "conflict"
        finally:
            release.set()
        unknown = await pending
        assert unknown.outcome.public_content["status"] == "outcome_unknown"
        assert "password=private" not in json.dumps(unknown.outcome.public_content)
        with pytest.raises(ToolPipelineError):
            await executor().execute_confirmed(one.plan_id, context=a)
        client.refresh_for_paths.assert_called_once()
    asyncio.run(check())


def test_invalid_refresh_binding_closes_client_and_never_falls_back(backend, media_server):
    selected, client, _, _ = media_server
    client.list_virtual_folders.return_value = []
    with pytest.raises(LocalMediaServiceError):
        library.prepare_local_task_refresh(selected)
    client.close.assert_called_once()
    client.refresh_for_paths.assert_not_called()


@pytest.mark.parametrize("inventory,expected", [(SeriesEpisodeInventory([], truncated=True), "inconclusive"),
    (SeriesEpisodeInventory([]), "missing"), (SeriesEpisodeInventory([(2, 24)], truncated=True), "visible")])
def test_visibility_does_not_turn_incomplete_inventory_into_missing_or_playback(backend, media_server, inventory, expected):
    selected, client, _, _ = media_server
    client.find_series_candidates_by_tmdb.return_value = SeriesSearchResult([SeriesCandidate("series-id", "Show")])
    client.list_series_episode_inventory.return_value = inventory
    result = sdk.local.verify_library(selected)
    assert result["status"] == expected and result["data"]["playback_status"] == "not_checked"
    client.find_series_candidates_by_tmdb.assert_called_once_with("42", limit=20, parent_id="real-library-id")
    client.refresh_for_paths.assert_not_called()


@pytest.mark.parametrize("language", ["python", "shell"])
@pytest.mark.parametrize("outcome", ["confirm", "cancel", "drift"])
def test_native_read_complete_error_then_cold_retry_without_replaying_script(backend, language, outcome):
    if not sandbox.sandbox_supported():
        pytest.skip(sandbox.workspace_unavailable_reason())
    selected = task(backend)
    async def check():
        effects = executor()
        context = await context_for(effects, session=language + "-" + outcome)
        pipeline = ToolPipeline(catalog=effects.catalog, state_store=effects.state_store, effect_store=effects.effect_store,
                                authorization=MediaFluxAuthorizationPolicy())
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        if language == "python":
            code = ('import json\nfrom pathlib import Path\nfrom mf import local\n'
                    'page=local.tasks(scope="failed",limit=1)\n'
                    'detail=local.task(page["items"][0]["task_id"])\n'
                    'json.dump(detail,open("facts.json","w"))\n'
                    'assert detail["task"]["error"].startswith("完整错误"*1200)\n'
                    'Path("before").write_text("once")\nlocal.retry(detail["task"]["task_id"])\n'
                    'Path("after").write_text("unexpected")')
        else:
            code = (f'mf local.tasks --scope failed --limit 1 > page.json\nmf local.task --task-id {selected} > facts.json\n'
                    'echo once > before\n' + f'mf local.retry --task-id {selected}\n' + 'echo unexpected > after')
        try:
            result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": code, "language": language}, context=context)
            plan = result.effect_plan
            assert plan is not None and plan.tool_name == "local.retry", result.outcome.public_content
            assert plan.arguments == {"task_id": selected, "confirm_interrupted_write": False}
            root, _ = sandbox._workspace_location(context.owner, context.session_id)
            detail = json.loads((root / "work/facts.json").read_text())
            assert detail["task"]["error"].startswith("完整错误" * 1200)
            assert "private-password" not in json.dumps(detail)
            assert (root / "work/before").read_text().strip() == "once" and not (root / "work/after").exists()
            cold = executor()
            pending = await cold.get_pending_plan(owner=OWNER, session_id=context.session_id)
            assert pending is not None and pending.plan_id == plan.plan_id
            if outcome == "cancel":
                assert await cold.cancel_effect(plan.plan_id, lease=context.lease)
            elif outcome == "drift":
                db.update_local_media_task(selected, error="changed")
                with pytest.raises(ToolPipelineError):
                    await cold.execute_confirmed(plan.plan_id, context=context)
            else:
                accepted = await cold.execute_confirmed(plan.plan_id, context=context)
                assert accepted.outcome.public_content["status"] == "accepted"
            assert current_task(selected).status == ("waiting_stable" if outcome == "confirm" else "failed")
            assert not (root / "work/after").exists()
            with pytest.raises(ToolPipelineError):
                await executor().execute_confirmed(plan.plan_id, context=context)
        finally:
            sandbox.clear_workspace(owner=context.owner, session_id=context.session_id)
    asyncio.run(check())
