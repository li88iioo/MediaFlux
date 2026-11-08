"""配置发布后搜索与剧集审计不能复用旧来源缓存或旧在途查询。"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.requests import Request

from app import config, services, sdk
from app.modules import episode_audit
from app.agent.kernel.bootstrap import get_agent_kernel_runtime, invalidate_agent_kernel_runtime
from app.modules.episode_audit import EpisodeAuditResult
from app.clients.base import MediaItem, MediaSearchResult, SeriesCandidate, SeriesEpisodeInventory, SeriesSearchResult
from tests.support import initialized_web_runtime

MEDIA_CONFIG = {
    "EMBY_ENABLED": "0", "EMBY_URL": "http://emby.invalid", "EMBY_TOKEN": "emby-key",
    "JELLYFIN_ENABLED": "1", "JELLYFIN_URL": "http://old-library.invalid", "JELLYFIN_API_KEY": "jellyfin-key",
    "EMBY_USER_ID": "emby-user", "JELLYFIN_USER_ID": "jellyfin-user",
}
TMDB_CONFIG = {"TMDB_API_KEY": "tmdb-key", "TMDB_API_URL": "http://tmdb.invalid/3", "PROXY_URL": ""}
AUDIT = {"query": "The Show", "tmdb_id": "", "season": None, "as_of": "2026-08-01"}


@pytest.fixture(autouse=True)
def clear_caches():
    resets = (services.clear_library_search_cache, episode_audit.reset_episode_audit_cache_for_tests,)
    for reset in resets:
        reset()
    yield
    for reset in resets:
        reset()


@pytest.mark.parametrize("changed_key", [*MEDIA_CONFIG, *TMDB_CONFIG])
def test_effective_source_changes_partition_cache_and_unchanged_reads_stay_hot(monkeypatch, changed_key):
    for key, value in {**MEDIA_CONFIG, **TMDB_CONFIG}.items():
        monkeypatch.setenv(key, value)
    with (
        patch.object(services, "search_media_servers", side_effect=[[{"version": 1}], [{"version": 2}]]) as media,
        patch.object(episode_audit, "_audit_uncached", side_effect=[EpisodeAuditResult(True, "up_to_date", "old"), EpisodeAuditResult(True, "up_to_date", "new")]) as audit,
    ):
        assert services._search_sources("Same query", 8) == [{"version": 1}]
        assert episode_audit.audit_series_episodes(AUDIT).summary == "old"
        assert services._search_sources("Same query", 8) == [{"version": 1}]
        assert episode_audit.audit_series_episodes(AUDIT).summary == "old"
        value = (MEDIA_CONFIG | TMDB_CONFIG)[changed_key]
        monkeypatch.setenv(changed_key, "1" if value == "0" else "0" if value == "1" else value + "-changed")
        expected_search_version = 2 if changed_key in MEDIA_CONFIG else 1
        assert services._search_sources("Same query", 8) == [{"version": expected_search_version}]
        assert episode_audit.audit_series_episodes(AUDIT).summary == "new"
        assert services._search_sources("Same query", 8) == [{"version": expected_search_version}]
        assert episode_audit.audit_series_episodes(AUDIT).summary == "new"
    assert media.call_count == expected_search_version
    assert audit.call_count == 2


def test_search_late_old_backend_response_cannot_replace_new_scope_cache(monkeypatch):
    monkeypatch.setenv("JELLYFIN_URL", "http://old-library.invalid")
    started, release = threading.Event(), threading.Event()
    calls = []

    def read(*_args, **_kwargs):
        url = config.get("JELLYFIN_URL")
        calls.append(url)
        if "old" in url:
            started.set()
            assert release.wait(3)
        return [{"backend": url}]

    with patch.object(services, "search_media_servers", side_effect=read), ThreadPoolExecutor(max_workers=1) as pool:
        try:
            old = pool.submit(services._search_sources, "Same query", 8)
            assert started.wait(3)
            monkeypatch.setenv("JELLYFIN_URL", "http://new-library.invalid")
            assert services._search_sources("Same query", 8) == [{"backend": "http://new-library.invalid"}]
            release.set()
            assert old.result(3) == [{"backend": "http://old-library.invalid"}]
            assert services._search_sources("Same query", 8) == [{"backend": "http://new-library.invalid"}]
            assert len(calls) == 2
        finally:
            release.set()


@pytest.mark.parametrize("old_first", [False, True])
def test_audit_configuration_scopes_have_independent_flights_and_publishers(monkeypatch, old_first):
    monkeypatch.setenv("JELLYFIN_URL", "http://old-library.invalid")
    started = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    joined, guard = threading.Event(), threading.Lock()
    calls = []

    def read(_arguments):
        with guard:
            number = len(calls)
            calls.append(config.get("JELLYFIN_URL"))
        started[number].set()
        assert release[number].wait(3)
        return EpisodeAuditResult(True, "up_to_date", str(number))

    with patch.object(episode_audit, "_audit_uncached", side_effect=read), ThreadPoolExecutor(max_workers=3) as pool:
        try:
            old = pool.submit(episode_audit.audit_series_episodes, AUDIT)
            assert started[0].wait(3)
            monkeypatch.setenv("JELLYFIN_URL", "http://new-library.invalid")
            fresh = pool.submit(episode_audit.audit_series_episodes, AUDIT)
            assert started[1].wait(3)
            flight = list(episode_audit._inflight.values())[-1]
            wait = flight.wait

            def wait_for_new(*args, **kwargs):
                joined.set()
                return wait(*args, **kwargs)

            with patch.object(flight, "wait", side_effect=wait_for_new):
                reader = pool.submit(episode_audit.audit_series_episodes, AUDIT)
                assert joined.wait(3)
                if old_first:
                    release[0].set()
                    assert old.result(3).summary == "0"
                    assert list(episode_audit._inflight.values()) == [flight]
                    assert not reader.done()
                release[1].set()
                assert fresh.result(3).summary == reader.result(3).summary == "1"
                release[0].set()
                assert old.result(3).summary == "0"
            assert episode_audit.audit_series_episodes(AUDIT).summary == "1"
            assert calls == ["http://old-library.invalid", "http://new-library.invalid"]
            assert not episode_audit._inflight
        finally:
            for event in release:
                event.set()


def test_bound_runtime_snapshot_selects_its_own_effective_cache_scope(monkeypatch):
    monkeypatch.setenv("JELLYFIN_URL", "http://new-library.invalid")
    snapshot = config.RuntimeConfigSnapshot(
        file_values={**MEDIA_CONFIG, **TMDB_CONFIG}, startup_overrides={},
        config_digest="old-file", effective_digest="old-effective",
    )
    with (
        patch.object(services, "search_media_servers", side_effect=lambda *_args, **_kwargs: [{"url": config.get("JELLYFIN_URL")}]) as media,
        patch.object(episode_audit, "_audit_uncached", side_effect=lambda _arguments: EpisodeAuditResult(True, "up_to_date", config.get("JELLYFIN_URL"))) as audit,
    ):
        with config.use_runtime_config_snapshot(snapshot):
            assert services._search_sources("Same query", 8) == [{"url": "http://old-library.invalid"}]
            assert episode_audit.audit_series_episodes(AUDIT).summary == "http://old-library.invalid"
        assert services._search_sources("Same query", 8) == [{"url": "http://new-library.invalid"}]
        assert episode_audit.audit_series_episodes(AUDIT).summary == "http://new-library.invalid"
        with config.use_runtime_config_snapshot(snapshot):
            assert services._search_sources("Same query", 8) == [{"url": "http://old-library.invalid"}]
            assert episode_audit.audit_series_episodes(AUDIT).summary == "http://old-library.invalid"
    assert media.call_count == audit.call_count == 2


def test_real_config_save_and_runtime_rebuild_refresh_search_and_episode_inventory():
    from app.routes import api

    calls = []

    class Media:
        def __init__(self, url, _token):
            self.url = url

        def search_media(self, _query, *, cancelled=None, **options):
            calls.append(("search", self.url))
            title = "旧服务器库存" if "old" in self.url else "新服务器库存"
            return MediaSearchResult(items=[MediaItem(id="private-item", name=title, type="movie")], total=1, has_more=False, **options)

        def search_series_candidates(self, _query, **_options):
            return SeriesSearchResult([SeriesCandidate("private-series", "The Show", "2026", "12345")], total=1)

        def list_series_episode_inventory(self, _series_id, **_options):
            calls.append(("inventory", self.url))
            episodes = [(1, 1)] if "old" in self.url else [(1, 1), (1, 2)]
            return SeriesEpisodeInventory(episodes, total=len(episodes))

        def close(self):
            pass

    class TMDB:
        def detail(self, _tmdb_id, _media_type):
            return {"name": "The Show", "first_air_date": "2026-01-01", "seasons": [{"season_number": 1}]}

        def tv_season_detail(self, _tmdb_id, _season):
            return {"episodes": [{"episode_number": episode, "air_date": "2026-07-01"} for episode in (1, 2)]}

        def close(self):
            pass

    async def scenario():
        with (
            initialized_web_runtime(),
            patch("app.services.JellyfinClient", Media),
            patch.object(episode_audit, "TMDBClient", TMDB),
            patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
        ):
            config.set_and_save({**MEDIA_CONFIG, **TMDB_CONFIG, "AGENT_LLM_API_URL": "http://model.invalid/v1", "AGENT_LLM_MODEL": "test-model"})
            invalidate_agent_kernel_runtime()
            try:
                async def query():
                    found = await asyncio.to_thread(sdk.library.search, "same-query", limit=8)
                    audited = await asyncio.to_thread(sdk.library.audit, "The Show", as_of="2026-08-01")
                    return found, audited

                old_runtime = get_agent_kernel_runtime()
                before, old_audit = await query()
                await query()
                request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(background_services_enabled=False))})
                with patch.object(api, "require_api_login"):
                    saved = api.save_config(request, {"JELLYFIN_URL": "http://new-library.invalid"})
                assert isinstance(saved, dict) and saved["success"], saved
                new_runtime = get_agent_kernel_runtime()
                assert new_runtime is not old_runtime
                after, new_audit = await query()
                await query()
                assert before["data"]["sources"][0]["items"][0]["name"] == "旧服务器库存"
                assert after["data"]["sources"][0]["items"][0]["name"] == "新服务器库存"
                assert old_audit["data"]["local_episode_count"] == 1
                assert new_audit["data"]["local_episode_count"] == 2
                assert old_audit["status"] == "updates_available" and new_audit["status"] == "up_to_date"
                assert calls == [
                    ("search", "http://old-library.invalid"), ("inventory", "http://old-library.invalid"),
                    ("search", "http://new-library.invalid"), ("inventory", "http://new-library.invalid"),
                ]
                assert after["data"]["sources"][0]["items"][0]["id"] == "private-item"
                for secret in ("jellyfin-key", "tmdb-key"):
                    assert secret not in repr((before, old_audit, after, new_audit))
            finally:
                invalidate_agent_kernel_runtime()

    asyncio.run(scenario())
