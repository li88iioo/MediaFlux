from __future__ import annotations

from app.agent.models import ToolContext
from app.agent.tools import build_tool_registry


def test_provider_gateway_tools_are_registered_as_read_only():
    registry = build_tool_registry()
    names = {item["name"] for item in registry.capabilities()}
    assert "provider.capabilities" in names
    assert "provider.query" in names
    assert registry.risk_for("provider.capabilities").value == "read"
    assert registry.risk_for("provider.query").value == "read"


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
