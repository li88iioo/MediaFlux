"""Bangumi 日历与普通榜单共用原生单页 catalog。"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from app import sdk
from app.discovery.cache import DiscoveryCache
from app.discovery.models import ProviderUnavailable
from app.discovery.providers.bangumi import BangumiProvider
from app.discovery.registry import ProviderRegistry
from app.discovery.service import DiscoveryService
from tests.support import FakeHttpSession, FakeJsonResponse, isolated_test_database


@pytest.fixture
def calendar(monkeypatch):
    monkeypatch.setenv("DISCOVERY_ENABLED", "1")
    http = FakeHttpSession([FakeJsonResponse([
        {"weekday": {"id": day}, "items": [{"id": day * 100 + i, "name_cn": f"番剧-{day}-{i}",
                                           "air_date": "2026-08-01"} for i in range(1, 25)]}
        for day in range(1, 8)
    ])])
    provider = BangumiProvider(session=http, page_size=30)
    with isolated_test_database():
        service = DiscoveryService(registry=ProviderRegistry({"bangumi": provider}),
                                   cache=DiscoveryCache(clock=lambda: datetime(2026, 9, 10)))
        with patch("app.discovery.service.get_discovery_service", return_value=service):
            try:
                yield http, service
            finally:
                service.shutdown()


def test_catalog_default_is_one_full_source_page_and_weekday_is_not_anime_calendar(calendar):
    http, _ = calendar
    first = sdk.media.catalog("bangumi", "calendar", "tv")
    assert len(first["items"]) > 20 and first["has_more"] is True
    assert {item["provider"] for item in first["items"]} == {"bangumi"}
    second = sdk.media.catalog("bangumi", "calendar", "tv", page=2)
    assert set(item["external_id"] for item in first["items"]).isdisjoint(item["external_id"] for item in second["items"])
    selected = sdk.media.catalog("bangumi", "calendar", "tv", filters={"weekday": "7"})
    assert len(selected["items"]) == 24 and all(item["weekday"] == 7 for item in selected["items"])
    assert not selected["has_more"]
    assert len(http.calls) == 1  # weekday 与分页从同一次完整周历派生。


@pytest.mark.parametrize("arguments", [
    {"page": False}, {"page": 101}, {"filters": {"weekday": "0"}}, {"filters": {"weekday": "8"}},
    {"filters": {"url": "https://untrusted.invalid"}}, {"filters": {"weekday": True}},
    {"limit": 2}, {"exclude_library": True}, {"region": "日本"},
])
def test_calendar_rejects_unsupported_constraints_without_query(calendar, arguments):
    http, _ = calendar
    with pytest.raises(ValueError):
        sdk.media.catalog("bangumi", "calendar", "tv", **arguments)
    assert http.calls == []


def test_catalog_cached_empty_and_provider_failure_remain_distinct(calendar):
    _, service = calendar
    first = sdk.media.catalog("bangumi", "calendar", "tv", page=100)
    assert first["items"] == [] and not first["has_more"] and not first["cached"]
    cached = sdk.media.catalog("bangumi", "calendar", "tv", page=100)
    assert cached["items"] == [] and cached["cached"]
    with patch.object(service.registry.get("bangumi"), "list_items", side_effect=ProviderUnavailable("暂不可用")), pytest.raises(ProviderUnavailable):
        sdk.media.catalog("bangumi", "calendar", "tv", page=99)
