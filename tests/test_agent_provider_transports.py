from __future__ import annotations

from app.agent.providers.media_server import MediaServerProviderTransport
from app.agent.providers.qbittorrent import QBittorrentProviderTransport
from app.clients.base import (
    MediaItem,
    SeriesCandidate,
    SeriesEpisodeInventory,
    SeriesSearchResult,
)
from app.clients.qbittorrent import TorrentFile, TorrentTask, TransferInfo
from app.modules.media_server_profiles import MediaServerProfile


class _FakeMediaClient:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def server_identity(self):
        return "Dev Jellyfin", "Jellyfin", "12.0.0"

    def list_virtual_folders(self):
        return [{"id": "lib-1", "name": "动漫", "collection_type": "tvshows", "paths": ["/secret"]}]

    def search_media(self, query, limit=12):
        assert query == "暗芝居"
        assert limit == 5
        return [MediaItem(id="item-1", name="暗芝居", type="Series", year="2013")]

    def search_series_candidates(self, query, limit=6):
        return SeriesSearchResult(
            candidates=[SeriesCandidate(id="series-1", name=query, year="2013", tmdb_id="56559")],
            total=1,
        )

    def list_series_episode_inventory(self, series_id, **_kwargs):
        assert series_id == "series-1"
        return SeriesEpisodeInventory(episodes=[(1, 1), (17, 6)], total=2)


class _FakeQBClient:
    api_key = "test"

    def close(self):
        return None

    def get_version(self):
        return {"app": "5.0.0", "webapi": "2.11.4"}

    def get_transfer_info(self):
        return TransferInfo(connection_status="connected", dl_info_speed=1024)

    def list_torrents(self, category=""):
        assert category in {"", "tv"}
        return [TorrentTask(
            hash="a" * 40,
            name="Demo",
            progress=0.5,
            state="downloading",
            save_path="/secret",
            content_path="/secret/file",
            size=100,
            downloaded=50,
            dlspeed=10,
            upspeed=2,
            eta=5,
            ratio=0.1,
            category="tv",
            added_on=1,
        )]

    def get_torrent_files(self, torrent_hash):
        assert torrent_hash == "a" * 40
        return [TorrentFile(index=0, name="episode.mkv", size=100, progress=0.5)]


def test_media_transport_reuses_profile_and_projects_native_reads(monkeypatch):
    transport = MediaServerProviderTransport()
    profile = MediaServerProfile(
        source="configured:jellyfin",
        server_type="jellyfin",
        label="Jellyfin",
        url="http://hidden",
        credential="hidden",
        enabled=True,
    )
    monkeypatch.setattr(
        "app.agent.providers.media_server.list_configured_profiles",
        lambda: [profile],
    )
    monkeypatch.setattr(transport, "_client", lambda _profile: _FakeMediaClient())

    libraries = transport.execute_read("configured:jellyfin", "media.libraries.list", {})
    assert libraries.data["libraries"][0]["__object_id"] == "lib-1"
    assert "paths" not in libraries.data["libraries"][0]

    search = transport.execute_read(
        "configured:jellyfin", "media.items.search", {"query": "暗芝居", "limit": 5}
    )
    assert search.data["items"][0]["name"] == "暗芝居"

    series = transport.execute_read(
        "configured:jellyfin", "media.series.search", {"query": "暗芝居", "limit": 6}
    )
    assert series.data["series"][0]["__object_kind"] == "media_series"

    episodes = transport.execute_read(
        "configured:jellyfin",
        "media.series.episodes",
        {"series_ref": "series-1", "max_episodes": 10, "include_specials": False},
    )
    assert episodes.data["episodes"] == [
        {"season": 1, "episode": 1},
        {"season": 17, "episode": 6},
    ]


def test_qb_transport_reuses_client_and_never_returns_paths(monkeypatch):
    transport = QBittorrentProviderTransport()
    monkeypatch.setattr(transport, "_client", lambda _profile_ref: _FakeQBClient())

    queue = transport.execute_read(
        "configured:qbittorrent", "qb.torrents.info", {"category": "tv", "limit": 10}
    )
    task = queue.data["torrents"][0]
    assert task["__object_id"] == "a" * 40
    assert "save_path" not in task
    assert "content_path" not in task

    files = transport.execute_read(
        "configured:qbittorrent",
        "qb.torrents.files",
        {"torrent_ref": "a" * 40, "limit": 50},
    )
    assert files.data["files"][0]["name"] == "episode.mkv"


def test_media_transport_precise_refresh_never_calls_global_refresh(monkeypatch):
    calls: list[str] = []

    class _RefreshClient(_FakeMediaClient):
        def refresh_library(self, target_id):
            calls.append(target_id)
            return True

        def refresh_all(self):
            raise AssertionError("Agent 精准刷新不得调用全库刷新")

    transport = MediaServerProviderTransport()
    profile = MediaServerProfile(
        source="configured:jellyfin",
        server_type="jellyfin",
        label="Jellyfin",
        url="http://hidden",
        credential="hidden",
        enabled=True,
    )
    monkeypatch.setattr(
        "app.agent.providers.media_server.list_configured_profiles",
        lambda: [profile],
    )
    monkeypatch.setattr(transport, "_client", lambda _profile: _RefreshClient())

    preview = transport.preview_write(
        "configured:jellyfin",
        "media.library.refresh",
        {"library_ref": "lib-1"},
        {"library_ref": {"name": "动漫", "collection_type": "tvshows"}},
    )
    assert preview.data["scope"] == "library"
    result = transport.execute_write(
        "configured:jellyfin", "media.library.refresh", {"library_ref": "lib-1"}
    )
    assert result.data["global_refresh"] is False
    assert calls == ["lib-1"]


def test_qb_transport_writes_are_bounded_and_delete_keeps_files(monkeypatch):
    calls: list[tuple] = []

    class _WriteQBClient(_FakeQBClient):
        def pause_torrents(self, hashes):
            calls.append(("pause", hashes))
            return True

        def resume_torrents(self, hashes):
            calls.append(("resume", hashes))
            return True

        def delete_torrents(self, hashes, delete_files=False):
            calls.append(("delete", hashes, delete_files))
            return True

    transport = QBittorrentProviderTransport()
    monkeypatch.setattr(transport, "_client", lambda _profile_ref: _WriteQBClient())
    target_hash = "a" * 40

    preview = transport.preview_write(
        "configured:qbittorrent",
        "qb.torrents.delete_task",
        {"torrent_refs": [target_hash]},
        {"torrent_refs": [{"name": "Demo"}]},
    )
    assert preview.data["delete_files"] is False
    result = transport.execute_write(
        "configured:qbittorrent",
        "qb.torrents.delete_task",
        {"torrent_refs": [target_hash]},
    )
    assert result.data["delete_files"] is False
    assert calls == [("delete", target_hash, False)]
