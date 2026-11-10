"""活动查询与补偿 SDK 的真实原生文件 IPC；后端只用内存模拟，不访问云端。"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import Mock
from typing import Any, cast

import pytest

from app import database as db, sdk
from app.repositories import organize_history as organize_history_repo
from app.clients.guangya import GuangYaClient, GuangYaFile
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.effects import EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.modules import activity_undo, organize_correction, organize_tasks
from app.repositories import agent_compensations as receipts
from tests import test_agent_sdk_preferences as preference_tests

OWNER, context_for, executor = preference_tests.OWNER, preference_tests.context_for, preference_tests.executor
runtime = preference_tests.runtime


@pytest.fixture
def organize_backend(monkeypatch):
    detail = {"title": "撤销样本", "allowed_actions": {"revert": True}, "version": 1,
              "status": "success", "items": [{"id": 7}],
              "operations": [{"action": "move_rename", "status": "success", "operation_token": "original",
                              "from_name": "old.mkv", "to_name": "new.mkv"}]}
    clients, callbacks = [], []

    def service():
        value = Mock()
        value.detail.side_effect = lambda log_id: detail
        value.preflight_revert.side_effect = lambda log_id: {"detail": detail, "steps": detail["operations"]}
        value.client.logged_in = True
        value.revert_latest.return_value = {"success": True}
        clients.append(value)
        return value

    manager = Mock()
    manager.start_operation.side_effect = lambda _operation, _title, callback: (
        callbacks.append(callback) or {"ok": True, "task_id": "native-undo-task"})
    monkeypatch.setattr(organize_correction, "OrganizeCorrectionService", service)
    monkeypatch.setattr(organize_tasks, "get_organize_manager", lambda: manager)
    return detail, clients, callbacks, manager


def test_sdk_uses_real_log_id_host_owner_and_ledger_receipt(runtime, organize_backend):
    detail, clients, callbacks, manager = organize_backend
    with pytest.raises(ValueError):
        sdk.activity.inspect_undo(7)
    with sdk.call_owner(OWNER):
        invalid: Any
        for invalid in (True, 0, "7", {"id": 7}):
            with pytest.raises(ValueError):
                sdk.activity.inspect_undo(cast(Any, invalid))
        result = sdk.activity.inspect_undo(7)
        receipt = result["data"]["receipt_id"]
    assert result["data"]["log_id"] == 7 and isinstance(receipt, str)
    assert result["data"]["changes"] == [{"from": "new.mkv", "to": "old.mkv"}]
    assert result["data"]["affected"] == 1
    assert not {"references", "reference_arguments", "refs"} & result.keys()
    assert not callbacks
    manager.start_operation.assert_not_called()
    with sdk.call_owner("other-owner"), pytest.raises(ValueError):
        sdk.activity.undo(receipt)
    forged = "unknown-receipt"
    with sdk.call_owner(OWNER), pytest.raises(ValueError):
        sdk.activity.undo(forged)
    detail["allowed_actions"]["revert"] = False
    with sdk.call_owner(OWNER), pytest.raises(activity_undo.UndoError) as error:
        sdk.activity.inspect_undo(7)
    assert error.value.code == "undo_not_supported"
    assert all(client.close.call_count == 1 for client in clients)


def test_agent_prepare_rejects_domain_preflight_conflict_without_claiming_receipt(runtime, organize_backend, monkeypatch):
    """离线端口契约：真实账本/确认链复用领域预检，不签发有冲突的卡片。"""
    detail, _clients, _callbacks, manager = organize_backend
    with sdk.call_owner(OWNER):
        receipt = sdk.activity.inspect_undo(7)["data"]["receipt_id"]
    service = Mock()
    service.detail.return_value = detail
    service.client.logged_in = True
    service.preflight_revert.side_effect = RuntimeError("目标目录存在同名文件")
    monkeypatch.setattr(organize_correction, "OrganizeCorrectionService", lambda: service)

    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        with pytest.raises(ToolPipelineError) as error:
            await effects.prepare("activity.undo", {"receipt_id": receipt}, context=context)
        assert error.value.code == "precondition_failed"
        assert await effects.get_pending_plan(owner=OWNER, session_id=context.session_id) is None
    asyncio.run(check())
    assert receipts.state(receipt, db.media_owner_digest(OWNER)) == "available"
    manager.start_operation.assert_not_called()
    service.close.assert_called_once()


@pytest.mark.parametrize("members,later_steps", [(301, 0), (1, 300)])
def test_inspect_undo_uses_complete_reversible_batch_outside_history_window(runtime, monkeypatch, members, later_steps):
    class ReadClient:
        logged_in = True
        def file_info(self, identity):
            if identity in {"source", "target"}:
                return GuangYaFile(identity, identity, True, parent_id="0")
            return GuangYaFile(identity, "new-" + identity + ".mkv", False, parent_id="target", size=10)
        def list_dir(self, _identity):
            return []
    log_id = db.add_organize_log("guangya", "/source/old", "/target/new", "member-0", "success",
        original_parent_id="source", original_name="old-member-0.mkv", source_dir_id="source")
    db.add_organize_log_items(log_id, [{"file_id": f"member-{i}", "role": "video" if i == 0 else "subtitle",
        "original_parent_id": "source", "original_name": f"old-member-{i}.mkv",
        "current_parent_id": "target", "current_name": f"new-member-{i}.mkv", "size": 10} for i in range(members)])
    for i in range(members):
        organize_history_repo.add_organize_operation_step(log_id, "batch", i, "move_rename", file_id=f"member-{i}",
            from_parent_id="source", from_name=f"old-member-{i}.mkv", to_parent_id="target",
            to_name=f"new-member-{i}.mkv", status="success")
    for i in range(later_steps):
        organize_history_repo.add_organize_operation_step(log_id, "history", i, "probe_rename", status="failed")
    service_class = organize_correction.OrganizeCorrectionService
    monkeypatch.setattr(organize_correction, "OrganizeCorrectionService", lambda: service_class(client=cast(GuangYaClient, ReadClient()), scraper=Mock()))
    with sdk.call_owner(OWNER):
        result = sdk.activity.inspect_undo(log_id)["data"]
        assert result["affected"] == members and len(result["changes"]) == min(members, 20)
        prepared = activity_undo.prepare_undo(result["receipt_id"], owner=OWNER)
        assert prepared["preview"]["data"]["changes"]["affected"] == members
        # 展示窗口之外的真实回退目标变化，同样必须撤销原确认。
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_operation_steps SET from_name='changed.mkv' WHERE log_id=? AND action='move_rename' AND step_index=0", (log_id,))
        with pytest.raises(activity_undo.UndoError) as error:
            activity_undo.prepare_undo(result["receipt_id"], owner=OWNER)
        assert error.value.code == "confirmation_stale"


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="需要真实 Linux root 工作区")
@pytest.mark.parametrize("cancel", [False, True])
def test_root_sdk_file_ipc_query_receipt_cold_confirm_cancel_and_owner(
    runtime, organize_backend, monkeypatch, tmp_path, cancel,
):
    from types import SimpleNamespace

    assert sandbox.sandbox_supported(), sandbox.workspace_unavailable_reason()
    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))
    detail, clients, callbacks, manager = organize_backend
    with db.get_conn() as conn:
        log_id = conn.execute(
            "INSERT INTO organize_log(source,original_path,new_path,title,status,created_at,updated_at) "
            "VALUES('guangya','/fixture/old','/fixture/new','撤销 IPC 样本','success',?,?)", (db.now(), db.now())
        ).lastrowid

    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        pipeline = executor(store, cls=ToolPipeline)
        assert isinstance(pipeline, ToolPipeline)
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": '''
from pathlib import Path
import json, subprocess
from mf import activity
page = activity.search("撤销 IPC 样本", limit=1)
row = page["items"][0]
assert row["kind"] == "organize"
timeline = activity.timeline(row["kind"], row["id"])
assert timeline["ok"]
inspected = activity.inspect_undo(row["id"])
receipt = inspected["data"]["receipt_id"]
assert inspected["data"]["log_id"] == row["id"]
Path("receipt.json").write_text(json.dumps(receipt))
with Path("runs.txt").open("a") as stream:
    stream.write("once\\n")
subprocess.run(["mf", "activity.undo", "--receipt-id", json.loads(Path("receipt.json").read_text())], check=True)
Path("after.txt").write_text("must not run")
'''}, context=context)
        plan = result.effect_plan
        assert plan is not None and plan.tool_name == "activity.undo", json.dumps(result.outcome.public_content, ensure_ascii=False)
        root, _ = sandbox._workspace_location(OWNER, context.session_id)
        saved = json.loads((root / "work/receipt.json").read_text())
        assert plan.arguments == {"receipt_id": saved}
        assert (root / "work/runs.txt").read_text() == "once\n"
        assert not (root / "work/after.txt").exists()
        manager.start_operation.assert_not_called()
        rebuilt = executor(SQLiteKernelStore(), cls=EffectExecutor)
        foreign = await context_for(store, "other-owner")
        with pytest.raises(ToolPipelineError):
            await rebuilt.execute_confirmed(plan.plan_id, context=foreign)
        if cancel:
            assert await pipeline.cancel_effect(plan.plan_id, lease=context.lease)
            with pytest.raises(ToolPipelineError):
                await rebuilt.execute_confirmed(plan.plan_id, context=context)
            manager.start_operation.assert_not_called()
            assert receipts.state(saved, db.media_owner_digest(OWNER)) == "available"
        else:
            pending = await rebuilt.get_pending_plan(owner=OWNER, session_id=context.session_id)
            assert pending is not None and pending.plan_id == plan.plan_id
            outcome = (await rebuilt.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
            assert outcome["ok"] and outcome["status"] == "accepted"
            assert outcome["data"]["log_id"] == log_id
            assert "reference_arguments" not in outcome
            assert len(callbacks) == 1
            callbacks[0]()
            executed = [client for client in clients if client.revert_latest.called]
            assert len(executed) == 1
            executed[0].revert_latest.assert_called_once_with(log_id, "undo-" + saved[:48], 1)
            with pytest.raises(ToolPipelineError):
                await rebuilt.prepare("activity.undo", {"receipt_id": saved}, context=context)
        assert (root / "work/runs.txt").read_text() == "once\n"
        assert not (root / "work/after.txt").exists()

    asyncio.run(check())


def test_preference_receipt_survives_public_projection_and_preserves_snapshot(runtime):
    async def check():
        with sdk.call_owner(OWNER):
            before = {"memory_notes": ["库路径是 /media/archive；记录 token=local-note"], "preferred_download_target": "guangya"}
            assert sdk.preferences.update(**before)["ok"]
            before = sdk.preferences.get()
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        plan = (await effects.prepare("preferences.update", {"memory_notes": ["新备忘"]}, context=context)).effect_plan
        assert plan is not None
        written = (await effects.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
        receipt = written["data"]["receipt_id"]
        assert isinstance(receipt, str)
        action = written["data"]["undo"]
        assert action == {"method": "activity.undo", "arguments": {"receipt_id": receipt}}
        assert "/media/archive" not in json.dumps(written) and "local-note" not in json.dumps(written)
        with sdk.call_owner(OWNER):
            saved = sdk.preferences.get()
        db.init_db()  # 关闭重开后的账本读取保留原私有快照。
        undo = (await executor(SQLiteKernelStore()).prepare(action["method"], action["arguments"], context=context)).effect_plan
        assert undo is not None and undo.tool_name == "activity.undo"
        assert receipts.state(receipt, db.media_owner_digest(OWNER)) == "available"
        with sdk.call_owner(OWNER):
            assert sdk.preferences.get() == saved
        assert (await executor(SQLiteKernelStore()).execute_confirmed(undo.plan_id, context=context)).outcome.public_content["ok"]
        with sdk.call_owner(OWNER):
            assert sdk.preferences.get() == before
    asyncio.run(check())


def test_reinspect_never_renews_original_receipt_and_expiry_is_checked_at_claim(runtime, organize_backend, monkeypatch):
    instant = activity_undo.time.time()
    monkeypatch.setattr(activity_undo.time, "time", lambda: instant)
    with sdk.call_owner(OWNER):
        receipt = sdk.activity.inspect_undo(7)["data"]["receipt_id"]
    digest = db.media_owner_digest(OWNER)
    stored = receipts.get(receipt, digest)
    assert stored is not None
    original_expiry = stored["expires_at"]
    assert original_expiry == instant + 86400
    monkeypatch.setattr(activity_undo.time, "time", lambda: instant + 60)
    with sdk.call_owner(OWNER):
        assert sdk.activity.inspect_undo(7)["data"]["receipt_id"] == receipt
    stored = receipts.get(receipt, digest)
    assert stored is not None and stored["expires_at"] == original_expiry
    monkeypatch.setattr(activity_undo.time, "time", lambda: original_expiry)
    assert not receipts.claim(receipt, digest)
    with sdk.call_owner(OWNER), pytest.raises(activity_undo.UndoError) as error:
        sdk.activity.undo(receipt)
    assert error.value.code == "confirmation_stale"
    with sdk.call_owner(OWNER):
        renewed = sdk.activity.inspect_undo(7)["data"]["receipt_id"]
    assert renewed != receipt
    stored = receipts.get(receipt, digest)
    assert stored is not None and stored["expires_at"] == original_expiry
    stored = receipts.get(renewed, digest)
    assert stored is not None and stored["expires_at"] == original_expiry + 86400
    assert receipts.state(receipt, digest) == "available"
    organize_backend[3].start_operation.assert_not_called()


def test_unknown_admission_error_remains_closed_and_is_not_reported_unstarted(runtime, organize_backend):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        with sdk.call_owner(OWNER):
            receipt = sdk.activity.inspect_undo(7)["data"]["receipt_id"]
        plan = (await effects.prepare("activity.undo", {"receipt_id": receipt}, context=context)).effect_plan
        organize_backend[3].start_operation.side_effect = ValueError("admission response lost")
        assert plan is not None
        with pytest.raises(ToolPipelineError) as error:
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert error.value.code == "outcome_unknown" and error.value.execution_started is True
        assert isinstance(error.value.__cause__, ValueError)
        assert receipts.state(receipt, db.media_owner_digest(OWNER)) == "outcome_unknown"
        with pytest.raises(ToolPipelineError):
            await executor(SQLiteKernelStore()).prepare("activity.undo", {"receipt_id": receipt}, context=context)
        with sdk.call_owner(OWNER), pytest.raises(activity_undo.UndoError):
            sdk.activity.inspect_undo(7)
        organize_backend[3].start_operation.assert_called_once()
    asyncio.run(check())
