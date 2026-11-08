"""原生反代管理：真实SQLite/SDK/文件IPC；运行时与上游HTTP仅用替身。"""
from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import json
import socket
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from app import database as db, sdk
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.kernel.bootstrap import build_tool_catalog
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.sdk_effects import playback_tools
from app.agent.kernel.state_types import CancellationToken
from app.modules import media_proxy as service
from app.repositories import media_proxy as repository
from tests.support import isolated_test_database
from tests.test_agent_sdk_effects import context_for


def add(*, enabled=1, config_source="custom", port=19096):
    return db.add_media_proxy_instance(name="真实实例名称", server_type="jellyfin", config_source=config_source,
        upstream_url="http://upstream.invalid:8096/emby", api_key="NATIVE_PROXY_PRIVATE_KEY",
        listen_host="127.0.0.1", listen_port=port, local_root="/fixture/media", enabled=enabled)


@pytest.fixture
def backend(monkeypatch):
    for key, value in {"JELLYFIN_ENABLED": "1", "JELLYFIN_URL": "http://configured.invalid:8096",
                       "JELLYFIN_API_KEY": "NATIVE_CONFIGURED_PRIVATE_KEY"}.items():
        monkeypatch.setenv(key, value)
    manager = Mock()
    manager.status.return_value = {}
    manager.request_reconcile.return_value = True
    future = concurrent.futures.Future()
    future.set_result({"restarted": True, "reason": ""})
    manager.request_restart.return_value = future
    with (isolated_test_database(), patch.object(service, "get_media_proxy_manager", return_value=manager),
          patch.object(service, "clear_signed_url_cache", return_value=3) as clear,
          patch.object(service, "_PROXY_RESTART_WAIT_SECONDS", 0.001),
          patch("socket.socket.connect", side_effect=AssertionError("不得联网"))):
        yield SimpleNamespace(manager=manager, clear=clear)


def executor():
    store = SQLiteKernelStore()
    return EffectExecutor(catalog=ToolCatalog(playback_tools()), state_store=store, reference_store=store,
                          effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))


def runtime(instance_id: int) -> service.ProxyRuntime:
    return service.ProxyRuntime(instance_id=instance_id, bind=("127.0.0.1", 19096),
        server=Mock(spec=service.uvicorn.Server), task=Mock(spec=asyncio.Task),
        sock=Mock(spec=socket.socket), signed_urls=service.SignedUrlCache())


def record(instance_id, index, status=502):
    return db.record_media_proxy_playback_attempt(instance_id=instance_id, playback_session_key="fixture-session",
        media_item_id="fixture-media", media_name="保留的媒体名", route_class=f"route_{index % 12}", method="GET",
        status_code=status, source="guangya", failure_stage=f"stage_{index % 12}", total_latency_ms=55,
        upstream_latency_ms=12, error="无法生成直链 " + "完整诊断细节。" * 80
        + " tail-fact NATIVE_PROXY_PRIVATE_KEY password=VERY_PRIVATE https://private.invalid/?token=PRIVATE")


def test_only_native_entries_and_real_instance_id_contract():
    old = {"media_proxy." + name for name in ("status_summary", "playback_failure_summary", "test_instance", "set_instance_enabled", "restart_instance")}
    assert not old & {spec.name for spec in build_tool_catalog().visible()}
    for module in ("proxy_actions", "tools"):
        assert importlib.util.find_spec("app.agent.domains.playback." + module) is None
    assert {"playback.instances", "playback.failures", "playback.test", "playback.set_enabled", "playback.restart"} <= sdk.METHODS.keys()
    assert {"playback.set_enabled", "playback.restart"} <= sdk.WRITE_METHODS
    for tool in playback_tools():
        assert "instance_number" not in tool.input_schema["properties"]
        assert tool.name not in old


def test_instances_keep_full_configuration_runtime_errors_and_real_ids(backend):
    removed = add(port=19090)
    target = add(port=19091, config_source="configured:jellyfin")
    db.delete_media_proxy_instance(removed)
    db.update_media_proxy_instance(target, {"status": "error", "last_error": "根因：" + "详细异常。" * 160 + " tail-fact NATIVE_CONFIGURED_PRIVATE_KEY"})
    backend.manager.status.return_value = {target: {"running": True, "listen_host": "127.0.0.1", "listen_port": 19091}}
    result = sdk.playback.instances()
    item, = result["data"]["instances"]
    assert item["instance_id"] == item["id"] == target and target != 1
    assert item["local_root"] == "/fixture/media" and item["listen_port"] == 19091
    assert item["effective_config"]["upstream_url"] == "http://configured.invalid:8096"
    assert item["runtime_status"] == "running" and item["runtime_scope"] == "current_process"
    assert "tail-fact" in item["last_error"] and len(item["last_error"]) > 600
    assert "PRIVATE_KEY" not in json.dumps(result)
    assert item["api_key"] == item["effective_config"]["api_key"] == "********"
    backend.manager.status.side_effect = RuntimeError("PRIVATE runtime unavailable")
    unknown = sdk.playback.instances()
    assert unknown["status"] == "partial" and not unknown["ok"]
    assert unknown["data"]["instances"][0]["runtime_status"] == "unknown"
    backend.clear.assert_not_called()
    backend.manager.request_restart.assert_not_called()
    backend.manager.request_reconcile.assert_not_called()


def test_failures_read_all_stored_facts_in_pages_without_top_eight_loss(backend):
    identifier = add()
    ids = [record(identifier, i) for i in range(205)]
    record(identifier, 999, 302)
    old = record(identifier, 1000)
    with db.get_conn() as conn:
        conn.execute("UPDATE media_proxy_playback_records SET created_at=datetime('now','-2 days','localtime') WHERE id=?", (old,))
    items, offset = [], 0
    while True:
        result = sdk.playback.failures(instance_id=identifier, hours=24, limit=120, offset=offset)
        data = result["data"]
        assert data["failed"] == data["total"] == 205 and data["total_recorded"] == 206
        assert data["summary_group_limit"] == len(data["failure_stages"]) == 8
        assert data["coverage"] == "recorded_proxy_requests_only"
        items.extend(data["items"])
        if not data["has_more"]:
            assert data["next_offset"] is None
            break
        offset = data["next_offset"]
    assert [row["id"] for row in items] == list(reversed(ids))
    assert len({row["failure_stage"] for row in items}) == 12
    assert all(row["instance_id"] == identifier and row["status_code"] == 502 for row in items)
    assert "tail-fact" in items[0]["error"] and len(items[0]["error"]) > 600
    assert type(items[0]["session_id"]) is int  # 数据库关联ID不是上游会话令牌。
    assert "PRIVATE" not in json.dumps(items)
    assert "不能还原" in data["error_note"]
    backend.manager.request_restart.assert_not_called()
    with patch.object(repository, "media_proxy_failure_facts", side_effect=RuntimeError("database unavailable")):
        with pytest.raises(RuntimeError):
            sdk.playback.failures()


@pytest.mark.parametrize("code,expected", [(204, "reachable"), (401, "authentication_failed"),
                                          (302, "redirect_not_allowed"), (503, "upstream_error")])
def test_native_probe_uses_original_fixed_probe_http_boundary(backend, code, expected):
    identifier = add()
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(code, headers={"Location": "http://no-follow.invalid"}, content=b"diagnostic")
    factory = httpx.AsyncClient
    with (patch.object(service, "_pin_upstream_target", return_value=SimpleNamespace(
            connect_url="http://127.0.0.1/System/Info", host_header="upstream.invalid:8096", sni_hostname="upstream.invalid")),
          patch.object(service.httpx, "AsyncClient", side_effect=lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs))):
        result = sdk.playback.test(identifier)
    assert result["data"]["instance_id"] == identifier
    assert result["data"]["connection_status"] == expected and result["data"]["status_code"] == code
    assert result["ok"] == (code == 204)
    assert len(requests) == 1 and requests[0].url.path == "/System/Info"
    assert requests[0].headers["Authorization"] == 'MediaBrowser Token="NATIVE_PROXY_PRIVATE_KEY"'
    assert "PRIVATE_KEY" not in json.dumps(result)
    backend.clear.assert_not_called()


def test_probe_timeout_and_missing_target_preserve_unknown_not_healthy(backend):
    identifier = add()
    with patch.object(service, "probe_media_proxy_instance", new=AsyncMock(side_effect=httpx.ReadTimeout("PRIVATE target"))):
        result = sdk.playback.test(identifier)
    assert result["status"] == "timeout" and not result["ok"] and "PRIVATE" not in json.dumps(result)
    result = sdk.playback.test(identifier + 999)
    assert result["status"] == "not_configured" and not result["data"]["network_accessed"]


@pytest.mark.parametrize("operation", ["set_enabled", "restart"])
def test_cold_sqlite_confirmation_only_operates_real_target_once(backend, operation):
    first = add(port=19090)
    target = add(port=19091)
    args = {"instance_id": target, **({"enabled": False} if operation == "set_enabled" else {})}
    async def check():
        effects = executor()
        context = await context_for(effects.state_store)
        prepared = await effects.prepare("playback." + operation, args, context=context)
        plan = prepared.effect_plan
        assert plan is not None and plan.arguments == args
        assert plan.preview["data"]["instance_id"] == target
        backend.clear.assert_not_called()
        backend.manager.request_restart.assert_not_called()
        backend.manager.request_reconcile.assert_not_called()
        db.delete_media_proxy_instance(first)  # 原序号移动不改变真实目标。
        cold = executor()
        pending = await cold.get_pending_plan(owner=context.owner, session_id=context.session_id)
        assert pending is not None and pending.plan_id == plan.plan_id
        result = await cold.execute_confirmed(plan.plan_id, context=context)
        assert result.outcome.public_content["status"] == "completed"
        assert result.outcome.public_content["data"]["instance_id"] == target
        with pytest.raises(ToolPipelineError):
            await executor().execute_confirmed(plan.plan_id, context=context)
        backend.clear.assert_called_once_with(target)
        row = db.get_media_proxy_instance(target)
        assert row is not None
        if operation == "set_enabled":
            assert row["enabled"] == 0
            assert result.outcome.public_content["data"]["runtime_refreshed"]
            backend.manager.request_reconcile.assert_called_once_with()
            backend.manager.request_restart.assert_not_called()
        else:
            assert row["enabled"] == 1
            backend.manager.request_restart.assert_called_once_with(target, expected_fingerprint=plan.metadata["proxy_fingerprint"])
            assert result.outcome.public_content["data"]["restarted"] is True
    asyncio.run(check())


@pytest.mark.parametrize("change", ["cancel", "token", "cancel_during_recheck", "owner", "session", "delete", "target_config", "credential", "forwarding", "profile"])
@pytest.mark.parametrize("operation", ["set_enabled", "restart"])
def test_frozen_target_config_cancellation_and_scope_block_all_effects(backend, monkeypatch, change, operation):
    target = add(config_source="configured:jellyfin" if change == "profile" else "custom")
    args = {"instance_id": target, **({"enabled": False} if operation == "set_enabled" else {})}
    async def check():
        effects = executor()
        context = await context_for(effects.state_store)
        plan = (await effects.prepare("playback." + operation, args, context=context)).effect_plan
        assert plan is not None
        if change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "token":
            context.cancellation.cancel()
        elif change in {"owner", "session"}:
            context = await context_for(SQLiteKernelStore(), **{change: "other"})
        elif change == "delete":
            db.delete_media_proxy_instance(target)
        elif change == "target_config":
            db.update_media_proxy_instance(target, {"listen_port": 29096, "local_root": "/new/media"})
        elif change == "credential":
            db.update_media_proxy_instance(target, {"api_key": "changed-private-key"})
        elif change == "forwarding":
            db.update_media_proxy_instance(target, {"trust_forwarded_headers": 1, "trusted_proxy_cidrs_json": '["10.0.0.0/8"]'})
        elif change == "profile":
            monkeypatch.setenv("JELLYFIN_API_KEY", "changed-configured-key")
        capture = service._proxy_change_state
        def capture_then_cancel(*args, **kwargs):
            state = capture(*args, **kwargs)
            if change == "cancel_during_recheck":
                context.cancellation.cancel()
            return state
        with patch.object(service, "_proxy_change_state", side_effect=capture_then_cancel):
            if change == "cancel_during_recheck":
                result = await executor().execute_confirmed(plan.plan_id, context=context)
                assert result.outcome.public_content["status"] == "cancelled"
                with pytest.raises(ToolPipelineError):
                    await executor().execute_confirmed(plan.plan_id, context=replace(context, cancellation=CancellationToken()))
            else:
                with pytest.raises((ToolPipelineError, asyncio.CancelledError)) as caught:
                    await executor().execute_confirmed(plan.plan_id, context=context)
                if change in {"delete", "target_config", "credential", "forwarding", "profile"}:
                    assert isinstance(caught.value, ToolPipelineError)
                    assert caught.value.code == "confirmation_stale" and caught.value.execution_started is False
        backend.clear.assert_not_called()
        backend.manager.request_restart.assert_not_called()
        backend.manager.request_reconcile.assert_not_called()
        if change != "delete":
            row = db.get_media_proxy_instance(target)
            assert row is not None and row["enabled"] == 1
    asyncio.run(check())


@pytest.mark.parametrize("outcome,expected", [("pending", "outcome_unknown"), ("exception", "outcome_unknown"),
                                             ("rejected", "unavailable"), ("failed", "failed"),
                                             ("missing_result", "outcome_unknown"), ("invalid_boolean", "outcome_unknown")])
def test_restart_original_future_unknown_is_consumed_without_cancel_or_replay(backend, outcome, expected):
    target = add()
    future = concurrent.futures.Future()
    if outcome == "exception":
        future.set_exception(RuntimeError("PRIVATE backend exception"))
    elif outcome == "failed":
        future.set_result({"restarted": False, "reason": "start_failed"})
    elif outcome == "missing_result":
        future.set_result({})
    elif outcome == "invalid_boolean":
        future.set_result({"restarted": "true"})
    backend.manager.request_restart.return_value = None if outcome == "rejected" else future
    async def check():
        effects = executor()
        context = await context_for(effects.state_store)
        plan = (await effects.prepare("playback.restart", {"instance_id": target}, context=context)).effect_plan
        assert plan is not None
        result = await executor().execute_confirmed(plan.plan_id, context=context)
        public = result.outcome.public_content
        assert public["status"] == expected and not public["ok"]
        assert public["data"]["accepted"] == (outcome != "rejected")
        assert "PRIVATE" not in json.dumps(public) and not future.cancelled()
        with pytest.raises(ToolPipelineError):
            await executor().execute_confirmed(plan.plan_id, context=context)
        assert backend.manager.request_restart.call_count == backend.clear.call_count == 1
    asyncio.run(check())


def test_enable_saved_when_runtime_reconcile_not_queued(backend):
    target = add(enabled=0)
    backend.manager.request_reconcile.side_effect = RuntimeError("PRIVATE queue error")
    result = sdk.playback.set_enabled(target, True)
    assert result["ok"] and result["status"] == "completed" and not result["data"]["runtime_refreshed"]
    row = db.get_media_proxy_instance(target)
    assert row is not None and row["enabled"] == 1 and "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("arguments", [{"instance_number": 1}, {"instance_id": True}, {"instance_id": 0},
                                         {"instance_id": "1"}, {"instance_id": 1, "enabled": "false"},
                                         {"instance_id": 1, "enabled": None}])
def test_native_control_rejects_old_ordinals_and_invalid_parameters(backend, arguments):
    async def check():
        effects = executor()
        context = await context_for(effects.state_store)
        with pytest.raises((ToolPipelineError, ValueError, TypeError)):
            await effects.prepare("playback.set_enabled", arguments, context=context)
        backend.clear.assert_not_called()
    asyncio.run(check())


def test_queued_restart_config_drift_does_not_stop_or_start_runtime(backend):
    target = add()
    prepared = service.prepare_playback_proxy_change("restart", target)
    db.update_media_proxy_instance(target, {"listen_port": 29096})
    manager = service.MediaProxyManager()
    previous = runtime(target)
    manager._runtimes[target] = previous
    async def check():
        with patch.object(manager, "_stop_runtime", new=AsyncMock()) as stop, patch.object(manager, "_start_runtime", new=AsyncMock()) as start:
            result = await manager.restart_instance(target, expected_fingerprint=prepared["fingerprint"])
        assert result == {"restarted": False, "reason": "confirmation_stale"}
        stop.assert_not_awaited()
        start.assert_not_awaited()
        assert manager._runtimes[target] is previous
    asyncio.run(check())


def test_frozen_invalid_profile_keeps_existing_runtime_before_restart(backend, monkeypatch):
    target = add(config_source="configured:jellyfin")
    monkeypatch.setenv("JELLYFIN_ENABLED", "0")
    prepared = service.prepare_playback_proxy_change("restart", target)
    manager = service.MediaProxyManager()
    previous = runtime(target)
    manager._runtimes[target] = previous
    async def check():
        with patch.object(manager, "_stop_runtime", new=AsyncMock()) as stop, patch.object(manager, "_start_runtime", new=AsyncMock()) as start:
            result = await manager.restart_instance(target, expected_fingerprint=prepared["fingerprint"])
        assert result == {"restarted": False, "reason": "invalid_config"}
        stop.assert_not_awaited()
        start.assert_not_awaited()
        assert manager._runtimes[target] is previous
    asyncio.run(check())


def test_native_restart_uses_original_manager_future_and_resolved_configuration(backend):
    target = add()
    manager = service.MediaProxyManager()
    previous, replacement = runtime(target), runtime(target)
    manager._runtimes[target] = previous
    async def check():
        manager._loop = asyncio.get_running_loop()
        with (patch.object(service, "get_media_proxy_manager", return_value=manager),
              patch.object(service, "_PROXY_RESTART_WAIT_SECONDS", 2.0),
              patch.object(manager, "_stop_runtime", new=AsyncMock()) as stop,
              patch.object(manager, "_start_runtime", new=AsyncMock(return_value=replacement)) as start):
            result = await asyncio.to_thread(sdk.playback.restart, target)
        assert result["status"] == "completed" and result["data"]["restarted"] is True
        stop.assert_awaited_once_with(previous)
        start.assert_awaited_once()
        started = start.await_args
        assert started is not None
        assert started.args[0]["id"] == target
        assert started.args[0]["api_key"] == "NATIVE_PROXY_PRIVATE_KEY"
        assert manager._runtimes[target] is replacement
    asyncio.run(check())


def test_enable_exception_after_sqlite_write_is_unknown_and_never_replayed(backend):
    target = add()
    backend.clear.side_effect = RuntimeError("PRIVATE cache failure after commit")
    async def check():
        effects = executor()
        context = await context_for(effects.state_store)
        plan = (await effects.prepare("playback.set_enabled", {"instance_id": target, "enabled": False}, context=context)).effect_plan
        assert plan is not None
        with pytest.raises(ToolPipelineError) as caught:
            await executor().execute_confirmed(plan.plan_id, context=context)
        assert caught.value.execution_started is True and "PRIVATE" not in str(caught.value)
        row = db.get_media_proxy_instance(target)
        assert row is not None and row["enabled"] == 0
        with pytest.raises(ToolPipelineError):
            await executor().execute_confirmed(plan.plan_id, context=context)
        assert backend.clear.call_count == 1
    asyncio.run(check())


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="需要现有root镜像的真实隔离工作区")
@pytest.mark.parametrize("language", ["python", "shell"])
def test_native_python_cli_full_fact_files_to_cold_confirmation(backend, tmp_path, monkeypatch, language):
    obsolete = add(port=19090)
    target = add(port=19091)
    db.delete_media_proxy_instance(obsolete)
    record(target, 0)
    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        catalog = ToolCatalog(playback_tools())
        pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store,
            effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))
        catalog.register(sandbox.run_code_tool(pipeline))
        code = ('import json\nfrom mf import playback\n'
                'instance = playback.instances()["data"]["instances"][0]\n'
                'facts = playback.failures(instance_id=instance["instance_id"])\n'
                'open("/work/instances.json", "w").write(json.dumps(instance))\n'
                'open("/work/failures.json", "w").write(json.dumps(facts))\n'
                'print(json.dumps({"instance_id": instance["instance_id"], "returned": facts["data"]["returned"]}))\n') if language == "python" else (
                    'mf playback.instances > /work/instances.json\n'
                    'python -c \'import json; p=json.load(open("/work/instances.json")); print(p["data"]["instances"][0]["instance_id"])\' > /work/id.txt\n'
                    'mf playback.failures --instance-id "$(cat /work/id.txt)" > /work/failures.json\n'
                    'python -c \'import json; p=json.load(open("/work/failures.json")); print(json.dumps({"instance_id":p["data"]["instance_id"],"returned":p["data"]["returned"]}))\'\n')
        result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"language": language, "code": code}, context=context)
        public = result.outcome.public_content
        assert public["ok"] and public["data"]["exit_code"] == 0, public
        observed = json.loads(public["data"]["stdout"])
        assert observed == {"instance_id": target, "returned": 1}
        root, _ = sandbox._workspace_location(context.owner, context.session_id)
        # 展示stdout会二次脱敏；完整事实以SDK归档及工作区文件核验。
        facts = json.loads((root / "work/failures.json").read_text())
        row, = facts["data"]["items"]
        assert row["instance_id"] == target and "tail-fact" in row["error"] and len(row["error"]) > 600
        assert type(row["session_id"]) is int
        assert "PRIVATE_KEY" not in json.dumps(facts)
        call = public["data"]["sdk_calls"][-1]
        archived = json.loads((root / call["response_path"].lstrip("/")).read_text())
        assert archived["result"] == facts
        backend.clear.assert_not_called()
        code = ('import json\nfrom mf import playback\n'
                'instance = json.load(open("/work/instances.json"))\n'
                'playback.restart(instance["instance_id"])\n') if language == "python" else (
                    'mf playback.restart --instance-id "$(cat /work/id.txt)"\n')
        proposal = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"language": language, "code": code}, context=context)
        plan = proposal.effect_plan
        assert plan is not None and plan.arguments == {"instance_id": target}
        backend.clear.assert_not_called()
        confirmed = await executor().execute_confirmed(plan.plan_id, context=context)
        assert confirmed.outcome.public_content["data"]["restarted"] is True
        backend.clear.assert_called_once_with(target)
        sandbox.clear_workspace(owner=context.owner, session_id=context.session_id)
    asyncio.run(check())
