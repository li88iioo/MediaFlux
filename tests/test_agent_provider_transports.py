from __future__ import annotations
from typing import Any

from contextlib import contextmanager

import pytest

from app import services
from app.clients.base import (
    MediaItem,
    MediaRecommendationCandidate,
    MediaRecommendationInventory,
    MediaSearchResult,
    SeriesCandidate,
    SeriesEpisodeInventory,
    SeriesSearchResult,
)
from app.clients.qbittorrent import TorrentFile, TorrentTask, TransferInfo
from app.modules.media_server_profiles import MediaServerProfile
from app.modules import media_library, qb_control
from app.modules.qb_control import (
    QBControlConflict,
    QBControlSafetyUnavailable,
)
from tests.support import isolated_test_database


class _FakeMediaClient:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def server_identity(self):
        return "Dev Jellyfin", "Jellyfin", "12.0.0"

    def _user_id(self):
        return "viewer-1"

    def media_web_url(self, item_id):
        return f"http://hidden/web/index.html#!/details?id={item_id}"

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
        return MediaSearchResult(items=[
            MediaItem(
                id="item-1",
                name="暗芝居",
                type="Series",
                year="2013",
                web_url=self.media_web_url("item-1"),
            )
        ], total=1, has_more=False, limit=limit)

    def recent_media(self, limit=8):
        assert limit == 4
        return [
            MediaItem(
                id="recent-1",
                name="新入库电影",
                type="Movie",
                year="2026",
                genres=("科幻",),
                web_url=self.media_web_url("recent-1"),
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
                web_url=self.media_web_url("played-1"),
            ),
            MediaItem(
                id="played-2",
                name="示例电影",
                type="Movie",
                last_played="2026-09-02T20:00:00+08:00",
                genres=("科幻",),
                web_url=self.media_web_url("played-2"),
            ),
        ]

    def enrich_media_genres(self, user_id, items):
        assert user_id == "viewer-1"
        return items

    def list_recommendation_candidates(
        self, user_id, *, media_type, max_items, page_size, cancelled=None
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
                web_url=self.media_web_url("resume-1"),
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


def test_media_reads_reuse_profile_through_public_library(monkeypatch):
    profile = MediaServerProfile(
        source="configured:jellyfin",
        server_type="jellyfin",
        label="Jellyfin",
        url="http://hidden",
        credential="hidden",
        enabled=True,
        user_id="viewer-1",
    )
    monkeypatch.setattr(MediaServerProfile, "create_client", lambda _profile: _FakeMediaClient())
    monkeypatch.setattr(services, "list_configured_profiles", lambda: [profile])
    monkeypatch.setattr(media_library, "list_configured_profiles", lambda: [profile])

    counts = services.get_library_counts("jellyfin")
    assert counts["status"] == "success"
    assert counts["data"]["counts"] == {
        "total_items": 166,
        "movie_count": 40,
        "series_count": 12,
        "episode_count": 114,
    }

    libraries = media_library.libraries("jellyfin")
    assert libraries["libraries"][0]["id"] == "lib-1"
    assert "paths" not in libraries["libraries"][0]

    library_counts = services.get_library_counts("jellyfin", "lib-1")
    assert library_counts["status"] == "success"
    assert library_counts["data"]["counts"] == {
        "total_items": 126,
        "movie_count": 0,
        "series_count": 9,
        "episode_count": 126,
    }
    assert library_counts["data"]["scope"] == "library"
    assert library_counts["data"]["library_id"] == "lib-1"

    recent = media_library.recent("jellyfin", limit=4)
    assert recent["items"][0]["id"] == "recent-1"
    assert recent["items"][0]["genres"] == ["科幻"]
    assert recent["items"][0]["open_url"].endswith("id=recent-1")

    played = media_library.history("jellyfin", limit=6)
    assert played["history_kind"] == "最近播放"
    assert played["items"][0]["open_url"].endswith("id=played-1")
    assert played["preference_signals"] == {
        "recent_titles": ["示例动画", "示例电影"],
        "top_genres": ["动画", "奇幻", "科幻"],
        "media_types": ["episode", "movie"],
    }
    recommendations = media_library.recommend(
        "jellyfin", media_type="tv", must_match=["动画|Animation"],
        prefer=["日本|Japan|Japanese", "喜剧|Comedy"], exclude=[],
        min_rating=7.0, exclude_played=True, limit=5,
    )
    assert recommendations["items"][0]["name"] == "男子高中生的日常"
    assert recommendations["history"]["records_used"] == 2
    assert recommendations["items"][0]["id"] == "recommend-1"
    assert recommendations["items"][0]["open_url"].endswith("id=recommend-1")
    resume = media_library.resume("jellyfin", limit=3)
    assert resume["history_kind"] == "继续观看"
    assert resume["items"][0]["progress"] == 42.5
    assert resume["items"][0]["open_url"].endswith("id=resume-1")

    episodes = media_library.series_episodes("jellyfin", "series-1", max_episodes=10, include_specials=False)
    assert episodes["data"]["episodes"] == [{"season":1,"episode":1},{"season":17,"episode":6}]


def test_public_playback_uses_server_default_user_when_not_configured(monkeypatch):
    profile = MediaServerProfile(
        source="configured:emby", server_type="emby", label="Emby",
        url="http://hidden", credential="hidden", enabled=True, user_id="",
    )
    monkeypatch.setattr(media_library, "list_configured_profiles", lambda: [profile])
    monkeypatch.setattr(MediaServerProfile, "create_client", lambda _profile: _FakeMediaClient())
    for result in (media_library.history("emby", limit=6), media_library.resume("emby", limit=3)):
        assert result["user_selection"] == "服务器默认用户"
        assert result["scope"]["user_id"] == "viewer-1"


def test_public_library_precise_refresh_never_calls_global_refresh(monkeypatch):
    calls: list[str] = []

    class _RefreshClient(_FakeMediaClient):
        def refresh_library(self, target_id, **options):
            calls.append(target_id)
            return True

        def refresh_all(self):
            raise AssertionError("精准刷新不得调用全库刷新")

    profile = MediaServerProfile(
        source="configured:jellyfin",
        server_type="jellyfin",
        label="Jellyfin",
        url="http://hidden",
        credential="hidden",
        enabled=True,
    )
    monkeypatch.setattr(
        media_library, "list_configured_profiles",
        lambda: [profile],
    )
    monkeypatch.setattr(MediaServerProfile, "create_client", lambda _profile: _RefreshClient())

    preview = media_library.prepare_refresh("jellyfin", "lib-1")
    assert preview["library"]["id"] == "lib-1"
    assert preview["snapshot"]["profile_revision"] == profile.fingerprint()
    result = media_library.refresh(
        "jellyfin", "lib-1", expected_snapshot=preview["snapshot"],
    )
    assert result["status"] == "accepted"
    assert result["data"]["accepted"] is True
    assert result["data"]["library_id"] == "lib-1"
    assert result["data"]["global_refresh"] is False
    for library_id in ("", "missing-library"):
        with pytest.raises(media_library.MediaLibraryError):
            media_library.prepare_refresh("jellyfin", library_id)
        with pytest.raises(media_library.MediaLibraryError):
            media_library.refresh("jellyfin", library_id, expected_snapshot=preview["snapshot"])
    assert calls == ["lib-1"]


@pytest.mark.parametrize("failure", [False, TimeoutError("synthetic timeout")], ids=["false", "exception"])
def test_public_library_refresh_rejection_or_exception_has_unknown_outcome(
    monkeypatch, failure,
):
    calls = []

    class _RejectedRefreshClient(_FakeMediaClient):
        def refresh_library(self, target_id, **options):
            calls.append(target_id)
            if isinstance(failure, Exception):
                raise failure
            return failure

        def refresh_all(self):
            raise AssertionError("结果未知时不得回退全库刷新")

    profile = MediaServerProfile(
        source="configured:jellyfin",
        server_type="jellyfin",
        label="Jellyfin",
        url="http://hidden",
        credential="hidden",
        enabled=True,
    )
    monkeypatch.setattr(
        media_library, "list_configured_profiles",
        lambda: [profile],
    )
    monkeypatch.setattr(
        MediaServerProfile, "create_client", lambda _profile: _RejectedRefreshClient()
    )

    preview = media_library.prepare_refresh("jellyfin", "lib-1")
    with pytest.raises(media_library.MediaLibraryError) as rejected:
        media_library.refresh(
            "jellyfin", "lib-1", expected_snapshot=preview["snapshot"],
        )

    assert rejected.value.code == "outcome_unknown"
    assert rejected.value.execution_started
    assert calls == ["lib-1"]


_QB_SETTINGS = {"QB_URL": "http://qb-a.invalid", "QB_USERNAME": "user-a", "QB_PASSWORD": "password-a", "QB_API_KEY": ""}


def _qb_control_client(monkeypatch, client):
    monkeypatch.setattr(qb_control.config, "get_many", lambda _keys: dict(_QB_SETTINGS))
    monkeypatch.setattr(qb_control, "configured_qb_client", lambda _values: client)
    return client


def test_qb_shared_writes_are_bounded_and_delete_keeps_files(monkeypatch):
    calls = []
    class Client(_FakeQBClient):
        removed = False
        def list_torrents(self, category=""):
            return [] if self.removed else super().list_torrents(category)
        def delete_torrents(self, hashes, delete_files=False):
            calls.append(("delete", hashes, delete_files))
            self.removed = True
            return True
    _qb_control_client(monkeypatch, Client())
    with isolated_test_database():
        preview = qb_control.prepare_qb_control("delete", ["a" * 40])
        result = qb_control.execute_qb_control(preview)
    assert result["delete_files"] is False and result["verification"] == "verified"
    assert calls == [("delete", "a" * 40, False)]


def test_qb_pause_only_reports_verified_after_state_changes(monkeypatch):
    class Client(_FakeQBClient):
        def pause_torrents(self, _hashes):
            return True
    _qb_control_client(monkeypatch, Client())
    result = qb_control.execute_qb_control(qb_control.prepare_qb_control("pause", ["a" * 40]))
    assert result["verification"] == "pending" and result["status"] == "accepted"


@pytest.mark.parametrize("failure", [QBControlConflict, QBControlSafetyUnavailable])
def test_qb_shared_control_rechecks_local_media_conflict_and_unavailable_safety(monkeypatch, failure):
    client = _qb_control_client(monkeypatch, _FakeQBClient())
    with isolated_test_database():
        snapshot = qb_control.prepare_qb_control("resume", ["a" * 40])
        def reject(*_args, **_kwargs):
            raise failure("本地整理安全状态阻止恢复")
        monkeypatch.setattr(qb_control, "assert_qb_control_allowed", reject)
        with pytest.raises(failure):
            qb_control.prepare_qb_control("resume", ["a" * 40])
        with pytest.raises(failure):
            qb_control.execute_qb_control(snapshot)
    # 假客户端不提供写方法；如绕过安全检查便会得到错误结果，不能抛出这里要求的安全异常。
    assert not hasattr(client, "resume_torrents")


def test_qb_shared_control_holds_lease_across_check_write_and_verification(monkeypatch):
    checkpoints = []
    active = False
    class Client(_FakeQBClient):
        def list_torrents(self, category=""):
            assert active
            checkpoints.append("read")
            return super().list_torrents(category)
        def pause_torrents(self, hashes):
            assert active and hashes == "a" * 40
            checkpoints.append("write")
            return True
    _qb_control_client(monkeypatch, Client())
    @contextmanager
    def lease():
        nonlocal active
        assert not active
        active = True
        checkpoints.append("lease_enter")
        try:
            yield
        finally:
            checkpoints.append("lease_exit")
            active = False
    def safety(*_args, **_kwargs):
        assert active
        checkpoints.append("safety")
    monkeypatch.setattr(qb_control, "qb_control_write_lease", lease)
    monkeypatch.setattr(qb_control, "assert_qb_control_allowed", safety)
    snapshot = {"operation": "pause", "profile_revision": qb_control.qb_profile_revision(_QB_SETTINGS),
                "targets": [{"hash": "a" * 40, "name": "Demo", "size": 100, "added_on": 1}]}
    result = qb_control.execute_qb_control(snapshot)
    assert result["accepted"] is True
    assert checkpoints == ["lease_enter", "read", "safety", "write", "read", "lease_exit"]


def test_qb_write_revision_and_client_share_one_settings_snapshot(monkeypatch):
    urls, writes, reads = [], [], []
    class Client(_FakeQBClient):
        def pause_torrents(self, hashes):
            writes.append(hashes)
            return True
    def settings(_keys):
        reads.append(True)
        return dict(_QB_SETTINGS if len(reads) == 1 else dict(_QB_SETTINGS, QB_URL="http://qb-b.invalid"))
    def client(values):
        urls.append(values["QB_URL"])
        return Client()
    monkeypatch.setattr(qb_control.config, "get_many", settings)
    monkeypatch.setattr(qb_control, "configured_qb_client", client)
    snapshot = {"operation": "pause", "profile_revision": qb_control.qb_profile_revision(_QB_SETTINGS),
                "targets": [{"hash": "a" * 40, "name": "Demo", "size": 100, "added_on": 1}]}
    result = qb_control.execute_qb_control(snapshot)
    assert result["accepted"] is True and len(reads) == 1
    assert urls == [_QB_SETTINGS["QB_URL"]] and writes == ["a" * 40]


def test_qb_write_rejects_changed_snapshot_before_client_or_side_effect(monkeypatch):
    snapshot = {"operation": "pause", "profile_revision": qb_control.qb_profile_revision(_QB_SETTINGS),
                "targets": [{"hash": "a" * 40, "name": "Demo", "size": 100, "added_on": 1}]}
    monkeypatch.setattr(qb_control.config, "get_many", lambda _keys: dict(_QB_SETTINGS, QB_URL="http://qb-b.invalid"))
    calls = []
    monkeypatch.setattr(qb_control, "configured_qb_client", lambda values: calls.append(values))
    with pytest.raises(qb_control.QBControlStale):
        qb_control.execute_qb_control(snapshot)
    assert calls == []


@pytest.mark.parametrize("stale", [False, True])
def test_media_refresh_revision_and_client_share_one_profile_snapshot(monkeypatch, stale):
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
    expected_snapshot = {
        "profile_revision": profile_a.fingerprint(),
        "library": {"id": "lib-1", "name": "动漫", "collection_type": "tvshows"},
        "options": {"mode": "scan", "replace_images": False, "regenerate_trickplay": False},
    }
    profile_calls = 0
    client_urls: list[str] = []
    side_effects: list[str] = []

    class _RefreshClient(_FakeMediaClient):
        def refresh_library(self, target_id, **options):
            side_effects.append(target_id)
            return True

    def configured_profiles():
        nonlocal profile_calls
        profile_calls += 1
        return [profile_a if not stale and profile_calls == 1 else profile_b]

    def client_from_profile(profile):
        client_urls.append(profile.url)
        return _RefreshClient()

    monkeypatch.setattr(
        media_library, "list_configured_profiles",
        configured_profiles,
    )
    monkeypatch.setattr(MediaServerProfile, "create_client", client_from_profile)

    if stale:
        with pytest.raises(media_library.MediaLibraryError) as rejected:
            media_library.refresh("jellyfin", "lib-1", expected_snapshot=expected_snapshot)
        assert rejected.value.code == "confirmation_stale"
        assert not rejected.value.execution_started
        assert side_effects == []
    else:
        result = media_library.refresh("jellyfin", "lib-1", expected_snapshot=expected_snapshot)
        assert result["data"]["accepted"] is True
        assert side_effects == ["lib-1"]
    assert profile_calls == 1
    assert client_urls == [profile_b.url if stale else profile_a.url]




@pytest.mark.parametrize("failure", ["timeout", "invalid_payload", "server_error"])
def test_emby_counts_failure_reaches_public_service_instead_of_zero_inventory(monkeypatch, failure):
    import requests

    profile = MediaServerProfile(
        source="configured:emby", server_type="emby", label="Emby",
        url="http://media.invalid", credential="test-only", enabled=True, user_id="viewer-1",
    )
    monkeypatch.setattr(services, "list_configured_profiles", lambda: [profile])
    paths = []

    def get(_session, url, **_kwargs):
        paths.append(url)
        response = requests.Response()
        response.status_code = 200
        response.url = url
        if url.endswith("/Items/Counts"):
            if failure == "timeout":
                raise requests.Timeout("synthetic unavailable upstream")
            response.status_code = 503 if failure == "server_error" else 200
            response._content = b"[]"
        else:
            assert url.endswith("/Users/viewer-1/Items")
            response._content = b'{"TotalRecordCount":3200,"Items":[]}'
        return response

    monkeypatch.setattr(requests.Session, "get", get)
    result = services.get_library_counts("emby")
    assert result["ok"] is False and result["status"] == "unavailable"
    assert result["data"]["counts"]["total_items"] is None
    assert result["data"]["sources"][0]["status"] == "unavailable"
    assert len(paths) == 2


@pytest.mark.parametrize("server_type", ["jellyfin", "emby"])
@pytest.mark.parametrize("scope_key", ["library_name", "library_id"])
@pytest.mark.parametrize("media_type,item_types,expected_names", [
    (None, "Movie,Series,Episode", ["第6集", "其他剧", "第三部剧"]),
    ("all", "Movie,Series,Episode", ["第6集", "其他剧", "第三部剧"]),
    ("tv", "Series,Episode", ["第6集", "其他剧", "第三部剧"]),
    ("movie", "Movie", []),
    ("episode", "Episode", ["第6集", "第5集", "第4集"]),
])
def test_recent_added_granularity_through_public_library_and_client_http(
    monkeypatch, server_type, scope_key, media_type, item_types, expected_names,
):
    import json
    import requests

    profile = MediaServerProfile(
        source=f"configured:{server_type}", server_type=server_type, label=server_type,
        url="http://media.invalid", credential="test", enabled=True, user_id="viewer",
    )
    monkeypatch.setattr(media_library, "list_configured_profiles", lambda: [profile])
    folders = [
        {"ItemId": "tv-library", "Name": "电视剧", "CollectionType": "tvshows"},
        {"ItemId": "anime-library", "Name": "动漫", "CollectionType": "tvshows"},
    ]
    rows: list[dict[str, Any]] = [
        {"Id": f"episode-{number}", "Name": f"第{number}集", "Type": "Episode",
         "SeriesId": "series-a", "SeriesName": "黑镜", "ParentIndexNumber": 7,
         "IndexNumber": number, "DateCreated": f"2026-10-16T12:00:0{number}Z",
         "ParentId": "tv-library"}
        for number in (6, 5, 4)
    ]
    rows.extend([
        {**rows[0], "DateCreated": "2026-10-16T12:00:03Z"},
        {"Id": "series-a", "Name": "黑镜", "Type": "Series",
         "DateCreated": "2026-10-16T12:00:03Z", "ParentId": "tv-library"},
        {"Id": "series-b", "Name": "其他剧", "Type": "Series",
         "DateCreated": "2026-10-16T12:00:02Z", "ParentId": "tv-library"},
        {"Id": "series-c", "Name": "第三部剧", "Type": "Series",
         "DateCreated": "2026-10-16T12:00:01Z", "ParentId": "tv-library"},
        {"Id": "anime-episode", "Name": "无动画标签的动漫", "Type": "Episode",
         "SeriesId": "anime-series", "DateCreated": "2026-10-16T12:00:07Z",
         "Genres": [], "ParentId": "anime-library"},
        {"Id": "movie", "Name": "电影", "Type": "Movie",
         "DateCreated": "2026-10-16T12:00:08Z", "ParentId": "movie-library"},
    ])
    calls = []

    def request(path, **kwargs):
        if path == "/System/Info/Public":
            return {"ProductName": server_type, "Version": "4.9"}
        if path == "/Library/VirtualFolders/Query":
            return {"Items": folders}
        if path == "/Library/VirtualFolders":
            return folders
        assert path == "/Users/viewer/Items"
        params = kwargs["params"]
        calls.append(params)
        filtered = sorted([
            row for row in rows
            if row["ParentId"] == params.get("ParentId")
            and row["Type"] in params["IncludeItemTypes"].split(",")
        ], key=lambda row: row["DateCreated"], reverse=True)
        offset = params["StartIndex"]
        return {"Items": filtered[offset:offset + params["Limit"]]}

    def get(_session, url, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response._content = json.dumps(request(url.removeprefix(profile.url), **kwargs)).encode()
        return response

    monkeypatch.setattr(requests.Session, "get", get)
    if scope_key == "library_id":
        libraries = media_library.libraries(server_type)
        scope_value = libraries["libraries"][0]["id"]
    else:
        scope_value = "电视剧"
    arguments = {"server": server_type, "limit": 3, scope_key: scope_value}
    if media_type is not None:
        arguments["media_type"] = media_type
    with isolated_test_database():
        result = media_library.recent(**arguments)
    assert [item["name"] for item in result["items"]] == expected_names
    assert result["count"] == len(expected_names)
    assert len({item["id"] for item in result["items"]}) == len(expected_names)
    assert result["granularity"] == ("episode" if media_type == "episode" else "work")
    assert result["scope"]["kind"] == "library"
    assert result["scope"]["library"]["id"] == "tv-library"
    assert result["scope"]["library"]["name"] == "电视剧"
    assert result["scope"]["media_type"] == (media_type or "all")
    if media_type == "episode":
        assert [(item["series_name"], item["season_number"], item["episode_number"])
                for item in result["items"]] == [("黑镜", 7, 6), ("黑镜", 7, 5), ("黑镜", 7, 4)]
    assert len(calls) == 1
    assert calls[0]["ParentId"] == "tv-library"
    assert calls[0]["IncludeItemTypes"] == item_types
    assert calls[0]["SortBy"] == "DateCreated"
    assert calls[0]["SortOrder"] == "Descending"
    assert calls[0]["Recursive"] == "true"


def test_recent_added_uses_real_library_scope_and_never_genre_guessing(monkeypatch):
    import json
    import requests

    profile = MediaServerProfile(source="configured:jellyfin", server_type="jellyfin", label="Jellyfin", url="http://media.invalid", credential="test", enabled=True, user_id="viewer")
    monkeypatch.setattr(media_library, "list_configured_profiles", lambda: [profile])
    calls = []
    folders = [{"ItemId": "tv-library", "Name": "电视剧", "CollectionType": "tvshows"},
               {"ItemId": "anime-library", "Name": "动漫", "CollectionType": "tvshows"}]
    def request(path, **kwargs):
        calls.append((path, kwargs))
        if path == "/Library/VirtualFolders":
            return folders
        assert path == "/Users/viewer/Items"
        parent = kwargs["params"].get("ParentId")
        # 动漫条目没有Animation标签；只能由ParentId排除，不能从Genres猜测。
        return {"Items": [{"Id": "one", "Name": "真人剧" if parent == "tv-library" else "无动画标签的动漫", "Type": "Series", "Genres": []}]}
    def get(_session, url, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response._content = json.dumps(request(url.removeprefix(profile.url), **kwargs)).encode()
        return response

    monkeypatch.setattr(requests.Session, "get", get)
    for query in ({"library_name": "电视剧"}, {}):
        result = media_library.recent("jellyfin", **query)
        assert result["scope"]["kind"] == ("library" if query else "all")
        assert result["items"][0]["name"] == ("真人剧" if query else "无动画标签的动漫")
    listed = media_library.libraries("jellyfin")
    library_id = listed["libraries"][0]["id"]
    result = media_library.recent("jellyfin", library_id=library_id, media_type="tv")
    assert result["scope"]["library"]["name"] == "电视剧"
    assert result["scope"]["library"]["id"] == library_id
    assert "__object_id" not in result["scope"]["library"]
    assert calls[-1][1]["params"]["ParentId"] == "tv-library"
    assert calls[-1][1]["params"]["IncludeItemTypes"] == "Series,Episode"
    for query in ({"library_id": "missing-library"}, {"library_name": "不存在"}, {"library_name": "电视剧"}):
        if query.get("library_name") == "电视剧":
            folders.append({"ItemId": "tv-duplicate", "Name": "电视剧"})
        before = len(calls)
        with pytest.raises(media_library.MediaLibraryError) as rejected:
            media_library.recent("jellyfin", **query)
        assert rejected.value.code == "precondition_failed"
        assert len(calls) == before + 1
        assert calls[-1][0] == "/Library/VirtualFolders"
