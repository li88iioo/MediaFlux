from __future__ import annotations

from unittest.mock import Mock

import pytest

from app.agent.library_quality_actions import (
    inspect_library_quality,
    inspect_quality_item,
)
from app.agent.provider_models import ProviderGatewayError
from app.agent.provider_projection import project_provider_value


def item(**kwargs):
    return {"Id": "movie-1", "Name": "测试影片", "Type": "Movie", **kwargs}


def inspect(value, min_resolution=1080, language="chinese"):
    return inspect_quality_item(
        value, min_resolution=min_resolution, subtitle_language=language
    )


def test_missing_streams_and_size_remain_unknown():
    result = inspect(item())
    assert result["resolution"] is None
    assert result["subtitle_state"] == "unknown"
    assert result["size_bytes"] is None
    assert result["availability"] == "unknown"
    assert result["issues"] == []


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [(1920, 800, 1080), (3840, 1600, 2160), (1280, 544, 720), (720, 480, 480)],
)
def test_cinemascope_resolution_is_not_misclassified(width, height, expected):
    result = inspect(
        item(MediaStreams=[{"Type": "Video", "Width": width, "Height": height}])
    )
    assert result["resolution"] == expected


@pytest.mark.parametrize(
    ("streams", "has", "expected"),
    [
        ([{"Type": "Subtitle", "Language": "chi"}], True, "present"),
        ([{"Type": "Subtitle", "Language": "eng"}], True, "missing"),
        ([{"Type": "Subtitle", "Language": ""}], True, "unknown"),
        ([], True, "unknown"),
        ([], False, "missing"),
        ([], None, "unknown"),
    ],
)
def test_missing_subtitle_metadata_is_not_missing_subtitle(streams, has, expected):
    assert (
        inspect(item(MediaStreams=streams, HasSubtitles=has))["subtitle_state"]
        == expected
    )


def test_any_subtitles_accepts_explicit_available_flag():
    assert (
        inspect(item(HasSubtitles=True), language="any")["subtitle_state"] == "present"
    )


def test_versions_sizes_and_server_reported_missing():
    result = inspect(item(MediaSources=[{"Size": 100}, {"Size": 200}], IsMissing=True))
    assert result["size_bytes"] == 300
    assert result["version_count"] == 2
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
        {"library_ref": "library1", "limit": 2},
        server_label="Jellyfin",
        source="jellyfin_api",
    )
    assert outcome.data["complete"] is False
    assert outcome.data["has_more"] is True
    assert outcome.data["next_start_index"] == 2
    assert outcome.data["duplicate_groups"] == 1
    assert outcome.data["duplicate_scope"] == "仅本页内检查"
    assert outcome.data["unknown_resolution"] == 2
    assert not outcome.data["playability_verified"]
    assert client._request.call_args.kwargs["params"]["ParentId"] == "library1"


def test_last_page_never_claims_full_library_complete():
    client = Mock()
    client._request.return_value = {"Items": [item()], "TotalRecordCount": 101}
    data = inspect_library_quality(
        client,
        "viewer",
        {"start_index": 100},
        server_label="Jellyfin",
        source="jellyfin_api",
    ).data
    assert data["has_more"] is False
    assert data["complete"] is False


def test_unknown_total_is_never_full_library_and_keeps_page_cursor():
    client = Mock()
    client._request.return_value = {"Items": [item()]}
    data = inspect_library_quality(
        client, "viewer", {"limit": 1}, server_label="Jellyfin", source="jellyfin_api"
    ).data
    assert data["reported_total"] is None
    assert data["next_start_index"] == 1
    assert data["complete"] is False


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"Items": [{}]},
        {"Items": [item()], "TotalRecordCount": 0},
        {"Items": [], "TotalRecordCount": 1},
    ],
)
def test_invalid_or_changed_pagination_fails_not_fake_healthy(raw):
    client = Mock()
    client._request.return_value = raw
    with pytest.raises(ProviderGatewayError):
        inspect_library_quality(
            client, "viewer", {}, server_label="Jellyfin", source="jellyfin_api"
        )


def test_quality_projection_retains_flags_and_nulls():
    client = Mock()
    client._request.return_value = {
        "Items": [item(HasSubtitles=False)],
        "TotalRecordCount": 1,
    }
    data = inspect_library_quality(
        client, "viewer", {}, server_label="Jellyfin", source="jellyfin_api"
    ).data
    projected = project_provider_value(data)
    assert projected["items"][0]["issues"] == ["缺少目标字幕"]
    assert projected["items"][0]["resolution"] is None
