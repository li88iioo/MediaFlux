"""探索业务编排：缓存、Provider、收藏与跨来源映射。"""
from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from typing import Any, Callable

from app import config, database
from app.discovery.cache import CacheLookup, DiscoveryCache
from app.discovery.models import (
    DiscoveryPage, MediaCard, ProviderAuthenticationError, ProviderError,
    ProviderInvalidResponse, ProviderNotConfigured, ProviderRateLimited, ProviderTimeout,
    ProviderUnavailable,
)
from app.discovery.registry import (
    ProviderRegistry,
    build_default_registry,
    list_filter_definitions,
    list_section_definitions,
    validate_filters,
    validate_request,
)

_ERROR_TYPES = {
    "authentication": ProviderAuthenticationError,
    "invalid_response": ProviderInvalidResponse,
    "not_configured": ProviderNotConfigured,
    "rate_limited": ProviderRateLimited,
    "timeout": ProviderTimeout,
    "unavailable": ProviderUnavailable,
}


class DiscoveryService:
    def __init__(
        self,
        *,
        registry: ProviderRegistry | None = None,
        cache: DiscoveryCache | None = None,
        cache_ttl_seconds: int | None = None,
        stale_ttl_seconds: int | None = None,
        refresh_submit: Callable[[Callable[[], None]], Any] | None = None,
        refresh_clock: Callable[[], float] | None = None,
        scraper_factory: Callable[[], Any] | None = None,
    ):
        self.registry = registry or build_default_registry()
        self.cache = cache or DiscoveryCache()
        self.cache_ttl_seconds = max(60, int(cache_ttl_seconds or config.get_int("DISCOVERY_CACHE_TTL_SECONDS", 3600)))
        self.stale_ttl_seconds = max(
            self.cache_ttl_seconds,
            int(stale_ttl_seconds or config.get_int("DISCOVERY_STALE_TTL_SECONDS", 604800)),
        )
        self._refresh_guard = threading.Lock()
        self._pending_refreshes: set[str] = set()
        self._refresh_cooldowns: dict[str, float] = {}
        self._refresh_clock = refresh_clock or time.monotonic
        self._refresh_cooldown_seconds = 30.0
        self._shutdown = False
        self._executor: ThreadPoolExecutor | None = None
        if refresh_submit is None:
            self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="discovery-refresh")
            self._refresh_submit = self._executor.submit
        else:
            self._refresh_submit = refresh_submit
        if scraper_factory is None:
            from app.modules.scraper import TMDBScraper
            scraper_factory = TMDBScraper
        self._scraper_factory = scraper_factory

    def list_sections(self) -> list[dict[str, Any]]:
        return list_section_definitions()

    @staticmethod
    def list_filters(provider: str, media_type: str) -> dict[str, Any]:
        return list_filter_definitions(provider, media_type)

    def list_items(self, provider: str, category: str, media_type: str,
                   page: int = 1, filters: dict[str, Any] | None = None) -> DiscoveryPage:
        page = int(page)
        if page < 1 or page > 100:
            raise ValueError("页码必须在 1 到 100 之间")
        provider, category, media_type = validate_request(provider, category, media_type)
        normalized = validate_filters(provider, category, media_type, filters)
        cache_key = self.cache.make_key(provider, category, media_type, page, normalized)
        lookup = self.cache.get(cache_key)
        if lookup.status == "fresh" and lookup.payload:
            return self._decorate(DiscoveryPage.from_dict(lookup.payload), cached=True, stale=False)
        if lookup.status == "error":
            raise self._cached_error(lookup)
        if lookup.status == "stale" and lookup.payload:
            old_page = self._decorate(DiscoveryPage.from_dict(lookup.payload), cached=True, stale=True)
            self._schedule_refresh(cache_key, provider, category, media_type, page, normalized)
            return old_page
        return self._load_current(cache_key, provider, category, media_type, page, normalized)

    def _load_current(self, cache_key: str, provider: str, category: str,
                      media_type: str, page: int, filters: dict[str, str]) -> DiscoveryPage:
        with self.cache.singleflight(cache_key):
            lookup = self.cache.get(cache_key)
            if lookup.status == "fresh" and lookup.payload:
                return self._decorate(DiscoveryPage.from_dict(lookup.payload), cached=True, stale=False)
            if lookup.status == "error":
                raise self._cached_error(lookup)
            try:
                result = self._fetch_and_store(cache_key, provider, category, media_type, page, filters)
                return self._decorate(result, cached=False, stale=False)
            except ProviderError:
                fallback = self.cache.get(cache_key)
                if fallback.status == "stale" and fallback.payload:
                    return self._decorate(DiscoveryPage.from_dict(fallback.payload), cached=True, stale=True)
                raise

    def _fetch_and_store(self, cache_key: str, provider: str, category: str,
                         media_type: str, page: int, filters: dict[str, str]) -> DiscoveryPage:
        adapter = self.registry.get(provider)
        try:
            result = adapter.list_items(category, media_type, page, filters)
        except ProviderError as exc:
            self.cache.set_error(
                cache_key, provider, exc.safe_message, ttl_seconds=30,
                code=exc.code, status_code=exc.status_code, retry_after=exc.retry_after,
            )
            raise
        if not isinstance(result, DiscoveryPage):
            raise TypeError("Provider must return DiscoveryPage")
        payload = replace(result, cached=False, stale=False).to_dict()
        self.cache.set_success(
            cache_key,
            provider,
            payload,
            ttl_seconds=self._provider_ttl(provider),
            stale_seconds=self.stale_ttl_seconds,
        )
        return replace(result, cached=False, stale=False)

    @staticmethod
    def _cached_error(lookup: CacheLookup) -> ProviderError:
        error_type = _ERROR_TYPES.get(lookup.error_code, ProviderUnavailable)
        return error_type(
            lookup.last_error or "数据源暂不可用", retry_after=lookup.retry_after
        )

    def _schedule_refresh(self, cache_key: str, provider: str, category: str,
                          media_type: str, page: int, filters: dict[str, str]) -> None:
        with self._refresh_guard:
            now = self._refresh_clock()
            retry_at = self._refresh_cooldowns.get(cache_key, 0.0)
            if self._shutdown or cache_key in self._pending_refreshes or retry_at > now:
                return
            self._refresh_cooldowns.pop(cache_key, None)
            self._pending_refreshes.add(cache_key)

        def refresh() -> None:
            refreshed = False
            try:
                refreshed = self._refresh_safely(
                    cache_key, provider, category, media_type, page, filters,
                )
            finally:
                with self._refresh_guard:
                    if refreshed:
                        self._refresh_cooldowns.pop(cache_key, None)
                    else:
                        self._refresh_cooldowns[cache_key] = (
                            self._refresh_clock() + self._refresh_cooldown_seconds
                        )
                    self._pending_refreshes.discard(cache_key)

        try:
            self._refresh_submit(refresh)
        except RuntimeError:
            with self._refresh_guard:
                self._pending_refreshes.discard(cache_key)

    def _refresh_safely(self, cache_key: str, provider: str, category: str,
                        media_type: str, page: int, filters: dict[str, str]) -> bool:
        try:
            with self.cache.singleflight(cache_key):
                lookup = self.cache.get(cache_key)
                if lookup.status in {"fresh", "error"}:
                    return True
                self._fetch_and_store(cache_key, provider, category, media_type, page, filters)
                return True
        except (ProviderError, TypeError, ValueError):
            return False

    def _provider_ttl(self, provider: str) -> int:
        if provider == "douban":
            return max(300, config.get_int("DOUBAN_CACHE_TTL_SECONDS", 21600))
        if provider == "bangumi":
            return max(300, config.get_int("BANGUMI_CACHE_TTL_SECONDS", 21600))
        return self.cache_ttl_seconds

    @staticmethod
    def _decorate(page: DiscoveryPage, *, cached: bool, stale: bool) -> DiscoveryPage:
        identities = [(item.provider, item.external_id, item.media_type) for item in page.items]
        watched = database.list_media_watchlist_keys(identities)
        items = [
            replace(item, state="watchlisted" if item.stable_id in watched else item.state)
            for item in page.items
        ]
        return replace(page, items=items, cached=cached, stale=stale)

    def shutdown(self) -> None:
        with self._refresh_guard:
            self._shutdown = True
            self._pending_refreshes.clear()
            self._refresh_cooldowns.clear()
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def get_detail(self, provider: str, media_type: str, external_id: str) -> MediaCard | None:
        if media_type not in {"movie", "tv"}:
            raise ValueError("媒体类型无效")
        return self.registry.get(provider).get_detail(str(external_id), media_type)

    @staticmethod
    def add_watchlist(card: MediaCard) -> None:
        database.add_media_watchlist(
            card.provider, card.external_id, card.media_type, card.title, card.year, card.poster_key
        )

    @staticmethod
    def remove_watchlist(provider: str, media_type: str, external_id: str) -> bool:
        return database.delete_media_watchlist(provider, external_id, media_type)

    @staticmethod
    def list_watchlist() -> list[dict[str, Any]]:
        return [dict(row) for row in database.list_media_watchlist()]

    def map_to_tmdb(self, provider: str, external_id: str, media_type: str,
                    title: str, year: str = "", *, confirmed_tmdb_id: str = "",
                    confirmed_title: str = "", confirmed_year: str = "") -> dict[str, Any]:
        if provider == "tmdb":
            return {"tmdb_id": str(external_id), "confirmed": True, "candidates": []}
        if media_type not in {"movie", "tv"}:
            raise ValueError("媒体类型无效")
        existing = database.get_media_external_id(provider, external_id, media_type)
        if existing and bool(existing["confirmed"]):
            return {
                "tmdb_id": existing["tmdb_id"], "confirmed": bool(existing["confirmed"]),
                "candidates": [],
            }
        if confirmed_tmdb_id:
            database.upsert_media_external_id(
                provider, external_id, media_type, confirmed_tmdb_id,
                confirmed_title or title, confirmed_year or year, 1.0, True,
            )
            return {"tmdb_id": str(confirmed_tmdb_id), "confirmed": True, "candidates": []}
        if existing:
            return {
                "tmdb_id": existing["tmdb_id"], "confirmed": False,
                "candidates": [],
            }

        candidates = self._scraper_factory().search_candidates(title, year, media_type)
        serialized = [self._candidate_dict(candidate) for candidate in candidates]
        if serialized and float(serialized[0]["score"] or 0) >= 0.9:
            best = serialized[0]
            database.upsert_media_external_id(
                provider, external_id, media_type, best["tmdb_id"], best["title"],
                best["year"], best["score"], False,
            )
            return {"tmdb_id": best["tmdb_id"], "confirmed": False, "candidates": serialized}
        return {"tmdb_id": "", "confirmed": False, "candidates": serialized}

    async def map_to_tmdb_async(
        self,
        provider: str,
        external_id: str,
        media_type: str,
        title: str,
        year: str = "",
        *,
        confirmed_tmdb_id: str = "",
        confirmed_title: str = "",
        confirmed_year: str = "",
    ) -> dict[str, Any]:
        """在探索专用的有界 executor 中执行同步 TMDB 映射。"""
        callback = partial(
            self.map_to_tmdb,
            provider,
            external_id,
            media_type,
            title,
            year,
            confirmed_tmdb_id=confirmed_tmdb_id,
            confirmed_title=confirmed_title,
            confirmed_year=confirmed_year,
        )
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, callback)

    @staticmethod
    def _candidate_dict(candidate: Any) -> dict[str, Any]:
        return {
            "tmdb_id": str(getattr(candidate, "tmdb_id", "") or ""),
            "title": str(getattr(candidate, "title", "") or ""),
            "year": str(getattr(candidate, "year", "") or ""),
            "score": float(getattr(candidate, "score", 0) or 0),
            "media_type": str(getattr(candidate, "media_type", "") or ""),
        }


_service: DiscoveryService | None = None
_service_lock = threading.Lock()


def get_discovery_service() -> DiscoveryService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = DiscoveryService()
    return _service


def shutdown_discovery_service() -> None:
    global _service
    with _service_lock:
        service, _service = _service, None
    if service is not None:
        service.shutdown()
