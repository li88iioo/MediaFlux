"""STRM 失败事实 → 原生 Python/CLI → 原 SQLite 确认 → 公共重试；只替换云盘边界。"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app import database as db, sdk
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.kernel.bootstrap import build_agent_kernel
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog, ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.sdk_effects import strm_failure_tools
from app.agent.kernel.state_types import CancellationToken
from app.modules import strm as service
from tests.support import initialized_web_runtime, isolated_test_database
from tests.test_agent_kernel_core import ScriptedModel
from tests.test_agent_sdk_effects import context_for
from tests.test_strm_retry_progress import _TreeClient, _Response
from app.clients.guangya import GuangYaFile


def record(index: int, action: str = "generate") -> int:
    return db.record_strm_failure(source_id="source", source_name="测试来源", file_id=f"video-{index}",
        parent_id="source", filename=f"Video-{index}.mkv", action=action, rel_dir="/old/relative",
        target_rel_path=f"old/Video-{index}.strm",
        error="无法写入：" + "保留完整细节。" * 36 + " permission denied password=PRIVATE https://private.invalid/token")


@pytest.fixture
def backend(tmp_path, monkeypatch):
    settings = {"GY_STRM_SOURCE_DIRS": '[{"id":"source","name":"测试来源"}]',
                "GY_STRM_BASE_URL": "http://mediaflux.invalid", "STRM_ROOT": str(tmp_path / "strm"),
                "STRM_VIDEO_EXTS": "mkv", "STRM_METADATA_EXTS": "nfo", "STRM_METADATA_ENABLED": "0"}
    client = _TreeClient({"source": [GuangYaFile(f"video-{i}", f"Video-{i}.mkv", False, 1024, f"e{i}", "source")
                                      for i in range(5)]})
    setattr(client, "close", Mock())
    with (isolated_test_database(),
          patch.object(service, "get", side_effect=lambda key, default="": settings.get(key, default)),
          patch.object(service, "GuangYaClient", return_value=client),
          patch("socket.socket.connect", side_effect=AssertionError("不得联网"))):
        yield SimpleNamespace(client=client, settings=settings, root=tmp_path / "strm")


def executor():
    store = SQLiteKernelStore()
    return EffectExecutor(catalog=ToolCatalog(strm_failure_tools()), state_store=store, reference_store=store,
                          effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))


def test_single_native_catalog_and_sdk_contract():
    with initialized_web_runtime():
        kernel = build_agent_kernel(model=ScriptedModel([]))
        assert kernel.catalog.get("strm.failures").effect is ToolEffect.READ
        assert kernel.catalog.get("strm.retry_failures").effect is ToolEffect.DANGER
        assert "strm.triage_failures" not in {spec.name for spec in kernel.catalog.visible()}
        assert {spec.name for spec in kernel.visible_model_tools({})} == {"agent.run_code"}
    for name in ("failure_actions", "retry_actions", "tools"):
        assert importlib.util.find_spec(f"app.agent.domains.strm.{name}") is None
    assert sdk.METHODS["strm.failures"] == sdk.strm.failures
    assert sdk.METHODS["strm.retry_failures"] == sdk.strm.retry_failures
    assert "strm.retry_failures" in sdk.WRITE_METHODS and "strm.failures" not in sdk.WRITE_METHODS
    read, write = strm_failure_tools()
    assert read.effect is ToolEffect.READ and write.effect is ToolEffect.DANGER
    assert set(write.input_schema["properties"]) == {"ids"}
    assert "maxItems" not in write.input_schema["properties"]["ids"]


@pytest.mark.parametrize("arguments", [{}, {"scope": "all"}, {"ids": [True]}, {"ids": [0]},
                                         {"ids": [-1]}, {"ids": ["1"]}, {"ids": "1"}])
def test_retry_rejects_invalid_native_arguments_before_preview(backend, arguments):
    async def check():
        effects = executor()
        context = await context_for(effects.state_store)
        with pytest.raises((ToolPipelineError, ValueError, TypeError)):
            await effects.prepare("strm.retry_failures", arguments, context=context)
        assert not backend.client.list_calls
    asyncio.run(check())


def test_preview_reads_once_has_no_write_and_no_hundred_item_limit(backend):
    ids = [record(i) for i in range(105)]
    async def check():
        effects = executor()
        context = await context_for(effects.state_store)
        with patch.object(service, "capture_strm_retry_runtime_config", wraps=service.capture_strm_retry_runtime_config) as capture:
            prepared = await effects.prepare("strm.retry_failures", {"ids": ids + [ids[0]]}, context=context)
        plan = prepared.effect_plan
        assert plan is not None and plan.arguments == {"ids": ids}
        assert plan.preview["data"]["ids"] == ids and plan.preview["data"]["selected_count"] == 105
        assert plan.preview["data"]["by_action"] == {"generate": 105, "metadata": 0}
        assert capture.call_count == 1 and not backend.client.list_calls
        assert all(row["retry_count"] == 0 for row in db.list_strm_failures(limit=1000))
        public = json.dumps(plan.public_dict(), ensure_ascii=False)
        assert "PRIVATE" not in public and "/old/relative" not in public
        assert not backend.root.exists()
    asyncio.run(check())


def test_empty_selection_no_confirmation_and_unusable_selection_rejected(backend):
    async def check():
        effects = executor()
        context = await context_for(effects.state_store)
        empty = await effects.prepare("strm.retry_failures", {"ids": []}, context=context)
        assert empty.effect_plan is None and empty.outcome.public_content["status"] == "no_changes"
        identifier = record(0)
        for status in ("resolved", "retrying"):
            with db.get_conn() as conn:
                conn.execute("UPDATE strm_failures SET status=? WHERE id=?", (status, identifier))
            with pytest.raises(ToolPipelineError) as caught:
                await effects.prepare("strm.retry_failures", {"ids": [identifier]}, context=context)
            assert caught.value.code == "conflict"
        with pytest.raises(ToolPipelineError):
            await effects.prepare("strm.retry_failures", {"ids": [identifier + 100]}, context=context)
        assert not backend.client.list_calls
    asyncio.run(check())


def test_invalid_config_is_safe_and_does_not_issue_confirmation(backend):
    identifier = record(0)
    async def check():
        effects = executor()
        context = await context_for(effects.state_store)
        with patch.object(service, "capture_strm_retry_runtime_config", return_value=({}, "PRIVATE /root/secret")):
            with pytest.raises(ToolPipelineError) as caught:
                await effects.prepare("strm.retry_failures", {"ids": [identifier]}, context=context)
        assert caught.value.code == "not_configured" and "PRIVATE" not in str(caught.value)
        assert await executor().get_pending_plan(owner=context.owner, session_id=context.session_id) is None
        assert not backend.client.list_calls
    asyncio.run(check())


def test_metadata_selection_reuses_public_download_install_and_ledger(backend):
    identifier = record(0, "metadata")
    backend.client.tree["source"] = [GuangYaFile("video-0", "Show.nfo", False, 8, "fresh", "source")]
    with patch.object(service.requests, "get", return_value=_Response(b"metadata")):
        result = sdk.strm.retry_failures([identifier])
    assert result["status"] == "completed" and result["data"]["resolved"] == 1
    assert (backend.root / service.STRM_SUBDIR / "Show.nfo").read_bytes() == b"metadata"
    assert db.list_strm_failures(status="resolved")[0]["id"] == identifier


@pytest.mark.parametrize("scenario", ["busy", "scan_incomplete", "stop"])
def test_public_retry_keeps_busy_deferred_and_stop_facts(backend, scenario):
    identifier = record(0)
    prepared = service.prepare_strm_failure_retry([identifier])
    if scenario == "busy":
        assert service.STRM_OPERATION_LOCK.acquire(blocking=False)
        try:
            result = service.retry_selected_strm_failures([identifier], expected_fingerprint=prepared["fingerprint"])
        finally:
            service.STRM_OPERATION_LOCK.release()
        assert not result["ok"] and result["status"] == "conflict" and result["data"]["matched"] == 0
        assert not backend.client.list_calls
    else:
        backend.client.tree["source"] = RuntimeError("temporary cloud directory failure password=PRIVATE")
        result = service.retry_selected_strm_failures([identifier], expected_fingerprint=prepared["fingerprint"],
            should_stop=(lambda: bool(backend.client.list_calls)) if scenario == "stop" else None)
        assert result["status"] == "partial" and result["data"]["deferred"] == 1
        assert result["data"]["resolved"] == result["data"]["stale"] == 0
        assert result["data"]["scan_incomplete"]
        assert result["data"]["stopped"] == (scenario == "stop")
        assert "PRIVATE" not in json.dumps(result)
        assert db.list_strm_failures(status="open")[0]["id"] == identifier


def test_cold_sqlite_confirmation_executes_only_selected_ids_once(backend):
    selected, untouched = record(0), record(1)
    async def check():
        first = executor()
        context = await context_for(first.state_store)
        plan = (await first.prepare("strm.retry_failures", {"ids": [selected]}, context=context)).effect_plan
        assert plan is not None and not backend.client.list_calls
        # 新增失败不能扩大已经冻结的集合。
        record(2)
        cold = executor()
        pending = await cold.get_pending_plan(owner=context.owner, session_id=context.session_id)
        assert pending is not None and pending.plan_id == plan.plan_id
        result = await cold.execute_confirmed(plan.plan_id, context=context)
        data = result.outcome.public_content["data"]
        assert result.outcome.public_content["status"] == "completed"
        assert (data["requested"], data["matched"], data["resolved"]) == (1, 1, 1)
        assert db.list_strm_failures(status="resolved")[0]["id"] == selected
        assert untouched in [row["id"] for row in db.list_strm_failures()]
        assert (backend.root / service.STRM_SUBDIR / "Video-0.strm").is_file()
        with pytest.raises(ToolPipelineError):
            await executor().execute_confirmed(plan.plan_id, context=context)
        assert backend.client.list_calls == ["source"]
    asyncio.run(check())


@pytest.mark.parametrize("change", ["cancel", "token", "cancel_during_recheck", "owner", "session", "repeat_same_second", "resolved",
                                    "removed", "row_identity", "error", "runtime", "extension", "notify"])
def test_cold_confirmation_rejects_cancellation_and_drift_without_cloud_write(backend, change):
    identifier = record(0)
    async def check():
        effects = executor()
        context = await context_for(effects.state_store)
        with patch("app.database.now", return_value="2026-08-02 11:30:00"):
            record(0)
            plan = (await effects.prepare("strm.retry_failures", {"ids": [identifier]}, context=context)).effect_plan
            assert plan is not None
            if change == "repeat_same_second":
                record(0)
        if change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "token":
            context.cancellation.cancel()
        elif change in {"owner", "session"}:
            context = await context_for(SQLiteKernelStore(), **{change: "other"})
        elif change == "removed":
            db.delete_strm_failures(ids=[identifier])
        elif change in {"resolved", "row_identity", "error"}:
            field, value = {"resolved": ("status", "resolved"), "row_identity": ("file_id", "different"),
                            "error": ("error", "same version, new error")}[change]
            with db.get_conn() as conn:
                conn.execute(f"UPDATE strm_failures SET {field}=? WHERE id=?", (value, identifier))
        elif change == "runtime":
            backend.settings["GY_STRM_BASE_URL"] = "http://changed.invalid"
        elif change == "extension":
            backend.settings["STRM_VIDEO_EXTS"] = "mp4"
        elif change == "notify":
            backend.settings["STRM_NOTIFY_ENABLED"] = "1"
        capture = service.capture_strm_retry_runtime_config
        def capture_then_cancel():
            result = capture()
            if change == "cancel_during_recheck":
                context.cancellation.cancel()
            return result
        with patch.object(service, "capture_strm_retry_runtime_config", side_effect=capture_then_cancel):
            if change == "cancel_during_recheck":
                cancelled = await executor().execute_confirmed(plan.plan_id, context=context)
                assert cancelled.outcome.public_content["status"] == "cancelled"
                with pytest.raises(ToolPipelineError):
                    await executor().execute_confirmed(plan.plan_id,
                        context=replace(context, cancellation=CancellationToken()))
            else:
                with pytest.raises((ToolPipelineError, asyncio.CancelledError)) as caught:
                    await executor().execute_confirmed(plan.plan_id, context=context)
                if change not in {"cancel", "token", "owner", "session"}:
                    assert isinstance(caught.value, ToolPipelineError)
                    assert caught.value.code == "confirmation_stale"
                    assert caught.value.execution_started is False
        assert not backend.client.list_calls and not backend.root.exists()
        assert all(row["retry_count"] == 0 for row in db.list_strm_failures(status="all"))
    asyncio.run(check())


def test_public_cancellation_before_retry_does_not_claim_rows(backend):
    identifier = record(0)
    prepared = service.prepare_strm_failure_retry([identifier])
    result = service.retry_selected_strm_failures([identifier], expected_fingerprint=prepared["fingerprint"], should_stop=lambda: True)
    assert result["status"] == "cancelled" and not result["ok"]
    assert result["data"]["requested"] == 1 and result["data"]["matched"] == 0
    assert db.list_strm_failures()[0]["retry_count"] == 0
    assert not backend.client.list_calls


@pytest.mark.parametrize("claimed_elsewhere", [1, 2])
def test_racing_claim_preserves_partial_or_zero_actual_result(backend, claimed_elsewhere):
    ids = [record(0), record(1)]
    original = db.claim_strm_failures
    def race(batch, **kwargs):
        original(ids[:claimed_elsewhere])
        return original(batch, **kwargs)
    async def check():
        effects = executor()
        context = await context_for(effects.state_store)
        plan = (await effects.prepare("strm.retry_failures", {"ids": ids}, context=context)).effect_plan
        assert plan is not None
        with patch.object(db, "claim_strm_failures", side_effect=race):
            result = await executor().execute_confirmed(plan.plan_id, context=context)
        public = result.outcome.public_content
        assert public["status"] == ("partial" if claimed_elsewhere == 1 else "conflict")
        assert public["ok"] == (claimed_elsewhere == 1)
        assert public["data"]["requested"] == 2
        assert public["data"]["matched"] == public["data"]["resolved"] == 2 - claimed_elsewhere
    asyncio.run(check())


def test_unknown_after_write_is_consumed_and_never_replayed(backend):
    identifier = record(0)
    original = service.retry_strm_failures
    def uncertain(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError("PRIVATE upstream result lost")
    async def check():
        effects = executor()
        context = await context_for(effects.state_store)
        plan = (await effects.prepare("strm.retry_failures", {"ids": [identifier]}, context=context)).effect_plan
        assert plan is not None
        with patch.object(service, "retry_strm_failures", side_effect=uncertain) as retry:
            with pytest.raises(ToolPipelineError) as caught:
                await executor().execute_confirmed(plan.plan_id, context=context)
            assert caught.value.execution_started is True and "PRIVATE" not in str(caught.value)
            with pytest.raises(ToolPipelineError):
                await executor().execute_confirmed(plan.plan_id, context=context)
        assert retry.call_count == 1 and backend.client.list_calls == ["source"]
        assert db.list_strm_failures(status="resolved")[0]["id"] == identifier
    asyncio.run(check())


def test_selected_retry_processes_ids_beyond_one_thousand_without_losing_tail(backend):
    ids = [record(i) for i in range(1005)]
    backend.client.tree["source"] = []
    prepared = service.prepare_strm_failure_retry(ids)
    assert prepared["data"]["selected_count"] == 1005
    result = service.retry_selected_strm_failures(ids, expected_fingerprint=prepared["fingerprint"])
    assert result["status"] == "partial"  # 原业务：不存在的对象仍记为失败，不伪装成解决。
    assert result["data"]["requested"] == result["data"]["matched"] == result["data"]["stale"] == result["data"]["failed"] == 1005
    assert db.count_strm_failures(status="retrying") == 0
    assert {row["retry_count"] for row in db.list_strm_failures(limit=1000)} == {1}


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="需要现有 root 验收镜像的真实隔离工作区")
@pytest.mark.parametrize("language", ["python", "shell"])
def test_real_native_workspace_reads_full_facts_then_cold_confirms_selected_ids(backend, tmp_path, monkeypatch, language):
    ids = [record(i) for i in range(3)]
    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(
        data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        catalog = ToolCatalog(strm_failure_tools())
        pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store,
            effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))
        catalog.register(sandbox.run_code_tool(pipeline))
        code = ('import json\nfrom mf import strm\n'
                'page = strm.failures(status="open", limit=2)\n'
                'open("/work/failures.json", "w").write(json.dumps(page, ensure_ascii=False))\n'
                'print(json.dumps(page, ensure_ascii=False))\n') if language == "python" else (
                    'mf strm.failures --status open --limit 2 > /work/failures.json\ncat /work/failures.json\n')
        result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"language": language, "code": code}, context=context)
        data = result.outcome.public_content["data"]
        assert result.outcome.public_content["ok"] and data["exit_code"] == 0, data
        page = json.loads(data["stdout"])["data"]
        assert [row["id"] for row in page["items"]] == list(reversed(ids))[:2]
        assert page["has_more"] and page["next_offset"] == 2
        assert page["items"][0]["error"] == db.list_strm_failures()[0]["error"]
        assert "permission denied" in page["items"][0]["error"]
        assert "PRIVATE" not in json.dumps(page)
        root, _ = sandbox._workspace_location(context.owner, context.session_id)
        assert json.loads((root / "work/failures.json").read_text())["data"] == page
        response = root / data["sdk_calls"][0]["response_path"].lstrip("/")
        assert json.loads(response.read_text())["result"]["data"] == page
        assert not backend.client.list_calls
        code = ('import json\nfrom mf import strm\n'
                'facts = json.load(open("/work/failures.json"))\n'
                'strm.retry_failures([facts["data"]["items"][0]["id"]])\n') if language == "python" else (
                    'python -c \'import json; p=json.load(open("/work/failures.json")); '
                    'print(json.dumps([p["data"]["items"][0]["id"]]))\' > /work/ids.json\n'
                    'mf strm.retry_failures --ids @/work/ids.json\n')
        proposed = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"language": language, "code": code}, context=context)
        plan = proposed.effect_plan
        assert plan is not None and plan.arguments == {"ids": [ids[-1]]}
        assert not backend.client.list_calls
        confirmed = await executor().execute_confirmed(plan.plan_id, context=context)
        assert confirmed.outcome.public_content["data"]["resolved"] == 1
        assert [row["id"] for row in db.list_strm_failures(status="resolved")] == [ids[-1]]
        sandbox.clear_workspace(owner=context.owner, session_id=context.session_id)
    asyncio.run(check())
