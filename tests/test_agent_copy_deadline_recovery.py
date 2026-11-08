"""已受理 copy 的原期限与恢复后取消契约；仅使用隔离 SQLite 和假 Provider。"""

from __future__ import annotations
from app.clients.guangya import GuangYaClient
from typing import cast

import asyncio
import json
import socket
import threading
from app.modules.process_lock import CrossProcessLock
import time
from functools import partial
from types import SimpleNamespace
from unittest import mock

import pytest

from app.agent.effect_completion import wait_for_effect_completion
from app.agent.models import ToolContext, ToolResult
from app.modules import guangya_fs_change, organize_tasks
from app.modules.organize_tasks import OrganizeTaskManager
from app.repositories import organize_operation_jobs as jobs
from tests.support import isolated_test_database
from tests.test_agent_guangya_fs_gateway import FakeGatewayClient

_OWNER = "copy-deadline-recovery-owner"


@pytest.fixture
def copy_queue(monkeypatch, tmp_path):
    def deny_network(*_args, **_kwargs):
        raise AssertionError("copy 期限/恢复测试禁止外联")

    for method in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, method, deny_network)
    monkeypatch.setattr(guangya_fs_change, "_directory", lambda: tmp_path / "changes")
    monkeypatch.setattr(guangya_fs_change, "get_web_secret", lambda: "copy-recovery-test-secret")
    monkeypatch.setattr(guangya_fs_change, "_COPY_FOREGROUND_ATTEMPTS", 1)

    client = FakeGatewayClient()
    client.copy = mock.Mock(wraps=client.copy)
    client.move = mock.Mock(wraps=client.move)
    client.rename = mock.Mock(wraps=client.rename)
    client.task_status = mock.Mock(return_value={"data": {"status": 1}})
    clock = [time.time()]
    # 本链路共享墙钟，不修改全局 time 或绕过原 expires_at/lease 检查。
    wall_clock = SimpleNamespace(time=lambda: clock[0])
    for module in (jobs, guangya_fs_change):
        monkeypatch.setattr(module, "time", wall_clock)
    # manager 新增的 time.time() 期限 guard 也必须与队列共用受控墙钟。
    monkeypatch.setattr(organize_tasks, "time", wall_clock, raising=False)
    monkeypatch.setattr(
        guangya_fs_change,
        "execute_fs_change_plan",
        partial(guangya_fs_change.execute_fs_change_plan, client_factory=lambda: cast(GuangYaClient, client)),
    )
    monkeypatch.setattr(OrganizeTaskManager, "_wake_download_tracker", lambda _self: None)
    with isolated_test_database():
        yield client, clock


def _enqueue_plan(client, operation="copy"):
    operations = (
        [
            {"op": "copy", "file_id": "trash", "target_path": "/target"},
            {"op": "move", "file_id": "source", "target_path": "/target"},
        ]
        if operation == "copy"
        else [{"op": "rename", "file_id": "rename", "new_name": "renamed.mp4"}]
    )
    plan = guangya_fs_change.build_fs_change_plan(
        client, owner=_OWNER,
        operations=operations, trigger_strm=False,
    )
    manager = OrganizeTaskManager()
    manager._worker = cast(threading.Thread, object())
    with mock.patch.object(organize_tasks, "get_organize_manager", return_value=manager), \
            mock.patch.object(manager, "_ensure_operation_dispatcher"):
        task = guangya_fs_change.submit_fs_change_plan(
            plan["plan_id"], owner=_OWNER, expected_fingerprint=plan["fingerprint"],
        )
    row = jobs.get_organize_operation_job(task["task_id"])
    assert row is not None
    assert not task["replayed"]
    assert jobs.verify_organize_operation_payload(row)
    bound = guangya_fs_change._read(plan["plan_id"])
    assert bound["job_id"] == row["job_id"]
    assert bound["queue_until_epoch"] == row["expires_at"]
    return plan, row


def _run_claimed(row):
    # 沿用现有队列测试的同步 worker 夹具，不启动 dispatcher 或后台线程。
    manager = OrganizeTaskManager()
    worker_lock = threading.Lock()
    manager._lock = cast(CrossProcessLock, worker_lock)
    manager._lock.acquire()
    manager._worker = cast(threading.Thread, object())
    manager._task = {
        "id": row["job_id"], "status": "running", "durable": True,
        "owner_digest": row["owner_digest"], "stats": {},
    }
    # 不替换 _execute_durable_operation：真实签名验证、取消 guard 和 dispatch 都执行。
    manager._run_durable_operation(dict(row))
    assert manager._worker is None
    assert not worker_lock.locked()
    return manager


def _accepted_copy_pending(client):
    plan, queued = _enqueue_plan(client)
    claimed = jobs.claim_organize_operation_job(queued["job_id"])
    assert claimed is not None
    _run_claimed(claimed)
    pending = jobs.get_organize_operation_job(queued["job_id"])
    assert pending is not None
    assert pending["status"] == "pending"
    assert pending["error_code"] == jobs.CLOUD_COPY_PENDING_CODE
    assert pending["expires_at"] == queued["expires_at"]
    assert pending["payload_json"] == queued["payload_json"]
    client.copy.assert_called_once_with(["trash"], "target")
    client.move.assert_not_called()

    checkpoint = guangya_fs_change._read_fs_checkpoints(
        guangya_fs_change._read(plan["plan_id"]),
    )
    assert checkpoint["copy_tasks"]["1"]
    assert checkpoint["operation_items"][1]["status"] == "not_started"
    return plan, pending, checkpoint


def _terminal_receipt(manager, job_id):
    accepted = ToolResult(True, "accepted", "文件变更已提交", data={
        "operation": "filesystem_change",
        "operation_ref": jobs.organize_operation_public_ref(job_id),
    })
    with (
        mock.patch("app.modules.organize_tasks.get_organize_manager", return_value=manager),
        mock.patch.object(manager, "status", return_value={}),
    ):
        return asyncio.run(wait_for_effect_completion(
            accepted, tool="cloud.change",
            context=ToolContext(owner=_OWNER), timeout_seconds=0,
        ))


def test_copy_readback_crossing_original_deadline_blocks_remaining_move(copy_queue):
    client, clock = copy_queue
    _plan, pending, checkpoint = _accepted_copy_pending(client)
    assert pending is not None
    deadline = pending["expires_at"]
    clock[0] = deadline - 1
    claimed = jobs.claim_organize_operation_job(pending["job_id"])
    assert claimed is not None
    assert claimed["expires_at"] == deadline

    def finish_copy_after_deadline(task_id):
        assert task_id == checkpoint["copy_tasks"]["1"]
        assert clock[0] < deadline
        # 真实队列仍是 running；在已有 Provider 句柄回读期间跨过原期限。
        clock[0] = deadline + 1
        return {"data": {"status": 2, "detail": {"code": 0}}}

    client.task_status.side_effect = finish_copy_after_deadline
    manager = _run_claimed(claimed)
    finished = jobs.get_organize_operation_job(pending["job_id"])
    receipt = _terminal_receipt(manager, pending["job_id"])
    client.copy.assert_called_once_with(["trash"], "target")
    assert client.task_status.call_count == 2
    assert finished is not None
    assert finished["expires_at"] == deadline
    assert client.move.call_count == 0, (
        "copy 回读跨过原 expires_at 后仍执行了剩余 move："
        f"queue={finished['status']}, receipt={receipt.status}, "
        f"calls={client.move.call_args_list}"
    )
    assert client.file_info("source").parent_id == "0"
    result = json.loads(finished["result_json"])
    assert result["operation_items"][1]["status"] == "not_started"
    # 期限只能收束本地续行，不能把已受理的云端 copy 宣称为已撤销或全计划完成。
    assert finished["status"] in {"manual_review", "partial"}
    assert receipt.status in {"manual_review", "partial"}
    assert not receipt.ok
    assert "已取消" not in receipt.summary
    assert "云端已取消" not in finished["error"] + receipt.error


def test_accepted_copy_stays_uncancellable_after_running_orphan_recovery(copy_queue):
    client, clock = copy_queue
    plan, pending, checkpoint = _accepted_copy_pending(client)
    clock[0] += jobs.CLOUD_COPY_POLL_SECONDS + 1
    assert pending is not None
    running = jobs.claim_organize_operation_job(pending["job_id"])
    assert running is not None
    assert running["status"] == "running"
    assert running["lease_generation"] > pending["lease_generation"]
    _, before = jobs.request_cancel_organize_operation_job(
        running["job_id"], owner=_OWNER,
        expected_lease_generation=running["lease_generation"],
    )
    assert before == "uncancellable"
    signed_plan = guangya_fs_change._plan_path(plan["plan_id"]).read_bytes()
    journal = guangya_fs_change._journal_path(plan["plan_id"]).read_bytes()

    # worker 已失去执行者；真实恢复读取并认证上述计划/checkpoint，不替换恢复检查器。
    assert jobs.recover_orphaned_organize_operation_jobs() == 1
    recovered = jobs.get_organize_operation_job(running["job_id"])
    assert recovered is not None
    assert recovered["status"] == "pending"
    assert recovered["lease_generation"] > running["lease_generation"]
    assert recovered["expires_at"] == pending["expires_at"]
    cancelled, outcome = jobs.request_cancel_organize_operation_job(
        recovered["job_id"], owner=_OWNER,
        expected_lease_generation=recovered["lease_generation"],
    )
    assert outcome == "uncancellable", (
        "已受理 copy 的恢复改变了取消契约："
        f"before={before}, after={outcome}, recovery_code={recovered['error_code']}"
    )
    assert cancelled is not None
    assert cancelled["status"] == "pending"
    assert cancelled["cancel_requested"] == 0
    assert cancelled["payload_json"] == pending["payload_json"]
    assert cancelled["payload_auth"] == pending["payload_auth"]
    assert jobs.verify_organize_operation_payload(cancelled)
    assert guangya_fs_change._plan_path(plan["plan_id"]).read_bytes() == signed_plan
    assert guangya_fs_change._journal_path(plan["plan_id"]).read_bytes() == journal
    assert guangya_fs_change._read_fs_checkpoints(
        guangya_fs_change._read(plan["plan_id"]),
    ) == checkpoint
    client.copy.assert_called_once_with(["trash"], "target")
    client.move.assert_not_called()

    manager = OrganizeTaskManager()
    manager._worker = cast(threading.Thread, object())
    with mock.patch.object(organize_tasks, "get_organize_manager", return_value=manager), \
            mock.patch.object(manager, "_ensure_operation_dispatcher", side_effect=AssertionError("只复用原复制任务")):
        repeated = guangya_fs_change.submit_fs_change_plan(
            plan["plan_id"], owner=_OWNER, expected_fingerprint=plan["fingerprint"],
        )
    assert repeated["task_id"] == recovered["job_id"]
    assert repeated["expires_at"] == recovered["expires_at"]
    assert repeated["replayed"] and repeated["status"] == "pending"
    assert guangya_fs_change._read_fs_checkpoints(guangya_fs_change._read(plan["plan_id"])) == checkpoint
    client.copy.assert_called_once_with(["trash"], "target")


def test_accepted_copy_handle_is_uncancellable_before_first_defer(copy_queue):
    client, _clock = copy_queue
    plan, queued = _enqueue_plan(client)
    claimed = jobs.claim_organize_operation_job(queued["job_id"])
    assert claimed is not None
    attempts = []

    def persisted_snapshot():
        signed = guangya_fs_change._read(plan["plan_id"])
        get_organize_operation_job_result = jobs.get_organize_operation_job(queued["job_id"])
        assert get_organize_operation_job_result is not None
        return {
            "row": dict(get_organize_operation_job_result),
            "plan": guangya_fs_change._plan_path(plan["plan_id"]).read_bytes(),
            "journal": guangya_fs_change._journal_path(plan["plan_id"]).read_bytes(),
            "checkpoint": guangya_fs_change._read_fs_checkpoints(signed),
        }

    def cancel_during_first_copy_readback(task_id):
        # copy 已返回稳定 handle 并写入真实签名 journal，首轮 worker 尚未 defer。
        before = persisted_snapshot()
        _row, outcome = jobs.request_cancel_organize_operation_job(
            queued["job_id"], owner=_OWNER,
            expected_lease_generation=claimed["lease_generation"],
        )
        attempts.append((task_id, outcome, before, persisted_snapshot()))
        return {"data": {"status": 1}}

    client.task_status.side_effect = cancel_during_first_copy_readback
    _run_claimed(claimed)
    finished = jobs.get_organize_operation_job(queued["job_id"])
    assert len(attempts) == 1
    task_id, outcome, before, after = attempts[0]
    assert before["row"]["status"] == "running"
    assert before["row"]["error_code"] != jobs.CLOUD_COPY_PENDING_CODE
    assert before["row"]["cancel_requested"] == 0
    assert before["checkpoint"]["copy_tasks"]["1"] == task_id
    assert before["checkpoint"]["operation_items"][1]["status"] == "not_started"
    assert jobs.verify_organize_operation_payload(before["row"])
    client.copy.assert_called_once_with(["trash"], "target")
    assert finished is not None
    assert outcome == "uncancellable", (
        "已有签名 copy handle、首轮 defer 前仍接受了取消："
        f"outcome={outcome}, queue_after_worker={finished['status']}"
    )
    assert after["row"]["status"] == "running"
    assert after["row"]["cancel_requested"] == 0
    for field in ("payload_json", "payload_auth", "expires_at"):
        assert after["row"][field] == before["row"][field]
    for field in ("plan", "journal", "checkpoint"):
        assert after[field] == before[field]
    assert finished is not None
    assert finished["status"] == "pending"
    assert finished["error_code"] == jobs.CLOUD_COPY_PENDING_CODE
    assert finished["cancel_requested"] == 0
    assert finished["expires_at"] == queued["expires_at"]
    assert finished["payload_json"] == queued["payload_json"]
    assert jobs.verify_organize_operation_payload(finished)
    durable_checkpoint = guangya_fs_change._read_fs_checkpoints(
        guangya_fs_change._read(plan["plan_id"]),
    )
    assert durable_checkpoint["copy_tasks"]["1"] == task_id
    assert durable_checkpoint["operation_items"][1]["status"] == "not_started"
    client.move.assert_not_called()


@pytest.mark.parametrize("operation", ["copy", "rename"])
@pytest.mark.parametrize("claim_first", [False, True], ids=["pending", "claimed-not-started"])
def test_unstarted_copy_and_plain_rename_remain_cancellable(copy_queue, operation, claim_first):
    client, _clock = copy_queue
    plan, queued = _enqueue_plan(client, operation)
    row = jobs.claim_organize_operation_job(queued["job_id"]) if claim_first else queued
    assert row is not None
    cancelled, outcome = jobs.request_cancel_organize_operation_job(
        row["job_id"], owner=_OWNER, expected_lease_generation=row["lease_generation"],
    )
    assert outcome == ("requested" if claim_first else "cancelled")
    assert cancelled is not None
    assert cancelled["cancel_requested"] == 1
    if claim_first:
        _run_claimed(cancelled)
    terminal = jobs.get_organize_operation_job(row["job_id"])
    assert terminal is not None
    assert terminal["status"] == "cancelled"
    assert terminal["payload_json"] == "{}"
    assert terminal["payload_auth"] == ""
    assert guangya_fs_change._read(plan["plan_id"])["status"] == "cancelled"
    client.copy.assert_not_called()
    client.task_status.assert_not_called()
    client.move.assert_not_called()
    client.rename.assert_not_called()
    assert client.file_info("source").parent_id == "0"
    assert client.file_info("rename").name == "广告-ABC.mp4"


def test_copy_expiry_after_crash_before_first_defer_does_not_claim_remote_cancel(copy_queue):
    client, clock = copy_queue
    plan, queued = _enqueue_plan(client)
    claimed = jobs.claim_organize_operation_job(queued['job_id'])
    assert claimed is not None
    client.task_status.side_effect = SystemExit(73)
    # 保留真实签名 copy handle，进程在首轮 defer/队列 marker 前消失。
    with pytest.raises(SystemExit):
        OrganizeTaskManager._execute_durable_operation(dict(claimed))
    crashed = jobs.get_organize_operation_job(queued['job_id'])
    assert crashed is not None
    assert crashed['status'] == 'running'
    assert crashed['error_code'] != jobs.CLOUD_COPY_PENDING_CODE
    journal = guangya_fs_change._read_fs_checkpoints(guangya_fs_change._read(plan['plan_id']))
    assert journal['copy_tasks']['1']
    assert jobs.recover_orphaned_organize_operation_jobs() == 1
    restored = jobs.get_organize_operation_job(queued['job_id'])
    assert restored is not None
    assert restored['status'] == 'pending'
    clock[0] = restored['expires_at'] + 1
    assert jobs.claim_organize_operation_job(queued['job_id']) is None
    expired = jobs.get_organize_operation_job(queued['job_id'])
    assert expired is not None
    assert expired['status'] == 'manual_review', dict(expired)
    assert expired['error_code'] == 'ProviderCopyOutcomeUnknown'
    assert '视为已取消' in expired['error']
    assert client.copy.call_count == 1
    client.move.assert_not_called()
