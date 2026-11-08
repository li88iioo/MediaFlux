"""路径映射原生纵切：自然键、真实文件CAS、SQLite冷确认与有界Web并发。"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app import config, sdk
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.kernel.bootstrap import build_tool_catalog
from app.agent.errors import ToolPipelineError
from app.agent.feature_gate import begin_runtime_change, read_runtime_fence
from app import database as db
from app.agent.kernel import sandbox
from app.agent.kernel.bootstrap import build_agent_kernel_runtime
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.ports.sdk_effects import media_path_mapping_tools
from app.modules import media_server_path_mapping as service
from app.modules.process_lock import CrossProcessLock
from app.routes import media_libraries_api as web
from tests.support import initialized_web_runtime
from tests.test_agent_kernel_core import ScriptedModel
from tests.test_agent_sdk_effects import context_for

OWNER = "webk:v1:" + "b" * 64
ARGS = {"server": "jellyfin", "local_path": "/媒体/strm", "server_path": "//NAS/媒体"}
SECRET = "mapping-fixture-private"


@pytest.fixture
def backend(monkeypatch):
    def offline(*args, **kwargs):
        raise AssertionError("映射验收禁止外网")
    monkeypatch.setattr(socket.socket, "connect", offline)
    with initialized_web_runtime() as paths:
        config.set_and_save({"JELLYFIN_PATH_MAPPINGS": "", "EMBY_PATH_MAPPINGS":
            service.encode_media_server_path_mappings('{"/other":"//NAS/Other"}'), "TMDB_API_KEY": SECRET})
        yield paths


def executor():
    store = SQLiteKernelStore()
    return EffectExecutor(catalog=ToolCatalog(media_path_mapping_tools()), state_store=store, reference_store=store,
        effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore(), record_actions=True),
        authorization=MediaFluxAuthorizationPolicy())


def test_cold_confirm_crud_natural_key_full_facts_and_secret_boundary(backend):
    config.set_and_save({"JELLYFIN_PATH_MAPPINGS": service.encode_media_server_path_mappings(json.dumps([
        {"local": "D:/Short", "server": "/short"}, {"local": "/much/longer/prefix", "server": "/long"}]))})
    async def check():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        initial = config.read_env_snapshot(config.ENV_FILE)[0]
        plan = (await effects.prepare("library.update_path_mapping", {
            "server": "jellyfin", "local_path": "d:\\short\\", "server_path": "E:/New"}, context=context)).effect_plan
        assert plan is not None
        assert config.read_env_snapshot(config.ENV_FILE)[0] == initial
        assert not any(value in json.dumps(plan.metadata) + json.dumps(plan.public_dict())
                       for value in (SECRET, "TMDB_API_KEY", "ENV_WEB_PASSWORD", "user.env"))
        cold = executor()
        pending = await cold.get_pending_plan(owner=OWNER, session_id=context.session_id)
        assert pending is not None and pending.plan_id == plan.plan_id
        result = await cold.execute_confirmed(plan.plan_id, context=context)
        assert result.outcome.public_content["ok"]
        items = sdk.library.path_mappings("jellyfin")["items"]
        assert items == web._mapping_payload("jellyfin")[0]
        assert items == [{"local": "/much/longer/prefix", "server": "/long"}, {"local": "D:/Short", "server": "E:/New"}]
        with pytest.raises(ToolPipelineError):
            await executor().execute_confirmed(plan.plan_id, context=context)
        update = {"server": "jellyfin", "local_path": "D:/Short", "new_local_path": "/longest/replacement/path"}
        plan = (await cold.prepare("library.update_path_mapping", update, context=context)).effect_plan
        assert plan is not None
        await executor().execute_confirmed(plan.plan_id, context=context)
        assert sdk.library.path_mappings("jellyfin")["items"][0]["local"] == update["new_local_path"]
        plan = (await cold.prepare("library.delete_path_mapping", {
            "server": "jellyfin", "local_path": update["new_local_path"]}, context=context)).effect_plan
        assert plan is not None
        await executor().execute_confirmed(plan.plan_id, context=context)
        assert sdk.library.path_mappings("jellyfin")["items"] == [{"local": "/much/longer/prefix", "server": "/long"}]
        saved = config.read_env_snapshot(config.ENV_FILE)[1]
        assert saved["TMDB_API_KEY"] == SECRET
        assert json.loads(saved["EMBY_PATH_MAPPINGS"]) == [{"local": "/other", "server": "//NAS/Other"}]
    asyncio.run(check())


@pytest.mark.parametrize("change", ["cancel", "cancellation", "owner", "session", "drift", "fence"])
def test_cold_plan_scope_cancel_and_drift_never_publish(backend, change):
    async def check():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        plan = (await effects.prepare("library.create_path_mapping", ARGS, context=context)).effect_plan
        assert plan is not None
        if change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "cancellation":
            context.cancellation.cancel()
        elif change == "owner":
            context = await context_for(effects.state_store, owner="webk:v1:" + "a" * 64)
        elif change == "session":
            context = await context_for(effects.state_store, owner=OWNER, session="foreign")
        elif change == "drift":
            config.set_and_save({"STRM_SCHEDULE_ENABLED": "1"})
        else:
            with db.get_conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                begin_runtime_change(conn, read_runtime_fence(conn))
        before = config.read_env_snapshot(config.ENV_FILE)[0]
        with pytest.raises((ToolPipelineError, asyncio.CancelledError)):
            await executor().execute_confirmed(plan.plan_id, context=context)
        assert config.read_env_snapshot(config.ENV_FILE)[0] == before
        assert sdk.library.path_mappings("jellyfin")["count"] == 0
    asyncio.run(check())


@pytest.mark.parametrize("boundary", ["after_prepare", "inside_cas"])
def test_external_writer_between_precheck_and_publish_is_not_overwritten(backend, monkeypatch, boundary):
    async def check():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        plan = (await effects.prepare("library.create_path_mapping", ARGS, context=context)).effect_plan
        assert plan is not None
        initial = config.ENV_FILE.read_bytes()
        external = initial + b'\nMAPPING_RACE_SENTINEL=external\n'
        if boundary == "after_prepare":
            original_prepare = service.prepare_path_mapping_change
            def race_prepare(*args, **kwargs):
                prepared = original_prepare(*args, **kwargs)
                config.ENV_FILE.write_bytes(external)
                return prepared
            monkeypatch.setattr(service, "prepare_path_mapping_change", race_prepare)
        else:
            original_update = config.update_env_file
            def race_cas(path, updates, *, expected):
                assert expected == initial
                config.ENV_FILE.write_bytes(external)
                return original_update(path, updates, expected=expected)
            monkeypatch.setattr(config, "update_env_file", race_cas)
        with pytest.raises(ToolPipelineError) as error:
            await executor().execute_confirmed(plan.plan_id, context=context)
        assert error.value.code == "confirmation_stale" and error.value.execution_started is False
        assert config.ENV_FILE.read_bytes() == external
        assert sdk.library.path_mappings("jellyfin")["count"] == 0
    asyncio.run(check())


def test_unknown_after_real_publication_is_readable_and_cannot_replay(backend, monkeypatch):
    async def check():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        plan = (await effects.prepare("library.create_path_mapping", ARGS, context=context)).effect_plan
        assert plan is not None
        original = config.update_runtime_env_file
        calls = []
        def lost_receipt(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(result)
            raise OSError("模拟提交后的回执丢失")
        monkeypatch.setattr(config, "update_runtime_env_file", lost_receipt)
        result = await executor().execute_confirmed(plan.plan_id, context=context)
        assert result.outcome.public_content["status"] == "outcome_unknown"
        assert result.outcome.public_content["data"]["execution_started"] is True
        assert sdk.library.path_mappings("jellyfin")["items"] == [{"local": ARGS["local_path"], "server": ARGS["server_path"]}]
        with pytest.raises(ToolPipelineError):
            await executor().execute_confirmed(plan.plan_id, context=context)
        assert len(calls) == 1
    asyncio.run(check())


def test_web_and_agent_share_lock_order_with_bounded_real_concurrency(backend, monkeypatch):
    acquired = CrossProcessLock.acquire
    release = CrossProcessLock.release
    held: dict[int, set[str]] = {}
    observed = []
    def bounded(lock, blocking=True):
        deadline = time.monotonic() + 2
        while not acquired(lock, blocking=False):
            if not blocking:
                return False
            if time.monotonic() >= deadline:
                raise TimeoutError("有界锁竞争超时")
            time.sleep(0.005)
        names = held.setdefault(threading.get_ident(), set())
        if lock._name == "media-library-mappings":
            if "config-snapshot" not in names:
                release(lock)
                raise AssertionError("映射锁不能在配置锁之前获取")
            observed.append(threading.get_ident())
        names.add(lock._name)
        return True
    def released(lock):
        held.setdefault(threading.get_ident(), set()).discard(lock._name)
        release(lock)
    monkeypatch.setattr(CrossProcessLock, "acquire", bounded)
    monkeypatch.setattr(CrossProcessLock, "release", released)
    monkeypatch.setattr("app.modules.local_media_scheduler.get_local_media_scheduler", lambda: SimpleNamespace(reload=lambda: None))
    effects = executor()
    context = asyncio.run(context_for(effects.state_store, owner=OWNER))
    async def prepare():
        plan = (await effects.prepare("library.create_path_mapping", ARGS, context=context)).effect_plan
        assert plan is not None
        return plan
    plan = asyncio.run(prepare())
    ready = threading.Barrier(2, timeout=2)
    def agent():
        ready.wait()
        try:
            return asyncio.run(executor().execute_confirmed(plan.plan_id, context=context)).outcome.public_content["ok"]
        except ToolPipelineError as exc:
            assert exc.code in {"confirmation_stale", "agent_disabled"}
            return False
    def web_save():
        ready.wait()
        request = Request({"type": "http", "session": {"logged_in": True}})
        return web.save_media_library_mappings(request, {"strm_mappings": {
            "emby": [{"local": "/other", "server": "//NAS/Changed"}]}, "local_bindings": []})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(agent), pool.submit(web_save)
        first.result(timeout=6)
        web_result = second.result(timeout=6)
    assert isinstance(web_result, dict) and web_result["success"]
    assert observed
    assert sdk.library.path_mappings("emby")["items"] == [{"local": "/other", "server": "//NAS/Changed"}]
    assert config.read_env_snapshot(config.ENV_FILE)[1]["TMDB_API_KEY"] == SECRET


def test_native_help_and_real_factory_have_only_new_mapping_entries(backend):
    methods = {row["name"]: row for row in sdk.describe_methods()}
    assert "new_local_path: str" in methods["library.update_path_mapping"]["signature"]
    assert {"library.path_mappings", *(f"library.{op}_path_mapping" for op in ("create", "update", "delete"))} <= methods.keys()
    old = {"config.media_path_mappings", *(f"config.{op}_media_path_mapping" for op in ("create", "update", "delete"))}
    assert not old & {spec.name for spec in build_tool_catalog().visible()}
    runtime = build_agent_kernel_runtime(model=ScriptedModel([]))
    assert not any(runtime.session.catalog.has(name) for name in old)
    assert all(runtime.session.catalog.has(spec.name) for spec in media_path_mapping_tools())
    assert runtime.session.catalog.has("recognition.create_knowledge")
    assert runtime.session.catalog.has("library.refresh")
    with pytest.raises(ValueError):
        sdk.library.path_mappings("auto")


@pytest.mark.parametrize(("language", "outcome"), [
    ("python", "confirm"), ("shell", "confirm"), ("shell", "cancel"), ("python", "drift"), ("python", "unknown"),
])
def test_real_python_cli_cold_confirmation_stops_script_and_never_replays(backend, monkeypatch, language, outcome):
    if not sandbox.sandbox_supported():
        pytest.skip(sandbox.workspace_unavailable_reason())
    async def check():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER, session=f"mapping-{language}-{outcome}")
        pipeline = ToolPipeline(catalog=effects.catalog, state_store=effects.state_store, reference_store=effects.reference_store,
                                effect_store=effects.effect_store, authorization=MediaFluxAuthorizationPolicy())
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        if language == "python":
            code = ('from pathlib import Path\nfrom mf import library\n'
                    'assert library.path_mappings("jellyfin")["count"] == 0\n'
                    'Path("before").write_text("once")\n'
                    f'library.create_path_mapping(**{ARGS!r})\n'
                    'Path("after").write_text("unexpected")')
        else:
            code = ('mf library.path_mappings --server jellyfin\necho once > before\n'
                    'mf library.create_path_mapping --server jellyfin --local-path /媒体/strm --server-path //NAS/媒体\n'
                    'echo unexpected > after')
        try:
            result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"language": language, "code": code}, context=context)
            plan = result.effect_plan
            assert plan is not None and plan.tool_name == "library.create_path_mapping", result.outcome.public_content
            assert sdk.library.path_mappings("jellyfin")["count"] == 0
            root, _ = sandbox._workspace_location(context.owner, context.session_id)
            assert (root / "work/before").read_text().strip() == "once"
            assert not (root / "work/after").exists()
            cold = executor()
            pending = await cold.get_pending_plan(owner=OWNER, session_id=context.session_id)
            assert pending is not None and pending.plan_id == plan.plan_id
            if outcome == "cancel":
                assert await cold.cancel_effect(plan.plan_id, lease=context.lease)
            else:
                if outcome == "drift":
                    config.set_and_save({"STRM_SCHEDULE_ENABLED": "1"})
                elif outcome == "unknown":
                    original = config.update_runtime_env_file
                    def lost(*args, **kwargs):
                        original(*args, **kwargs)
                        raise OSError("模拟提交后断开")
                    monkeypatch.setattr(config, "update_runtime_env_file", lost)
                if outcome == "drift":
                    with pytest.raises(ToolPipelineError):
                        await cold.execute_confirmed(plan.plan_id, context=context)
                else:
                    written = await cold.execute_confirmed(plan.plan_id, context=context)
                    assert written.outcome.public_content["status"] == ("outcome_unknown" if outcome == "unknown" else "completed")
            with pytest.raises(ToolPipelineError):
                await executor().execute_confirmed(plan.plan_id, context=context)
            assert not (root / "work/after").exists()
            reread = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"language": "shell",
                "code": 'mf library.path_mappings --server jellyfin'}, context=context)
            assert reread.outcome.public_content["data"]["exit_code"] == 0
            expected = 0 if outcome in {"cancel", "drift"} else 1
            assert json.loads(reread.outcome.public_content["data"]["stdout"])["count"] == expected
            if (language, outcome) == ("python", "confirm"):
                for command in (
                    'mf library.update_path_mapping --server jellyfin --local-path /媒体/strm --server-path D:/Media',
                    'mf library.delete_path_mapping --server jellyfin --local-path /媒体/strm',
                ):
                    next_result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"language": "shell", "code": command}, context=context)
                    next_plan = next_result.effect_plan
                    assert next_plan is not None
                    await executor().execute_confirmed(next_plan.plan_id, context=context)
                assert sdk.library.path_mappings("jellyfin")["count"] == 0
        finally:
            sandbox.clear_workspace(owner=context.owner, session_id=context.session_id)
    asyncio.run(check())
