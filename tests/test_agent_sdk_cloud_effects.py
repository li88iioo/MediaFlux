"""原生云盘写复用真实确认账本/业务冻结/持久队列，Provider 全部离线。"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
from dataclasses import asdict, replace
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app import config, database as db
from app.agent import effect_completion
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.feature_gate import begin_runtime_change, read_runtime_fence
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog, ToolEffect
from app.agent.kernel.completion_receipts import _completion_cipher
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.ports import sdk_effects
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.ports.sdk_effects import cloud_change_tool
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.clients.guangya import GuangYaClient, GuangYaFile
from app.modules import guangya_fs_change as fs
from app.repositories import organize_operation_jobs as jobs
from tests.support import initialized_web_runtime
from tests.test_agent_effect_admission import _paused_manager, _run_real_worker
from tests.test_agent_guangya_fs_gateway import FakeGatewayClient
from tests.test_guangya_directory_scrape import _DirectoryScrapeTMDB
from app.modules.scraper import TMDBScraper
from tests.test_agent_guangya_recycle_generation import _RecycleClient


class NativeGatewayClient(FakeGatewayClient, GuangYaClient):
    """沿既有离线网关实现真实客户端接口。"""

    logged_in: bool = True
    credential_generation: int = 31


@pytest.fixture
def directory_scraper(cloud_backend, monkeypatch):
    scraper = TMDBScraper()
    offline = _DirectoryScrapeTMDB()
    for method in ("match", "match_from_tmdb", "parse_media", "search_candidates", "get_detail", "get_detail_with_credits"):
        monkeypatch.setattr(scraper, method, getattr(offline, method))
    try:
        yield scraper
    finally:
        scraper.close()


def _deny_network(*_args, **_kwargs):
    raise AssertionError("native cloud effect 测试禁止外联")


def _operations(count=1):
    return [{"op": "rename", "file_id": f"native-{index}", "new_name": f"New-{index}.mp4"}
            for index in range(count)]


@pytest.fixture
def cloud_backend(tmp_path, monkeypatch):
    for method in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, method, _deny_network)
    monkeypatch.setattr(socket, "create_connection", _deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", _deny_network)
    with initialized_web_runtime() as paths:
        client = NativeGatewayClient()
        client.directories["source"].extend(
            GuangYaFile(f"native-{index}", f"Old-{index}.mp4", False, parent_id="source",
                        size=100 + index, etag=f"version-{index}") for index in range(20)
        )
        for method in ("rename", "move", "copy", "delete", "create_dir", "close"):
            monkeypatch.setattr(client, method, Mock(wraps=getattr(client, method)))
        monkeypatch.setattr(sdk_effects, "GuangYaClient", lambda: client)
        monkeypatch.setattr(fs, "_directory", lambda: tmp_path / "plans")
        monkeypatch.setattr(fs, "execute_fs_change_plan", partial(fs.execute_fs_change_plan, client_factory=lambda: client))
        with _paused_manager() as manager:
            yield SimpleNamespace(client=client, manager=manager, root=tmp_path,
                                  database_path=paths.database_path, paths=paths)


async def _context(store, *, owner="native-cloud-owner", session="native-cloud-session"):
    lease, _ = await store.begin_turn(owner=owner, session_id=session, request_id="native-cloud")
    return ToolCallContext(owner=owner, session_id=session, request_id=lease.request_id,
                           turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(),
                           report_progress=AsyncMock(), channel="web")


def _executor(store, *, effect_store=None, authorization=None):
    return EffectExecutor(
        catalog=ToolCatalog([sdk_effects.directory_scrape_tool(), cloud_change_tool(), *sdk_effects.cloud_recycle_tools()]), state_store=store, reference_store=store,
        effect_store=effect_store or ConfirmationEffectPlanStore(SQLiteConfirmationStore()),
        effect_lifecycle=MediaFluxEffectLifecycle(), authorization=authorization,
    )


@pytest.fixture
def recycle_backend(cloud_backend, monkeypatch):
    client = _RecycleClient(7)
    client.items = [GuangYaFile(f"trash-{i}", f"已删除-{i}.mkv", False, size=123, parent_id="source")
                    for i in range(20)]
    monkeypatch.setattr(client, "task_status", Mock(return_value={"data": {"status": "completed", "progress": 100}}), raising=False)
    monkeypatch.setattr(sdk_effects, "GuangYaClient", lambda: client)
    monkeypatch.setattr("app.clients.guangya.GuangYaClient", lambda: client)
    cloud_backend.client = client
    return cloud_backend


@pytest.mark.parametrize("tool", ["cloud.restore", "cloud.clear_recycle"])
def test_recycle_native_confirmation_persists_original_task_and_never_writes_twice(recycle_backend, tool):
    async def check():
        client = recycle_backend.client
        store = SQLiteKernelStore()
        context = await _context(store, owner="webk:v1:" + "f" * 64)
        effects = _executor(store, authorization=MediaFluxAuthorizationPolicy())
        arguments = {"file_ids": [item.file_id for item in client.items]} if tool == "cloud.restore" else {}
        (recycle_backend.root / "run").mkdir()
        bridge = sandbox._SDKRequests(context, recycle_backend.root, effects)
        bridge.uid = os.getuid()
        request = recycle_backend.root / "run/mf-requests/recycle.json"
        request.write_text(json.dumps({"method": tool, "arguments": arguments}))
        try:
            await bridge.dispatch(request.name)
            assert bridge.proposal is not None
            plan = bridge.proposal.effect_plan
        finally:
            await bridge.close()
        assert plan is not None and plan.preview["data"]["count"] == 20 and client.writes == []
        if tool == "cloud.restore":
            assert len(plan.preview["data"]["resources"]) == 20 and plan.effect is ToolEffect.WRITE
        else:
            assert plan.preview["data"]["irreversible"] and plan.effect is ToolEffect.DANGER
        assert "recycle_snapshot" not in json.dumps(plan.public_dict())
        client.items.reverse()  # Provider 分页顺序不改变冻结集合。
        result = await effects.execute_confirmed(plan.plan_id, context=context)
        receipt = result.outcome.public_content
        operation = "restore" if tool == "cloud.restore" else "clear"
        assert receipt["status"] == "accepted" and receipt["data"]["task_id"] == operation + "-task"
        assert receipt["data"]["verification_pending"] and not receipt["data"]["verified"]
        with pytest.raises(ToolPipelineError):
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert len(client.writes) == 1 and client.items == []
        persisted = await store.load(owner=context.owner, session_id=context.session_id)
        seed = json.loads(_completion_cipher().decrypt(persisted.metadata["effect_waits"][plan.plan_id]["sealed"].encode()))
        assert seed["tracker"] == {"kind": "guangya_task", "value": {
            "task_id": operation + "-task", "operation": "recycle_" + operation}}
        def due(current):
            current.metadata["effect_waits"][plan.plan_id]["next_poll_at"] = 0
            current.metadata["effect_next_poll_at"] = 0
            return True
        await store.update_effect_state(owner=context.owner, session_id=context.session_id, change=due)
        assert await effect_completion.poll_effect_receipts(SQLiteKernelStore()) == 1
        persisted = await store.load(owner=context.owner, session_id=context.session_id)
        completed = persisted.metadata["effect_waits"][plan.plan_id]
        assert completed["delivered"] is True and completed["final_result"]["data"]["verified"] is True
        assert completed["final_result"]["data"]["verification_pending"] is False
        client.task_status.assert_called_once_with(operation + "-task")
        assert len(client.writes) == 1
    asyncio.run(check())


@pytest.mark.parametrize("change", ["generation", "config_aba", "cancel", "cancel_during_read", "unknown"])
def test_recycle_confirm_rechecks_authority_and_cancellation_without_resubmitting_unknown(recycle_backend, monkeypatch, change):
    async def check():
        client = recycle_backend.client
        store = SQLiteKernelStore()
        context = await _context(store, owner="webk:v1:" + "f" * 64)
        effects = _executor(store, authorization=MediaFluxAuthorizationPolicy())
        plan = (await effects.prepare("cloud.restore", {"file_ids": ["trash-1"]}, context=context)).effect_plan
        assert plan is not None
        if change == "generation":
            client.credential_generation += 1
        elif change == "config_aba":
            original = config.get("TG_QB_SAVE_PATH", "")
            config.set_and_save({"TG_QB_SAVE_PATH": "/changed"})
            config.set_and_save({"TG_QB_SAVE_PATH": original})
        elif change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "cancel_during_read":
            def cancelled_read(**kwargs):
                context.cancellation.cancel()
                return list(client.items)
            monkeypatch.setattr(client, "list_recycle", cancelled_read)
        else:
            def unknown(file_ids):
                client.writes.append(("restore", tuple(file_ids)))
                raise RuntimeError("secret-provider-token")
            monkeypatch.setattr(client, "restore_from_recycle", unknown)
        if change == "unknown":
            result = await effects.execute_confirmed(plan.plan_id, context=context)
            assert result.outcome.public_content["status"] == "outcome_unknown"
            assert "secret-provider-token" not in json.dumps(result.outcome.public_content)
            with pytest.raises(ToolPipelineError):
                await effects.execute_confirmed(plan.plan_id, context=context)
            assert len(client.writes) == 1
        elif change == "cancel_during_read":
            with pytest.raises(asyncio.CancelledError):
                await effects.execute_confirmed(plan.plan_id, context=context)
            assert client.writes == []
        else:
            with pytest.raises(ToolPipelineError) as error:
                await effects.execute_confirmed(plan.plan_id, context=context)
            if change in {"generation", "config_aba"}:
                assert error.value.code == "confirmation_stale" and error.value.execution_started is False
            assert client.writes == []
    asyncio.run(check())


def test_recycle_native_catalog_retires_old_live_entries(recycle_backend):
    from app import sdk
    from app.agent.kernel.bootstrap import build_agent_kernel_runtime

    catalog = build_agent_kernel_runtime(model=SimpleNamespace()).session.catalog
    for name in ("guangya.recycle.list", "guangya.recycle.restore", "guangya.recycle.clear", "guangya.operation.status"):
        assert not catalog.has(name) and not catalog.has(name.replace(".", "__"))
    assert catalog.get("cloud.restore").effect is ToolEffect.WRITE
    assert catalog.get("cloud.clear_recycle").effect is ToolEffect.DANGER
    assert {"cloud.restore", "cloud.clear_recycle"} <= sdk.WRITE_METHODS
    assert {"cloud.recycle", "cloud.task"}.isdisjoint(sdk.WRITE_METHODS)


@pytest.mark.parametrize("tool", ["cloud.restore", "cloud.clear_recycle"])
def test_recycle_real_cold_process_confirms_same_private_snapshot_once(recycle_backend, tool):
    async def prepare():
        store = SQLiteKernelStore()
        context = await _context(store)
        arguments = {"file_ids": ["trash-1"]} if tool == "cloud.restore" else {}
        plan = (await _executor(store).prepare(tool, arguments, context=context)).effect_plan
        return context, plan
    context, plan = asyncio.run(prepare())
    assert plan is not None
    state_file = recycle_backend.root / "recycle-cold.json"
    state_file.write_text(json.dumps({
        "startup_overrides": dict(config.capture_runtime_config_snapshot().startup_overrides),
        "paths": {key: str(value) for key, value in asdict(recycle_backend.paths).items()},
        "database": str(recycle_backend.database_path), "owner": context.owner,
        "session": context.session_id, "plan_id": plan.plan_id,
        "items": [asdict(item) for item in recycle_backend.client.items],
    }))
    script = r'''
import tests
import asyncio, json, os, socket, sys
from pathlib import Path
def deny(*args, **kwargs):
    raise AssertionError("cold recycle test forbids network")
for name in ("connect", "connect_ex", "sendto"):
    setattr(socket.socket, name, deny)
state = json.loads(Path(sys.argv[1]).read_text())
from app import config, database as db, runtime_paths
paths = runtime_paths.RuntimePaths(**{key: Path(value) for key, value in state["paths"].items()})
runtime_paths.configure_runtime_paths(paths)
config.PATHS, config.ENV_FILE, config._cache = paths, paths.env_file, None
config._SYSTEM_ENV_SNAPSHOT = state["startup_overrides"]
db.configure_database(state["database"], test_mode=False)
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.ports import sdk_effects
from app.clients.guangya import GuangYaFile
from app.agent.kernel.state_types import CancellationToken, PublicationLease
from app.agent.kernel.tool_calls import ToolCallContext
from tests.test_agent_guangya_recycle_generation import _RecycleClient
from app.agent.errors import ToolPipelineError
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
client = _RecycleClient(7)
client.items = [GuangYaFile(**item) for item in state["items"]]
sdk_effects.GuangYaClient = lambda: client
async def run():
    store = SQLiteKernelStore()
    effects = EffectExecutor(catalog=ToolCatalog(sdk_effects.cloud_recycle_tools()),
        state_store=store, reference_store=store,
        effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()),
        effect_lifecycle=MediaFluxEffectLifecycle())
    current = await store.load(owner=state["owner"], session_id=state["session"])
    publication = current.metadata["active_publication"]
    lease = PublicationLease(current.owner, current.session_id, current.generation,
                             publication["turn_id"], publication["request_id"])
    async def progress(event):
        pass
    context = ToolCallContext(owner=current.owner, session_id=current.session_id, request_id=lease.request_id,
                              turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(),
                              report_progress=progress, channel="web")
    plan = await effects.get_pending_plan(owner=context.owner, session_id=context.session_id)
    assert plan.plan_id == state["plan_id"]
    accepted = await effects.execute_confirmed(plan.plan_id, context=context)
    try:
        await effects.execute_confirmed(plan.plan_id, context=context)
    except ToolPipelineError:
        pass
    else:
        raise AssertionError("cold confirmation was replayable")
    print(json.dumps({"pid": os.getpid(), "receipt": accepted.outcome.public_content, "writes": client.writes}))
asyncio.run(run())
'''
    child = subprocess.run([sys.executable, "-c", script, str(state_file)], capture_output=True, text=True,
                           timeout=30, check=False)
    assert child.returncode == 0, child.stderr
    cold = json.loads(child.stdout)
    assert cold["pid"] != os.getpid() and len(cold["writes"]) == 1
    operation = "restore" if tool == "cloud.restore" else "clear"
    assert cold["receipt"]["status"] == "accepted" and cold["receipt"]["data"]["task_id"] == operation + "-task"
    assert cold["writes"] == [[operation, ["trash-1"] if operation == "restore" else []]]
    assert recycle_backend.client.writes == []


def _job_count():
    with db.get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM organize_operation_jobs").fetchone()[0]


def test_full_twenty_preview_uses_generic_resources_and_default_trigger(cloud_backend):
    async def check():
        store = SQLiteKernelStore()
        context = await _context(store)
        result = await _executor(store).prepare("cloud.change", {"operations": _operations(20)}, context=context)
        plan = result.effect_plan
        assert plan is not None and plan.effect is ToolEffect.WRITE
        data = plan.preview["data"]
        assert data["count"] == data["stats"]["total"] == len(data["operations"]) == len(data["resources"]) == 20
        assert all(item["count"] == 1 and "改名" in item["title"] for item in data["resources"])
        assert "Old-19.mp4" in data["resources"][19]["title"] and "New-19.mp4" in data["resources"][19]["title"]
        assert [item["source"]["file_id"] for item in data["operations"]] == [f"native-{index}" for index in range(20)]
        assert plan.arguments["trigger_strm"] is True and data["trigger_strm"] is True
        assert len(plan.public_dict()["preview"]["data"]["resources"]) == 20
        assert plan.metadata["fs_change_plan_id"] not in json.dumps(plan.public_dict())
        frozen = fs.load_fs_change_plan(plan.metadata["fs_change_plan_id"], owner=context.owner)
        assert frozen["operations"] == data["operations"]
        assert frozen["fingerprint"] == plan.snapshot_fingerprint
        assert _job_count() == 0
        cloud_backend.client.rename.assert_not_called()
        cloud_backend.client.close.assert_called_once()
    asyncio.run(check())


def test_trash_promotes_danger_and_only_queues_after_confirmation(cloud_backend):
    async def check():
        store = SQLiteKernelStore()
        context = await _context(store)
        effects = _executor(store)
        result = await effects.prepare("cloud.change", {
            "operations": [{"op": "trash", "file_id": "trash"}], "trigger_strm": False,
        }, context=context)
        plan = result.effect_plan
        assert plan is not None and plan.effect is ToolEffect.DANGER
        assert plan.metadata["risk"] == plan.confirmation_contract["audit_risk"] == "danger"
        assert "移入回收站" in plan.preview["data"]["resources"][0]["title"]
        assert _job_count() == 0
        accepted = await effects.execute_confirmed(plan.plan_id, context=context)
        assert accepted.outcome.public_content["status"] == "accepted"
        assert accepted.outcome.public_content["data"]["total"] == 1
        assert accepted.outcome.public_content["data"]["operation_ref"].startswith("GY-")
        assert _job_count() == 1
        cloud_backend.client.delete.assert_not_called()
    asyncio.run(check())


def test_no_changes_returns_outcome_without_plan_or_queue(cloud_backend):
    async def check():
        store = SQLiteKernelStore()
        context = await _context(store)
        effects = _executor(store)
        result = await effects.prepare("cloud.change", {"operations": [
            {"op": "rename", "file_id": "rename", "new_name": "广告-ABC.mp4"},
        ]}, context=context)
        assert result.effect_plan is None and result.outcome.public_content["status"] == "no_changes"
        assert result.outcome.public_content["data"]["resources"] == []
        assert await effects.get_pending_plan(owner=context.owner, session_id=context.session_id) is None
        assert not list((cloud_backend.root / "plans").glob("*.json"))
        assert _job_count() == 0
        cloud_backend.client.rename.assert_not_called()
    asyncio.run(check())


def test_confirm_admits_original_job_and_completion_tracker_once(cloud_backend):
    async def check():
        store = SQLiteKernelStore()
        context = await _context(store, owner="webk:v1:" + "f" * 64)
        effects = _executor(store)
        result = await effects.prepare("cloud.change", {"operations": _operations(), "trigger_strm": False}, context=context)
        plan = result.effect_plan
        assert plan is not None
        accepted = await effects.execute_confirmed(plan.plan_id, context=context)
        state = await store.load(owner=context.owner, session_id=context.session_id)
        seed = json.loads(_completion_cipher().decrypt(state.metadata["effect_waits"][plan.plan_id]["sealed"].encode()))
        operation_ref = accepted.outcome.public_content["data"]["operation_ref"]
        assert seed["tracker"] == {"kind": "guangya_operation", "value": {"operation_ref": operation_ref}}
        assert seed["tool"] == "cloud.change" and seed["plan_id"] == plan.plan_id
        frozen = fs.load_fs_change_plan(plan.metadata["fs_change_plan_id"], owner=context.owner)
        row = jobs.get_organize_operation_job(frozen["job_id"])
        assert row is not None
        assert row["status"] == "pending" and jobs.verify_organize_operation_payload(row)
        assert row["job_kind"] == "agent_guangya_fs_change"
        cloud_backend.client.rename.assert_not_called()
        _run_real_worker(jobs.claim_organize_operation_job(row["job_id"]))
        stored_row_2 = jobs.get_organize_operation_job(row["job_id"])
        assert stored_row_2 is not None
        assert stored_row_2["status"] == "completed"
        cloud_backend.client.rename.assert_called_once_with("native-0", "New-0.mp4")
        with pytest.raises(ToolPipelineError):
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert _job_count() == 1
        cloud_backend.client.rename.assert_called_once()
        def due(current):
            current.metadata["effect_waits"][plan.plan_id]["next_poll_at"] = 0
            current.metadata["effect_next_poll_at"] = 0
            return True
        await store.update_effect_state(owner=context.owner, session_id=context.session_id, change=due)
        assert await effect_completion.poll_effect_receipts(store) == 1
        state = await store.load(owner=context.owner, session_id=context.session_id)
        assert state.metadata["effect_waits"][plan.plan_id]["delivered"] is True
    asyncio.run(check())


def test_cancel_and_unpublished_failures_only_discard_native_previews(cloud_backend):
    async def check():
        store = SQLiteKernelStore()
        context = await _context(store)
        effects = _executor(store)
        prepared = await effects.prepare("cloud.change", {"operations": _operations()}, context=context)
        plan = prepared.effect_plan
        assert plan is not None
        private_id = plan.metadata["fs_change_plan_id"]
        assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        assert not fs._plan_path(private_id).exists() and _job_count() == 0
        prepared = await effects.prepare("cloud.change", {"operations": _operations()}, context=context)
        next_plan = prepared.effect_plan
        assert next_plan is not None
        assert not await effects.cancel_effect(plan.plan_id, lease=context.lease)
        assert fs._plan_path(next_plan.metadata["fs_change_plan_id"]).exists()
        unrelated = replace(next_plan, tool_name="resources.download")
        MediaFluxEffectLifecycle().cancelled(plan=unrelated)
        assert fs._plan_path(next_plan.metadata["fs_change_plan_id"]).exists()
        await effects.discard_unpublished_effect(plan=next_plan, prepared=prepared.prepared_effect, context=context)
        assert not fs._plan_path(next_plan.metadata["fs_change_plan_id"]).exists()
        with patch.object(effects.effect_store, "freeze", side_effect=RuntimeError("freeze failed")), pytest.raises(RuntimeError):
            await effects.prepare("cloud.change", {"operations": _operations()}, context=context)
        assert not list((cloud_backend.root / "plans").glob("*.json"))
    asyncio.run(check())


def test_cancelling_awaiter_after_build_discards_only_its_undelivered_preview(cloud_backend, monkeypatch):
    built, release, closed = threading.Event(), threading.Event(), threading.Event()
    observed = {}
    original_build = fs.build_fs_change_plan
    def paused_build(*args, **kwargs):
        plan = original_build(*args, **kwargs)
        if not built.is_set():
            observed.update(plan_id=plan["plan_id"], thread=threading.get_ident())
            built.set()
            assert release.wait(10)
        return plan
    def close():
        if threading.get_ident() == observed.get("thread"):
            closed.set()
        return True
    async def check():
        store = SQLiteKernelStore()
        effects = _executor(store)
        older_context = await _context(store, session="older")
        older = (await effects.prepare("cloud.change", {"operations": _operations()}, context=older_context)).effect_plan
        monkeypatch.setattr(fs, "build_fs_change_plan", paused_build)
        monkeypatch.setattr(cloud_backend.client, "close", Mock(side_effect=close))
        cancelled_context = await _context(store, session="cancelled")
        pending = asyncio.create_task(effects.prepare("cloud.change", {"operations": _operations()}, context=cancelled_context))
        try:
            assert await asyncio.to_thread(built.wait, 10)
            pending.cancel()
            newer_context = await _context(store, session="newer")
            newer = (await effects.prepare("cloud.change", {"operations": _operations()}, context=newer_context)).effect_plan
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert await asyncio.to_thread(closed.wait, 10)
        assert not fs._plan_path(observed["plan_id"]).exists()
        assert older is not None and newer is not None
        for context, plan in ((older_context, older), (newer_context, newer)):
            assert fs._plan_path(plan.metadata["fs_change_plan_id"]).exists()
            restored = await effects.get_pending_plan(owner=context.owner, session_id=context.session_id)
            assert restored is not None and restored.plan_id == plan.plan_id
        assert _job_count() == 0
        cloud_backend.client.rename.assert_not_called()
    asyncio.run(check())


@pytest.mark.parametrize("drift", ["fingerprint", "missing", "owner", "scrape_source", "scrape_prepare", "scrape_unknown"])
def test_confirm_rejects_drifted_private_plan_without_admitting(cloud_backend, drift, monkeypatch, directory_scraper):
    if drift.startswith("scrape_"):
        from dataclasses import replace
        from app.modules import directory_scrape
        from app.modules.organize import OrganizeRules

        client = cloud_backend.client
        client.directories["source"] = [GuangYaFile("scrape-file", "Iron.Man.2008.1080p.mkv", False,
            parent_id="source", size=2 * 1024**3, etag="scrape-v1", extension="mkv")]
        rules = OrganizeRules(target_dir_id="target", small_file_mb=0, region_split=False, year_split=False,
            clean_empty=False, link_strm=False, notify_enabled=False)
        service = directory_scrape.DirectoryScrapeService(client=client, scraper=directory_scraper,
            store=directory_scrape.DirectoryScrapeStore(), rules_loader=lambda: rules)
        monkeypatch.setattr(directory_scrape, "get_directory_scrape_service", lambda: service)

        async def check_scrape():
            store = SQLiteKernelStore()
            context = await _context(store)
            effects = _executor(store)
            arguments = {"file_id": "source", "media_type": "movie", "tmdb_id": "1726"}
            if drift == "scrape_prepare":
                with pytest.raises(ToolPipelineError) as error:
                    await effects.prepare("cloud.scrape", {**arguments, "file_id": "missing"}, context=context)
                assert error.value.code == "precondition_failed"
                assert error.value.execution_started is None
                assert "原确认已使用" not in str(error.value)
                assert not (await store.load(owner=context.owner, session_id=context.session_id)).pending_effect_plan_id
            else:
                plan = (await effects.prepare("cloud.scrape", arguments, context=context)).effect_plan
                assert plan is not None
                if drift == "scrape_source":
                    client.directories["source"][0] = replace(client.directories["source"][0], name="Iron.Man.2008.720p.mkv")
                else:
                    monkeypatch.setattr(service, "submit_execution", Mock(side_effect=RuntimeError("unknown provider boundary")))
                with pytest.raises(ToolPipelineError) as error:
                    await effects.execute_confirmed(plan.plan_id, context=context)
                assert error.value.code == ("confirmation_stale" if drift == "scrape_source" else "unavailable")
                assert error.value.execution_started is (drift == "scrape_unknown")
                assert ("原确认已使用" in str(error.value)) == (drift == "scrape_source")
            assert _job_count() == 0
            client.rename.assert_not_called()
            client.move.assert_not_called()
        try:
            asyncio.run(check_scrape())
        finally:
            service.close()
        return
    async def check():
        store = SQLiteKernelStore()
        context = await _context(store)
        effects = _executor(store)
        plan = (await effects.prepare("cloud.change", {"operations": _operations()}, context=context)).effect_plan
        assert plan is not None
        private_id = plan.metadata["fs_change_plan_id"]
        frozen = fs.load_fs_change_plan(private_id, owner=context.owner)
        if drift == "missing":
            assert fs.discard_fs_change_plan(private_id, preview_only=True)
        else:
            if drift == "fingerprint":
                frozen["operations"][0]["new_name"] = "Changed.mp4"
                frozen["fingerprint"] = fs._fingerprint(frozen)
            else:
                frozen["owner_digest"] = jobs.organize_operation_owner_digest("different-owner")
            fs._atomic_write(frozen)
        with pytest.raises(ToolPipelineError) as error:
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert error.value.code == ("confirmation_stale" if drift == "fingerprint" else "precondition_failed")
        assert error.value.execution_started is False and _job_count() == 0
        cloud_backend.client.rename.assert_not_called()
    asyncio.run(check())


def test_runtime_fence_is_rechecked_in_queue_admission_transaction(cloud_backend, monkeypatch):
    original = fs.submit_fs_change_plan
    def revoke_before_enqueue(*args, **kwargs):
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            begin_runtime_change(conn, read_runtime_fence(conn))
        return original(*args, **kwargs)
    async def check():
        store = SQLiteKernelStore()
        context = await _context(store)
        effects = _executor(store)
        plan = (await effects.prepare("cloud.change", {"operations": _operations()}, context=context)).effect_plan
        assert plan is not None
        monkeypatch.setattr(fs, "submit_fs_change_plan", revoke_before_enqueue)
        with pytest.raises(ToolPipelineError) as error:
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert error.value.code == "precondition_failed" and error.value.execution_started is False
        assert _job_count() == 0
        frozen = fs.load_fs_change_plan(plan.metadata["fs_change_plan_id"], owner=context.owner)
        assert not frozen.get("job_id")
        cloud_backend.client.rename.assert_not_called()
    asyncio.run(check())


@pytest.mark.parametrize("change", ["unchanged", "config_aba", "disabled"])
def test_production_confirmation_runtime_and_authority_are_frozen(cloud_backend, change):
    async def check():
        store = SQLiteKernelStore()
        context = await _context(store, owner="webk:v1:" + "c" * 64)
        effects = _executor(store, authorization=MediaFluxAuthorizationPolicy())
        plan = (await effects.prepare("cloud.change", {"operations": _operations(), "trigger_strm": False}, context=context)).effect_plan
        assert plan is not None and plan.snapshot_fingerprint.startswith("mfkr2:")
        if change == "config_aba":
            original = config.get("TG_QB_SAVE_PATH", "")
            config.set_and_save({"TG_QB_SAVE_PATH": "/different"})
            config.set_and_save({"TG_QB_SAVE_PATH": original})
        elif change == "disabled":
            config.set_and_save({"AGENT_ENABLED": "0"})
        if change == "unchanged":
            accepted = await effects.execute_confirmed(plan.plan_id, context=context)
            assert accepted.outcome.public_content["status"] == "accepted" and _job_count() == 1
        else:
            with pytest.raises(ToolPipelineError) as error:
                await effects.execute_confirmed(plan.plan_id, context=context)
            assert error.value.code == ("confirmation_stale" if change == "config_aba" else "agent_disabled")
            assert error.value.execution_started is False and _job_count() == 0
        cloud_backend.client.rename.assert_not_called()
    asyncio.run(check())


def test_cleanup_preserves_native_plan_after_its_job_has_been_admitted(cloud_backend):
    async def check():
        store = SQLiteKernelStore()
        context = await _context(store)
        effects = _executor(store)
        plan = (await effects.prepare("cloud.change", {"operations": _operations()}, context=context)).effect_plan
        assert plan is not None
        await effects.execute_confirmed(plan.plan_id, context=context)
        private_id = plan.metadata["fs_change_plan_id"]
        before = fs._plan_path(private_id).read_bytes()
        lifecycle = MediaFluxEffectLifecycle()
        lifecycle.cancelled(plan=plan)
        lifecycle.prepare_failed(prepared=PreparedEffect(preview=plan.preview, snapshot_fingerprint=plan.snapshot_fingerprint,
                                                         metadata=plan.metadata), context=context)
        assert fs._plan_path(private_id).read_bytes() == before
        assert fs.load_fs_change_plan(private_id, owner=context.owner)["status"] == "queued"
        assert _job_count() == 1
    asyncio.run(check())


def test_prepare_projection_failure_discards_just_built_plan(cloud_backend):
    async def check():
        store = SQLiteKernelStore()
        context = await _context(store)
        with patch.object(sdk_effects, "build_confirmation_contract", side_effect=RuntimeError("projection failed")), pytest.raises(ToolPipelineError) as error:
            await _executor(store).prepare("cloud.change", {"operations": _operations()}, context=context)
        assert error.value.code == "unavailable"
        assert not list((cloud_backend.root / "plans").glob("*.json"))
        assert _job_count() == 0
        cloud_backend.client.rename.assert_not_called()
    asyncio.run(check())


def test_real_cold_sqlite_restores_full_plan_and_admits_original_completion(cloud_backend, monkeypatch):
    password = "native-cloud-cold-fixture-password"
    monkeypatch.setenv("ENV_WEB_PASSWORD", password)
    monkeypatch.setitem(config._SYSTEM_ENV_SNAPSHOT, "ENV_WEB_PASSWORD", password)
    config.set_and_save({"ENV_WEB_PASSWORD": password})
    assert config.web_credentials()[1] == password

    async def prepare():
        store = SQLiteKernelStore()
        context = await _context(store, owner="webk:v1:" + "e" * 64)
        result = await _executor(store, authorization=MediaFluxAuthorizationPolicy()).prepare(
            "cloud.change", {"operations": _operations(20), "trigger_strm": False}, context=context,
        )
        return context, result.effect_plan
    context, plan = asyncio.run(prepare())
    assert plan is not None
    state_file = cloud_backend.root / "cold.json"
    state_file.write_text(json.dumps({
        "startup_overrides": dict(config.capture_runtime_config_snapshot().startup_overrides),
        "paths": {key: str(value) for key, value in asdict(cloud_backend.paths).items()},
        "plans": str(cloud_backend.root / "plans"), "database": str(cloud_backend.database_path),
        "owner": context.owner, "session": context.session_id, "plan_id": plan.plan_id,
        "directories": {key: [asdict(item) for item in items] for key, items in cloud_backend.client.directories.items()},
    }))
    script = r'''
import tests
import asyncio, json, os, socket, sys
from pathlib import Path
from functools import partial
from unittest.mock import patch
def deny(*args, **kwargs):
    raise AssertionError("cold native cloud test forbids network")
for name in ("connect", "connect_ex", "sendto"):
    setattr(socket.socket, name, deny)
state = json.loads(Path(sys.argv[1]).read_text())
from app import config, database as db, runtime_paths
paths = runtime_paths.RuntimePaths(**{key: Path(value) for key, value in state["paths"].items()})
runtime_paths.configure_runtime_paths(paths)
config.PATHS, config.ENV_FILE, config._cache = paths, paths.env_file, None
config._SYSTEM_ENV_SNAPSHOT = state["startup_overrides"]
assert config.web_credentials()[1] == os.environ["ENV_WEB_PASSWORD"]
db.configure_database(state["database"], test_mode=False)
from app.agent.kernel.state_types import CancellationToken, PublicationLease
from app.agent.kernel.tool_calls import ToolCallContext
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.ports import sdk_effects
from app.modules import guangya_fs_change as fs
from app.clients.guangya import GuangYaFile
from app.repositories import organize_operation_jobs as jobs
from tests.test_agent_sdk_cloud_effects import _executor, _paused_manager, _run_real_worker, MediaFluxAuthorizationPolicy
from tests.test_agent_guangya_fs_gateway import FakeGatewayClient
from app.agent.kernel.completion_receipts import _completion_cipher
client = FakeGatewayClient()
client.directories = {key: [GuangYaFile(**item) for item in items] for key, items in state["directories"].items()}
sdk_effects.GuangYaClient = lambda: client
fs._directory = lambda: Path(state["plans"])
execute = fs.execute_fs_change_plan
fs.execute_fs_change_plan = partial(execute, client_factory=lambda: client)
async def run():
    store = SQLiteKernelStore()
    effects = _executor(store, authorization=MediaFluxAuthorizationPolicy())
    plan = await effects.get_pending_plan(owner=state["owner"], session_id=state["session"])
    assert plan.plan_id == state["plan_id"] and len(plan.preview["data"]["resources"]) == 20
    assert len(plan.preview["data"]["operations"]) == 20
    current = await store.load(owner=state["owner"], session_id=state["session"])
    publication = current.metadata["active_publication"]
    lease = PublicationLease(current.owner, current.session_id, current.generation,
                             publication["turn_id"], publication["request_id"])
    async def progress(_event):
        pass
    context = ToolCallContext(owner=current.owner, session_id=current.session_id,
                              turn_id=lease.turn_id, request_id=lease.request_id, lease=lease,
                              cancellation=CancellationToken(), report_progress=progress, channel="web")
    with _paused_manager():
        accepted = await effects.execute_confirmed(plan.plan_id, context=context)
    frozen = fs.load_fs_change_plan(plan.metadata["fs_change_plan_id"], owner=current.owner)
    row = jobs.get_organize_operation_job(frozen["job_id"])
    persisted = await store.load(owner=current.owner, session_id=current.session_id)
    seed = json.loads(_completion_cipher().decrypt(persisted.metadata["effect_waits"][plan.plan_id]["sealed"].encode()))
    assert seed["tool"] == "cloud.change" and seed["plan_id"] == plan.plan_id
    _run_real_worker(jobs.claim_organize_operation_job(row["job_id"]))
    assert jobs.get_organize_operation_job(row["job_id"])["status"] == "completed"
    print(json.dumps({"pid": os.getpid(), "effect_plan_id": plan.plan_id, "job_id": row["job_id"],
                      "fs_plan_id": frozen["plan_id"], "names": [item.name for item in client.directories["source"]],
                      "receipt": accepted.outcome.public_content, "tracker": seed["tracker"]}))
asyncio.run(run())
'''
    child = subprocess.run([sys.executable, "-c", script, str(state_file)], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "ENV_WEB_PASSWORD": password})
    assert child.returncode == 0, child.stderr
    cold = json.loads(child.stdout)
    assert cold["pid"] != os.getpid() and cold["effect_plan_id"] == plan.plan_id
    assert cold["fs_plan_id"] == plan.metadata["fs_change_plan_id"]
    assert cold["receipt"]["status"] == "accepted" and cold["receipt"]["data"]["total"] == 20
    assert {f"New-{index}.mp4" for index in range(20)}.issubset(cold["names"])
    assert cold["tracker"]["value"]["operation_ref"] == cold["receipt"]["data"]["operation_ref"]
    stored_row_1 = jobs.get_organize_operation_job(cold["job_id"])
    assert stored_row_1 is not None
    assert stored_row_1["status"] == "completed"
    assert _job_count() == 1
    cloud_backend.client.rename.assert_not_called()


def test_completed_task_replay_uses_shared_status_instead_of_accepted(cloud_backend, monkeypatch):
    original = fs.submit_fs_change_plan
    def completed_before_reply(*args, **kwargs):
        receipt = original(*args, **kwargs)
        _run_real_worker(jobs.claim_organize_operation_job(receipt["task_id"]))
        return original(*args, **kwargs)
    async def check():
        store = SQLiteKernelStore()
        context = await _context(store)
        effects = _executor(store)
        plan = (await effects.prepare("cloud.change", {"operations": _operations(), "trigger_strm": False}, context=context)).effect_plan
        assert plan is not None
        monkeypatch.setattr(fs, "submit_fs_change_plan", completed_before_reply)
        with patch.object(sdk_effects, "get_organize_operation_status", wraps=sdk_effects.get_organize_operation_status) as status:
            replay = await effects.execute_confirmed(plan.plan_id, context=context)
        result = replay.outcome.public_content
        assert result["ok"] is True and result["status"] == "completed"
        assert result["data"]["replayed"] is True and result["data"]["queued"] is False
        assert result["data"]["total"] == result["data"]["task"]["stats"]["total"] == 1
        status.assert_called_once_with(result["data"]["operation_ref"], owner=context.owner)
        assert _job_count() == 1
        cloud_backend.client.rename.assert_called_once_with("native-0", "New-0.mp4")
    asyncio.run(check())


def test_native_scrape_rpc_freezes_all_twenty_one_files_with_explicit_media_identity(cloud_backend, monkeypatch, directory_scraper):
    from app.modules import directory_scrape
    from app.modules.organize import OrganizeRules

    client = cloud_backend.client
    client.directories["source"] = [GuangYaFile(f"scrape-{i}", "Iron.Man.2008.1080p.mkv", False,
        parent_id="source", size=2 * 1024**3, etag=f"scrape-etag-{i}", extension="mkv") for i in range(21)]
    rules = OrganizeRules(target_dir_id="target", small_file_mb=0, region_split=False, year_split=False,
        clean_empty=False, link_strm=False, notify_enabled=False)
    service = directory_scrape.DirectoryScrapeService(client=client, scraper=directory_scraper,
        store=directory_scrape.DirectoryScrapeStore(), rules_loader=lambda: rules)
    monkeypatch.setattr(directory_scrape, "get_directory_scrape_service", lambda: service)

    async def check():
        store = SQLiteKernelStore()
        context = await _context(store, owner="webk:v1:" + "e" * 64)
        effects = _executor(store, authorization=MediaFluxAuthorizationPolicy())
        (cloud_backend.root / "run").mkdir()
        bridge = sandbox._SDKRequests(context, cloud_backend.root, effects)
        bridge.uid = os.getuid()
        request = cloud_backend.root / "run/mf-requests/scrape.json"
        request.write_text(json.dumps({"method": "cloud.scrape", "arguments": {
            "file_id": "source", "media_type": "movie", "provider": "tmdb", "tmdb_id": "1726"}}))
        try:
            await bridge.dispatch(request.name)
            assert bridge.proposal is not None
            plan = bridge.proposal.effect_plan
            assert plan is not None
        finally:
            await bridge.close()
        assert plan.effect is ToolEffect.DANGER
        data = plan.preview["data"]
        assert len(data["plans"]) == len(data["resources"]) == data["count"] == 21
        assert {item["file_id"] for item in data["plans"]} == {f"scrape-{i}" for i in range(21)}
        assert all("tmdb:1726" in row["title"] for row in data["resources"])
        assert (data["match"]["provider"], data["match"]["external_id"], data["match"]["tmdb_id"]) == ("tmdb", "1726", "1726")
        client.rename.assert_not_called()
        client.move.assert_not_called()
        result = await effects.execute_confirmed(plan.plan_id, context=context)
        assert result.outcome.public_content["status"] == "accepted"
        payload = plan.metadata["directory_scrape_payload"]
        assert payload["version"] == 2 and payload["flow"]["selection_source"] == "automatic"
        with pytest.raises(ToolPipelineError):
            await effects.execute_confirmed(plan.plan_id, context=context)
        client.rename.assert_not_called()
        client.move.assert_not_called()
    try:
        asyncio.run(check())
    finally:
        service.close()
