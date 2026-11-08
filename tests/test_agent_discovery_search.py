"""公网搜索/详情通过普通 SDK 复用真实业务服务；不依赖旧工具投影。"""
from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest

from app import sdk
from app.clients.tmdb import TMDBClient
from app.discovery.models import MediaCard, ProviderTimeout
from app.discovery.providers.tmdb import TMDBProvider
from app.discovery.search import DiscoverySearchService, TMDBSearchProvider
from app.discovery.service import DiscoveryService
from tests.support import FakeHttpSession, FakeJsonResponse, isolated_test_database


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("DISCOVERY_ENABLED", "1")
    monkeypatch.setenv("DISCOVERY_DOUBAN_ENABLED", "1")


def card(number=1, **values):
    fields: dict[str, Any] = {"provider": "tmdb", "external_id": str(number), "media_type": "movie",
                              "title": "影片", "year": "2026", **values}
    return MediaCard(**fields)


@pytest.mark.parametrize("arguments", [
    {"query": ""}, {"query": "x\ny"}, {"query": 42}, {"query": "x", "page": True},
    {"query": "x", "page": 101}, {"query": "x", "providers": []},
    {"query": "x", "providers": "tmdb"}, {"query": "x", "providers": [1]},
    {"query": "x", "providers": ["unknown"]}, {"query": "x", "year": "1899"},
    {"query": "x", "media_type": "all"}, {"query": "x" * 120, "region": "日本"},
    {"query": "x", "genre": "\0"},
])
def test_search_rejects_invalid_constraints_before_provider(enabled, arguments):
    provider = SimpleNamespace(search=Mock(return_value=([], False)))
    service = DiscoverySearchService(providers={"tmdb": provider})
    try:
        with patch("app.discovery.search.get_discovery_search_service", return_value=service), pytest.raises(ValueError):
            sdk.media.search(**arguments)
        provider.search.assert_not_called()
    finally:
        service.shutdown()


@pytest.mark.parametrize("call,getter", [
    (lambda: sdk.media.search("x"), "app.discovery.search.get_discovery_search_service"),
    (lambda: sdk.media.detail("tmdb", "tv", "1"), "app.discovery.service.get_discovery_service"),
    (lambda: sdk.media.catalog("tmdb", "discover", "movie"), "app.discovery.service.get_discovery_service"),
    (lambda: sdk.media.catalog("tmdb", "discover", "movie", limit=2), "app.discovery.service.get_discovery_service"),
    (lambda: sdk.media.catalog("bangumi", "calendar", "tv"), "app.discovery.service.get_discovery_service"),
    (lambda: sdk.media.calendar(), "app.discovery.calendar.service.get_calendar_service"),
])
def test_disabled_metadata_never_constructs_service(monkeypatch, call, getter):
    monkeypatch.setenv("DISCOVERY_ENABLED", "0")
    with patch(getter) as factory, pytest.raises(ValueError, match="关闭"):
        call()
    factory.assert_not_called()


@pytest.mark.parametrize("arguments,query,ids", [
    ({"media_type": "tv", "year": "2026"}, "沙丘2", ["2"]),
    ({"year": "2026"}, "沙丘2", ["1", "2"]),
    ({"region": "日本", "genre": "动画"}, "沙丘2 日本 动画", ["1", "2", "3"]),
])
def test_search_filters_keep_title_and_actual_provider_page(enabled, arguments, query, ids):
    provider = SimpleNamespace(search=Mock(return_value=(
        [card(), card(2, media_type="tv"), card(3, media_type="tv", year="2025")], True)))
    service = DiscoverySearchService(providers={"tmdb": provider})
    try:
        with patch("app.discovery.search.get_discovery_search_service", return_value=service):
            result = sdk.media.search(" 沙丘２ ", providers=["TMDB", "tmdb"], **arguments)
        provider.search.assert_called_once_with(query, 1)
        assert [item["external_id"] for item in result["items"]] == ids
        assert result["has_more"] is True and result["providers_succeeded"] == ("tmdb",)
    finally:
        service.shutdown()


@pytest.mark.parametrize("cards,failed,status", [
    ([card()], False, "success"), ([], False, "empty"), ([card()], True, "partial"), ([], True, "unavailable"),
])
def test_search_partial_empty_and_failure_are_distinct(enabled, cards, failed, status):
    providers = {"tmdb": SimpleNamespace(search=Mock(return_value=(cards, False)))}
    if failed:
        providers["douban"] = SimpleNamespace(search=Mock(side_effect=ProviderTimeout("token=private")))
        if not cards:
            providers["tmdb"].search.side_effect = ProviderTimeout("token=private")
    service = DiscoverySearchService(providers=providers)
    try:
        with patch("app.discovery.search.get_discovery_search_service", return_value=service):
            result = sdk.media.search("作品")
        assert result["status"] == status
        assert result["ok"] is (status != "unavailable")
        assert result["partial"] is (status == "partial")
        assert "private" not in str(result)
    finally:
        service.shutdown()


def test_search_wait_is_cancelled_without_closing_inflight_provider(enabled):
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    def search(*_):
        entered.set()
        assert release.wait(3)
        return [], False
    provider = SimpleNamespace(search=search, close=Mock())
    service = DiscoverySearchService(providers={"tmdb": provider})
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(service.search, "作品", cancelled=cancelled.is_set)
            assert entered.wait(2)
            cancelled.set()
            with pytest.raises(RuntimeError, match="取消"):
                future.result(timeout=1)
            assert service.shutdown() is False
            provider.close.assert_not_called()
            release.set()
    finally:
        release.set()
        service.shutdown()


class AgentDiscoveryIdentityRegressionTests(unittest.TestCase):
    # 数据库隔离套件直接引用该行为节点，保留名称；执行链已改为 SDK。
    def test_tmdb_agent_search_keeps_chinese_query_on_single_http_request(self):
        http = FakeHttpSession([FakeJsonResponse({"results": [
            {"id": 75787, "media_type": "tv", "name": "狐妖小红娘", "first_air_date": "2015-06-25"},
            {"id": 205599, "media_type": "tv", "name": "狐妖小红娘月红篇", "first_air_date": "2024-05-23"},
        ], "total_pages": 1})])
        client = TMDBClient(api_key="test-key", proxy_url="", base_url="https://tmdb.invalid/3", session=http)
        service = DiscoverySearchService(providers={"tmdb": TMDBSearchProvider(client=client)})
        try:
            with patch("app.config.get_bool", return_value=True), patch("app.discovery.search.get_discovery_search_service", return_value=service):
                result = sdk.media.search("狐妖小红娘", media_type="tv", year="2015", providers=["tmdb"])
            self.assertEqual(len(http.calls), 1)
            self.assertEqual(http.calls[0][2]["params"]["query"], "狐妖小红娘")
            self.assertEqual([item["external_id"] for item in result["items"]], ["75787"])
        finally:
            service.shutdown()


@pytest.mark.parametrize("provider", ["tmdb", "douban", "bangumi"])
def test_detail_preserves_identity_unknown_counts_and_confirmation(enabled, provider):
    with isolated_test_database():
        service = DiscoveryService()
        item = card(42, provider=provider, media_type="tv")
        try:
            with patch.object(service, "get_detail", return_value=item), patch("app.discovery.service.get_discovery_service", return_value=service):
                for confirmed in (False, True):
                    with patch("app.database.get_media_external_id", return_value={"confirmed": confirmed}):
                        result = sdk.media.detail(provider, "tv", "42")["item"]
                    assert result["external_id"] == "42" and result["stable_id"] == f"{provider}:tv:42"
                    assert result["mapping_confirmed"] is (provider == "tmdb" or confirmed)
                    assert result["number_of_seasons"] is None and result["number_of_episodes"] is None and result["seasons"] is None
                    assert result["credits_state"] == "not_queried"
        finally:
            service.shutdown()


def test_detail_keeps_specials_and_full_source_counts_without_mutation(enabled):
    item = TMDBProvider._card({"id": 75787, "name": "作品", "number_of_seasons": 1, "number_of_episodes": 183,
                              "seasons": [{"season_number": 0, "name": "特别篇", "episode_count": 29},
                                          {"season_number": 1, "name": "全集", "episode_count": 183}]}, "tv")
    with isolated_test_database():
        service = DiscoveryService()
        try:
            with patch.object(service, "get_detail", return_value=item), patch("app.discovery.service.get_discovery_service", return_value=service):
                result = sdk.media.detail("tmdb", "tv", "75787")["item"]
            assert result["number_of_seasons"] == 1 and result["number_of_episodes"] == 183
            assert [(s["season_number"], s["episode_count"]) for s in result["seasons"]] == [(0, 29), (1, 183)]
            assert item is not None and item.seasons is not None and item.seasons[0]["name"] == "特别篇"
            with patch.object(service, "get_detail", return_value=None):
                assert service.describe_media("tmdb", "tv", "1") is None
            with patch.object(service, "get_detail", side_effect=ProviderTimeout("超时")), pytest.raises(ProviderTimeout):
                service.describe_media("tmdb", "tv", "1")
        finally:
            service.shutdown()
