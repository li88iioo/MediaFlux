"""原生分享 SDK：离线 Provider、root 文件 IPC、持久确认与唯一执行账本。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app import config, database as db, sdk
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.bootstrap import build_agent_kernel_runtime
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.ports import sdk_effects
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.state_types import CancellationToken, StalePublicationError
from app.agent.kernel.tool_calls import ToolCallContext
from app.clients.guangya import GuangYaShareError, GuangYaWriteRejected
from tests.support import initialized_web_runtime
from tests.test_agent_guangya_share_generation import ShareClient
from tests.test_guangya_read_contracts import ReadClient


def _deny_network(*_args, **_kwargs):
    raise AssertionError("native share tests forbid network")


@pytest.fixture
def backend(tmp_path, monkeypatch):
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, _deny_network)
    monkeypatch.setattr(socket, "create_connection", _deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", _deny_network)
    with initialized_web_runtime() as paths:
        client = ShareClient()
        monkeypatch.setattr(sdk_effects, "GuangYaClient", lambda: client)
        monkeypatch.setattr("app.clients.guangya.GuangYaClient", lambda: client)
        yield SimpleNamespace(client=client, root=tmp_path, paths=paths)


async def context_for(store, *, owner="webk:v1:" + "f" * 64, session="native-share"):
    lease, _ = await store.begin_turn(owner=owner, session_id=session, request_id="share-test")
    return ToolCallContext(owner=owner, session_id=session, request_id=lease.request_id,
        turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(),
        report_progress=AsyncMock(), channel="web")


def executor(store, *, confirmation_store=None):
    return EffectExecutor(catalog=ToolCatalog(sdk_effects.cloud_share_tools()),
        state_store=store, reference_store=store,
        effect_store=ConfirmationEffectPlanStore(confirmation_store or SQLiteConfirmationStore(), record_actions=True),
        authorization=MediaFluxAuthorizationPolicy(), effect_lifecycle=MediaFluxEffectLifecycle())


def arguments_for(tool):
    return {"share_ids": ["20002"]} if tool == "cloud.revoke_share" else {
        "file_ids": ["file-1"], "expires_days": 7, "access_code": "Ab12Cd34", "allow_download": False}


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="需要可运行 Bubblewrap 的 Linux 隔离工作区")
@pytest.mark.parametrize("tool", ["cloud.share", "cloud.revoke_share"])
def test_root_workspace_file_ipc_reads_real_ids_and_only_prepares_write(backend, tool):
    async def check():
        assert sandbox.sandbox_supported(), sandbox.workspace_unavailable_reason()
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        data, bridge = await sandbox._execute(backend.root / "workspace", {"code":
            'from mf import cloud\nimport json\nprint(json.dumps(cloud.shares()))\n'
            + ("cloud.revoke_share(['20002'])\n" if tool == "cloud.revoke_share" else
               "cloud.share(['file-1'], expires_days=7, access_code='Ab12Cd34', allow_download=False)\n")
            + "open('/work/replayed', 'w').write('bad')\n", "timeout_seconds": 20}, context, effects)
        assert backend.client.writes == [], data
        assert [row["method"] for row in data["sdk_calls"]] == ["cloud.shares", tool], data
        assert json.loads(data["stdout"])["items"][1]["share_id"] == "20002"
        assert bridge.proposal is not None and bridge.proposal.effect_plan is not None, data
        plan = bridge.proposal.effect_plan
        assert plan.tool_name == tool and plan.preview["data"]["count"] == 1
        assert "share_snapshot" not in json.dumps(plan.public_dict())
        assert "Ab12Cd34" not in json.dumps(plan.public_approval_dict())
        restored = await executor(SQLiteKernelStore()).get_pending_plan(owner=context.owner, session_id=context.session_id)
        assert restored is not None
        assert restored.plan_id == plan.plan_id and restored.metadata["share_snapshot"] == plan.metadata["share_snapshot"]
        backend.client.shares.reverse()
        result = await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
        receipt = result.outcome.public_content
        assert receipt["status"] == "completed" and receipt["data"]["verified"]
        assert "share_url" not in result.outcome.model_content and "access_code" not in result.outcome.model_content
        with pytest.raises(ToolPipelineError):
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert len(backend.client.writes) == 1
        assert backend.client.writes[0][1] == (["20002"] if tool == "cloud.revoke_share" else ["file-1"])
        assert not (backend.root / "workspace/work/replayed").exists()
    asyncio.run(check())


@pytest.mark.parametrize("tool", ["cloud.share", "cloud.revoke_share"])
@pytest.mark.parametrize("change", ["generation", "config_aba", "object", "same_name_replacement", "cancel", "cancel_during_read", "owner", "session", "publication"])
def test_confirm_rejects_stale_or_foreign_plans_before_writing(backend, monkeypatch, tool, change):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        plan = (await effects.prepare(tool, arguments_for(tool), context=context)).effect_plan
        assert plan is not None and backend.client.writes == []
        if change == "generation":
            backend.client.credential_generation += 1
        elif change == "config_aba":
            original = config.get("TG_QB_SAVE_PATH", "")
            config.set_and_save({"TG_QB_SAVE_PATH": "/changed"})
            config.set_and_save({"TG_QB_SAVE_PATH": original})
        elif change == "object":
            if tool == "cloud.share":
                backend.client.files[0].name = "changed"
            else:
                backend.client.shares[1]["status"] = "revoked"
        elif change == "same_name_replacement":
            if tool == "cloud.share":
                backend.client.files[0].file_id = "replacement-file"
            else:
                backend.client.shares[1]["id"] = "replacement-share"
        elif change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "cancel_during_read":
            method = "file_info" if tool == "cloud.share" else "list_user_shares"
            original = getattr(backend.client, method)
            def cancelled(*args, **kwargs):
                result = original(*args, **kwargs)
                context.cancellation.cancel()
                return result
            monkeypatch.setattr(backend.client, method, cancelled)
        elif change in {"owner", "session"}:
            context = await context_for(store, owner="webk:v1:" + "a" * 64 if change == "owner" else context.owner,
                                        session="another" if change == "session" else context.session_id)
        else:
            await store.begin_turn(owner=context.owner, session_id=context.session_id, request_id="superseded")
        with pytest.raises((ToolPipelineError, StalePublicationError, asyncio.CancelledError)) as raised:
            await effects.execute_confirmed(plan.plan_id, context=context)
        if change in {"generation", "config_aba", "object", "same_name_replacement"}:
            assert isinstance(raised.value, ToolPipelineError)
            assert raised.value.code == "confirmation_stale" and raised.value.execution_started is False
        assert backend.client.writes == []
    asyncio.run(check())


@pytest.mark.parametrize("tool", ["cloud.share", "cloud.revoke_share"])
def test_expired_confirmation_is_consumed_without_provider_write(backend, tool):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        now = [2_000_000_000.0]
        effects = executor(store, confirmation_store=SQLiteConfirmationStore(clock=lambda: now[0]))
        plan = (await effects.prepare(tool, arguments_for(tool), context=context)).effect_plan
        assert plan is not None
        now[0] = plan.expires_at + 1
        with pytest.raises(ToolPipelineError) as raised:
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert raised.value.code == "confirmation_invalid" and backend.client.writes == []
        with pytest.raises(ToolPipelineError):
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert backend.client.writes == []
    asyncio.run(check())


@pytest.mark.parametrize("tool", ["cloud.share", "cloud.revoke_share"])
def test_two_concurrent_confirmations_claim_provider_write_once(backend, tool):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        plan = (await executor(store).prepare(tool, arguments_for(tool), context=context)).effect_plan
        assert plan is not None
        results = await asyncio.gather(*[executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
                                         for _ in range(2)], return_exceptions=True)
        assert sum(not isinstance(result, BaseException) for result in results) == 1, results
        assert len(backend.client.writes) == 1
    asyncio.run(check())


@pytest.mark.parametrize("tool", ["cloud.share", "cloud.revoke_share"])
@pytest.mark.parametrize("outcome", ["unknown", "rejected", "accepted"])
def test_post_submission_results_are_not_replayed_or_overstated(backend, monkeypatch, tool, outcome):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        plan = (await effects.prepare(tool, arguments_for(tool), context=context)).effect_plan
        assert plan is not None
        method = "create_user_share" if tool == "cloud.share" else "delete_user_shares"
        original = getattr(backend.client, method)
        def submit(*args, **kwargs):
            if outcome == "rejected":
                raise GuangYaWriteRejected("share_create", code="400", message="https://www.guangyapan.com/s/private-link?code=Ab12Cd34")
            result = original(*args, **kwargs)
            if outcome == "unknown":
                raise RuntimeError("private-provider-error")
            backend.client.read_failure = True
            return {"code": 200, "msg": "操作成功"} if tool == "cloud.share" else result
        monkeypatch.setattr(backend.client, method, submit)
        if outcome == "rejected":
            with pytest.raises(ToolPipelineError) as raised:
                await effects.execute_confirmed(plan.plan_id, context=context)
            assert raised.value.code == "provider_rejected" and raised.value.execution_started is False
            assert "private-link" not in str(raised.value) and "Ab12Cd34" not in str(raised.value)
        else:
            result = (await effects.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
            assert result["status"] == ("outcome_unknown" if outcome == "unknown" else "accepted")
            assert result["data"]["verification_pending"] and not result["data"]["verified"]
            assert "private-provider-error" not in json.dumps(result)
            assert "未执行业务操作" not in json.dumps(result, ensure_ascii=False)
        with pytest.raises(ToolPipelineError):
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert len(backend.client.writes) == (0 if outcome == "rejected" else 1)
    asyncio.run(check())


@pytest.mark.parametrize("tool", ["cloud.share", "cloud.revoke_share"])
def test_real_cold_process_restores_private_plan_and_confirms_once(backend, tool):
    async def prepare():
        store = SQLiteKernelStore()
        context = await context_for(store)
        plan = (await executor(store).prepare(tool, arguments_for(tool), context=context)).effect_plan
        assert plan is not None
        return context, plan
    context, plan = asyncio.run(prepare())
    state = backend.root / "cold.json"
    state.write_text(json.dumps({"paths": {key: str(value) for key, value in asdict(backend.paths).items()},
        "startup_overrides": dict(config.capture_runtime_config_snapshot().startup_overrides),
        "owner": context.owner, "session": context.session_id, "plan_id": plan.plan_id}))
    child = subprocess.run([sys.executable, "-c", r'''
import tests
import asyncio, json, os, socket, sys
from pathlib import Path
for name in ("connect", "connect_ex", "sendto"):
    setattr(socket.socket, name, lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network forbidden")))
state = json.loads(Path(sys.argv[1]).read_text())
from app import config, database as db, runtime_paths
paths = runtime_paths.RuntimePaths(**{key: Path(value) for key, value in state["paths"].items()})
runtime_paths.configure_runtime_paths(paths)
config.PATHS, config.ENV_FILE, config._cache = paths, paths.env_file, None
config._SYSTEM_ENV_SNAPSHOT = state["startup_overrides"]
db.configure_database(paths.database_path, test_mode=False)
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.state_types import CancellationToken, PublicationLease
from app.agent.kernel.tool_calls import ToolCallContext
from app.agent.kernel.ports import sdk_effects
from app.agent.errors import ToolPipelineError
from tests.test_agent_sdk_share_effects import executor
from tests.test_agent_guangya_share_generation import ShareClient
client = ShareClient()
sdk_effects.GuangYaClient = lambda: client
async def run():
    store = SQLiteKernelStore()
    current = await store.load(owner=state["owner"], session_id=state["session"])
    publication = current.metadata["active_publication"]
    lease = PublicationLease(current.owner, current.session_id, current.generation,
        publication["turn_id"], publication["request_id"])
    async def progress(event):
        pass
    context = ToolCallContext(owner=current.owner, session_id=current.session_id, request_id=lease.request_id,
        turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=progress, channel="web")
    effects = executor(store)
    plan = await effects.get_pending_plan(owner=current.owner, session_id=current.session_id)
    assert plan.plan_id == state["plan_id"]
    result = await effects.execute_confirmed(plan.plan_id, context=context)
    try:
        await effects.execute_confirmed(plan.plan_id, context=context)
    except ToolPipelineError:
        pass
    else:
        raise AssertionError("cold confirmation was replayed")
    print(json.dumps({"pid": os.getpid(), "result": result.outcome.public_content, "writes": client.writes}))
asyncio.run(run())
''', str(state)], capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr
    result = json.loads(child.stdout.splitlines()[-1])
    assert result["pid"] != os.getpid() and result["result"]["status"] == "completed"
    assert len(result["writes"]) == 1 and backend.client.writes == []


@pytest.mark.parametrize("outcome", ["completed", "unknown"])
def test_share_links_codes_and_provider_exceptions_never_enter_logs_or_audit(backend, monkeypatch, caplog, outcome):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        plan = (await effects.prepare("cloud.share", arguments_for("cloud.share"), context=context)).effect_plan
        assert plan is not None
        secret_url = "https://www.guangyapan.com/s/private-share-link?code=Ab12Cd34"
        original = backend.client.create_user_share
        def submit(*args, **kwargs):
            result = original(*args, **kwargs)
            if outcome == "unknown":
                raise RuntimeError(secret_url + " private-provider-token")
            result["data"]["shareUrl"] = secret_url
            return result
        monkeypatch.setattr(backend.client, "create_user_share", submit)
        def close_failure():
            raise RuntimeError(secret_url + " private-provider-token")
        monkeypatch.setattr(backend.client, "close", close_failure)
        with caplog.at_level(logging.DEBUG):
            result = await effects.execute_confirmed(plan.plan_id, context=context)
        assert result.outcome.public_content["status"] == ("completed" if outcome == "completed" else "outcome_unknown")
        if outcome == "completed":
            assert result.outcome.public_content["data"]["share_url"] == secret_url
        history = db.list_agent_action_history(owner_digest=db.media_owner_digest(context.owner), limit=10)
        assert len(history) == 1 and history[0]["status"] == ("completed" if outcome == "completed" else "outcome_unknown")
        for value in ("private-share-link", "Ab12Cd34", "private-provider-token"):
            assert value not in caplog.text + json.dumps([dict(row) for row in history]) + result.outcome.model_content
        with pytest.raises(ToolPipelineError):
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert len(backend.client.writes) == 1
    asyncio.run(check())


def test_only_native_methods_remain_in_catalog_and_sdk(backend):
    catalog = build_agent_kernel_runtime(model=SimpleNamespace()).session.catalog
    assert {"cloud.share", "cloud.revoke_share"} <= sdk.WRITE_METHODS
    assert "cloud.shares" in sdk.METHODS and "cloud.shares" not in sdk.WRITE_METHODS
    assert catalog.has("cloud.share") and catalog.has("cloud.revoke_share")
    for name in ("guangya.share.list", "guangya.share.create", "guangya.share.revoke"):
        assert not catalog.has(name) and not catalog.has(name.replace(".", "__"))


@pytest.mark.parametrize("failure", [None, "second_page", "missing_identity", "limit", "stuck"])
def test_shares_sdk_keeps_complete_paging_management_ids_and_errors(backend, monkeypatch, failure):
    class PagedClient(ReadClient):
        logged_in = True
        credential_generation = 9
        def close(self):
            return True
    client = PagedClient(None)
    rows = [{"id": str(i), "shareId": f"public-{i}", "title": "同名分享"} for i in range(103)]
    rows[1]["shareId"] = rows[0]["shareId"]  # 公开链接标识不能吞掉另一条管理记录。
    pages: list[Any] = [{"data": {"total": 103, "list": rows[:52]}}, {"data": {"total": 103, "list": rows[51:]}}]
    if failure == "second_page":
        pages[1] = RuntimeError("private-provider-error")
    elif failure == "missing_identity":
        del rows[1]["id"]
    elif failure == "limit":
        pages = [{"data": {"total": 2001, "list": [
            {"id": str(i), "shareId": f"public-{i}"} for i in range(start, start + 100)]}} for start in range(0, 2100, 100)]
    elif failure == "stuck":
        pages[1] = pages[0]
    client.raw.share_user_list.side_effect = pages
    monkeypatch.setattr("app.clients.guangya.GuangYaClient", lambda: client)
    if failure:
        with pytest.raises((RuntimeError, GuangYaShareError)):
            sdk.cloud.shares()
    else:
        result = sdk.cloud.shares()
        assert len(result["items"]) == 103 and {row["share_id"] for row in result["items"]} == {str(i) for i in range(103)}
        assert client.raw.share_user_list.call_count == 2
        assert "access_code" not in json.dumps(result)
