"""原生观看读取：只替换HTTP边界，真实SDK、服务、用户选择和客户端解析。"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from urllib.parse import urlsplit

import pytest
import requests

from app import sdk
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.state import InMemorySessionStateStore
from app.clients.base import MediaRecommendationInventory
from app.modules import media_library
from app.modules.media_server_profiles import MediaServerProfile
from tests.support import isolated_test_database
from tests.test_agent_code_sandbox import make_context


@pytest.fixture
def native(monkeypatch):
    profiles = []
    calls = []
    responses = {}

    def get(_session, url, **kwargs):
        path = urlsplit(url).path
        params = kwargs.get("params") or {}
        calls.append((path, params))
        payload = responses[path]
        if callable(payload):
            payload = payload(params)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        return response

    def add(server="jellyfin", user="viewer"):
        profile = MediaServerProfile(f"configured:{server}", server, server.title(),
                                     f"http://{server}.invalid", "private-playback-token", True, user)
        profiles.append(profile)
        return profile

    monkeypatch.setattr(media_library, "list_configured_profiles", lambda: profiles)
    monkeypatch.setattr(requests.Session, "get", get)
    with isolated_test_database():
        yield add, profiles, responses, calls


def media(item_id="episode-1", **changes):
    return {"Id": item_id, "Name": "同名集", "Type": "Episode", "SeriesId": "real-series",
            "SeriesName": "真实剧名", "IndexNumber": 3, "ParentIndexNumber": 2,
            "Overview": "完整简介" * 40, "Genres": [f"题材{i}" for i in range(12)],
            "UserData": {"LastPlayedDate": "2026-09-03T10:00:00Z", "PlayedPercentage": 42.5},
            **changes}


@pytest.mark.parametrize("server", ["jellyfin", "emby"])
@pytest.mark.parametrize("total,offset,limit", [
    (40, 0, 250), (40, 20, 10), (1000, 700, 10),
    (0, 0, 250), (40, 60, 10), (1000, 1200, 10),
])
def test_playlist_reads_only_requested_member_page(native, server, total, offset, limit):
    add, _, responses, calls = native
    add(server)
    scanned = []
    responses["/Users/viewer/Items/playlist"] = {"Id": "playlist", "Type": "Playlist", "Name": "片单"}

    def page(params):
        items = [{"Id": f"m{i // 2}", "PlaylistItemId": f"e{i}", "Name": f"条目{i}"}
                 for i in range(params["StartIndex"], min(total, params["StartIndex"] + params["Limit"]))]
        scanned.append(len(items))
        return {"Items": items, "TotalRecordCount": total}

    responses["/Playlists/playlist/Items"] = page
    result = sdk.library.playlist("playlist", server=server, offset=offset, limit=limit)
    assert result["items"] == [{"item_id": f"m{i // 2}", "entry_id": f"e{i}", "name": f"条目{i}"}
                               for i in range(offset, min(total, offset + limit))]
    reads = [params for path, params in calls if path == "/Playlists/playlist/Items"]
    assert scanned == [len(result["items"])]
    complete = offset == 0 and total <= limit
    assert result["playlist"]["complete"] is complete and result["can_modify"] is complete
    assert result["total"] == total and result["scope"]["user_id"] == "viewer"
    assert all(params["UserId"] == "viewer" for params in reads)
    assert result["next_offset"] == (offset + limit if offset + limit < total else None)


@pytest.mark.parametrize("server,configured", [("jellyfin", True), ("jellyfin", False), ("emby", True), ("emby", False)])
def test_history_and_resume_keep_user_scope_real_ids_and_full_facts(native, server, configured):
    add, _, responses, calls = native
    add(server, "viewer" if configured else "")
    responses["/Users"] = [{"Id": "other"}, {"Id": "viewer", "Policy": {"IsAdministrator": True}}]
    responses["/System/ActivityLog/Entries"] = {"Items": [
        {"UserId": "other", "Type": "VideoPlayback", "ItemId": "foreign", "Date": "2026-09-04"},
        {"UserId": "viewer", "Type": "VideoPlayback", "ItemId": "episode-1", "Date": "2026-09-03"},
    ]}
    responses["/Items"] = {"Items": [media()]}
    responses["/Users/viewer/Items"] = {"Items": [media(), media("unplayed", UserData={})]}
    responses["/Users/viewer/Items/Resume"] = {"Items": [media("resume-1")]}
    history = sdk.METHODS["library.history"](server=server, limit=2)
    resume = sdk.METHODS["library.resume"](server=server, limit=2)
    assert history["history_kind"] == "最近播放" and resume["history_kind"] == "继续观看"
    for result, item_id in ((history, "episode-1"), (resume, "resume-1")):
        assert result["count"] == result["returned"] == 1
        assert result["status"] == "success" and result["partial"] is False
        assert result["total"] is result["complete"] is result["has_more"] is None
        assert result["scope"] == {"kind": "user", "profile": f"configured:{server}", "user_id": "viewer"}
        assert result["user_selection"] == ("配置用户" if configured else "服务器默认用户")
        item = result["items"][0]
        assert item["id"] == item_id and item["series_id"] == "real-series"
        assert item["episode_number"] == 3 and item["season_number"] == 2
        assert item["overview"] == "完整简介" * 40 and len(item["genres"]) == 12
        assert item["open_url"].endswith(f"id={item_id}")
        assert "private-playback-token" not in json.dumps(result)
    assert history["items"][0]["last_played"]
    assert resume["items"][0]["progress"] == 42.5
    assert ("/Users" in [path for path, _ in calls]) is (not configured)


@pytest.mark.parametrize("method", ["history", "resume"])
def test_empty_playback_window_is_empty_with_unknown_collection_size(native, method):
    add, _, responses, calls = native
    add()
    responses["/System/ActivityLog/Entries"] = {"Items": []}
    responses["/Users/viewer/Items/Resume"] = {"Items": []}
    result = sdk.METHODS[f"library.{method}"](limit=8)
    assert result["status"] == "empty" and result["partial"] is False
    assert result["items"] == [] and result["count"] == result["returned"] == 0
    assert result["total"] is result["has_more"] is result["complete"] is None
    assert result["limit"] == 8 and result["scope"]["user_id"] == "viewer"
    assert calls


@pytest.mark.parametrize("server", ["jellyfin", "emby"])
@pytest.mark.parametrize("total_known", [True, False])
def test_recommendation_ranks_across_real_client_pages_without_secondary_truncation(native, server, total_known):
    add, _, responses, calls = native
    add(server)
    responses["/System/ActivityLog/Entries"] = {"Items": []}
    responses["/System/Info/Public"] = {"ProductName": server, "Version": "12.0"}
    candidates = [media(str(i), Name="同名电影", Type="Movie", CommunityRating=7,
                        UserData={}, Overview="完整简介" * 150, Tags=[f"标签{j}" for j in range(24)],
                        Studios=[{"Name": "真实工作室"}], ProductionLocations=["日本"],
                        ProviderIds={"Tmdb": str(i + 1)}) for i in range(205)]
    candidates[-1]["CommunityRating"] = 9.5

    def page(params):
        if params.get("SortBy") == "DatePlayed":
            return {"Items": []}
        assert params["IncludeItemTypes"] == "Movie"
        offset, limit = params["StartIndex"], params["Limit"]
        payload: dict[str, object] = {"Items": candidates[offset:offset + limit]}
        if total_known:
            payload["TotalRecordCount"] = 205
        return payload

    responses["/Users/viewer/Items"] = page
    result = sdk.METHODS["library.recommend"](server=server, media_type="movie", must_match=["题材0|Genre0"], limit=20)
    assert result["criteria"]["must_match"] == ["题材0|Genre0"]
    assert result["items"][0]["id"] == "204"
    assert result["inventory_total"] == result["considered_count"] == result["matched_count"] == 205
    assert result["count"] == len(result["items"]) == 20
    assert len({item["id"] for item in result["items"]}) == 20
    assert result["status"] == "success" and not result["inventory_truncated"]
    assert result["partial"] is False and result["inventory_total_exact"] is True
    item = result["items"][0]
    assert item["overview"] == "完整简介" * 150
    assert len(item["genres"]) == 12 and len(item["tags"]) == 24
    assert item["studios"] == ["真实工作室"] and item["production_locations"] == ["日本"]
    assert item["tmdb_id"] == "205" and item["watched"] is False
    assert [params["StartIndex"] for _, params in calls if params.get("SortBy") == "SortName"] == [0, 200]
    assert result["scope"]["media_type"] == "movie" and result["scope"]["user_id"] == "viewer"
    assert result["history"]["records_used"] == 0 and result["history"]["total"] is None


@pytest.mark.parametrize("server", ["jellyfin", "emby"])
@pytest.mark.parametrize("exclude_played", [True, False])
def test_recommendation_does_not_treat_namesakes_as_watched(native, server, exclude_played):
    add, _, responses, _ = native
    add(server)
    history = [
        media("seen-movie", Name="魔女嘉莉", Type="Movie", ProductionYear=1976, SeriesId="", SeriesName=""),
        media("episode", Name="第一集", SeriesId="seen-series", SeriesName="同名作品"),
    ]
    candidates = [
        {"Id": identity, "Name": name, "Type": kind, "ProductionYear": year,
         "CommunityRating": 8, "Genres": ["剧情"], "UserData": {"Played": identity == "played"}}
        for identity, name, kind, year in [
            ("seen-movie", "魔女嘉莉", "Movie", 1976),
            ("remake", "魔女嘉莉", "Movie", 2013),
            ("seen-series", "同名作品", "Series", 2020),
            ("namesake-movie", "同名作品", "Movie", 2020),
            ("played", "已看作品", "Movie", 2024),
        ]
    ]
    responses["/System/Info/Public"] = {"ProductName": server, "Version": "12.0"}
    responses["/System/ActivityLog/Entries"] = {"Items": [
        {"UserId": "viewer", "Type": "VideoPlayback", "ItemId": item["Id"], "Date": "2026-09-03"}
        for item in history]}
    responses["/Items"] = {"Items": history}
    responses["/Users/viewer/Items"] = lambda params: (
        {"Items": history} if params.get("SortBy") == "DatePlayed" else
        {"Items": candidates, "TotalRecordCount": len(candidates)})
    result = sdk.library.recommend(server=server, exclude_played=exclude_played)
    expected = {"remake", "namesake-movie"} if exclude_played else {item["Id"] for item in candidates}
    assert {item["id"] for item in result["items"]} == expected
    assert result["excluded"]["played_or_started"] == (3 if exclude_played else 0)
    assert result["matched_count"] == result["count"] == len(expected)
    assert result["history"]["unique_works_used"] == 2
    assert result["status"] == "success" and result["partial"] is False


@pytest.mark.parametrize("server", ["jellyfin", "emby"])
@pytest.mark.parametrize("failure,total", [("timeout", 205), ("error_json", None), ("error_json", 205)])
def test_recommendation_page_failure_is_not_an_empty_or_successful_inventory(native, server, failure, total):
    add, _, responses, calls = native
    add(server)
    responses["/System/ActivityLog/Entries"] = {"Items": []}

    def page(params):
        if params.get("SortBy") == "DatePlayed":
            return {"Items": []}
        if params["StartIndex"]:
            if failure == "timeout":
                raise TimeoutError("private-page-detail")
            return {"error": "private-page-detail"}
        payload: dict[str, object] = {"Items": [media(str(i), Type="Movie", CommunityRating=7, UserData={}) for i in range(200)]}
        if total is not None:
            payload["TotalRecordCount"] = total
        return payload

    responses["/Users/viewer/Items"] = page
    with pytest.raises(media_library.MediaLibraryError) as caught:
        sdk.library.recommend(server=server, media_type="movie", min_rating=8)
    assert caught.value.code == ("upstream_timeout" if failure == "timeout" else "provider_unavailable")
    assert "private-page-detail" not in str(caught.value)
    assert [params["StartIndex"] for _, params in calls if params.get("SortBy") == "SortName"] == [0, 200]


@pytest.mark.parametrize("server", ["jellyfin", "emby"])
@pytest.mark.parametrize("size", [0, 1, 205])
def test_recommendation_cancellation_stops_inventory_and_does_not_return_results(native, monkeypatch, server, size):
    add, _, responses, calls = native
    add(server)
    stopped = size == 0
    closed = []
    original_close = requests.Session.close

    def close(session):
        closed.append(session)
        original_close(session)

    def page(params):
        nonlocal stopped
        if params.get("SortBy") == "DatePlayed":
            return {"Items": []}
        stopped = True
        start = params["StartIndex"]
        return {"Items": [media(str(i), Type="Movie", CommunityRating=9, UserData={})
                          for i in range(start, min(size, start + params["Limit"]))],
                "TotalRecordCount": size}

    monkeypatch.setattr(requests.Session, "close", close)
    responses["/System/ActivityLog/Entries"] = {"Items": []}
    responses["/Users/viewer/Items"] = page
    with sdk.call_owner("fixture", cancelled=lambda: stopped), pytest.raises(media_library.MediaLibraryError) as caught:
        sdk.library.recommend(server=server, media_type="movie", min_rating=8)
    assert caught.value.code == "cancelled"
    assert [params["StartIndex"] for _, params in calls if params.get("SortBy") == "SortName"] == ([0] if size else [])
    assert len(closed) == (1 if size else 0)
    if not size:
        assert calls == []


@pytest.mark.parametrize("server", ["jellyfin", "emby"])
@pytest.mark.parametrize("size,total,status", [(0, None, "empty"), (0, 0, "empty"), (0, 3, "partial"), (1, 3, "partial")])
def test_recommendation_empty_results_preserve_inventory_completeness(native, server, size, total, status):
    add, _, responses, calls = native
    add(server)
    responses["/System/ActivityLog/Entries"] = {"Items": []}

    def page(params):
        if params.get("SortBy") == "DatePlayed":
            return {"Items": []}
        payload: dict[str, object] = {"Items": [media(str(i), Type="Movie", CommunityRating=7, UserData={}) for i in range(size)]}
        if total is not None:
            payload["TotalRecordCount"] = total
        return payload

    responses["/Users/viewer/Items"] = page
    result = sdk.library.recommend(server=server, media_type="movie", min_rating=8)
    assert result["status"] == status
    assert result["items"] == [] and result["count"] == result["matched_count"] == 0
    assert result["considered_count"] == result["excluded"]["below_rating"] == size
    assert result["inventory_total"] == (0 if total is None else total)
    assert result["partial"] is (status == "partial")
    assert result["inventory_truncated"] is (status == "partial")
    assert result["inventory_total_exact"] is (status == "empty")
    assert result["total"] == (None if status == "partial" else 0)
    assert [params["StartIndex"] for _, params in calls if params.get("SortBy") == "SortName"] == [0]


@pytest.mark.parametrize("total,truncated", [(None, True), (5001, True)])
def test_inventory_unknown_or_truncated_is_not_a_complete_empty(native, monkeypatch, total, truncated):
    from app.clients.base import MediaServerClient

    add, _, responses, _ = native
    add()
    responses["/System/ActivityLog/Entries"] = {"Items": []}
    inventory = MediaRecommendationInventory(truncated=truncated)
    inventory.total = cast(int, total)
    monkeypatch.setattr(MediaServerClient, "list_recommendation_candidates", lambda *a, **kw: inventory)
    result = sdk.library.recommend()
    assert result["inventory_total"] == total and result["count"] == 0
    assert result["status"] == ("partial" if truncated or total is None else "empty")
    assert result["matched_count_scope"] == "scanned_candidates"


@pytest.mark.parametrize("method,arguments", [
    ("history", {"limit": True}), ("history", {"limit": 51}),
    ("resume", {"limit": 0}), ("resume", {"limit": 21}),
    ("recommend", {"min_rating": float("nan")}), ("recommend", {"min_rating": 10**1000}),
    ("recommend", {"exclude_played": 0}), ("recommend", {"must_match": ["喜剧|"]}),
    ("recommend", {"prefer": ["x"] * 13}),
])
def test_invalid_native_arguments_fail_before_client_requests(native, method, arguments):
    add, _, _, calls = native
    add()
    with pytest.raises(media_library.MediaLibraryError) as caught:
        sdk.METHODS[f"library.{method}"](**arguments)
    assert caught.value.code == "invalid_arguments" and not calls


def test_missing_ambiguous_disabled_and_userless_profiles_fail_without_cross_user_fallback(native):
    add, profiles, responses, calls = native
    with pytest.raises(media_library.MediaLibraryError) as missing:
        sdk.library.history()
    assert missing.value.code == "provider_not_configured"
    first = add()
    add("emby")
    with pytest.raises(media_library.MediaLibraryError):
        sdk.library.resume()
    assert not calls
    profiles[:] = [replace(first, enabled=False)]
    with pytest.raises(media_library.MediaLibraryError) as disabled:
        sdk.library.recommend()
    assert disabled.value.code == "provider_not_configured" and not calls
    profiles[:] = [replace(first, user_id="")]
    responses["/Users"] = []
    with pytest.raises(media_library.MediaLibraryError) as userless:
        sdk.library.history()
    assert userless.value.code == "provider_user_required"
    assert all(path == "/Users" for path, _ in calls)


def test_history_failure_is_not_empty_or_resume_and_does_not_leak_upstream_details(native):
    add, _, responses, calls = native
    add("emby")

    def failure(_params):
        raise RuntimeError("private-token-and-upstream-url")

    responses["/Users/viewer/Items"] = failure
    with pytest.raises(media_library.MediaLibraryError) as caught:
        sdk.library.history()
    assert caught.value.code == "provider_unavailable"
    assert "private-token" not in str(caught.value)
    assert not any("Resume" in path for path, _ in calls)


def test_owner_preferences_choose_server_and_preserve_explicit_empty_zero_false(native):
    from app.database import media_owner_digest
    from app.repositories.media_experience import (
        get_media_preferences,
        set_media_preferences,
    )

    add, _, responses, _ = native
    add()
    add("emby", "second-viewer")
    set_media_preferences(media_owner_digest("owner-a"), expected_revision=0, updates={
        "preferred_server": "jellyfin", "preferred_genres": ["喜剧"], "excluded_genres": ["剧情"],
        "min_rating": 8, "exclude_played": True,
    })
    set_media_preferences(media_owner_digest("owner-b"), expected_revision=0, updates={
        "preferred_server": "emby", "preferred_genres": ["剧情"], "excluded_genres": ["喜剧"],
        "min_rating": 0, "exclude_played": False,
    })
    responses["/System/ActivityLog/Entries"] = {"Items": []}
    candidates = [
        media("comedy", Type="Movie", Genres=["喜剧"], CommunityRating=8.8, UserData={}),
        media("drama", Type="Movie", Genres=["剧情"], CommunityRating=4, UserData={"Played": True}),
    ]

    def inventory(params):
        return {"Items": [] if params.get("SortBy") == "DatePlayed" else candidates, "TotalRecordCount": 2}

    responses["/Users/viewer/Items"] = inventory
    responses["/Users/second-viewer/Items"] = inventory
    responses["/Users/second-viewer/Items/Resume"] = {"Items": [media("resume-b")]}
    with sdk.call_owner("owner-a"):
        result = sdk.library.recommend()
        assert result["server"] == "jellyfin" and [item["id"] for item in result["items"]] == ["comedy"]
        result = sdk.library.recommend(prefer=[], exclude=[], min_rating=0, exclude_played=False)
        assert {item["id"] for item in result["items"]} == {"comedy", "drama"}
        assert result["criteria"]["prefer"] == result["criteria"]["exclude"] == []
        assert result["criteria"]["min_rating"] == 0 and result["criteria"]["exclude_played"] is False
        assert sdk.library.history()["server"] == "jellyfin"
    with sdk.call_owner("owner-b"):
        result = sdk.library.recommend()
        assert result["server"] == "emby" and [item["id"] for item in result["items"]] == ["drama"]
        assert result["scope"]["user_id"] == "second-viewer"
        assert sdk.library.resume()["server"] == "emby"
        assert sdk.library.history(server="jellyfin")["scope"]["user_id"] == "viewer"
    assert get_media_preferences(media_owner_digest("owner-a"))["min_rating"] == 8
    assert get_media_preferences(media_owner_digest("owner-b"))["exclude_played"] is False


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="需要可运行 Bubblewrap 的 Linux 隔离工作区")
def test_native_workspace_keeps_preferences_recovers_parameters_and_saves_full_results(native, monkeypatch, tmp_path):
    from app.database import media_owner_digest
    from app.repositories.media_experience import set_media_preferences

    add, _, responses, calls = native
    add()
    add("emby", "unused-viewer")
    owner = "webk:v1:playback-native"
    set_media_preferences(media_owner_digest(owner), expected_revision=0, updates={
        "preferred_server": "jellyfin", "excluded_genres": ["剧情"],
        "min_rating": 8, "exclude_played": True,
    })
    responses["/System/ActivityLog/Entries"] = {"Items": []}
    responses["/Users/viewer/Items/Resume"] = {"Items": [media("resume-real")]}
    candidates = [media("allowed", Type="Movie", Genres=["喜剧"], CommunityRating=9,
                        Overview="完整简介" * 150, UserData={}),
                  media("excluded", Type="Movie", Genres=["剧情"], CommunityRating=9, UserData={})]
    responses["/Users/viewer/Items"] = {"Items": candidates, "TotalRecordCount": 2}
    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(
        data_dir=tmp_path, runtime_dir=tmp_path / "runtime",
    ))

    async def check():
        store = InMemorySessionStateStore()
        context = await make_context(store, owner=owner)
        pipeline = ToolPipeline(catalog=ToolCatalog(), state_store=store)
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": '''import json, subprocess
from mf import library
try:
    library.recommend(media_type="movie", limit=0)
except RuntimeError:
    print("修正参数")
result = library.recommend(media_type="movie", limit=2)
history = library.history(limit=3)
resume = json.loads(subprocess.check_output(["mf", "library.resume", "--limit", "3"]))
json.dump({"recommend": result, "history": history, "resume": resume}, open("playback.json", "w"))
print(result["items"][0]["id"], resume["items"][0]["id"], history["status"])
'''}, context=context)
        data = result.outcome.public_content["data"]
        assert data["exit_code"] == 0, data
        assert data["stdout"].strip().splitlines() == ["修正参数", "allowed resume-real empty"]
        assert [call["ok"] for call in data["sdk_calls"]] == [False, True, True, True]
        assert result.effect_plan is None
        saved = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": '''import json
r=json.load(open("playback.json"))
assert r["recommend"]["criteria"]["media_type"] == "movie"
assert r["recommend"]["criteria"]["exclude"] == ["剧情"]
assert r["recommend"]["items"][0]["overview"] == "完整简介" * 150
assert len(r["recommend"]["items"]) == 1
print(r["resume"]["scope"]["user_id"])
'''}, context=context)
        data = saved.outcome.public_content["data"]
        assert data["exit_code"] == 0, data
        assert data["stdout"].strip() == "viewer" and data["sdk_calls"] == []

    asyncio.run(check())
    assert calls and all("unused-viewer" not in path for path, _ in calls)
