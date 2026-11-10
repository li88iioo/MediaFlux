"""探索业务编排：缓存、Provider、收藏与跨来源映射。"""
from __future__ import annotations

import asyncio
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Generator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from functools import partial
from itertools import groupby
from typing import Any

from app import config
from app.modules.policy_config import policy_int
from app.repositories import discovery as discovery_repo
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
from app.logger import get_logger
from app.repositories.discovery import count_media_watchlist, media_watchlist_snapshot

_DISCOVERY_CLOSED_MESSAGE = "探索服务已关闭，请重试"
_DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_MAX_REFRESH_COOLDOWNS = 512
_YEAR_RE = re.compile(r"\d{4}")
logger = get_logger(__name__)

_ERROR_TYPES = {
    "authentication": ProviderAuthenticationError,
    "invalid_response": ProviderInvalidResponse,
    "not_configured": ProviderNotConfigured,
    "rate_limited": ProviderRateLimited,
    "timeout": ProviderTimeout,
    "unavailable": ProviderUnavailable,
}


def require_discovery_enabled() -> None:
    """公开探索入口的总开关；本地收藏读取不受此开关限制。"""
    if not config.get_bool("DISCOVERY_ENABLED"):
        raise ValueError("影视探索功能当前已关闭")


def watchlist_identity(provider: str, media_type: str, external_id: str) -> tuple[str, str, str]:
    """收藏写入共用的来源身份校验；不截断目标 ID。"""
    if not all(isinstance(value, str) for value in (provider, media_type, external_id)):
        raise ValueError("收藏来源、媒体类型和来源 ID 必须是字符串")
    provider, media_type, external_id = (
        unicodedata.normalize("NFKC", value).strip() for value in (provider, media_type, external_id)
    )
    provider, media_type = provider.lower(), media_type.lower()
    if provider not in {"tmdb", "douban", "bangumi"}:
        raise ValueError("不支持的数据源")
    if media_type not in {"movie", "tv"} or (provider == "bangumi" and media_type != "tv"):
        raise ValueError("数据源与媒体类型组合无效")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", external_id):
        raise ValueError("来源 ID 无效")
    if provider in {"tmdb", "bangumi"} and not re.fullmatch(r"\d{1,20}", external_id):
        raise ValueError("来源 ID 无效")
    return provider, media_type, external_id


def _watchlist_text(value: str, limit: int) -> str:
    normalized = unicodedata.normalize("NFKC", (value or "")).strip()
    cleaned = "".join(" " if unicodedata.category(char).startswith("C") else char for char in normalized)
    return " ".join(cleaned.split())[:limit]


def confirmed_tmdb_mappings(items: tuple[MediaCard, ...]) -> dict[tuple[str, str, str], str]:
    """按来源身份批量读取已确认映射；未确认候选不成为作品事实。"""
    mappings = discovery_repo.list_media_external_ids([
        (item.provider, item.external_id, item.media_type)
        for item in items if item.provider != "tmdb"
    ])
    return {key: str(row["tmdb_id"]) for key, row in mappings.items()
            if row["confirmed"] and row["tmdb_id"]}


def project_watchlist_state(items: tuple[MediaCard, ...]) -> tuple[MediaCard, ...]:
    """列表与搜索共用读取时的批量收藏投影，不污染来源卡片或缓存。"""
    watched = discovery_repo.list_media_watchlist_keys([
        (item.provider, item.external_id, item.media_type) for item in items
    ])
    mappings = confirmed_tmdb_mappings(items)
    return tuple(replace(
        item, state="watchlisted" if item.stable_id in watched else item.state,
        tmdb_id=mappings.get((item.provider, item.external_id, item.media_type), item.tmdb_id),
    ) for item in items)


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
        self.cache_ttl_seconds = max(60, (cache_ttl_seconds or policy_int("DISCOVERY_CACHE_TTL_SECONDS")))
        self.stale_ttl_seconds = max(
            self.cache_ttl_seconds,
            (stale_ttl_seconds or policy_int("DISCOVERY_STALE_TTL_SECONDS")),
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
    def _network_operation(self) -> Generator[None, None, None]:
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
    def _scraper_operation(self) -> Generator[Any, None, None]:
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
        if type(page) is not int or page < 1 or page > 100:
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
            return policy_int("DOUBAN_CACHE_TTL_SECONDS")
        if provider == "bangumi":
            return max(300, config.get_int("BANGUMI_CACHE_TTL_SECONDS", 21600))
        return self.cache_ttl_seconds

    def _decorate(self, page: DiscoveryPage, *, cached: bool, stale: bool) -> DiscoveryPage:
        missing = [
            (item.provider, item.media_type, item.external_id)
            for item in page.items
            if not (item.year or "").strip() or not (item.release_date or "").strip()
        ]
        metadata = self.cache.get_detail_metadata(missing)
        items = []
        for item in project_watchlist_state(page.items):
            detail = metadata.get((item.provider, item.media_type, item.external_id), {})
            year, release_date = item.year, item.release_date
            if not (year or "").strip() and self._valid_year(detail.get("year", "")):
                year = detail["year"]
            if not (release_date or "").strip() and self._valid_release_date(detail.get("release_date", "")):
                release_date = detail["release_date"]
            stale = stale or (detail.get("stale") is True and (year, release_date) != (item.year, item.release_date))
            items.append(replace(
                item, year=year, release_date=release_date,
            ))
        return replace(page, items=tuple(items), cached=cached, stale=stale)

    @staticmethod
    def _valid_year(value: str) -> bool:
        text = (value or "").strip()
        return bool(
            _YEAR_RE.fullmatch(text) and 1800 <= int(text) <= 2200
        )

    @staticmethod
    def _valid_release_date(value: str) -> bool:
        text = (value or "").strip()
        try:
            return date.fromisoformat(text).isoformat() == text
        except ValueError:
            return False

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
            detail = self.registry.get(provider).get_detail(external_id, media_type)
            provider_key = (provider or "").strip().lower()
            external_key = (external_id or "").strip()
            if (
                isinstance(detail, MediaCard)
                and (detail.provider, detail.media_type, detail.external_id)
                == (provider_key, media_type, external_key)
            ):
                year = detail.year.strip()
                if not self._valid_year(year):
                    year = ""
                release_date = detail.release_date.strip()
                if not self._valid_release_date(release_date):
                    release_date = ""
                if not year and release_date:
                    year = release_date[:4]
                if year or release_date:
                    try:
                        self._write_cache_if_open(lambda: self.cache.set_detail_metadata(
                            provider_key,
                            media_type,
                            external_key,
                            year=year,
                            release_date=release_date,
                            ttl_seconds=self._provider_ttl(provider_key),
                            stale_seconds=self.stale_ttl_seconds,
                        ))
                    except Exception as exc:
                        # 详情请求成功不应因可选缓存写入失败而失败。
                        logger.warning("写入探索详情日期缓存失败: %s", exc)
            return detail

    def describe_media(self, provider: str, media_type: str, external_id: str) -> dict[str, Any] | None:
        """来源详情与本地映射确认事实；读取不保存或猜选映射。"""
        provider, media_type, external_id = watchlist_identity(provider, media_type, external_id)
        card = self.get_detail(provider, media_type, external_id)
        if card is None:
            return None
        mapping = confirmed_tmdb_mappings((card,)).get((provider, external_id, media_type))
        return {**card.to_dict(), "tmdb_id": mapping or card.tmdb_id,
                "mapping_confirmed": provider == "tmdb" or bool(mapping),
                "credits_state": "not_queried"}

    def prepare_watchlist_add(self, provider: str, media_type: str, external_id: str) -> dict[str, Any] | None:
        """核对来源并冻结待添加内容；已有收藏不查询来源，也不写入收藏。"""
        identity = watchlist_identity(provider, media_type, external_id)
        provider, media_type, external_id = identity
        if discovery_repo.get_media_watchlist(provider, external_id, media_type) is not None:
            return {"operation": "add", "expected_absent": False}
        try:
            card = self.get_detail(provider, media_type, external_id)
        except ValueError as exc:
            raise ProviderInvalidResponse("影视数据源返回的条目信息无效。") from exc
        if not isinstance(card, MediaCard):
            return None
        if (card.provider, card.media_type, card.external_id) != identity:
            raise ValueError("影视数据源返回的条目标识不一致")
        title = _watchlist_text(card.title, 160)
        if not title:
            raise ValueError("影视条目缺少可用标题")
        return {"operation": "add", "expected_absent": True, "canonical": {
            "provider": provider, "media_type": media_type, "external_id": external_id,
            "title": title, "year": _watchlist_text(card.year, 12), "poster_key": (card.poster_key or "")[:500],
        }}

    @staticmethod
    def add_watchlist(card: MediaCard, *, expected_absent: bool = False) -> int | None:
        provider, media_type, external_id = watchlist_identity(card.provider, card.media_type, card.external_id)
        return discovery_repo.add_media_watchlist(
            provider, external_id, media_type, card.title, card.year, card.poster_key,
            expected_absent=expected_absent,
        )

    @staticmethod
    def get_watchlist_snapshot(watchlist_number: int) -> dict[str, Any]:
        if isinstance(watchlist_number, bool) or not isinstance(watchlist_number, int) or not 1 <= watchlist_number <= 2_147_483_647:
            raise ValueError("watchlist_number 必须是正整数")
        return media_watchlist_snapshot(discovery_repo.get_media_watchlist_by_id(watchlist_number))

    @staticmethod
    def get_watchlist(watchlist_id: int) -> dict[str, Any]:
        """按真实收藏编号读取作品身份；不展示确认快照中的私有海报及并发校验字段。"""
        snapshot = DiscoveryService.get_watchlist_snapshot(watchlist_id)
        item = None
        if snapshot["exists"]:
            item = {
                "id": snapshot["watchlist_number"],
                **{key: snapshot[key] for key in ("provider", "external_id", "media_type")},
                "title": _watchlist_text(snapshot["title"], 160) or "未命名条目",
                "year": _watchlist_text(snapshot["year"], 12),
            }
        return {"item": item}

    @staticmethod
    def watchlist_statistics() -> dict[str, Any]:
        """同一数据库快照统计全库各类型和各类型最新三项，不受列表页长影响。"""
        rows = discovery_repo.get_media_watchlist_statistics()
        groups = []
        for media_type, group in groupby(rows, key=lambda row: row["media_type"]):
            latest = list(group)
            groups.append({
                "media_type": media_type,
                "count": int(latest[0]["type_count"]),
                "latest": [{
                    "id": int(row["id"]), "provider": row["provider"],
                    "media_type": row["media_type"],
                    "title": _watchlist_text(row["title"], 160) or "未命名条目",
                    "year": _watchlist_text(row["year"], 12),
                } for row in latest],
            })
        return {"total": int(rows[0]["total"]) if rows else 0, "by_media_type": groups}

    @staticmethod
    def remove_watchlist(
        provider: str, media_type: str, external_id: str, *, expected_snapshot: dict[str, Any] | None = None,
    ) -> bool:
        identity = watchlist_identity(provider, media_type, external_id)
        if expected_snapshot is not None and (
            not isinstance(expected_snapshot, dict) or expected_snapshot.get("exists") is not True
            or tuple(expected_snapshot.get(key) for key in ("provider", "media_type", "external_id")) != identity
        ):
            raise ValueError("收藏快照与目标不一致")
        provider, media_type, external_id = identity
        return discovery_repo.delete_media_watchlist(provider, external_id, media_type, expected_snapshot=expected_snapshot)

    @staticmethod
    def list_watchlist(*, limit: int = 200, before_number: int | None = None) -> dict[str, Any]:
        if type(limit) is not int:
            raise ValueError("limit 必须是整数")
        if before_number is not None and (type(before_number) is not int or not 1 <= before_number <= 2**63 - 1):
            raise ValueError("before_number 必须是有效的正整数收藏编号")
        limit = max(1, min(limit, 200))
        rows = discovery_repo.list_media_watchlist(limit=limit + 1, before_number=before_number)
        has_more = len(rows) > limit
        rows = rows[:limit]
        return {"items": [dict(row) for row in rows], "total": count_media_watchlist(), "has_more": has_more,
                "next": {"before_number": rows[-1]["id"], "limit": limit} if has_more else None}

    def lookup_tmdb_mapping(
        self, provider: str, external_id: str, media_type: str, title: str, year: str = ""
    ) -> dict[str, Any]:
        """只读返回已确认映射或 TMDB 候选；绝不写入映射表。"""
        if provider == "tmdb":
            return {"tmdb_id": external_id, "confirmed": True, "candidates": []}
        if media_type not in {"movie", "tv"}:
            raise ValueError("媒体类型无效")
        existing = discovery_repo.get_media_external_id(provider, external_id, media_type)
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
            if external_id != tmdb_id:
                raise ValueError("TMDB 来源身份不一致")
            return {"tmdb_id": tmdb_id, "confirmed": True, "candidates": []}
        verified = self.verify_tmdb_mapping_candidate(tmdb_id, media_type)
        requested_id = verified["tmdb_id"]
        discovery_repo.upsert_media_external_id(
            provider, external_id, media_type, requested_id,
            verified["title"],
            verified["year"],
            1.0, True,
        )
        return {"tmdb_id": requested_id, "confirmed": True, "candidates": []}

    def _tmdb_mapping_source(self, provider: str, external_id: str, media_type: str) -> dict[str, Any] | None:
        card = self.get_detail(provider, media_type, external_id)
        if card is None:
            return None
        if not isinstance(card, MediaCard) or (card.provider, card.external_id, card.media_type) != (provider, external_id, media_type):
            raise ValueError("影视数据源返回的条目标识不一致")
        # 推荐质量指标不参与映射确认，保持既有身份快照格式。
        return {key: value for key, value in card.to_dict().items() if key not in {"vote_count", "popularity"}}

    def prepare_tmdb_mapping(
        self, provider: str, external_id: str, media_type: str, tmdb_id: str,
    ) -> dict[str, Any] | None:
        """只读冻结显式映射的来源详情、核验目标和当前映射；不保存候选或确认票据。"""
        provider, media_type, external_id = watchlist_identity(provider, media_type, external_id)
        if provider == "tmdb" and external_id != tmdb_id:
            raise ValueError("TMDB 来源身份不一致")
        source = self._tmdb_mapping_source(provider, external_id, media_type)
        if source is None:
            return None
        target = self.verify_tmdb_mapping_candidate(tmdb_id, media_type)
        existing = discovery_repo.get_media_external_id(provider, external_id, media_type)
        mapping = None if existing is None else {
            "tmdb_id": str(existing["tmdb_id"] or ""), "confirmed": bool(existing["confirmed"]),
            "version": int(existing["version"] or 0), "updated_at": str(existing["updated_at"] or ""),
        }
        return {"source": source, "target": target, "mapping": mapping}

    def confirm_tmdb_mapping_if_unchanged(
        self,
        provider: str,
        external_id: str,
        media_type: str,
        tmdb_id: str,
        expected_mapping: dict[str, Any] | None,
        *,
        expected_source: dict[str, Any] | None = None,
        expected_target: dict[str, str] | None = None,
        check_cancelled: Callable[[], None] | None = None,
    ) -> dict[str, Any] | None:
        """复核冻结来源与目标后以 CAS 保存；任一快照已变化时返回 ``None``。"""
        provider, media_type, external_id = watchlist_identity(provider, media_type, external_id)
        if expected_source is not None and self._tmdb_mapping_source(provider, external_id, media_type) != expected_source:
            return None
        if provider == "tmdb":
            if external_id != tmdb_id:
                raise ValueError("TMDB 来源身份不一致")
            return {"tmdb_id": tmdb_id, "confirmed": True, "candidates": []}
        verified = self.verify_tmdb_mapping_candidate(tmdb_id, media_type)
        if expected_target is not None and verified != expected_target:
            return None
        if check_cancelled is not None:
            check_cancelled()
        requested_id = verified["tmdb_id"]
        if not discovery_repo.confirm_media_external_id_if_unchanged(
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
        if not isinstance(tmdb_id, str):
            raise ValueError("TMDB ID 无效")
        requested_id = tmdb_id.strip()
        if not re.fullmatch(r"[0-9]{1,10}", requested_id):
            raise ValueError("TMDB ID 无效")
        with self._network_operation(), self._scraper_operation() as scraper:
            match = scraper.match_from_tmdb(requested_id, media_type)
        if (bool(getattr(match, "need_confirm", True)) or str(getattr(match, "tmdb_id", "")) != requested_id
                or getattr(match, "media_type", "") != media_type):
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
            return {"tmdb_id": external_id, "confirmed": True, "candidates": []}
        if media_type not in {"movie", "tv"}:
            raise ValueError("媒体类型无效")
        existing = discovery_repo.get_media_external_id(provider, external_id, media_type)
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
