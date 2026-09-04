from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.agent.provider_models import ProviderGatewayError
from app.agent.provider_projection import project_provider_value
from app.agent.providers.media_server import MediaServerProviderTransport
from app.agent.providers.qbittorrent import QBittorrentProviderTransport
from app.clients.base import (
    MediaItem,
    MediaRecommendationCandidate,
    MediaRecommendationInventory,
    SeriesCandidate,
    SeriesEpisodeInventory,
    SeriesSearchResult,
)
from app.clients.qbittorrent import TorrentFile, TorrentTask, TransferInfo
from app.modules.media_server_profiles import MediaServerProfile
from app.modules.qb_control import (
    QBControlConflict,
    QBControlSafetyUnavailable,
)


class _FakeMediaClient:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def server_identity(self):
        return "Dev Jellyfin", "Jellyfin", "12.0.0"

    def _user_id(self):
        return "viewer-1"

    def get_media_counts(self):
        return {
            "total_items": 166,
            "movie_count": 40,
            "series_count": 12,
            "episode_count": 114,
        }

    def get_library_media_counts(self, library_id):
        assert library_id == "lib-1"
        return {
            "total_items": 126,
            "movie_count": 0,
            "series_count": 9,
            "episode_count": 126,
        }

    def list_virtual_folders(self):
        return [
            {
                "id": "lib-1",
                "name": "动漫",
                "collection_type": "tvshows",
                "paths": ["/secret"],
            }
        ]

    def search_media(self, query, limit=12):
        assert query == "暗芝居"
        assert limit == 5
        return [MediaItem(id="item-1", name="暗芝居", type="Series", year="2013")]

    def recent_media(self, limit=8):
        assert limit == 4
        return [
            MediaItem(
                id="recent-1",
                name="新入库电影",
                type="Movie",
                year="2026",
                genres=("科幻",),
            )
        ]

    def recently_played(self, user_id, limit=8):
        assert user_id == "viewer-1"
        assert limit in {6, 50}
        return [
            MediaItem(
                id="played-1",
                name="第 3 集",
                type="Episode",
                series_name="示例动画",
                season_number=1,
                episode_number=3,
                last_played="2026-09-03T10:00:00+08:00",
                progress=100,
                genres=("动画", "奇幻"),
            ),
            MediaItem(
                id="played-2",
                name="示例电影",
                type="Movie",
                last_played="2026-09-02T20:00:00+08:00",
                genres=("科幻",),
            ),
        ]

    def enrich_media_genres(self, user_id, items):
        assert user_id == "viewer-1"
        return items

    def list_recommendation_candidates(
        self, user_id, *, media_type, max_items, page_size
    ):
        assert user_id == "viewer-1"
        assert media_type == "tv"
        assert max_items == 5000
        assert page_size == 200
        return MediaRecommendationInventory(
            candidates=[
                MediaRecommendationCandidate(
                    id="recommend-1",
                    name="男子高中生的日常",
                    media_type="tv",
                    year="2012",
                    original_title="男子高校生の日常",
                    genres=("动画", "喜剧"),
                    tags=("搞笑", "日常"),
                    community_rating=8.8,
                )
            ],
            total=1,
        )

    def continue_watching(self, user_id, limit=8):
        assert user_id == "viewer-1"
        assert limit == 3
        return [
            MediaItem(
                id="resume-1",
                name="第 4 集",
                type="Episode",
                series_name="示例动画",
                season_number=1,
                episode_number=4,
                progress=42.5,
            )
        ]

    def search_series_candidates(self, query, limit=6):
        return SeriesSearchResult(
            candidates=[
                SeriesCandidate(id="series-1", name=query, year="2013", tmdb_id="56559")
            ],
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
        return [
            TorrentTask(
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
            )
        ]

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
        user_id="viewer-1",
    )
    monkeypatch.setattr(
        "app.agent.providers.media_server.list_configured_profiles",
        lambda: [profile],
    )
    monkeypatch.setattr(transport, "_client", lambda _profile: _FakeMediaClient())

    counts = transport.execute_read(
        "configured:jellyfin", "media.items.counts", {}
    )
    assert counts.data == {
        "server_label": "Jellyfin",
        "total_items": 166,
        "movie_count": 40,
        "series_count": 12,
        "episode_count": 114,
    }

    libraries = transport.execute_read(
        "configured:jellyfin", "media.libraries.list", {}
    )
    assert libraries.data["libraries"][0]["__object_id"] == "lib-1"
    assert "paths" not in libraries.data["libraries"][0]

    library_counts = transport.execute_read(
        "configured:jellyfin",
        "media.library.counts",
        {"library_ref": "lib-1"},
    )
    assert library_counts.data == {
        "server_label": "Jellyfin",
        "total_items": 126,
        "movie_count": 0,
        "series_count": 9,
        "episode_count": 126,
    }
    assert "9 部剧集" in library_counts.summary

    search = transport.execute_read(
        "configured:jellyfin", "media.items.search", {"query": "暗芝居", "limit": 5}
    )
    assert search.data["items"][0]["name"] == "暗芝居"

    recent = transport.execute_read(
        "configured:jellyfin", "media.items.recent_added", {"limit": 4}
    )
    assert recent.data["items"][0]["genres"] == ["科幻"]

    played = transport.execute_read(
        "configured:jellyfin", "media.items.recent_played", {"limit": 6}
    )
    assert played.data["history_kind"] == "最近播放"
    assert played.data["preference_signals"] == {
        "recent_titles": ["示例动画", "示例电影"],
        "top_genres": ["动画", "奇幻", "科幻"],
        "media_types": ["episode", "movie"],
    }

    recommendations = transport.execute_read(
        "configured:jellyfin",
        "media.items.recommend_from_library",
        {
            "media_type": "tv",
            "must_match": ["动画|Animation"],
            "prefer": ["日本|Japan|Japanese", "喜剧|Comedy"],
            "exclude": [],
            "min_rating": 7.0,
            "exclude_played": True,
            "limit": 5,
        },
    )
    assert recommendations.data["items"][0]["name"] == "男子高中生的日常"
    assert recommendations.data["history"]["records_used"] == 2
    assert recommendations.data["items"][0]["__object_kind"] == "media_item"

    resume = transport.execute_read(
        "configured:jellyfin", "media.items.continue_watching", {"limit": 3}
    )
    assert resume.data["history_kind"] == "继续观看"
    assert resume.data["items"][0]["progress_percent"] == 42.5

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


def test_media_transport_uses_server_default_user_when_not_configured(monkeypatch):
    transport = MediaServerProviderTransport()
    profile = MediaServerProfile(
        source="configured:emby",
        server_type="emby",
        label="Emby",
        url="http://hidden",
        credential="hidden",
        enabled=True,
        user_id="",
    )
    monkeypatch.setattr(
        "app.agent.providers.media_server.list_configured_profiles",
        lambda: [profile],
    )
    monkeypatch.setattr(transport, "_client", lambda _profile: _FakeMediaClient())

    played = transport.execute_read(
        "configured:emby", "media.items.recent_played", {"limit": 6}
    )
    assert played.data["user_selection"] == "服务器默认用户"

    resume = transport.execute_read(
        "configured:emby", "media.items.continue_watching", {"limit": 3}
    )
    assert resume.data["user_selection"] == "服务器默认用户"


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
    assert task["progress_percent"] == 50.0
    assert task["downloaded_bytes"] == 50
    assert "progress" not in task
    assert "downloaded" not in task
    assert queue.summary == (
        "qBittorrent 实时队列共 1 项：进行中 1、排队 0、暂停 0、异常 0、已完成 0、其他 0"
    )
    assert queue.data["state_counts"] == {
        "active": 1,
        "queued": 0,
        "paused": 0,
        "failed": 0,
        "completed": 0,
        "other": 0,
    }
    assert queue.data["transfer"] == {
        "connection_status": "connected",
        "download_speed": 1024,
        "upload_speed": 0,
    }

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
        "configured:jellyfin",
        "media.library.refresh",
        {"library_ref": "lib-1"},
        expected_profile_revision=transport.profile_revision(
            "configured:jellyfin"
        ),
    )
    assert result.data["global_refresh"] is False
    assert calls == ["lib-1"]


def test_media_transport_marks_rejected_refresh_as_possible_external_write(
    monkeypatch,
):
    class _RejectedRefreshClient(_FakeMediaClient):
        def refresh_library(self, _target_id):
            return False

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
    monkeypatch.setattr(
        transport, "_client", lambda _profile: _RejectedRefreshClient()
    )

    with pytest.raises(ProviderGatewayError) as rejected:
        transport.execute_write(
            "configured:jellyfin",
            "media.library.refresh",
            {"library_ref": "lib-1"},
            expected_profile_revision=transport.profile_revision(
                "configured:jellyfin"
            ),
        )

    assert rejected.value.code == "provider_write_failed"
    assert rejected.value.external_write_possible


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
    monkeypatch.setattr(
        transport,
        "_client_from_settings",
        lambda _profile_ref, _settings: _WriteQBClient(),
    )
    monkeypatch.setattr(
        "app.agent.providers.qbittorrent.assert_qb_control_allowed",
        lambda *_args, **_kwargs: None,
    )
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
        expected_profile_revision=transport.profile_revision(
            "configured:qbittorrent"
        ),
    )
    assert result.data["delete_files"] is False
    assert calls == [("delete", target_hash, False)]


def test_qb_pause_only_reports_verified_after_state_changes(monkeypatch):
    class _PauseClient(_FakeQBClient):
        def pause_torrents(self, _hashes):
            return True

    transport = QBittorrentProviderTransport()
    monkeypatch.setattr(
        transport,
        "_client_from_settings",
        lambda _profile_ref, _settings: _PauseClient(),
    )
    result = transport.execute_write(
        "configured:qbittorrent",
        "qb.torrents.pause",
        {"torrent_refs": ["a" * 40]},
        expected_profile_revision=transport.profile_revision(
            "configured:qbittorrent"
        ),
    )
    assert result.data["verification"] == "pending"


def test_qb_provider_maps_local_media_write_conflict(monkeypatch):
    transport = QBittorrentProviderTransport()
    monkeypatch.setattr(transport, "_client", lambda _profile_ref: _FakeQBClient())

    def reject_control(*_args, **_kwargs):
        raise QBControlConflict("本地整理写入中")

    monkeypatch.setattr(
        "app.agent.providers.qbittorrent.assert_qb_control_allowed",
        reject_control,
    )
    try:
        transport.preview_write(
            "configured:qbittorrent",
            "qb.torrents.resume",
            {"torrent_refs": ["a" * 40]},
            {"torrent_refs": [{"name": "Demo"}]},
        )
    except ProviderGatewayError as exc:
        assert exc.code == "provider_conflict"
        assert not exc.external_write_possible
    else:
        raise AssertionError("本地整理冲突必须拒绝 qB 恢复")

    monkeypatch.setattr(
        transport,
        "_client_from_settings",
        lambda _profile_ref, _settings: _FakeQBClient(),
    )
    with pytest.raises(ProviderGatewayError) as blocked:
        transport.execute_write(
            "configured:qbittorrent",
            "qb.torrents.resume",
            {"torrent_refs": ["a" * 40]},
            expected_profile_revision=transport.profile_revision(
                "configured:qbittorrent"
            ),
        )
    assert blocked.value.code == "provider_conflict"
    assert not blocked.value.external_write_possible


def test_qb_provider_distinguishes_unavailable_safety_state(monkeypatch):
    transport = QBittorrentProviderTransport()
    monkeypatch.setattr(transport, "_client", lambda _profile_ref: _FakeQBClient())

    def unavailable(*_args, **_kwargs):
        raise QBControlSafetyUnavailable("安全状态暂不可用")

    monkeypatch.setattr(
        "app.agent.providers.qbittorrent.assert_qb_control_allowed",
        unavailable,
    )
    with pytest.raises(ProviderGatewayError) as preview_failure:
        transport.preview_write(
            "configured:qbittorrent",
            "qb.torrents.resume",
            {"torrent_refs": ["a" * 40]},
            {"torrent_refs": [{"name": "Demo"}]},
        )
    assert preview_failure.value.code == "provider_unavailable"
    assert not preview_failure.value.external_write_possible

    monkeypatch.setattr(
        transport,
        "_client_from_settings",
        lambda _profile_ref, _settings: _FakeQBClient(),
    )
    with pytest.raises(ProviderGatewayError) as execute_failure:
        transport.execute_write(
            "configured:qbittorrent",
            "qb.torrents.resume",
            {"torrent_refs": ["a" * 40]},
            expected_profile_revision=transport.profile_revision(
                "configured:qbittorrent"
            ),
        )
    assert execute_failure.value.code == "provider_unavailable"
    assert not execute_failure.value.external_write_possible


def test_qb_provider_holds_shared_lease_across_check_write_and_verification(
    monkeypatch,
):
    lease_active = False
    checkpoints: list[str] = []

    @contextmanager
    def lease():
        nonlocal lease_active
        assert not lease_active
        lease_active = True
        checkpoints.append("lease_enter")
        try:
            yield
        finally:
            checkpoints.append("lease_exit")
            lease_active = False

    class _LeaseAwareClient(_FakeQBClient):
        def list_torrents(self, category=""):
            assert lease_active
            checkpoints.append("read")
            return super().list_torrents(category)

        def pause_torrents(self, hashes):
            assert lease_active
            checkpoints.append("write")
            assert hashes == "a" * 40
            return True

    def safety_check(*_args, **_kwargs):
        assert lease_active
        checkpoints.append("safety")

    transport = QBittorrentProviderTransport()
    monkeypatch.setattr(
        transport,
        "_client_from_settings",
        lambda _profile_ref, _settings: _LeaseAwareClient(),
    )
    monkeypatch.setattr(
        "app.agent.providers.qbittorrent.qb_control_write_lease",
        lease,
    )
    monkeypatch.setattr(
        "app.agent.providers.qbittorrent.assert_qb_control_allowed",
        safety_check,
    )

    result = transport.execute_write(
        "configured:qbittorrent",
        "qb.torrents.pause",
        {"torrent_refs": ["a" * 40]},
        expected_profile_revision=transport.profile_revision(
            "configured:qbittorrent"
        ),
    )

    assert result.data["accepted"] is True
    assert checkpoints == [
        "lease_enter",
        "read",
        "safety",
        "write",
        "read",
        "lease_exit",
    ]


def test_qb_write_revision_and_client_share_one_settings_snapshot(monkeypatch):
    settings_a = {
        "QB_URL": "http://qb-a.invalid",
        "QB_USERNAME": "user-a",
        "QB_PASSWORD": "password-a",
        "QB_API_KEY": "",
    }
    settings_b = {
        "QB_URL": "http://qb-b.invalid",
        "QB_USERNAME": "user-b",
        "QB_PASSWORD": "password-b",
        "QB_API_KEY": "",
    }
    transport = QBittorrentProviderTransport()
    expected_revision = transport._profile_revision_from_settings(
        "configured:qbittorrent", settings_a
    )
    settings_calls = 0
    client_urls: list[str] = []
    side_effects: list[str] = []

    class _PauseClient(_FakeQBClient):
        def pause_torrents(self, hashes):
            side_effects.append(hashes)
            return True

    def settings_snapshot():
        nonlocal settings_calls
        settings_calls += 1
        return dict(settings_a if settings_calls == 1 else settings_b)

    def client_from_settings(_profile_ref, values):
        client_urls.append(str(values["QB_URL"]))
        return _PauseClient()

    monkeypatch.setattr(transport, "_settings", settings_snapshot)
    monkeypatch.setattr(transport, "_client_from_settings", client_from_settings)
    monkeypatch.setattr(
        "app.agent.providers.qbittorrent.assert_qb_control_allowed",
        lambda *_args, **_kwargs: None,
    )

    result = transport.execute_write(
        "configured:qbittorrent",
        "qb.torrents.pause",
        {"torrent_refs": ["a" * 40]},
        expected_profile_revision=expected_revision,
    )

    assert result.data["accepted"] is True
    assert settings_calls == 1
    assert client_urls == [settings_a["QB_URL"]]
    assert side_effects == ["a" * 40]


def test_qb_write_rejects_changed_snapshot_before_client_or_side_effect(monkeypatch):
    settings_a = {
        "QB_URL": "http://qb-a.invalid",
        "QB_USERNAME": "user-a",
        "QB_PASSWORD": "password-a",
        "QB_API_KEY": "",
    }
    settings_b = dict(settings_a, QB_URL="http://qb-b.invalid")
    transport = QBittorrentProviderTransport()
    expected_revision = transport._profile_revision_from_settings(
        "configured:qbittorrent", settings_a
    )
    client_calls: list[dict] = []
    monkeypatch.setattr(transport, "_settings", lambda: dict(settings_b))
    monkeypatch.setattr(
        transport,
        "_client_from_settings",
        lambda _profile_ref, values: client_calls.append(dict(values)),
    )

    with pytest.raises(ProviderGatewayError) as stale:
        transport.execute_write(
            "configured:qbittorrent",
            "qb.torrents.pause",
            {"torrent_refs": ["a" * 40]},
            expected_profile_revision=expected_revision,
        )

    assert stale.value.code == "confirmation_stale"
    assert client_calls == []


def test_media_write_revision_and_client_share_one_profile_snapshot(monkeypatch):
    profile_a = MediaServerProfile(
        source="configured:jellyfin",
        server_type="jellyfin",
        label="Jellyfin A",
        url="http://media-a.invalid",
        credential="token-a",
        enabled=True,
    )
    profile_b = MediaServerProfile(
        source="configured:jellyfin",
        server_type="jellyfin",
        label="Jellyfin B",
        url="http://media-b.invalid",
        credential="token-b",
        enabled=True,
    )
    transport = MediaServerProviderTransport()
    expected_revision = transport._profile_revision_from_profile(profile_a)
    profile_calls = 0
    client_urls: list[str] = []
    side_effects: list[str] = []

    class _RefreshClient(_FakeMediaClient):
        def refresh_library(self, target_id):
            side_effects.append(target_id)
            return True

    def configured_profiles():
        nonlocal profile_calls
        profile_calls += 1
        return [profile_a if profile_calls == 1 else profile_b]

    def client_from_profile(profile):
        client_urls.append(profile.url)
        return _RefreshClient()

    monkeypatch.setattr(
        "app.agent.providers.media_server.list_configured_profiles",
        configured_profiles,
    )
    monkeypatch.setattr(transport, "_client", client_from_profile)

    result = transport.execute_write(
        "configured:jellyfin",
        "media.library.refresh",
        {"library_ref": "lib-1"},
        expected_profile_revision=expected_revision,
    )

    assert result.data["accepted"] is True
    assert profile_calls == 1
    assert client_urls == [profile_a.url]
    assert side_effects == ["lib-1"]


def test_provider_projection_preserves_dotted_name_without_relaxing_other_fields():
    dotted_name = "Example.Show.S01E01.1080p.mkv"
    internal_text = "private.provider.operation"
    projected = project_provider_value(
        {
            "name": dotted_name,
            "description": internal_text,
            "path": "/secret/Example.Show.S01E01.1080p.mkv",
            "nested": {"name": "Another.Show.S02E03.mkv", "note": internal_text},
        }
    )

    assert projected["name"] == dotted_name
    assert projected["description"] != internal_text
    assert "path" not in projected
    assert projected["nested"]["name"] == "Another.Show.S02E03.mkv"
    assert projected["nested"]["note"] != internal_text
    assert project_provider_value({"name": "/secret/Hidden.Show.mkv"})["name"] == ""
