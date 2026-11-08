"""整理原生确认：离线 provider + 真实 SQLite、单飞管理器和工作区 RPC。"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
from app.agent.kernel.ports.sdk_effects import cloud_organize_tools
from app.modules import organize_service as service
from app.modules.organize import Organizer
from app.modules.organize_results import observe_organize_task
from app.modules.organize_tasks import OrganizeTaskManager
from tests.support import initialized_web_runtime
from tests.test_agent_kernel_existing_ports import context_for
from tests.test_agent_organize_actions import _preview_plan, _preview_stats

OWNER = "webk:v1:" + "c" * 64


@pytest.fixture
def backend(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("禁止外联")
    # AF_UNIX 留给真实隔离工作区 RPC。
    original_connect = socket.socket.connect
    def connect(sock, address):
        return original_connect(sock, address) if sock.family == socket.AF_UNIX else deny()
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    with initialized_web_runtime():
        config.set_and_save({"GY_ORGANIZE_SOURCE_DIRS": json.dumps([
            {"id": "source-a", "name": "来源 A"}, {"id": "source-b", "name": "来源 B"}]),
            "GY_ORGANIZE_TARGET_DIR": "target", "GY_ORGANIZE_CLEAN_EMPTY": "0",
            "GY_ORGANIZE_LINK_STRM": "0", "GY_ORGANIZE_NOTIFY_ENABLED": "0", "TG_CHAT_ID": ""})
        manager = OrganizeTaskManager()
        client = Mock(logged_in=True, credential_generation=1)
        client.file_info.return_value = SimpleNamespace(is_dir=True, parent_id="0")
        release = threading.Event()
        calls = []
        def organize(self, source, rules, **options):
            calls.append((source, rules, options))
            if options.get("dry_run"):
                return [_preview_plan("完整标题" * 80)], _preview_stats()
            assert release.wait(10), "测试未释放整理线程"
            return [], {**_preview_stats(), "skipped": 1}
        monkeypatch.setattr(service, "GuangYaClient", lambda: client)
        monkeypatch.setattr(service, "get_organize_manager", lambda: manager)
        monkeypatch.setattr("app.modules.organize_tasks.get_organize_manager", lambda: manager)
        monkeypatch.setattr(Organizer, "organize", organize)
        monkeypatch.setattr("app.modules.telegram_organize_lifecycle.publish_organize_lifecycle", Mock(return_value=False))
        try:
            yield SimpleNamespace(manager=manager, client=client, release=release, calls=calls)
        finally:
            release.set()
            worker = manager._worker
            if worker:
                worker.join(5)
                assert not worker.is_alive()


def executor(store):
    return EffectExecutor(catalog=ToolCatalog(cloud_organize_tools()), state_store=store, reference_store=store,
        effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()), authorization=MediaFluxAuthorizationPolicy(),
        effect_lifecycle=MediaFluxEffectLifecycle())


def runs():
    with db.get_conn() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM task_runs WHERE task_name='guangya_organize' ORDER BY id")]


async def prepare(effects, context, name="cloud.organize", arguments=None):
    result = await effects.prepare(name, arguments or {}, context=context)
    assert result.effect_plan is not None, result.outcome.public_content
    return result.effect_plan


def test_preview_real_source_selection_full_fields_and_execution_scope(backend):
    result = sdk.cloud.preview_organize(["source-b"], max_files=1)
    assert result["ok"] and result["data"]["sources"] == [{"id": "source-b", "name": "来源 B"}]
    assert result["data"]["target_dir_id"] == "target"
    assert result["data"]["plans"][0]["title"] == "完整标题" * 80
    assert result["data"]["plans"][0]["source_id"] == "source-b"
    assert result["data"]["sample_limit"] == 1 and result["data"]["truncated"]
    with pytest.raises(ValueError):
        sdk.cloud.preview_organize(["unconfigured"])
    assert runs() == []


@pytest.mark.parametrize("failure", ["not_configured", "not_logged_in", "incomplete", "unavailable", "busy", "empty", "stop_idle"])
def test_failed_or_empty_preflight_never_issues_a_card(backend, failure):
    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        effects = executor(store)
        if failure == "not_configured":
            config.set_and_save({"GY_ORGANIZE_TARGET_DIR": "0"})
        elif failure == "not_logged_in":
            backend.client.logged_in = False
        if failure == "busy":
            backend.manager._task = {"status": "running"}
        expected_status = {"not_logged_in": "not_configured", "incomplete": "inconclusive", "busy": "conflict", "stop_idle": "conflict"}.get(failure, failure)
        preflight = (patch.object(Organizer, "organize", return_value=([], _preview_stats(total=0, scan_errors=["private"] if failure == "incomplete" else [])))
                     if failure in {"incomplete", "empty"} else patch.object(Organizer, "organize", side_effect=RuntimeError("private"))
                     if failure == "unavailable" else nullcontext())
        with preflight, patch.object(effects.effect_store, "freeze", wraps=effects.effect_store.freeze) as freeze:
            if failure == "empty":
                result = await effects.prepare("cloud.organize", {}, context=context)
                assert result.effect_plan is None and result.outcome.public_content["status"] == "no_changes"
            else:
                with pytest.raises(ToolPipelineError) as error:
                    await effects.prepare("cloud.stop_organize" if failure == "stop_idle" else "cloud.organize", {}, context=context)
                assert error.value.code == expected_status
                assert "private" not in str(error.value)
            freeze.assert_not_called()
        assert runs() == []
    asyncio.run(exercise())


def test_cold_confirmation_owner_session_once_singleflight_and_stop_run_identity(backend):
    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER, session="organize")
        plan = await prepare(executor(store), context, arguments={"source_ids": ["source-b"]})
        assert runs() == []
        cold = executor(SQLiteKernelStore())
        for owner, session in [(OWNER, "wrong"), ("webk:v1:" + "d" * 64, "organize")]:
            foreign = await context_for(store, owner=owner, session=session)
            with pytest.raises(ToolPipelineError):
                await cold.execute_confirmed(plan.plan_id, context=foreign)
        accepted = (await cold.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
        assert accepted["status"] == "accepted"
        run_id = accepted["data"]["run_id"]
        assert len(runs()) == 1 and runs()[0]["id"] == run_id
        assert backend.manager.task_status()["sources"] == [{"id": "source-b", "name": "来源 B"}]
        with pytest.raises(ToolPipelineError):
            await cold.execute_confirmed(plan.plan_id, context=context)
        competing = service.run(["source-a"])
        assert not competing["ok"] and len(runs()) == 1
        context = await context_for(store, owner=OWNER, session="stop")
        stop_plan = await prepare(executor(store), context, "cloud.stop_organize", {"run_id": run_id})
        stopped = (await executor(SQLiteKernelStore()).execute_confirmed(stop_plan.plan_id, context=context)).outcome.public_content
        assert stopped["status"] == "accepted" and stopped["data"]["run_id"] == run_id
        assert backend.manager._cancel_event.is_set()
        backend.release.set()
        backend.manager._worker.join(5)
        task_id = backend.manager.task_status()["id"]
        with patch.object(service, "get_organize_manager", return_value=OrganizeTaskManager()):
            observed = observe_organize_task({"task_id": task_id, "run_id": run_id, "operation": "stop"})
            assert observed["status"] in {"stopped", "completed"}
            assert observe_organize_task({"task_id": "other", "run_id": run_id, "operation": "stop"})["status"] == "unknown"
            assert sdk.cloud.organize_status(run_id) == observed
            assert sdk.cloud.organize_status(run_id + 1000)["status"] == "unknown"
            with pytest.raises(ValueError, match="run_id"):
                sdk.cloud.organize_status(0)
        executions = [options for _, _, options in backend.calls if not options.get("dry_run")]
        assert executions and all(not options.get("max_files") for options in executions)
    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["cancel", "cancel_before", "config_aba", "target", "sources", "credentials"])
def test_cancel_and_changed_configuration_do_not_admit_a_task(backend, change):
    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        effects = executor(store)
        plan = await prepare(effects, context)
        if change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "cancel_before":
            context.cancellation.cancel()
        elif change == "credentials":
            backend.client.credential_generation += 1
        elif change == "sources":
            config.set_and_save({"GY_ORGANIZE_SOURCE_DIRS": '[{"id":"different"}]'})
        else:
            config.set_and_save({"GY_ORGANIZE_TARGET_DIR": "different"})
            if change == "config_aba":
                config.set_and_save({"GY_ORGANIZE_TARGET_DIR": "target"})
        with pytest.raises(asyncio.CancelledError if change == "cancel_before" else ToolPipelineError):
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert runs() == [] and not backend.manager.task_status().get("id")
    asyncio.run(exercise())


def test_stop_recheck_observes_cancellation_without_writing(backend):
    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        accepted = service.run(["source-b"])
        run_id = accepted["data"]["run_id"]
        effects = executor(store)
        plan = await prepare(effects, context, "cloud.stop_organize", {"run_id": run_id})
        original = service._stop_snapshot
        def cancelled_snapshot():
            snapshot = original()
            context.cancellation.cancel()
            return snapshot
        with patch.object(service, "_stop_snapshot", side_effect=cancelled_snapshot), patch.object(backend.manager, "stop", wraps=backend.manager.stop) as stop:
            with pytest.raises(asyncio.CancelledError):
                await effects.execute_confirmed(plan.plan_id, context=context)
            stop.assert_not_called()
        assert not backend.manager._cancel_event.is_set()
    asyncio.run(exercise())


def test_unknown_submission_is_not_replayed_after_restart(backend):
    original = service.run
    def lose_reply(*args, **kwargs):
        assert original(*args, **kwargs)["ok"]
        raise RuntimeError("lost reply")
    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        effects = executor(store)
        plan = await prepare(effects, context)
        with patch.object(service, "run", side_effect=lose_reply) as submission:
            with pytest.raises(ToolPipelineError) as failure:
                await effects.execute_confirmed(plan.plan_id, context=context)
            assert failure.value.execution_started is True
            with pytest.raises(ToolPipelineError):
                await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
            submission.assert_called_once()
        assert len(runs()) == 1
    asyncio.run(exercise())


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="真实 Linux root 工作区 RPC")
def test_workspace_coldconfirm_keeps_preview_and_never_replays_script(backend, monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))
    async def exercise():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER, session="workspace-organize")
        catalog = ToolCatalog(cloud_organize_tools())
        pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store,
            effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()), authorization=MediaFluxAuthorizationPolicy())
        catalog.register(sandbox.run_code_tool(pipeline))
        result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": '''
from pathlib import Path
import json
from mf import cloud
with Path("runs.txt").open("a") as output:
    output.write("run\\n")
Path("preview.json").write_text(json.dumps(cloud.preview_organize(["source-b"], max_files=1)))
Path("status.json").write_text(json.dumps(cloud.organize_status()))
cloud.organize(["source-b"])
Path("tail.txt").write_text("must not resume")
'''}, context=context)
        assert result.effect_plan is not None, result.outcome.public_content
        assert result.effect_plan.tool_name == "cloud.organize" and runs() == []
        accepted = (await executor(SQLiteKernelStore()).execute_confirmed(result.effect_plan.plan_id, context=context)).outcome.public_content
        assert accepted["status"] == "accepted" and len(runs()) == 1
        root, _ = sandbox._workspace_location(context.owner, context.session_id)
        assert (root / "work/runs.txt").read_text() == "run\n"
        assert not (root / "work/tail.txt").exists()
        preview = json.loads((root / "work/preview.json").read_text())
        assert preview["data"]["plans"][0]["source_id"] == "source-b"
        assert preview["data"]["plans"][0]["title"] == "完整标题" * 80
        assert json.loads((root / "work/status.json").read_text())["status"] == "idle"
    asyncio.run(exercise())
