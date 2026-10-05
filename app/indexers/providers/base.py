from __future__ import annotations

import asyncio
import base64
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

from app.concurrency import CrossLoopAsyncLock
from ..errors import IndexerInvalidResponse, IndexerSecurityError
from ..models import IndexerCapabilities, IndexerItem, IndexerPage, IndexerSearchRequest, ResolvedDownload

# 有限公开Tracker种子列表；不在运行时拉取远程列表，也不承诺可用性或下载速度。
PUBLIC_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://open.demonii.com:1337/announce",
)

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


# 当前执行上下文的只读页通知，不保存独立结果，也不创建额外搜索任务。
page_observer: ContextVar[Callable[[str, IndexerPage], None] | None] = ContextVar("indexer_page_observer", default=None)


def report_page(site_id: str, page: IndexerPage) -> None:
    observer = page_observer.get()
    if observer is not None:
        observer(site_id, page)


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

    def _join_known_host(self, candidate: str, *, relative_base_url: str | None = None) -> str:
        """同一注册站点及其镜像共用相对地址解析，避免各适配器复制策略。"""
        bases = getattr(self, "_host_bases", (self.base_url,))
        if relative_base_url is not None:
            bases = tuple(dict.fromkeys((relative_base_url, *bases)))
        last_error: IndexerSecurityError | None = None
        for base_url in bases:
            try:
                return fixed_host_join(base_url, candidate)
            except IndexerSecurityError as exc:
                last_error = exc
        assert last_error is not None
        raise last_error


    def http_client_for_result(self, stored_result: IndexerItem):
        """外围读取来源链接/种子时，使用该冻结结果所属的客户端。"""
        if stored_result.site_id != self.site_id:
            raise IndexerSecurityError("result provider mismatch")
        return self.http

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


def augment_public_magnet(value: str, title: str = "", *, trackers=PUBLIC_TRACKERS) -> str:
    """仅供已知公开资源：保留原参数/编码/哈希，只补缺失名称和有限Tracker。"""
    if not magnet_infohash(value):
        return value
    params = parse_qs(urlsplit(value).query, keep_blank_values=True)
    extra: list[tuple[str, str]] = []
    if title.strip() and not any(name.strip() for name in params.get("dn", ())):
        extra.append(("dn", title.strip()))
    seen = {tracker.rstrip("/").casefold() for tracker in params.get("tr", ())}
    added = 0
    for tracker in trackers:
        tracker = str(tracker or "").strip()
        parsed = urlsplit(tracker)
        key = tracker.rstrip("/").casefold()
        if (key in seen or parsed.scheme not in {"udp", "http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment):
            continue
        seen.add(key)
        extra.append(("tr", tracker))
        added += 1
        if added == len(PUBLIC_TRACKERS):
            break
    if not extra:
        return value
    body, separator, fragment = value.partition("#")
    joiner = "" if body.endswith(("?", "&")) else "&"
    return body + joiner + urlencode(extra, quote_via=quote) + separator + fragment


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
