from __future__ import annotations

from app.agent import media_links
from app.agent.domain_catalog import library_search
from app.agent.kernel.projection import DefaultProjector
from app.agent.kernel.session import DEFAULT_SYSTEM_PROMPT
from app.agent.models import ToolResult
from app.agent.provider_artifacts import ProviderArtifactStore
from app.agent.workspace_actions import _library_section
from app.bot.telegram_markdown import render_telegram_markdown
from app.clients.base import MediaItem
from app.modules.media_server_profiles import MediaServerProfile


class _ProxyManager:
    def __init__(self, status):
        self._status = status

    def status(self):
        return self._status


def _profile() -> MediaServerProfile:
    return MediaServerProfile(
        source="configured:jellyfin",
        server_type="jellyfin",
        label="Jellyfin",
        url="http://media.local:8096",
        credential="secret",
        enabled=True,
    )


def _patch_proxy_state(monkeypatch, *, rows, runtime, public_base):
    monkeypatch.setattr(
        media_links.config,
        "get",
        lambda key, default="": public_base if key == "GY_STRM_BASE_URL" else default,
    )
    monkeypatch.setattr(
        "app.repositories.media_proxy.list_media_proxy_instances",
        lambda: rows,
    )
    monkeypatch.setattr(
        "app.modules.media_proxy.get_media_proxy_manager",
        lambda: _ProxyManager(runtime),
    )


def test_media_link_falls_back_to_trusted_server_url(monkeypatch):
    _patch_proxy_state(monkeypatch, rows=[], runtime={}, public_base="")
    direct = "http://media.local:8096/web/index.html#!/details?id=item-1"

    assert media_links.resolve_media_open_url(_profile(), direct) == direct
    assert (
        media_links.resolve_media_open_url(
            _profile(),
            "http://other.local:8096/web/index.html#!/details?id=item-1",
        )
        == ""
    )


def test_media_link_prefers_running_matching_proxy(monkeypatch):
    row = {
        "id": 7,
        "enabled": 1,
        "config_source": "custom",
        "server_type": "jellyfin",
        "upstream_url": "http://media.local:8096",
        "listen_port": 18096,
    }
    _patch_proxy_state(
        monkeypatch,
        rows=[row],
        runtime={
            7: {
                "running": True,
                "listen_host": "0.0.0.0",
                "listen_port": 18096,
            }
        },
        public_base="http://192.168.0.195:1258",
    )

    result = media_links.resolve_media_open_url(
        _profile(),
        "http://media.local:8096/web/index.html#!/details?id=item-1",
    )

    assert result == "http://192.168.0.195:18096/web/index.html#!/details?id=item-1"


def test_media_link_uses_explicit_proxy_host_without_public_base(monkeypatch):
    row = {
        "id": 7,
        "enabled": 1,
        "config_source": "custom",
        "server_type": "jellyfin",
        "upstream_url": "http://media.local:8096",
        "listen_host": "192.168.0.195",
        "listen_port": 18096,
    }
    _patch_proxy_state(
        monkeypatch,
        rows=[row],
        runtime={
            7: {
                "running": True,
                "listen_host": "192.168.0.195",
                "listen_port": 18096,
            }
        },
        public_base="",
    )

    result = media_links.resolve_media_open_url(
        _profile(),
        "http://media.local:8096/web/index.html#!/details?id=item-1",
    )

    assert result == "http://192.168.0.195:18096/web/index.html#!/details?id=item-1"


def test_media_link_ignores_stopped_or_mismatched_proxy(monkeypatch):
    rows = [
        {
            "id": 7,
            "enabled": 1,
            "config_source": "custom",
            "server_type": "jellyfin",
            "upstream_url": "http://another.local:8096",
            "listen_port": 18096,
        },
        {
            "id": 8,
            "enabled": 1,
            "config_source": "custom",
            "server_type": "jellyfin",
            "upstream_url": "http://media.local:8096",
            "listen_port": 18097,
        },
    ]
    _patch_proxy_state(
        monkeypatch,
        rows=rows,
        runtime={
            7: {"running": True, "listen_port": 18096},
            8: {"running": False, "listen_port": 18097},
        },
        public_base="http://192.168.0.195:1258",
    )
    direct = "http://media.local:8096/web/index.html#!/details?id=item-1"

    assert media_links.resolve_media_open_url(_profile(), direct) == direct


def test_media_link_rejects_credentials_and_sensitive_query_values():
    assert (
        media_links.sanitize_media_open_url(
            "http://user:password@media.local/web/index.html#!/details?id=item-1"
        )
        == ""
    )
    assert (
        media_links.sanitize_media_open_url(
            "http://media.local/web/index.html?api_key=secret"
        )
        == ""
    )
    assert (
        media_links.sanitize_media_open_url(
            "http://media.local/web/index.html#!/details?token=secret"
        )
        == ""
    )
    assert (
        media_links.sanitize_media_open_url(
            "http://media.local/web/index.html#!/details%3Fapi_key%3Dsecret"
        )
        == ""
    )
    assert media_links.sanitize_media_open_url("http://media.local:99999/web") == ""
    assert (
        media_links.sanitize_media_open_url(
            "http://media.local/web",
            expected_base="http://media.local:99999",
        )
        == ""
    )


def test_media_link_ignores_invalid_proxy_runtime(monkeypatch):
    _patch_proxy_state(
        monkeypatch,
        rows=[
            {
                "id": 7,
                "enabled": 1,
                "config_source": "custom",
                "server_type": "jellyfin",
                "upstream_url": "http://media.local:8096",
                "listen_port": 18096,
            }
        ],
        runtime=None,
        public_base="http://192.168.0.195:1258",
    )
    direct = "http://media.local:8096/web/index.html#!/details?id=item-1"

    assert media_links.resolve_media_open_url(_profile(), direct) == direct


def test_library_search_includes_verified_open_url(monkeypatch):
    direct = "http://media.local:8096/web/index.html#!/details?id=item-1"
    monkeypatch.setattr(
        library_search,
        "_search_sources",
        lambda _query, _limit: [
            {
                "server_type": "jellyfin",
                "web_url": "http://media.local:8096",
                "items": [
                    MediaItem(
                        id="item-1",
                        name="黄泉使者",
                        type="Series",
                        year="2026",
                        web_url=direct,
                    )
                ],
                "error": "",
            }
        ],
    )
    monkeypatch.setattr(
        library_search,
        "media_open_url_resolver",
        lambda **_kwargs: lambda item_url: str(item_url or ""),
    )

    result = library_search.search_library({"query": "黄泉使者", "limit": 8})

    assert result.data["sources"][0]["items"][0]["open_url"] == direct


def test_workspace_library_result_includes_verified_open_url(monkeypatch):
    direct = "http://media.local:8096/web/index.html#!/details?id=item-1"
    monkeypatch.setattr(
        "app.agent.workspace_actions.search_media_servers",
        lambda _query, limit: [
            {
                "server_type": "jellyfin",
                "web_url": "http://media.local:8096",
                "items": [
                    MediaItem(
                        id="item-1",
                        name="黄泉使者",
                        type="Series",
                        year="2026",
                        web_url=direct,
                    )
                ],
                "error": "",
            }
        ],
    )
    monkeypatch.setattr(
        "app.agent.workspace_actions.media_open_url_resolver",
        lambda **_kwargs: lambda item_url: str(item_url or ""),
    )

    section, network_accessed = _library_section("黄泉使者")

    assert network_accessed is True
    assert section["items"][0]["open_url"] == direct


def test_agent_prompt_requires_exact_verified_media_link():
    assert "`open_url`" in DEFAULT_SYSTEM_PROMPT
    assert "[打开媒体库](原样 open_url)" in DEFAULT_SYSTEM_PROMPT


def test_verified_media_link_survives_kernel_and_telegram_projection():
    direct = "http://media.local:8096/web/index.html#!/details?id=item-1"
    outcome = DefaultProjector().project(
        ToolResult(
            True,
            "success",
            "媒体库搜索完成",
            data={"items": [{"name": "黄泉使者", "open_url": direct}]},
        )
    )

    assert outcome.public_content["data"]["items"][0]["open_url"] == direct
    assert direct in outcome.model_content
    assert f'<a href="{direct}">打开媒体库</a>' in render_telegram_markdown(
        f"[打开媒体库]({direct})"
    )


def test_only_media_provider_artifacts_may_publish_open_url():
    direct = "http://media.local:8096/web/index.html#!/details?id=item-1"
    store = ProviderArtifactStore()

    _media_ref, media_data = store.put(
        owner="owner-1",
        session_id="session-1",
        provider="media",
        profile_ref="configured:jellyfin",
        operation="media.items.search",
        data={"items": [{"name": "黄泉使者", "open_url": direct}]},
    )
    _other_ref, other_data = store.put(
        owner="owner-1",
        session_id="session-1",
        provider="qbittorrent",
        profile_ref="configured:qbittorrent",
        operation="download.items.list",
        data={"items": [{"name": "伪造链接", "open_url": direct}]},
    )

    assert media_data["items"][0]["open_url"] == direct
    assert "open_url" not in other_data["items"][0]
