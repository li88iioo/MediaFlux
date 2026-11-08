"""策略纵切：真实配置 CAS、确认账本、锁外 reload、冷确认及 Python/CLI RPC。"""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from starlette.requests import Request

from app import config, sdk
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.ports.sdk_effects import policy_config_tool
from app.modules import policy_config as service
from tests.test_agent_feature_state import backend as backend  # noqa: PLC0414
from tests.test_agent_sdk_effects import context_for

OWNER = "webk:v1:" + "f" * 64
WRITE = "settings.set_policy"
CASES = [
    ("tmdb_match_mode", "TMDB_MATCH_MODE", "loose"),
    ("login_wallpaper_mode", "LOGIN_WALLPAPER_MODE", "tmdb"),
    ("web_search_depth", "TAVILY_SEARCH_DEPTH", "advanced"),
    ("web_search_max_results", "TAVILY_MAX_RESULTS", 7),
    ("web_search_timeout_seconds", "TAVILY_TIMEOUT_SECONDS", 12),
    ("web_search_cache_ttl_seconds", "TAVILY_CACHE_TTL_SECONDS", 120),
    ("web_read_timeout_seconds", "WEB_READ_TIMEOUT_SECONDS", 12),
    ("web_read_cache_ttl_seconds", "WEB_READ_CACHE_TTL_SECONDS", 120),
    ("web_search_daily_credit_limit", "TAVILY_DAILY_CREDIT_LIMIT", 101),
    ("discovery_cache_ttl_seconds", "DISCOVERY_CACHE_TTL_SECONDS", 600),
    ("discovery_stale_ttl_seconds", "DISCOVERY_STALE_TTL_SECONDS", 86400),
    ("douban_cache_ttl_seconds", "DOUBAN_CACHE_TTL_SECONDS", 900),
    ("indexer_btbtla_min_interval_seconds", "INDEXER_BTBTLA_MIN_INTERVAL_SECONDS", 0),
]


def executor():
    store = SQLiteKernelStore()
    return EffectExecutor(catalog=ToolCatalog([policy_config_tool()]), state_store=store, reference_store=store,
        effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore(), record_actions=True),
        authorization=MediaFluxAuthorizationPolicy())


def item(policy):
    return next(row for row in sdk.settings.policies()["data"]["policies"] if row["policy"] == policy)


@pytest.mark.parametrize(("policy", "key", "value"), CASES)
def test_all_thirteen_original_policies_cold_confirm_only_one_key(backend, policy, key, value):
    config.set_and_save({"TMDB_API_KEY": "not-a-real-secret"})
    async def exercise():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        _, before = config.read_env_snapshot(config.ENV_FILE)
        plan = (await effects.prepare(WRITE, {"policy": policy, "value": value}, context=context)).effect_plan
        assert plan is not None and plan.arguments == {"policy": policy, "value": value}
        assert "not-a-real-secret" not in json.dumps([plan.public_dict(), dict(plan.metadata)])
        cold = executor()
        pending = await cold.get_pending_plan(owner=OWNER, session_id=context.session_id)
        assert pending is not None and pending.plan_id == plan.plan_id
        with patch("app.modules.login_wallpaper.schedule_login_wallpaper_refresh", return_value=True) as wallpaper:
            result = (await cold.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
            assert result["ok"] and result["status"] == "completed"
            assert result["data"]["saved"] and result["data"]["runtime_refreshed"]
            assert wallpaper.call_count == (1 if policy == "login_wallpaper_mode" else 0)
        _, after = config.read_env_snapshot(config.ENV_FILE)
        assert after == {**before, key: str(value)} and item(policy)["value"] == value
        with pytest.raises(ToolPipelineError):
            await executor().execute_confirmed(plan.plan_id, context=context)
    asyncio.run(exercise())


def test_original_invalid_persisted_values_fallback_clamp_and_override(backend, monkeypatch):
    config.set_and_save({"TMDB_MATCH_MODE": "invalid", "TAVILY_MAX_RESULTS": "999", "WEB_READ_TIMEOUT_SECONDS": "-1"})
    assert item("tmdb_match_mode")["value"] == "strict"
    assert item("web_search_max_results")["value"] == 10
    assert item("web_read_timeout_seconds")["value"] == 2
    monkeypatch.setenv("TAVILY_MAX_RESULTS", "3")
    monkeypatch.setattr(config, "_STARTUP_ENV_OVERRIDES", frozenset({"TAVILY_MAX_RESULTS"}))
    monkeypatch.setattr(config, "_SYSTEM_ENV_SNAPSHOT", {**config._SYSTEM_ENV_SNAPSHOT, "TAVILY_MAX_RESULTS": "3"})
    row = item("web_search_max_results")
    assert row["managed_by_environment"] and row["value"] == 3
    assert service.prepare_policy("web_search_max_results", 7)["preview"]["status"] == "environment_override"


@pytest.mark.parametrize("precondition", ["wallpaper", "ttl", "nochanges", "environment"])
def test_failed_or_unchanged_preflight_never_freezes_or_writes(backend, monkeypatch, precondition):
    values = {"policy": "login_wallpaper_mode", "value": "tmdb"}
    if precondition == "ttl":
        values = {"policy": "discovery_stale_ttl_seconds", "value": 600}
    elif precondition == "nochanges":
        values = {"policy": "tmdb_match_mode", "value": "strict"}
    elif precondition == "environment":
        monkeypatch.setenv("LOGIN_WALLPAPER_MODE", "default")
        monkeypatch.setattr(config, "_STARTUP_ENV_OVERRIDES", frozenset({"LOGIN_WALLPAPER_MODE"}))
    async def exercise():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        with patch.object(effects.effect_store, "freeze") as freeze, patch.object(config, "update_runtime_env_file") as publish:
            if precondition == "nochanges":
                result = await effects.prepare(WRITE, values, context=context)
                assert result.effect_plan is None and result.outcome.public_content["status"] == "no_changes"
            else:
                with pytest.raises(ToolPipelineError) as error:
                    await effects.prepare(WRITE, values, context=context)
                assert error.value.code == ("environment_override" if precondition == "environment" else "precondition_failed")
            freeze.assert_not_called()
            publish.assert_not_called()
    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["cancel", "cancel_recheck", "owner", "session", "config_aba", "cas"])
def test_changed_or_cancelled_card_cannot_publish(backend, monkeypatch, change):
    async def exercise():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        plan = (await effects.prepare(WRITE, {"policy": "web_read_timeout_seconds", "value": 15}, context=context)).effect_plan
        assert plan is not None
        if change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "cancel_recheck":
            original = service._prepare
            def cancelled(*args, **kwargs):
                result = original(*args, **kwargs)
                context.cancellation.cancel()
                return result
            monkeypatch.setattr(service, "_prepare", cancelled)
        elif change == "owner":
            context = await context_for(effects.state_store, owner="webk:v1:" + "e" * 64)
        elif change == "session":
            context = await context_for(effects.state_store, owner=OWNER, session="foreign")
        elif change == "config_aba":
            config.set_and_save({"WEB_READ_TIMEOUT_SECONDS": "11"})
            config.set_and_save({"WEB_READ_TIMEOUT_SECONDS": "10"})
        else:
            original = config.update_runtime_env_file
            def concurrent(path, updates, *, expected):
                config.update_env_file(path, {"WEB_READ_TIMEOUT_SECONDS": "12"}, expected=expected)
                return original(path, updates, expected=expected)
            monkeypatch.setattr(config, "update_runtime_env_file", concurrent)
        with pytest.raises(asyncio.CancelledError if change == "cancel_recheck" else ToolPipelineError):
            await executor().execute_confirmed(plan.plan_id, context=context)
        assert config.read_env_snapshot(config.ENV_FILE)[1].get("WEB_READ_TIMEOUT_SECONDS") != "15"
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["publisher_after_write", "readback", "reload", "worker_false", "wallpaper_error"])
def test_postwrite_failures_are_unknown_and_never_replayed(backend, monkeypatch, failure):
    policy = "login_wallpaper_mode" if failure == "wallpaper_error" else "tmdb_match_mode"
    value = "tmdb" if failure == "wallpaper_error" else "loose"
    if failure == "wallpaper_error":
        config.set_and_save({"TMDB_API_KEY": "isolated-test-key"})
    async def exercise():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        plan = (await effects.prepare(WRITE, {"policy": policy, "value": value}, context=context)).effect_plan
        assert plan is not None
        original = config.update_runtime_env_file
        def published(*args, **kwargs):
            result = original(*args, **kwargs)
            if failure == "publisher_after_write":
                raise config.AtomicPublishError("private")
            return result
        with monkeypatch.context() as scope:
            # readback 使用当前作用域，退出后仍能核对磁盘事实。
            def publish(*args, **kwargs):
                if failure == "readback":
                    result = original(*args, **kwargs)
                    scope.setattr(config, "read_env_snapshot", Mock(side_effect=OSError("private")))
                    return result
                return published(*args, **kwargs)
            writer = Mock(side_effect=publish)
            scope.setattr(config, "update_runtime_env_file", writer)
            if failure == "reload":
                scope.setattr(service, "reload_runtime", Mock(side_effect=RuntimeError("private")))
            elif failure == "wallpaper_error":
                scope.setattr("app.modules.login_wallpaper.schedule_login_wallpaper_refresh", Mock(side_effect=RuntimeError("private")))
            elif failure == "worker_false":
                scope.setattr("app.discovery.service.shutdown_discovery_service", lambda: False)
            result = (await executor().execute_confirmed(plan.plan_id, context=context)).outcome.public_content
            assert not result["ok"] and result["status"] == "outcome_unknown"
            assert "private" not in json.dumps(result)
            with pytest.raises(ToolPipelineError):
                await executor().execute_confirmed(plan.plan_id, context=context)
            writer.assert_called_once()
        assert item(policy)["value"] == value
    asyncio.run(exercise())


@pytest.mark.parametrize("writer", ["web", "sdk"])
def test_web_and_sdk_reload_share_indexer_helper_after_guards_release(backend, monkeypatch, writer):
    from app.routes.api import save_config
    with ThreadPoolExecutor(max_workers=1) as pool:
        def read():
            with config.runtime_config_read_guard():
                return config.get_int("INDEXER_BTBTLA_MIN_INTERVAL_SECONDS")
        def shutdown():
            assert pool.submit(read).result(timeout=2) == 3
            return {"web": True, "telegram": True}
        refresh = Mock(side_effect=shutdown)
        monkeypatch.setattr("app.indexers.runtime.refresh_indexer_configuration", refresh)
        if writer == "web":
            request = Request({"type": "http", "session": {"logged_in": True},
                "app": SimpleNamespace(state=SimpleNamespace(background_services_enabled=False))})
            assert save_config(request, {"INDEXER_BTBTLA_MIN_INTERVAL_SECONDS": "3"}) == {"success": True}
        else:
            async def exercise():
                effects = executor()
                context = await context_for(effects.state_store, owner=OWNER)
                plan = (await effects.prepare(WRITE, {"policy": "indexer_btbtla_min_interval_seconds", "value": 3}, context=context)).effect_plan
                assert plan is not None
                result = await effects.execute_confirmed(plan.plan_id, context=context)
                assert result.outcome.public_content["ok"]
            asyncio.run(exercise())
        refresh.assert_called_once()


@pytest.mark.parametrize(("language", "policy", "value"), [("python", "web_read_timeout_seconds", 15), ("shell", "web_read_timeout_seconds", 15), ("shell", "tmdb_match_mode", "loose")])
def test_root_python_and_cli_preserve_scalar_type_and_stop_at_confirmation(backend, language, policy, value):
    if not sandbox.sandbox_supported():
        pytest.skip(sandbox.workspace_unavailable_reason())
    async def exercise():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        pipeline = ToolPipeline(catalog=effects.catalog, state_store=effects.state_store, reference_store=effects.reference_store,
                                effect_store=effects.effect_store, authorization=MediaFluxAuthorizationPolicy())
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        code = (f'from pathlib import Path\nfrom mf import settings\n'
                f'assert settings.policies()["data"]["policy_count"] == 13\n'
                f'Path("before").write_text("once")\nsettings.set_policy({policy!r}, {value!r})\n'
                f'Path("after").write_text("unexpected")') if language == "python" else (
                'mf settings.policies > policies.json\n'
                'python -c \'import json;assert json.load(open("policies.json"))["data"]["policy_count"]==13\'\n'
                f'echo once > before\nmf settings.set_policy --policy {policy} --value {value}\necho unexpected > after')
        try:
            result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": code, "language": language}, context=context)
            plan = result.effect_plan
            assert plan is not None and plan.tool_name == WRITE, result.outcome.public_content
            assert plan.arguments == {"policy": policy, "value": value}
            root, _ = sandbox._workspace_location(context.owner, context.session_id)
            assert (root / "work/before").read_text().strip() == "once" and not (root / "work/after").exists()
            assert item(policy)["value"] != value
            await executor().execute_confirmed(plan.plan_id, context=context)
            assert item(policy)["value"] == value and not (root / "work/after").exists()
        finally:
            sandbox.clear_workspace(owner=context.owner, session_id=context.session_id)
    asyncio.run(exercise())
