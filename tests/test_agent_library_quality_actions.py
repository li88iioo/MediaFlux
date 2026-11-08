from __future__ import annotations

from unittest.mock import Mock
from typing import Any

import pytest

from app.modules.library_quality import (
    LibraryQualityError,
    inspect_library_quality,
    inspect_quality_item,
)


def item(**kwargs):
    return {"Id": "movie-1", "Name": "测试影片", "Type": "Movie", **kwargs}


def inspect(value, min_resolution=1080, language="chinese"):
    return inspect_quality_item(
        value, min_resolution=min_resolution, subtitle_language=language
    )


@pytest.mark.parametrize(
    "metadata",
    [{}, {"MediaStreams": None, "MediaSources": None},
     {"MediaStreams": [None, "invalid"], "MediaSources": [None, "invalid"]}],
)
def test_missing_streams_and_size_remain_unknown(metadata):
    result = inspect(item(**metadata))
    assert result["id"] == "movie-1"
    assert not {"__object_id", "__object_kind", "ref"} & result.keys()
    assert result["resolution"] is None
    assert result["subtitle_state"] == "unknown"
    assert result["size_bytes"] is None
    assert result["availability"] == "unknown"
    assert result["issues"] == []


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [(1920, 800, 1080), (3840, 1600, 2160), (1280, 544, 720), (720, 480, 480),
     (1920, None, None), (True, 1080, None), (1920, 0, None)],
)
def test_cinemascope_resolution_is_not_misclassified(width, height, expected):
    result = inspect(
        item(MediaStreams=[{"Type": "Video", "Width": width, "Height": height}])
    )
    assert result["resolution"] == expected


@pytest.mark.parametrize(
    ("streams", "has", "language", "expected"),
    [
        ([{"Type": "Subtitle", "Language": "chi"}], True, "chinese", "present"),
        ([{"Type": "Subtitle", "Language": " ZH-Hant "}], True, "chinese", "present"),
        ([{"Type": "Subtitle", "Language": "eng"}], True, "chinese", "missing"),
        ([{"Type": "Subtitle", "Language": ""}], True, "chinese", "unknown"),
        ([{"Type": "Subtitle", "Language": "und"}], True, "chinese", "unknown"),
        ([], True, "chinese", "unknown"),
        ([], False, "chinese", "missing"),
        ([], None, "chinese", "unknown"),
        ([], True, "any", "present"),
        ([], False, "any", "missing"),
        ([], None, "any", "unknown"),
        ([{"Type": "Subtitle"}], None, "any", "present"),
    ],
)
def test_missing_subtitle_metadata_is_not_missing_subtitle(streams, has, language, expected):
    assert (
        inspect(item(MediaStreams=streams, HasSubtitles=has), language=language)["subtitle_state"]
        == expected
    )


def test_versions_sizes_and_server_reported_missing():
    result = inspect(item(MediaSources=[
        {"Size": 100, "MediaStreams": [{"Type": "Video", "Width": 1920, "Height": 800}]},
        {"Size": 200, "MediaStreams": [{"Type": "Subtitle", "Language": "chi"}]},
    ], IsMissing=True))
    assert result["size_bytes"] == 300
    assert result["version_count"] == 2
    assert result["resolution"] == 1080
    assert result["subtitle_state"] == "present"
    assert result["issues"] == ["多个媒体版本", "服务器报告缺失"]
    assert inspect(item(MediaSources=[{"Size": 100}, {}]))["size_bytes"] is None


def test_quality_page_is_explicitly_partial_and_duplicates_are_page_local():
    client = Mock()
    client._request.return_value = {
        "Items": [
            item(ProviderIds={"Tmdb": "42"}),
            item(Id="movie-2", ProviderIds={"tmdb": "42"}),
        ],
        "TotalRecordCount": 1000,
    }
    outcome = inspect_library_quality(
        client,
        "viewer",
        library_id="library1",
        limit=50,
        server_label="Jellyfin",
        source="jellyfin_api",
    )
    data = outcome["data"]
    assert data["complete"] is False
    assert data["has_more"] is True
    assert data["next_start_index"] == 2
    assert data["duplicate_groups"] == 1
    assert data["duplicate_scope"] == "仅本页内检查"
    assert data["unknown_resolution"] == data["unknown_subtitles"] == 2
    assert data["issue_counts"] == {"本页疑似重复": 2}
    assert all(value["issues"] == ["本页疑似重复"] for value in data["items"])
    assert not data["playability_verified"]
    assert "不代表全库结论" in outcome["summary"]
    params = client._request.call_args.kwargs["params"]
    assert params["ParentId"] == "library1"
    assert params["Limit"] == 50


@pytest.mark.parametrize("start,total,items", [(100, 101, [item()]), (0, 0, [])])
def test_page_completion_depends_on_first_page_and_known_total(start, total, items):
    client = Mock()
    client._request.return_value = {"Items": items, "TotalRecordCount": total}
    data = inspect_library_quality(
        client,
        "viewer",
        library_id=None,
        start_index=start,
        server_label="Jellyfin",
        source="jellyfin_api",
    )["data"]
    assert data["has_more"] is False
    assert data["next_start_index"] is None
    assert data["complete"] is (start == 0)
    assert data["scope"] == "所有媒体库"
    params = client._request.call_args.kwargs["params"]
    assert params["StartIndex"] == start
    assert "ParentId" not in params


@pytest.mark.parametrize("total", [{}, {"TotalRecordCount": True}, {"TotalRecordCount": -1},
                                   {"TotalRecordCount": "100"}])
@pytest.mark.parametrize("limit,next_index", [(1, 1), (2, None)])
def test_unknown_total_is_never_full_library_and_keeps_page_cursor(total, limit, next_index):
    client = Mock()
    client._request.return_value = {"Items": [item()], **total}
    data = inspect_library_quality(
        client, "viewer", library_id=None, limit=limit,
        server_label="Jellyfin", source="jellyfin_api"
    )["data"]
    assert data["reported_total"] is None
    assert data["next_start_index"] == next_index
    assert data["has_more"] is (next_index is not None)
    assert data["complete"] is False


@pytest.mark.parametrize(
    "raw,code",
    [
        (None, "invalid_response"),
        ({}, "invalid_response"),
        ({"Items": {}}, "invalid_response"),
        ({"Items": [None]}, "invalid_response"),
        ({"Items": [{}]}, "invalid_response"),
        ({"Items": [item(), item(Id="movie-2")]}, "invalid_response"),
        ({"Items": [item()], "TotalRecordCount": 0}, "provider_snapshot_changed"),
        ({"Items": [], "TotalRecordCount": 1}, "provider_snapshot_changed"),
    ],
)
def test_invalid_or_changed_pagination_fails_not_fake_healthy(raw, code):
    client = Mock()
    client._request.return_value = raw
    with pytest.raises(LibraryQualityError) as raised:
        inspect_library_quality(
            client, "viewer", library_id=None, limit=1,
            server_label="Jellyfin", source="jellyfin_api"
        )
    assert isinstance(raised.value, ValueError)
    assert raised.value.code == code


def test_quality_page_retains_all_items_flags_nulls_and_real_ids():
    client = Mock()
    received = [item(Id=f"real-id-{index}", HasSubtitles=False) for index in range(31)]
    client._request.return_value = {
        "Items": received,
        "TotalRecordCount": len(received),
    }
    result = inspect_library_quality(
        client, "viewer", library_id="library1", limit=50,
        server_label="Jellyfin", source="jellyfin_api"
    )
    assert type(result) is dict
    assert result["source"] == "jellyfin_api"
    assert result["status"] == "success"
    data = result["data"]
    assert type(data) is dict
    assert data["scanned"] == len(data["items"]) == 31
    assert data["issue_counts"] == {"缺少目标字幕": 31}
    assert data["complete"] is True
    assert data["has_more"] is False
    assert [value["id"] for value in data["items"]] == [value["Id"] for value in received]
    for value in data["items"]:
        assert type(value) is dict
        assert not {"__object_id", "__object_kind", "ref"} & value.keys()
        assert value["issues"] == ["缺少目标字幕"]
        assert value["resolution"] is None


@pytest.mark.parametrize("arguments", [
    {"start_index": -1}, {"start_index": True}, {"start_index": False},
    {"start_index": "0"}, {"start_index": 0.5},
    {"limit": 0}, {"limit": -1}, {"limit": True}, {"limit": False},
    {"limit": "2"}, {"limit": 1.5}, {"limit": None},
    {"min_resolution": 480}, {"min_resolution": 1080.0}, {"min_resolution": []},
    {"subtitle_language": "english"}, {"subtitle_language": []},
    {"library_id": True},
])
def test_invalid_arguments_fail_before_request(arguments: dict[str, Any]):
    client = Mock()
    request: dict[str, Any] = {"library_id": None, **arguments}
    with pytest.raises(LibraryQualityError) as raised:
        inspect_library_quality(
            client, "viewer", **request,
            server_label="Jellyfin", source="jellyfin_api"
        )
    assert raised.value.code == "invalid_arguments"
    client._request.assert_not_called()


def test_request_failure_is_not_reported_as_a_healthy_empty_page():
    client = Mock()
    error = ValueError("媒体服务器请求失败")
    client._request.side_effect = error
    with pytest.raises(ValueError) as raised:
        inspect_library_quality(
            client, "viewer", library_id=None,
            server_label="Jellyfin", source="jellyfin_api"
        )
    assert raised.value is error
