"""识别知识原生切片：真实 SQLite、业务 CAS、冷确认与 Python/Shell 工作区。"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import database as db, sdk
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.kernel.bootstrap import build_tool_catalog
from app.agent.errors import ToolPipelineError
from app.agent.feature_gate import begin_runtime_change, read_runtime_fence
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.ports.sdk_effects import recognition_knowledge_tools
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.modules import recognition_knowledge as knowledge
from tests.support import initialized_web_runtime
from tests.test_agent_kernel_core import ScriptedModel

OWNER = "webk:v1:" + "c" * 64


@pytest.fixture
def backend(monkeypatch):
    def offline(*args, **kwargs):
        raise AssertionError("识别知识验收禁止外网")
    monkeypatch.setattr(socket.socket, "connect", offline)
    with initialized_web_runtime() as paths:
        knowledge.reset_runtime_state_for_tests()
        knowledge.ensure_seed_knowledge()
        yield paths
        knowledge.reset_runtime_state_for_tests()


def executor():
    store = SQLiteKernelStore()
    return EffectExecutor(catalog=ToolCatalog(recognition_knowledge_tools()), state_store=store,
        reference_store=store, effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore(), record_actions=True),
        authorization=MediaFluxAuthorizationPolicy())


async def context_for(effects, owner=OWNER, session="native-knowledge"):
    lease, _ = await effects.state_store.begin_turn(owner=owner, session_id=session, request_id="knowledge")
    async def progress(_):
        pass
    return ToolCallContext(owner=owner, session_id=session, request_id="knowledge", turn_id=lease.turn_id,
        lease=lease, cancellation=CancellationToken(), report_progress=progress)


def test_sdk_query_complete_fields_and_exhaustible_filtered_pages(backend, tmp_path):
    stamp = db.now()
    with db.get_conn() as conn:
        conn.executemany("""INSERT INTO recognition_knowledge(knowledge_key,knowledge_type,
            canonical_value,normalized_value,aliases_json,source,confidence,evidence_json,created_at,updated_at)
            VALUES(?,'release_group',?,?,?,'user',0.923,?,?,?)""", [
            (f"page:{i}", f"PagingGroup{i:04}", f"paginggroup{i:04}", json.dumps([f"别名{i}"]),
             json.dumps({"long": "事实" * 3000, "sample": i}), stamp, stamp) for i in range(523)])
    first = sdk.recognition.knowledge(keyword="PagingGroup")
    assert len(first["items"]) == 300 and first["total"] == 523
    assert first["summary"]["total"] > first["total"]
    rows = []
    page = sdk.recognition.knowledge(keyword="PagingGroup", limit=9999)
    assert page["limit"] == 500 and len(page["items"]) == 500
    while True:
        rows.extend(page["items"])
        if not page["has_more"]:
            assert page["next_offset"] is None
            break
        page = sdk.recognition.knowledge(keyword="PagingGroup", limit=500, offset=page["next_offset"])
    assert len(rows) == len({row["id"] for row in rows}) == 523
    assert rows[0] == knowledge.get_entry(rows[0]["id"])
    assert len(rows[0]["evidence"]["long"]) == 6000
    assert {"knowledge_key", "confidence", "evidence", "created_at", "hit_count", "user_modified"} <= rows[0].keys()
    assert "entry_number" not in rows[0]
    assert sdk.recognition.knowledge(keyword="PagingGroup", offset=523)["items"] == []
    assert sdk.recognition.knowledge(keyword="missing")["total"] == 0
    with pytest.raises(ValueError):
        sdk.recognition.knowledge(offset=-1)
    async def check_rpc():
        effects = executor()
        context = await context_for(effects)
        (tmp_path / "run").mkdir()
        bridge = sandbox._SDKRequests(context, tmp_path, effects)
        bridge.uid = os.getuid()
        request = tmp_path / "run/mf-requests/query.json"
        request.write_text(json.dumps({"method": "recognition.knowledge", "arguments": {"keyword": "PagingGroup"}}))
        try:
            await bridge.dispatch(request.name)
            reply = json.loads((tmp_path / "run/mf-responses/query.json").read_text())
            assert reply["result"] == first
        finally:
            await bridge.close()
    asyncio.run(check_rpc())


def test_rpc_cold_confirm_crud_once_and_cache(backend, tmp_path):
    async def check():
        effects = executor()
        context = await context_for(effects)
        (tmp_path / "run").mkdir()
        bridge = sandbox._SDKRequests(context, tmp_path, effects)
        bridge.uid = os.getuid()
        args = {"knowledge_type": "release_group", "canonical_value": "AgentTestGroup", "aliases": ["Agent Test Group"]}
        request = tmp_path / "run/mf-requests/create.json"
        request.write_text(json.dumps({"method": "recognition.create_knowledge", "arguments": args}))
        try:
            await bridge.dispatch(request.name)
            proposal = bridge.proposal
            assert proposal is not None
            plan = proposal.effect_plan
            assert plan is not None and plan.tool_name == "recognition.create_knowledge"
            assert sdk.recognition.knowledge(keyword="AgentTestGroup")["items"] == []
        finally:
            await bridge.close()
        cold = executor()
        pending = await cold.get_pending_plan(owner=OWNER, session_id=context.session_id)
        assert pending is not None
        assert pending.plan_id == plan.plan_id
        result = await cold.execute_confirmed(plan.plan_id, context=context)
        row = result.outcome.public_content["data"]["item"]
        cached = knowledge.lookup("Agent Test Group")
        assert cached is not None
        assert row["id"] == cached["id"]
        assert row["source"] == "user"
        with pytest.raises(ToolPipelineError):
            await executor().execute_confirmed(plan.plan_id, context=context)
        assert sdk.recognition.knowledge(keyword="AgentTestGroup")["total"] == 1
        update = (await cold.prepare("recognition.update_knowledge", {"entry_id": row["id"], "disabled": True}, context=context)).effect_plan
        assert update is not None
        assert update.preview["data"]["before"] == knowledge.get_entry(row["id"])
        knowledge.record_hit(row["id"], success=True)
        changed = await executor().execute_confirmed(update.plan_id, context=context)
        assert changed.outcome.public_content["data"]["item"]["disabled"]
        assert knowledge.lookup("Agent Test Group") is None
        remove = (await cold.prepare("recognition.delete_knowledge", {"entry_id": row["id"]}, context=context)).effect_plan
        assert remove is not None
        assert remove.effect.value == "danger"
        await executor().execute_confirmed(remove.plan_id, context=context)
        assert knowledge.get_entry(row["id"]) is None
        assert knowledge.lookup_any("Agent Test Group") is None
    asyncio.run(check())


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_cas_rejects_business_drift_and_preserves_concurrent_write(backend, operation):
    row = knowledge.create_entry({"canonical_value": "SnapshotTest"})
    async def check():
        effects = executor()
        context = await context_for(effects)
        args = {"entry_id": row["id"], **({"disabled": True} if operation == "update" else {})}
        plan = (await effects.prepare(f"recognition.{operation}_knowledge", args, context=context)).effect_plan
        assert plan is not None
        knowledge.update_entry(row["id"], {"aliases": ["another"]})
        with pytest.raises(ToolPipelineError) as error:
            await executor().execute_confirmed(plan.plan_id, context=context)
        assert error.value.code == "confirmation_stale" and error.value.execution_started is False
        current = knowledge.get_entry(row["id"])
        assert current is not None
        assert not current["disabled"] and "another" in current["aliases"]
    asyncio.run(check())


def test_public_cas_waits_for_real_sqlite_writer_and_checks_inside_transaction(backend):
    row = knowledge.create_entry({"canonical_value": "ConcurrentTeam"})
    snapshot = knowledge.entry_snapshot(row)
    started = threading.Event()
    def update():
        started.set()
        return knowledge.update_entry(row["id"], {"disabled": True}, expected_snapshot=snapshot)
    with sqlite3.connect(db.resolve_db_path()) as writer, ThreadPoolExecutor(max_workers=1) as pool:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE recognition_knowledge SET canonical_value='ExternalTeam' WHERE id=?", (row["id"],))
        future = pool.submit(update)
        assert started.wait(2)
        writer.commit()
        with pytest.raises(knowledge.KnowledgeConflict):
            future.result(timeout=5)
    current = knowledge.get_entry(row["id"])
    assert current is not None
    assert current["canonical_value"] == "ExternalTeam" and not current["disabled"]
    def check_locked(conn):
        assert conn.in_transaction
        with sqlite3.connect(db.resolve_db_path(), timeout=0.02) as rival:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                rival.execute("UPDATE recognition_knowledge SET disabled=1 WHERE id=?", (row["id"],))
    assert knowledge.delete_entry(row["id"], expected_snapshot=knowledge.entry_snapshot(current), check_write=check_locked)


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
def test_builtin_protected_fields_and_empty_updates(backend, operation):
    builtin = next(row for row in knowledge.list_entries()["items"] if row["source"] == "builtin")
    async def check():
        effects = executor()
        context = await context_for(effects)
        base = ({"knowledge_type": "release_group", "canonical_value": "ProtectedTeam"} if operation == "create"
                else {"entry_id": builtin["id"], **({"disabled": True} if operation == "update" else {})})
        for field in ("source", "evidence", "knowledge_key", "confidence"):
            with pytest.raises(ToolPipelineError):
                await effects.prepare(f"recognition.{operation}_knowledge", {**base, field: "forged"}, context=context)
        if operation == "delete":
            with pytest.raises(ToolPipelineError, match="不能删除"):
                await effects.prepare("recognition.delete_knowledge", base, context=context)
        if operation == "update":
            with pytest.raises(ToolPipelineError):
                await effects.prepare("recognition.update_knowledge", {"entry_id": builtin["id"]}, context=context)
            plan = (await effects.prepare("recognition.update_knowledge", base, context=context)).effect_plan
            assert plan is not None
            await executor().execute_confirmed(plan.plan_id, context=context)
            current = knowledge.get_entry(builtin["id"])
            assert current is not None
            assert current["disabled"]
            assert all(current[key] == builtin[key] for key in ("source", "evidence", "knowledge_key", "confidence"))
            knowledge.reset_runtime_state_for_tests()
            knowledge.ensure_seed_knowledge()
            reloaded = knowledge.get_entry(builtin["id"])
            assert reloaded is not None
            assert reloaded["disabled"]
    asyncio.run(check())


def test_learning_statistics_and_protected_precision_survive_native_update(backend):
    row = knowledge.record_learned_release_group("LearnedTeam", confidence=0.987,
        evidence={"sample_key": "a", "tmdb_id": "42"})
    row = knowledge.record_learned_release_group("LearnedTeam", confidence=0.98765,
        evidence={"sample_key": "b", "tmdb_id": "43"})
    assert not row["disabled"] and row["success_count"] == 2
    async def check():
        effects = executor()
        context = await context_for(effects)
        plan = (await effects.prepare("recognition.update_knowledge", {"entry_id": row["id"], "disabled": True, "aliases": []}, context=context)).effect_plan
        assert plan is not None
        knowledge.record_hit(row["id"], success=False)
        await executor().execute_confirmed(plan.plan_id, context=context)
        current = knowledge.get_entry(row["id"])
        assert current is not None
        assert current["disabled"] and current["conflict_count"] == 1
        assert all(current[key] == row[key] for key in ("source", "evidence", "knowledge_key", "confidence"))
        again = knowledge.record_learned_release_group("LearnedTeam", confidence=0.99, evidence={"sample_key": "c"})
        assert again["disabled"] and again["user_modified"]
    asyncio.run(check())


@pytest.mark.parametrize("change", ["cancel", "cancellation", "owner", "session", "fence", "collision"])
def test_cold_confirmation_rejects_cancel_foreign_scope_fence_and_collision(backend, change):
    async def check():
        effects = executor()
        context = await context_for(effects)
        plan = (await effects.prepare("recognition.create_knowledge", {
            "knowledge_type": "release_group", "canonical_value": "PendingTeam", "aliases": ["PendingAlias"]}, context=context)).effect_plan
        assert plan is not None
        if change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "cancellation":
            context.cancellation.cancel()
        elif change == "owner":
            context = await context_for(executor(), owner="webk:v1:" + "d" * 64)
        elif change == "session":
            context = await context_for(executor(), session="foreign")
        elif change == "fence":
            with db.get_conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                begin_runtime_change(conn, read_runtime_fence(conn))
        elif change == "collision":
            knowledge.create_entry({"canonical_value": "OtherTeam", "aliases": ["PendingAlias"]})
        with pytest.raises((ToolPipelineError, asyncio.CancelledError)):
            await executor().execute_confirmed(plan.plan_id, context=context)
        assert sdk.recognition.knowledge(keyword="PendingTeam")["items"] == []
    asyncio.run(check())


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
def test_runtime_fence_is_checked_in_actual_crud_transaction(backend, monkeypatch, operation):
    row = knowledge.create_entry({"canonical_value": "FenceTeam"})
    async def check():
        effects = executor()
        context = await context_for(effects)
        args = ({"knowledge_type": "release_group", "canonical_value": "FenceCreate"} if operation == "create"
                else {"entry_id": row["id"], **({"disabled": True} if operation == "update" else {})})
        plan = (await effects.prepare(f"recognition.{operation}_knowledge", args, context=context)).effect_plan
        assert plan is not None
        original = getattr(knowledge, f"{operation}_entry")
        def drift(*args, **kwargs):
            with db.get_conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                begin_runtime_change(conn, read_runtime_fence(conn))
            return original(*args, **kwargs)
        monkeypatch.setattr(knowledge, f"{operation}_entry", drift)
        with pytest.raises(ToolPipelineError) as error:
            await executor().execute_confirmed(plan.plan_id, context=context)
        assert error.value.code == "agent_disabled" and error.value.execution_started is False
        assert knowledge.get_entry(row["id"]) == row
        assert not knowledge.lookup_any("FenceCreate")
    asyncio.run(check())


def test_sdk_help_and_real_factory_remove_old_entrypoints(backend):
    from app.agent.kernel.bootstrap import build_agent_kernel_runtime
    methods = {row["name"]: row for row in sdk.describe_methods()}
    assert "entry_id: int" in methods["recognition.update_knowledge"]["signature"]
    assert "offset: int = 0" in methods["recognition.knowledge"]["signature"]
    old = {"config.recognition_knowledge", *(f"config.{op}_recognition_knowledge" for op in ("create", "update", "delete"))}
    assert not old & {spec.name for spec in build_tool_catalog().visible()}
    runtime = build_agent_kernel_runtime(model=ScriptedModel([]))
    for spec in recognition_knowledge_tools():
        assert runtime.session.catalog.has(spec.name)
    assert runtime.session.catalog.has("recognition.save_release_format")
    assert runtime.session.catalog.has("library.refresh")
    assert not any(runtime.session.catalog.has(name) for name in old)


@pytest.mark.parametrize("language", ["python", "shell"])
@pytest.mark.parametrize("outcome", ["confirm", "cancel", "drift"])
def test_native_python_cli_card_cold_confirmation_and_no_script_replay(backend, language, outcome):
    # 非 root 主机使用 RPC/SQLite 验证；容器中必须运行真实 chroot/IPC，不更换执行器。
    if not sandbox.sandbox_supported():
        pytest.skip(sandbox.workspace_unavailable_reason())
    async def check():
        effects = executor()
        context = await context_for(effects, session="script-" + language + "-" + outcome)
        pipeline = ToolPipeline(catalog=effects.catalog, state_store=effects.state_store, effect_store=effects.effect_store,
                                authorization=MediaFluxAuthorizationPolicy())
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        if language == "python":
            code = ('from pathlib import Path\nfrom mf import recognition\n'
                    'assert recognition.knowledge(keyword="ScriptTeam")["total"] == 0\n'
                    'Path("before").write_text("once")\n'
                    'recognition.create_knowledge("release_group", "ScriptTeam")\n'
                    'Path("after").write_text("unexpected replay")')
        else:
            code = ('mf recognition.knowledge --keyword ScriptTeam\n'
                    'echo once > before\n'
                    'mf recognition.create_knowledge --knowledge-type release_group --canonical-value ScriptTeam\n'
                    'echo unexpected > after')
        try:
            result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": code, "language": language}, context=context)
            plan = result.effect_plan
            assert plan is not None and plan.tool_name == "recognition.create_knowledge", result.outcome.public_content
            root, _ = sandbox._workspace_location(context.owner, context.session_id)
            assert (root / "work/before").read_text().strip() == "once" and not (root / "work/after").exists()
            assert not knowledge.lookup_any("ScriptTeam")
            cold = executor()
            pending = await cold.get_pending_plan(owner=OWNER, session_id=context.session_id)
            assert pending is not None
            assert pending.plan_id == plan.plan_id
            if outcome == "cancel":
                assert await cold.cancel_effect(plan.plan_id, lease=context.lease)
                assert not knowledge.lookup_any("ScriptTeam")
            elif outcome == "drift":
                external = knowledge.create_entry({"canonical_value": "ScriptTeam", "aliases": ["External"], "disabled": True})
                with pytest.raises(ToolPipelineError):
                    await cold.execute_confirmed(plan.plan_id, context=context)
                assert knowledge.get_entry(external["id"]) == external
            else:
                await cold.execute_confirmed(plan.plan_id, context=context)
                assert knowledge.lookup("ScriptTeam")
            assert not (root / "work/after").exists()
            with pytest.raises(ToolPipelineError):
                await executor().execute_confirmed(plan.plan_id, context=context)
            reread = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": 'mf recognition.knowledge --keyword ScriptTeam', "language": "shell"}, context=context)
            assert reread.outcome.public_content["data"]["exit_code"] == 0
            payload = json.loads(reread.outcome.public_content["data"]["stdout"])
            assert payload["total"] == (0 if outcome == "cancel" else 1)
        finally:
            sandbox.clear_workspace(owner=context.owner, session_id=context.session_id)
    asyncio.run(check())
