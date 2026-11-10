"""qB 公共控制与原生确认共用真实业务入口；客户端全部离线替身。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import config
from app.repositories import download_requests as download_requests_repo
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.modules import qb_control
from tests.support import initialized_web_runtime


class _QBClient:
    def __init__(self):
        self.tasks = [SimpleNamespace(hash=key * 40, name=f"任务{key}", size=123, added_on=1,
                                     state="downloading", progress=0.5) for key in ("a", "b")]
        self.writes = []
        self.mode = ""
    def close(self):
        pass
    def list_torrents(self):
        if self.mode == "read_failed" and self.writes:
            raise RuntimeError("private-read-token")
        return deepcopy(self.tasks)
    def _write(self, operation, hashes):
        self.writes.append((operation, hashes))
        if self.mode == "unknown":
            raise RuntimeError("private-write-token")
        selected = set(hashes.split("|"))
        if operation == "delete":
            self.tasks = [task for task in self.tasks if task.hash not in selected]
        else:
            for task in self.tasks:
                if task.hash in selected:
                    task.state = "stoppedDL" if operation == "pause" else "downloading"
        return True
    def pause_torrents(self, hashes):
        return self._write("pause", hashes)
    def resume_torrents(self, hashes):
        return self._write("resume", hashes)
    def delete_torrents(self, hashes, delete_files=False):
        assert delete_files is False
        return self._write("delete", hashes)


@pytest.fixture
def qb_backend(tmp_path, monkeypatch):
    def deny(*_args, **_kwargs):
        raise AssertionError("qB 控制测试禁止外联")
    monkeypatch.setattr(socket.socket, "connect", deny)
    with initialized_web_runtime():
        config.set_and_save({"QB_URL": "http://qb.invalid", "QB_API_KEY": "test-key"})
        client = _QBClient()
        monkeypatch.setattr(qb_control, "QBittorrentClient", lambda **_kwargs: client)
        yield SimpleNamespace(client=client, root=tmp_path)


@pytest.mark.parametrize("operation,hashes", [("all", ["a" * 40]), ("pause", ["all"]), ("delete", []),
                                              ("resume", ["a" * 40] * 21)])
def test_shared_qb_control_rejects_invalid_target_before_opening_client(monkeypatch, operation, hashes):
    opened = []
    monkeypatch.setattr(qb_control, "configured_qb_client", lambda values: opened.append(values))
    with pytest.raises(ValueError):
        qb_control.prepare_qb_control(operation, hashes)
    assert opened == []


def test_shared_freeze_allows_progress_but_rejects_readded_identity(qb_backend):
    client = qb_backend.client
    snapshot = qb_control.prepare_qb_control("pause", ["a" * 40])
    client.tasks[0].progress = 0.9
    assert qb_control.execute_qb_control(snapshot)["verification"] == "verified"
    snapshot = qb_control.prepare_qb_control("resume", ["a" * 40])
    client.tasks[0].added_on += 1
    with pytest.raises(qb_control.QBControlStale):
        qb_control.execute_qb_control(snapshot)
    assert len(client.writes) == 1


def test_shared_cancel_before_delete_keeps_tracking_and_remote_task(qb_backend):
    request_id, _ = download_requests_repo.create_download_request("cancel-qb-control", "magnet")
    download_requests_repo.update_download_request(request_id, qb_status="downloading", qb_task_id="a" * 40)
    snapshot = qb_control.prepare_qb_control("delete", ["a" * 40])
    def cancelled():
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        qb_control.execute_qb_control(snapshot, cancel_check=cancelled)
    saved = download_requests_repo.get_download_request(request_id)
    assert saved is not None and saved["qb_status"] == "downloading"
    assert qb_backend.client.writes == []


def test_shared_resume_does_not_claim_error_state_was_verified(qb_backend, monkeypatch):
    client = qb_backend.client
    client.tasks[0].state = "error"
    monkeypatch.setattr(client, "resume_torrents", lambda hashes: True)
    result = qb_control.execute_qb_control(qb_control.prepare_qb_control("resume", ["a" * 40]))
    assert result["accepted"] is True and result["status"] == "accepted" and result["verification"] == "pending"


def _executor(store):
    from app.agent.kernel.ports.sdk_effects import qb_control_tools
    return EffectExecutor(catalog=ToolCatalog(qb_control_tools()), state_store=store, reference_store=store,
        effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()),
        effect_lifecycle=MediaFluxEffectLifecycle(), authorization=MediaFluxAuthorizationPolicy())


async def _context(store):
    owner = "webk:v1:" + "e" * 64
    lease, _ = await store.begin_turn(owner=owner, session_id="native-qb", request_id="native-qb")
    return ToolCallContext(owner=owner, session_id=lease.session_id, request_id=lease.request_id,
        turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=AsyncMock(), channel="web")


@pytest.mark.parametrize("method,operation", [("pause", "pause"), ("resume", "resume"), ("remove", "delete")])
def test_native_qb_rpc_cold_store_confirmation_and_duplicate_receipt(qb_backend, method, operation):
    async def run():
        store = SQLiteKernelStore()
        context = await _context(store)
        effects = _executor(store)
        (qb_backend.root / "run").mkdir()
        bridge = sandbox._SDKRequests(context, qb_backend.root, effects)
        bridge.uid = os.getuid()
        request = qb_backend.root / "run/mf-requests/qb.json"
        request.write_text(json.dumps({"method": "downloads." + method, "arguments": {"hashes": ["a" * 40]}}))
        try:
            await bridge.dispatch(request.name)
            assert bridge.proposal is not None
            plan = bridge.proposal.effect_plan
        finally:
            await bridge.close()
        assert plan is not None and qb_backend.client.writes == []
        assert "qb_snapshot" not in json.dumps(plan.public_dict())
        # 新建 store/executor，从原 SQLite 确认账本恢复快照，不依赖原内存对象。
        cold = _executor(SQLiteKernelStore())
        result = await cold.execute_confirmed(plan.plan_id, context=context)
        receipt = result.outcome.public_content
        assert receipt["status"] == "completed" and receipt["data"]["verification"] == "verified"
        assert receipt["data"]["delete_files"] is False
        with pytest.raises(ToolPipelineError):
            await cold.execute_confirmed(plan.plan_id, context=context)
        assert qb_backend.client.writes == [(operation, "a" * 40)]
        assert next(task for task in qb_backend.client.tasks if task.hash == "b" * 40).state == "downloading"
    asyncio.run(run())


@pytest.mark.parametrize("change", ["cancel", "cancel_during_read", "drift", "unknown", "read_failed"])
def test_native_qb_cancel_drift_and_unknown_never_replay(qb_backend, monkeypatch, change):
    async def run():
        store = SQLiteKernelStore()
        context = await _context(store)
        effects = _executor(store)
        plan = (await effects.prepare("downloads.pause", {"hashes": ["a" * 40]}, context=context)).effect_plan
        assert plan is not None
        client = qb_backend.client
        if change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "cancel_during_read":
            original = client.list_torrents
            def read():
                context.cancellation.cancel()
                return original()
            monkeypatch.setattr(client, "list_torrents", read)
        elif change == "drift":
            client.tasks[0].added_on += 1
        else:
            client.mode = change
        if change in {"unknown", "read_failed"}:
            result = await effects.execute_confirmed(plan.plan_id, context=context)
            public = result.outcome.public_content
            assert public["status"] == "outcome_unknown" and "private-" not in json.dumps(public)
            assert public["data"]["accepted"] is (True if change == "read_failed" else None)
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
            if change == "drift":
                assert error.value.code == "confirmation_stale" and error.value.execution_started is False
            assert client.writes == []
    asyncio.run(run())


def test_native_qb_catalog_retires_provider_and_keeps_sdk_methods(qb_backend):
    from app import sdk
    from app.agent.kernel.bootstrap import build_agent_kernel_runtime
    assert {"downloads.tasks", "downloads.transfer", "downloads.version", "downloads.files"} <= sdk.METHODS.keys()
    catalog = build_agent_kernel_runtime(model=SimpleNamespace()).session.catalog
    for name in ("downloads.pause", "downloads.resume", "downloads.remove"):
        assert catalog.has(name) and name in sdk.WRITE_METHODS
    assert catalog.has("library.refresh") and "library.refresh" in sdk.WRITE_METHODS
