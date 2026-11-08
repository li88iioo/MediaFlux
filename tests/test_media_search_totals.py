"""搜索总数必须来自原生 API；页长、标题匹配不能替代演员作品统计。"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import services, sdk
from app.clients.base import MediaItem, MediaSearchResult
from app.clients.emby import EmbyClient
from app.clients.jellyfin import JellyfinClient


@pytest.fixture(autouse=True)
def clear_search_cache():
    services.clear_library_search_cache()
    yield
    services.clear_library_search_cache()


@pytest.fixture(params=[JellyfinClient, EmbyClient], ids=["jellyfin", "emby"])
def client(request):
    instance = request.param("http://synthetic.invalid", "synthetic")
    instance._cached_user_id = "viewer"
    if isinstance(instance, EmbyClient):
        instance.product_kind = "emby"
    yield instance
    instance.close()


def payload(count=137, size=8, start=0) -> dict:
    return {
        "Items": [{"Id": f"item-{i}", "Name": f"Unrelated title {i}", "Type": "Movie"} for i in range(start, start + size)],
        "TotalRecordCount": count,
    }


def source(result, server_type="jellyfin", error=""):
    return {
        "server_type": server_type, "server_name": server_type,
        "web_url": "http://synthetic.invalid", "items": result.items,
        **result.metadata(), "error": error,
    }


def library_result(monkeypatch, sources, **options):
    monkeypatch.setattr(services, "_search_sources", lambda *_args, **_kwargs: sources)
    return sdk.library.search("Sample", limit=8, **options)


def test_search_50_is_a_page_not_the_total(client):
    with patch.object(client, "_request", return_value=payload(size=50)) as request:
        result = client.search_media("Sample", limit=50)
    assert result.total == 137
    assert result.metadata()["returned"] == 50
    assert result.has_more is True
    assert result.next_offset == 50
    params = request.call_args.kwargs["params"]
    assert params["SearchTerm"] == "Sample"
    assert params["IncludeItemTypes"] == "Movie,Series,Episode"
    assert params["Limit"] == 50
    assert params["EnableTotalRecordCount"] == "true"
    assert params["StartIndex"] == 0


@pytest.mark.parametrize("offset,size,next_offset,has_more", [(50, 50, 100, True), (100, 37, None, False), (200, 0, None, False)])
def test_native_offset_and_terminal_page(client, offset, size, next_offset, has_more):
    with patch.object(client, "_request", return_value=payload(size=size, start=offset)) as request:
        result = client.search_media("Sample", limit=50, offset=offset)
    assert result.total == 137
    assert result.has_more is has_more
    assert result.next_offset == next_offset
    assert request.call_args.kwargs["params"]["StartIndex"] == offset


@pytest.mark.parametrize("count", [None, True, False, -1, 0, 1.5, "bad", "-1"])
def test_bad_or_contradictory_count_is_unknown_not_len(client, count):
    data = payload(count=count)
    if count is None:
        del data["TotalRecordCount"]
    with patch.object(client, "_request", return_value=data):
        result = client.search_media("Sample", limit=8)
    assert result.total is None
    assert result.has_more is None
    assert result.metadata()["returned"] == 8
    assert result.next_offset == 8
    assert result.total_source == "unknown"


def test_numeric_record_count_is_accepted(client):
    with patch.object(client, "_request", return_value=payload(count="137")):
        assert client.search_media("Sample").total == 137


def test_empty_page_without_total_is_not_a_confirmed_zero(client):
    with patch.object(client, "_request", return_value={"Items": []}):
        result = client.search_media("Sample")
    assert result.total is None
    assert result.has_more is None


@pytest.mark.parametrize("tmdb_id,expected_year", [("2", "2013"), ("3", None), ("", "1976")])
def test_movie_updates_use_explicit_identity_and_movie_scope(client, tmdb_id, expected_year):
    inventory: list[dict] = [
        *[{"Id": f"other-{i}", "Name": f"Other {i}", "Type": "Movie", "ProviderIds": {"Tmdb": str(100 + i)}}
          for i in range(500)],
        {"Id": "old", "Name": "魔女嘉莉", "Type": "Movie", "ProductionYear": 1976, "ProviderIds": {"Tmdb": "1"}},
        {"Id": "new", "Name": "Carrie", "Type": "Movie", "ProductionYear": 2013, "ProviderIds": {"Tmdb": "2"}},
    ]

    def response(path, *, params=None, **kwargs):
        assert path == "/Users/viewer/Items"
        assert params is not None
        rows = inventory if not params.get("SearchTerm") else [row for row in inventory if params["SearchTerm"] in row["Name"]]
        start = params.get("StartIndex", 0)
        return {"Items": rows[start:start + params["Limit"]], "TotalRecordCount": len(rows)}

    with (
        patch.object(client, "_request", side_effect=response) as request,
        patch("app.services._configured_media_sources", return_value=[("jellyfin", "Fixture", "", client)]),
        patch("socket.socket.connect", side_effect=AssertionError("禁止真实网络")),
    ):
        result = sdk.library.updates(query="魔女嘉莉", media_type="movie", tmdb_id=tmdb_id)
    rows = result["data"]["sources"][0]["items"]
    assert result["data"]["local_match_status"] == ("found" if expected_year else "not_found")
    assert [row["year"] for row in rows] == ([expected_year] if expected_year else [])
    assert all(call.kwargs["params"]["IncludeItemTypes"] == "Movie" for call in request.call_args_list)
    if tmdb_id:
        assert any(call.kwargs["params"]["StartIndex"] > 0 for call in request.call_args_list)
        assert all("SearchTerm" not in call.kwargs["params"] for call in request.call_args_list)
        assert all(row["match"] == "tmdb_id" for row in rows)


@pytest.mark.parametrize("kind", ["movie", "series"])
def test_tmdb_search_pages_and_counts_only_matching_identity(client, kind):
    rows = [{"Id": str(i), "Name": f"Title {i}", "Type": kind.title(),
             "ProviderIds": {"Tmdb": "2" if i % 2 else "1"}} for i in range(8)]
    with patch.object(client, "_request", return_value={"Items": rows, "TotalRecordCount": len(rows)}) as request:
        first = client.search_media("2", search_by="tmdb", media_type=kind, limit=3)
        last = client.search_media("2", search_by="tmdb", media_type=kind, limit=3, offset=first.next_offset)
        count = client.search_media("2", search_by="tmdb", media_type=kind, count_only=True)
    assert [item.id for item in first.items + last.items] == ["1", "3", "5", "7"]
    assert first.total == last.total == count.total == 4 and count.items == []
    assert first.has_more and first.next_offset == 3
    assert last.has_more is False and last.next_offset is None
    assert first.metadata()["match_basis"] == "provider_id"
    assert first.total_source == "identity_inventory"
    assert all(call.kwargs["params"]["IncludeItemTypes"] == kind.title() for call in request.call_args_list)


@pytest.mark.parametrize("tmdb_id", ["2", "3"])
def test_movie_identity_updates_preserve_incomplete_inventory(client, tmdb_id):
    rows = [{"Id": "new", "Name": "Carrie", "Type": "Movie", "ProviderIds": {"Tmdb": "2"}}]
    with (
        patch.object(client, "_inventory_items", return_value=(rows, 10001, True)),
        patch("app.services._configured_media_sources", return_value=[("jellyfin", "Fixture", "", client)]),
        patch("socket.socket.connect", side_effect=AssertionError("禁止真实网络")),
    ):
        result = sdk.library.updates(query="魔女嘉莉", media_type="movie", tmdb_id=tmdb_id)
    assert result["data"]["sources"][0]["truncated"] is True
    assert result["data"]["search_truncated_server_count"] == 1
    assert result["data"]["local_match_status"] == ("found" if tmdb_id == "2" else "indeterminate")
    assert result["status"] == ("comparison_unavailable" if tmdb_id == "2" else "review_required")


def test_movie_identity_update_stops_paging_after_cancellation(client):
    stopped = False

    def response(path, *, params=None, **kwargs):
        nonlocal stopped
        stopped = True
        assert params is not None
        start = params["StartIndex"]
        return payload(count=501, size=min(500, 501 - start), start=start)

    with (
        patch.object(client, "_request", side_effect=response) as request,
        patch("app.services._configured_media_sources", return_value=[("jellyfin", "Fixture", "", client)]),
        sdk.call_owner("fixture", cancelled=lambda: stopped),
    ):
        result = sdk.library.updates(query="魔女嘉莉", media_type="movie", tmdb_id="2")
    assert request.call_count == 1
    assert result["data"]["local_match_status"] == "indeterminate"


@pytest.mark.parametrize("known_total", [True, False])
def test_movie_title_updates_use_reported_coverage_instead_of_page_length(client, known_total):
    data = payload(count=50, size=50) if known_total else {"Items": []}
    with (
        patch.object(client, "_request", return_value=data),
        patch("app.services._configured_media_sources", return_value=[("jellyfin", "Fixture", "", client)]),
    ):
        result = sdk.library.updates(query="Unknown", media_type="movie")
    assert result["data"]["sources"][0]["truncated"] is (not known_total)
    assert result["data"]["local_match_status"] == ("possible" if known_total else "indeterminate")


def test_count_only_uses_same_filter_and_native_zero_item_limit(client):
    with patch.object(client, "_request", return_value=payload(size=0)) as request:
        result = client.search_media("Sample", media_type="movie", count_only=True)
    assert result.total == 137
    assert result.items == []
    assert result.next_offset is None
    assert request.call_count == 1
    assert request.call_args.kwargs["params"]["Limit"] == 0
    assert request.call_args.kwargs["params"]["IncludeItemTypes"] == "Movie"
    assert request.call_args.kwargs["params"]["SearchTerm"] == "Sample"


@pytest.mark.parametrize("count_only", [False, True])
def test_actor_uses_exact_person_role_not_title_and_excludes_episodes(client, count_only):
    people = {"Items": [{"Id": "person-1", "Name": "Sample Performer"}], "TotalRecordCount": 1}
    with patch.object(client, "_request", side_effect=[people, payload(size=0 if count_only else 8)]) as request:
        result = client.search_media("Sample Performer", search_by="actor", count_only=count_only)
    assert result.total == 137
    assert result.person_match == "exact"
    assert result.metadata()["match_basis"] == "actor_metadata"
    assert result.metadata()["included_item_types"] == ["movie", "series"]
    first, second = request.call_args_list
    assert first.args == ("/Persons",)
    assert first.kwargs["params"]["UserId"] == "viewer"
    params = second.kwargs["params"]
    assert "SearchTerm" not in params
    assert "Person" not in params
    assert params["PersonIds"] == "person-1"
    assert params["PersonTypes"] == "Actor"
    assert params["IncludeItemTypes"] == "Movie,Series"
    assert params["Limit"] == (0 if count_only else 12)
    if not count_only:
        assert all("Sample Performer" not in item.name for item in result.items)


@pytest.mark.parametrize("media_type,item_type", [("movie", "Movie"), ("series", "Series"), ("episode", "Episode")])
def test_actor_count_can_be_scoped_by_record_type(client, media_type, item_type):
    people = {"Items": [{"Id": "person-1", "Name": "Sample"}], "TotalRecordCount": 1}
    with patch.object(client, "_request", side_effect=[people, payload(size=0)]) as request:
        result = client.search_media("Sample", search_by="actor", media_type=media_type, count_only=True)
    assert result.total == 137
    assert request.call_args.kwargs["params"]["IncludeItemTypes"] == item_type


@pytest.mark.parametrize("people,expected", [
    ({"Items": [{"Id": "a", "Name": "Sample"}, {"Id": "b", "Name": "SAMPLE"}], "TotalRecordCount": 2}, "ambiguous"),
    ({"Items": [{"Id": "a", "Name": "Sample"}], "TotalRecordCount": 51}, "incomplete"),
    ({"Items": [{"Id": "a", "Name": "Sample"}]}, "incomplete"),
    ({"Items": [{"Name": "Sample"}], "TotalRecordCount": 1}, "incomplete"),
])
def test_actor_resolution_must_be_complete_and_unambiguous(client, people, expected):
    with patch.object(client, "_request", return_value=people) as request:
        result = client.search_media("Sample", search_by="actor")
    assert request.call_count == 1
    assert result.total is None
    assert result.person_match == expected
    assert result.has_more is None


def test_fuzzy_person_name_is_not_silently_selected(client):
    people = {"Items": [{"Id": "a", "Name": "Sample Other"}], "TotalRecordCount": 1}
    with patch.object(client, "_request", return_value=people) as request:
        result = client.search_media("Sample", search_by="actor")
    assert request.call_count == 1
    assert result.total is None
    assert result.person_match == "not_found"
    assert result.total_source == "unknown"


def test_bad_rows_do_not_break_backend_cursor(client):
    data = payload(count=137, size=1)
    data["Items"].insert(0, None)
    with patch.object(client, "_request", return_value=data):
        result = client.search_media("Sample")
    assert result.metadata()["returned"] == 1
    assert result.next_offset == 2
    assert result.total == 137


def test_service_transmits_counts_paging_and_closes_clients(client):
    with (
        patch("app.services._configured_media_sources", return_value=[("jellyfin", "Synthetic", client.url, client)]),
        patch.object(client, "_request", return_value=payload(size=8, start=8)) as request,
        patch.object(client, "close") as close,
    ):
        sources = services.search_media_servers("Sample", limit=8, offset=8, media_type="movie")
    assert sources[0]["total"] == 137
    assert sources[0]["returned"] == 8
    assert sources[0]["has_more"] is True
    assert sources[0]["next_offset"] == 16
    assert request.call_args.kwargs["params"]["StartIndex"] == 8
    close.assert_called_once_with()


def test_library_keeps_server_total_separate_from_returned(monkeypatch):
    result = library_result(monkeypatch, [source(MediaSearchResult(items=[MediaItem(id="one", name="One", type="Movie")], total=137, has_more=True, next_offset=1))])
    assert result["data"]["total"] == 137
    assert result["data"]["returned"] == 1
    assert result["data"]["has_more"] is True
    assert result["data"]["sources"][0]["total"] == 137
    assert result["data"]["total_known"] is True


@pytest.mark.parametrize("count", [None, 137])
def test_unknown_or_partial_library_total_is_not_a_precise_total(monkeypatch, count):
    sources = [source(MediaSearchResult(items=[MediaItem(id="one", name="One", type="Movie")], total=count, has_more=True if count else None))]
    if count is not None:
        sources.append(source(MediaSearchResult(), server_type="emby", error="unavailable"))
    result = library_result(monkeypatch, sources)
    assert result["data"]["total"] is None
    assert result["data"]["known_total"] == (count or 0)
    assert result["data"]["returned"] == 1
    assert result["status"] == "partial"
    assert result["data"]["total_known"] is False


def test_library_multi_source_count_is_explicitly_not_deduplicated(monkeypatch):
    sources = [source(MediaSearchResult(total=137, has_more=True)), source(MediaSearchResult(total=60, has_more=True), "emby")]
    result = library_result(monkeypatch, sources, count_only=True)
    assert result["data"]["total"] == 197
    assert result["data"]["returned"] == 0
    assert result["data"]["count_scope"] == "server_records_sum_not_deduplicated"
    assert all(source["returned"] == 0 for source in result["data"]["sources"])
    assert result["data"]["match_status"] == "found"


@pytest.mark.parametrize("sources", [[], [source(MediaSearchResult(), error="unavailable")]])
def test_not_configured_or_unavailable_is_not_zero_results(monkeypatch, sources):
    result = library_result(monkeypatch, sources)
    assert result["data"]["total"] is None
    assert result["data"]["returned"] == 0
    assert result["data"]["has_more"] is None
    assert result["ok"] is False


def test_actor_ambiguity_is_explained_by_library(monkeypatch):
    result = library_result(monkeypatch, [source(MediaSearchResult(search_by="actor", person_match="ambiguous"))], search_by="actor", count_only=True)
    assert result["data"]["total"] is None
    assert result["data"]["match_status"] == "indeterminate"
    assert result["data"]["sources"][0]["person_match"] == "ambiguous"


def test_paging_filter_and_count_mode_all_partition_cache(monkeypatch):
    services.clear_library_search_cache()
    search = MagicMock(return_value=[])
    monkeypatch.setattr(services, "search_media_servers", search)
    options: list[dict] = [{}, {"offset": 8}, {"search_by": "actor"}, {"media_type": "movie"}, {"count_only": True}]
    try:
        for option in options:
            services._search_sources("Sample", 8, **option)
            services._search_sources("Sample", 8, **option)
        assert search.call_count == len(options)
    finally:
        services.clear_library_search_cache()


@pytest.mark.parametrize("options", [{"offset": -1}, {"offset": True}, {"offset": "8"}, {"search_by": "person"}, {"media_type": "video"}, {"count_only": 1}, {"unexpected": True}])
def test_library_rejects_invalid_search_options(options):
    with patch.object(services, "_configured_media_sources", side_effect=AssertionError("invalid input reached backend")):
        with pytest.raises(TypeError if "unexpected" in options else ValueError):
            sdk.library.search("Sample", **options)



def test_native_search_reports_total_not_page_length_and_selected_server(client):
    services.clear_library_search_cache()
    with (patch.object(services, "_configured_media_sources", return_value=[("jellyfin", "Jellyfin", client.url, client)]),
          patch.object(client, "_request", return_value=payload(size=20,start=20)) as request,
          patch.object(client, "close") as close):
        result=sdk.library.search("Sample",limit=20,offset=20,server="jellyfin")["data"]
    assert result["total"]==137 and result["returned"]==20
    assert result["has_more"] and result["next_offset"]==40
    assert request.call_args.kwargs["params"]["StartIndex"]==20
    close.assert_called_once_with()
    services.clear_library_search_cache()


@pytest.mark.parametrize("ambiguous", [False, True])
def test_native_search_keeps_actor_statistics_and_unknowns(client, ambiguous):
    services.clear_library_search_cache()
    people=[{"Id":"a","Name":"Sample"}]
    if ambiguous: people.append({"Id":"b","Name":"Sample"})
    with (patch.object(services,"_configured_media_sources",return_value=[("jellyfin","Jellyfin",client.url,client)]),
          patch.object(client,"_request",side_effect=[{"Items":people,"TotalRecordCount":len(people)},payload(size=0)])):
        result=sdk.library.search("Sample",search_by="actor",media_type="movie",count_only=True,server="jellyfin")
    data=result["data"]
    assert data["total"]==(None if ambiguous else 137) and data["returned"]==0
    assert data["sources"][0]["person_match"]==("ambiguous" if ambiguous else "exact")
    assert data["count_only"] is True and result["status"]==("partial" if ambiguous else "success")
    services.clear_library_search_cache()


@pytest.mark.parametrize("count_only", [False, True])
def test_episode_title_empty_result_is_not_series_inventory(monkeypatch, count_only):
    result = library_result(
        monkeypatch, [source(MediaSearchResult(total=0, has_more=False))],
        media_type="episode", count_only=count_only,
    )
    assert result["status"] == "empty"
    assert result["data"]["total"] == 0
    assert result["data"]["match_status"] == "not_found"
    assert result["data"]["search_scope"] == "episode_title"


def test_episode_title_found_keeps_native_results_and_scope(monkeypatch):
    result = library_result(
        monkeypatch, [source(MediaSearchResult(
            items=[MediaItem(id="ep-7", name="Sample", type="Episode", series_name="Different series")],
            total=1, has_more=False,
        ))], media_type="episode",
    )
    assert result["data"]["search_scope"] == "episode_title"
    assert result["data"]["total"] == 1
    assert result["data"]["sources"][0]["items"][0]["series_name"] == "Different series"


def test_episode_actor_search_does_not_become_title_search(monkeypatch):
    result = library_result(
        monkeypatch, [source(MediaSearchResult(total=2, has_more=True, search_by="actor"))],
        media_type="episode", search_by="actor", count_only=True,
    )
    assert result["data"]["search_scope"] == "actor_metadata"
    assert result["data"]["match_basis"] == "actor_metadata"
    assert result["data"]["total"] == 2


@pytest.mark.parametrize("partial", [False, True])
def test_shared_search_total_known_does_not_mean_all_pages_returned(monkeypatch, partial):
    sources = [source(MediaSearchResult(
        items=[MediaItem(id="one", name="One", type="Movie")], total=137,
        offset=8, limit=8, has_more=True, next_offset=16,
    ))]
    if partial:
        sources.append(source(MediaSearchResult(), "emby", error="unavailable"))
    search = MagicMock(return_value=sources)
    monkeypatch.setattr(services, "search_media_servers", search)
    result = services.search_library("Sample", offset=8)
    data = result["data"]
    assert result["status"] == ("partial" if partial else "success")
    assert data["total"] == (None if partial else 137)
    assert data["total_known"] is (not partial)
    assert data["known_total"] == 137 and data["returned"] == 1
    assert data["has_more"] is True and data["next_offset"] == 16
    assert data["pagination_scope"] == "per_server"
    assert data["sources"][0]["total_known"] is True
    if partial:
        assert data["sources"][1]["total_known"] is False
    agent = sdk.library.search("Sample", limit=8, offset=8)
    assert agent["data"]["total"] == data["total"]
    assert agent["data"] == data
    assert "complete" not in data and all("complete" not in row for row in data["sources"])
    search.assert_called_once_with(
        "Sample", limit=8, offset=8, search_by="title", media_type="all", count_only=False, server="all",
    )


@pytest.mark.parametrize("people_complete", [False, True])
def test_shared_search_and_agent_reuse_exact_native_actor_count(client, people_complete):
    people = {"Items": [{"Id": "person-1", "Name": "Sample"}], "TotalRecordCount": 1 if people_complete else 51}
    options = {"offset": 16, "search_by": "actor", "media_type": "movie", "count_only": True}
    with (
        patch.object(services, "_configured_media_sources", return_value=[("jellyfin", "Synthetic", client.url, client)]) as configured,
        patch.object(client, "_request", side_effect=[people, payload(count=149, size=0)]) as request,
        patch.object(client, "close") as close,
    ):
        data = services.search_library("Sample", **options)["data"]
        agent = sdk.library.search("Sample", limit=8, **options)
    assert data["total"] == agent["data"]["total"] == (149 if people_complete else None)
    assert data["total_known"] is people_complete
    assert data["known_total"] == (149 if people_complete else 0)
    assert data["returned"] == 0 and data["next_offset"] is None
    assert data["sources"][0]["person_match"] == ("exact" if people_complete else "incomplete")
    assert agent["data"] == data
    assert request.call_count == (2 if people_complete else 1)
    assert request.call_args_list[0].args == ("/Persons",)
    if people_complete:
        params = request.call_args_list[1].kwargs["params"]
        assert params["Limit"] == 0 and params["StartIndex"] == 16
        assert params["PersonIds"] == "person-1" and params["PersonTypes"] == "Actor"
        assert params["IncludeItemTypes"] == "Movie" and "SearchTerm" not in params
    configured.assert_called_once_with()
    close.assert_called_once_with()


def test_shared_search_json_mutation_cannot_change_cached_agent_results(monkeypatch):
    item = MediaItem(
        id="episode-7", name="Original", type="Episode", series_id="series-1", series_name="The Series",
        season_number=0, episode_number=7, genres=("Drama",),
        primary_image="/media-image/jellyfin/series-1?tag=cover-1",
        web_url="http://synthetic.invalid/web/index.html#!/details?id=episode-7",
    )
    backend = MagicMock(side_effect=[
        [source(MediaSearchResult(items=[item], total=1, has_more=False))],
        [source(MediaSearchResult(items=[MediaItem(id="fresh", name="Fresh", type="Movie")], total=1, has_more=False))],
    ])
    monkeypatch.setattr(services, "search_media_servers", backend)
    first = services.search_library("Sample")["data"]
    projected = first["sources"][0]["items"][0]
    assert projected["id"] == "episode-7" and projected["series_id"] == "series-1"
    assert projected["name"] == "Original" and projected["display_name"] == "The Series"
    assert projected["primary_image"] == item.primary_image and projected["web_url"] == item.web_url
    assert projected["season_number"] == 0 and projected["episode_number"] == 7
    projected["name"] = "Mutated"
    projected["genres"].append("Mutated")
    first["sources"][0]["included_item_types"].clear()
    json.dumps(first, allow_nan=False)
    agent = sdk.library.search("Sample", limit=8)
    again = services.search_library("Sample")["data"]
    assert agent["data"]["sources"][0]["items"][0]["name"] == "Original"
    assert again["sources"][0]["items"][0]["genres"] == ["Drama"]
    assert again["sources"][0]["included_item_types"] == ["movie", "series", "episode"]
    assert backend.call_count == 1
    services.clear_library_search_cache()
    assert services.search_library("Sample")["data"]["sources"][0]["items"][0]["name"] == "Fresh"
    assert backend.call_count == 2


@pytest.mark.parametrize("private_url", [
    "http://viewer:private-credential@synthetic.invalid/web/index.html",
    "http://synthetic.invalid/web/index.html?api_key=private-credential",
    "http://synthetic.invalid/web/index.html#!/details?id=one&X-Emby-Token=private-credential",
    "/srv/private-credential/media.mkv",
])
def test_shared_search_never_serializes_private_url_credentials(monkeypatch, private_url):
    item = MediaItem(
        id="one", name="One", type="Movie", series_id="series-1",
        primary_image=private_url, web_url=private_url, series_web_url=private_url,
    )
    raw = {**source(MediaSearchResult(items=[item], total=1, has_more=False)), "web_url": private_url}
    monkeypatch.setattr(services, "search_media_servers", lambda *_args, **_kwargs: [raw])
    result = services.search_library("Sample")
    public_source = result["data"]["sources"][0]
    public_item = public_source["items"][0]
    assert public_source["web_url"] == ""
    assert all(public_item[key] == "" for key in ("primary_image", "web_url", "series_web_url", "open_url"))
    assert public_item["id"] == "one" and public_item["series_id"] == "series-1"
    assert "private-credential" not in json.dumps(result, allow_nan=False)


def test_shared_search_runs_without_agent_or_route_runtime():
    script = textwrap.dedent('''
        import json
        import sys
        import tests
        from unittest.mock import patch

        blocked = ("app.routes", "fastapi", "app.agent.domains", "app.agent.kernel")
        with patch.dict(sys.modules, dict.fromkeys(blocked)), patch(
            "socket.socket.connect", side_effect=AssertionError("禁止真实外联"),
        ):
            from app.services import search_library
            from app.clients.base import MediaItem
            page = {"server_type": "jellyfin", "server_name": "Jellyfin", "total": 149,
                    "items": [MediaItem(id="one", name="One", type="Movie")],
                    "has_more": True, "next_offset": 1}
            with patch("app.services.search_media_servers", return_value=[page]):
                result = search_library("Sample")
            assert not any(module is not None and (name == prefix or name.startswith(prefix + "."))
                           for name, module in sys.modules.items() for prefix in blocked)
            print(json.dumps(result, allow_nan=False))
    ''')
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["ok"] and result["data"]["total_known"] and result["data"]["has_more"]
    assert result["data"]["sources"][0]["items"][0]["id"] == "one"
    assert "evidence" not in result and "summary" not in result
