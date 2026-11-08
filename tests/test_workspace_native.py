"""工作区公共服务及稳定SDK的离线契约；媒体连接仅使用替身。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import sdk
from app.agent.kernel.bootstrap import build_tool_catalog
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.agent.kernel.ux_display import next_actions_view
from app.modules import workspace
from tests.support import isolated_test_database


@pytest.fixture
def local_workspace(monkeypatch):
    with isolated_test_database():
        monkeypatch.setattr(
            workspace,
            "source_readiness",
            lambda **_: {
                "ok": True,
                "status": "ready",
                "counts": {"enabled": 2, "searchable": 2, "attention": 0},
            },
        )
        monkeypatch.setattr(
            workspace.diagnostics,
            "configuration",
            lambda: {
                "ok": True,
                "status": "healthy",
                "data": {
                    "counts": {"errors": 0, "warnings": 0},
                    "components": [
                        {
                            "component": "jellyfin",
                            "status": "ready",
                            "missing_field_labels": [],
                        }
                    ],
                },
            },
        )
        yield


def test_sdk_reads_are_native_only_and_old_actions_physically_removed():
    names = {spec.name for spec in build_tool_catalog().visible()}
    assert {"diagnostics.overview", "diagnostics.search"} <= sdk.METHODS.keys()
    assert {"diagnostics.overview", "diagnostics.search"}.isdisjoint(
        sdk.WRITE_METHODS | names
    )
    assert not any(name.startswith("workspace.") for name in names)
    for module in (
        "actions",
        "briefing_actions",
        "health_actions",
        "todo_actions",
        "next_actions",
        "tools",
    ):
        # 父包已经一起移除；不再靠空注册函数维持旧进口。
        try:
            spec = importlib.util.find_spec("app.agent.domains.workspace." + module)
        except ModuleNotFoundError:
            spec = None
        assert spec is None
    for method in (
        sdk.diagnostics.overview,
        lambda: sdk.diagnostics.search("Dune", ["rss"]),
    ):
        with sdk.call_owner("unauthorized"), pytest.raises(PermissionError):
            method()


def test_local_overview_reuses_snapshot_and_never_probes(local_workspace, monkeypatch):
    probe = Mock(side_effect=AssertionError("默认概览不得探测"))
    monkeypatch.setattr(workspace, "connection", probe)
    with sdk.call_owner("webk:v1:offline"):
        result = sdk.diagnostics.overview()
    assert result["status"] == "healthy"
    data = result["data"]
    assert data["network_accessed"] is False and not data["content_filesystem_scanned"]
    assert (
        data["sources"]["configuration"]["data"]["components"][0]["component"]
        == "jellyfin"
    )
    assert "media_server_connectivity" in data["coverage"]["not_probed"]
    assert next_actions_view(data["next_actions"]) == {
        "actions": [],
        "snapshot_status": "empty",
    }
    assert "不能证明" in data["scope"]
    probe.assert_not_called()


def test_partial_sources_keep_data_actions_and_unknown_connection_counts(
    local_workspace, monkeypatch
):
    monkeypatch.setattr(
        workspace.db,
        "get_dashboard_automation_summary",
        lambda: {"downloads_review": 2},
    )
    monkeypatch.setattr(
        workspace.db,
        "get_local_media_diagnostic_summary",
        Mock(side_effect=RuntimeError("token=PRIVATE")),
    )
    monkeypatch.setattr(
        workspace,
        "connection",
        lambda: {
            "ok": False,
            "status": "unavailable",
            "data": {
                "probe_complete": False,
                "network_accessed": False,
                "nodes": [],
                "counts": {
                    "enabled": 1,
                    "configured": 1,
                    "online": None,
                    "compatible": None,
                },
            },
        },
    )
    result = workspace.overview(probe=True)
    assert result["ok"] and result["status"] == "partial"
    data = result["data"]
    assert set(data["coverage"]["unavailable"]) == {"local_media", "media_servers"}
    assert data["next_actions"]["status"] == "partial"
    assert (
        next_actions_view(data["next_actions"])["actions"][0]["id"]
        == "review_downloads"
    )
    media = next(area for area in data["areas"] if area["source"] == "media_servers")
    assert media["online_count"] is None and media["compatible_count"] is None
    assert "media_server_offline" not in media["reason_codes"]
    assert "PRIVATE" not in json.dumps(result)


def test_probe_preserves_full_connection_payload_and_offline_is_attention(
    local_workspace, monkeypatch
):
    payload = {
        "ok": False,
        "status": "unavailable",
        "data": {
            "network_accessed": True,
            "probe_complete": True,
            "counts": {
                "enabled": 1,
                "configured": 1,
                "online": 0,
                "compatible": 0,
                "attention": 1,
            },
            "nodes": [
                {
                    "server_type": "jellyfin",
                    "status": "offline",
                    "reason_code": "timeout",
                }
            ],
        },
    }
    probe = Mock(return_value=payload)
    monkeypatch.setattr(workspace, "connection", probe)
    result = workspace.overview(True)
    assert (
        result["status"] == "attention" and result["data"]["network_accessed"] is True
    )
    assert result["data"]["sources"]["media_servers"] is payload
    assert "media_servers" not in result["data"]["coverage"]["unavailable"]
    media = result["data"]["areas"][-1]
    assert media["reason_codes"] == ["media_server_offline"]
    probe.assert_called_once_with()


def test_all_failed_sources_are_unavailable_and_no_healthy_claim(
    local_workspace, monkeypatch
):
    for name in (
        "get_dashboard_automation_summary",
        "get_local_media_diagnostic_summary",
        "get_agent_persistent_health_summary",
    ):
        monkeypatch.setattr(
            workspace.db, name, Mock(side_effect=RuntimeError("unavailable"))
        )
    monkeypatch.setattr(workspace, "source_readiness", Mock(side_effect=RuntimeError))
    monkeypatch.setattr(
        workspace.diagnostics, "configuration", Mock(side_effect=RuntimeError)
    )
    monkeypatch.setattr(workspace, "connection", Mock(side_effect=RuntimeError))
    result = workspace.overview(True)
    assert result["ok"] is False and result["status"] == "unavailable"
    assert not result["data"]["coverage"]["available"]
    assert next_actions_view(result["data"]["next_actions"]) == {
        "actions": [],
        "snapshot_status": "unavailable",
    }


def test_rss_failure_and_disabled_indexers_remain_explicit(
    local_workspace, monkeypatch
):
    monkeypatch.setattr(
        workspace, "diagnose_rss", Mock(side_effect=RuntimeError("token=PRIVATE"))
    )
    monkeypatch.setattr(
        workspace, "source_readiness", lambda **_: {"status": "disabled", "counts": {}}
    )
    result = workspace.overview()
    assert result["status"] == "partial"
    assert result["data"]["coverage"]["unavailable"] == ["rss"]
    assert result["data"]["coverage"]["disabled"] == ["indexers"]
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("value", [None, 0, 1, "false"])
def test_probe_requires_boolean_before_any_read(value, monkeypatch):
    monkeypatch.setattr(
        workspace, "_backlog", lambda: pytest.fail("invalid probe read DB")
    )
    with pytest.raises(ValueError):
        workspace.overview(value)


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="需要Linux原生隔离工作区")
@pytest.mark.parametrize("owner", ["webk:v1:native", "tg:v1:native"])
def test_native_rpc_cli_recovery_pagination_and_full_file_readback(
    local_workspace, monkeypatch, tmp_path, owner
):
    from app import database as db

    with db.get_conn() as conn:
        stamp = db.now()
        subscription = conn.execute(
            "INSERT INTO rss_items(name,urls,created_at,updated_at) VALUES(?,?,?,?)",
            ("local", "https://example.invalid/offline", stamp, stamp),
        ).lastrowid
        expected = [
            conn.execute(
                "INSERT INTO rss_entries(rss_item_id,title,status,created_at) VALUES(?,?,?,?)",
                (subscription, f"Dune {i}", "pending", stamp),
            ).lastrowid
            for i in range(13)
        ]
    monkeypatch.setattr(
        sandbox,
        "get_runtime_paths",
        lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"),
    )
    session = "workspace-native"

    async def run():
        store = InMemorySessionStateStore()
        lease, _ = await store.begin_turn(
            owner=owner, session_id=session, request_id="native"
        )
        context = ToolCallContext(
            owner=owner,
            session_id=session,
            request_id=lease.request_id,
            turn_id=lease.turn_id,
            lease=lease,
            cancellation=CancellationToken(),
            report_progress=AsyncMock(),
        )
        pipeline = ToolPipeline(catalog=ToolCatalog(), state_store=store)
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        code = """import json, subprocess
from mf import diagnostics, Error
try:
    diagnostics.search("Dune", sections=["rss"], limit=0)
except Error as exc:
    assert exc.code=="ValueError"
    print("recovered")
result=diagnostics.overview()
assert result["data"]["network_accessed"] is False
pages=[]
offset=0
while True:
    page=json.loads(subprocess.check_output(["mf","diagnostics.search","--query","Dune","--sections","rss","--limit","5","--offset",str(offset)]))
    pages.append(page)
    if not page["data"]["has_more"]: break
    offset=page["data"]["next_offset"]
json.dump({"overview":result,"pages":pages},open("workspace.json","w"))
print(sum(p["data"]["returned"] for p in pages))
"""
        result = await pipeline.execute(
            sandbox.RUN_CODE_TOOL, {"code": code}, context=context
        )
        data = result.outcome.public_content["data"]
        assert data["exit_code"] == 0, data
        assert data["stdout"].splitlines() == ["recovered", "13"]
        assert [call["method"] for call in data["sdk_calls"]] == [
            "diagnostics.search",
            "diagnostics.overview",
            "diagnostics.search",
            "diagnostics.search",
            "diagnostics.search",
        ]
        saved = await pipeline.execute(
            sandbox.RUN_CODE_TOOL,
            {
                "code": """import json
r=json.load(open("workspace.json"))
ids=[item["id"] for page in r["pages"] for section in page["data"]["sections"] for item in section["items"]]
assert len(ids)==len(set(ids))==13
assert ids==sorted(ids,reverse=True)
assert all(s["rss_item_id"]>0 for page in r["pages"] for section in page["data"]["sections"] for s in section["items"])
assert r["overview"]["data"]["sources"]["configuration"]["data"]["components"][0]["component"]=="jellyfin"
print(ids)
"""
            },
            context=context,
        )
        assert saved.outcome.public_content["data"]["exit_code"] == 0, (
            saved.outcome.public_content
        )
        assert json.loads(saved.outcome.public_content["data"]["stdout"]) == list(
            reversed(expected)
        )
        assert saved.outcome.public_content["data"]["sdk_calls"] == []
        # 初始只读接力的模型即使继续用代码，也不能通过SDK准备写卡或修改技能。
        effects = Mock(side_effect=AssertionError("read handoff reached effects"))
        monkeypatch.setattr(pipeline, "prepare", effects)
        denied = await pipeline.execute(
            sandbox.RUN_CODE_TOOL,
            {
                "code": """from mf import settings, skills, Error
for method,args in [(settings.test_notification,{}),(skills.save,dict(name="blocked",description="blocked",content="blocked"))]:
    try: method(**args)
    except Error as exc: assert exc.code=="read_scope_required"
    else: raise AssertionError("write was allowed")
print("read-only")
"""
            },
            context=replace(context, read_only=True),
        )
        assert denied.outcome.public_content["data"]["exit_code"] == 0, (
            denied.outcome.public_content
        )
        effects.assert_not_called()
        assert denied.effect_plan is None

    try:
        asyncio.run(run())
    finally:
        sandbox.clear_workspace(owner=owner, session_id=session)
