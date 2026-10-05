from __future__ import annotations

import asyncio
import time
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from ..errors import (
    IndexerError,
    IndexerSecurityError,
    IndexerTimeout,
    IndexerUnavailable,
)
from ..models import (
    IndexerCapabilities,
    IndexerItem,
    IndexerPage,
    IndexerProviderError,
    IndexerSearchRequest,
    IndexerSourceStatus,
    ResolvedDownload,
)
from ..ranking import rank_item
from ..release import parse_indexer_release_position
from .base import IndexerAdapter, fixed_host_join

_CACHE_LIMIT = 256
_SUCCESS_TTL = 120.0
_EMPTY_TTL = 8.0
_PARTIAL_TTL = 8.0
_COOLDOWN_SECONDS = 30.0
_INITIAL_SOURCES = 2
_MAX_ACTIVE_SOURCES = 3
_HEDGE_DELAY_SECONDS = 0.12
_COLD_ORDER = {"aipan": 0, "btbtla": 1}


@dataclass(slots=True)
class _CachedPage:
    expires_at: float
    page: IndexerPage
    stale_until: float


@dataclass(slots=True)
class _SourceFlight:
    task: asyncio.Task[tuple[IndexerPage, bool]]
    subscribers: int = 0


class GeneralAdapter(IndexerAdapter):
    """一个公共站点入口，保留子站来源和失败证据，不引入第二套搜索/下载管线。"""

    site_id = "btbtla"
    site_name = "综合"
    base_url = "https://www.btbtlb.com/"
    default_enabled = True
    capabilities = IndexerCapabilities(pagination_supported=True, download_kinds=("magnet", "torrent"))

    def __init__(self, members: tuple[IndexerAdapter, ...], *, timeout_seconds: float = 9, cache_ttl_seconds: float = _SUCCESS_TTL):
        self.members = members
        self.timeout_seconds = timeout_seconds
        if cache_ttl_seconds <= 0:
            raise ValueError("source cache TTL must be positive")
        self.cache_ttl_seconds = float(cache_ttl_seconds)
        self._cache_lock = threading.RLock()
        self._cooldowns: dict[str, tuple[float, IndexerProviderError]] = {}
        self._cache: OrderedDict[tuple[str, IndexerSearchRequest], _CachedPage] = OrderedDict()
        self._inflight: dict[tuple[asyncio.AbstractEventLoop, str, IndexerSearchRequest], _SourceFlight] = {}
        # (平均响应秒数, 成功数, 失败数)：只用于朴素排序，不参与停止/裁剪决策。
        self._observations: dict[str, tuple[float, int, int]] = {}

    def iter_http_clients(self) -> tuple[object, ...]:
        return tuple(client for member in self.members for client in member.iter_http_clients())

    def search_timeout_overhead_seconds(self) -> float:
        # BT 影视既有请求间隔预算不因合并站点而缩短；新源与其并行。
        return max((member.search_timeout_overhead_seconds() for member in self.members), default=0)

    @staticmethod
    def _clone_page(page: IndexerPage) -> IndexerPage:
        return IndexerPage(
            items=[replace(item) for item in page.items],
            page=page.page,
            has_more=page.has_more,
            pagination_supported=page.pagination_supported,
            errors=tuple(page.errors),
            source_statuses=tuple(page.source_statuses),
            complete=page.complete,
            total_items=page.total_items,
        )

    def _cache_entry(self, member: IndexerAdapter, request: IndexerSearchRequest, *, allow_stale: bool = False) -> _CachedPage | None:
        key = (member.site_id, request)
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is None:
                return None
            now = time.monotonic()
            if cached.stale_until <= now:
                self._cache.pop(key, None)
                return None
            if cached.expires_at <= now and not allow_stale:
                return None
            self._cache.move_to_end(key)
            return cached

    def _cached_page(self, member: IndexerAdapter, request: IndexerSearchRequest) -> IndexerPage | None:
        cached = self._cache_entry(member, request)
        return self._clone_page(cached.page) if cached is not None else None

    def _cache_has(self, member: IndexerAdapter, request: IndexerSearchRequest) -> bool:
        return self._cache_entry(member, request) is not None

    def _remember_page(self, member: IndexerAdapter, request: IndexerSearchRequest, page: IndexerPage) -> None:
        if page.errors and not page.items:
            return
        ttl = _PARTIAL_TTL if page.errors else self.cache_ttl_seconds if page.items else _EMPTY_TTL
        ttl = min(ttl, self.cache_ttl_seconds)
        now = time.monotonic()
        cached = _CachedPage(now + ttl, self._clone_page(page), now + (self.cache_ttl_seconds if page.items else ttl))
        with self._cache_lock:
            self._cache[(member.site_id, request)] = cached
            self._cache.move_to_end((member.site_id, request))
            while len(self._cache) > _CACHE_LIMIT:
                self._cache.popitem(last=False)

    def _observe(self, member: IndexerAdapter, elapsed: float, succeeded: bool) -> None:
        previous = self._observations.get(member.site_id)
        if previous is None:
            self._observations[member.site_id] = (elapsed, int(succeeded), int(not succeeded))
            return
        average, successes, failures = previous
        attempts = successes + failures
        self._observations[member.site_id] = (
            (average * attempts + elapsed) / (attempts + 1),
            successes + int(succeeded),
            failures + int(not succeeded),
        )

    def _member_order(self, members: tuple[IndexerAdapter, ...], request: IndexerSearchRequest) -> list[IndexerAdapter]:
        member_position = {member.site_id: index for index, member in enumerate(members)}

        def priority(member: IndexerAdapter) -> tuple[float, ...]:
            observation = self._observations.get(member.site_id)
            if observation is None:
                return (
                    0 if self._cache_has(member, request) else 1,
                    1,
                    float(_COLD_ORDER.get(member.site_id, len(_COLD_ORDER) + member_position[member.site_id])),
                    0.0,
                )
            average, successes, failures = observation
            attempts = successes + failures
            return (
                0 if self._cache_has(member, request) else 1,
                0,
                failures / attempts,
                average,
                float(member_position[member.site_id]),
            )

        return sorted(members, key=priority)

    def _initial_members(
        self,
        ordered: list[IndexerAdapter],
    ) -> list[IndexerAdapter]:
        now = time.monotonic()
        healthy = []
        for member in ordered:
            cooldown = self._cooldowns.get(member.site_id)
            if cooldown is not None and now < cooldown[0]:
                continue
            observation = self._observations.get(member.site_id)
            if observation is not None and observation[1] <= observation[2]:
                continue
            healthy.append(member)

        if not healthy:
            return ordered[:_INITIAL_SOURCES]

        first = healthy[0]
        initial = [first]
        btbtla = next((member for member in healthy if member.site_id == "btbtla"), None)
        if btbtla is not None and btbtla is not first:
            initial.append(btbtla)
        if len(initial) < _INITIAL_SOURCES:
            second = next((member for member in healthy if member is not first), None)
            if second is not None:
                initial.append(second)
        return initial

    async def _search_member(self, member: IndexerAdapter, request: IndexerSearchRequest) -> IndexerPage:
        cooldown = self._cooldowns.get(member.site_id)
        if cooldown is not None:
            if time.monotonic() < cooldown[0]:
                return IndexerPage(
                    [], request.page, False, member.capabilities.pagination_supported, (cooldown[1],)
                )
            self._cooldowns.pop(member.site_id, None)

        try:
            async with asyncio.timeout(self.timeout_seconds + member.search_timeout_overhead_seconds()):
                await member.wait_for_search_slot(request)
                page = await member.search(request)
            # 单个详情损坏不封禁整个来源；明确限流才需要全源冷却。
            limited = next((error for error in page.errors if error.code == "rate_limited"), None)
            if limited is not None:
                self._cooldowns[member.site_id] = (time.monotonic() + _COOLDOWN_SECONDS, limited)
            else:
                self._cooldowns.pop(member.site_id, None)
            return page
        except TimeoutError:
            error = IndexerTimeout()
        except IndexerError as exc:
            error = exc
        except Exception:  # noqa: BLE001 - isolate any source-specific failure
            error = IndexerUnavailable()

        public_error = IndexerProviderError(
            self.site_id, error.code, f"{member.site_name}：{error.public_message}"
        )
        # 单站频控/故障独立冷却，不阻断其余综合来源，也不随别名检索反复敲打。
        self._cooldowns[member.site_id] = (time.monotonic() + _COOLDOWN_SECONDS, public_error)
        return IndexerPage(
            [], request.page, False, member.capabilities.pagination_supported, (public_error,)
        )

    async def _load_source(self, member: IndexerAdapter, request: IndexerSearchRequest) -> tuple[IndexerPage, bool]:
        started = time.monotonic()
        cooldown = self._cooldowns.get(member.site_id)
        cooling = cooldown is not None and started < cooldown[0]
        page = await self._search_member(member, request)
        if not cooling:
            self._observe(member, time.monotonic() - started, bool(page.items) or not page.errors)
        previous = self._cache_entry(member, request, allow_stale=True)
        if page.errors and not page.items and previous is not None and previous.page.items:
            # 短期回看旧成功结果，但明确携带当前错误；绝不延长原始数据寿命。
            retained = self._clone_page(previous.page)
            retained.errors = tuple(dict.fromkeys((*retained.errors, *page.errors)))
            return retained, True
        self._remember_page(member, request, page)
        return page, False

    async def _source_page(
        self, member: IndexerAdapter, request: IndexerSearchRequest
    ) -> tuple[IndexerPage, bool]:
        cached = self._cached_page(member, request)
        if cached is not None:
            return cached, True

        loop = asyncio.get_running_loop()
        key = (loop, member.site_id, request)
        flight = self._inflight.get(key)
        if flight is None:
            flight = _SourceFlight(asyncio.create_task(self._load_source(member, request)))
            self._inflight[key] = flight

            def discard_finished(task: asyncio.Task[tuple[IndexerPage, bool]]) -> None:
                current = self._inflight.get(key)
                if current is flight:
                    self._inflight.pop(key, None)

            flight.task.add_done_callback(discard_finished)
        flight.subscribers += 1
        try:
            page, cached = await asyncio.shield(flight.task)
            return self._clone_page(page), cached
        finally:
            flight.subscribers -= 1
            if flight.subscribers == 0 and not flight.task.done():
                if self._inflight.get(key) is flight:
                    self._inflight.pop(key, None)
                flight.task.cancel()
                try:
                    await flight.task
                except asyncio.CancelledError:
                    pass

    @staticmethod
    def _is_downloadable(item: IndexerItem) -> bool:
        if item.download_state == "ready":
            return bool(item.magnet or item.torrent_url)
        if item.download_state == "resolvable":
            return bool(item.magnet or item.torrent_url or item.detail_url)
        return False

    @staticmethod
    def _is_title_relevant(item: IndexerItem, request: IndexerSearchRequest) -> bool:
        ranked = rank_item(item, media=None, fallback_query=request.query)
        return bool(
            {"title_exact", "title_contains", "title_similar"}
            & set(ranked.match_reasons)
        )

    @classmethod
    def _has_exact_coverage(cls, request: IndexerSearchRequest, pages: dict[str, IndexerPage]) -> bool:
        if request.season is None and request.episode is None:
            return False
        for page in pages.values():
            for item in page.items:
                if (
                    not cls._is_downloadable(item)
                    or not cls._is_title_relevant(item, request)
                ):
                    continue
                position = parse_indexer_release_position(item.title)
                season = position["season"]
                episode = position["episode"]
                episode_end = position["episode_end"]
                if request.season is not None and season != request.season:
                    continue
                if request.episode is not None:
                    if episode is None:
                        continue
                    if episode_end is None and episode != request.episode:
                        continue
                    if episode_end is not None and not episode <= request.episode <= episode_end:
                        continue
                return True
        return False

    @classmethod
    def _has_valid_results(
        cls, request: IndexerSearchRequest, pages: dict[str, IndexerPage]
    ) -> bool:
        return any(
            cls._is_downloadable(item) and cls._is_title_relevant(item, request)
            for page in pages.values()
            for item in page.items
        )

    def _aggregate(
        self,
        request: IndexerSearchRequest,
        members: tuple[IndexerAdapter, ...],
        pages: dict[str, IndexerPage],
        statuses: dict[str, IndexerSourceStatus],
        *,
        complete: bool,
    ) -> IndexerPage:
        source_pages = [(member, pages[member.site_id]) for member in members if member.site_id in pages]
        return IndexerPage(
            items=[
                replace(item, site_id=self.site_id)
                for _, page in source_pages
                for item in page.items
            ],
            page=request.page,
            has_more=any(page.has_more for _, page in source_pages),
            pagination_supported=True,
            errors=tuple(dict.fromkeys(error for _, page in source_pages for error in page.errors)),
            source_statuses=tuple(statuses[member.site_id] for member in members),
            complete=complete,
        )

    def _report_progress(
        self,
        request: IndexerSearchRequest,
        members: tuple[IndexerAdapter, ...],
        pages: dict[str, IndexerPage],
        statuses: dict[str, IndexerSourceStatus],
    ) -> None:
        from .base import report_page

        report_page(
            self.site_id,
            self._aggregate(request, members, pages, statuses, complete=False),
        )

    @staticmethod
    def _source_status(
        member: IndexerAdapter, page: IndexerPage, *, cached: bool
    ) -> IndexerSourceStatus:
        if page.errors:
            status = "partial" if page.items else "error"
            code = page.errors[0].code
        else:
            status = "success" if page.items else "empty"
            code = ""
        return IndexerSourceStatus(
            site_id=member.site_id,
            site_name=member.site_name,
            status=status,
            count=len(page.items),
            cached=cached,
            code=code,
        )

    async def search(self, request: IndexerSearchRequest) -> IndexerPage:
        if request.page > 1:
            members = tuple(
                member for member in self.members
                if member.capabilities.pagination_supported
            )
        else:
            members = self.members
        if not members:
            return IndexerPage([], request.page, False, True, complete=True)

        ordered = self._member_order(members, request)
        initial = self._initial_members(ordered)
        initial_ids = {member.site_id for member in initial}
        by_id = {member.site_id: member for member in members}
        pending = [member.site_id for member in ordered if member.site_id not in initial_ids]
        pages: dict[str, IndexerPage] = {}
        statuses = {
            member.site_id: IndexerSourceStatus(member.site_id, member.site_name, "pending")
            for member in members
        }
        active: dict[asyncio.Task[tuple[IndexerPage, bool]], IndexerAdapter] = {}
        loop = asyncio.get_running_loop()
        hedge_at: float | None = None

        def start(member_id: str) -> None:
            member = by_id[member_id]
            statuses[member_id] = IndexerSourceStatus(member.site_id, member.site_name, "searching")
            active[asyncio.create_task(self._source_page(member, request))] = member
            self._report_progress(request, members, pages, statuses)

        try:
            for member in initial:
                start(member.site_id)
            if pending:
                hedge_at = loop.time() + _HEDGE_DELAY_SECONDS
            while pending or active:
                if pending and len(active) < _MAX_ACTIVE_SOURCES:
                    if active and hedge_at is not None and loop.time() < hedge_at:
                        timeout = hedge_at - loop.time()
                    else:
                        timeout = 0.0

                    if timeout == 0.0:
                        while pending and len(active) < _MAX_ACTIVE_SOURCES:
                            start(pending.pop(0))
                        hedge_at = None
                        continue
                else:
                    timeout = None

                done, _ = await asyncio.wait(
                    active,
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    hedge_at = None
                    continue

                for task in done:
                    member = active.pop(task)
                    page, cached = task.result()
                    pages[member.site_id] = page
                    statuses[member.site_id] = self._source_status(member, page, cached=cached)
                    self._report_progress(request, members, pages, statuses)

                has_signal = (
                    self._has_exact_coverage(request, pages)
                    if request.season is not None or request.episode is not None
                    else self._has_valid_results(request, pages)
                )
                hedge_at = loop.time() + _HEDGE_DELAY_SECONDS if has_signal and pending else None
        finally:
            unfinished = tuple(active)
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)

        return self._aggregate(request, members, pages, statuses, complete=True)

    def _member_for_result(self, stored_result: IndexerItem) -> IndexerAdapter:
        if stored_result.site_id != self.site_id:
            raise IndexerSecurityError("result provider mismatch")
        # 来源取冻结结果的已校验URL，不相信展示名称，也不跨子站借用客户端。
        host = urlsplit(stored_result.detail_url or "").hostname
        for member in self.members:
            for base in (member.base_url, *getattr(member, "mirror_base_urls", ())):
                if host == urlsplit(base).hostname:
                    fixed_host_join(base, stored_result.detail_url or "")
                    return member
        raise IndexerSecurityError("result origin is not a registered general source")

    def http_client_for_result(self, stored_result: IndexerItem):
        member = self._member_for_result(stored_result)
        return member.http_client_for_result(replace(stored_result, site_id=member.site_id))

    async def resolve(self, stored_result: IndexerItem) -> ResolvedDownload:
        member = self._member_for_result(stored_result)
        return await member.resolve(replace(stored_result, site_id=member.site_id))
