"""人物完整片单直接交公共库存查询，真实ID/年份且一次库存读取。"""
from unittest.mock import Mock, patch

import pytest

from app.clients.base import MediaIdentityCandidate, MediaIdentityInventory
from app.sdk import library, media


@pytest.mark.parametrize("extra", [0, 135])
def test_filmography_identities_feed_one_native_inventory_read(extra):
    tmdb = Mock()
    tmdb.search_people.return_value = {"page": 1, "total_pages": 1, "total_results": 2, "results": [
        {"id": 19508, "name": "诺兰", "known_for_department": "Acting", "popularity": 999},
        {"id": 525, "name": "诺兰", "known_for_department": "Directing", "popularity": 1,
         "known_for": [{"id": 157336, "title": "星际穿越", "media_type": "movie"}]},
    ]}
    tmdb.person_movie_credits.return_value = {"cast": [], "crew": [
        {"id": 157336, "title": "星际穿越", "release_date": "2014-11-05", "department": "Directing", "job": "Director"},
        {"id": 77, "title": "记忆碎片", "release_date": "2000-10-11", "department": "Directing", "job": "Director"},
        *[{"id": 200000 + i, "title": f"电影{i}", "release_date": "2020-01-01", "department": "Directing", "job": "Director"}
          for i in range(extra)],
    ]}
    client = Mock()
    client.list_media_identity_inventory.return_value = MediaIdentityInventory(
        candidates=[MediaIdentityCandidate(tmdb_id="157336", name="星际穿越", year="2014", media_type="movie")], total=1,
    )
    with (
        patch("app.discovery.people.config.get_bool", return_value=True),
        patch("app.discovery.people.TMDBClient", return_value=tmdb),
        patch("app.services._configured_media_sources", return_value=[("jellyfin", "Jellyfin", "", client)]),
    ):
        people = media.people("诺兰")
        assert [p["id"] for p in people["items"]] == [19508, 525]
        # 调用者用已知导演身份核对候选；服务不自动选择高popularity演员。
        director = next(p for p in people["items"] if p["known_for_department"] == "Directing")
        films = media.filmography(str(director["id"]), role="directing", include_upcoming=False)
        result = library.presence(films["library_check_items"])

    assert films["ok"] and films["complete"] and result["ok"] and result["status"] == "success"
    rows = result["data"]["items"]
    assert [(row["tmdb_id"], row["year"], row["library_status"]) for row in rows[:2]] == [
        ("77", "2000", "missing"), ("157336", "2014", "present"),
    ]
    assert result["data"]["total"] == len(rows) == films["total"] == 2 + extra
    assert all(row["media_type"] == "movie" and row["library_status"] == "missing" for row in rows[2:])
    assert tmdb.search_people.call_count == 1
    assert tmdb.search_people.call_args.args == ("诺兰",)
    assert tmdb.search_people.call_args.kwargs["page"] == 1
    args, kwargs = tmdb.person_movie_credits.call_args
    assert args == ("525",) and kwargs["retries"] == 0 and kwargs["deadline_at"] > 0
    client.list_media_identity_inventory.assert_called_once_with("movie", max_items=5000, page_size=200)
    assert tmdb.close.call_count == 2
    client.close.assert_called_once_with()
