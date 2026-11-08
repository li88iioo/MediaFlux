"""普通 SDK 完整周表契约；实际公共服务与假来源，复用原缓存生命周期。"""
from __future__ import annotations

from datetime import datetime, timedelta
from concurrent.futures import Future
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app import sdk
from app.discovery.cache import DiscoveryCache
from app.discovery.calendar.models import CalendarEntry, CalendarEvent, SourceResult, SourceUnavailable
from app.discovery.calendar.service import CalendarService
from tests.support import isolated_test_database
from tests.test_free_calendar_core import NoNetwork, RawMetadata


class CalendarSource:
    allowed_hosts = frozenset({"v.qq.com", "www.iqiyi.com", "v.youku.com"})

    def __init__(self, source, entries):
        self.source, self.entries, self.calls, self.failure = source, entries, 0, False

    async def fetch(self, _http):
        self.calls += 1
        if self.failure:
            raise SourceUnavailable("公开来源不可用")
        return SourceResult(tuple(self.entries), "partial", "公开样本，非全站", sampled=len(self.entries))


def calendar_service(now=datetime(2026, 9, 10, 12)):
    monday = (now - timedelta(days=now.weekday())).date()
    sources = {}
    for name, url in (("tencent", "https://v.qq.com/x/cover/show.html"),
                      ("iqiyi", "https://www.iqiyi.com/v_show.html"),
                      ("youku", "https://v.youku.com/v_show/id_show.html")):
        entries = [CalendarEntry(name, f"show_{index}", f"动画{index}", "animation", url, evidence="提供者明确排期", events=(
            CalendarEvent((monday + timedelta(days=index % 7)).isoformat(), "10:00", "VIP更新", "member"),
            CalendarEvent((monday + timedelta(days=index % 7)).isoformat(), "", "非会员更新，时间未知", "free"),
        )) for index in range(18)]
        sources[name] = CalendarSource(name, entries)
    return CalendarService(providers=sources, cache=DiscoveryCache(clock=lambda: now),
                           metadata=RawMetadata(), http_factory=NoNetwork, submit=lambda fn: fn(),
                           clock=lambda: now), sources


@pytest.fixture
def calendar(monkeypatch):
    monkeypatch.setenv("DISCOVERY_ENABLED", "1")
    with isolated_test_database():
        service, sources = calendar_service()
        with patch("app.discovery.calendar.service.get_calendar_service", return_value=service):
            try:
                yield service, sources
            finally:
                service.shutdown()


def test_full_week_keeps_all_three_sources_and_member_free_unknown_time(calendar):
    _, sources = calendar
    result = sdk.media.calendar()
    assert result["timezone"] == "Asia/Shanghai" and result["week_start"] == "2026-09-07"
    assert len(result["days"]) == 7
    rows = [row for day in result["days"] for row in day["items"]]
    assert len(rows) == 54 and {row["source"] for row in rows} == set(sources)
    assert all({event["audience"] for event in row["events"]} == {"member", "free"} for row in rows)
    assert all(next(event for event in row["events"] if event["audience"] == "free")["update_time"] == "" for row in rows)
    assert all(source.calls == 1 for source in sources.values())
    assert all(row["fetched_at"] for row in result["sources"])


def test_actual_source_dates_survive_cache_and_legacy_cache_stays_unknown(calendar):
    service, sources = calendar
    dates = tuple(f"2026-09-{day:02d}" for day in range(7, 14))
    source = sources["tencent"]
    with patch.object(source, "fetch", AsyncMock(return_value=SourceResult(
        tuple(source.entries), "partial", covered_dates=dates,
    ))):
        result = sdk.media.calendar()
    state = next(row for row in result["sources"] if row["id"] == "tencent")
    assert state["status"] == "partial" and state["covered_dates"] == list(dates)
    cached = service.cache.get(service.key("tencent"))
    assert cached.payload is not None
    payload = dict(cached.payload)
    assert payload.pop("covered_dates") == list(dates)
    service.cache.set_success(service.key("tencent"), "calendar:tencent", payload,
                              ttl_seconds=3600, stale_seconds=2 * 86400)
    legacy = sdk.media.calendar()
    assert next(row for row in legacy["sources"] if row["id"] == "tencent")["covered_dates"] == []
    assert legacy["days"] == result["days"]
    assert source.calls == 0  # 未将旧缓存无覆盖字段误当作需要重抓。


def test_failure_retains_last_known_facts_and_does_not_claim_refresh(calendar):
    service, sources = calendar
    first = sdk.media.calendar()
    sources["youku"].failure = True
    clock = service.monotonic()
    service.monotonic = lambda: clock + 301
    current = sdk.media.calendar(refresh=True)
    previous = next(source for source in first["sources"] if source["id"] == "youku")
    source = next(source for source in current["sources"] if source["id"] == "youku")
    assert source["status"] == "stale" and source["fetched_at"] == previous["fetched_at"]
    assert len([row for day in current["days"] for row in day["items"] if row["source"] == "youku"]) == 18


def test_published_week_is_usable_while_optional_mapping_remains_pending(calendar):
    service, sources = calendar
    service.metadata = SimpleNamespace(enrich=lambda entries: [
        {**entry.to_dict(), "mapping_status": "pending"} for entry in entries
    ])
    for _ in range(12):
        result = sdk.media.calendar()
        assert not result["refreshing"]
        assert result["metadata_pending"]
        assert len(result["days"]) == 7
        assert sum(len(day["items"]) for day in result["days"]) == 54
        assert all(source["status"] == "partial" for source in result["sources"])
    assert all(source.calls == 1 for source in sources.values())


def test_calendar_cross_year_uses_service_shanghai_week_not_host_clock(monkeypatch):
    monkeypatch.setenv("DISCOVERY_ENABLED", "1")
    with isolated_test_database():
        service, _ = calendar_service(datetime(2026, 12, 31, 23))
        try:
            with patch("app.discovery.calendar.service.get_calendar_service", return_value=service):
                result = sdk.media.calendar()
            assert result["today"] == "2026-12-31"
            assert result["days"][0]["date"] == "2026-12-28"
            assert result["days"][-1]["date"] == "2027-01-03"
            assert not any(day["date"] == "2027-01-04" for day in result["days"])
        finally:
            service.shutdown()


def test_sdk_passes_host_cancel_and_refresh_without_model_cancel_argument(calendar):
    service, _ = calendar
    cancelled = lambda: False
    with patch.object(service, "get_week", wraps=service.get_week) as get_week, sdk.call_owner("owner", cancelled=cancelled):
        sdk.media.calendar(refresh=True)
    get_week.assert_called_once_with(force=True, wait_sources=tuple(service.providers), cancelled=cancelled)
    private: dict[str, Any] = {"cancelled": lambda: True}
    invalid: dict[str, Any] = {"refresh": "true"}
    with pytest.raises(TypeError):
        sdk.media.calendar(**private)
    with pytest.raises(ValueError):
        sdk.media.calendar(**invalid)


def test_calendar_cooperative_cancel_leaves_shared_collection_owned_by_service(calendar):
    service, _ = calendar
    service._submit = lambda _fn: Future()
    with sdk.call_owner("owner", cancelled=lambda: True), pytest.raises(SourceUnavailable, match="停止"):
        sdk.media.calendar()
