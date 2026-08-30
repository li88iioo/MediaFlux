"""Bangumi Calendar/Subject 媒体探索 Provider。"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import requests

from app import config as app_config
from app.discovery.models import (
    DiscoveryPage,
    MediaCard,
    ProviderHealth,
    ProviderInvalidResponse,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.discovery.providers.base import (
    DEFAULT_TIMEOUT,
    DiscoveryProvider,
    TimeoutValue,
    map_request_error,
    response_json,
)
from app.logger import get_logger

logger = get_logger(__name__)

_BANGUMI_IMAGE_HOSTS = {"lain.bgm.tv"}


def _image_key(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("large") or value.get("common") or value.get("medium") or value.get("small")
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in _BANGUMI_IMAGE_HOSTS:
        return ""
    path = parsed.path.lstrip("/")
    if not path or ".." in path.split("/"):
        return ""
    return f"{host}/{path}"


def _score(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("score")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class BangumiProvider(DiscoveryProvider):
    name = "bangumi"

    def __init__(
        self,
        *,
        base_url: str = "https://api.bgm.tv",
        user_agent: str | None = None,
        session: requests.Session | Any | None = None,
        clock: Callable[[], datetime] | None = None,
        timeout: TimeoutValue = DEFAULT_TIMEOUT,
        page_size: int = 20,
        calendar_ttl_seconds: int | None = None,
        calendar_attempts: int = 2,
        calendar_clock: Callable[[], float] | None = None,
        config: Mapping[str, Any] | None = None,
    ):
        self.base_url = str(base_url or "https://api.bgm.tv").rstrip("/")
        configured_agent = (
            str(config.get("BANGUMI_USER_AGENT", "")) if config is not None
            else app_config.get("BANGUMI_USER_AGENT", "")
        )
        self.user_agent = str(user_agent or configured_agent or "MediaFlux/1.0").strip()
        self.session = session or requests.Session()
        self.clock = clock or datetime.now
        self.timeout = timeout
        self.page_size = max(1, min(int(page_size), 100))
        configured_ttl = (
            config.get("BANGUMI_CALENDAR_CACHE_TTL_SECONDS") if config is not None
            else app_config.get("BANGUMI_CALENDAR_CACHE_TTL_SECONDS", "")
        )
        try:
            default_ttl = int(str(configured_ttl or "21600"))
        except (TypeError, ValueError):
            default_ttl = 21600
        self.calendar_ttl_seconds = max(
            300, int(calendar_ttl_seconds if calendar_ttl_seconds is not None else default_ttl)
        )
        self.calendar_attempts = max(1, min(int(calendar_attempts), 2))
        self.calendar_retry_seconds = max(30, min(300, self.calendar_ttl_seconds))
        self._calendar_clock = calendar_clock or time.monotonic
        self._calendar_lock = threading.Lock()
        self._calendar_payload: list[Any] | None = None
        self._calendar_expires_at = 0.0
        self._calendar_is_stale = False
        self._close_call_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closing = False
        self._closed = False

    def _ensure_open(self) -> None:
        with self._close_lock:
            if self._closed or self._closing:
                raise ProviderUnavailable("Bangumi 数据源已关闭")

    def close(self) -> bool:
        # 不可在持有 _close_lock 时等待 _calendar_lock：周历加载路径会先持有
        # _calendar_lock 再检查关闭状态，反向加锁会在关机与刷新并发时死锁。
        with self._close_call_lock:
            with self._close_lock:
                if self._closed:
                    return True
                self._closing = True
            close = getattr(self.session, "close", None)
            if callable(close):
                try:
                    closed = close()
                except Exception as exc:
                    logger.warning(
                        "关闭 Bangumi Session 失败 type=%s", type(exc).__name__
                    )
                    return False
                if closed is False:
                    return False
            with self._calendar_lock:
                self._calendar_payload = None
                self._calendar_expires_at = 0.0
                self._calendar_is_stale = False
            with self._close_lock:
                self._closed = True
                self._closing = False
            return True

    def _get(self, path: str, *, expected_type: type) -> Any:
        self._ensure_open()
        try:
            response = self.session.get(
                f"{self.base_url}{path}",
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                timeout=self.timeout,
            )
            return response_json(response, expected_type=expected_type)
        except (requests.Timeout, requests.RequestException) as exc:
            raise map_request_error(exc) from exc

    def _calendar(self) -> tuple[list[Any], bool]:
        """复用完整周历；不同 weekday 只在本地派生，失败时保留旧快照。"""
        now = float(self._calendar_clock())
        with self._calendar_lock:
            if self._calendar_payload is not None and now < self._calendar_expires_at:
                return self._calendar_payload, self._calendar_is_stale

            last_error: ProviderTimeout | ProviderUnavailable | None = None
            for _attempt in range(self.calendar_attempts):
                try:
                    payload = self._get("/calendar", expected_type=list)
                except (ProviderTimeout, ProviderUnavailable) as exc:
                    last_error = exc
                    continue
                self._calendar_payload = payload
                self._calendar_is_stale = False
                self._calendar_expires_at = float(self._calendar_clock()) + self.calendar_ttl_seconds
                return payload, False

            if self._calendar_payload is not None:
                self._calendar_is_stale = True
                self._calendar_expires_at = float(self._calendar_clock()) + self.calendar_retry_seconds
                return self._calendar_payload, True
            assert last_error is not None
            raise last_error

    def list_items(
        self,
        category: str,
        media_type: str,
        page: int,
        filters: dict[str, Any] | None,
    ) -> DiscoveryPage:
        self._ensure_open()
        category = str(category or "").strip().lower()
        if category not in {"weekly", "calendar", "today"}:
            raise ProviderInvalidResponse("不支持的 Bangumi 分类")
        if media_type not in {"tv", "all"}:
            raise ProviderInvalidResponse("Bangumi 仅支持动画条目")
        page = max(1, int(page))
        calendar, calendar_stale = self._calendar()

        selected_weekday: int | None = None
        raw_weekday = (filters or {}).get("weekday")
        if raw_weekday not in (None, ""):
            try:
                selected_weekday = int(raw_weekday)
            except (TypeError, ValueError) as exc:
                raise ProviderInvalidResponse("Bangumi weekday 无效") from exc
            if selected_weekday < 1 or selected_weekday > 7:
                raise ProviderInvalidResponse("Bangumi weekday 无效")
        elif category == "today":
            selected_weekday = self.clock().isoweekday()

        flattened: list[MediaCard] = []
        for group in calendar:
            if not isinstance(group, dict):
                raise ProviderInvalidResponse("Bangumi Calendar 响应结构无效")
            weekday_data = group.get("weekday")
            items = group.get("items")
            if not isinstance(weekday_data, dict) or not isinstance(items, list):
                raise ProviderInvalidResponse("Bangumi Calendar 响应结构无效")
            try:
                upstream_weekday = int(weekday_data.get("id"))
            except (TypeError, ValueError) as exc:
                raise ProviderInvalidResponse("Bangumi weekday 响应无效") from exc
            weekday = 7 if upstream_weekday == 0 else upstream_weekday
            if weekday < 1 or weekday > 7:
                raise ProviderInvalidResponse("Bangumi weekday 响应无效")
            if selected_weekday is not None and weekday != selected_weekday:
                continue
            for raw in items:
                if isinstance(raw, dict):
                    card = self._card(raw, weekday)
                    if card:
                        flattened.append(card)

        start = (page - 1) * self.page_size
        end = start + self.page_size
        return DiscoveryPage(
            items=flattened[start:end],
            page=page,
            has_more=end < len(flattened),
            provider=ProviderHealth(
                name=self.name,
                status="degraded" if calendar_stale else "healthy",
                message="使用缓存周历" if calendar_stale else "",
            ),
        )

    def get_detail(self, external_id: str, media_type: str) -> MediaCard:
        if media_type not in {"tv", "all"}:
            raise ProviderInvalidResponse("Bangumi 仅支持动画条目")
        payload = self._get(f"/v0/subjects/{str(external_id or '').strip()}", expected_type=dict)
        card = self._card(payload, None)
        if card is None:
            raise ProviderInvalidResponse("Bangumi 详情响应结构无效")
        return card

    @staticmethod
    def _card(raw: dict[str, Any], weekday: int | None) -> MediaCard | None:
        external_id = str(raw.get("id") or "").strip()
        if not external_id:
            return None
        release_date = str(raw.get("date") or "").strip()
        return MediaCard(
            provider="bangumi",
            external_id=external_id,
            media_type="tv",
            title=str(raw.get("name_cn") or raw.get("name") or "").strip(),
            original_title=str(raw.get("name") or "").strip(),
            year=release_date[:4] if release_date else "",
            overview=str(raw.get("summary") or "").strip(),
            poster_key=_image_key(raw.get("images")),
            rating=_score(raw.get("rating")),
            rating_source="bangumi",
            release_date=release_date,
            weekday=weekday,
            bangumi_id=external_id,
        )
