"""功能配置纵切：真实配置/SQLite、原确认、CAS、reload 与离线工作区。"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.requests import Request

from app import config, sdk, database as db
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.kernel.bootstrap import build_tool_catalog
from app.agent.errors import ToolPipelineError
from app.agent.feature_gate import begin_runtime_change, read_runtime_fence
from app.agent.kernel import sandbox
from app.agent.kernel.bootstrap import build_agent_kernel_runtime
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.model import ModelEvent, ModelEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.ports.sdk_effects import feature_config_tool
from app.modules import feature_config as service
from tests.support import initialized_web_runtime
from tests.test_agent_kernel_core import ScriptedModel
from tests.test_agent_sdk_effects import context_for

OWNER = "webk:v1:" + "f" * 64
WRITE = "settings.set_feature"
SECRET = "FEATURE-CONFIG-SUPER-SECRET"


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *args, **kwargs: pytest.fail("禁止功能配置验收访问网络"))
    with initialized_web_runtime() as paths:
        config.set_and_save({"DISCOVERY_ENABLED": "0", "WEB_READ_ENABLED": "1", "TAVILY_API_KEY": SECRET})
        yield paths


def executor():
    store = SQLiteKernelStore()
    return EffectExecutor(catalog=ToolCatalog([feature_config_tool()]), state_store=store, reference_store=store,
        effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore(), record_actions=True),
        authorization=MediaFluxAuthorizationPolicy())


def item(feature):
    return next(row for row in sdk.settings.features()["features"] if row["feature"] == feature)


def test_typed_sdk_only_accepts_original_feature_permissions(backend):
    assert {"settings.features", WRITE} <= sdk.METHODS.keys()
    assert WRITE in sdk.WRITE_METHODS and "settings.features" not in sdk.WRITE_METHODS
    assert not {"config.feature_summary", "config.set_feature_state"} & {spec.name for spec in build_tool_catalog().visible()}
    assert build_agent_kernel_runtime(model=ScriptedModel([])).session.catalog.has(WRITE)
    async def check():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        before = config.read_env_snapshot(config.ENV_FILE)[0]
        for values in ({}, {"feature": "DISCOVERY_ENABLED", "enabled": True},
                {"feature": " Discovery ", "enabled": True}, {"feature": "agent", "enabled": True},
                {"feature": "discovery", "enabled": 1}, {"feature": "discovery", "enabled": "false"},
                {"feature": "discovery", "enabled": True, "key": "TMDB_API_KEY"}):
            with pytest.raises(ToolPipelineError):
                await effects.prepare(WRITE, values, context=context)
        assert config.read_env_snapshot(config.ENV_FILE)[0] == before
    asyncio.run(check())


@pytest.mark.parametrize(("feature", "enabled"), [("discovery", True), ("indexer_search", False), ("web_read", False)])
def test_sqlite_cold_confirmation_preserves_other_configuration_and_never_replays(backend, feature, enabled):
    async def check():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        _, before = config.read_env_snapshot(config.ENV_FILE)
        plan = (await effects.prepare(WRITE, {"feature": feature, "enabled": enabled}, context=context)).effect_plan
        assert plan is not None
        assert SECRET not in json.dumps([plan.public_dict(), dict(plan.metadata)], ensure_ascii=False)
        pending = await executor().get_pending_plan(owner=OWNER, session_id=context.session_id)
        assert pending is not None and pending.plan_id == plan.plan_id
        assert item(feature)["enabled"] != enabled
        done = await executor().execute_confirmed(plan.plan_id, context=context)
        public = done.outcome.public_content
        assert public["ok"] and public["status"] == "completed"
        assert public["data"]["runtime_refreshed"] and public["data"]["verification_state"] == "verified"
        _, after = config.read_env_snapshot(config.ENV_FILE)
        key = service.FEATURES[feature][0]
        assert after == {**before, key: "1" if enabled else "0"} and item(feature)["enabled"] == enabled
        with pytest.raises(ToolPipelineError):
            await executor().execute_confirmed(plan.plan_id, context=context)
    asyncio.run(check())


def test_same_state_finishes_without_card_write_reload_or_verification(backend):
    async def check():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        with patch.object(config, "update_runtime_env_file") as writer, patch.object(service, "reload_runtime") as reload:
            result = await effects.prepare(WRITE, {"feature": "discovery", "enabled": False}, context=context)
        assert result.effect_plan is None and result.outcome.public_content["status"] == "no_changes"
        assert result.outcome.public_content["ok"] and not result.outcome.public_content.get("error")
        writer.assert_not_called()
        reload.assert_not_called()
        assert await executor().get_pending_plan(owner=OWNER, session_id=context.session_id) is None
    asyncio.run(check())


@pytest.mark.parametrize("writer", ["sdk", "web"])
def test_reload_releases_confirmation_and_config_guards_before_waiting_for_old_worker(backend, monkeypatch, writer):
    from app.routes.api import save_config

    with ThreadPoolExecutor(max_workers=1) as pool:
        def read_on_old_worker():
            with config.runtime_config_read_guard():
                return config.get_bool("DISCOVERY_ENABLED")
        def shutdown():
            # 若提交者仍持有配置 guard，本反例两秒内失败，外层退出释放后 worker 可收尾。
            assert pool.submit(read_on_old_worker).result(timeout=2)
            return True
        monkeypatch.setattr("app.discovery.service.shutdown_discovery_service", shutdown)
        if writer == "web":
            request = Request({"type": "http", "session": {"logged_in": True},
                "app": SimpleNamespace(state=SimpleNamespace(background_services_enabled=False))})
            assert save_config(request, {"DISCOVERY_ENABLED": "1"}) == {"success": True}
        else:
            async def check():
                effects = executor()
                context = await context_for(effects.state_store, owner=OWNER)
                plan = (await effects.prepare(WRITE, {"feature": "discovery", "enabled": True}, context=context)).effect_plan
                assert plan is not None
                done = await effects.execute_confirmed(plan.plan_id, context=context)
                assert done.outcome.public_content["ok"]
            asyncio.run(check())
        assert item("discovery")["enabled"]


@pytest.mark.parametrize("condition", ["environment", "sites", "tavily"])
def test_dependency_and_deployment_preconditions_never_issue_card(backend, monkeypatch, condition):
    feature = "discovery"
    if condition == "environment":
        monkeypatch.setenv("DISCOVERY_ENABLED", "1")
        monkeypatch.setattr(config, "_STARTUP_ENV_OVERRIDES", frozenset({"DISCOVERY_ENABLED"}))
        monkeypatch.setattr(config, "_SYSTEM_ENV_SNAPSHOT", {**config._SYSTEM_ENV_SNAPSHOT, "DISCOVERY_ENABLED": "1"})
    elif condition == "sites":
        config.set_and_save({"INDEXER_SEARCH_ENABLED": "0", "INDEXER_ENABLED_SITES": "", "INDEXER_SUKEBEI_ENABLED": "0"})
        feature = "indexer_search"
    else:
        config.set_and_save({"WEB_SEARCH_ENABLED": "0", "TAVILY_API_KEY": ""})
        feature = "web_search"
    async def check():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        with patch.object(config, "update_runtime_env_file") as writer, pytest.raises(ToolPipelineError) as error:
            await effects.prepare(WRITE, {"feature": feature, "enabled": True}, context=context)
        assert error.value.code == ("environment_override" if condition == "environment" else "not_configured")
        writer.assert_not_called()
        assert await executor().get_pending_plan(owner=OWNER, session_id=context.session_id) is None
    asyncio.run(check())


def test_file_cas_rejects_writer_between_recheck_and_publication(backend, monkeypatch):
    prepared = service.prepare_feature("discovery", True)
    publish = config.update_runtime_env_file
    def other_writer(path, updates, *, expected):
        config.update_env_file(path, {"WEB_READ_ENABLED": "0"}, expected=expected)
        return publish(path, updates, expected=expected)
    monkeypatch.setattr(config, "update_runtime_env_file", other_writer)
    with pytest.raises(service.FeatureConflict):
        service.set_feature("discovery", True, expected_fingerprint=prepared["fingerprint"])
    _, after = config.read_env_snapshot(config.ENV_FILE)
    assert after["DISCOVERY_ENABLED"] == "0" and after["WEB_READ_ENABLED"] == "0"


def test_configuration_drift_after_card_is_rejected_without_overwrite(backend):
    async def check():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        plan = (await effects.prepare(WRITE, {"feature": "discovery", "enabled": True}, context=context)).effect_plan
        assert plan is not None
        config.set_and_save({"WEB_READ_ENABLED": "0"})
        with pytest.raises(ToolPipelineError) as error:
            await executor().execute_confirmed(plan.plan_id, context=context)
        assert error.value.execution_started is False
        assert not item("discovery")["enabled"] and not item("web_read")["enabled"]
    asyncio.run(check())


@pytest.mark.parametrize("failure", ["reload_false", "reload_error", "readback"])
def test_persisted_but_unverified_or_unreloaded_is_unknown_not_success(backend, monkeypatch, failure):
    async def check():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        plan = (await effects.prepare(WRITE, {"feature": "discovery", "enabled": True}, context=context)).effect_plan
        assert plan is not None
        with monkeypatch.context() as changes:
            if failure == "readback":
                publish = config.update_runtime_env_file
                def published(*args, **kwargs):
                    result = publish(*args, **kwargs)
                    changes.setattr(config, "read_env_snapshot", lambda *a, **k: (_ for _ in ()).throw(OSError(SECRET)))
                    return result
                changes.setattr(config, "update_runtime_env_file", published)
            elif failure == "reload_error":
                changes.setattr("app.discovery.service.shutdown_discovery_service", lambda: (_ for _ in ()).throw(OSError(SECRET)))
            else:
                changes.setattr("app.discovery.service.shutdown_discovery_service", lambda: False)
            result = (await executor().execute_confirmed(plan.plan_id, context=context)).outcome.public_content
            assert not result["ok"] and result["status"] == "outcome_unknown"
            assert SECRET not in json.dumps(result, ensure_ascii=False)
            if failure != "readback":
                assert result["data"]["saved"] and not result["data"]["runtime_refreshed"]
        assert item("discovery")["enabled"]
        with pytest.raises(ToolPipelineError):
            await executor().execute_confirmed(plan.plan_id, context=context)
    asyncio.run(check())


@pytest.mark.parametrize("change", ["cancel", "cancellation", "owner", "session", "fence"])
def test_cancel_or_foreign_plan_never_toggles_feature(backend, change):
    async def check():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        plan = (await effects.prepare(WRITE, {"feature": "discovery", "enabled": True}, context=context)).effect_plan
        assert plan is not None
        if change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "cancellation":
            context.cancellation.cancel()
        elif change == "owner":
            context = await context_for(effects.state_store, owner="webk:v1:" + "a" * 64)
        elif change == "session":
            context = await context_for(effects.state_store, owner=OWNER, session="foreign")
        else:
            with db.get_conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                begin_runtime_change(conn, read_runtime_fence(conn))
        with pytest.raises((ToolPipelineError, asyncio.CancelledError)):
            await executor().execute_confirmed(plan.plan_id, context=context)
        assert not item("discovery")["enabled"]
    asyncio.run(check())


def test_cold_confirmation_survives_real_process_restart(backend):
    async def check():
        runtime = build_agent_kernel_runtime(model=ScriptedModel([]))
        context = await context_for(runtime.store, owner=OWNER, session="feature-cold")
        plan = (await runtime.session.pipeline.prepare(WRITE, {"feature": "discovery", "enabled": True}, context=context)).effect_plan
        assert plan is not None
        env = {key: value for key, value in os.environ.items() if key not in config.all_items()}
        env.update({**config._SYSTEM_ENV_SNAPSHOT, "PYTHONPATH": os.getcwd(), "APP_ENV": "development",
            "MEDIAFLUX_TEST_MODE": "0", "MEDIAFLUX_TEST_DB_PATH": "",
            **{f"MEDIAFLUX_{name.upper()}_DIR": str(getattr(backend, f"{name}_dir")) for name in ("data", "config", "cache", "log", "strm")}})
        child_result = subprocess.run([sys.executable, __file__, plan.plan_id], env=env, input=json.dumps(config._SYSTEM_ENV_SNAPSHOT),
                                     capture_output=True, text=True, timeout=20, check=False)
        assert child_result.returncode == 0, child_result.stderr
        events = json.loads(child_result.stdout)
        completed = [event for event in events if event["type"] == "effect.completed"]
        assert len(completed) == 1 and completed[0]["payload"]["result"]["ok"]
        assert item("discovery")["enabled"]
    asyncio.run(check())


@pytest.mark.parametrize("language", ["python", "shell"])
def test_root_rpc_reads_features_and_stops_at_one_confirmation(backend, language):
    if not sandbox.sandbox_supported():
        pytest.skip(sandbox.workspace_unavailable_reason())
    async def check():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        pipeline = ToolPipeline(catalog=effects.catalog, state_store=effects.state_store, reference_store=effects.reference_store,
                                effect_store=effects.effect_store, authorization=MediaFluxAuthorizationPolicy())
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        code = ('from pathlib import Path\nfrom mf import settings\n'
                'facts=settings.features()\nassert any(row["feature"]=="discovery" for row in facts["features"])\n'
                'Path("before").write_text("once")\nsettings.set_feature("discovery", True)\n'
                'Path("after").write_text("unexpected")') if language == "python" else (
                'mf settings.features > features.json\n'
                'python -c \'import json;assert any(row["feature"]=="discovery" for row in json.load(open("features.json"))["features"])\'\n'
                'echo once > before\nmf settings.set_feature --feature discovery --enabled\necho unexpected > after')
        try:
            result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": code, "language": language}, context=context)
            plan = result.effect_plan
            assert plan is not None and plan.tool_name == WRITE, result.outcome.public_content
            root, _ = sandbox._workspace_location(context.owner, context.session_id)
            assert (root / "work/before").read_text().strip() == "once" and not (root / "work/after").exists()
            assert not item("discovery")["enabled"]
            await executor().execute_confirmed(plan.plan_id, context=context)
            assert item("discovery")["enabled"] and not (root / "work/after").exists()
            with pytest.raises(ToolPipelineError):
                await executor().execute_confirmed(plan.plan_id, context=context)
        finally:
            sandbox.clear_workspace(owner=context.owner, session_id=context.session_id)
    asyncio.run(check())


async def child(plan_id):
    os.environ["MEDIAFLUX_TEST_MODE"] = "0"
    os.environ["MEDIAFLUX_TEST_DB_PATH"] = ""
    db.configure_database(config.PATHS.database_path, test_mode=False)
    startup = json.loads(sys.stdin.read())
    with patch.object(config, "_SYSTEM_ENV_SNAPSHOT", startup), patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
        runtime = build_agent_kernel_runtime(model=ScriptedModel([[ModelEvent(ModelEventType.FINISH)]]))
        events = [event.to_dict() async for event in runtime.session.confirm(owner=OWNER, session_id="feature-cold", plan_id=plan_id)]
        print(json.dumps(events, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(child(sys.argv[1]))
