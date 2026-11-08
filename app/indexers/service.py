from __future__ import annotations

import asyncio
import hashlib
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any, cast
from urllib.parse import urlsplit

from app import config
from app.clients.http import HttpError, HttpInvalidResponse, HttpSecurityError
from app.logger import get_logger
from app.modules.media_probe import release_dynamic_range_evidence

from .config import (
    AGENT_ONLY_INDEXER_SITE_IDS,
    INDEXER_SITE_LABELS,
    MANUAL_INDEXER_SITE_ORDER,
    RETIRED_INDEXER_SITE_IDS,
    SENSITIVE_INDEXER_SITE_IDS,
)
from .errors import IndexerTimeout, IndexerUnavailable, IndexerValidationError
from .models import (
    AggregatedIndexerResult,
    IndexerItem,
    IndexerMediaSearchRequest,
    IndexerPage,
    IndexerProviderError,
    IndexerSearchRequest,
    IndexerSitePageState,
    ResolvedDownload,
    indexer_site_status,
)
from .providers.base import magnet_infohash, page_observer, report_page
from .query_plan import build_site_queries, needs_bilingual_search
from .ranking import (
    annotate_clusters,
    candidate_sort_key,
    match_order,
    match_priority,
    published_timestamp,
    rank_item,
)
from .registry import IndexerRegistry
from .release import parse_indexer_release_position
from .result_store import IndexerResultStore

logger = get_logger(__name__)


@dataclass(slots=True)
class _CacheEntry:
    result: AggregatedIndexerResult
    expires_at: datetime


@dataclass(slots=True)
class _BreakerState:
    failures: int = 0
    open_until: datetime | None = None
    code: str = ""


@dataclass(slots=True)
class _ProviderOutcome:
    site_id: str
    page: IndexerPage | None = None
    error: IndexerProviderError | None = None
    query: str = ""
    attempts: int = 0
    duration_ms: int = 0


class _CloseAttempt:
    __slots__ = ("complete", "error")

    def __init__(self) -> None:
        self.complete = threading.Event()
        self.error: BaseException | None = None


class IndexerService:
    """并发聚合各 Provider 的公开页；最终结果完整保留本页已取得的候选。

    max_results_per_site 仅限制中间进度快照。续页沿用 Provider 的 page，
    不把展示限量变成业务截断；上游未覆盖量仍由站点计数和错误如实返回。
    """

    def __init__(
        self,
        *,
        registry: IndexerRegistry,
        result_store: IndexerResultStore,
        site_timeout_seconds: float = 10,
        total_timeout_seconds: float = 15,
        max_results_per_site: int = 40,
        max_concurrency: int = 5,
        cache_ttl_seconds: int = 120,
        max_cache_entries: int = 256,
        breaker_failure_threshold: int = 3,
        breaker_cooldown_seconds: int = 300,
        enabled_site_ids: Iterable[str] | None = None,
        sukebei_authorized: bool | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        if site_timeout_seconds <= 0 or total_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        if (
            max_results_per_site <= 0
            or max_concurrency <= 0
            or cache_ttl_seconds <= 0
            or max_cache_entries <= 0
        ):
            raise ValueError("limits must be positive")
        if breaker_failure_threshold <= 0 or breaker_cooldown_seconds <= 0:
            raise ValueError("breaker limits must be positive")
        self.registry = registry
        self.result_store = result_store
        self.site_timeout_seconds = float(site_timeout_seconds)
        self.total_timeout_seconds = float(total_timeout_seconds)
        self.max_results_per_site = max_results_per_site
        self.max_concurrency = max_concurrency
        self.cache_ttl_seconds = cache_ttl_seconds
        self.max_cache_entries = max_cache_entries
        self.breaker_failure_threshold = breaker_failure_threshold
        self.breaker_cooldown_seconds = breaker_cooldown_seconds
        configured = (
            registry.enabled_ids()
            if enabled_site_ids is None
            else tuple(enabled_site_ids)
        )
        registered = set(registry.ids())
        configured_ids = {site_id.strip().lower() for site_id in configured}
        self.sukebei_authorized = (
            "sukebei" in configured_ids
            if sukebei_authorized is None
            else sukebei_authorized
        )
        manual = {
            site_id
            for site_id in configured_ids & registered
            if site_id not in AGENT_ONLY_INDEXER_SITE_IDS
            and site_id not in SENSITIVE_INDEXER_SITE_IDS
            and site_id not in RETIRED_INDEXER_SITE_IDS
        }
        if self.sukebei_authorized and "sukebei" in registered:
            manual.add("sukebei")
        self.enabled_site_ids = frozenset(manual)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: OrderedDict[tuple[object, ...], _CacheEntry] = OrderedDict()
        self._cache_lock = threading.RLock()
        self._breaker_lock = threading.RLock()
        self._breaker_states: dict[str, _BreakerState] = {}
        self._runtime_lock = threading.RLock()
        self._runtime_condition = threading.Condition(self._runtime_lock)
        self._inflight: dict[
            tuple[asyncio.AbstractEventLoop, tuple[object, ...]],
            asyncio.Task[object],
        ] = {}
        self._progress_callbacks: dict[tuple, set[Callable[[AggregatedIndexerResult], None]]] = {}
        self._progress_last: dict[tuple, AggregatedIndexerResult] = {}
        self._search_waiters: dict[tuple, int] = {}
        self._loop_semaphores: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}
        self._closing = False
        self._closed = False
        self._close_attempt: _CloseAttempt | None = None

    async def search(
        self,
        query: str,
        page: int = 1,
        site_ids: Iterable[str] | None = None,
        *,
        scope: str = "manual",
        sort_mode: str = "relevance_desc",
        on_progress: Callable[[AggregatedIndexerResult], None] | None = None,
    ) -> AggregatedIndexerResult:
        request = IndexerSearchRequest.create(
            query,
            page,
            sort_mode=sort_mode,
        )
        scope = self._normalize_scope(scope)
        selected = self.select_sites(site_ids, scope=scope)
        plans: dict[str, tuple[str, ...]] = {site_id: (request.query,) for site_id in selected}
        cache_key = (
            "query",
            request.query,
            request.page,
            request.sort_mode,
            selected,
        )
        return await self._search_plans(
            display_query=request.query,
            page=request.page,
            selected=selected,
            plans=plans,
            cache_key=cache_key,
            ranking_context=None,
            sort_mode=request.sort_mode,
            on_progress=on_progress,
        )

    async def search_media(
        self,
        request: IndexerMediaSearchRequest,
        site_ids: Iterable[str] | None = None,
        *,
        scope: str = "manual",
        timeout_seconds: float | None = None,
        on_progress: Callable[[AggregatedIndexerResult], None] | None = None,
    ) -> AggregatedIndexerResult:
        if not isinstance(request, IndexerMediaSearchRequest):
            raise IndexerValidationError("invalid media search request")
        scope = self._normalize_scope(scope)
        selected = self.select_sites(site_ids, scope=scope)
        plans = {site_id: build_site_queries(site_id, request) for site_id in selected}
        plan_identity = tuple((site_id, plans[site_id]) for site_id in selected)
        cache_key = (
            "media",
            request.cache_identity(),
            request.page,
            selected,
            plan_identity,
        )
        return await self._search_plans(
            display_query=request.title,
            page=request.page,
            selected=selected,
            plans=plans,
            cache_key=cache_key,
            ranking_context=request,
            sort_mode=request.sort_mode,
            on_progress=on_progress,
            timeout_seconds=timeout_seconds,
        )

    async def _search_plans(
        self,
        *,
        display_query: str,
        page: int,
        selected: tuple[str, ...],
        plans: dict[str, tuple[str, ...]],
        cache_key: tuple[object, ...],
        ranking_context: IndexerMediaSearchRequest | None,
        sort_mode: str,
        on_progress: Callable[[AggregatedIndexerResult], None] | None = None,
        timeout_seconds: float | None = None,
    ) -> AggregatedIndexerResult:
        self._ensure_open()
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise IndexerValidationError("invalid search timeout")
        track_progress = on_progress is not None or timeout_seconds is not None
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached
        loop = asyncio.get_running_loop()
        inflight_key = (loop, cache_key)

        def cleanup(done_task: asyncio.Task[object]) -> None:
            with self._runtime_condition:
                if self._inflight.get(inflight_key) is done_task:
                    self._inflight.pop(inflight_key, None)
                    self._progress_last.pop(inflight_key, None)
                active_loops = {active_key[0] for active_key in self._inflight}
                if loop not in active_loops:
                    self._loop_semaphores.pop(loop, None)
                self._runtime_condition.notify_all()

        # 最后一个等待者取消后，旧任务仍需完成清理；新查询不能复用它，
        # 也不能提前移出在途台账，否则服务关闭会漏等仍在使用HTTP连接的任务。
        while True:
            with self._runtime_condition:
                closing = self._inflight.get(inflight_key)
            if closing is None or not closing.cancelling():
                break
            started = loop.time()
            await asyncio.wait_for(asyncio.shield(asyncio.gather(closing, return_exceptions=True)),
                                   timeout=timeout_seconds)
            cleanup(closing)
            if timeout_seconds is not None:
                timeout_seconds -= loop.time() - started
                if timeout_seconds <= 0:
                    raise TimeoutError("caller search timeout")
        pinned: dict[tuple, IndexerItem] = {}
        def stable_view(result: AggregatedIndexerResult) -> AggregatedIndexerResult:
            view = result.clone()
            view.items = []
            for item in result.items:
                identity = self._result_identity(item)
                current = self._with_frozen_target(item, pinned.get(identity, item))
                pinned[identity] = current
                view.items.append(current)
            return view
        def notify(result: AggregatedIndexerResult) -> None:
            if on_progress is not None:
                on_progress(stable_view(result))
        with self._runtime_condition:
            self._ensure_open_locked()
            self._search_waiters[inflight_key] = self._search_waiters.get(inflight_key, 0) + 1
            if track_progress:
                self._progress_callbacks.setdefault(inflight_key, set()).add(notify)
                latest = self._progress_last.get(inflight_key)
                if latest is not None:
                    notify(latest.clone())
            task = self._inflight.get(inflight_key)
            created = task is None
            if task is None:
                task = asyncio.create_task(
                    self._search_plans_uncached(
                        display_query=display_query,
                        page=page,
                        selected=selected,
                        plans=plans,
                        cache_key=cache_key,
                        ranking_context=ranking_context,
                        sort_mode=sort_mode,
                    )
                )
                self._inflight[inflight_key] = task

                task.add_done_callback(cleanup)
        try:
            try:
                result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
                assert isinstance(result, AggregatedIndexerResult)
            except asyncio.TimeoutError:
                with self._runtime_condition:
                    latest = self._progress_last.get(inflight_key)
                    result = latest.clone(cached=False) if latest is not None else None
                if result is None:
                    raise
                # 只结束当前等待者：保留已到达的结果、不缓存未完成快照，
                # 不缩短同时等待同一检索的其它请求的预算。
                result.partial = True
                result.complete = False
                known = set(result.sites_succeeded) | {error.site_id for error in result.errors}
                result.errors.extend(self._public_error(site_id, IndexerTimeout("caller search timeout"))
                                     for site_id in selected if site_id not in known)
                return stable_view(result) if on_progress is not None else result
            if on_progress is not None:
                return stable_view(result)
            if created:
                return result
            cached = self._get_cached(cache_key)
            if cached is not None:
                return cached
            clone = result.clone(cached=False)
            clone.items = [
                replace(item, result_id=self.result_store.put(replace(item, result_id=None)))
                for item in clone.items
            ]
            return clone
        finally:
            with self._runtime_condition:
                callbacks = self._progress_callbacks.get(inflight_key)
                if callbacks is not None and track_progress:
                    callbacks.discard(notify)
                    if not callbacks:
                        self._progress_callbacks.pop(inflight_key, None)
                remaining = self._search_waiters.get(inflight_key, 1) - 1
                if remaining:
                    self._search_waiters[inflight_key] = remaining
                else:
                    self._search_waiters.pop(inflight_key, None)
                    if not task.done():
                        task.cancel()
            if not remaining and not task.done():
                await asyncio.gather(task, return_exceptions=True)

    def _publish_progress(self, key: tuple, result: AggregatedIndexerResult) -> None:
        with self._runtime_condition:
            self._progress_last[key] = result.clone()
            callbacks = tuple(self._progress_callbacks.get(key, ()))
        for callback in callbacks:
            try:
                callback(result.clone())
            except Exception:
                logger.warning("indexer.progress consumer callback failed", exc_info=True)

    async def _search_plans_uncached(
        self,
        *,
        display_query: str,
        page: int,
        selected: tuple[str, ...],
        plans: dict[str, tuple[str, ...]],
        cache_key: tuple[object, ...],
        ranking_context: IndexerMediaSearchRequest | None,
        sort_mode: str,
    ) -> AggregatedIndexerResult:
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached
        loop = asyncio.get_running_loop()
        key = (loop, cache_key)
        progress = asyncio.Event()
        outcomes: dict[str, _ProviderOutcome] = {}
        published_items: dict[tuple, IndexerItem] = {}
        cache_expires_at = None
        if page > 1:
            # 两类既有缓存键的第三项都是页码，其余部分绑定查询、站点及计划。
            previous_key = (*cache_key[:2], page - 1, *cache_key[3:])
            with self._cache_lock:
                previous = self._cache.get(previous_key)
                if (previous is not None and previous.expires_at > self._clock()
                        and previous.result.complete and not previous.result.partial):
                    for site_id, state in previous.result.site_page_states.items():
                        if (state.has_more is False and site_id in previous.result.sites_succeeded
                                and not previous.result.site_truncated_counts.get(site_id)):
                            outcomes[site_id] = _ProviderOutcome(
                                site_id, page=IndexerPage([], page, False, state.pagination_supported),
                                query=previous.result.site_queries.get(site_id, ""),
                            )
                    if outcomes:
                        cache_expires_at = previous.expires_at

        def observe(site_id: str, result_page: IndexerPage) -> None:
            if site_id not in selected:
                return
            previous = outcomes.get(site_id)
            if previous is not None and previous.page is not None:
                result_page = replace(result_page, items=self._merge_plan_items(previous.page.items, result_page.items))
            outcomes[site_id] = _ProviderOutcome(site_id, page=result_page, query=plans[site_id][0])
            progress.set()

        def finish(outcome: _ProviderOutcome) -> None:
            previous = outcomes.get(outcome.site_id)
            if outcome.page is None and previous is not None and previous.page is not None:
                outcome.page = replace(
                    previous.page, complete=True,
                    source_statuses=tuple(
                        replace(row, status="error", code=outcome.error.code if outcome.error else "unavailable")
                        if row.status in {"pending", "searching"} else row
                        for row in previous.page.source_statuses
                    ),
                )
            outcomes[outcome.site_id] = outcome

        token = page_observer.set(observe)
        tasks = {site_id: asyncio.create_task(self._search_site_plan(
            site_id, plans[site_id], page, ranking_context=ranking_context, sort_mode=sort_mode,
        )) for site_id in selected if site_id not in outcomes}
        page_observer.reset(token)
        pending = set(tasks.values())
        deadline = loop.time() + self.total_timeout_seconds + self._maximum_timeout_overhead(selected)
        notifier = asyncio.create_task(progress.wait())
        try:
            while pending:
                done, _ = await asyncio.wait(pending | {notifier}, timeout=max(0, deadline - loop.time()), return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    break
                for task in tuple(pending):
                    if task not in done:
                        continue
                    outcome = task.result()
                    finish(outcome)
                    pending.remove(task)
                if notifier in done:
                    progress.clear()
                    notifier = asyncio.create_task(progress.wait())
                if pending and self._progress_callbacks.get(key):
                    snapshot = self._aggregate_outcomes(
                        display_query=display_query, page=page,
                        selected=selected,
                        outcome_by_site=outcomes, ranking_context=ranking_context,
                        sort_mode=sort_mode, complete=False, published_items=published_items,
                    )
                    self._publish_progress(key, snapshot)
            for site_id, task in tasks.items():
                if task in pending:
                    finish(_ProviderOutcome(
                        site_id, error=self._public_error(site_id, IndexerTimeout("total search timeout")),
                        query=plans[site_id][0],
                    ))
        finally:
            unfinished = [task for task in (*tasks.values(), notifier) if not task.done()]
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)
        result = self._aggregate_outcomes(
            display_query=display_query, page=page,
            selected=selected,
            outcome_by_site=outcomes, ranking_context=ranking_context,
            sort_mode=sort_mode, complete=True, published_items=published_items,
        )
        cache_ttl = self._cache_ttl_for(result)
        if cache_ttl is not None and cache_expires_at is not None:
            # 推导出的后续空页不能续长原始“已耗尽”证据的有效期。
            cache_ttl = min(cache_ttl, (cache_expires_at - self._clock()).total_seconds())
        if cache_ttl is not None and cache_ttl > 0:
            self._put_cached(cache_key, result, cache_ttl)
        return result.clone(cached=False)

    @staticmethod
    def _with_frozen_target(candidate: IndexerItem, anchor: IndexerItem) -> IndexerItem:
        # 同一资源的新展示证据可以补全；已发出的执行引用、来源和下载目标不变。
        values = {name: getattr(anchor, name) for name in (
            "result_id", "site_id", "site_name", "detail_url", "magnet", "torrent_url",
            "download_state", "download_kinds",
        )}
        for name in ("size_text", "size_bytes", "seeders", "leechers", "downloads", "published_at"):
            if getattr(candidate, name) is None:
                values[name] = getattr(anchor, name)
        return replace(candidate, **values)

    @staticmethod
    def _result_identity(item: IndexerItem) -> tuple:
        infohash = magnet_infohash(item.magnet)
        return ("hash", infohash) if infohash else (item.site_id, item.detail_url, item.title)

    def _aggregate_outcomes(
        self, *, display_query: str, page: int,
        selected: tuple[str, ...],
        outcome_by_site: dict[str, _ProviderOutcome],
        ranking_context: IndexerMediaSearchRequest | None, sort_mode: str,
        complete: bool, published_items: dict[tuple, IndexerItem],
    ) -> AggregatedIndexerResult:
        log = logger.info if complete else logger.debug
        candidates: list[tuple[int, int, IndexerItem]] = []
        succeeded: list[str] = []
        errors: list[IndexerProviderError] = []
        site_item_counts: dict[str, int] = {}
        site_collected_counts: dict[str, int] = {}
        site_truncated_counts: dict[str, int] = {}
        site_visible_counts: dict[str, int] = {site_id: 0 for site_id in selected}
        site_queries: dict[str, str] = {}
        site_attempt_counts: dict[str, int] = {}
        site_page_states: dict[str, IndexerSitePageState] = {}
        has_more = False
        for site_index, site_id in enumerate(selected):
            outcome = outcome_by_site.get(site_id)
            adapter = self.registry.get(site_id)
            adapter_pagination_supported = bool(
                getattr(
                    getattr(adapter, "capabilities", None),
                    "pagination_supported",
                    False,
                )
            )
            if outcome is None:
                if not complete:
                    continue
                errors.append(
                    self._public_error(
                        site_id, IndexerTimeout("missing provider outcome")
                    )
                )
                site_page_states[site_id] = IndexerSitePageState(
                    pagination_supported=adapter_pagination_supported,
                    requested_page=page,
                    has_more=None,
                    next_page=None,
                )
                continue
            if outcome.query:
                site_queries[site_id] = outcome.query
            site_attempt_counts[site_id] = max(0, outcome.attempts)
            if outcome.error is not None:
                errors.append(outcome.error)
            if outcome.page is None:
                site_page_states[site_id] = IndexerSitePageState(
                    pagination_supported=adapter_pagination_supported,
                    requested_page=page,
                    has_more=None,
                    next_page=None,
                )
                logger.warning(
                    "indexer.site_search site_id=%s outcome=%s duration_ms=%d attempts=%d item_count=0",
                    site_id,
                    outcome.error.code
                    if outcome.error is not None
                    else "invalid_response",
                    outcome.duration_ms,
                    outcome.attempts,
                )
                continue
            page_result = outcome.page
            errors.extend(page_result.errors)
            coverage_limited = bool(page_result.errors) and all(error.code == "coverage_limited" for error in page_result.errors)
            if page_result.items or (outcome.error is None and (coverage_limited or not page_result.errors and page_result.complete)) or any(
                row.status in {"success", "empty", "partial"} for row in page_result.source_statuses
            ):
                succeeded.append(site_id)
            page_has_more = bool(page < 100 and page_result and page_result.has_more)
            has_more = has_more or page_has_more
            site_page_states[site_id] = IndexerSitePageState(
                pagination_supported=(page_result.pagination_supported
                    if page_result
                    else adapter_pagination_supported),
                requested_page=page,
                has_more=page_has_more,
                next_page=page + 1 if page_has_more else None,
            )
            provider_items = [rank_item(item, media=ranking_context, fallback_query=display_query, now=self._clock()) for item in page_result.items]
            if not complete:
                provider_items = [entry[1] for entry in sorted(
                    enumerate(provider_items),
                    key=lambda entry: candidate_sort_key((site_index, entry[0], entry[1]), sort_mode),
                )][: self.max_results_per_site]
            collected = max(len(page_result.items), page_result.total_items or 0)
            site_collected_counts[site_id] = collected
            site_truncated_counts[site_id] = max(0, collected - len(provider_items))
            site_item_counts[site_id] = 0
            for provider_index, candidate in enumerate(provider_items):
                if candidate.site_id != site_id:
                    errors.append(
                        self._public_error(
                            site_id,
                            HttpInvalidResponse(
                                "provider returned mismatched site_id"
                            ),
                        )
                    )
                    continue
                site_item_counts[site_id] += 1
                candidates.append((site_index, provider_index, candidate))
            log(
                "indexer.site_search site_id=%s outcome=%s duration_ms=%d attempts=%d item_count=%d",
                site_id,
                "partial"
                if outcome.error is not None or page_result.errors
                else ("success" if provider_items else "empty"),
                outcome.duration_ms,
                outcome.attempts,
                site_item_counts[site_id],
            )

        candidates = self._deduplicate_candidates(candidates)
        for _site_index, _provider_index, candidate in candidates:
            site_visible_counts[candidate.site_id] += 1
        candidates.sort(key=lambda entry: candidate_sort_key(entry, sort_mode))
        ranked_items = annotate_clusters([entry[2] for entry in candidates])
        items: list[IndexerItem] = []
        for candidate in ranked_items:
            identity = self._result_identity(candidate)
            frozen = published_items.get(identity) if not complete else None
            if frozen is None:
                frozen = candidate.with_result_id(self.result_store.put(candidate))
            display = self._with_frozen_target(candidate, frozen)
            if not complete:
                published_items[identity] = display
            items.append(display)

        result = AggregatedIndexerResult(
            query=display_query,
            page=page,
            items=items,
            sites_attempted=selected,
            sites_succeeded=tuple(succeeded),
            site_item_counts=site_item_counts,
            site_collected_counts=site_collected_counts,
            site_truncated_counts=site_truncated_counts,
            site_visible_counts=site_visible_counts,
            site_queries=site_queries,
            site_attempt_counts=site_attempt_counts,
            site_page_states=site_page_states,
            has_more=has_more,
            errors=errors,
            partial=bool(errors),
            cached=False,
            complete=complete,
            source_statuses={key: outcome.page.source_statuses for key, outcome in outcome_by_site.items() if outcome.page is not None and outcome.page.source_statuses},
        )
        log(
            "indexer.search sites_attempted=%d sites_succeeded=%d items=%d partial=%s cached=false",
            len(selected),
            len(succeeded),
            len(items),
            bool(errors),
        )
        return result

    def get_result(self, result_id: str, *, scope: str = "manual") -> IndexerItem:
        """读取共享结果，并按当前调用入口允许的来源集合校验。"""
        self._ensure_open()
        allowed = self.site_ids_for_scope(scope)
        stored_result = self.result_store.get(result_id)
        if stored_result.site_id not in allowed:
            raise HttpSecurityError(
                "stored result provider is not allowed for this caller"
            )
        return stored_result

    async def resolve(
        self, result_id: str, *, scope: str = "manual"
    ) -> ResolvedDownload:
        self._ensure_open()
        stored_result = self.get_result(result_id, scope=scope)
        site_id = stored_result.site_id
        if stored_result.download_state not in {"ready", "resolvable"}:
            raise HttpInvalidResponse("stored result is not downloadable")
        adapter = self.registry.get(site_id)
        loop = asyncio.get_running_loop()
        current_task = asyncio.current_task()
        if current_task is None:
            raise IndexerUnavailable("indexer resolve task is unavailable")
        operation_key = (loop, ("resolve", result_id, id(current_task)))
        with self._runtime_condition:
            self._ensure_open_locked()
            self._inflight[operation_key] = cast(asyncio.Task[object], current_task)
        try:
            try:
                resolved = await asyncio.wait_for(
                    adapter.resolve(stored_result),
                    timeout=self.site_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise IndexerTimeout() from exc
            except asyncio.CancelledError:
                raise
            except HttpError as exc:
                raise type(exc)(public_message=exc.public_message) from None
            except Exception as exc:
                raise IndexerUnavailable() from exc
        finally:
            with self._runtime_condition:
                if self._inflight.get(operation_key) is current_task:
                    self._inflight.pop(operation_key, None)
                active_loops = {active_key[0] for active_key in self._inflight}
                if loop not in active_loops:
                    self._loop_semaphores.pop(loop, None)
                self._runtime_condition.notify_all()
        if not isinstance(resolved, ResolvedDownload):
            raise HttpInvalidResponse(
                "provider returned an invalid resolved download"
            )
        if resolved.kind not in stored_result.download_kinds:
            raise HttpInvalidResponse(
                "provider returned an undeclared download kind"
            )
        if resolved.kind == "magnet" and (
            not isinstance(resolved.value, str) or not magnet_infohash(resolved.value)
        ):
            raise HttpInvalidResponse("provider returned an invalid magnet")
        if resolved.kind == "torrent" and not isinstance(resolved.value, (str, bytes)):
            raise HttpInvalidResponse(
                "provider returned an invalid torrent candidate"
            )
        return resolved

    async def aclose(self) -> None:
        with self._runtime_condition:
            if self._closed:
                return
            current_attempt = self._close_attempt
            if current_attempt is not None and not current_attempt.complete.is_set():
                attempt = current_attempt
                close_owner = False
            else:
                # 失败后的服务继续保持关闭栅栏，但后续 aclose 可以拥有一个
                # 新尝试，直到 registry 确实释放成功。
                self._closing = True
                attempt = _CloseAttempt()
                self._close_attempt = attempt
                close_owner = True

        if not close_owner:
            if not attempt.complete.is_set():
                await asyncio.to_thread(attempt.complete.wait)
            if attempt.error is not None:
                raise attempt.error
            return

        try:
            graceful_timeout = min(0.25, max(0.05, self.site_timeout_seconds))
            with self._runtime_condition:
                drained = not self._inflight
            if not drained:
                drained = await asyncio.to_thread(
                    self._wait_for_inflight,
                    graceful_timeout,
                )
            if not drained:
                self._cancel_inflight()
                cancel_timeout = max(
                    1.0,
                    min(5.0, self.total_timeout_seconds),
                )
                drained = await asyncio.to_thread(
                    self._wait_for_inflight,
                    cancel_timeout,
                )
            if not drained:
                raise RuntimeError(
                    "索引器仍有任务未退出，为避免关闭正在使用的上游客户端，本次未关闭 registry"
                )
            await self.registry.aclose()
        except BaseException as exc:
            attempt.error = exc
            # _closing 保持为 True，禁止失败后重新接收业务请求；完成事件只
            # 结束本轮等待者，下一次 aclose 会创建新尝试重试资源释放。
            with self._runtime_condition:
                self._runtime_condition.notify_all()
            raise
        else:
            with self._runtime_condition:
                self._closed = True
                self._closing = False
                self._runtime_condition.notify_all()
        finally:
            attempt.complete.set()

    def _ensure_open(self) -> None:
        with self._runtime_lock:
            self._ensure_open_locked()

    def _ensure_open_locked(self) -> None:
        if self._closing or self._closed:
            raise IndexerUnavailable("indexer service is shutting down")

    def _wait_for_inflight(self, timeout_seconds: float) -> bool:
        deadline = perf_counter() + max(0.0, float(timeout_seconds))
        with self._runtime_condition:
            while any(not task.done() for task in self._inflight.values()):
                remaining = deadline - perf_counter()
                if remaining <= 0:
                    return False
                self._runtime_condition.wait(timeout=remaining)
            return True

    def _cancel_inflight(self) -> None:
        with self._runtime_lock:
            pending = tuple(
                (loop, task)
                for (loop, _cache_key), task in self._inflight.items()
                if not task.done()
            )
        for loop, task in pending:
            if loop.is_closed():
                continue
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                logger.warning("取消 Indexer 任务失败：所属事件循环已关闭")

    def media_site_route(
        self,
        *,
        is_animation: bool,
        original_language: str = "",
        scope: str = "manual",
    ) -> tuple[str, ...]:
        """按媒体语义返回启用站点的收敛子集，供未显式配置站点的调用方使用。"""
        from .config import plan_media_site_route

        available = self.site_ids_for_scope(scope)
        return plan_media_site_route(
            available,
            is_animation=is_animation,
            original_language=original_language,
        )

    @staticmethod
    def _normalize_scope(scope: str) -> str:
        normalized = (scope or "").strip().lower()
        if normalized not in {"manual", "agent"}:
            raise IndexerValidationError("invalid indexer search scope")
        return normalized

    def site_ids_for_scope(self, scope: str = "manual") -> tuple[str, ...]:
        """返回该服务端调用入口当前可用的站点白名单。"""
        normalized_scope = self._normalize_scope(scope)
        if not config.get_bool("INDEXER_SEARCH_ENABLED"):
            raise IndexerValidationError(
                "indexer search is disabled",
                public_message="资源检索功能已关闭",
            )
        registered = tuple(self.registry.ids())
        if normalized_scope == "manual":
            allowed = self.enabled_site_ids
        else:
            allowed = {
                site_id for site_id in registered
                if site_id not in RETIRED_INDEXER_SITE_IDS
                and site_id not in SENSITIVE_INDEXER_SITE_IDS
            }
            if self.sukebei_authorized and "sukebei" in registered:
                allowed.add("sukebei")
        return tuple(site_id for site_id in registered if site_id in allowed)

    def select_sites(
        self, site_ids: Iterable[str] | None, *, scope: str = "manual"
    ) -> tuple[str, ...]:
        """按既有调用范围选择并校验站点；不改变开关、授权或错误合同。"""
        allowed = set(self.site_ids_for_scope(scope))
        requested = (
            tuple(site_id for site_id in self.registry.ids() if site_id in allowed)
            if site_ids is None
            else tuple(site_ids)
        )
        selected: list[str] = []
        for raw_site_id in requested:
            site_id = (raw_site_id or "").strip().lower()
            if not site_id or site_id in selected or site_id in RETIRED_INDEXER_SITE_IDS:
                continue
            if (
                site_id not in self.registry.ids()
                or site_id not in allowed
            ):
                raise IndexerValidationError(f"unknown or disabled site: {site_id}")
            selected.append(site_id)
        if not selected:
            raise IndexerValidationError(
                "at least one enabled indexer site is required",
                public_message="未选择可用资源站点，请在设置中重新选择",
            )
        return tuple(selected)

    @classmethod
    def _candidate_preference(
        cls,
        entry: tuple[int, int, IndexerItem],
    ) -> tuple[object, ...]:
        site_index, provider_index, item = entry
        state_rank = {"ready": 2, "resolvable": 1}.get(item.download_state, 0)
        actionable_rank = int(state_rank > 0)
        published_value = published_timestamp(item.published_at)
        metadata_count = sum(
            value is not None and value != ""
            for value in (
                item.size_bytes,
                item.seeders,
                item.leechers,
                item.downloads,
                item.published_at,
                item.detail_url,
                item.torrent_url,
            )
        )
        return (
            actionable_rank,
            *(-value for value in match_order(item)),
            (item.relevance_score or 0),
            state_rank,
            (item.seeders if item.seeders is not None else -1),
            published_value,
            metadata_count,
            -site_index,
            -provider_index,
        )

    @classmethod
    def _deduplicate_candidates(
        cls,
        candidates: list[tuple[int, int, IndexerItem]],
    ) -> list[tuple[int, int, IndexerItem]]:
        """Keep the most useful representation when multiple sites expose one infohash."""

        output: list[tuple[int, int, IndexerItem]] = []
        infohash_positions: dict[str, int] = {}
        for entry in candidates:
            infohash = magnet_infohash(entry[2].magnet)
            if not infohash:
                output.append(entry)
                continue
            existing_position = infohash_positions.get(infohash)
            if existing_position is None:
                infohash_positions[infohash] = len(output)
                output.append(entry)
                continue
            existing = output[existing_position]
            if cls._candidate_preference(entry) > cls._candidate_preference(existing):
                output[existing_position] = entry
        return output

    async def _search_site_plan(
        self,
        site_id: str,
        queries: tuple[str, ...],
        page: int,
        *,
        ranking_context: IndexerMediaSearchRequest | None,
        sort_mode: str,
    ) -> _ProviderOutcome:
        circuit_error = self._circuit_error(site_id)
        if circuit_error is not None:
            return _ProviderOutcome(
                site_id=site_id,
                error=circuit_error,
                query=queries[0] if queries else "",
                attempts=0,
            )
        adapter = self.registry.get(site_id)
        timeout_overhead: Callable[[], float] | None = getattr(adapter, "search_timeout_overhead_seconds", None)
        overhead_seconds = (
            float(timeout_overhead()) if callable(timeout_overhead) else 0.0
        )
        plan_budget_seconds = self.site_timeout_seconds + max(0.0, overhead_seconds)
        plan_started = perf_counter()
        last_outcome: _ProviderOutcome | None = None
        last_error: _ProviderOutcome | None = None
        merged_items: list[IndexerItem] = []
        reported_omissions = 0
        page_errors: dict[tuple[str, str], IndexerProviderError] = {}
        contributed_query = ""
        has_more = False
        has_more_known = False
        pagination_supported = bool(
            getattr(
                getattr(adapter, "capabilities", None), "pagination_supported", False
            )
        )
        attempts_made = 0
        duration_ms = 0
        minimum_queries = 2 if site_id == "nyaa" and needs_bilingual_search(queries) else 1
        for attempts, query in enumerate(queries, start=1):
            remaining_seconds = plan_budget_seconds - (perf_counter() - plan_started)
            if attempts > 1 and remaining_seconds <= 0:
                last_error = _ProviderOutcome(site_id=site_id, error=self._public_error(
                    site_id, IndexerTimeout("search variant budget exhausted"),
                ))
                break
            attempts_made = attempts
            media_type = (
                ranking_context.media_type if ranking_context is not None else ""
            )
            season = ranking_context.season if ranking_context is not None else None
            episode = ranking_context.episode if ranking_context is not None else None
            outcome = await self._search_site(
                site_id,
                IndexerSearchRequest.create(
                    query,
                    page,
                    media_type=media_type,
                    year=ranking_context.year if ranking_context is not None else None,
                    sort_mode=sort_mode,
                    season=season,
                    episode=episode,
                ),
                timeout_seconds=remaining_seconds if attempts > 1 else None,
            )
            outcome.query = query
            outcome.attempts = attempts
            duration_ms += outcome.duration_ms
            if outcome.error is not None:
                if outcome.error.code == "query_rejected":
                    if last_outcome is None:
                        last_error = outcome
                    continue  # 输入问题不是站点故障，允许尝试下一个完整别名。
                self._record_circuit_failure(site_id, code=outcome.error.code)
                last_error = outcome
                break
            if last_error is not None and last_error.error is not None and last_error.error.code == "query_rejected":
                last_error = None
            pause = next((error for error in (outcome.page.errors if outcome.page else ())
                          if error.code in {"rate_limited", "security_error", "challenge_required"}), None)
            if pause is not None:
                self._record_circuit_failure(site_id, code=pause.code)
            else:
                self._record_circuit_success(site_id)
            last_outcome = outcome
            if outcome.page is None:
                continue
            reported_omissions = max(reported_omissions, (outcome.page.total_items or 0) - len(outcome.page.items))
            page_errors.update(((err.code, err.message), err) for err in outcome.page.errors)
            has_more = has_more or outcome.page.has_more
            has_more_known = True
            pagination_supported = pagination_supported or outcome.page.pagination_supported
            if outcome.page.items:
                contributed_query = query
                plan_items = [
                    rank_item(
                        item,
                        media=ranking_context,
                        fallback_query=query,
                        now=self._clock(),
                    )
                    for item in outcome.page.items
                ]
                merged_items = self._merge_plan_items(merged_items, plan_items)
            if pause is not None:
                break
            if merged_items and attempts >= minimum_queries and self._site_plan_quality_satisfied(
                merged_items,
                media=ranking_context,
                fallback_query=query,
            ):
                break
            if merged_items and site_id in {"nyaa", "dygang", "ys5266", "kpkuang"} and attempts < len(queries):
                # 平滑查询间先交付已知结果，下一别名失败/取消不抹掉已返回证据。
                report_page(site_id, IndexerPage(
                    items=list(merged_items), page=page, has_more=has_more,
                    pagination_supported=pagination_supported, complete=False,
                    errors=tuple(page_errors.values()),
                ))
        outcome = last_error or last_outcome
        if outcome is None:
            return _ProviderOutcome(
                site_id=site_id,
                error=self._public_error(
                    site_id, IndexerValidationError("site query plan is empty")
                ),
            )
        # 空页同样具有分页事实；后续别名失败不能抹掉已经取得的页。
        if has_more_known:
            outcome.page = IndexerPage(
                items=merged_items,
                total_items=len(merged_items) + reported_omissions,
                page=page,
                has_more=has_more,
                pagination_supported=pagination_supported,
                errors=tuple(page_errors.values()),
                source_statuses=last_outcome.page.source_statuses if last_outcome is not None and last_outcome.page is not None else (),
            )
            outcome.query = contributed_query or outcome.query
        outcome.attempts = attempts_made
        outcome.duration_ms = duration_ms
        return outcome

    @staticmethod
    def _plan_item_keys(item: IndexerItem) -> tuple[tuple[str, str], ...]:
        keys: list[tuple[str, str]] = []
        infohash = magnet_infohash(item.magnet)
        if infohash:
            keys.append(("hash", infohash))
        if item.detail_url:
            keys.append(("url", item.detail_url))
        if not keys:
            keys.append(("title", " ".join(item.title.casefold().split())))
        return tuple(keys)

    @classmethod
    def _merge_plan_items(
        cls,
        current: list[IndexerItem],
        incoming: list[IndexerItem],
    ) -> list[IndexerItem]:
        output = list(current)
        positions: dict[tuple[str, str], int] = {}
        for position, item in enumerate(output):
            for key in cls._plan_item_keys(item):
                positions.setdefault(key, position)
        for item in incoming:
            keys = cls._plan_item_keys(item)
            # 同一影片详情可包含多个季集/清晰度；已知不同 hash 不能因 URL 相同合并。
            existing_position = next(
                (
                    positions[key] for key in keys if key in positions
                    and not (
                        key[0] == "url"
                        and (existing_hash := magnet_infohash(output[positions[key]].magnet))
                        and (incoming_hash := magnet_infohash(item.magnet))
                        and existing_hash != incoming_hash
                    )
                ),
                None,
            )
            if existing_position is not None:
                existing = output[existing_position]
                if cls._candidate_preference((0, 0, item)) > cls._candidate_preference(
                    (0, 0, existing)
                ):
                    output[existing_position] = item
                for key in keys:
                    positions.setdefault(key, existing_position)
                continue
            position = len(output)
            output.append(item)
            for key in keys:
                positions[key] = position
        return output

    @staticmethod
    def _site_plan_quality_satisfied(
        items: list[IndexerItem],
        *,
        media: IndexerMediaSearchRequest | None,
        fallback_query: str,
    ) -> bool:
        if not items:
            return False
        if media is None:
            return True
        ranked = [
            rank_item(item, media=media, fallback_query=fallback_query)
            for item in items
            if item.download_state in {"ready", "resolvable"}
        ]
        if media.episode is not None:
            return any(
                (item.relevance_score or 0) >= 78
                and "episode_exact" in item.match_reasons
                for item in ranked
            )
        strong = [item for item in ranked if (item.relevance_score or 0) >= 78]
        return len(strong) >= 3 or any(
            (item.relevance_score or 0) >= 80 for item in strong
        )

    def _circuit_error(self, site_id: str) -> IndexerProviderError | None:
        now = self._clock()
        with self._breaker_lock:
            state = self._breaker_states.get(site_id)
            if (
                state is not None
                and state.open_until is not None
                and state.open_until > now
            ):
                if state.code == "rate_limited":
                    return IndexerProviderError(
                        site_id=site_id,
                        code="rate_limited",
                        message="索引站点请求过于频繁，已暂时冷却",
                    )
                if state.code == "challenge_required":
                    return IndexerProviderError(
                        site_id=site_id, code="challenge_required",
                        message="站点要求人机验证，已暂缓自动查询",
                    )
                if state.code == "security_error":
                    return IndexerProviderError(
                        site_id=site_id,
                        code="security_error",
                        message="上游地址未通过安全校验，已暂时冷却",
                    )
                return IndexerProviderError(
                    site_id=site_id,
                    code="unavailable",
                    message="索引站点连续失败，已暂时冷却",
                )
            if state is not None and state.open_until is not None:
                self._breaker_states.pop(site_id, None)
        return None

    def _record_circuit_failure(self, site_id: str, *, code: str = "") -> None:
        now = self._clock()
        # 限流与安全校验类失败继续重试只会更糟（触发更严格的封锁），
        # 不必等失败次数达到阈值，立即进入冷却。
        immediate = code in {"rate_limited", "security_error", "challenge_required"}
        with self._breaker_lock:
            previous = self._breaker_states.get(site_id)
            failures = (previous.failures if previous is not None else 0) + 1
            open_until = (
                now + timedelta(seconds=self.breaker_cooldown_seconds)
                if immediate or failures >= self.breaker_failure_threshold
                else None
            )
            self._breaker_states[site_id] = _BreakerState(
                failures=failures,
                open_until=open_until,
                code=code,
            )

    def _record_circuit_success(self, site_id: str) -> None:
        with self._breaker_lock:
            self._breaker_states.pop(site_id, None)

    async def _search_site(
        self,
        site_id: str,
        request: IndexerSearchRequest,
        *,
        timeout_seconds: float | None = None,
    ) -> _ProviderOutcome:
        adapter = self.registry.get(site_id)
        started = perf_counter()
        try:
            wait_for_slot = getattr(adapter, "wait_for_search_slot", None)
            if callable(wait_for_slot):
                typed_wait_for_slot = cast(
                    Callable[[IndexerSearchRequest], Awaitable[None]],
                    wait_for_slot,
                )
                await typed_wait_for_slot(request)
            timeout_overhead: Callable[[], float] | None = getattr(adapter, "search_timeout_overhead_seconds", None)
            overhead_seconds = (
                float(timeout_overhead()) if callable(timeout_overhead) else 0.0
            )
            default_timeout_seconds = self.site_timeout_seconds + max(
                0.0, overhead_seconds
            )
            provider_timeout_seconds = (
                default_timeout_seconds
                if timeout_seconds is None
                else max(0.001, min(default_timeout_seconds, float(timeout_seconds)))
            )
            async with self._loop_semaphore():
                page = await asyncio.wait_for(
                    adapter.search(request), timeout=provider_timeout_seconds
                )
            if not isinstance(page, IndexerPage):
                raise HttpInvalidResponse("provider returned an invalid page")
            # search 已返回；complete=False 不应在后续合并中变成“已穷尽”。
            # 使用既有部分结果通道传递范围限制，不把调用结束状态锁在 searching。
            if not page.complete and not page.errors:
                page.errors = (IndexerProviderError(site_id, "coverage_limited", "仅检索部分候选，未覆盖范围仍未知"),)
            return _ProviderOutcome(
                site_id=site_id,
                page=page,
                duration_ms=round((perf_counter() - started) * 1000),
            )
        except asyncio.TimeoutError:
            outcome = self._error_outcome(site_id, IndexerTimeout("site timeout"))
        except asyncio.CancelledError:
            raise
        except HttpError as exc:
            outcome = self._error_outcome(site_id, exc)
        except Exception:  # noqa: BLE001 - provider 边界统一降级为安全错误
            outcome = self._error_outcome(
                site_id, IndexerUnavailable("unexpected provider failure")
            )
        outcome.duration_ms = round((perf_counter() - started) * 1000)
        return outcome

    def _maximum_timeout_overhead(self, selected: tuple[str, ...]) -> float:
        overhead_seconds = 0.0
        for site_id in selected:
            adapter = self.registry.get(site_id)
            timeout_overhead: Callable[[], float] | None = getattr(adapter, "search_timeout_overhead_seconds", None)
            if callable(timeout_overhead):
                overhead_seconds = max(
                    overhead_seconds, max(0.0, float(timeout_overhead()))
                )
        return overhead_seconds

    def _loop_semaphore(self) -> asyncio.Semaphore:
        """返回当前事件循环专用的并发门，避免跨线程复用 asyncio 原语。"""
        loop = asyncio.get_running_loop()
        with self._runtime_lock:
            semaphore = self._loop_semaphores.get(loop)
            if semaphore is None:
                semaphore = asyncio.Semaphore(self.max_concurrency)
                self._loop_semaphores[loop] = semaphore
            return semaphore

    def _error_outcome(self, site_id: str, error: HttpError) -> _ProviderOutcome:
        return _ProviderOutcome(
            site_id=site_id, error=self._public_error(site_id, error)
        )

    @staticmethod
    def _public_error(site_id: str, error: HttpError) -> IndexerProviderError:
        return IndexerProviderError(
            site_id=site_id, code=error.code, message=error.public_message
        )

    def _get_cached(self, key: tuple[object, ...]) -> AggregatedIndexerResult | None:
        now = self._clock()
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._cache.pop(key, None)
                return None
            self._cache.move_to_end(key)
            base = entry.result.clone(cached=True)
        base.items = [
            replace(
                item, result_id=self.result_store.put(replace(item, result_id=None))
            )
            for item in base.items
        ]
        return base

    def _cache_ttl_for(self, result: AggregatedIndexerResult) -> float | None:
        if result.items:
            return (
                min(self.cache_ttl_seconds, 15)
                if result.partial
                else self.cache_ttl_seconds
            )
        if result.partial:
            return None
        return min(self.cache_ttl_seconds, 30)

    def _put_cached(
        self,
        key: tuple[object, ...],
        result: AggregatedIndexerResult,
        ttl_seconds: float,
    ) -> None:
        now = self._clock()
        cached_result = result.clone(cached=False)
        cached_result.items = [
            replace(item, result_id=None) for item in cached_result.items
        ]
        with self._cache_lock:
            expired = [
                cache_key
                for cache_key, entry in self._cache.items()
                if entry.expires_at <= now
            ]
            for cache_key in expired:
                self._cache.pop(cache_key, None)
            self._cache[key] = _CacheEntry(
                result=cached_result,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            self._cache.move_to_end(key)
            while len(self._cache) > self.max_cache_entries:
                self._cache.popitem(last=False)


_SITE_ERROR_CATALOG: dict[str, tuple[str, bool]] = {
    "timeout": ("站点响应超时，请稍后重试", True),
    "unavailable": ("站点暂不可用，请稍后重试", True),
    "rate_limited": ("站点请求过于频繁，请稍后再试", True),
    "invalid_response": ("站点返回异常数据，请稍后重试", True),
    "query_rejected": ("关键词长度或字符不受该站点支持，请换用完整片名或别名", False),
    "challenge_required": ("站点要求人机验证，本次未完成检索", False),
    "response_too_large": ("站点响应异常，暂无法处理", False),
    "security_error": ("站点连接未通过安全校验", False),
    "coverage_limited": ("仅检索部分作品候选，未覆盖范围仍未知；可细化片名、年份或季集", False),
}


def _safe_site_error(error) -> tuple[str, str, bool]:
    code = str(getattr(error, "code", "") or "unavailable")
    message, retryable = _SITE_ERROR_CATALOG.get(
        code, ("站点检索失败，请稍后重试", True)
    )
    return code, message, retryable


def _public_provider_error(error) -> dict[str, str]:
    code, message, _retryable = _safe_site_error(error)
    return {"site_id": str(error.site_id), "code": code, "message": message}


def _public_source_url(service, item) -> str | None:
    value = str(getattr(item, "detail_url", "") or "").strip()
    if not value or service is None:
        return None
    try:
        parsed = urlsplit(value)
        adapter = service.registry.get(item.site_id)
        client = adapter.http_client_for_result(item)
        allowed_hosts = {str(host).rstrip(".").lower() for host in getattr(client, "allowed_hosts", ())}
    except (HttpError, KeyError, TypeError, ValueError):
        return None
    try:
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port or 443
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or parsed.username or parsed.password:
        return None
    if not host or host not in allowed_hosts or port != 443:
        return None
    return value


def search_item(item, service=None) -> dict[str, Any]:
    payload = item.to_public_dict()
    payload["technical_evidence"] = release_dynamic_range_evidence(payload["title"])
    payload["source_url"] = _public_source_url(service, item)
    payload["match_priority"] = match_priority(item)
    # 稳定身份只供展示/勾选迁移；下载仍必须使用当前result_id并经过确认。
    payload["resource_key"] = hashlib.sha256(repr(IndexerService._result_identity(item)).encode("utf-8")).hexdigest()
    payload.update(parse_indexer_release_position(item.title))
    return payload


def search_site_statuses(service, result) -> list[dict[str, Any]]:
    raw_counts = getattr(result, "site_item_counts", None)
    if isinstance(raw_counts, dict) and raw_counts:
        counts = {str(site_id): max(0, int(count)) for site_id, count in raw_counts.items()}
    else:
        counts: dict[str, int] = {}
        for item in result.items:
            counts[item.site_id] = counts.get(item.site_id, 0) + 1
    errors = {error.site_id: error for error in result.errors}
    errors.update((error.site_id, error) for error in result.errors if error.code != "coverage_limited")
    site_queries = getattr(result, "site_queries", {}) or {}
    site_attempt_counts = getattr(result, "site_attempt_counts", {}) or {}
    visible_counts = getattr(result, "site_visible_counts", {}) or {}
    page_states = getattr(result, "site_page_states", {}) or {}
    enabled_site_ids = getattr(service, "enabled_site_ids", None)
    if enabled_site_ids is None:
        enabled_site_ids = frozenset(service.registry.enabled_ids())
    registered = set(service.registry.ids())
    manual_sites = set(MANUAL_INDEXER_SITE_ORDER)
    participating = dict.fromkeys((*result.sites_attempted, *result.sites_succeeded))
    extra_sites = tuple(site_id for site_id in participating if site_id in registered and site_id not in manual_sites)
    payload = []
    for site_id in (*MANUAL_INDEXER_SITE_ORDER, *extra_sites):
        if site_id not in registered:
            continue
        adapter = service.registry.get(site_id)
        code = None
        retryable = False
        sources = (getattr(result, "source_statuses", {}) or {}).get(site_id, ())
        diagnostics = [{
            "site_id": row.site_id, "site_name": row.site_name, "status": row.status,
            "count": row.count, "cached": bool(result.cached or row.cached), "code": row.code or None,
            "message": (
                ("本轮未完整核验，暂保留缓存结果" if result.cached or row.cached else "部分结果未能取得，已保留成功结果")
                if row.status == "partial" else
                _SITE_ERROR_CATALOG.get(row.code, ("来源检索未完成", True))[0] if row.code else ""
            ),
        } for row in sources]
        status = ("disabled" if site_id in manual_sites and site_id not in enabled_site_ids
                  else indexer_site_status(result, site_id, counts.get(site_id, 0)))
        message = ""
        if status is None:
            continue
        if status == "searching":
            message = "正在补充检索"
        elif status == "partial":
            if site_id in errors and errors[site_id].code == "coverage_limited":
                code, message, retryable = _safe_site_error(errors[site_id])
            else:
                message, retryable = "本轮未找到匹配资源，部分来源未完成", True
        elif status == "error":
            if site_id in errors:
                code, message, retryable = _safe_site_error(errors[site_id])
            else:
                code, message, retryable = "unavailable", "站点未返回有效状态", True
        if result.site_truncated_counts.get(site_id, 0):
            hint = "达到候选上限，可细化片名、年份或季集后重搜"
            message = f"{message}；{hint}" if message else hint
        page_state = page_states.get(site_id)
        payload.append({
            "site_id": site_id,
            "site_name": INDEXER_SITE_LABELS.get(site_id, adapter.site_name),
            "status": status,
            "count": counts.get(site_id, 0),
            "collected_count": result.site_collected_counts.get(site_id, counts.get(site_id, 0)),
            "truncated_count": result.site_truncated_counts.get(site_id, 0),
            "visible_count": max(0, int(visible_counts.get(site_id, counts.get(site_id, 0)) or 0)),
            "message": message,
            "code": code,
            "retryable": retryable,
            "diagnostics": diagnostics,
            "query": str(site_queries.get(site_id) or ""),
            "attempts": max(0, int(site_attempt_counts.get(site_id, 0) or 0)),
            "pagination_supported": bool(
                page_state.pagination_supported if page_state is not None else adapter.capabilities.pagination_supported
            ),
            "requested_page": int(page_state.requested_page if page_state is not None else result.page),
            "has_more": page_state.has_more if page_state is not None else None,
            "next_page": page_state.next_page if page_state is not None else None,
        })
    return payload


def search_payload(service, result) -> dict[str, Any]:
    return {
        "query": result.query,
        "page": result.page,
        "items": [search_item(item, service) for item in result.items],
        "sites_attempted": list(result.sites_attempted),
        "sites_succeeded": list(result.sites_succeeded),
        "errors": [_public_provider_error(error) for error in result.errors],
        "site_statuses": search_site_statuses(service, result),
        "site_collected_counts": dict(result.site_collected_counts),
        "site_truncated_counts": dict(result.site_truncated_counts),
        "partial": result.partial,
        "truncated": any(result.site_truncated_counts.values()),
        "cached": result.cached,
        "has_more": bool(getattr(result, "has_more", False)),
        "complete": bool(getattr(result, "complete", True)),
    }
