from __future__ import annotations

import pytest

from app.agent.models import ToolContext
from app.agent.provider_actions import provider_capabilities_arguments
from app.agent.registry import AgentToolError
from app.agent.tools import build_tool_registry


def test_provider_gateway_tools_expose_reads_and_confirmation_gated_writes():
    registry = build_tool_registry()
    capabilities = {item["name"]: item for item in registry.capabilities()}
    assert "provider.capabilities" in capabilities
    assert "provider.query" in capabilities
    assert "provider.change.preview" in capabilities
    assert "provider.change.execute" in capabilities
    assert "provider.job.status" in capabilities
    assert registry.risk_for("provider.capabilities").value == "read"
    assert registry.risk_for("provider.query").value == "read"
    assert registry.risk_for("provider.change.preview").value == "read"
    assert registry.risk_for("provider.job.status").value == "read"
    assert registry.risk_for("provider.change.execute").value == "write"
    assert capabilities["provider.change.execute"]["requires_confirmation"] is True


def test_provider_capabilities_returns_only_static_non_sensitive_state(monkeypatch):
    monkeypatch.setattr(
        "app.agent.providers.media_server.list_configured_profiles",
        list,
    )
    monkeypatch.setattr(
        "app.agent.providers.qbittorrent.QBittorrentProviderTransport._settings",
        lambda _self: {"QB_URL": "", "QB_USERNAME": "", "QB_PASSWORD": "", "QB_API_KEY": ""},
    )
    registry = build_tool_registry()
    result, _elapsed = registry.execute(
        "provider.capabilities",
        {"provider": "media", "intent": "媒体库", "limit": 8},
        context=ToolContext(owner="owner", session_id="session"),
    )
    assert result.ok
    assert result.data["operations"]
    assert result.data["rules"]["arbitrary_http_allowed"] is False


def test_provider_capability_projection_keeps_only_safe_operation_contract(monkeypatch):
    from app.agent.result_projection import project_agent_response_for_llm

    monkeypatch.setattr(
        "app.agent.providers.media_server.list_configured_profiles",
        list,
    )
    registry = build_tool_registry()
    result, _elapsed = registry.execute(
        "provider.capabilities",
        {"provider": "media", "intent": "媒体库", "limit": 8},
        context=ToolContext(owner="owner", session_id="session"),
    )
    projected = project_agent_response_for_llm({
        "tool_call": {"name": "provider.capabilities", "arguments": {}},
        "result": result.to_dict(),
    })
    assert projected is not None
    serialized = repr(projected)
    assert "media.libraries.list" in serialized
    assert "输入字段" in serialized
    assert "query:string(required)" in serialized
    assert "Token" not in serialized
    assert "http://" not in serialized


def test_provider_plan_projection_keeps_opaque_plan_ref_and_drops_internal_ids():
    from app.agent.result_projection import project_agent_response_for_llm

    projected = project_agent_response_for_llm({
        "tool_call": {"name": "provider.change.preview", "arguments": {}},
        "result": {
            "ok": True,
            "status": "preview",
            "summary": "计划已创建",
            "data": {
                "plan_ref": "PP-0123456789ABCDEF01234567",
                "operation": "qb.torrents.pause",
                "status": "prepared",
                "target_snapshot": {
                    "targets": [{"name": "Demo", "hash": "a" * 40}],
                    "delete_files": False,
                },
            },
            "evidence": [],
            "suggestions": [],
            "error": "",
        },
    })
    serialized = repr(projected)
    assert "PP-0123456789ABCDEF01234567" in serialized
    assert "qb.torrents.pause" in serialized
    assert "a" * 40 not in serialized

def test_provider_capability_limit_rejects_lossy_integer_values():
    for invalid in (True, 1.0, 1.9, "1.0", "1e3"):
        with pytest.raises(AgentToolError):
            provider_capabilities_arguments({"limit": invalid})
    assert provider_capabilities_arguments({"limit": "8"})["limit"] == 8
