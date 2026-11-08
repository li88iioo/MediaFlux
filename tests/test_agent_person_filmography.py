"""明确人物身份的完整作品表；不再让旧Agent工具按姓名热度猜选。"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from app import sdk
from app.clients.tmdb import TMDBClient
from tests.test_agent_media_credits import _Session


def _call(payload, **kwargs):
    session = _Session(payload)
    client = TMDBClient(api_key="private-secret", base_url="https://api.themoviedb.org/3", proxy_url="", session=session)
    arguments: dict[str, Any] = {"person_id": "525", **kwargs}
    with patch("app.discovery.people.config.get_bool", return_value=True), patch("app.discovery.people.TMDBClient", return_value=client):
        result = sdk.media.filmography(**arguments)
    assert session.closed
    return result, session


def _movie(identity, title, released, job="Director", department="Directing"):
    return {"id": identity, "title": title, "release_date": released, "department": department, "job": job}


def test_explicit_person_id_preserves_department_filter_years_and_released_order():
    result, session = _call({"id": 525, "cast": [], "crew": [
        _movie(157336, "星际穿越", "2014-11-05"), _movie(77, "记忆碎片", "2000-10-11"),
        _movie(999, "未来项目", "2099-01-01"), _movie(77, "记忆碎片", "2000-10-11", "Writer", "Writing"),
    ]}, role="directing", include_upcoming=False)
    assert result["ok"] and result["request"]["person_id"] == "525"
    assert [(item["tmdb_id"], item["year"]) for item in result["items"]] == [("77", "2000"), ("157336", "2014")]
    assert all(item["roles"] == [{"kind": "crew", "department": "Directing", "job": "Director"}] for item in result["items"])
    assert result["excluded_upcoming"] == 1 and result["total"] == 2 and result["complete"]
    # 不调用search_people；同名候选及其popularity完全不参与身份选择。
    assert len(session.calls) == 1 and session.calls[0][0].endswith("/person/525/movie_credits")


def test_default_all_roles_and_upcoming_unknown_dates_are_preserved():
    result, _ = _call({"cast": [{"id": 77, "title": "记忆碎片", "release_date": "2000-10-11", "character": "旁白"}],
                       "crew": [_movie(77, "记忆碎片", "2000-10-11"), _movie(77, "记忆碎片", "2000-10-11", "Writer", "Writing"),
                                _movie(999, "未来", "2099-01-01"), _movie(1000, "未知日期", "")]})
    assert result["total"] == result["returned"] == 3 and result["complete"]
    movie, future, unknown = result["items"]
    assert movie["tmdb_id"] == "77" and len(movie["roles"]) == 3 and movie["upcoming"] is False
    assert movie["roles"][0] == {"kind": "cast", "character": "旁白"}
    assert future["upcoming"] is True and unknown["upcoming"] is None and unknown["year"] == ""
    released, _ = _call({"cast": [], "crew": [_movie(1000, "未知日期", "")]}, include_upcoming=False)
    assert released["excluded_undated"] == 1 and released["total"] == 0


@pytest.mark.parametrize("role,ids", [("acting", ["1"]), ("directing", ["2"]), ("writing", ["3"]), ("production", ["4"])])
def test_role_filters_keep_original_department_contract(role, ids):
    result, _ = _call({"cast": [{"id": 1, "title": "演员", "character": "主角"}], "crew": [
        _movie(2, "导演", "2000-01-01"), _movie(3, "编剧", "2000-01-01", "Screenplay", "Writing"),
        _movie(4, "制片", "2000-01-01", "Producer", "Production"), _movie(5, "副导演", "2000-01-01", "Assistant Director"),
        _movie(6, "摄影部门", "2000-01-01", "", "Camera"),
    ]}, role=role)
    assert [row["tmdb_id"] for row in result["items"]] == ids
    assert result["complete"] and result["invalid_credits"] == 0


@pytest.mark.parametrize("role,credit", [
    ("directing", {"job": "Director"}),
    ("directing", {"department": "Directing"}),
    ("writing", {"department": 123, "job": "Screenplay"}),
    ("directing", {"department": "Directing", "job": ["Director"]}),
])
def test_missing_role_filter_facts_are_partial_not_empty(role, credit):
    result, session = _call({"cast": [], "crew": [
        {"id": 77, "title": "记忆碎片", "release_date": "2000-10-11", **credit},
    ]}, role=role, include_upcoming=False)
    assert result["ok"] and result["status"] == result["query_state"] == "partial"
    assert result["source_incomplete"] and not result["complete"] and result["invalid_credits"] == 1
    assert result["items"] == result["library_check_items"] == [] and result["total"] == 0
    assert result["source_counts"] == {"cast": 0, "crew": 1}
    assert len(session.calls) == 1 and session.calls[0][0].endswith("/person/525/movie_credits")


def test_full_table_over_old_cap_and_positive_limit_resumes_without_loss():
    payload = {"id": 525, "cast": [], "crew": [_movie(i, f"作品{i:03}", "2000-01-01") for i in range(1, 138)]}
    # 同一电影多职务不变成重复作品，也不截断角色。
    payload["crew"] += [_movie(137, "作品137", "2000-01-01", f"职务{i}", "Writing") for i in range(27)]
    full, _ = _call(payload)
    first, _ = _call(payload, limit=101)
    last, _ = _call(payload, limit=101, offset=first["next_offset"])
    assert full["total"] == full["returned"] == 137 and full["complete"]
    assert len(full["items"][-1]["roles"]) == 28
    assert first["total"] == last["total"] == 137 and first["next_offset"] == 101
    assert first["items"] + last["items"] == full["items"]
    assert first["has_more"] and not last["has_more"] and last["next_offset"] is None
    assert first["returned"] == 101 and last["returned"] == 36 and not last["complete"]


@pytest.mark.parametrize("payload", [{}, {"cast": []}, {"cast": [], "crew": None}, {"id": 19508, "cast": [], "crew": []}])
def test_missing_table_or_wrong_person_identity_is_failed_not_empty(payload):
    result, _ = _call(payload)
    assert not result["ok"] and result["query_state"] == "failed" and result["status"] == "invalid_response"
    assert "items" not in result


def test_real_empty_and_invalid_rows_are_distinct():
    empty, _ = _call({"id": 525, "cast": [], "crew": []})
    partial, _ = _call({"cast": [None, {"id": "not-id", "title": "不能排库"}], "crew": []})
    assert empty["ok"] and empty["query_state"] == "empty" and empty["complete"] and empty["total"] == 0
    assert partial["ok"] and partial["query_state"] == "partial" and partial["source_incomplete"]
    assert partial["items"] == [] and partial["invalid_credits"] == 2 and not partial["complete"]


@pytest.mark.parametrize("bad", [{"person_id": "诺兰"}, {"person_id": True}, {"person_id": "0"}, {"person_id": "１２３"},
                                  {"role": "unknown"}, {"include_upcoming": 1}, {"limit": 0}, {"offset": -1}])
def test_invalid_person_or_filter_fails_before_provider(bad):
    with patch("app.discovery.people.TMDBClient") as factory, pytest.raises(ValueError):
        sdk.media.filmography(**({"person_id": "525"} | bad))
    factory.assert_not_called()


def test_people_full_source_pages_keep_same_name_identity_without_popularity_selection():
    page1 = {"page": 1, "total_pages": 2, "total_results": 26, "results": [
        {"id": i, "name": "诺兰", "popularity": 999 - i, "known_for_department": "Acting", "known_for": []}
        for i in range(1, 26)]}
    target = {"id": 525, "name": "诺兰", "popularity": 0, "known_for_department": "Directing",
              "known_for": [{"id": 157336, "media_type": "movie", "title": "星际穿越"}]}
    session = _Session(page1)
    client = TMDBClient(api_key="private", proxy_url="", session=session)
    with patch("app.discovery.people.config.get_bool", return_value=True), patch("app.discovery.people.TMDBClient", return_value=client):
        first = sdk.media.people(" 诺兰 ")
    assert session.closed
    assert first["items"] == page1["results"] and first["returned"] == 25 and first["total"] == 26
    assert first["total_pages"] == 2 and first["next_page"] == 2 and first["has_more"]
    # 完整25项源页不受旧20上限影响，不能静默选出高热度演员。
    assert "person_id" not in first and "selected" not in first
    assert session.calls[0][1]["params"]["query"] == "诺兰" and session.calls[0][1]["params"]["page"] == 1
    session = _Session({"page": 2, "total_pages": 2, "total_results": 26, "results": [target]})
    client = TMDBClient(api_key="private", proxy_url="", session=session)
    with patch("app.discovery.people.config.get_bool", return_value=True), patch("app.discovery.people.TMDBClient", return_value=client):
        second = sdk.media.people("诺兰", page=first["next_page"])
    assert session.closed and second["items"] == [target] and second["total"] == 26
    assert second["page"] == 2 and not second["has_more"] and second["next_page"] is None
    assert session.calls[0][1]["params"]["page"] == 2


@pytest.mark.parametrize("payload", [{}, {"page": 1, "results": []},
    {"page": 2, "total_pages": 2, "total_results": 30, "results": []},
    {"page": 1, "total_pages": 1, "total_results": 1, "results": None}])
def test_people_missing_pagination_or_wrong_page_fails_not_empty(payload):
    session = _Session(payload)
    client = TMDBClient(api_key="private", proxy_url="", session=session)
    with patch("app.discovery.people.config.get_bool", return_value=True), patch("app.discovery.people.TMDBClient", return_value=client):
        result = sdk.media.people("诺兰")
    assert session.closed and result["status"] == "invalid_response" and result["query_state"] == "failed"
    assert "items" not in result and "total" not in result


def test_people_empty_source_page_preserves_known_zero_total():
    session = _Session({"page": 1, "total_pages": 0, "total_results": 0, "results": []})
    client = TMDBClient(api_key="private", proxy_url="", session=session)
    with patch("app.discovery.people.config.get_bool", return_value=True), patch("app.discovery.people.TMDBClient", return_value=client):
        result = sdk.media.people("不存在人物")
    assert session.closed and result["ok"] and result["query_state"] == "empty"
    assert result["items"] == [] and result["total"] == result["total_pages"] == 0 and result["next_page"] is None


@pytest.mark.parametrize("args", [("", 1), (None, 1), ("诺兰", 0), ("诺兰", True)])
def test_people_invalid_query_or_page_does_not_query_provider(args):
    with patch("app.discovery.people.TMDBClient") as factory, pytest.raises(ValueError):
        sdk.media.people(*args)
    factory.assert_not_called()
