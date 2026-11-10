"""Discovery 元数据统一搜索。"""
from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping

import requests

from app import config as app_config
from app.clients.douban_authenticated import normalize_dbcl2
from app.clients.tmdb import TMDBClient
from app.discovery.models import MediaCard, ProviderError, ProviderInvalidResponse
from app.discovery.providers.bangumi import BangumiProvider
from app.discovery.providers.base import (
    DEFAULT_TIMEOUT,
    map_request_error,
    response_json,
)
from app.discovery.providers.douban import DoubanProvider
from app.discovery.providers.tmdb import TMDBProvider
from app.logger import get_logger
from app.discovery.models import DISCOVERY_PROVIDERS

logger = get_logger(__name__)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

_SAFE_ERROR_MESSAGES = {
    "timeout": "数据源请求超时",
    "rate_limited": "数据源请求受限",
    "authentication": "数据源认证失败",
    "not_configured": "数据源未配置",
    "invalid_response": "数据源响应无效",
    "unavailable": "数据源暂不可用",
}


@dataclass(frozen=True)
class DiscoverySearchResult:
    query: str
    page: int
    items: tuple[MediaCard, ...]
    has_more: bool
    providers_attempted: tuple[str, ...]
    providers_succeeded: tuple[str, ...]
    errors: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        from app.discovery.service import confirmed_tmdb_mappings

        mappings = confirmed_tmdb_mappings(self.items)
        partial = bool(self.errors and self.providers_succeeded)
        status = ("partial" if partial else "success" if self.items else "empty") if self.providers_succeeded else "unavailable"
        return {**asdict(self), "items": [replace(card, tmdb_id=mappings.get(
                    (card.provider, card.external_id, card.media_type), card.tmdb_id)).to_dict() for card in self.items],
                "ok": bool(self.providers_succeeded), "status": status, "partial": partial}


class TMDBSearchProvider:
    name = "tmdb"

    def __init__(self, client: TMDBClient | None = None):
        self.client = client or TMDBClient()

    def search(self, query: str, page: int) -> tuple[list[MediaCard], bool]:
        payload = self.client.get(
            "/search/multi",
            {"query": query, "page": page, "include_adult": "false"},
        )
        results = payload.get("results")
        if not isinstance(results, list):
            raise ProviderInvalidResponse("TMDB 搜索响应结构无效")
        cards: list[MediaCard] = []
        for raw in results:
            if not isinstance(raw, dict):
                continue
            media_type = str(raw.get("media_type") or "").lower()
            if media_type not in {"movie", "tv"}:
                continue
            card = TMDBProvider._card(raw, media_type)
            if card is not None:
                cards.append(card)
        try:
            total_pages = int(payload.get("total_pages") or page)
        except (TypeError, ValueError):
            total_pages = page
        return cards, page < min(total_pages, 100)


    def close(self) -> None:
        close = getattr(getattr(self.client, "session", None), "close", None)
        if callable(close):
            close()


class DoubanSearchProvider:
    name = "douban"
    base_url = "https://movie.douban.com"

    def __init__(self, *, session: requests.Session | Any | None = None, timeout=DEFAULT_TIMEOUT):
        self.session = session or requests.Session()
        self.timeout = timeout
        if hasattr(self.session, "trust_env"):
            self.session.trust_env = False

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Referer": f"{self.base_url}/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        try:
            dbcl2 = normalize_dbcl2(app_config.get("DOUBAN_DBCL2", ""))
        except ValueError:
            dbcl2 = ""
        if dbcl2:
            headers["Cookie"] = f"dbcl2={dbcl2}"
        return headers

    def search(self, query: str, page: int) -> tuple[list[MediaCard], bool]:
        if page > 1:
            return [], False
        try:
            response = self.session.get(
                f"{self.base_url}/j/subject_suggest",
                params={"q": query},
                headers=self._headers(),
                timeout=self.timeout,
                allow_redirects=False,
            )
            payload = response_json(response, expected_type=list)
        except ProviderError:
            raise
        except (requests.Timeout, requests.RequestException) as exc:
            raise map_request_error(exc) from exc
        cards: list[MediaCard] = []
        for raw in payload[:20]:
            if not isinstance(raw, Mapping):
                continue
            raw_type = str(raw.get("type") or "movie").strip().lower()
            media_type = "tv" if raw_type in {"tv", "series", "电视剧"} else "movie"
            normalized = dict(raw)
            normalized["media_type"] = media_type
            normalized["poster_url"] = raw.get("img") or raw.get("cover_url") or raw.get("cover")
            card = DoubanProvider._card(normalized, media_type)
            if card is not None:
                cards.append(card)
        return cards, False


    def close(self) -> None:
        close = getattr(self.session, "close", None)
        if callable(close):
            close()


class BangumiSearchProvider:
    name = "bangumi"
    base_url = "https://api.bgm.tv"

    def __init__(
        self,
        *,
        session: requests.Session | Any | None = None,
        user_agent: str | None = None,
        timeout=DEFAULT_TIMEOUT,
    ):
        self.session = session or requests.Session()
        self.user_agent = (user_agent or app_config.get("BANGUMI_USER_AGENT", "MediaFlux/1.0") or "MediaFlux/1.0").strip()
        self.timeout = timeout
        if hasattr(self.session, "trust_env"):
            self.session.trust_env = False

    def search(self, query: str, page: int) -> tuple[list[MediaCard], bool]:
        limit = 20
        offset = (page - 1) * limit
        try:
            response = self.session.post(
                f"{self.base_url}/v0/search/subjects",
                params={"limit": limit, "offset": offset},
                json={"keyword": query, "sort": "match", "filter": {"type": [2]}},
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                timeout=self.timeout,
                allow_redirects=False,
            )
            payload = response_json(response, expected_type=dict)
        except ProviderError:
            raise
        except (requests.Timeout, requests.RequestException) as exc:
            raise map_request_error(exc) from exc
        data = payload.get("data")
        if not isinstance(data, list):
            raise ProviderInvalidResponse("Bangumi 搜索响应结构无效")
        cards = [
            card
            for raw in data
            if isinstance(raw, dict)
            if (card := BangumiProvider._card(raw, None)) is not None
        ]
        try:
            total = int(payload.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        return cards, bool(total and offset + len(data) < min(total, 2000))


    def close(self) -> None:
        close = getattr(self.session, "close", None)
        if callable(close):
            close()


class DiscoverySearchService:
    def __init__(
        self,
        providers: Mapping[str, Any] | None = None,
        *,
        executor: ThreadPoolExecutor | None = None,
    ):
        self.providers = dict(providers or {
            "tmdb": TMDBSearchProvider(),
            "douban": DoubanSearchProvider(),
            "bangumi": BangumiSearchProvider(),
        })
        self._executor = executor or ThreadPoolExecutor(max_workers=3, thread_name_prefix="discovery-search")
        self._owns_executor = executor is None
        self._closed = False
        self._state_lock = threading.RLock()
        self._inflight: set[Future] = set()
        self._provider_close_lock = threading.Lock()
        self._closed_provider_ids: set[int] = set()

    @property
    def closed(self) -> bool:
        with self._state_lock:
            return self._closed

    def _close_providers(self) -> bool:
        # close 失败的 provider 保留在待关闭集合中；后续 shutdown/get 会仅重试
        # 失败项，已经成功释放的共享 provider 不重复关闭。
        with self._provider_close_lock:
            unique: list[tuple[str, Any]] = []
            seen: set[int] = set()
            for name, provider in self.providers.items():
                identity = id(provider)
                if identity in seen:
                    continue
                seen.add(identity)
                unique.append((name, provider))

            for name, provider in unique:
                identity = id(provider)
                if identity in self._closed_provider_ids:
                    continue
                close = getattr(provider, "close", None)
                if not callable(close):
                    self._closed_provider_ids.add(identity)
                    continue
                try:
                    close()
                except Exception as exc:
                    logger.warning(
                        "Discovery provider close failed provider=%s type=%s",
                        name,
                        type(exc).__name__,
                    )
                    continue
                self._closed_provider_ids.add(identity)
            return len(self._closed_provider_ids) == len(unique)

    def _future_finished(self, future: Future) -> None:
        close_providers = False
        with self._state_lock:
            self._inflight.discard(future)
            close_providers = self._closed and not self._inflight
        if close_providers and self._close_providers():
            _release_discovery_search_service(self)

    def _enabled_names(self) -> list[str]:
        names = [name for name in DISCOVERY_PROVIDERS if name in self.providers]
        if not app_config.get_bool("DISCOVERY_DOUBAN_ENABLED", True):
            names = [name for name in names if name != "douban"]
        return names

    def _normalize(self, query: str, page: int, provider_names: list[str] | tuple[str, ...] | None):
        if not isinstance(query, str):
            raise ValueError("搜索关键词必须是字符串")
        normalized_query = unicodedata.normalize("NFKC", (query or "")).strip()
        has_control = any(unicodedata.category(char).startswith("C") for char in normalized_query)
        if not normalized_query or len(normalized_query) > 120 or has_control or _CONTROL_RE.search(normalized_query):
            raise ValueError("搜索关键词必须为 1 到 120 个可见字符")

        if type(page) is not int or page < 1 or page > 100:
            raise ValueError("页码必须在 1 到 100 之间")
        enabled = self._enabled_names()
        if provider_names is not None and (
            not isinstance(provider_names, (list, tuple)) or not provider_names
            or len(provider_names) > 3 or not all(isinstance(name, str) for name in provider_names)
        ):
            raise ValueError("providers 必须选择 1 到 3 个来源")
        selected = list(provider_names if provider_names is not None else enabled)
        selected = list(dict.fromkeys((name or "").strip().lower() for name in selected if (name or "").strip()))
        if not selected:
            raise ValueError("至少选择一个搜索来源")
        unknown = [name for name in selected if name not in DISCOVERY_PROVIDERS]
        if unknown:
            raise ValueError("不支持的搜索来源")
        disabled = [name for name in selected if name not in enabled]
        if disabled:
            raise ValueError("搜索来源已关闭")
        return normalized_query, page, selected

    def search(
        self,
        query: str,
        page: int = 1,
        provider_names: list[str] | tuple[str, ...] | None = None,
        *,
        timeout_seconds: float | None = None,
        media_type: str = "",
        year: str = "",
        region: str = "",
        genre: str = "",
        cancelled: Callable[[], bool] | None = None,
    ) -> DiscoverySearchResult:
        query, page, selected = self._normalize(query, page, provider_names)
        if isinstance(media_type, str):
            media_type = media_type.strip().lower()
        if isinstance(year, str):
            year = year.strip()
        if media_type not in {"", "movie", "tv"}:
            raise ValueError("media_type 必须是 movie 或 tv")
        if not isinstance(year, str) or (year and not re.fullmatch(r"(?:19|20)[0-9]{2}", year)):
            raise ValueError("year 必须是 1900 到 2099 的四位年份")
        for value in (region, genre):
            if not isinstance(value, str) or any(unicodedata.category(char).startswith("C") for char in value):
                raise ValueError("地区和题材必须是可见文本")
        # 类型/年份精确过滤；地区/题材只作关键词，不能宣称硬性筛选。
        folded = query.casefold()
        suffixes = [unicodedata.normalize("NFKC", value).strip() for value in (region, genre)]
        query = " ".join([query, *(value for value in suffixes if value and value.casefold() not in folded)])
        if len(query) > 120:
            raise ValueError("片名与地区/题材关键词合计不能超过 120 个字符")
        if cancelled and cancelled():
            raise RuntimeError("搜索已取消")
        attempted = tuple(selected)
        succeeded: list[str] = []
        errors_by_name: dict[str, dict[str, Any]] = {}
        items_by_name: dict[str, list[MediaCard]] = {}
        more_by_name: dict[str, bool] = {}

        future_map: dict[Future, str] = {}
        try:
            with self._state_lock:
                if self._closed:
                    raise RuntimeError("Discovery search service is closed")
                # 每次 submit 成功后立即登记，避免后续 submit 异常让已运行 Future
                # 脱离生命周期账本并在 shutdown 时被提前关闭 provider。
                for name in selected:
                    if name not in self.providers:
                        continue
                    future = self._executor.submit(
                        self.providers[name].search, query, page,
                    )
                    future_map[future] = name
                    self._inflight.add(future)
        finally:
            # 已完成 Future 的 callback 可能同步执行；必须在 state lock 外注册，
            # 否则极快 provider 会在同一线程回调并自锁。
            for future in future_map:
                future.add_done_callback(self._future_finished)
        missing = [name for name in selected if name not in self.providers]
        for name in missing:
            errors_by_name[name] = {
                "provider": name,
                "code": "unavailable",
                "message": "数据源暂不可用",
                "retry_after": 0,
            }
        budget = None if timeout_seconds is None else max(0.1, float(timeout_seconds))
        deadline = None if budget is None else time.monotonic() + budget
        pending = set(future_map)
        while pending:
            if cancelled and cancelled():
                for future in pending:
                    future.cancel()
                raise RuntimeError("搜索已取消")
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            _, pending = wait(pending, timeout=min(0.05, remaining) if remaining is not None else (0.05 if cancelled else None))
            if deadline is not None and time.monotonic() >= deadline:
                break
        done = set(future_map) - pending
        for future in pending:
            name = future_map[future]
            future.cancel()
            errors_by_name[name] = {
                "provider": name, "code": "timeout",
                "message": _SAFE_ERROR_MESSAGES["timeout"], "retry_after": 0,
            }
        for future in done:
            name = future_map[future]
            try:
                cards, has_more = future.result()
                items_by_name[name] = [card for card in cards if isinstance(card, MediaCard)]
                more_by_name[name] = bool(has_more)
                succeeded.append(name)
            except ProviderError as exc:
                errors_by_name[name] = {
                    "provider": name,
                    "code": exc.code,
                    "message": _SAFE_ERROR_MESSAGES.get(exc.code, "数据源暂不可用"),
                    "retry_after": exc.retry_after,
                }
            except Exception as exc:
                logger.warning("Discovery search provider failed provider=%s type=%s", name, type(exc).__name__)
                errors_by_name[name] = {
                    "provider": name,
                    "code": "unavailable",
                    "message": "数据源暂不可用",
                    "retry_after": 0,
                }

        ordered_succeeded = tuple(name for name in selected if name in succeeded)
        seen: set[str] = set()
        items: list[MediaCard] = []
        for name in selected:
            for card in items_by_name.get(name, []):
                if (card.stable_id in seen or (media_type and card.media_type != media_type)
                        or (year and card.year.strip() != year)):
                    continue
                seen.add(card.stable_id)
                items.append(card)
        errors = tuple(errors_by_name[name] for name in selected if name in errors_by_name)
        return DiscoverySearchResult(
            query=query,
            page=page,
            items=tuple(items),
            has_more=any(more_by_name.values()),
            providers_attempted=attempted,
            providers_succeeded=ordered_succeeded,
            errors=errors,
        )


    def shutdown(self) -> bool:
        with self._state_lock:
            first_attempt = not self._closed
            self._closed = True
            close_providers = not self._inflight
        if first_attempt and self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
        if not close_providers:
            return False
        closed = self._close_providers()
        if closed:
            _release_discovery_search_service(self)
        return closed


_search_service: DiscoverySearchService | None = None
_search_service_lock = threading.Lock()


def _release_discovery_search_service(service: DiscoverySearchService) -> None:
    global _search_service
    with _search_service_lock:
        if _search_service is service:
            _search_service = None


def get_discovery_search_service() -> DiscoverySearchService:
    global _search_service
    service = _search_service
    if service is not None and service.closed:
        service.shutdown()
    if _search_service is None:
        with _search_service_lock:
            if _search_service is None:
                _search_service = DiscoverySearchService()
    return _search_service


def shutdown_discovery_search_service() -> bool:
    service = _search_service
    if service is None:
        return True
    return service.shutdown()
