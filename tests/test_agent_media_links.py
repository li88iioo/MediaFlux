from __future__ import annotations

from app import sdk, services
from app.agent.kernel.projection import DefaultProjector
from app.agent.models import ToolResult
from app.bot.telegram_markdown import render_telegram_markdown
from app.clients.base import MediaItem
from app.modules import media_links
from app.modules.media_server_profiles import MediaServerProfile
from app.modules.workspace_search import _library_section
from tests.test_agent_task_entry import isolated_db as isolated_db


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


def _open_url(item_url: str) -> str:
    profile = _profile()
    return media_links.media_open_url_resolver(
        server_type=profile.server_type, server_url=profile.url,
    )(item_url)


def test_media_link_falls_back_to_trusted_server_url(monkeypatch):
    _patch_proxy_state(monkeypatch, rows=[], runtime={}, public_base="")
    direct = "http://media.local:8096/web/index.html#!/details?id=item-1"

    assert _open_url(direct) == direct
    assert (
        _open_url("http://other.local:8096/web/index.html#!/details?id=item-1",
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

    result = _open_url("http://media.local:8096/web/index.html#!/details?id=item-1",
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

    result = _open_url("http://media.local:8096/web/index.html#!/details?id=item-1",
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

    assert _open_url(direct) == direct


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

    assert _open_url(direct) == direct


def test_library_search_includes_verified_open_url(monkeypatch):
    direct = "http://media.local:8096/web/index.html#!/details?id=item-1"
    monkeypatch.setattr(
        services,
        "_search_sources",
        lambda _query, _limit, **_options: [
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

    result = sdk.library.search("黄泉使者", limit=8)

    assert result["data"]["sources"][0]["items"][0]["open_url"] == direct


def test_workspace_library_result_includes_verified_open_url(monkeypatch):
    direct = "http://media.local:8096/web/index.html#!/details?id=item-1"
    monkeypatch.setattr(
        "app.services._search_sources",
        lambda _query, limit, **_kwargs: [
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

    section, network_accessed = _library_section("黄泉使者")

    assert network_accessed is True
    assert section["items"][0]["open_url"] == direct




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




def test_media_link_does_not_match_a_different_case_sensitive_base_path(monkeypatch):
    row = {
        "id": 7, "enabled": 1, "config_source": "custom", "server_type": "jellyfin",
        "upstream_url": "http://media.local:8096/Media", "listen_port": 18096,
    }
    _patch_proxy_state(
        monkeypatch, rows=[row], runtime={7: {"running": True, "listen_port": 18096}},
        public_base="http://192.168.0.195:1258",
    )
    resolve = media_links.media_open_url_resolver(
        server_type="jellyfin", server_url="http://media.local:8096/media"
    )
    direct = "http://media.local:8096/media/web/index.html#!/details?id=item-1"
    assert resolve(direct) == direct
