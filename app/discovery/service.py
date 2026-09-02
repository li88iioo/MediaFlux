"""探索业务编排：缓存、Provider、收藏与跨来源映射。"""
from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from typing import Any

from app import config, database
from app.logger import get_logger
from app.discovery.cache import CacheLookup, DiscoveryCache
from app.discovery.models import (
    DiscoveryPage,
    MediaCard,
    ProviderAuthenticationError,
    ProviderError,
    ProviderInvalidResponse,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderTimeout,
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

_DISCOVERY_CLOSED_MESSAGE = "探索服务已关闭，请重试"
_DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_MAX_REFRESH_COOLDOWNS = 512
logger = get_logger(__name__)

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
        self._refresh_guard = threading.Condition(threading.RLock())
        self._pending_refreshes: set[str] = set()
        self._refresh_cooldowns: dict[str, float] = {}
        self._inflight_futures: set[Future[Any]] = set()
        self._active_network_operations = 0
        self._active_submissions = 0
        self._refresh_clock = refresh_clock or time.monotonic
        self._refresh_cooldown_seconds = 30.0
        self._closed = False
        self._registry_close_lock = threading.Lock()
        self._registry_closed = False
        # 映射与默认后台刷新始终共用专用有界 executor；即使测试或嵌入方
        # 注入了 refresh_submit，异步映射也不能退回 asyncio 默认线程池。
        self._executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
            max_workers=3, thread_name_prefix="discovery-refresh"
        )
        self._refresh_submit = refresh_submit
        if scraper_factory is None:
            from app.modules.scraper import TMDBScraper
            scraper_factory = TMDBScraper
        self._scraper_factory = scraper_factory

    @staticmethod
    def _closed_error() -> ProviderUnavailable:
        return ProviderUnavailable(_DISCOVERY_CLOSED_MESSAGE)

    def _lifecycle_drained_locked(self) -> bool:
        return (
            not self._inflight_futures
            and self._active_network_operations == 0
            and self._active_submissions == 0
        )

    @property
    def closed(self) -> bool:
        with self._refresh_guard:
            return self._closed

    def _close_registry(self) -> bool:
        with self._registry_close_lock:
            if self._registry_closed:
                return True
            try:
                closed = self.registry.close()
            except Exception as exc:
                logger.warning(
                    "关闭 Discovery provider registry 失败 type=%s",
                    type(exc).__name__,
                )
                return False
            if closed is False:
                return False
            self._registry_closed = True
            return True

    def _finish_deferred_shutdown(self) -> None:
        if self._close_registry():
            _release_discovery_service(self)

    @contextmanager
    def _network_operation(self) -> Iterator[None]:
        with self._refresh_guard:
            if self._closed:
                raise self._closed_error()
            self._active_network_operations += 1
        try:
            yield
        finally:
            close_registry = False
            with self._refresh_guard:
                self._active_network_operations -= 1
                self._refresh_guard.notify_all()
                close_registry = self._closed and self._lifecycle_drained_locked()
            if close_registry:
                self._finish_deferred_shutdown()

    @contextmanager
    def _scraper_operation(self) -> Iterator[Any]:
        """为一次映射请求创建并尽力释放 TMDB Scraper。"""
        scraper = self._scraper_factory()
        try:
            yield scraper
        finally:
            close = getattr(scraper, "close", None)
            if callable(close):
                try:
                    closed = close()
                except Exception as exc:
                    logger.warning(
                        "关闭 Discovery 临时 TMDB Scraper 失败 type=%s",
                        type(exc).__name__,
                    )
                else:
                    if closed is False:
                        logger.warning(
                            "关闭 Discovery 临时 TMDB Scraper 未完成"
                        )

    def _future_finished(self, future: Future[Any]) -> None:
        close_registry = False
        with self._refresh_guard:
            self._inflight_futures.discard(future)
            self._refresh_guard.notify_all()
            close_registry = self._closed and self._lifecycle_drained_locked()
        if close_registry:
            self._finish_deferred_shutdown()

    def _track_future_locked(self, future: Future[Any]) -> None:
        self._inflight_futures.add(future)
        future.add_done_callback(self._future_finished)

    def _submit_refresh(self, callback: Callable[[], None]) -> bool:
        with self._refresh_guard:
            if self._closed:
                return False
            if self._refresh_submit is None:
                executor = self._executor
                if executor is None:
                    return False
                try:
                    submitted = executor.submit(callback)
                except RuntimeError:
                    return False
                self._track_future_locked(submitted)
                return True
            submit = self._refresh_submit
            self._active_submissions += 1

        submitted: Any = None
        succeeded = False
        try:
            submitted = submit(callback)
            succeeded = True
        except RuntimeError:
            succeeded = False
        finally:
            close_registry = False
            with self._refresh_guard:
                if succeeded and isinstance(submitted, Future):
                    self._track_future_locked(submitted)
                self._active_submissions -= 1
                self._refresh_guard.notify_all()
                close_registry = self._closed and self._lifecycle_drained_locked()
            if close_registry:
                self._finish_deferred_shutdown()
        return succeeded

    def _submit_mapping(self, callback: Callable[[], dict[str, Any]]) -> Future[dict[str, Any]]:
        with self._refresh_guard:
            if self._closed or self._executor is None:
                raise self._closed_error()
            future = self._executor.submit(callback)
            self._track_future_locked(future)
            return future

    def _write_cache_if_open(self, callback: Callable[[], None]) -> None:
        # 把 closed 检查与 SQLite 写入放在同一个很短的临界区，保证 shutdown
        # 一旦发布 closed 状态，旧代任务不会再落盘成功或错误缓存。
        with self._refresh_guard:
            if self._closed:
                raise self._closed_error()
            callback()

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
        with self._network_operation():
            adapter = self.registry.get(provider)
            try:
                result = adapter.list_items(category, media_type, page, filters)
            except ProviderError as exc:
                error_values = (
                    exc.safe_message, exc.code, exc.status_code, exc.retry_after,
                )
                self._write_cache_if_open(lambda: self.cache.set_error(
                    cache_key, provider, error_values[0], ttl_seconds=30,
                    code=error_values[1], status_code=error_values[2],
                    retry_after=error_values[3],
                ))
                raise
            if not isinstance(result, DiscoveryPage):
                raise TypeError("Provider must return DiscoveryPage")
            payload = replace(result, cached=False, stale=False).to_dict()
            self._write_cache_if_open(lambda: self.cache.set_success(
                cache_key,
                provider,
                payload,
                ttl_seconds=self._provider_ttl(provider),
                stale_seconds=self.stale_ttl_seconds,
            ))
            return replace(result, cached=False, stale=False)

    @staticmethod
    def _cached_error(lookup: CacheLookup) -> ProviderError:
        error_type = _ERROR_TYPES.get(lookup.error_code, ProviderUnavailable)
        return error_type(
            lookup.last_error or "数据源暂不可用", retry_after=lookup.retry_after
        )

    def _remember_refresh_failure_locked(self, cache_key: str) -> None:
        """记录短期退避，并限制高基数失败请求占用的常驻内存。"""
        now = self._refresh_clock()
        self._refresh_cooldowns = {
            key: retry_at
            for key, retry_at in self._refresh_cooldowns.items()
            if retry_at > now and key != cache_key
        }
        while len(self._refresh_cooldowns) >= _MAX_REFRESH_COOLDOWNS:
            oldest = min(
                self._refresh_cooldowns,
                key=self._refresh_cooldowns.__getitem__,
            )
            self._refresh_cooldowns.pop(oldest, None)
        self._refresh_cooldowns[cache_key] = now + self._refresh_cooldown_seconds

    def _schedule_refresh(self, cache_key: str, provider: str, category: str,
                          media_type: str, page: int, filters: dict[str, str]) -> None:
        with self._refresh_guard:
            now = self._refresh_clock()
            retry_at = self._refresh_cooldowns.get(cache_key, 0.0)
            if self._closed or cache_key in self._pending_refreshes or retry_at > now:
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
                    # shutdown 已发布 closed 后，旧代任务只能完成自身清理，
                    # 不得重新写入 cooldown 或其它可观察运行时状态。
                    if not self._closed:
                        if refreshed:
                            self._refresh_cooldowns.pop(cache_key, None)
                        else:
                            self._remember_refresh_failure_locked(cache_key)
                    self._pending_refreshes.discard(cache_key)
                    self._refresh_guard.notify_all()

        if not self._submit_refresh(refresh):
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

    def shutdown(
        self, timeout_seconds: float = _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with self._refresh_guard:
            self._closed = True
            self._pending_refreshes.clear()
            self._refresh_cooldowns.clear()
            executor, self._executor = self._executor, None
            futures = tuple(self._inflight_futures)
            self._refresh_guard.notify_all()

        for future in futures:
            future.cancel()
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

        with self._refresh_guard:
            while not self._lifecycle_drained_locked():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._refresh_guard.wait(timeout=remaining)
            drained = self._lifecycle_drained_locked()

        if not drained:
            return False
        closed = self._close_registry()
        if closed:
            _release_discovery_service(self)
        return closed

    def get_detail(self, provider: str, media_type: str, external_id: str) -> MediaCard | None:
        if media_type not in {"movie", "tv"}:
            raise ValueError("媒体类型无效")
        with self._network_operation():
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

    def lookup_tmdb_mapping(
        self, provider: str, external_id: str, media_type: str, title: str, year: str = ""
    ) -> dict[str, Any]:
        """只读返回已确认映射或 TMDB 候选；绝不写入映射表。"""
        if provider == "tmdb":
            return {"tmdb_id": str(external_id), "confirmed": True, "candidates": []}
        if media_type not in {"movie", "tv"}:
            raise ValueError("媒体类型无效")
        existing = database.get_media_external_id(provider, external_id, media_type)
        if existing and bool(existing["confirmed"]):
            return {
                "tmdb_id": str(existing["tmdb_id"] or ""),
                "confirmed": True,
                "candidates": [],
            }
        with self._network_operation(), self._scraper_operation() as scraper:
            candidates = scraper.search_candidates(title, year, media_type)
        serialized = [self._candidate_dict(candidate) for candidate in candidates]
        return {
            "tmdb_id": "",
            "confirmed": False,
            "candidates": serialized,
        }

    def confirm_tmdb_mapping(
        self, provider: str, external_id: str, media_type: str, tmdb_id: str
    ) -> dict[str, Any]:
        """核验显式 TMDB 身份后持久化 confirmed 映射。"""
        if provider == "tmdb":
            if str(external_id) != str(tmdb_id):
                raise ValueError("TMDB 来源身份不一致")
            return {"tmdb_id": str(tmdb_id), "confirmed": True, "candidates": []}
        verified = self.verify_tmdb_mapping_candidate(tmdb_id, media_type)
        requested_id = verified["tmdb_id"]
        database.upsert_media_external_id(
            provider, external_id, media_type, requested_id,
            verified["title"],
            verified["year"],
            1.0, True,
        )
        return {"tmdb_id": requested_id, "confirmed": True, "candidates": []}

    def confirm_tmdb_mapping_if_unchanged(
        self,
        provider: str,
        external_id: str,
        media_type: str,
        tmdb_id: str,
        expected_mapping: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """核验候选并以 CAS 保存；映射已变化时返回 ``None``。"""
        if provider == "tmdb":
            if str(external_id) != str(tmdb_id):
                raise ValueError("TMDB 来源身份不一致")
            return {"tmdb_id": str(tmdb_id), "confirmed": True, "candidates": []}
        verified = self.verify_tmdb_mapping_candidate(tmdb_id, media_type)
        requested_id = verified["tmdb_id"]
        if not database.confirm_media_external_id_if_unchanged(
            provider,
            external_id,
            media_type,
            requested_id,
            verified["title"],
            verified["year"],
            expected_mapping,
        ):
            return None
        return {"tmdb_id": requested_id, "confirmed": True, "candidates": []}

    def verify_tmdb_mapping_candidate(self, tmdb_id: str, media_type: str) -> dict[str, str]:
        """只读核验一个显式 TMDB 候选，供预检与最终提交共同复用。"""
        if media_type not in {"movie", "tv"}:
            raise ValueError("媒体类型无效")
        requested_id = str(tmdb_id or "").strip()
        if not requested_id.isdigit() or not 1 <= len(requested_id) <= 10:
            raise ValueError("TMDB ID 无效")
        with self._network_operation(), self._scraper_operation() as scraper:
            match = scraper.match_from_tmdb(requested_id, media_type)
        if bool(getattr(match, "need_confirm", True)) or str(getattr(match, "tmdb_id", "")) != requested_id:
            raise ValueError("无法核验所选 TMDB 映射")
        return {
            "tmdb_id": requested_id,
            "title": str(getattr(match, "title", "") or ""),
            "year": str(getattr(match, "year", "") or ""),
            "media_type": media_type,
        }

    def map_to_tmdb(self, provider: str, external_id: str, media_type: str,
                    title: str, year: str = "", *, confirmed_tmdb_id: str = "",
                    confirmed_title: str = "", confirmed_year: str = "") -> dict[str, Any]:
        """兼容入口：复用既有确认；仅新显式 tmdb_id 会核验后写入。"""
        del confirmed_title, confirmed_year
        if provider == "tmdb":
            return {"tmdb_id": str(external_id), "confirmed": True, "candidates": []}
        if media_type not in {"movie", "tv"}:
            raise ValueError("媒体类型无效")
        existing = database.get_media_external_id(provider, external_id, media_type)
        if existing and bool(existing["confirmed"]):
            return {
                "tmdb_id": str(existing["tmdb_id"] or ""),
                "confirmed": True,
                "candidates": [],
            }
        if confirmed_tmdb_id:
            return self.confirm_tmdb_mapping(
                provider, external_id, media_type, confirmed_tmdb_id
            )
        return self.lookup_tmdb_mapping(provider, external_id, media_type, title, year)

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
        future = self._submit_mapping(callback)
        return await asyncio.wrap_future(future, loop=loop)

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


def _release_discovery_service(service: DiscoveryService) -> None:
    global _service
    with _service_lock:
        if _service is service:
            _service = None


def get_discovery_service() -> DiscoveryService:
    global _service
    service = _service
    if service is not None and service.closed:
        service.shutdown()
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = DiscoveryService()
    return _service


def shutdown_discovery_service() -> bool:
    service = _service
    if service is None:
        return True
    return service.shutdown()
