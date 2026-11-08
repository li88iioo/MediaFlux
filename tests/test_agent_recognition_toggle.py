"""原生规则纵切：公共事务CAS、真实ID、缓存、格式注册表与SQLite冷确认。"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app import config, database as db, sdk
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.kernel.bootstrap import build_tool_catalog
from app.agent.errors import ToolPipelineError
from app.agent.feature_gate import begin_runtime_change, read_runtime_fence
from app.agent.kernel import sandbox
from app.agent.kernel.bootstrap import build_agent_kernel_runtime
from app.agent.kernel.capabilities import ToolCatalog, ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.model import ModelEvent, ModelEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.ports.sdk_effects import recognition_knowledge_tools, recognition_rule_tool
from app.modules import recognition_knowledge as knowledge, recognition_preprocess_rules as preprocess
from app.modules import recognition_service as service, tmdb_regex_rules as regex
from app.modules.recognition import formats
from app.modules.scraper import extract_recognition_context
from tests.support import initialized_web_runtime
from tests.test_agent_kernel_core import ScriptedModel
from tests.test_agent_sdk_effects import context_for
from tests.test_release_formats import PARENT, filename, save_teaching, teaching

OWNER = "webk:v1:" + "e" * 64
KINDS = ("preprocess_rule", "tmdb_regex_rule", "release_format")
PRIVATE = ("SECRET preprocess rule", "SECRET_PATTERN", "SECRET_REPLACEMENT", "SECRET regex rule", "SECRET_REGEX", "987654")


@pytest.fixture
def backend(monkeypatch):
    def offline(*args, **kwargs):
        raise AssertionError("识别规则验收禁止外网")
    monkeypatch.setattr(socket.socket, "connect", offline)
    with initialized_web_runtime() as paths:
        preprocess.invalidate_active_cache()
        formats.invalidate_cache()
        yield paths
        preprocess.invalidate_active_cache()
        formats.invalidate_cache()


def executor():
    store = SQLiteKernelStore()
    return EffectExecutor(catalog=ToolCatalog([recognition_rule_tool(), *recognition_knowledge_tools()]),
        state_store=store, reference_store=store,
        effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore(), record_actions=True),
        authorization=MediaFluxAuthorizationPolicy())


def seed(rule_type):
    if rule_type == "preprocess_rule":
        return preprocess.create_rule({"name": PRIVATE[0], "matcher_type": "text", "pattern": PRIVATE[1],
            "scope": "filename", "action": "replace", "replacement": PRIVATE[2], "priority": 50})
    if rule_type == "tmdb_regex_rule":
        return regex.create_rule({"name": PRIVATE[3], "pattern": PRIVATE[4], "match_target": "filename",
            "tmdb_id": PRIVATE[5], "media_type": "tv", "season_override": 2, "priority": 100})
    return save_teaching()[0]


def current(rule_type, rule_id):
    return next(row for row in sdk.recognition.rules(rule_type)["items"] if row["id"] == rule_id)


def test_rules_are_complete_read_only_real_ids_without_implicit_seed(backend, monkeypatch):
    regex_item = seed("tmdb_regex_rule")
    format_item = seed("release_format")
    with db.get_conn() as conn:
        conn.execute("DELETE FROM recognition_preprocess_rules")
    original = db.get_conn
    statements = []
    @contextmanager
    def readonly():
        with original() as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.set_trace_callback(statements.append)
            yield conn
    monkeypatch.setattr(preprocess, "get_conn", readonly)
    monkeypatch.setattr(regex, "get_conn", readonly)
    monkeypatch.setattr(db, "get_conn", readonly)
    monkeypatch.setattr(preprocess, "ensure_builtin_rules", lambda: pytest.fail("只读列表不能初始化种子"))
    assert sdk.recognition.rules("preprocess_rule") == {"rule_type": "preprocess_rule", "items": [], "total": 0}
    assert sdk.recognition.rules("tmdb_regex_rule")["items"] == [regex_item]
    assert sdk.recognition.rules("release_format")["items"] == [format_item]
    assert regex_item["id"] == format_item["id"] == 1
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")) for sql in statements)


@pytest.mark.parametrize("rule_type", KINDS)
def test_cold_confirm_changes_only_target_state_and_refreshes_real_parser(backend, rule_type):
    row = seed(rule_type)
    async def check():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        before = current(rule_type, row["id"])
        if rule_type == "preprocess_rule":
            assert any(item["id"] == row["id"] for item in preprocess.list_rules(enabled_only=True))
        elif rule_type == "release_format":
            assert extract_recognition_context(filename(15), PARENT).episode == 15
        for enabled in (False, True):
            args = {"rule_type": rule_type, "rule_id": row["id"], "enabled": enabled}
            plan = (await effects.prepare("recognition.set_rule_enabled", args, context=context)).effect_plan
            assert plan is not None
            assert current(rule_type, row["id"])["disabled"] == enabled
            cold = executor()
            pending = await cold.get_pending_plan(owner=OWNER, session_id=context.session_id)
            assert pending is not None and pending.plan_id == plan.plan_id
            done = await cold.execute_confirmed(plan.plan_id, context=context)
            assert done.outcome.public_content["status"] == "completed"
            assert done.outcome.public_content["data"]["affected"] == 1
            public = json.dumps([plan.public_dict(), done.outcome.public_content], ensure_ascii=False)
            assert all(value not in public for value in PRIVATE)
            after = current(rule_type, row["id"])
            assert after["disabled"] == (not enabled)
            assert {k: v for k, v in after.items() if k not in {"disabled", "updated_at", "revision"}} == {
                k: v for k, v in before.items() if k not in {"disabled", "updated_at", "revision"}}
            if rule_type == "preprocess_rule":
                assert any(item["id"] == row["id"] for item in preprocess.list_rules(enabled_only=True)) == enabled
            elif rule_type == "release_format":
                assert extract_recognition_context(filename(15), PARENT).episode == (15 if enabled else None)
            else:
                assert (regex.find_tmdb_regex_match(PRIVATE[4], media_type="tv") is not None) == enabled
            with pytest.raises(ToolPipelineError):
                await executor().execute_confirmed(plan.plan_id, context=context)
    asyncio.run(check())


@pytest.mark.parametrize("rule_type", KINDS)
def test_changed_business_row_rejects_confirmation_without_overwrite(backend, rule_type):
    row = seed(rule_type)
    async def check():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        plan = (await effects.prepare("recognition.set_rule_enabled", {
            "rule_type": rule_type, "rule_id": row["id"], "enabled": False}, context=context)).effect_plan
        assert plan is not None
        if rule_type == "release_format":
            formats.change(row["id"], {"revision": row["revision"], "disabled": True})
        else:
            module = preprocess if rule_type == "preprocess_rule" else regex
            module.update_rule(row["id"], {**row, "name": "SECRET changed name"})
        external = current(rule_type, row["id"])
        with pytest.raises(ToolPipelineError) as error:
            await executor().execute_confirmed(plan.plan_id, context=context)
        assert error.value.code == "conflict" and error.value.execution_started is False
        assert current(rule_type, row["id"]) == external
    asyncio.run(check())


@pytest.mark.parametrize(("rule_type", "table"), [
    ("preprocess_rule", "recognition_preprocess_rules"), ("tmdb_regex_rule", "tmdb_regex_rules"),
])
def test_public_cas_waits_for_sqlite_writer_and_compares_inside_transaction(backend, monkeypatch, rule_type, table):
    row = seed(rule_type)
    prepared = service.prepare_rule_enabled(rule_type, row["id"], False)
    started = threading.Event()
    original = db.get_conn
    @contextmanager
    def traced_connection():
        with original() as conn:
            conn.set_trace_callback(lambda sql: started.set() if sql == "BEGIN IMMEDIATE" else None)
            yield conn
    monkeypatch.setattr(preprocess if rule_type == "preprocess_rule" else regex, "get_conn", traced_connection)
    def update():
        return service.set_rule_enabled(rule_type, row["id"], False, expected_snapshot=prepared["snapshot"])
    with sqlite3.connect(db.resolve_db_path()) as writer, ThreadPoolExecutor(max_workers=1) as pool:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(f"UPDATE {table} SET name='External Edit' WHERE id=?", (row["id"],))
        future = pool.submit(update)
        assert started.wait(2)
        assert not future.done()
        writer.commit()
        with pytest.raises(service.RuleConflict):
            future.result(timeout=5)
    after = current(rule_type, row["id"])
    assert after["name"] == "External Edit" and not after["disabled"]


def test_knowledge_control_has_only_existing_update_knowledge_path(backend):
    row = knowledge.create_entry({"knowledge_type": "release_group", "canonical_value": "SecretGroup", "aliases": ["Secret-Group"]})
    assert knowledge.lookup("Secret-Group") is not None
    async def check():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        with pytest.raises(ToolPipelineError):
            await effects.prepare("recognition.set_rule_enabled", {
                "rule_type": "knowledge_entry", "rule_id": row["id"], "enabled": False}, context=context)
        plan = (await effects.prepare("recognition.update_knowledge", {"entry_id": row["id"], "disabled": True}, context=context)).effect_plan
        assert plan is not None and plan.tool_name == "recognition.update_knowledge"
        knowledge.record_hit(row["id"], success=True)
        await executor().execute_confirmed(plan.plan_id, context=context)
        item = knowledge.get_entry(row["id"])
        assert item is not None and item["user_modified"] and item["disabled"] and item["success_count"] == 1
        assert knowledge.lookup("Secret-Group") is None
    asyncio.run(check())


def test_release_format_change_after_precheck_still_obeys_native_revision(backend, monkeypatch):
    row = seed("release_format")
    prepared = service.prepare_rule_enabled("release_format", row["id"], False)
    real_change = formats.change
    def concurrent_change(rule_id, value, **kwargs):
        real_change(rule_id, {"revision": row["revision"], "disabled": True})
        return real_change(rule_id, value, **kwargs)
    monkeypatch.setattr(formats, "change", concurrent_change)
    with pytest.raises(service.RuleConflict):
        service.set_rule_enabled("release_format", row["id"], False, expected_snapshot=prepared["snapshot"])
    after = current("release_format", row["id"])
    assert after["disabled"] and after["revision"] == row["revision"] + 1


def test_format_registry_change_after_evaluation_rejects_before_commit(backend, monkeypatch):
    row = seed("release_format")
    row = formats.change(row["id"], {"revision": row["revision"], "disabled": True})["item"]
    other_value = teaching()
    other_value["draft"]["parent_path"] = "/Other"
    other, _ = save_teaching(other_value)
    prepared = service.prepare_rule_enabled("release_format", row["id"], True)
    real_evaluate = formats._evaluate
    def concurrent_registry(*args, **kwargs):
        result = real_evaluate(*args, **kwargs)
        formats.change(other["id"], {"revision": other["revision"], "disabled": True})
        return result
    monkeypatch.setattr(formats, "_evaluate", concurrent_registry)
    with pytest.raises(service.RuleConflict):
        service.set_rule_enabled("release_format", row["id"], True, expected_snapshot=prepared["snapshot"])
    assert current("release_format", row["id"])["disabled"]
    assert current("release_format", other["id"])["disabled"]


def test_format_reenable_keeps_conflict_evaluation_against_latest_registry(backend):
    row = seed("release_format")
    formats.change(row["id"], {"revision": row["revision"], "disabled": True})
    prepared = service.prepare_rule_enabled("release_format", row["id"], True)
    alternate = teaching()
    alternate["draft"]["template"] = alternate["draft"]["template"].replace("track{episode}r{version}", "track0{version}r{episode}")
    alternate["examples"][0]["episode"] = 2
    alternate["examples"][1]["filename"] = alternate["examples"][1]["filename"].replace("r2]", "r3]")
    alternate["examples"][1]["episode"] = 3
    alternate["filenames"] = []
    other, _ = save_teaching(alternate)
    with pytest.raises(service.RuleConflict):
        service.set_rule_enabled("release_format", row["id"], True, expected_snapshot=prepared["snapshot"])
    assert current("release_format", row["id"])["disabled"]
    assert not current("release_format", other["id"])["disabled"]


def test_unrelated_format_added_before_confirm_is_revalidated_not_blanket_stale(backend):
    row = seed("release_format")
    formats.change(row["id"], {"revision": row["revision"], "disabled": True})
    prepared = service.prepare_rule_enabled("release_format", row["id"], True)
    other = teaching()
    other["draft"]["parent_path"] = "/Other"
    save_teaching(other)
    assert service.set_rule_enabled("release_format", row["id"], True, expected_snapshot=prepared["snapshot"])["ok"]


@pytest.mark.parametrize("change", ["cancel", "cancellation", "owner", "session", "fence"])
def test_cancel_or_foreign_cold_plan_never_updates_state(backend, change):
    row = seed("preprocess_rule")
    async def check():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        plan = (await effects.prepare("recognition.set_rule_enabled", {
            "rule_type": "preprocess_rule", "rule_id": row["id"], "enabled": False}, context=context)).effect_plan
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
        assert not current("preprocess_rule", row["id"])["disabled"]
    asyncio.run(check())


def test_failure_after_sqlite_commit_is_unknown_and_not_replayed(backend, monkeypatch):
    row = seed("preprocess_rule")
    async def check():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        plan = (await effects.prepare("recognition.set_rule_enabled", {
            "rule_type": "preprocess_rule", "rule_id": row["id"], "enabled": False}, context=context)).effect_plan
        assert plan is not None
        calls = []
        def failed_cache():
            calls.append(True)
            raise OSError("模拟提交后缓存回执失败")
        with monkeypatch.context() as changes:
            changes.setattr(preprocess, "_invalidate_cache", failed_cache)
            result = await executor().execute_confirmed(plan.plan_id, context=context)
            assert result.outcome.public_content["status"] == "outcome_unknown"
            assert result.outcome.public_content["data"]["execution_started"] is True
            assert current("preprocess_rule", row["id"])["disabled"]
            with pytest.raises(ToolPipelineError):
                await executor().execute_confirmed(plan.plan_id, context=context)
            assert len(calls) == 1
    asyncio.run(check())


def cold_confirm(paths, plan_id):
    env = {key: value for key, value in os.environ.items() if key not in config.all_items()}
    env.update({**config._SYSTEM_ENV_SNAPSHOT, "PYTHONPATH": os.getcwd(), "APP_ENV": "development",
        "MEDIAFLUX_TEST_MODE": "0", "MEDIAFLUX_TEST_DB_PATH": "",
        **{f"MEDIAFLUX_{name.upper()}_DIR": str(getattr(paths, f"{name}_dir")) for name in ("data", "config", "cache", "log", "strm")}})
    result = subprocess.run([sys.executable, __file__, plan_id], env=env, input=json.dumps(config._SYSTEM_ENV_SNAPSHOT),
                            capture_output=True, text=True, timeout=20, check=True)
    return json.loads(result.stdout)


def test_release_format_confirmation_survives_real_process_restart(backend):
    row = seed("release_format")
    formats.change(row["id"], {"revision": row["revision"], "disabled": True})
    async def check():
        runtime = build_agent_kernel_runtime(model=ScriptedModel([]))
        context = await context_for(runtime.store, owner=OWNER, session="rules-cold")
        plan = (await runtime.session.pipeline.prepare("recognition.set_rule_enabled", {
            "rule_type": "release_format", "rule_id": row["id"], "enabled": True}, context=context)).effect_plan
        assert plan is not None
        result = cold_confirm(backend, plan.plan_id)
        assert result["epoch"] != formats._PREVIEW_EPOCH
        completed = [event for event in result["events"] if event["type"] == "effect.completed"]
        assert len(completed) == 1 and completed[0]["payload"]["result"]["ok"]
        assert not current("release_format", row["id"])["disabled"]
    asyncio.run(check())


def test_sdk_factory_same_state_and_protected_fields_are_bounded(backend):
    row = seed("tmdb_regex_rule")
    methods = {item["name"]: item for item in sdk.describe_methods()}
    assert "rule_id: int" in methods["recognition.set_rule_enabled"]["signature"]
    assert "recognition.rules" in methods
    assert build_tool_catalog().get("recognition.set_rule_enabled").effect is ToolEffect.WRITE
    runtime = build_agent_kernel_runtime(model=ScriptedModel([]))
    assert runtime.session.catalog.has("recognition.set_rule_enabled")
    assert runtime.session.catalog.has("recognition.update_knowledge")
    assert {spec.name for spec in runtime.session.visible_model_tools({})} == {"agent.run_code"}
    async def check():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER)
        base = {"rule_type": "tmdb_regex_rule", "rule_id": row["id"], "enabled": False}
        for field in ("pattern", "tmdb_id", "priority", "disabled"):
            with pytest.raises(ToolPipelineError):
                await effects.prepare("recognition.set_rule_enabled", {**base, field: "forged"}, context=context)
        with pytest.raises(ToolPipelineError):
            await effects.prepare("recognition.set_rule_enabled", {**base, "enabled": True}, context=context)
        assert current("tmdb_regex_rule", row["id"]) == row
    asyncio.run(check())


@pytest.mark.parametrize(("rule_type", "language"), [("preprocess_rule", "python"), ("tmdb_regex_rule", "shell"), ("release_format", "python")])
def test_root_rpc_lists_real_ids_stops_at_card_and_does_not_replay_script(backend, rule_type, language):
    if not sandbox.sandbox_supported():
        pytest.skip(sandbox.workspace_unavailable_reason())
    row = seed(rule_type)
    async def check():
        effects = executor()
        context = await context_for(effects.state_store, owner=OWNER, session="rules-script-" + rule_type)
        pipeline = ToolPipeline(catalog=effects.catalog, state_store=effects.state_store, reference_store=effects.reference_store,
                                effect_store=effects.effect_store, authorization=MediaFluxAuthorizationPolicy())
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        if language == "python":
            code = ('from pathlib import Path\nfrom mf import recognition\n'
                    f'items=recognition.rules({rule_type!r})["items"]\n'
                    f'rule=next(item for item in items if item["id"]=={row["id"]})\n'
                    'Path("before").write_text("once")\n'
                    f'recognition.set_rule_enabled({rule_type!r}, rule["id"], False)\n'
                    'Path("after").write_text("unexpected")')
        else:
            code = ('mf recognition.rules --rule-type tmdb_regex_rule > rules.json\n'
                    'rule_id=$(python -c \'import json;print(json.load(open("rules.json"))["items"][0]["id"])\')\n'
                    'echo once > before\n'
                    'mf recognition.set_rule_enabled --rule-type tmdb_regex_rule --rule-id "$rule_id" --no-enabled\n'
                    'echo unexpected > after')
        try:
            result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": code, "language": language}, context=context)
            plan = result.effect_plan
            assert plan is not None and plan.tool_name == "recognition.set_rule_enabled", result.outcome.public_content
            assert plan.arguments["rule_id"] == row["id"] and plan.arguments["rule_type"] == rule_type
            root, _ = sandbox._workspace_location(context.owner, context.session_id)
            assert (root / "work/before").read_text().strip() == "once" and not (root / "work/after").exists()
            assert not current(rule_type, row["id"])["disabled"]
            await executor().execute_confirmed(plan.plan_id, context=context)
            assert current(rule_type, row["id"])["disabled"] and not (root / "work/after").exists()
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
        events = [event.to_dict() async for event in runtime.session.confirm(owner=OWNER, session_id="rules-cold", plan_id=plan_id)]
        print(json.dumps({"events": events, "epoch": formats._PREVIEW_EPOCH}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(child(sys.argv[1]))
