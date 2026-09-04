from __future__ import annotations

from unittest.mock import patch

import pytest

from app.agent.errors import AgentToolError
from app.agent.library_batch_presence import (
    batch_library_presence,
    batch_presence_arguments,
)
from app.clients.base import MediaIdentityCandidate, MediaIdentityInventory
from app.services import _media_identity_batch_source_payload

_ITEMS = [
    {"tmdb_id": "77", "media_type": "movie", "title": "记忆碎片", "year": "2000"},
    {"tmdb_id": "157336", "media_type": "movie", "title": "星际穿越", "year": "2014"},
    {"tmdb_id": "872585", "media_type": "movie", "title": "奥本海默", "year": "2023"},
]


class _FakeMediaClient:
    def __init__(self):
        self.calls = 0

    def list_media_identity_inventory(self, media_type, **kwargs):
        self.calls += 1
        assert media_type == "movie"
        assert kwargs == {"max_items": 5000, "page_size": 200}
        return MediaIdentityInventory(
            candidates=[
                MediaIdentityCandidate(
                    name="记忆碎片", year="2000", tmdb_id="77", media_type="movie"
                ),
                MediaIdentityCandidate(
                    name="星际穿越", year="2014", tmdb_id="", media_type="movie"
                ),
            ],
            total=2,
            unmapped=1,
        )


def test_service_batch_presence_enumerates_library_once_per_media_type():
    client = _FakeMediaClient()
    result = _media_identity_batch_source_payload(
        "jellyfin", "Jellyfin", client, _ITEMS
    )

    assert client.calls == 1
    assert [item["status"] for item in result["items"]] == [
        "present",
        "possible",
        "missing",
    ]


def test_batch_presence_aggregates_all_sources_without_per_title_search():
    sources = [
        {
            "server_type": "jellyfin",
            "server_name": "Jellyfin",
            "status": "ready",
            "inventories": {"movie": {"total": 100, "truncated": False, "unmapped": 0}},
            "items": [
                {"tmdb_id": "77", "media_type": "movie", "status": "present", "match": "provider_id"},
                {"tmdb_id": "157336", "media_type": "movie", "status": "missing", "match": "none"},
                {"tmdb_id": "872585", "media_type": "movie", "status": "missing", "match": "none"},
            ],
        }
    ]
    with patch(
        "app.agent.library_batch_presence.inspect_media_identity_batch",
        return_value=sources,
    ) as inspect:
        result = batch_library_presence({"items": _ITEMS})

    inspect.assert_called_once_with(_ITEMS)
    assert result.ok
    assert result.data["counts"] == {
        "present": 1,
        "possible": 0,
        "missing": 2,
        "indeterminate": 0,
    }
    assert [item["library_status"] for item in result.data["items"]] == [
        "present",
        "missing",
        "missing",
    ]


def test_batch_presence_arguments_reject_unbounded_or_invalid_identity_lists():
    with pytest.raises(AgentToolError):
        batch_presence_arguments({"items": []})
    with pytest.raises(AgentToolError):
        batch_presence_arguments(
            {"items": [{"tmdb_id": "0", "media_type": "movie", "title": "X"}]}
        )
