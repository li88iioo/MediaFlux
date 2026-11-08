"""连接SDK的原生工作区、CLI和完整结果回读；HTTP替身，无真实服务器。"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import requests

from app import sdk
from app.agent.kernel.bootstrap import build_tool_catalog
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.database import media_owner_digest
from app.modules import media_server_profiles as profiles
from app.modules import workspace as health_actions
from app.repositories.media_experience import set_media_preferences
from tests.support import isolated_test_database
from tests.test_agent_config_connection import (
    connection_backend as connection_backend,  # noqa: PLC0414 - shared HTTP boundary
)


def test_connection_has_one_native_entry():
    assert "library.connection" in sdk.METHODS and "library.connection" not in sdk.WRITE_METHODS
    tools = {tool.name for tool in build_tool_catalog().visible()}
    for name in ("config.test_media_server", "config.diagnose_media_servers"):
        assert name not in tools and name.replace(".", "__") not in tools
    assert {"media.refresh", "library.episodes", "library.search"} <= sdk.METHODS.keys()
    assert importlib.util.find_spec("app.agent.domains.configuration.connection_actions") is None
    assert importlib.util.find_spec("app.agent.domains.system.media_server_actions") is None


@pytest.mark.parametrize("busy", [False, True])
def test_workspace_health_reads_public_connection_without_losing_unknown_counts(connection_backend, monkeypatch, busy):
    monkeypatch.setattr(health_actions, "_backlog", lambda: {"ok": True, "status": "empty", "data": {"areas": [], "active_total": 0, "waiting_total": 0}})
    monkeypatch.setattr(health_actions.diagnostics, "configuration", lambda: {"ok": True, "status": "healthy", "data": {}})
    monkeypatch.setattr(health_actions, "source_readiness", lambda **_: {"status": "ready", "counts": {}})
    if busy:
        limiter = SimpleNamespace(acquire=lambda **_: False)
        monkeypatch.setattr(profiles, "_CONNECTION_LIMIT", limiter)
    result = health_actions.overview(True)
    area = result["data"]["areas"][-1]
    assert area["next_method"] == "library.connection"
    if busy:
        assert result["status"] == "partial" and area["status"] == "unavailable"
        assert area["online_count"] is None and area["compatible_count"] is None
        assert "media_server_offline" not in area["reason_codes"]
    else:
        assert result["status"] == "healthy" and area["online_count"] == area["compatible_count"] == 1
    assert "jellyfin-secret-token" not in json.dumps(result)


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="需要可运行 Bubblewrap 的 Linux 隔离工作区")
def test_native_connection_api_cli_recovers_errors_and_saves_full_identity(connection_backend, monkeypatch, tmp_path):
    _, payloads, calls = connection_backend
    original = profiles.requests.get
    count = 0
    def get(url, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            saved = payloads["jellyfin"]
            failed = requests.Response()
            failed.status_code = 503
            failed.url = "http://private.internal/?token=secret"
            failed._content = b"secret upstream failure"
            payloads["jellyfin"] = failed
            try:
                return original(url, **kwargs)
            finally:
                payloads["jellyfin"] = saved
        return original(url, **kwargs)
    monkeypatch.setattr(profiles.requests, "get", get)
    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))
    owner, session = "connection-native-owner", "connection-native-session"

    async def run():
        store = InMemorySessionStateStore()
        lease, _ = await store.begin_turn(owner=owner, session_id=session, request_id="connection")
        context = ToolCallContext(owner=owner, session_id=session, request_id=lease.request_id,
            turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=AsyncMock())
        pipeline = ToolPipeline(catalog=ToolCatalog(), state_store=store)
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": '''import json, subprocess
from mf import library
try:
    library.connection(server="plex")
except RuntimeError:
    print("修正参数")
failed=library.connection(server="jellyfin")
assert failed["ok"] is False and failed["status"]=="http_error"
assert len(failed["data"]["nodes"])==1 and failed["data"]["nodes"][0]["error"]
single=library.connection(server="jellyfin")
both=json.loads(subprocess.check_output(["mf","library.connection","--server","all"]))
json.dump({"failed":failed,"single":single,"both":both},open("connection.json","w"))
print(single["data"]["nodes"][0]["server_name"],both["status"])
'''}, context=context)
        data = result.outcome.public_content["data"]
        assert data["exit_code"] == 0, data
        assert data["stdout"].strip().splitlines() == ["修正参数", "Living Room healthy"]
        assert [(call["ok"], call["status"]) for call in data["sdk_calls"]] == [
            (False, "ValueError"), (False, "http_error"), (True, "healthy"), (True, "healthy")]
        assert result.effect_plan is None
        saved = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": '''import json
r=json.load(open("connection.json"))
n=r["single"]["data"]["nodes"][0]
assert n["product_name"]=="Jellyfin Server" and n["product"]=="jellyfin"
assert n["version"]=="12.0.1" and n["major_version"]==12 and n["compatibility"]=="compatible"
assert n["product_detected"] and n["reason_code"]=="jellyfin12_slot_compatible"
assert r["both"]["data"]["server"]=="all" and len(r["both"]["data"]["nodes"])==2
assert r["both"]["data"]["nodes"][1]["connection_status"]=="disabled"
assert "secret" not in json.dumps(r) and "private.internal" not in json.dumps(r)
print(n["server_name"],r["failed"]["status"])
'''}, context=context)
        assert saved.outcome.public_content["data"]["exit_code"] == 0, saved.outcome.public_content
        assert saved.outcome.public_content["data"]["sdk_calls"] == []
    try:
        with isolated_test_database():
            set_media_preferences(media_owner_digest(owner), expected_revision=0, updates={"preferred_server": "emby"})
            asyncio.run(run())
    finally:
        sandbox.clear_workspace(owner=owner, session_id=session)
    assert len(calls) == 3 and all("emby.internal" not in url for url, _ in calls)
