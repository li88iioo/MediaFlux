from __future__ import annotations

from unittest.mock import patch

import pytest

from app.agent.errors import AgentToolError
from app.agent.person_filmography_actions import (
    person_filmography,
    person_filmography_arguments,
)


class _FakeTMDBClient:
    def __init__(self):
        self.closed = False

    def search_people(self, query, limit=10):
        assert query == "诺兰"
        assert limit == 10
        return [
            {
                "id": 19508,
                "name": "诺兰·诺斯",
                "known_for_department": "Acting",
                "popularity": 99,
            },
            {
                "id": 525,
                "name": "克里斯托弗·诺兰",
                "known_for_department": "Directing",
                "popularity": 12,
            },
        ]

    def person_movie_credits(self, person_id):
        assert person_id == "525"
        return {
            "cast": [],
            "crew": [
                {
                    "id": 157336,
                    "title": "星际穿越",
                    "original_title": "Interstellar",
                    "release_date": "2014-11-05",
                    "department": "Directing",
                    "job": "Director",
                },
                {
                    "id": 77,
                    "title": "记忆碎片",
                    "original_title": "Memento",
                    "release_date": "2000-10-11",
                    "department": "Directing",
                    "job": "Director",
                },
                {
                    "id": 999,
                    "title": "未来项目",
                    "release_date": "2099-01-01",
                    "department": "Directing",
                    "job": "Director",
                },
                {
                    "id": 77,
                    "title": "记忆碎片",
                    "release_date": "2000-10-11",
                    "department": "Writing",
                    "job": "Writer",
                },
            ],
        }

    def close(self):
        self.closed = True
        return True


def test_person_filmography_selects_department_and_sorts_released_movies():
    client = _FakeTMDBClient()
    with (
        patch("app.agent.person_filmography_actions.config.get_bool", return_value=True),
        patch("app.agent.person_filmography_actions.TMDBClient", return_value=client),
    ):
        result = person_filmography(
            {
                "person": "诺兰",
                "role": "directing",
                "include_upcoming": False,
                "limit": 50,
            }
        )

    assert result.ok
    assert result.data["person"]["tmdb_person_id"] == "525"
    assert [item["tmdb_id"] for item in result.data["items"]] == ["77", "157336"]
    assert [item["year"] for item in result.data["items"]] == ["2000", "2014"]
    assert result.data["library_check_items"] == [
        {
            "tmdb_id": "77",
            "media_type": "movie",
            "title": "记忆碎片",
            "year": "2000",
        },
        {
            "tmdb_id": "157336",
            "media_type": "movie",
            "title": "星际穿越",
            "year": "2014",
        },
    ]
    assert result.data["excluded_upcoming"] == 1
    assert client.closed


def test_person_filmography_arguments_are_bounded():
    with pytest.raises(AgentToolError):
        person_filmography_arguments({"person": "诺兰", "limit": 101})
    with pytest.raises(AgentToolError):
        person_filmography_arguments({"person": "诺兰", "role": "unknown"})
