"""媒体用户公共服务/SDK的HTTP业务边界；只使用隔离数据库和客户端替身。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from app import sdk
from app.modules import media_library, media_user
from app.modules.media_library import MediaLibraryError
from app.modules.media_server_profiles import MediaServerProfile
from tests.support import initialized_web_runtime


class MediaClient:
    url = "https://media.invalid"
    timeout = 3

    def __init__(self):
        self.user_id = "viewer"
        self.items: dict[str, dict[str, Any]] = {
            "movie1": {
                "Id": "movie1",
                "Name": "示例电影",
                "Type": "Movie",
                "UserData": {
                    "Played": False,
                    "IsFavorite": False,
                    "PlayCount": 0,
                    "PlaybackPositionTicks": 0,
                },
            },
            "movie2": {
                "Id": "movie2",
                "Name": "第二部",
                "Type": "Movie",
                "UserData": {"Played": False, "IsFavorite": False},
            },
            "playlist1": {"Id": "playlist1", "Name": "周末看片", "Type": "Playlist"},
        }
        self.members = {
            "playlist1": [
                {"Id": "movie1", "Name": "示例电影", "PlaylistItemId": "entry1"}
            ]
        }
        self.calls = []
        self.read_calls = []
        self.apply_write = True
        self._session = SimpleNamespace(request=self.request)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def _headers(self):
        return {"Authorization": "secret"}

    def _user_id(self):
        return self.user_id

    def _request(self, path, params=None, *, timeout=None):
        self.read_calls.append((path, deepcopy(params)))
        params = params or {}
        if path.startswith("/Users/"):
            return deepcopy(self.items.get(path.rsplit("/", 1)[-1], {}))
        if path.startswith("/Playlists/") and not path.endswith("/Items"):
            return {"OpenAccess": False}
        if path.startswith("/Playlists/"):
            values = self.members[path.split("/")[2]]
            start, limit = params.get("StartIndex", 0), params.get("Limit", 20)
            return {
                "Items": deepcopy(values[start : start + limit]),
                "TotalRecordCount": len(values),
            }
        if path == "/Items":
            items = [row for row in self.items.values() if row["Type"] == "Playlist"]
            if params.get("SearchTerm"):
                items = [row for row in items if params["SearchTerm"] in row["Name"]]
            start, limit = params.get("StartIndex", 0), params.get("Limit", 20)
            return {
                "Items": deepcopy(items[start : start + limit]),
                "TotalRecordCount": len(items),
            }
        raise AssertionError(path)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, deepcopy(kwargs)))
        path = url.removeprefix(self.url)
        data = None
        if self.apply_write:
            if "PlayedItems/" in path or "FavoriteItems/" in path:
                field = "Played" if "PlayedItems/" in path else "IsFavorite"
                self.items[path.rsplit("/", 1)[-1]]["UserData"][field] = (
                    method == "POST"
                )
            elif path == "/Playlists":
                body = kwargs["json"]
                data = {"Id": "created1"}
                self.items["created1"] = {
                    "Id": "created1",
                    "Name": body["Name"],
                    "Type": "Playlist",
                }
                self.members["created1"] = [
                    {
                        "Id": value,
                        "Name": self.items[value]["Name"],
                        "PlaylistItemId": f"created-entry{index}",
                    }
                    for index, value in enumerate(body["Ids"])
                ]
            elif path.startswith("/Playlists/"):
                values = self.members[path.split("/")[2]]
                if method == "POST":
                    for index, item_id in enumerate(kwargs["params"]["Ids"].split(",")):
                        values.append(
                            {
                                "Id": item_id,
                                "Name": self.items[item_id]["Name"],
                                "PlaylistItemId": f"added{index}",
                            }
                        )
                elif method == "DELETE":
                    entries = kwargs["params"]["EntryIds"].split(",")
                    values[:] = [
                        row for row in values if row["PlaylistItemId"] not in entries
                    ]
        response = Mock(content=b"{}" if data is not None else b"")
        response.json.return_value = data
        return response


@pytest.fixture
def backend(monkeypatch):
    with initialized_web_runtime() as paths:
        client = MediaClient()
        profiles = [MediaServerProfile("configured:jellyfin", "jellyfin", "Jellyfin",
                                       client.url, "secret", True, "viewer")]
        monkeypatch.setattr(media_library, "list_configured_profiles", lambda: profiles)
        monkeypatch.setattr(MediaServerProfile, "create_client", lambda _profile: client)
        yield SimpleNamespace(client=client, profiles=profiles, paths=paths)


def prepare(backend, operation, **arguments):
    return media_user.prepare_change(operation, server=backend.profiles[0].server_type, **arguments)["snapshot"]


@pytest.mark.parametrize("server", ["jellyfin", "emby"])
@pytest.mark.parametrize("operation,field,desired", [
    ("set_played", "Played", True), ("set_played", "Played", False),
    ("set_favorite", "IsFavorite", True), ("set_favorite", "IsFavorite", False),
])
def test_user_state_writes_keep_backend_endpoints_scope_and_readback(backend, server, operation, field, desired):
    backend.profiles[0] = replace(backend.profiles[0], server_type=server, source=f"configured:{server}")
    client = backend.client
    client.items["movie1"]["UserData"][field] = not desired
    before = sdk.library.user_state("movie1", server=server)
    assert before["item"]["id"] == "movie1" and before["scope"]["user_id"] == "viewer"
    snapshot = prepare(backend, operation, item_id="movie1", value=desired)
    result = media_user.execute_change(snapshot)
    assert result["status"] == "completed" and result["data"]["value"] is desired
    endpoint = "PlayedItems" if field == "Played" else "FavoriteItems"
    expected = f"/User{endpoint}/movie1" if server == "jellyfin" else f"/Users/viewer/{endpoint}/movie1"
    assert client.calls[0][0] == ("POST" if desired else "DELETE")
    assert client.calls[0][1] == client.url + expected
    assert client.calls[0][2]["params"] == ({"UserId": "viewer"} if server == "jellyfin" else None)
    assert result["data"]["item"]["state"][field] is desired


@pytest.mark.parametrize("key,value", [("Played", True), ("PlayCount", 2), ("PlaybackPositionTicks", 123), ("LastPlayedDate", "2026-11-05")])
def test_played_confirmation_rejects_changed_playback_state(backend, key, value):
    snapshot = prepare(backend, "set_played", item_id="movie1", value=True)
    backend.client.items["movie1"]["UserData"][key] = value
    with pytest.raises(MediaLibraryError) as error:
        media_user.execute_change(snapshot)
    assert error.value.code == "confirmation_stale" and not backend.client.calls


def test_favorite_ignores_playback_changes_but_preserves_other_user_state(backend):
    snapshot = prepare(backend, "set_favorite", item_id="movie1", value=True)
    backend.client.items["movie1"]["UserData"]["PlaybackPositionTicks"] = 1200
    result = media_user.execute_change(snapshot)
    assert result["data"]["value"] is True
    assert result["data"]["item"]["state"]["PlaybackPositionTicks"] == 1200


def test_noop_does_not_write_and_unknown_state_is_not_guessed(backend):
    result = media_user.execute_change(prepare(backend, "set_played", item_id="movie1", value=False))
    assert result["data"]["affected"] == 0 and not backend.client.calls
    del backend.client.items["movie1"]["UserData"]["Played"]
    assert sdk.library.user_state("movie1")["item"]["played"] is None
    with pytest.raises(MediaLibraryError) as error:
        prepare(backend, "set_played", item_id="movie1", value=True)
    assert error.value.code == "invalid_response" and not backend.client.calls


def test_series_played_and_playlist_video_expansion_are_refused(backend):
    backend.client.items["movie1"]["Type"] = "Series"
    for operation, arguments in [("set_played", {"item_id": "movie1", "value": True}),
                                  ("create_playlist", {"name": "新片单", "item_ids": ["movie1"]}),
                                  ("add_to_playlist", {"playlist_id": "playlist1", "item_ids": ["movie2"]})]:
        backend.client.items["movie2"]["Type"] = "Series"
        with pytest.raises(MediaLibraryError) as error:
            prepare(backend, operation, **arguments)
        assert error.value.code == "precondition_failed"
    assert not backend.client.calls


def test_playlist_add_skips_existing_and_returns_full_members(backend):
    result = media_user.execute_change(prepare(backend, "add_to_playlist", playlist_id="playlist1", item_ids=["movie1", "movie2"]))
    data = result["data"]
    assert data["affected"] == 1 and data["member_count"] == 2
    assert backend.client.calls[0][2]["params"]["Ids"] == "movie2"
    assert {row["item_id"] for row in data["items"]} == {"movie1", "movie2"}
    assert {row["entry_id"] for row in data["items"]} == {"entry1", "added0"}
    assert data["delete_media_files"] is False and data["may_be_shared"] is True


def test_playlist_remove_uses_entry_ids_and_preserves_duplicate_media_and_files(backend):
    backend.client.members["playlist1"].append({"Id": "movie1", "Name": "同媒体另一成员", "PlaylistItemId": "entry2"})
    snapshot = prepare(backend, "remove_from_playlist", playlist_id="playlist1", entry_ids=["entry1"])
    result = media_user.execute_change(snapshot)
    assert backend.client.calls[0][2]["params"] == {"EntryIds": "entry1"}
    assert result["data"]["items"] == [{"item_id": "movie1", "entry_id": "entry2", "name": "同媒体另一成员"}]
    assert "movie1" in backend.client.items and result["data"]["delete_media_files"] is False


def test_wrong_duplicate_entry_removal_is_unknown_even_if_media_counts_match(backend):
    backend.client.members["playlist1"].append({"Id": "movie1", "Name": "同媒体另一成员", "PlaylistItemId": "entry2"})
    snapshot = prepare(backend, "remove_from_playlist", playlist_id="playlist1", entry_ids=["entry1"])
    original = backend.client._session.request
    def wrong_entry(method, url, **kwargs):
        kwargs["params"] = {"EntryIds": "entry2"}
        return original(method, url, **kwargs)
    backend.client._session.request = wrong_entry
    result = media_user.execute_change(snapshot)
    assert result["status"] == "outcome_unknown" and len(backend.client.calls) == 1
    assert "movie1" in backend.client.items


@pytest.mark.parametrize("change", ["name", "members", "entry", "privacy"])
def test_playlist_snapshot_cas_rejects_concurrent_change(backend, change):
    snapshot = prepare(backend, "remove_from_playlist", playlist_id="playlist1", entry_ids=["entry1"])
    client = backend.client
    if change == "name":
        client.items["playlist1"]["Name"] = "改名"
    elif change == "privacy":
        client.items["playlist1"]["IsPublic"] = True
    elif change == "entry":
        client.members["playlist1"][0]["PlaylistItemId"] = "replacement"
    else:
        client.members["playlist1"].append({"Id": "movie2", "Name": "第二部", "PlaylistItemId": "entry2"})
    with pytest.raises(MediaLibraryError) as error:
        media_user.execute_change(snapshot)
    assert error.value.code == "confirmation_stale" and not client.calls


@pytest.mark.parametrize("entries", [["movie1"], ["foreign-playlist-entry"]])
def test_media_ids_or_foreign_entries_cannot_remove_current_playlist_members(backend, entries):
    with pytest.raises(MediaLibraryError) as error:
        prepare(backend, "remove_from_playlist", playlist_id="playlist1", entry_ids=entries)
    assert error.value.code == "confirmation_stale" and not backend.client.calls


@pytest.mark.parametrize("malformed", ["missing", "duplicate"])
def test_playlist_entry_identity_is_required_and_unique(backend, malformed):
    rows = backend.client.members["playlist1"]
    if malformed == "missing":
        del rows[0]["PlaylistItemId"]
    else:
        rows.append(dict(rows[0]))
    with pytest.raises(MediaLibraryError):
        sdk.library.playlist("playlist1")
    assert not backend.client.calls


def test_large_playlist_read_has_true_ids_cursor_and_no_modification_snapshot(backend):
    backend.client.members["playlist1"] = [{"Id": f"m{i}", "Name": str(i), "PlaylistItemId": f"e{i}"} for i in range(251)]
    first = sdk.library.playlist("playlist1")
    assert len(first["items"]) == 250 and first["total"] == 251 and first["next_offset"] == 250
    assert first["can_modify"] is False
    last = sdk.library.playlist("playlist1", offset=first["next_offset"])
    assert last["items"] == [{"item_id": "m250", "entry_id": "e250", "name": "250"}]
    assert last["has_more"] is False
    with pytest.raises(MediaLibraryError) as error:
        prepare(backend, "remove_from_playlist", playlist_id="playlist1", entry_ids=["e0"])
    assert error.value.code == "provider_scope_too_large" and not backend.client.calls


def test_complete_member_read_and_receipt_are_not_limited_to_provider_display_page(backend):
    backend.client.members["playlist1"] = [{"Id": f"m{i}", "Name": str(i), "PlaylistItemId": f"e{i}"} for i in range(40)]
    full = sdk.library.playlist("playlist1")
    assert len(full["items"]) == 40 and full["can_modify"] is True
    page = sdk.library.playlist("playlist1", offset=20, limit=10)
    assert len(page["items"]) == 10 and page["next_offset"] == 30 and page["total"] == 40
    result = media_user.execute_change(prepare(backend, "add_to_playlist", playlist_id="playlist1", item_ids=["movie2"]))
    assert len(result["data"]["items"]) == result["data"]["member_count"] == 41


def test_create_private_playlist_is_user_bound_and_verified(backend):
    result = media_user.execute_change(prepare(backend, "create_playlist", name="新片单", item_ids=["movie1", "movie2"]))
    assert result["data"]["playlist_id"] == "created1" and len(result["data"]["items"]) == 2
    assert result["data"]["may_be_shared"] is False
    assert backend.client.calls[0][2]["json"] == {"Name": "新片单", "Ids": ["movie1", "movie2"],
                                                   "UserId": "viewer", "MediaType": "Video", "IsPublic": False}


@pytest.mark.parametrize("failure", ["existing_name", "emby", "default_user"])
def test_create_requires_unique_name_supported_private_owner_and_configured_user(backend, failure):
    if failure == "emby":
        backend.profiles[0] = replace(backend.profiles[0], server_type="emby", source="configured:emby")
    elif failure == "default_user":
        backend.profiles[0] = replace(backend.profiles[0], user_id="")
    with pytest.raises(MediaLibraryError) as error:
        prepare(backend, "create_playlist", name="周末看片" if failure == "existing_name" else "新片单", item_ids=["movie1"])
    assert error.value.code == {"existing_name": "provider_conflict", "emby": "provider_version_unsupported", "default_user": "provider_user_required"}[failure]
    assert not backend.client.calls


@pytest.mark.parametrize("privacy", [{"OpenAccess": True}, {"OpenAccess": False, "OwnerUserId": "foreign"}])
def test_create_cannot_claim_success_when_privacy_or_returned_owner_disagrees(backend, privacy):
    snapshot = prepare(backend, "create_playlist", name="新片单", item_ids=["movie1"])
    original = backend.client._request
    backend.client._request = lambda path, *a, **kw: privacy if path == "/Playlists/created1" else original(path, *a, **kw)
    result = media_user.execute_change(snapshot)
    assert result["status"] == "outcome_unknown" and result["data"]["playlist_id"] == "created1"
    assert result["data"]["execution_started"] and len(backend.client.calls) == 1


def test_http_noop_is_not_verified_success(backend):
    snapshot = prepare(backend, "add_to_playlist", playlist_id="playlist1", item_ids=["movie2"])
    backend.client.apply_write = False
    result = media_user.execute_change(snapshot)
    assert result["status"] == "outcome_unknown" and len(backend.client.calls) == 1


@pytest.mark.parametrize("change", ["profile", "configured_user", "default_user"])
def test_original_profile_and_user_are_frozen(backend, change):
    if change == "default_user":
        backend.profiles[0] = replace(backend.profiles[0], user_id="")
    snapshot = prepare(backend, "set_favorite", item_id="movie1", value=True)
    if change == "profile":
        backend.profiles[0] = replace(backend.profiles[0], credential="changed")
    elif change == "configured_user":
        backend.profiles[0] = replace(backend.profiles[0], user_id="other")
    else:
        backend.client.user_id = "other"
    with pytest.raises(MediaLibraryError) as error:
        media_user.execute_change(snapshot)
    assert error.value.code == "confirmation_stale" and not backend.client.calls


@pytest.mark.parametrize("method", ["playlists", "playlist"])
def test_empty_page_with_remaining_total_is_not_an_infinite_cursor(backend, method):
    original = backend.client._request
    backend.client._request = lambda path, *a, **kw: {"Items": [], "TotalRecordCount": 5} if path == "/Items" or path.endswith("/Items") else original(path, *a, **kw)
    with pytest.raises(MediaLibraryError) as error:
        sdk.METHODS["library." + method](**({"playlist_id": "playlist1"} if method == "playlist" else {}))
    assert error.value.code == "provider_snapshot_changed"


def test_playlist_unknown_counts_and_public_ownership_are_not_inferred(backend):
    backend.client.items["playlist1"].update(IsPublic=True, OwnerUserId="other-owner")
    result = sdk.library.playlists()
    assert result["items"][0] == {"id": "playlist1", "name": "周末看片", "is_public": True, "owner_user_id": "other-owner"}
    original = backend.client._request
    def unknown(path, *a, **kw):
        value = original(path, *a, **kw)
        if path == "/Items":
            value.pop("TotalRecordCount")
        return value
    backend.client._request = unknown
    result = sdk.library.playlists()
    assert result["total"] is None and result["complete"] is False
    assert result["scope"]["user_id"] == "viewer"


def test_shared_operation_budget_and_cancellation_stop_before_write(backend, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(media_user.time, "monotonic", lambda: clock[0])
    original = backend.client._request
    def late(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] += 21
        return result
    backend.client._request = late
    with pytest.raises(MediaLibraryError) as error:
        prepare(backend, "add_to_playlist", playlist_id="playlist1", item_ids=["movie2"])
    assert error.value.code == "upstream_timeout"
    assert len(backend.client.read_calls) == 1 and not backend.client.calls
