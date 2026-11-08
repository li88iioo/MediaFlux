"""持久确认在真实配置发布和独立冷进程中保留相同的授权边界。"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import config, database as db
from app.agent.kernel.bootstrap import get_agent_kernel_runtime, invalidate_agent_kernel_runtime
from app.agent.kernel.model import ModelEvent, ModelEventType
from app.agent.kernel.effects import InMemoryRateLimiter
from app.clients.qbittorrent import TorrentAddResult
from tests.support import initialized_web_runtime
from tests.test_agent_kernel_core import ScriptedModel
from tests.test_agent_kernel_existing_ports import context_for

OWNER = "webk:v1:" + "a" * 64
OLD = {"QB_URL": "http://old-qb.invalid", "TG_QB_SAVE_PATH": "/old-target", "TG_QB_CATEGORY": "old"}
NEW = {"QB_URL": "http://new-qb.invalid", "TG_QB_SAVE_PATH": "/new-target", "TG_QB_CATEGORY": "new"}


class RecordingQB:
    calls: list[dict] = []

    def __init__(self, **kwargs):
        self.url = kwargs["url"]

    def add_torrent_detailed(self, **kwargs):
        self.calls.append({"url": self.url, "path": kwargs["save_path"], "category": kwargs["category"]})
        return TorrentAddResult(ok=True)

    def close(self):
        pass


def runtime():
    current = get_agent_kernel_runtime()
    current.session.pipeline.rate_limiter = InMemoryRateLimiter(limit=100)
    current.session.model = ScriptedModel([[ModelEvent(ModelEventType.FINISH)]])
    return current


async def prepare(current, session_id):
    context = await context_for(current.store, owner=OWNER, session=session_id)
    result = await current.session.pipeline.prepare(
        "downloads.submit", {"url": "magnet:?xt=urn:btih:" + "b" * 40, "target": "qb"}, context=context,
    )
    assert result.effect_plan is not None
    return result.effect_plan


async def confirm(current, session_id, plan_id):
    return [event.to_dict() async for event in current.session.confirm(owner=OWNER, session_id=session_id, plan_id=plan_id)]


def save(values):
    from app.routes import api
    from starlette.requests import Request
    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(background_services_enabled=False))})
    with patch.object(api, "require_api_login"):
        assert api.save_config(request, values) == {"success": True}


def test_confirmed_restart_does_not_block_concurrent_web_runtime_lookup():
    from starlette.requests import Request

    from app.modules import media_proxy
    from app.modules.media_proxy import MediaProxyManager
    from app.routes import agent_api

    async def scenario():
        with initialized_web_runtime():
            config.set_and_save({"AGENT_ENABLED": "1", "AGENT_LLM_API_URL": "http://model.invalid/v1", "AGENT_LLM_MODEL": "test-model"})
            invalidate_agent_kernel_runtime()
            current = await asyncio.to_thread(get_agent_kernel_runtime)
            instance_id = db.add_media_proxy_instance(
                name="test", server_type="jellyfin", upstream_url="http://proxy.invalid",
                api_key="test-key", listen_host="127.0.0.1", listen_port=19001,
                local_root="/media", enabled=1,
            )
            loop = asyncio.get_running_loop()
            # 模拟繁忙的业务执行器：确认占用一条线程，反代还需在线程读取实例。
            loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
            manager = MediaProxyManager()
            manager._loop = loop
            restart_started = asyncio.Event()
            lookup_started = asyncio.Event()
            restarted = []

            async def restart(selected, **_):
                restart_started.set()
                await lookup_started.wait()
                assert await asyncio.to_thread(db.get_media_proxy_instance, selected) is not None
                restarted.append(selected)
                return {"restarted": True}

            def lookup():
                loop.call_soon_threadsafe(lookup_started.set)
                return get_agent_kernel_runtime()

            context = await context_for(current.store, owner=OWNER, session="proxy-restart")
            args = {"instance_id": instance_id}
            prepared = await current.session.pipeline.prepare("playback.restart", args, context=context)
            assert prepared.effect_plan is not None
            try:
                with (
                    patch.object(media_proxy, "get_media_proxy_manager", return_value=manager),
                    patch.object(media_proxy, "_PROXY_RESTART_WAIT_SECONDS", 1.0),
                    patch.object(manager, "restart_instance", side_effect=restart),
                    patch.object(agent_api, "get_agent_kernel_runtime", side_effect=lookup),
                    patch.object(agent_api, "require_api_login"),
                    patch.object(agent_api, "_owner", return_value=OWNER),
                    patch.object(agent_api, "_draft_scope", return_value="test-scope"),
                ):
                    execution = asyncio.create_task(current.session.pipeline.execute_confirmed(
                        prepared.effect_plan.plan_id, context=context,
                    ))
                    await asyncio.wait_for(restart_started.wait(), timeout=5)
                    response = await agent_api.list_sessions(Request({"type": "http"}))
                    result = await execution
                assert response.status_code == 200
                assert result.outcome.public_content["status"] == "completed"
                assert restarted == [instance_id]
            finally:
                invalidate_agent_kernel_runtime()

    asyncio.run(scenario())


def cold_confirm(paths, plan_id):
    env = {key: value for key, value in os.environ.items() if key not in config.all_items()}
    env.update({
        **config._SYSTEM_ENV_SNAPSHOT,
        "PYTHONPATH": os.getcwd(),
        "MEDIAFLUX_TEST_MODE": "0", "MEDIAFLUX_TEST_DB_PATH": "",
        **{f"MEDIAFLUX_{name.upper()}_DIR": str(getattr(paths, f"{name}_dir"))
           for name in ("data", "config", "cache", "log", "strm")},
    })
    result = subprocess.run(
        [sys.executable, __file__, plan_id],
        env=env, input=json.dumps(config._SYSTEM_ENV_SNAPSHOT), capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("change", ["unchanged", "changed", "aba"])
def test_confirmed_destination_survives_restart_only_when_authority_is_unchanged(change):
    async def scenario():
        with initialized_web_runtime() as paths, patch("app.agent.feature_gate._runtime_generation", 0):
            config.set_and_save({**OLD, "AGENT_LLM_API_URL": "http://model.invalid/v1", "AGENT_LLM_MODEL": "test-model"})
            invalidate_agent_kernel_runtime()
            try:
                # 非零本地代次下的卡也必须在同配置重启后有效。
                save(NEW)
                plan = await prepare(runtime(), "cold-confirm")
                if change != "unchanged":
                    save(OLD)
                if change == "aba":
                    save(NEW)
                result = cold_confirm(paths, plan.plan_id)
                completed = [event for event in result["events"] if event["type"] == "effect.completed"]
                if change == "unchanged":
                    assert len(completed) == 1
                    assert result["calls"] == [{"url": NEW["QB_URL"], "path": NEW["TG_QB_SAVE_PATH"], "category": NEW["TG_QB_CATEGORY"]}]
                else:
                    assert not completed
                    assert result["calls"] == []
                    assert any(event["payload"].get("code") == "confirmation_stale" for event in result["events"])
            finally:
                invalidate_agent_kernel_runtime()
    asyncio.run(scenario())


def test_zero_generation_old_confirmation_is_rejected_after_configuration_change_and_restart():
    async def scenario():
        with initialized_web_runtime() as paths, patch("app.agent.feature_gate._runtime_generation", 0):
            config.set_and_save({**OLD, "AGENT_LLM_API_URL": "http://model.invalid/v1", "AGENT_LLM_MODEL": "test-model"})
            invalidate_agent_kernel_runtime()
            try:
                plan = await prepare(runtime(), "cold-confirm")
                save(NEW)
                result = cold_confirm(paths, plan.plan_id)
                assert result["calls"] == []
                assert any(event["payload"].get("code") == "confirmation_stale" for event in result["events"])
            finally:
                invalidate_agent_kernel_runtime()
    asyncio.run(scenario())


@pytest.mark.parametrize("tool,updates,arguments", [
    ("library.create_path_mapping", {"JELLYFIN_PATH_MAPPINGS": "[]"},
     {"server": "jellyfin", "local_path": "/media/strm/movies", "server_path": "/mnt/media/movies"}),
    ("library.set_patrol_policy", {"AGENT_LIBRARY_PATROL_ENABLED": "0"}, {"enabled": True}),
    ("strm.set_schedule", {"STRM_SCHEDULE_ENABLED": "0"}, {"enabled": True}),
    ("cloud.set_organize_schedule", {"GY_ORGANIZE_SCHEDULE_ENABLED": "0"}, {"enabled": True}),
])
def test_configuration_writer_verifies_and_reloads_its_committed_snapshot(tool, updates, arguments):
    from app.modules.agent_library_patrol_scheduler import AgentLibraryPatrolScheduler

    async def scenario():
        with initialized_web_runtime():
            config.set_and_save({**updates, "AGENT_LLM_API_URL": "http://model.invalid/v1", "AGENT_LLM_MODEL": "test-model"})
            invalidate_agent_kernel_runtime()
            try:
                current = runtime()
                context = await context_for(current.store, owner=OWNER, session="config-writer")
                preview = await current.session.pipeline.execute(tool, arguments, context=context)
                assert preview.effect_plan is not None
                invalidate_agent_kernel_runtime()
                current = runtime()  # 从持久确认账本恢复；不能依赖原预检对象或原进程缓存。
                scheduler = AgentLibraryPatrolScheduler()
                with patch("app.modules.agent_library_patrol_scheduler.get_agent_library_patrol_scheduler", return_value=scheduler):
                    events = await confirm(current, "config-writer", preview.effect_plan.plan_id)
                result = next(event["payload"]["result"] for event in events if event["type"] == "effect.completed")
                assert result["ok"] and result["status"] == "completed"
                values = config.read_env_snapshot(config.ENV_FILE)[1]
                if tool in {"library.set_patrol_policy", "strm.set_schedule", "cloud.set_organize_schedule"}:
                    assert values[next(iter(updates))] == "1"
                    assert result["data"]["policy"]["enabled"] is True
                    assert result["data"]["runtime_refreshed"] is True
                else:
                    assert len(json.loads(values["JELLYFIN_PATH_MAPPINGS"])) == 1
                    assert result["data"]["count"] == 1
            finally:
                invalidate_agent_kernel_runtime()
    asyncio.run(scenario())


async def child(plan_id):
    # tests 包的默认隔离不能把子进程重定向到另一安装的数据库。
    os.environ["MEDIAFLUX_TEST_MODE"] = "0"
    os.environ["MEDIAFLUX_TEST_DB_PATH"] = ""
    db.configure_database(config.PATHS.database_path, test_mode=False)
    # 安装夹具改了 PATHS，但父进程的启动覆盖仍来自测试包初始环境；
    # 传递同一份部署覆盖，避免临时目录差异伪装成配置变化。
    startup_overrides = json.loads(sys.stdin.read())
    with patch.object(config, "_SYSTEM_ENV_SNAPSHOT", startup_overrides), patch("app.modules.download_dispatcher.QBittorrentClient", RecordingQB), patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
        events = await confirm(runtime(), "cold-confirm", plan_id)
        print(json.dumps({"events": events, "calls": RecordingQB.calls}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(child(sys.argv[1]))
