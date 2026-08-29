from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Awaitable, Callable, Iterable, cast

from app.logger import get_logger

from .errors import (
    IndexerError,
    IndexerInvalidResponse,
    IndexerSecurityError,
    IndexerTimeout,
    IndexerUnavailable,
    IndexerValidationError,
)
from .models import (
    AggregatedIndexerResult,
    IndexerItem,
    IndexerMediaSearchRequest,
    IndexerPage,
    IndexerProviderError,
    IndexerSearchRequest,
    IndexerSitePageState,
    ResolvedDownload,
)
from .providers.base import magnet_infohash
from .query_plan import build_site_queries
from .ranking import annotate_clusters, rank_item
from .release import parse_indexer_release_position
from .registry import IndexerRegistry
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


class IndexerService:
    """Concurrent, bounded aggregation over registered resource indexers."""

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
        clock: Callable[[], datetime] | None = None,
    ):
        if site_timeout_seconds <= 0 or total_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        if max_results_per_site <= 0 or max_concurrency <= 0 or cache_ttl_seconds <= 0 or max_cache_entries <= 0:
            raise ValueError("limits must be positive")
        if breaker_failure_threshold <= 0 or breaker_cooldown_seconds <= 0:
            raise ValueError("breaker limits must be positive")
        self.registry = registry
        self.result_store = result_store
        self.site_timeout_seconds = float(site_timeout_seconds)
        self.total_timeout_seconds = float(total_timeout_seconds)
        self.max_results_per_site = int(max_results_per_site)
        self.max_concurrency = int(max_concurrency)
        self.cache_ttl_seconds = int(cache_ttl_seconds)
        self.max_cache_entries = int(max_cache_entries)
        self.breaker_failure_threshold = int(breaker_failure_threshold)
        self.breaker_cooldown_seconds = int(breaker_cooldown_seconds)
        enabled = registry.enabled_ids() if enabled_site_ids is None else tuple(enabled_site_ids)
        self.enabled_site_ids = frozenset(enabled)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: OrderedDict[tuple[object, ...], _CacheEntry] = OrderedDict()
        self._cache_lock = threading.RLock()
        self._breaker_lock = threading.RLock()
        self._breaker_states: dict[str, _BreakerState] = {}
        self._runtime_lock = threading.RLock()
        self._inflight: dict[
            tuple[asyncio.AbstractEventLoop, tuple[object, ...]],
            asyncio.Task[AggregatedIndexerResult],
        ] = {}
        self._loop_semaphores: dict[
            asyncio.AbstractEventLoop, asyncio.Semaphore
        ] = {}

    async def search(
        self,
        query: str,
        page: int = 1,
        site_ids: Iterable[str] | None = None,
        *,
        sort_mode: str = "relevance_desc",
    ) -> AggregatedIndexerResult:
        request = IndexerSearchRequest.create(
            query,
            page,
            sort_mode=sort_mode,
        )
        selected = self._select_sites(site_ids)
        plans = {site_id: (request.query,) for site_id in selected}
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
        )

    async def search_media(
        self,
        request: IndexerMediaSearchRequest,
        site_ids: Iterable[str] | None = None,
    ) -> AggregatedIndexerResult:
        if not isinstance(request, IndexerMediaSearchRequest):
            raise IndexerValidationError("invalid media search request")
        selected = self._select_sites(site_ids)
        plans = {site_id: build_site_queries(site_id, request) for site_id in selected}
        plan_identity = tuple((site_id, plans[site_id]) for site_id in selected)
        cache_key = ("media", request.cache_identity(), request.page, selected, plan_identity)
        return await self._search_plans(
            display_query=request.title,
            page=request.page,
            selected=selected,
            plans=plans,
            cache_key=cache_key,
            ranking_context=request,
            sort_mode=request.sort_mode,
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
    ) -> AggregatedIndexerResult:
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached
        loop = asyncio.get_running_loop()
        inflight_key = (loop, cache_key)
        with self._runtime_lock:
            task = self._inflight.get(inflight_key)
            created = task is None
            if task is None:
                task = asyncio.create_task(self._search_plans_uncached(
                    display_query=display_query,
                    page=page,
                    selected=selected,
                    plans=plans,
                    cache_key=cache_key,
                    ranking_context=ranking_context,
                    sort_mode=sort_mode,
                ))
                self._inflight[inflight_key] = task

                def cleanup(
                    done_task: asyncio.Task[AggregatedIndexerResult],
                    key=inflight_key,
                ) -> None:
                    with self._runtime_lock:
                        if self._inflight.get(key) is done_task:
                            self._inflight.pop(key, None)
                        active_loops = {active_key[0] for active_key in self._inflight}
                        if key[0] not in active_loops:
                            self._loop_semaphores.pop(key[0], None)

                task.add_done_callback(cleanup)
        result = await asyncio.shield(task)
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

        tasks = {
            site_id: asyncio.create_task(
                self._search_site_plan(
                    site_id,
                    plans[site_id],
                    page,
                    ranking_context=ranking_context,
                    sort_mode=sort_mode,
                )
            )
            for site_id in selected
        }
        total_timeout_seconds = self.total_timeout_seconds + self._maximum_timeout_overhead(selected)
        done, pending = await asyncio.wait(tasks.values(), timeout=total_timeout_seconds)
        outcome_by_site: dict[str, _ProviderOutcome] = {}
        for task in done:
            outcome = task.result()
            outcome_by_site[outcome.site_id] = outcome
        if pending:
            pending_sites = {site_id for site_id, task in tasks.items() if task in pending}
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for site_id in pending_sites:
                queries = plans.get(site_id, ())
                outcome_by_site[site_id] = _ProviderOutcome(
                    site_id=site_id,
                    error=self._public_error(site_id, IndexerTimeout("total search timeout")),
                    query=queries[0] if queries else "",
                    attempts=0,
                )

        candidates: list[tuple[int, int, IndexerItem]] = []
        succeeded: list[str] = []
        errors: list[IndexerProviderError] = []
        site_item_counts: dict[str, int] = {}
        site_visible_counts: dict[str, int] = {site_id: 0 for site_id in selected}
        site_queries: dict[str, str] = {}
        site_attempt_counts: dict[str, int] = {}
        site_page_states: dict[str, IndexerSitePageState] = {}
        has_more = False
        for site_index, site_id in enumerate(selected):
            outcome = outcome_by_site.get(site_id)
            adapter = self.registry.get(site_id)
            adapter_pagination_supported = bool(
                getattr(getattr(adapter, "capabilities", None), "pagination_supported", False)
            )
            if outcome is None:
                errors.append(self._public_error(site_id, IndexerTimeout("missing provider outcome")))
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
                    outcome.error.code if outcome.error is not None else "invalid_response",
                    outcome.duration_ms,
                    outcome.attempts,
                )
                continue
            succeeded.append(site_id)
            page_result = outcome.page
            page_has_more = bool(page < 100 and page_result and page_result.has_more)
            has_more = has_more or page_has_more
            site_page_states[site_id] = IndexerSitePageState(
                pagination_supported=bool(page_result.pagination_supported if page_result else adapter_pagination_supported),
                requested_page=page,
                has_more=page_has_more,
                next_page=page + 1 if page_has_more else None,
            )
            provider_items = (page_result.items if page_result is not None else [])[: self.max_results_per_site]
            site_item_counts[site_id] = 0
            for provider_index, candidate in enumerate(provider_items):
                if candidate.site_id != site_id:
                    errors.append(self._public_error(site_id, IndexerInvalidResponse("provider returned mismatched site_id")))
                    continue
                site_item_counts[site_id] += 1
                ranked = rank_item(
                    candidate,
                    media=ranking_context,
                    fallback_query=display_query,
                    now=self._clock(),
                )
                candidates.append((site_index, provider_index, ranked))
            logger.info(
                "indexer.site_search site_id=%s outcome=%s duration_ms=%d attempts=%d item_count=%d",
                site_id,
                "partial" if outcome.error is not None else ("success" if provider_items else "empty"),
                outcome.duration_ms,
                outcome.attempts,
                site_item_counts[site_id],
            )

        candidates = self._deduplicate_candidates(candidates)
        for _site_index, _provider_index, candidate in candidates:
            site_visible_counts[candidate.site_id] += 1
        candidates.sort(key=lambda entry: self._candidate_sort_key(entry, sort_mode))
        ranked_items = annotate_clusters([entry[2] for entry in candidates])
        items: list[IndexerItem] = []
        for candidate in ranked_items:
            result_id = self.result_store.put(candidate)
            items.append(candidate.with_result_id(result_id))

        site_fallbacks: dict[str, str] = {}
        error_sites = {error.site_id for error in errors}
        if (
            "btbtla" in selected
            and "1lou" in selected
            and "btbtla" in error_sites
            and site_item_counts.get("1lou", 0) > 0
        ):
            site_fallbacks["btbtla"] = "1lou"

        result = AggregatedIndexerResult(
            query=display_query,
            page=page,
            items=items,
            sites_attempted=selected,
            sites_succeeded=tuple(succeeded),
            site_item_counts=site_item_counts,
            site_visible_counts=site_visible_counts,
            site_queries=site_queries,
            site_attempt_counts=site_attempt_counts,
            site_fallbacks=site_fallbacks,
            site_page_states=site_page_states,
            has_more=has_more,
            errors=errors,
            partial=bool(errors),
            cached=False,
        )
        logger.info(
            "indexer.search sites_attempted=%d sites_succeeded=%d items=%d partial=%s cached=false",
            len(selected), len(succeeded), len(items), bool(errors),
        )
        cache_ttl = self._cache_ttl_for(result)
        if cache_ttl is not None:
            self._put_cached(cache_key, result, cache_ttl)
        return result.clone(cached=False)

    async def resolve(self, result_id: str) -> ResolvedDownload:
        stored_result = self.result_store.get(result_id)
        site_id = stored_result.site_id
        if site_id not in self.registry.ids() or site_id not in self.enabled_site_ids:
            raise IndexerSecurityError("stored result provider is not enabled")
        if stored_result.download_state not in {"ready", "resolvable"}:
            raise IndexerInvalidResponse("stored result is not downloadable")
        adapter = self.registry.get(site_id)
        try:
            resolved = await asyncio.wait_for(
                adapter.resolve(stored_result),
                timeout=self.site_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise IndexerTimeout() from exc
        except asyncio.CancelledError:
            raise
        except IndexerError as exc:
            raise type(exc)(public_message=exc.public_message) from None
        except Exception as exc:
            raise IndexerUnavailable() from exc
        if not isinstance(resolved, ResolvedDownload):
            raise IndexerInvalidResponse("provider returned an invalid resolved download")
        if resolved.kind not in stored_result.download_kinds:
            raise IndexerInvalidResponse("provider returned an undeclared download kind")
        if resolved.kind == "magnet" and (not isinstance(resolved.value, str) or not magnet_infohash(resolved.value)):
            raise IndexerInvalidResponse("provider returned an invalid magnet")
        if resolved.kind == "torrent" and not isinstance(resolved.value, (str, bytes)):
            raise IndexerInvalidResponse("provider returned an invalid torrent candidate")
        return resolved

    async def aclose(self) -> None:
        await self.registry.aclose()

    def media_site_route(
        self,
        *,
        is_animation: bool,
        original_language: str = "",
    ) -> tuple[str, ...]:
        """按媒体语义返回启用站点的收敛子集，供未显式配置站点的调用方使用。"""
        from .config import plan_media_site_route

        available = tuple(
            site_id for site_id in self.registry.ids() if site_id in self.enabled_site_ids
        )
        return plan_media_site_route(
            available,
            is_animation=is_animation,
            original_language=original_language,
        )

    def _select_sites(self, site_ids: Iterable[str] | None) -> tuple[str, ...]:
        requested = (
            tuple(site_id for site_id in self.registry.ids() if site_id in self.enabled_site_ids)
            if site_ids is None else tuple(site_ids)
        )
        selected: list[str] = []
        for raw_site_id in requested:
            site_id = str(raw_site_id or "").strip().lower()
            if not site_id or site_id in selected:
                continue
            if site_id not in self.registry.ids() or site_id not in self.enabled_site_ids:
                raise IndexerValidationError(f"unknown or disabled site: {site_id}")
            selected.append(site_id)
        if not selected:
            raise IndexerValidationError("at least one enabled indexer site is required")
        return tuple(selected)

    @staticmethod
    def _published_timestamp(value: datetime | None) -> float:
        if value is None:
            return -1.0
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()

    @classmethod
    def _candidate_preference(
        cls,
        entry: tuple[int, int, IndexerItem],
    ) -> tuple[object, ...]:
        site_index, provider_index, item = entry
        state_rank = {"ready": 2, "resolvable": 1}.get(item.download_state, 0)
        actionable_rank = int(state_rank > 0)
        published_value = cls._published_timestamp(item.published_at)
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
            int(item.relevance_score or 0),
            state_rank,
            int(item.seeders if item.seeders is not None else -1),
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

    @classmethod
    def _candidate_sort_key(
        cls,
        entry: tuple[int, int, IndexerItem],
        sort_mode: str,
    ) -> tuple[object, ...]:
        site_index, provider_index, item = entry
        relevance = int(item.relevance_score or 0)
        seeders = int(item.seeders if item.seeders is not None else -1)
        size = item.size_bytes
        published_value = cls._published_timestamp(item.published_at)
        position = parse_indexer_release_position(item.title)
        season = position.get("season")
        episode = position.get("episode")
        episode_end = position.get("episode_end") or episode
        if "episode_exact" in item.match_reasons:
            position_priority = 0
        elif "episode_range" in item.match_reasons:
            position_priority = 1
        elif "season_match" in item.match_reasons:
            position_priority = 2
        elif "episode_conflict" in item.match_reasons:
            position_priority = 4
        else:
            position_priority = 3
        stable = (-relevance, -seeders, -published_value, site_index, provider_index)
        if sort_mode == "published_desc":
            return (position_priority, -published_value, -relevance, -seeders, site_index, provider_index)
        if sort_mode == "episode_desc":
            return (
                position_priority,
                -(season if season is not None else -1),
                -(episode_end if episode_end is not None else -1),
                -(episode if episode is not None else -1),
                *stable,
            )
        if sort_mode == "seeders_desc":
            return (position_priority, -seeders, -relevance, -published_value, site_index, provider_index)
        if sort_mode == "size_desc":
            return (position_priority, size is None, -(size if size is not None else 0), *stable)
        if sort_mode == "size_asc":
            return (position_priority, size is None, size if size is not None else 0, *stable)
        return (position_priority, *stable)

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
        timeout_overhead = getattr(adapter, "search_timeout_overhead_seconds", None)
        overhead_seconds = float(timeout_overhead()) if callable(timeout_overhead) else 0.0
        plan_budget_seconds = self.site_timeout_seconds + max(0.0, overhead_seconds)
        plan_started = perf_counter()
        last_outcome: _ProviderOutcome | None = None
        last_error: _ProviderOutcome | None = None
        merged_items: list[IndexerItem] = []
        contributed_query = ""
        has_more = False
        pagination_supported = bool(
            getattr(getattr(adapter, "capabilities", None), "pagination_supported", False)
        )
        attempts_made = 0
        duration_ms = 0
        for attempts, query in enumerate(queries, start=1):
            remaining_seconds = plan_budget_seconds - (perf_counter() - plan_started)
            if attempts > 1 and remaining_seconds <= 0:
                break
            attempts_made = attempts
            media_type = ranking_context.media_type if ranking_context is not None else ""
            season = ranking_context.season if ranking_context is not None else None
            episode = ranking_context.episode if ranking_context is not None else None
            outcome = await self._search_site(
                site_id,
                IndexerSearchRequest.create(
                    query,
                    page,
                    media_type=media_type,
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
                self._record_circuit_failure(site_id, code=outcome.error.code)
                last_error = outcome
                break
            self._record_circuit_success(site_id)
            last_outcome = outcome
            if outcome.page is None:
                continue
            has_more = has_more or bool(outcome.page.has_more)
            pagination_supported = pagination_supported or bool(outcome.page.pagination_supported)
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
                if self._site_plan_quality_satisfied(
                    merged_items,
                    media=ranking_context,
                    fallback_query=query,
                ):
                    break
        if merged_items:
            ranked_items = [
                rank_item(
                    item,
                    media=ranking_context,
                    fallback_query=contributed_query or (queries[0] if queries else ""),
                    now=self._clock(),
                )
                for item in merged_items
            ]
            ranked_entries = list(enumerate(ranked_items))
            ranked_entries.sort(
                key=lambda entry: self._candidate_sort_key(
                    (0, entry[0], entry[1]),
                    sort_mode,
                )
            )
            ranked_items = [item for _provider_index, item in ranked_entries]
            return _ProviderOutcome(
                site_id=site_id,
                page=IndexerPage(
                    items=ranked_items[: self.max_results_per_site],
                    page=page,
                    has_more=has_more,
                    pagination_supported=pagination_supported,
                ),
                query=contributed_query or (queries[0] if queries else ""),
                attempts=attempts_made,
                duration_ms=duration_ms,
                error=last_error.error if last_error is not None else None,
            )
        if last_error is not None:
            last_error.attempts = attempts_made
            last_error.duration_ms = duration_ms
            return last_error
        if last_outcome is not None:
            last_outcome.attempts = attempts_made
            last_outcome.duration_ms = duration_ms
            return last_outcome
        return _ProviderOutcome(
            site_id=site_id,
            error=self._public_error(site_id, IndexerValidationError("site query plan is empty")),
        )

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
            existing_position = next(
                (positions[key] for key in keys if key in positions),
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
                int(item.relevance_score or 0) >= 78
                and "episode_exact" in item.match_reasons
                for item in ranked
            )
        strong = [item for item in ranked if int(item.relevance_score or 0) >= 78]
        return len(strong) >= 3 or any(int(item.relevance_score or 0) >= 80 for item in strong)

    def _circuit_error(self, site_id: str) -> IndexerProviderError | None:
        now = self._clock()
        with self._breaker_lock:
            state = self._breaker_states.get(site_id)
            if state is not None and state.open_until is not None and state.open_until > now:
                if state.code == "rate_limited":
                    return IndexerProviderError(
                        site_id=site_id,
                        code="rate_limited",
                        message="索引站点请求过于频繁，已暂时冷却",
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
        immediate = code in {"rate_limited", "security_error"}
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
            timeout_overhead = getattr(adapter, "search_timeout_overhead_seconds", None)
            overhead_seconds = float(timeout_overhead()) if callable(timeout_overhead) else 0.0
            default_timeout_seconds = self.site_timeout_seconds + max(0.0, overhead_seconds)
            provider_timeout_seconds = (
                default_timeout_seconds
                if timeout_seconds is None
                else max(0.001, min(default_timeout_seconds, float(timeout_seconds)))
            )
            async with self._loop_semaphore():
                page = await asyncio.wait_for(adapter.search(request), timeout=provider_timeout_seconds)
            if not isinstance(page, IndexerPage):
                raise IndexerInvalidResponse("provider returned an invalid page")
            return _ProviderOutcome(site_id=site_id, page=page, duration_ms=round((perf_counter() - started) * 1000))
        except asyncio.TimeoutError:
            outcome = self._error_outcome(site_id, IndexerTimeout("site timeout"))
        except asyncio.CancelledError:
            raise
        except IndexerError as exc:
            outcome = self._error_outcome(site_id, exc)
        except Exception:
            outcome = self._error_outcome(site_id, IndexerUnavailable("unexpected provider failure"))
        outcome.duration_ms = round((perf_counter() - started) * 1000)
        return outcome

    def _maximum_timeout_overhead(self, selected: tuple[str, ...]) -> float:
        overhead_seconds = 0.0
        for site_id in selected:
            adapter = self.registry.get(site_id)
            timeout_overhead = getattr(adapter, "search_timeout_overhead_seconds", None)
            if callable(timeout_overhead):
                overhead_seconds = max(overhead_seconds, max(0.0, float(timeout_overhead())))
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

    def _error_outcome(self, site_id: str, error: IndexerError) -> _ProviderOutcome:
        return _ProviderOutcome(site_id=site_id, error=self._public_error(site_id, error))

    @staticmethod
    def _public_error(site_id: str, error: IndexerError) -> IndexerProviderError:
        return IndexerProviderError(site_id=site_id, code=error.code, message=error.public_message)

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
            replace(item, result_id=self.result_store.put(replace(item, result_id=None)))
            for item in base.items
        ]
        return base

    def _cache_ttl_for(self, result: AggregatedIndexerResult) -> float | None:
        if result.items:
            return min(self.cache_ttl_seconds, 15) if result.partial else self.cache_ttl_seconds
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
        cached_result.items = [replace(item, result_id=None) for item in cached_result.items]
        with self._cache_lock:
            expired = [cache_key for cache_key, entry in self._cache.items() if entry.expires_at <= now]
            for cache_key in expired:
                self._cache.pop(cache_key, None)
            self._cache[key] = _CacheEntry(
                result=cached_result,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            self._cache.move_to_end(key)
            while len(self._cache) > self.max_cache_entries:
                self._cache.popitem(last=False)
