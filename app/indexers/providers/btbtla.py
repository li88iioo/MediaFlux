from __future__ import annotations

import asyncio
import html
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup

from ..errors import (
    IndexerInvalidResponse,
    IndexerRateLimited,
    IndexerResultExpired,
    IndexerSecurityError,
    IndexerUnavailable,
)
from ..models import IndexerCapabilities, IndexerItem, IndexerPage, IndexerSearchRequest, ResolvedDownload
from .base import IndexerAdapter, fixed_host_join, magnet_infohash, parse_size_bytes, require_html_response

_MAGNET_CANDIDATE = re.compile(r"magnet:\?[^\"'\s<>]+", re.IGNORECASE)
_RESOURCE_SIZE_SUFFIX = re.compile(r"\[\s*([0-9]+(?:\.[0-9]+)?\s*[KMGTPE]?i?B)\s*\]\s*$", re.IGNORECASE)
_SEARCH_TOKEN = re.compile(r"[^0-9a-z\u3400-\u9fff]+", re.IGNORECASE)
_ACCESS_BLOCK_MARKERS = (
    "cloudflare",
    "cf-chl-",
    "attention required",
    "access denied",
    "访问过于频繁",
    "请求过于频繁",
    "人机验证",
)


class BTBtlaAdapter(IndexerAdapter):
    site_id = "btbtla"
    site_name = "BTBtla"
    base_url = "https://www.btbtla.com/"
    default_enabled = True
    capabilities = IndexerCapabilities(pagination_supported=False, download_kinds=("magnet",))

    def __init__(
        self,
        *,
        http,
        min_interval_seconds: float = 0,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ):
        self.http = http
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleeper or asyncio.sleep
        self._request_lock = asyncio.Lock()
        self._last_request_started = 0.0

    async def search(self, request: IndexerSearchRequest) -> IndexerPage:
        if request.page > 1:
            return IndexerPage(items=[], page=request.page, has_more=False, pagination_supported=False)
        search_url = fixed_host_join(self.base_url, f"/search/{quote(request.query, safe='')}")
        response = await self._get(search_url)
        self._validate_response("search", response)
        require_html_response(response)
        soup = BeautifulSoup(response.body, "lxml")
        candidates = self._parse_search_candidates(soup)
        text = soup.get_text(" ", strip=True).lower()
        if not candidates:
            if any(marker in text for marker in ("暂无", "无结果", "no result")):
                return IndexerPage(items=[], page=1, has_more=False, pagination_supported=False)
            raise IndexerInvalidResponse("BTBtla search page structure is invalid")

        _, detail_url, category = max(
            candidates,
            key=lambda candidate: self._search_candidate_score(request.query, candidate[0]),
        )
        detail = await self._get(detail_url)
        self._validate_response("detail", detail)
        require_html_response(detail)
        detail_soup = BeautifulSoup(detail.body, "lxml")
        items = self._parse_resource_items(detail_soup, category=category)
        if (
            not items
            and detail_soup.select_one("#download-list") is None
            and not detail_soup.select(".module-row-info")
        ):
            raise IndexerInvalidResponse("BTBtla detail page omitted the resource list")
        return IndexerPage(items=items, page=1, has_more=False, pagination_supported=False)

    async def resolve(self, stored_result: IndexerItem) -> ResolvedDownload:
        self._validate_stored_result(stored_result)
        detail_url = fixed_host_join(self.base_url, stored_result.detail_url or "")
        if urlsplit(detail_url).path.startswith("/tdown/"):
            download_url = detail_url
        else:
            detail = await self._get(detail_url)
            self._validate_response("detail", detail)
            require_html_response(detail)
            download_url = self._select_download_url(BeautifulSoup(detail.body, "lxml"))
            if not download_url:
                raise IndexerInvalidResponse("BTBtla detail omitted download link")

        download = await self._get(download_url)
        self._validate_response("download", download)
        require_html_response(download)
        magnet = self._extract_magnet(download.body)
        return ResolvedDownload(kind="magnet", value=magnet)

    def _parse_search_candidates(self, soup: BeautifulSoup) -> list[tuple[str, str, str | None]]:
        candidates: list[tuple[str, str, str | None]] = []
        for node in soup.select("div.module-item"):
            # 站点改版把标题锚点从 div.video-name a 换成 a.module-item-title；
            # 两种布局都接受，最后兜底任意 /detail/ 链接。
            anchor = (
                node.select_one("a.module-item-title[href]")
                or node.select_one("div.video-name a[href]")
                or next(
                    (
                        candidate
                        for candidate in node.find_all("a", href=True)
                        if str(candidate.get("href") or "").startswith("/detail/")
                    ),
                    None,
                )
            )
            if anchor is None:
                continue
            title = str(anchor.get("title") or anchor.get_text(" ", strip=True)).strip()
            if not title:
                continue
            try:
                detail_url = fixed_host_join(self.base_url, anchor.get("href"))
            except IndexerSecurityError:
                continue
            category = self._parse_search_category(node)
            candidates.append((title, detail_url, category))
        return candidates

    @staticmethod
    def _parse_search_category(node) -> str | None:
        category_node = node.select_one("div.module-item-caption span.video-class")
        if category_node is not None:
            text = category_node.get_text(" ", strip=True)
            if text:
                return text
        # 新布局的 caption 是无类名 span 序列：年份 / 分类 / 地区。
        caption = node.select_one("div.module-item-caption")
        if caption is not None:
            spans = [span.get_text(" ", strip=True) for span in caption.find_all("span")]
            spans = [text for text in spans if text]
            if len(spans) >= 2 and not spans[1].isdigit():
                return spans[1]
        return None

    def _parse_resource_items(self, soup: BeautifulSoup, *, category: str | None) -> list[IndexerItem]:
        items: list[IndexerItem] = []
        seen_urls: set[str] = set()
        # 旧布局：#download-list 容器内的 module-row-one；新布局可能只保留
        # module-row-info 块，两者都解析。
        rows = soup.select("#download-list .module-row-one") or soup.select(".module-row-info")
        for row in rows:
            resource_anchor = None
            resource_url = ""
            anchors = row.select("a.module-row-text[href]") or row.find_all("a", href=True)
            for anchor in anchors:
                try:
                    candidate = fixed_host_join(self.base_url, anchor.get("href"))
                except IndexerSecurityError:
                    continue
                if urlsplit(candidate).path.startswith("/tdown/"):
                    resource_anchor = anchor
                    resource_url = candidate
                    break
            if resource_anchor is None or resource_url in seen_urls:
                continue
            seen_urls.add(resource_url)

            visible_title = resource_anchor.get_text(" ", strip=True)
            if not visible_title:
                heading = row.find("h4")
                if heading is not None:
                    visible_title = heading.get_text(" ", strip=True)
            size_match = _RESOURCE_SIZE_SUFFIX.search(visible_title)
            size_text = size_match.group(1).strip() if size_match else None
            title = _RESOURCE_SIZE_SUFFIX.sub("", visible_title).strip()
            if not title:
                title = str(resource_anchor.get("title") or "").strip()
                title = re.sub(r"\.torrent$", "", title, flags=re.IGNORECASE).strip()
            if not title:
                continue

            downloads = None
            download_counter = row.select_one("a.btn-down[href]")
            if download_counter is not None:
                count_match = re.search(r"\d+", download_counter.get_text(" ", strip=True))
                if count_match:
                    downloads = int(count_match.group(0))
            items.append(
                IndexerItem(
                    site_id=self.site_id,
                    site_name=self.site_name,
                    title=title,
                    detail_url=resource_url,
                    category=category,
                    size_text=size_text,
                    size_bytes=parse_size_bytes(size_text),
                    downloads=downloads,
                    download_state="resolvable",
                    download_kinds=("magnet",),
                )
            )
        return items

    @staticmethod
    def _search_candidate_score(query: str, title: str) -> tuple[int, int]:
        normalize = lambda value: _SEARCH_TOKEN.sub("", unicodedata.normalize("NFKC", value).casefold())
        normalized_query = normalize(query)
        normalized_title = normalize(title)
        if normalized_title == normalized_query:
            return (3, 0)
        if normalized_query and normalized_query in normalized_title:
            return (2, -abs(len(normalized_title) - len(normalized_query)))
        if normalized_title and normalized_title in normalized_query:
            return (1, -abs(len(normalized_title) - len(normalized_query)))
        return (0, -len(normalized_title))

    async def _get(self, url: str):
        async with self._request_lock:
            now = self._monotonic()
            remaining = self.min_interval_seconds - (now - self._last_request_started)
            if self._last_request_started and remaining > 0:
                await self._sleep(remaining)
            self._last_request_started = self._monotonic()
            return await self.http.get(url)

    def _select_download_url(self, soup: BeautifulSoup) -> str:
        candidates: list[str] = []
        for anchor in soup.select("a.btn-down[href]"):
            try:
                candidate = fixed_host_join(self.base_url, anchor.get("href"))
            except IndexerSecurityError:
                continue
            candidates.append(candidate)
        candidates.sort(key=lambda value: 0 if urlsplit(value).path.startswith("/tdown/") else 1)
        return candidates[0] if candidates else ""

    def _validate_stored_result(self, stored_result: IndexerItem) -> None:
        if stored_result.site_id != self.site_id:
            raise IndexerSecurityError("result provider mismatch")
        if stored_result.download_state != "resolvable" or "magnet" not in stored_result.download_kinds:
            raise IndexerInvalidResponse("result is not resolvable as magnet")
        if not stored_result.detail_url:
            raise IndexerInvalidResponse("result detail URL is missing")

    @staticmethod
    def _extract_magnet(body: bytes) -> str:
        text = html.unescape(body.decode("utf-8", errors="replace"))
        soup = BeautifulSoup(text, "lxml")
        for anchor in soup.select("a[href]"):
            candidate = html.unescape(str(anchor.get("href") or "").strip())
            if magnet_infohash(candidate):
                return candidate
        for candidate in _MAGNET_CANDIDATE.findall(text):
            normalized = html.unescape(candidate)
            if magnet_infohash(normalized):
                return normalized
        raise IndexerInvalidResponse("BTBtla download page omitted a valid magnet")

    @staticmethod
    def _looks_access_blocked(response) -> bool:
        server = str(response.headers.get("server") or "").lower()
        body = response.body[:64 * 1024].decode("utf-8", errors="replace").lower()
        return "cloudflare" in server and any(marker in body for marker in _ACCESS_BLOCK_MARKERS)

    @classmethod
    def _validate_response(cls, stage: str, response) -> None:
        status_code = int(response.status_code)
        if status_code == 429:
            raise IndexerRateLimited("BTBtla returned HTTP 429")
        if status_code == 404:
            if stage == "search" or cls._looks_access_blocked(response):
                raise IndexerUnavailable("BTBtla temporarily rejected the request")
            raise IndexerResultExpired("BTBtla resource page no longer exists")
        if status_code >= 500:
            raise IndexerUnavailable(f"BTBtla returned HTTP {status_code}")
        if status_code != 200:
            raise IndexerInvalidResponse(f"BTBtla returned HTTP {status_code}")
