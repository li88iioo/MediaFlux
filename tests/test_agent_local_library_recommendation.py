from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from app.agent.errors import AgentToolError
from app.agent.library_recommendation_actions import (
    get_library_recommendations,
    library_recommendation_arguments,
)
from app.agent.models import ToolContext, ToolResult
from app.agent.providers.media_recommendation import rank_local_recommendations
from app.clients.base import MediaItem, MediaRecommendationCandidate
from app.modules.media_server_profiles import MediaServerProfile


def _candidate(
    item_id: str,
    name: str,
    *,
    original_title: str,
    genres: tuple[str, ...],
    tags: tuple[str, ...],
    rating: float,
    watched: bool = False,
) -> MediaRecommendationCandidate:
    return MediaRecommendationCandidate(
        id=item_id,
        name=name,
        media_type="tv",
        year="2024",
        original_title=original_title,
        genres=genres,
        tags=tags,
        community_rating=rating,
        watched=watched,
    )


def test_rank_local_recommendations_uses_metadata_history_and_excludes_played():
    candidates = [
        _candidate(
            "watched",
            "碧蓝之海",
            original_title="ぐらんぶる",
            genres=("动画", "喜剧"),
            tags=("搞笑", "日常"),
            rating=8.9,
            watched=True,
        ),
        _candidate(
            "best",
            "男子高中生的日常",
            original_title="男子高校生の日常",
            genres=("动画", "喜剧"),
            tags=("搞笑", "日常", "无厘头"),
            rating=8.7,
        ),
        _candidate(
            "serious",
            "严肃日番",
            original_title="しんこく",
            genres=("动画", "剧情"),
            tags=("严肃",),
            rating=9.6,
        ),
        _candidate(
            "western",
            "欧美动画喜剧",
            original_title="Comedy Show",
            genres=("动画", "喜剧"),
            tags=("搞笑",),
            rating=9.0,
        ),
    ]
    result = rank_local_recommendations(
        candidates,
        [
            MediaItem(
                id="history-episode-1",
                name="第 1 集",
                type="Episode",
                series_id="history-series",
                series_name="历史喜剧",
                genres=("喜剧",),
            ),
            MediaItem(
                id="history-episode-2",
                name="第 2 集",
                type="Episode",
                series_id="history-series",
                series_name="历史喜剧",
                genres=("喜剧",),
            ),
        ],
        must_match=["动画|Animation"],
        prefer=["日本|Japan|Japanese", "喜剧|Comedy", "搞笑|无厘头|日常"],
        exclude=["恐怖|Horror"],
        min_rating=7.0,
        exclude_played=True,
        limit=3,
    )

    assert result["items"][0]["name"] == "男子高中生的日常"
    assert "日本|Japan|Japanese" in result["items"][0]["matched_preferences"]
    assert "碧蓝之海" not in [item["name"] for item in result["items"]]
    assert result["excluded"]["played_or_started"] == 1
    assert result["history"]["records_used"] == 2
    assert result["history"]["unique_works_used"] == 1
    assert result["history"]["genre_signals_used"] == 1
    assert "recent_titles" not in result["history"]
    assert "top_genres" not in result["history"]


def test_rank_local_recommendations_treats_prefer_as_ranking_not_filter():
    candidates = [
        _candidate(
            "preferred",
            "轻松喜剧",
            original_title="Comedy",
            genres=("喜剧",),
            tags=("轻松",),
            rating=7.5,
        ),
        _candidate(
            "fallback",
            "高分剧情",
            original_title="Drama",
            genres=("剧情",),
            tags=("治愈",),
            rating=9.0,
        ),
    ]

    result = rank_local_recommendations(
        candidates,
        [],
        must_match=[],
        prefer=["喜剧|Comedy"],
        exclude=[],
        min_rating=0.0,
        exclude_played=True,
        limit=2,
    )

    assert [item["name"] for item in result["items"]] == ["轻松喜剧", "高分剧情"]
    assert result["matched_count"] == 2
    assert result["excluded"]["required_terms"] == 0


def test_library_recommendation_arguments_are_bounded_and_support_synonyms():
    normalized = library_recommendation_arguments(
        {
            "media_type": "tv",
            "must_match": ["动画|Animation"],
            "prefer": ["喜剧|Comedy"],
            "exclude_played": True,
            "min_rating": 7.5,
            "limit": 6,
        }
    )
    assert normalized["server"] == "auto"
    assert normalized["must_match"] == ["动画|Animation"]
    with pytest.raises(AgentToolError):
        library_recommendation_arguments({"prefer": ["x"] * 13})
    with pytest.raises(AgentToolError):
        library_recommendation_arguments({"must_match": ["动画|"]})


def test_library_recommendation_uses_selected_profile_and_provider_gateway():
    profile = MediaServerProfile(
        source="configured:jellyfin",
        server_type="jellyfin",
        label="Jellyfin",
        url="http://private.local",
        credential="secret",
        enabled=True,
        user_id="viewer",
    )
    gateway = Mock()
    gateway.query.return_value = ToolResult(
        True,
        "success",
        "Jellyfin 从本地媒体库筛选出 1 部候选",
        data={"items": [{"name": "男子高中生的日常"}]},
    )
    arguments = {
        "server": "auto",
        "media_type": "tv",
        "must_match": ["动画|Animation"],
        "prefer": ["喜剧|Comedy"],
        "exclude": [],
        "min_rating": 7.0,
        "exclude_played": True,
        "limit": 8,
    }
    with (
        patch(
            "app.agent.media_consumption_actions.list_configured_profiles",
            return_value=[profile],
        ),
        patch(
            "app.agent.library_recommendation_actions.get_provider_gateway",
            return_value=gateway,
        ),
    ):
        result = get_library_recommendations(
            arguments,
            ToolContext(owner="owner", session_id="session"),
        )

    assert result.ok
    call = gateway.query.call_args.kwargs
    assert call["profile_ref"] == "configured:jellyfin"
    assert call["operation"] == "media.items.recommend_from_library"
    assert "server" not in call["arguments"]
    assert call["arguments"]["exclude_played"] is True
