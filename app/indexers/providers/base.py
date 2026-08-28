from __future__ import annotations

import asyncio
import base64
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, urljoin, urlsplit

from ..concurrency import CrossLoopAsyncLock
from ..errors import IndexerInvalidResponse, IndexerSecurityError
from ..models import IndexerCapabilities, IndexerItem, IndexerPage, IndexerSearchRequest, ResolvedDownload

_INFOHASH_HEX = re.compile(r"^[0-9a-fA-F]{40}$")
_INFOHASH_BASE32 = re.compile(r"^[A-Z2-7]{32}$", re.IGNORECASE)
_BTMH_SHA256 = re.compile(r"^1220[0-9a-fA-F]{64}$")
_CHALLENGE_MARKERS = (
    "just a moment",
    "verify you are human",
    "performing security verification",
    "challenge-platform",
    "cf-browser-verification",
    "turnstile",
)


class IndexerAdapter(ABC):
    site_id: str
    site_name: str
    base_url: str
    default_enabled: bool
    capabilities: IndexerCapabilities

    @abstractmethod
    async def search(self, request: IndexerSearchRequest) -> IndexerPage:
        raise NotImplementedError

    @abstractmethod
    async def resolve(self, stored_result: IndexerItem) -> ResolvedDownload:
        raise NotImplementedError

    def iter_http_clients(self) -> tuple[object, ...]:
        client = getattr(self, "http", None)
        return (client,) if client is not None else ()

    async def wait_for_search_slot(self, request: IndexerSearchRequest) -> None:
        return None

    def search_timeout_overhead_seconds(self) -> float:
        """不挤占站点主体请求预算的轻量预检时长，默认没有额外预算。"""
        return 0.0


class SearchRequestPacer:
    """为无自身限流能力的站点提供每适配器平滑请求间隔。"""

    def __init__(
        self,
        interval_seconds: float = 0,
        *,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.interval_seconds = max(0.0, float(interval_seconds))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleeper or asyncio.sleep
        self._lock = CrossLoopAsyncLock()
        self._last_started: float | None = None

    async def wait(self) -> None:
        if self.interval_seconds <= 0:
            return
        async with self._lock:
            now = self._monotonic()
            if self._last_started is not None:
                remaining = self.interval_seconds - (now - self._last_started)
                if remaining > 0:
                    await self._sleep(remaining)
            self._last_started = self._monotonic()


class DirectResultAdapter(IndexerAdapter):
    async def resolve(self, stored_result: IndexerItem) -> ResolvedDownload:
        if stored_result.site_id != self.site_id:
            raise IndexerSecurityError("result provider mismatch")
        if stored_result.magnet and magnet_infohash(stored_result.magnet):
            return ResolvedDownload(kind="magnet", value=stored_result.magnet)
        if stored_result.torrent_url:
            safe_url = fixed_host_join(self.base_url, stored_result.torrent_url)
            return ResolvedDownload(kind="torrent", value=safe_url)
        raise IndexerInvalidResponse("result has no downloadable candidate")


def fixed_host_join(base_url: str, candidate: str) -> str:
    absolute = urljoin(base_url, str(candidate or "").strip())
    base = urlsplit(base_url)
    parsed = urlsplit(absolute)
    if parsed.scheme != "https" or parsed.hostname != base.hostname or parsed.port not in (None, 443):
        raise IndexerSecurityError("provider result escaped its registered host")
    if parsed.username or parsed.password or parsed.fragment:
        raise IndexerSecurityError("provider result URL contains forbidden components")
    return absolute


def magnet_infohash(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "magnet":
        return None
    xt_values = parse_qs(parsed.query).get("xt", [])
    # Hybrid magnet 优先使用 v1 BTIH，与 qB/libtorrent 的 TorrentID 保持一致。
    for xt in xt_values:
        prefix = "urn:btih:"
        if xt.lower().startswith(prefix):
            infohash = xt[len(prefix) :]
            if _INFOHASH_HEX.fullmatch(infohash):
                return infohash.lower()
            if _INFOHASH_BASE32.fullmatch(infohash):
                try:
                    return base64.b32decode(infohash.upper()).hex()
                except (ValueError, TypeError):
                    return None
    for xt in xt_values:
        prefix = "urn:btmh:"
        if xt.lower().startswith(prefix):
            multihash = xt[len(prefix) :]
            if _BTMH_SHA256.fullmatch(multihash):
                return multihash[4:44].lower()
    return None


def is_likely_challenge_page(
    body: bytes | str,
    *,
    usable_markers: tuple[str, ...] = (),
) -> bool:
    """识别返回 HTTP 200 的人机验证页，同时避免覆盖包含有效业务结构的页面。"""
    if isinstance(body, bytes):
        text = body[:256 * 1024].decode("utf-8", errors="replace")
    else:
        text = str(body or "")[:256 * 1024]
    normalized = text.casefold()
    if any(marker.casefold() in normalized for marker in usable_markers):
        return False
    return any(marker in normalized for marker in _CHALLENGE_MARKERS)


def require_html_response(response) -> None:
    content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise IndexerInvalidResponse("provider returned a non-HTML response")


def parse_size_bytes(value: str | None) -> int | None:
    text = str(value or "").strip()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE]?i?B)", text, re.IGNORECASE)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2).lower()
    powers = {
        "b": 0,
        "kb": 1,
        "kib": 1,
        "mb": 2,
        "mib": 2,
        "gb": 3,
        "gib": 3,
        "tb": 4,
        "tib": 4,
        "pb": 5,
        "pib": 5,
        "eb": 6,
        "eib": 6,
    }
    base = 1024 if "i" in unit else 1000
    return int(amount * (base ** powers[unit]))
