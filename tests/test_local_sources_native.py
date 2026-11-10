"""本地来源纵切：真实 SQLite/CAS、原扫描回执及原生 Python/CLI 冷确认。"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import socket
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from app import database as db, sdk
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.kernel.bootstrap import build_tool_catalog
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog, ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.ports.sdk_effects import local_source_tools
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.modules import local_media_service as service
from app.modules.local_media_scan_runs import resolve_local_media_scan
from app.modules.local_media_scheduler import LocalMediaScheduler
from app.repositories import local_media as repository
from tests.support import initialized_web_runtime

OWNER = "webk:v1:" + "d" * 64
OLD_NAMES = {"config.create_local_source", "config.update_local_source", "config.delete_local_source",
             "local_media.source_summaries", "local_media.get_source_summary",
             "local_media.set_source_trigger_enabled", "local_media.scan_sources"}


@dataclass
class Backend:
    directory: Path
    target: Path
    scheduler: LocalMediaScheduler


@pytest.fixture
def backend(monkeypatch, tmp_path):
    def offline(*args, **kwargs):
        raise AssertionError("本地来源测试禁止外网")
    monkeypatch.setattr(socket.socket, "connect", offline)
    with initialized_web_runtime():
        target = tmp_path / "library"
        target.mkdir()
        scheduler = LocalMediaScheduler(service=Mock())
        monkeypatch.setattr(scheduler, "start", Mock())
        monkeypatch.setattr("app.modules.local_media_scheduler.get_local_media_scheduler", lambda: scheduler)
        yield Backend(tmp_path, target, scheduler)
        scheduler.shutdown()


def source(backend: Backend, name: str = "来源", *, enabled: bool = True, mode: str = "move", save=None) -> int:
    root = backend.directory / name
    root.mkdir()
    return (save or repository.save_local_media_source_bundle)(name=name, local_root=str(root), qb_profile="configured:qb",
        qb_path_prefix="Z:\\Downloads\\" + name, enabled=enabled, media_type="tv", mode=mode,
        targets=[{"category": "tv", "path": str(backend.target), "provider": "jellyfin", "library_id": "real-library-id",
                  "library_name": "剧集", "server_path": "/nas/Shows"}])


def executor() -> EffectExecutor:
    store = SQLiteKernelStore()
    return EffectExecutor(catalog=ToolCatalog(local_source_tools()), state_store=store, reference_store=store,
        effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore(), record_actions=True),
        authorization=MediaFluxAuthorizationPolicy())


async def context_for(effects: EffectExecutor, *, owner: str = OWNER, session: str = "local-native") -> ToolCallContext:
    lease, _ = await effects.state_store.begin_turn(owner=owner, session_id=session, request_id="local-source")
    async def progress(_):
        pass
    return ToolCallContext(owner=owner, session_id=session, request_id="local-source", turn_id=lease.turn_id,
        lease=lease, cancellation=CancellationToken(), report_progress=progress)


def test_real_ids_complete_facts_and_physical_retirement(backend):
    removed = source(backend, "先删除")
    selected = source(backend, "保留事实")
    repository.delete_local_media_source(removed)
    with db.get_conn() as conn:
        conn.execute("UPDATE local_media_sources SET smb_user='retired-user',smb_pass='retired-secret' WHERE id=?", (selected,))
    rows = sdk.local.sources()
    detail = sdk.local.source(selected)
    assert rows == {"items": [detail], "total": 1}
    assert detail["id"] == selected and detail["id"] != 1
    assert detail["name"] == "保留事实" and detail["qb_path_prefix"] == "Z:\\Downloads\\保留事实"
    assert detail["enabled"] and detail["local_root"] == str(backend.directory / "保留事实")
    assert detail["targets"][0]["library_id"] == "real-library-id"
    assert detail["targets"][0]["server_path"] == "/nas/Shows"
    assert "retired-secret" not in json.dumps(detail) and "smb_user" not in detail
    with pytest.raises(LookupError):
        sdk.local.source(removed)
    assert not OLD_NAMES & {spec.name for spec in build_tool_catalog().visible()}
    for module in ("source_actions", "scan_actions"):
        assert importlib.util.find_spec("app.agent.domains.local_media." + module) is None
    specs = {spec.name: spec for spec in local_source_tools()}
    assert len(specs) == 6 and specs["local.sources"].effect is ToolEffect.READ
    assert specs["local.delete_source"].effect is ToolEffect.DANGER
    assert "maxItems" not in specs["local.scan_sources"].input_schema["properties"]["source_ids"]
    assert all(name in sdk.METHODS for name in specs)
    assert {name for name in specs if name in sdk.WRITE_METHODS} == {
        "local.create_source", "local.update_source", "local.delete_source", "local.scan_sources"}


def test_crud_preserves_subdirectories_target_identity_and_permissions(backend):
    root = backend.directory / "created"
    nested = root / "nested"
    nested.mkdir(parents=True)
    video = nested / "Show.S01E01.mkv"
    video.write_bytes(b"unmoved-media")
    modes = {path: stat.S_IMODE(path.stat().st_mode) for path in (root, nested, video, backend.target)}
    created = sdk.local.create_source("created", str(root))
    identifier = created["data"]["source_id"]
    assert created["ok"] and not sdk.local.source(identifier)["enabled"]
    assert sdk.local.source(identifier)["mode"] == "preview_only"
    updated = sdk.local.update_source(identifier, enabled=True, mode="move", qb_path_prefix="/qb/downloads",
        targets=[{"category": "tv", "path": str(backend.target), "provider": "emby", "library_name": "剧集", "server_path": "D:\\TV"}])
    assert updated["ok"]
    before = sdk.local.source(identifier)
    assert sdk.local.update_source(identifier, name="new-name")["ok"]
    after = sdk.local.source(identifier)
    assert after["targets"] == before["targets"] and after["qb_path_prefix"] == "/qb/downloads"
    assert sdk.local.delete_source(identifier)["ok"]
    assert not repository.list_local_media_sources()
    assert video.read_bytes() == b"unmoved-media"
    assert {path: stat.S_IMODE(path.stat().st_mode) for path in modes} == modes


def test_active_tasks_keep_web_transaction_guard_and_qb_takeover_config(backend):
    identifier = source(backend)
    task_id = repository.create_local_media_task(identifier, "torrent-hash", str(backend.directory / "来源" / "Show.mkv"), trigger="qb_completed")
    task_before = repository.get_local_media_task(task_id)
    assert task_before is not None
    assert sdk.local.update_source(identifier, name="只改名称")["ok"]
    for operation in (lambda: sdk.local.update_source(identifier, enabled=False), lambda: sdk.local.delete_source(identifier)):
        with pytest.raises(ValueError, match="未完成任务"):
            operation()
    assert sdk.local.source(identifier)["enabled"]
    task_after = repository.get_local_media_task(task_id)
    assert task_after == task_before
    repository.update_local_media_task(task_id, status="completed")
    assert sdk.local.update_source(identifier, enabled=False)["ok"]
    assert not sdk.local.source(identifier)["enabled"]
    assert sdk.local.source(identifier)["targets"][0]["server_path"] == "/nas/Shows"


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
def test_real_transaction_cas_rejects_race_after_service_recheck(backend, operation):
    identifier = source(backend)
    changes = {"name": "confirmed-name"} if operation == "update" else {}
    if operation == "create":
        root = backend.directory / "new"
        root.mkdir()
        changes = {"name": "new", "local_root": str(root), "enabled": False, "mode": "preview_only"}
    selected = None if operation == "create" else identifier
    prepared = service.prepare_local_source_change(operation, selected, changes)
    writer = repository.delete_local_media_source if operation == "delete" else repository.save_local_media_source_bundle
    def race(*args, **kwargs):
        if operation == "create":
            source(backend, "external", save=writer)
        else:
            repository.upsert_local_library_target(identifier, "tv", str(backend.target), provider="jellyfin",
                library_name="外部映射", library_id="changed", server_path="/external/TV")
        return writer(*args, **kwargs)
    with patch.object(repository, writer.__name__, side_effect=race):
        with pytest.raises(repository.LocalMediaSourceChanged):
            service.apply_local_source_change(operation, selected, changes, expected_fingerprint=prepared["fingerprint"])
    assert sdk.local.source(identifier)["name"] == "来源"
    if operation == "create":
        assert {row["name"] for row in sdk.local.sources()["items"]} == {"来源", "external"}
    else:
        assert sdk.local.source(identifier)["targets"][0]["server_path"] == "/external/TV"


@pytest.mark.parametrize("change", ["cancel", "token", "transaction_cancel", "source", "targets", "removed", "owner", "session"])
def test_cold_confirmation_cancellation_scope_and_drift_never_write(backend, change):
    identifier = source(backend)
    async def check():
        effects = executor()
        context = await context_for(effects)
        plan = (await effects.prepare("local.update_source", {"source_id": identifier, "enabled": False}, context=context)).effect_plan
        assert plan is not None and sdk.local.source(identifier)["enabled"]
        if change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "token":
            context.cancellation.cancel()
        elif change == "source":
            repository.update_local_media_source(identifier, name="external")
        elif change == "targets":
            repository.upsert_local_library_target(identifier, "movie", str(backend.target))
        elif change == "removed":
            repository.delete_local_media_source(identifier)
        elif change == "owner":
            context = await context_for(executor(), owner="webk:v1:" + "e" * 64)
        elif change == "session":
            context = await context_for(executor(), session="other-session")
        cold = executor()
        if change == "transaction_cancel":
            writer = repository.save_local_media_source_bundle
            def cancel_at_transaction(*args, **kwargs):
                context.cancellation.cancel()
                return writer(*args, **kwargs)
            with patch.object(repository, "save_local_media_source_bundle", side_effect=cancel_at_transaction):
                result = await cold.execute_confirmed(plan.plan_id, context=context)
            assert result.outcome.public_content["status"] == "cancelled"
            with pytest.raises(ToolPipelineError):
                await executor().execute_confirmed(plan.plan_id, context=replace(context, cancellation=CancellationToken()))
        else:
            with pytest.raises((ToolPipelineError, asyncio.CancelledError)) as caught:
                await cold.execute_confirmed(plan.plan_id, context=context)
            if change in {"source", "targets", "removed"}:
                assert isinstance(caught.value, ToolPipelineError)
                assert caught.value.code == "confirmation_stale" and caught.value.execution_started is False
        if change != "removed":
            assert sdk.local.source(identifier)["enabled"]
    asyncio.run(check())


@pytest.mark.parametrize("lost_at", ["write_reply", "readback"])
def test_unknown_after_commit_is_consumed_and_never_replayed(backend, lost_at):
    identifier = source(backend)
    writer = repository.save_local_media_source_bundle
    def uncertain(*args, **kwargs):
        writer(*args, **kwargs)
        raise TimeoutError("private lost reply")
    async def check():
        effects = executor()
        context = await context_for(effects)
        plan = (await effects.prepare("local.update_source", {"source_id": identifier, "enabled": False}, context=context)).effect_plan
        assert plan is not None
        target = patch.object(repository, "save_local_media_source_bundle", side_effect=uncertain) if lost_at == "write_reply" else patch.object(service, "local_source", side_effect=LookupError("private result gone"))
        with target as called:
            with pytest.raises(ToolPipelineError) as caught:
                await executor().execute_confirmed(plan.plan_id, context=context)
            assert caught.value.execution_started is True and "private" not in str(caught.value)
            with pytest.raises(ToolPipelineError):
                await executor().execute_confirmed(plan.plan_id, context=context)
            assert called.call_count == 1
        assert not sdk.local.source(identifier)["enabled"]
    asyncio.run(check())


def test_scan_freezes_all_real_ids_has_no_old_batch_limit_and_persists_task_ids(backend):
    identifiers = [source(backend, f"batch-{index}") for index in range(21)]
    paths = []
    for index, identifier in enumerate(identifiers):
        root = Path(sdk.local.source(identifier)["local_root"])
        child = root / "subdirectory"
        child.mkdir()
        video = child / f"Show{index}.S01E01.mkv"
        video.write_bytes(b"unchanged")
        paths.append(video)
    modes = {path: stat.S_IMODE(path.stat().st_mode) for path in paths}
    async def check():
        effects = executor()
        context = await context_for(effects)
        plan = (await effects.prepare("local.scan_sources", {}, context=context)).effect_plan
        assert plan is not None and plan.preview["data"]["source_ids"] == identifiers
        later = source(backend, "later")
        (backend.directory / "later" / "Later.mkv").write_bytes(b"later")
        cold = executor()
        pending = await cold.get_pending_plan(owner=OWNER, session_id=context.session_id)
        assert pending is not None and pending.plan_id == plan.plan_id
        result = await cold.execute_confirmed(plan.plan_id, context=context)
        data = result.outcome.public_content["data"]
        assert result.outcome.public_content["status"] == "accepted"
        assert data["source_ids"] == identifiers and later not in data["source_ids"]
        assert len(data["task_ids"]) == 21 and len(set(data["task_ids"])) == 21
        receipt = resolve_local_media_scan(data["scan_ref"])
        assert receipt is not None and receipt["task_ids"] == data["task_ids"]
        assert receipt["summary"]["scan_status"] == "completed"
        for task_id in data["task_ids"]:
            task = repository.get_local_media_task(task_id)
            assert task is not None and task.source_id in identifiers and task.trigger == "scan"
        with pytest.raises(ToolPipelineError):
            await executor().execute_confirmed(plan.plan_id, context=context)
        assert len(repository.list_local_media_tasks()) == 21
    asyncio.run(check())
    assert {path: stat.S_IMODE(path.stat().st_mode) for path in modes} == modes
    assert all(path.read_bytes() == b"unchanged" for path in paths)


def test_scan_cas_at_enqueue_preserves_other_mapping_and_records_failure(backend):
    identifier = source(backend)
    candidate = backend.directory / "来源" / "Show.mkv"
    candidate.write_bytes(b"media")
    prepared = service.prepare_local_source_scan([identifier])
    def discover_then_drift(_):
        repository.upsert_local_library_target(identifier, "tv", str(backend.target), provider="jellyfin",
            library_name="changed", server_path="/changed/TV")
        return [candidate], ""
    with patch.object(backend.scheduler, "_source_candidates", side_effect=discover_then_drift):
        result = service.scan_local_sources([identifier], expected_fingerprint=prepared["fingerprint"])
    assert not result["ok"] and result["status"] == "failed"
    assert result["data"]["enqueue_failures"] == 1 and result["data"]["task_ids"] == []
    assert "不能判断" in result["error"]
    receipt = resolve_local_media_scan(result["data"]["scan_ref"])
    assert receipt is not None and receipt["summary"]["scan_status"] == "failed"
    assert sdk.local.source(identifier)["targets"][0]["server_path"] == "/changed/TV"
    assert candidate.read_bytes() == b"media"


def test_scan_query_filters_titles_and_keeps_empty_failure_and_skip_distinct(backend):
    selected = source(backend)
    root = backend.directory / "来源"
    (root / "Alpha.Show.S01E01.mkv").write_bytes(b"alpha")
    (root / "Beta.Show.S01E01.mkv").write_bytes(b"beta")
    source(backend, "preview", mode="preview_only")
    result = sdk.local.scan_sources(query=" Alpha.Show ")
    assert result["status"] == "accepted" and result["data"]["candidate_count"] == 1
    task = repository.get_local_media_task(result["data"]["task_ids"][0])
    assert task is not None and task.source_id == selected and "Alpha.Show" in task.content_path
    for query in ("/private/media", "access_token=secret", "https://private.invalid", "x" * 121):
        with pytest.raises(ValueError):
            sdk.local.scan_sources(query=query)
    empty = sdk.local.scan_sources([selected], query="No such media")
    assert empty["status"] == "completed" and not empty["data"]["task_ids"]


@pytest.mark.parametrize("language", ["python", "shell"])
@pytest.mark.parametrize("outcome", ["confirm", "cancel", "drift"])
def test_native_python_cli_full_read_then_cold_confirmation_without_script_replay(backend, language, outcome):
    if not sandbox.sandbox_supported():
        pytest.skip(sandbox.workspace_unavailable_reason())
    removed = source(backend, "removed")
    selected = source(backend, "script-source")
    repository.delete_local_media_source(removed)
    async def check():
        effects = executor()
        context = await context_for(effects, session="script-" + language + "-" + outcome)
        pipeline = ToolPipeline(catalog=effects.catalog, state_store=effects.state_store, effect_store=effects.effect_store,
                                authorization=MediaFluxAuthorizationPolicy())
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        if language == "python":
            code = ('import json\nfrom pathlib import Path\nfrom mf import local\n'
                    'facts = local.sources()\ndetail = local.source(facts["items"][0]["id"])\n'
                    'json.dump(detail, open("facts.json", "w"))\n'
                    'assert detail["targets"][0]["server_path"] == "/nas/Shows"\n'
                    'Path("before").write_text("once")\n'
                    'local.update_source(detail["id"], enabled=False)\n'
                    'Path("after").write_text("unexpected replay")')
        else:
            code = (f'mf local.sources > list.json\nmf local.source --source-id {selected} > facts.json\n'
                    'echo once > before\n'
                    f'mf local.update_source --source-id {selected} --no-enabled\n'
                    'echo unexpected > after')
        try:
            result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": code, "language": language}, context=context)
            plan = result.effect_plan
            assert plan is not None and plan.tool_name == "local.update_source", result.outcome.public_content
            assert plan.arguments == {"source_id": selected, "enabled": False}
            root, _ = sandbox._workspace_location(context.owner, context.session_id)
            facts = json.loads((root / "work/facts.json").read_text())
            assert facts == sdk.local.source(selected)
            assert (root / "work/before").read_text().strip() == "once" and not (root / "work/after").exists()
            assert sdk.local.source(selected)["enabled"]
            cold = executor()
            pending = await cold.get_pending_plan(owner=OWNER, session_id=context.session_id)
            assert pending is not None and pending.plan_id == plan.plan_id
            if outcome == "cancel":
                assert await cold.cancel_effect(plan.plan_id, lease=context.lease)
            elif outcome == "drift":
                repository.upsert_local_library_target(selected, "tv", str(backend.target), provider="jellyfin",
                    library_name="external", server_path="/external/TV")
                with pytest.raises(ToolPipelineError):
                    await cold.execute_confirmed(plan.plan_id, context=context)
            else:
                accepted = await cold.execute_confirmed(plan.plan_id, context=context)
                assert accepted.outcome.public_content["status"] == "completed"
            assert sdk.local.source(selected)["enabled"] is (outcome != "confirm")
            assert not (root / "work/after").exists()
            with pytest.raises(ToolPipelineError):
                await executor().execute_confirmed(plan.plan_id, context=context)
        finally:
            sandbox.clear_workspace(owner=context.owner, session_id=context.session_id)
    asyncio.run(check())


def test_native_rpc_scan_keeps_batch_receipt_ids_across_cold_confirmation(backend, tmp_path):
    identifiers = [source(backend, "rpc-a"), source(backend, "rpc-b")]
    for name in ("rpc-a", "rpc-b"):
        (backend.directory / name / "Show.mkv").write_bytes(b"unchanged")
    async def check():
        effects = executor()
        context = await context_for(effects)
        (tmp_path / "run").mkdir()
        bridge = sandbox._SDKRequests(context, tmp_path, effects)
        bridge.uid = os.getuid()
        request = tmp_path / "run/mf-requests/scan.json"
        request.write_text(json.dumps({"method": "local.scan_sources", "arguments": {"source_ids": identifiers}}))
        try:
            await bridge.dispatch(request.name)
            assert bridge.proposal is not None and bridge.proposal.effect_plan is not None
            plan = bridge.proposal.effect_plan
            assert plan.preview["data"]["source_ids"] == identifiers
            result = await executor().execute_confirmed(plan.plan_id, context=context)
            data = result.outcome.public_content["data"]
            assert data["source_ids"] == identifiers and len(data["task_ids"]) == 2
            receipt = resolve_local_media_scan(data["scan_ref"])
            assert receipt is not None and receipt["task_ids"] == data["task_ids"]
        finally:
            await bridge.close()
    asyncio.run(check())
