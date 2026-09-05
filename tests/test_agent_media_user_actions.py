from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import database as db
from app.agent.domain_catalog.catalog import ToolSpecCollector
from app.agent.domain_catalog.library_quality import register_specs
from app.agent.errors import AgentToolError
from app.agent.media_user_actions import (
    execute_media_user,
    preview_media_user,
    read_media_user,
)
from app.agent.models import ToolContext
from app.agent.provider_gateway import ProviderGateway
from app.agent.provider_models import ProviderGatewayError
from app.agent.provider_operations import build_provider_catalog
from app.agent.providers.media_server import MediaServerProviderTransport
from app.modules.media_server_profiles import MediaServerProfile


class MediaClient:
    url = "https://media.invalid"
    timeout = 3

    def __init__(self):
        self.items = {
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
        return "viewer"

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


def state_ref(client):
    return read_media_user(
        client, "viewer", "media.user.inspect", {"item_ref": "movie1"}, source="test"
    ).data["item"]["__object_id"]


def playlist_refs(client):
    data = read_media_user(
        client,
        "viewer",
        "media.playlist.inspect",
        {"playlist_ref": "playlist1"},
        source="test",
    ).data
    return data["playlist"]["__object_id"], data["items"][0]["__object_id"]


def preview(client, operation, arguments, server_type="jellyfin"):
    return preview_media_user(
        client, "viewer", operation, arguments, server_type=server_type, source="test"
    )


def execute(client, operation, arguments, server_type="jellyfin"):
    return execute_media_user(
        client, "viewer", operation, arguments, server_type=server_type, source="test"
    )


@pytest.mark.parametrize(
    ("operation", "field", "before", "expected", "method"),
    [
        ("mark_played", "Played", False, True, "POST"),
        ("mark_unplayed", "Played", True, False, "DELETE"),
        ("favorite", "IsFavorite", False, True, "POST"),
        ("unfavorite", "IsFavorite", True, False, "DELETE"),
    ],
)
@pytest.mark.parametrize("server_type", ["jellyfin", "emby"])
def test_user_state_changes_use_confirmed_scope_and_readback(
    operation, field, before, expected, method, server_type
):
    client = MediaClient()
    client.items["movie1"]["UserData"][field] = before
    args = {"item_ref": state_ref(client)}
    result = preview(client, f"media.user.{operation}", args, server_type)
    assert not client.calls
    assert result.data["before"] is before
    result = execute(client, f"media.user.{operation}", args, server_type)
    assert result.data["value"] is expected
    assert result.data["verification"] == "已回读验证"
    assert client.calls[0][0] == method
    assert client.calls[0][2]["params"] == (
        {"UserId": "viewer"} if server_type == "jellyfin" else None
    )


def test_changed_playback_position_invalidates_unplayed_write():
    client = MediaClient()
    args = {"item_ref": state_ref(client)}
    preview(client, "media.user.mark_played", args)
    client.items["movie1"]["UserData"]["PlaybackPositionTicks"] = 200
    with pytest.raises(ProviderGatewayError, match="状态已变化"):
        execute(client, "media.user.mark_played", args)
    assert not client.calls


def test_favorite_does_not_invalidate_when_only_playback_changes():
    client = MediaClient()
    args = {"item_ref": state_ref(client)}
    client.items["movie1"]["UserData"]["PlaybackPositionTicks"] = 200
    assert execute(client, "media.user.favorite", args).data["value"] is True


def test_already_desired_state_is_idempotent_no_http_write():
    client = MediaClient()
    args = {"item_ref": state_ref(client)}
    assert execute(client, "media.user.mark_unplayed", args).data["affected"] == 0
    assert not client.calls


def test_state_ref_cannot_cross_users():
    client = MediaClient()
    ref = state_ref(client)
    with pytest.raises(ProviderGatewayError, match="用户或快照"):
        execute_media_user(
            client,
            "other",
            "media.user.favorite",
            {"item_ref": ref},
            server_type="jellyfin",
            source="test",
        )
    assert not client.calls


def test_missing_user_data_fails_closed():
    client = MediaClient()
    del client.items["movie1"]["UserData"]["Played"]
    with pytest.raises(ProviderGatewayError, match="缺少用户状态"):
        preview(client, "media.user.mark_played", {"item_ref": state_ref(client)})
    assert not client.calls


def test_mark_series_played_refuses_implicit_bulk_change():
    client = MediaClient()
    client.items["movie1"]["Type"] = "Series"
    with pytest.raises(ProviderGatewayError, match="单个可播放"):
        preview(client, "media.user.mark_played", {"item_ref": state_ref(client)})


def test_accepted_http_without_verified_mutation_never_reports_success():
    client = MediaClient()
    client.apply_write = False
    with pytest.raises(ProviderGatewayError) as error:
        execute(client, "media.user.favorite", {"item_ref": state_ref(client)})
    assert error.value.external_write_possible
    assert error.value.code == "provider_write_outcome_unknown"


def test_playlist_add_skips_existing_and_verifies_membership():
    client = MediaClient()
    playlist, _entry = playlist_refs(client)
    args = {"playlist_ref": playlist, "item_refs": ["movie1", "movie2", "movie2"]}
    assert preview(client, "media.playlist.add_items", args).data["affected"] == 1
    result = execute(client, "media.playlist.add_items", args)
    assert result.data["member_count"] == 2
    assert result.data["affected"] == 1
    assert client.calls[0][2]["params"]["Ids"] == "movie2"


def test_playlist_remove_uses_entry_ids_not_media_ids_and_keeps_files():
    client = MediaClient()
    playlist, entry = playlist_refs(client)
    args = {"playlist_ref": playlist, "entry_refs": [entry]}
    result = execute(client, "media.playlist.remove_items", args)
    assert result.data["delete_media_files"] is False
    assert client.calls[0][2]["params"] == {"EntryIds": "entry1"}
    assert "movie1" in client.items
    assert result.data["member_count"] == 0


def test_playlist_changed_between_preview_and_confirm_stops_write():
    client = MediaClient()
    playlist, entry = playlist_refs(client)
    args = {"playlist_ref": playlist, "entry_refs": [entry]}
    preview(client, "media.playlist.remove_items", args)
    client.members["playlist1"].append(
        {"Id": "movie2", "PlaylistItemId": "new-entry", "Name": "第二部"}
    )
    with pytest.raises(ProviderGatewayError, match="成员或名称已变化"):
        execute(client, "media.playlist.remove_items", args)
    assert not client.calls


def test_playlist_cross_playlist_entry_rejected():
    client = MediaClient()
    playlist, entry = playlist_refs(client)
    args = {
        "playlist_ref": playlist,
        "entry_refs": [
            entry.replace('"playlist":"playlist1"', '"playlist":"playlist2"')
        ],
    }
    with pytest.raises(ProviderGatewayError, match="不属于"):
        preview(client, "media.playlist.remove_items", args)
    assert not client.calls


def test_playlist_missing_entry_identity_refuses_mutation():
    client = MediaClient()
    del client.members["playlist1"][0]["PlaylistItemId"]
    with pytest.raises(ProviderGatewayError):
        playlist_refs(client)


def test_large_playlist_never_treated_as_complete_snapshot():
    client = MediaClient()
    client.members["playlist1"] = [
        {"Id": f"m{i}", "Name": str(i), "PlaylistItemId": f"e{i}"} for i in range(251)
    ]
    data = read_media_user(
        client,
        "viewer",
        "media.playlist.inspect",
        {"playlist_ref": "playlist1"},
        source="test",
    ).data
    assert data["playlist"]["can_modify"] is False
    assert data["playlist"]["__object_kind"] == "media_playlist"
    assert data["playlist"]["member_count"] == 251
    assert data["next_start_index"] == 20


def test_playlist_members_display_is_paged_even_though_snapshot_complete():
    client = MediaClient()
    client.members["playlist1"] = [
        {"Id": f"m{i}", "Name": str(i), "PlaylistItemId": f"e{i}"} for i in range(40)
    ]
    result = read_media_user(
        client,
        "viewer",
        "media.playlist.inspect",
        {"playlist_ref": "playlist1", "start_index": 20, "limit": 10},
        source="test",
    )
    assert len(result.data["items"]) == 10
    assert result.data["next_start_index"] == 30
    assert result.data["playlist"]["member_count"] == 40


def test_playlist_creation_is_private_user_bound_and_readback_verified():
    client = MediaClient()
    result = execute(
        client,
        "media.playlist.create",
        {"name": "新片单", "item_refs": ["movie1", "movie2"]},
    )
    assert result.data["member_count"] == 2
    assert client.calls[0][2]["json"]["IsPublic"] is False
    assert client.calls[0][2]["json"]["UserId"] == "viewer"


def test_existing_name_prevents_duplicate_playlist_creation():
    client = MediaClient()
    with pytest.raises(ProviderGatewayError, match="同名"):
        preview(
            client,
            "media.playlist.create",
            {"name": "周末看片", "item_refs": ["movie1"]},
        )
    assert not client.calls


def test_emby_creation_refused_when_api_key_cannot_guarantee_owner():
    client = MediaClient()
    with pytest.raises(ProviderGatewayError, match="所有者"):
        preview(
            client,
            "media.playlist.create",
            {"name": "新片单", "item_refs": ["movie1"]},
            "emby",
        )
    assert not client.calls


@pytest.fixture
def gateway_env(tmp_path, monkeypatch):
    previous_path = db.DB_PATH
    previous_mode = bool(getattr(db, "_configured_test_mode", False))
    db.configure_database(tmp_path / "media-management.db", test_mode=True)
    db.init_db()
    client = MediaClient()
    profile = MediaServerProfile(
        "configured:jellyfin",
        "jellyfin",
        "Jellyfin",
        "https://media.invalid",
        "secret",
        True,
        "viewer",
    )
    monkeypatch.setattr(
        "app.agent.providers.media_server.list_configured_profiles", lambda: [profile]
    )
    transport = MediaServerProviderTransport()
    monkeypatch.setattr(transport, "_client", lambda _profile: client)
    gateway = ProviderGateway(catalog=build_provider_catalog(), transports=[transport])
    context = ToolContext(owner="test:viewer", session_id="session-a")
    _, data = gateway.artifacts.put(
        owner=context.owner,
        session_id=context.session_id,
        provider="media",
        profile_ref=profile.source,
        operation="media.items.search",
        data={
            "items": [
                {
                    "__object_id": "movie1",
                    "__object_kind": "media_item",
                    "name": "示例电影",
                }
            ]
        },
    )
    try:
        yield gateway, client, context, data["items"][0]["object_ref"]
    finally:
        db.configure_database(previous_path, test_mode=previous_mode)


def test_gateway_freezes_state_and_confirm_survives_artifact_store_clear(gateway_env):
    gateway, client, context, media_ref = gateway_env
    state = gateway.query(
        profile_ref="configured:jellyfin",
        operation="media.user.inspect",
        arguments={"item_ref": media_ref},
        context=context,
    )
    user_ref = state.data["item"]["object_ref"]
    assert "viewer" not in str(state.to_dict())
    assert "movie1" not in str(state.to_dict())
    preview_result = gateway.preview_change(
        profile_ref="configured:jellyfin",
        operation="media.user.favorite",
        arguments={"item_ref": user_ref},
        context=context,
    )
    plan_ref = preview_result.data["plan_ref"]
    _result, fingerprint = gateway.prepare_change_execution(
        plan_ref=plan_ref, context=context
    )
    gateway.artifacts.clear()
    assert not client.calls
    result = gateway.execute_change(
        plan_ref=plan_ref, expected_context=fingerprint, context=context
    )
    assert result.ok and result.data["status"] == "succeeded"
    assert len(client.calls) == 1
    with pytest.raises(AgentToolError, match="已经执行"):
        gateway.execute_change(
            plan_ref=plan_ref, expected_context=fingerprint, context=context
        )
    assert len(client.calls) == 1


def test_gateway_rejects_cross_session_and_wrong_kind_references(gateway_env):
    gateway, _client, context, media_ref = gateway_env
    with pytest.raises(ProviderGatewayError):
        gateway.query(
            profile_ref="configured:jellyfin",
            operation="media.user.inspect",
            arguments={"item_ref": media_ref},
            context=ToolContext(owner=context.owner, session_id="different"),
        )
    with pytest.raises(ProviderGatewayError):
        gateway.preview_change(
            profile_ref="configured:jellyfin",
            operation="media.user.favorite",
            arguments={"item_ref": media_ref},
            context=context,
        )


def test_atomic_domain_tools_have_single_confirmation_without_public_prepare(
    gateway_env, monkeypatch
):
    gateway, client, context, media_ref = gateway_env
    monkeypatch.setattr(
        "app.agent.domain_catalog.library_quality.get_provider_gateway", lambda: gateway
    )
    collector = ToolSpecCollector()
    register_specs(collector)
    specs = {spec.name: spec for spec in collector.items}
    inspected = specs["media.user.inspect"].context_handler(
        {"profile_ref": "configured:jellyfin", "item_ref": media_ref}, context
    )
    favorite = specs["media.user.favorite"]
    args = favorite.validator(
        {
            "profile_ref": "configured:jellyfin",
            "item_ref": inspected.data["item"]["object_ref"],
        }
    )
    result, fingerprint = favorite.context_confirmation_preparer(args, context)
    assert result.ok and not client.calls
    result = favorite.context_confirmed_handler(args, fingerprint, context)
    assert result.ok and len(client.calls) == 1


def test_media_operation_budget_prevents_unbounded_playlist_preflight(monkeypatch):
    from app.agent.media_user_actions import _BoundedMediaClient

    client = MediaClient()
    now = [100.0]
    monkeypatch.setattr("app.agent.media_user_actions.time.monotonic", lambda: now[0])
    bounded = _BoundedMediaClient(client)
    bounded._request("/Users/viewer/Items/movie1")
    now[0] += 21
    with pytest.raises(ProviderGatewayError, match="预算"):
        bounded._request("/Users/viewer/Items/movie2")
    assert len(client.read_calls) == 1


def test_new_playlist_public_privacy_readback_is_not_accepted():
    client = MediaClient()
    original = client._request

    def read(path, params=None, *, timeout=None):
        if path == "/Playlists/created1":
            return {"OpenAccess": True}
        return original(path, params=params, timeout=timeout)

    client._request = read
    with pytest.raises(ProviderGatewayError) as error:
        execute(
            client, "media.playlist.create", {"name": "新片单", "item_refs": ["movie1"]}
        )
    assert error.value.external_write_possible
    assert len(client.calls) == 1


def test_playlist_write_noop_http_cannot_fake_verified_success():
    client = MediaClient()
    playlist, _entry = playlist_refs(client)
    client.apply_write = False
    with pytest.raises(ProviderGatewayError) as error:
        execute(
            client,
            "media.playlist.add_items",
            {"playlist_ref": playlist, "item_refs": ["movie2"]},
        )
    assert error.value.external_write_possible
    assert len(client.calls) == 1


def test_create_playlist_requires_explicit_configured_user(gateway_env, monkeypatch):
    gateway, client, context, media_ref = gateway_env
    profile = MediaServerProfile(
        "configured:jellyfin",
        "jellyfin",
        "Jellyfin",
        "https://media.invalid",
        "secret",
        True,
        "",
    )
    monkeypatch.setattr(
        "app.agent.providers.media_server.list_configured_profiles", lambda: [profile]
    )
    with pytest.raises(ProviderGatewayError, match="明确配置"):
        gateway.preview_change(
            profile_ref=profile.source,
            operation="media.playlist.create",
            arguments={"name": "新列表", "item_refs": [media_ref]},
            context=context,
        )
    assert not client.calls


@pytest.mark.parametrize(
    "operation", ["media.playlists.list", "media.playlist.inspect"]
)
def test_empty_page_with_remaining_total_is_not_an_infinite_cursor(operation):
    client = MediaClient()
    original = client._request

    def read(path, params=None, *, timeout=None):
        if path == "/Items" or path.endswith("/Items"):
            return {"Items": [], "TotalRecordCount": 5}
        return original(path, params=params, timeout=timeout)

    client._request = read
    with pytest.raises(ProviderGatewayError, match="分页"):
        read_media_user(
            client, "viewer", operation, {"playlist_ref": "playlist1"}, source="test"
        )
