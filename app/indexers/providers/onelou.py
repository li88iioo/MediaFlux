from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup
import httpx

from ..errors import (
    IndexerError,
    IndexerInvalidResponse,
    IndexerRateLimited,
    IndexerSecurityError,
    IndexerUnavailable,
)
from ..models import IndexerCapabilities, IndexerItem, IndexerPage, IndexerSearchRequest, ResolvedDownload
from .base import IndexerAdapter, fixed_host_join, magnet_infohash, require_html_response
from .google_site import GoogleSiteResult, GoogleSiteSearch

# 网盘帖没有种子/磁力，除非标题同时带 BT 标记，否则跳过。
_CLOUD_ONLY_MARKERS = ("夸克", "阿里云盘", "百度网盘", "网盘", "迅雷云盘", "UC网盘", "天翼云盘")
_BT_MARKERS = re.compile(r"BT下载|\[BT\]|\.torrent|磁力|magnet", re.IGNORECASE)
_THREAD_PATH = re.compile(r"^/thread-\d+\.htm$")
_THREAD_HREF = re.compile(r"thread-\d+\.htm")

# 站点搜索 JSON API 的固定参数（“全部”筛选）。
_SEARCH_API_PATH = "/search/api/search.php"
_SEARCH_API_PARAMS = {
    "page": "1",
    "sort": "relevance",
    "scope": "全部",
    "type": "全部",
    "year": "全部",
    "quality": "全部",
    "source": "全部",
    "track": "1",
}


def _looks_like_cloud_only(title: str) -> bool:
    text = str(title or "")
    if _BT_MARKERS.search(text):
        return False
    return any(marker in text for marker in _CLOUD_ONLY_MARKERS)


class OneLouAdapter(IndexerAdapter):
    site_id = "1lou"
    site_name = "1lou"
    base_url = "https://www.1lou.me/"
    default_enabled = True
    capabilities = IndexerCapabilities(pagination_supported=False, download_kinds=("torrent", "magnet"))

    # .pro 与 .me 当前共享相同 API/详情结构；裸域用于兼容历史存量结果。
    mirror_base_urls = ("https://www.1lou.pro/",)
    _host_bases = (
        "https://www.1lou.me/",
        "https://1lou.me/",
        "https://www.1lou.pro/",
        "https://1lou.pro/",
    )

    def __init__(
        self,
        *,
        http,
        google_search: GoogleSiteSearch | None = None,
        min_interval_seconds: float = 0,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ):
        self.http = http
        self.google_search = google_search
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleeper or asyncio.sleep
        self._search_slot_lock = asyncio.Lock()
        self._last_search_started = 0.0

    def iter_http_clients(self) -> tuple[object, ...]:
        clients: list[object] = [self.http]
        if self.google_search is not None:
            clients.append(self.google_search.http)
        return tuple(clients)

    async def wait_for_search_slot(self, request: IndexerSearchRequest) -> None:
        """在服务层单站点超时之外平滑请求，避免候选标题形成瞬时突发。"""
        if request.page > 1 or self.min_interval_seconds <= 0:
            return
        async with self._search_slot_lock:
            now = self._monotonic()
            remaining = self.min_interval_seconds - (now - self._last_search_started)
            if self._last_search_started and remaining > 0:
                await self._sleep(remaining)
            self._last_search_started = self._monotonic()

    def search_timeout_overhead_seconds(self) -> float:
        # Google 是可选预检；失败后仍给 1LOU 原生链完整的站点超时预算。
        return self.google_search.timeout_seconds if self.google_search is not None else 0.0

    def _join_known_host(self, candidate: str) -> str:
        last_error: IndexerSecurityError | None = None
        for base in self._host_bases:
            try:
                return fixed_host_join(base, candidate)
            except IndexerSecurityError as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def search(self, request: IndexerSearchRequest) -> IndexerPage:
        if request.page > 1:
            return IndexerPage(items=[], page=request.page, has_more=False, pagination_supported=False)
        google_items = await self._search_google(request)
        if google_items:
            return IndexerPage(items=google_items, page=1, has_more=False, pagination_supported=False)
        items = await self._search_native(request)
        return IndexerPage(items=items, page=1, has_more=False, pagination_supported=False)

    async def _search_google(self, request: IndexerSearchRequest) -> list[IndexerItem] | None:
        if self.google_search is None:
            return None
        results = await self.google_search.search(
            request.query,
            site_domain="1lou.me",
            allowed_bases=self._host_bases,
        )
        if not results:
            return None
        items = self._items_from_google(results)
        return items or None

    def _items_from_google(self, results: list[GoogleSiteResult]) -> list[IndexerItem]:
        items: list[IndexerItem] = []
        seen_paths: set[str] = set()
        for result in results:
            title = str(result.title or "").strip()
            try:
                detail_url = self._join_known_host(result.url)
            except IndexerSecurityError:
                continue
            path = urlsplit(detail_url).path
            if not _THREAD_PATH.fullmatch(path) or path in seen_paths:
                continue
            if not title or _looks_like_cloud_only(title):
                continue
            seen_paths.add(path)
            items.append(
                IndexerItem(
                    site_id=self.site_id,
                    site_name=self.site_name,
                    title=title,
                    detail_url=detail_url,
                    download_state="resolvable",
                    download_kinds=("torrent",),
                )
            )
        return items

    async def _search_native(self, request: IndexerSearchRequest) -> list[IndexerItem]:
        best_error: IndexerError | None = None
        html_fallback_bases: list[str] = []
        for base_url in (self.base_url, *self.mirror_base_urls):
            try:
                response = await self.http.get(
                    fixed_host_join(base_url, _SEARCH_API_PATH),
                    params={"q": request.query, **_SEARCH_API_PARAMS},
                )
                self._validate_status(response.status_code)
                items = self._parse_api_items(response.body, base_url=base_url)
                if items is not None:
                    return items
                # 旧镜像偶尔会在 API 路径直接返回搜索 HTML，先就地兼容，避免额外请求。
                try:
                    return self._parse_html_items(response, base_url=base_url)
                except IndexerInvalidResponse as exc:
                    html_fallback_bases.append(base_url)
                    best_error = self._prefer_error(best_error, exc)
            except IndexerInvalidResponse as exc:
                html_fallback_bases.append(base_url)
                best_error = self._prefer_error(best_error, exc)
            except (IndexerRateLimited, IndexerUnavailable, IndexerSecurityError) as exc:
                # 429/5xx/安全跳转不追加旧搜索请求，避免限流时继续施压或放宽 HTTPS。
                best_error = self._prefer_error(best_error, exc)
            except httpx.TransportError:
                best_error = self._prefer_error(
                    best_error,
                    IndexerUnavailable("1lou transport failed"),
                )

        # 仅当 API 结构本身失效时尝试旧版 HTML 路径；镜像 429/5xx 不走此分支。
        for base_url in html_fallback_bases:
            try:
                response = await self.http.get(
                    fixed_host_join(base_url, f"/search-{quote(request.query, safe='')}.htm"),
                )
                self._validate_status(response.status_code)
                return self._parse_html_items(response, base_url=base_url)
            except httpx.TransportError:
                best_error = self._prefer_error(
                    best_error,
                    IndexerUnavailable("1lou legacy search transport failed"),
                )
            except (IndexerInvalidResponse, IndexerRateLimited, IndexerUnavailable, IndexerSecurityError) as exc:
                best_error = self._prefer_error(best_error, exc)
        assert best_error is not None
        raise best_error

    @staticmethod
    def _prefer_error(current: IndexerError | None, candidate: IndexerError) -> IndexerError:
        priorities = {
            IndexerInvalidResponse: 1,
            IndexerUnavailable: 2,
            IndexerSecurityError: 3,
            IndexerRateLimited: 4,
        }
        current_priority = max(
            (priority for error_type, priority in priorities.items() if isinstance(current, error_type)),
            default=0,
        )
        candidate_priority = max(
            (priority for error_type, priority in priorities.items() if isinstance(candidate, error_type)),
            default=0,
        )
        return candidate if candidate_priority > current_priority else current or candidate

    def _parse_api_items(self, body: bytes, *, base_url: str) -> list[IndexerItem] | None:
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict) or not data.get("ok"):
            return None
        hits = ((data.get("data") or {}).get("hits")) if isinstance(data.get("data"), dict) else None
        if not isinstance(hits, list):
            return None
        items: list[IndexerItem] = []
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            title = BeautifulSoup(str(hit.get("subject") or ""), "lxml").get_text(" ", strip=True)
            thread_url = str(hit.get("thread_url") or "").strip()
            if not title or not thread_url or _looks_like_cloud_only(title):
                continue
            try:
                detail_url = fixed_host_join(base_url, thread_url)
            except IndexerSecurityError:
                continue
            published_at = None
            try:
                stamp = int(hit.get("create_date") or 0)
                if stamp > 0:
                    published_at = datetime.fromtimestamp(stamp, tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                published_at = None
            items.append(
                IndexerItem(
                    site_id=self.site_id,
                    site_name=self.site_name,
                    title=title,
                    detail_url=detail_url,
                    published_at=published_at,
                    download_state="resolvable",
                    download_kinds=("torrent",),
                )
            )
        return items

    def _parse_html_items(self, response, *, base_url: str) -> list[IndexerItem]:
        require_html_response(response)
        soup = BeautifulSoup(response.body, "lxml")
        nodes = soup.select("li.media.thread") or soup.select("li.thread") or soup.select("div.threadlist")
        text = soup.get_text(" ", strip=True).casefold()
        if not nodes and not any(marker in text for marker in ("暂无", "无结果", "no result")):
            raise IndexerInvalidResponse("1lou search page structure is invalid")
        items: list[IndexerItem] = []
        for node in nodes:
            anchor = node.select_one("div.subject a[href]") or next(
                (
                    candidate
                    for candidate in node.find_all("a", href=True)
                    if _THREAD_HREF.search(str(candidate.get("href") or ""))
                ),
                None,
            )
            if anchor is None:
                continue
            title = anchor.get_text(" ", strip=True)
            if not title or _looks_like_cloud_only(title):
                continue
            try:
                detail_url = fixed_host_join(base_url, anchor.get("href"))
            except IndexerSecurityError:
                continue
            items.append(
                IndexerItem(
                    site_id=self.site_id,
                    site_name=self.site_name,
                    title=title,
                    detail_url=detail_url,
                    download_state="resolvable",
                    download_kinds=("torrent",),
                )
            )
        return items

    async def resolve(self, stored_result: IndexerItem) -> ResolvedDownload:
        self._validate_stored_result(stored_result)
        detail_url = self._join_known_host(stored_result.detail_url or "")
        response = await self.http.get(detail_url)
        self._validate_status(response.status_code)
        require_html_response(response)
        soup = BeautifulSoup(response.body, "lxml")
        anchor = soup.select_one('a[href*="attach-download"]')
        if anchor is not None:
            torrent_url = self._join_from_detail(detail_url, anchor.get("href"))
            filename = anchor.get_text(" ", strip=True) or None
            return ResolvedDownload(kind="torrent", value=torrent_url, filename=filename)
        # 帖内没有种子附件时回退磁力链接（部分资源帖只贴磁力）。
        for magnet_anchor in soup.select('a[href^="magnet:"]'):
            magnet = str(magnet_anchor.get("href") or "").strip()
            if magnet_infohash(magnet):
                return ResolvedDownload(kind="magnet", value=magnet)
        raise IndexerInvalidResponse("1lou detail omitted torrent attachment")

    def _join_from_detail(self, detail_url: str, candidate: str | None) -> str:
        try:
            return fixed_host_join(detail_url, candidate or "")
        except IndexerSecurityError:
            return self._join_known_host(candidate or "")

    def _validate_stored_result(self, stored_result: IndexerItem) -> None:
        if stored_result.site_id != self.site_id:
            raise IndexerSecurityError("result provider mismatch")
        if stored_result.download_state != "resolvable" or "torrent" not in stored_result.download_kinds:
            raise IndexerInvalidResponse("result is not resolvable as torrent")
        if not stored_result.detail_url:
            raise IndexerInvalidResponse("result detail URL is missing")

    @staticmethod
    def _validate_status(status_code: int) -> None:
        if status_code == 429:
            raise IndexerRateLimited("1lou returned HTTP 429")
        if status_code >= 500:
            raise IndexerUnavailable(f"1lou returned HTTP {status_code}")
        if status_code != 200:
            raise IndexerInvalidResponse(f"1lou returned HTTP {status_code}")
