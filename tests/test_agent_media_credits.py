"""演职员共用服务与SDK完整事实契约，替代旧工具展示投影测试。"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import Mock, patch

import pytest

from app import sdk
from app.agent.kernel.bootstrap import build_tool_catalog
from app.clients.tmdb import TMDBClient
from app.discovery.models import (
    MediaCard,
    ProviderAuthenticationError,
    ProviderInvalidResponse,
    ProviderNotConfigured,
    ProviderTimeout,
)
from app.discovery.service import DiscoveryService


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.proxies = {}
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = Mock()
        result.status_code = 200
        result.json.return_value = self.payload
        return result

    def close(self):
        self.closed = True


def _call(payload, **kwargs):
    session = _Session(payload)
    client = TMDBClient(api_key="private-secret", base_url="https://api.themoviedb.org/3", proxy_url="", session=session)
    arguments: dict[str, Any] = {"tmdb_id": "272938", "media_type": "tv", **kwargs}
    with patch("app.discovery.people.config.get_bool", return_value=True), patch("app.discovery.people.TMDBClient", return_value=client):
        result = sdk.media.credits(**arguments)
    assert session.closed
    return result, session


def test_movie_credits_preserve_complete_source_roles_order_and_public_fields():
    payload = {"id": 550, "cast": [
        {"id": 2, "name": "演员乙", "character": "配角", "order": 2, "profile_path": "/public-profile.jpg"},
        {"id": 1, "name": "演员甲", "original_name": "Actor A", "character": "主角", "order": 0},
    ], "crew": [
        {"id": 3, "name": "编剧", "job": "Writer", "department": "Writing"},
        {"id": 4, "name": "导演", "job": "Director", "department": "Directing"},
    ]}
    result, session = _call(payload, tmdb_id="550", media_type="movie")
    assert result["ok"] and result["complete"] and result["query_state"] == "available"
    assert result["scope"] == "movie" and result["request"]["tmdb_id"] == "550"
    assert result["cast"] == payload["cast"] and result["crew"] == payload["crew"]
    assert "private-secret" not in json.dumps(result)
    url, kwargs = session.calls[0]
    assert url.endswith("/movie/550/credits") and kwargs["params"]["language"] == "zh-CN"
    assert len(session.calls) == 1


@pytest.mark.parametrize("season,suffix,scope", [
    (None, "/tv/272938/aggregate_credits", "series_aggregate"),
    (0, "/tv/272938/season/0/aggregate_credits", "season_aggregate"),
    (2, "/tv/272938/season/2/aggregate_credits", "season_aggregate"),
    (101, "/tv/272938/season/101/aggregate_credits", "season_aggregate"),
])
def test_tv_aggregate_roles_and_optional_season(season, suffix, scope):
    payload = {"id": 272938 if season is None else 9999, "cast": [
        {"id": 1, "name": "演员甲", "order": 0, "total_episode_count": 26,
         "roles": [{"character": "李长寿", "episode_count": 26}]},
    ], "crew": [{"id": 2, "name": "导演乙", "department": "Directing", "total_episode_count": 26,
                 "jobs": [{"job": "Director", "episode_count": 26}]}]}
    result, session = _call(payload, season_number=season)
    assert result["ok"] and result["scope"] == scope
    assert session.calls[0][0].endswith(suffix)
    assert result["cast"] == payload["cast"] and result["crew"] == payload["crew"]


def test_default_full_cast_and_nested_roles_have_no_display_cap_and_pages_resume():
    roles = [{"character": f"角色{i}", "episode_count": i} for i in range(27)]
    payload = {"cast": [{"id": i + 1, "name": f"演员{i}", "roles": roles} for i in range(137)],
               "crew": [{"id": 200, "name": "导演", "job": "Director"}]}
    full, _ = _call(payload)
    first, _ = _call(payload, limit=101)
    last, _ = _call(payload, limit=101, offset=first["next_offset"])
    assert full["cast_total"] == full["cast_returned"] == 137 and full["complete"]
    assert len(full["cast"][-1]["roles"]) == 27 and full["cast"][-1]["id"] == 137
    assert first["status"] == "completed" and first["query_state"] == "available"
    assert first["cast_total"] == last["cast_total"] == 137
    assert first["has_more"] and first["next_offset"] == 101 and not first["complete"]
    assert first["cast"] + last["cast"] == full["cast"]
    assert first["crew"] + last["crew"] == full["crew"]
    assert last["cast_returned"] == 36 and last["crew_returned"] == 0
    assert not last["has_more"] and last["next_offset"] is None
    assert not last["complete"]  # 一页结束不代表这一页包含完整源表。


def test_invalid_unknown_rows_are_preserved_and_mark_source_incomplete():
    payload = {"cast": [
        {"id": 1, "name": "甲", "roles": [{"character": "甲", "episode_count": -3}]},
        {"id": 2, "name": "乙"}, None,
    ], "crew": [{"name": "导演", "jobs": "not-a-list"}]}
    result, _ = _call(payload)
    assert result["ok"] and result["status"] == result["query_state"] == "partial"
    assert result["source_incomplete"] and not result["complete"]
    assert result["cast"] == payload["cast"] and result["crew"] == payload["crew"]
    assert result["cast_total"] == 3 and result["crew_total"] == 1


def test_empty_provider_arrays_are_factual_empty_not_not_queried():
    result, _ = _call({"cast": [], "crew": []})
    assert result["ok"] and result["query_state"] == "empty" and result["complete"]
    assert result["cast"] == result["crew"] == [] and result["cast_total"] == result["crew_total"] == 0
    assert "空表仅是本次TMDB结果" in (sdk.media.credits.__doc__ or "")


@pytest.mark.parametrize("payload", [{}, {"cast": []}, {"cast": None, "crew": []}, {"cast": {}, "crew": []}])
def test_missing_or_invalid_fields_do_not_become_empty_cast(payload):
    result, _ = _call(payload)
    assert not result["ok"] and result["status"] == "invalid_response" and result["query_state"] == "failed"
    assert "cast" not in result and "crew" not in result


@pytest.mark.parametrize("payload,args", [
    ({"id": 2, "cast": [], "crew": []}, {"tmdb_id": "1"}),
    ({"season_number": 2, "cast": [], "crew": []}, {"season_number": 0}),
])
def test_explicit_wrong_media_identity_or_season_is_not_accepted(payload, args):
    result, _ = _call(payload, **args)
    assert not result["ok"] and result["status"] == "invalid_response" and result["query_state"] == "failed"
    assert "cast" not in result


@pytest.mark.parametrize("method,args", [("credits", ("1", "movie")), ("filmography", ("525",)), ("people", ("诺兰",))])
@pytest.mark.parametrize("exc,state,status", [
    (ProviderNotConfigured("未配置 TMDB_API_KEY"), "not_queried", "not_configured"),
    (ProviderTimeout("请求超时", detail="api_key=private-secret"), "failed", "timeout"),
    (ProviderAuthenticationError("认证失败", detail="secret-token"), "failed", "authentication"),
    (OSError("private-secret"), "failed", "unavailable"),
])
def test_failures_are_factual_sanitized_and_close_client(method, args, exc, state, status):
    client = Mock()
    getattr(client, {"credits": "media_credits", "filmography": "person_movie_credits", "people": "search_people"}[method]).side_effect = exc
    with patch("app.discovery.people.config.get_bool", return_value=True), patch("app.discovery.people.TMDBClient", return_value=client):
        result = getattr(sdk.media, method)(*args)
    assert not result["ok"] and result["status"] == status and result["query_state"] == state
    assert "cast" not in result and "items" not in result
    assert not any(secret in json.dumps(result) for secret in ("private-secret", "secret-token"))
    client.close.assert_called_once()


@pytest.mark.parametrize("method,args", [("credits", ("1", "movie")), ("filmography", ("525",)), ("people", ("诺兰",))])
def test_disabled_does_not_instantiate_provider_or_claim_empty(method, args):
    with patch("app.discovery.people.config.get_bool", return_value=False), patch("app.discovery.people.TMDBClient") as factory:
        result = getattr(sdk.media, method)(*args)
    factory.assert_not_called()
    assert result["status"] == "disabled" and result["query_state"] == "not_queried"
    assert "cast" not in result and "items" not in result


@pytest.mark.parametrize("bad", [
    {"tmdb_id": True}, {"tmdb_id": "0"}, {"tmdb_id": "１２３"}, {"tmdb_id": "1/credits"},
    {"media_type": []}, {"season_number": 1}, {"media_type": "tv", "season_number": True},
    {"media_type": "tv", "season_number": -1}, {"limit": 0}, {"limit": True}, {"offset": -1}, {"offset": False},
])
def test_invalid_identity_or_paging_is_rejected_before_provider(bad):
    with patch("app.discovery.people.TMDBClient") as factory, pytest.raises(ValueError):
        sdk.media.credits(**({"tmdb_id": "1", "media_type": "movie"} | bad))
    factory.assert_not_called()


def test_detail_explicitly_marks_credits_as_not_queried():
    service = Mock(spec=DiscoveryService)
    service.get_detail.return_value = MediaCard("tmdb", "272938", "tv", "真人剧")
    service.describe_media.side_effect = lambda *args: DiscoveryService.describe_media(service, *args)
    with patch("app.config.get_bool", return_value=True), patch("app.discovery.service.get_discovery_service", return_value=service):
        result = sdk.media.detail("tmdb", "tv", "272938")
    assert result["item"]["credits_state"] == "not_queried"
    assert not {"cast", "crew"}.intersection(result["item"])
    service.get_detail.assert_called_once_with("tmdb", "tv", "272938")


def test_credits_and_filmography_native_reads_replace_old_specs():
    assert {"media.people", "media.credits", "media.filmography"} <= sdk.METHODS.keys()
    assert {"media.people", "media.credits", "media.filmography"}.isdisjoint(sdk.WRITE_METHODS)
    assert {"discovery.credits", "discovery.person_filmography"}.isdisjoint(tool.name for tool in build_tool_catalog().visible())


def test_client_rejects_movie_season_before_http_and_missing_tables_after_http():
    session = _Session({"id": 1})
    client = TMDBClient(api_key="private", proxy_url="", session=session)
    try:
        with pytest.raises(ValueError):
            client.media_credits("1", "movie", season_number=1)
        assert not session.calls
        with pytest.raises(ProviderInvalidResponse):
            client.media_credits("1", "tv")
    finally:
        client.close()
    assert len(session.calls) == 1 and session.closed
