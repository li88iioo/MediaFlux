from __future__ import annotations

import asyncio
import html
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from urllib.parse import quote, unquote, urlsplit

from bs4 import BeautifulSoup

from ..concurrency import CrossLoopAsyncLock
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
    base_url = "https://www.btbtlb.com/"
    mirror_base_urls = ("https://btbtlb.com/",)
    default_enabled = True
    capabilities = IndexerCapabilities(pagination_supported=True, download_kinds=("magnet", "torrent"))

    def __init__(
        self,
        *,
        http,
        min_interval_seconds: float = 0,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        max_detail_candidates: int = 2,
        mirror_base_urls: tuple[str, ...] | None = None,
    ):
        self.http = http
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleeper or asyncio.sleep
        self._request_lock = CrossLoopAsyncLock()
        self._last_request_started: float | None = None
        self.max_detail_candidates = max(1, min(int(max_detail_candidates), 3))
        self.mirror_base_urls = tuple(
            dict.fromkeys(self.mirror_base_urls if mirror_base_urls is None else mirror_base_urls)
        )
        self._host_bases = tuple(dict.fromkeys((self.base_url, *self.mirror_base_urls)))

    def search_timeout_overhead_seconds(self) -> float:
        # 最坏可恢复链路：主域搜索 + 全部详情候选，再对每个备用域执行
        # 一次搜索和一次详情解析。把主动节流从网络请求预算中剥离。
        paced_gaps = self.max_detail_candidates + (2 * len(self.mirror_base_urls))
        return self.min_interval_seconds * paced_gaps

    async def search(self, request: IndexerSearchRequest) -> IndexerPage:
        last_error: IndexerRateLimited | IndexerUnavailable | None = None
        for base_url in self._host_bases:
            try:
                detail_limit = self.max_detail_candidates if base_url == self.base_url else 1
                return await self._search_base(base_url, request, detail_limit=detail_limit)
            except (IndexerRateLimited, IndexerUnavailable) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def _search_base(
        self,
        base_url: str,
        request: IndexerSearchRequest,
        *,
        detail_limit: int,
    ) -> IndexerPage:
        search_path = f"/search/{quote(request.query, safe='')}"
        if request.page > 1:
            search_path = f"{search_path}/{request.page}"
        search_url = fixed_host_join(base_url, search_path)
        response = await self._get(search_url)
        try:
            self._validate_response("search", response)
            require_html_response(response)
            response_base_url = self._base_for_url(response.url)
            soup = BeautifulSoup(response.body, "lxml")
            candidates = self._parse_search_candidates(soup, base_url=response_base_url)
            has_more = self._has_search_page_link(
                soup,
                query=request.query,
                page=request.page + 1,
                base_url=response_base_url,
            )
            text = soup.get_text(" ", strip=True).lower()
            if not candidates:
                if any(marker in text for marker in ("暂无", "无结果", "no result")):
                    return IndexerPage(
                        items=[],
                        page=request.page,
                        has_more=False,
                        pagination_supported=True,
                    )
                raise IndexerInvalidResponse("BTBtla search page structure is invalid")
        except IndexerInvalidResponse as exc:
            raise IndexerUnavailable("BTBtla search endpoint returned an invalid page") from exc

        ranked_candidates = sorted(
            candidates,
            key=lambda candidate: self._search_candidate_score(request.query, candidate[0]),
            reverse=True,
        )[:detail_limit]
        items: list[IndexerItem] = []
        seen_urls: set[str] = set()
        detail_error: IndexerInvalidResponse | IndexerResultExpired | None = None
        for _, detail_url, category in ranked_candidates:
            try:
                detail = await self._get(detail_url)
                self._validate_response("detail", detail)
                require_html_response(detail)
                detail_base_url = self._base_for_url(detail.url)
                detail_soup = BeautifulSoup(detail.body, "lxml")
                detail_items = self._parse_resource_items(
                    detail_soup,
                    category=category,
                    base_url=detail_base_url,
                )
                if (
                    not detail_items
                    and detail_soup.select_one("#download-list") is None
                    and not detail_soup.select(".module-row-info")
                ):
                    raise IndexerInvalidResponse("BTBtla detail page omitted the resource list")
            except (IndexerInvalidResponse, IndexerResultExpired) as exc:
                detail_error = detail_error or exc
                continue
            for item in detail_items:
                if item.detail_url and item.detail_url in seen_urls:
                    continue
                if item.detail_url:
                    seen_urls.add(item.detail_url)
                items.append(item)
            if items:
                break
        if not items and detail_error is not None:
            raise detail_error
        return IndexerPage(
            items=items,
            page=request.page,
            has_more=has_more,
            pagination_supported=True,
        )

    async def resolve(self, stored_result: IndexerItem) -> ResolvedDownload:
        self._validate_stored_result(stored_result)
        detail_url = self._join_known_host(stored_result.detail_url or "")
        if urlsplit(detail_url).path.startswith("/tdown/"):
            download_url = detail_url
        else:
            detail = await self._get(detail_url)
            self._validate_response("detail", detail)
            require_html_response(detail)
            download_url = self._select_download_url(
                BeautifulSoup(detail.body, "lxml"),
                base_url=self._base_for_url(detail.url),
            )
            if not download_url:
                raise IndexerInvalidResponse("BTBtla detail omitted download link")

        download = await self._get(download_url)
        self._validate_response("download", download)
        require_html_response(download)
        magnet = self._extract_magnet(download.body, required=False)
        if magnet:
            return ResolvedDownload(kind="magnet", value=magnet)
        torrent_url = self._extract_torrent_url(
            download.body,
            base_url=self._base_for_url(download.url),
        )
        if torrent_url:
            return ResolvedDownload(kind="torrent", value=torrent_url)
        raise IndexerInvalidResponse("BTBtla download page omitted a valid magnet or torrent")

    def _parse_search_candidates(
        self,
        soup: BeautifulSoup,
        *,
        base_url: str,
    ) -> list[tuple[str, str, str | None]]:
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
                detail_url = self._join_known_host(
                    anchor.get("href") or "",
                    relative_base_url=base_url,
                )
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

    def _parse_resource_items(
        self,
        soup: BeautifulSoup,
        *,
        category: str | None,
        base_url: str,
    ) -> list[IndexerItem]:
        items: list[IndexerItem] = []
        seen_urls: set[str] = set()
        download_list = soup.select_one("#download-list")
        scope = download_list if download_list is not None else soup
        modern_rows = scope.select(".module-row-info")
        legacy_rows = scope.select(".module-row-one")
        # 线上页面会同时保留少量旧 Tab 容器和全部现代资源行。旧实现用
        # ``old_rows or modern_rows``，只要旧容器存在就会丢掉绝大多数资源。
        # 现代行优先，再兼容旧容器。旧容器可能把 module-row-info 仅作为
        # 元数据块、把真正的下载链接放在外层；两类节点都扫描并由 URL 去重。
        rows = [*modern_rows, *legacy_rows]
        for row in rows:
            resource_anchor = None
            resource_url = ""
            anchors = row.select("a.module-row-text[href]") or row.find_all("a", href=True)
            for anchor in anchors:
                try:
                    candidate = self._join_known_host(
                        anchor.get("href") or "",
                        relative_base_url=base_url,
                    )
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
            download_counters = list(row.select("a.btn-down[href]"))
            if not download_counters:
                legacy_parent = row.find_parent(class_="module-row-one")
                if legacy_parent is not None:
                    download_counters = list(legacy_parent.select("a.btn-down[href]"))
            for download_counter in download_counters:
                try:
                    counter_url = self._join_known_host(
                        download_counter.get("href") or "",
                        relative_base_url=base_url,
                    )
                except IndexerSecurityError:
                    continue
                if counter_url != resource_url:
                    continue
                count_match = re.search(r"\d+", download_counter.get_text(" ", strip=True))
                if count_match:
                    downloads = int(count_match.group(0))
                break
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
                    download_kinds=("magnet", "torrent"),
                )
            )
        return items

    def _has_search_page_link(
        self,
        soup: BeautifulSoup,
        *,
        query: str,
        page: int,
        base_url: str,
    ) -> bool:
        expected_url = fixed_host_join(
            base_url,
            f"/search/{quote(query, safe='')}/{page}",
        )
        expected_path = unquote(urlsplit(expected_url).path).rstrip("/")
        for anchor in soup.select("a[href]"):
            try:
                candidate = self._join_known_host(
                    anchor.get("href") or "",
                    relative_base_url=base_url,
                )
            except IndexerSecurityError:
                continue
            if unquote(urlsplit(candidate).path).rstrip("/") == expected_path:
                return True
        return False

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
            if self._last_request_started is not None:
                remaining = self.min_interval_seconds - (now - self._last_request_started)
                if remaining > 0:
                    await self._sleep(remaining)
            self._last_request_started = self._monotonic()
            return await self.http.get(url)

    def _select_download_url(self, soup: BeautifulSoup, *, base_url: str) -> str:
        candidates: list[str] = []
        for anchor in soup.select("a.btn-down[href]"):
            try:
                candidate = self._join_known_host(
                    anchor.get("href") or "",
                    relative_base_url=base_url,
                )
            except IndexerSecurityError:
                continue
            candidates.append(candidate)
        candidates.sort(key=lambda value: 0 if urlsplit(value).path.startswith("/tdown/") else 1)
        return candidates[0] if candidates else ""

    def _validate_stored_result(self, stored_result: IndexerItem) -> None:
        if stored_result.site_id != self.site_id:
            raise IndexerSecurityError("result provider mismatch")
        if stored_result.download_state != "resolvable" or not set(
            stored_result.download_kinds
        ).intersection({"magnet", "torrent"}):
            raise IndexerInvalidResponse("result is not resolvable as magnet or torrent")
        if not stored_result.detail_url:
            raise IndexerInvalidResponse("result detail URL is missing")

    def _join_known_host(self, candidate: str, *, relative_base_url: str | None = None) -> str:
        bases = self._host_bases
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

    def _base_for_url(self, candidate: str) -> str:
        safe_url = self._join_known_host(candidate)
        host = urlsplit(safe_url).hostname
        for base_url in self._host_bases:
            if urlsplit(base_url).hostname == host:
                return base_url
        raise IndexerSecurityError("provider result escaped its registered host")

    @staticmethod
    def _extract_magnet(body: bytes, *, required: bool = True) -> str:
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
        if required:
            raise IndexerInvalidResponse("BTBtla download page omitted a valid magnet")
        return ""

    def _extract_torrent_url(self, body: bytes, *, base_url: str) -> str:
        soup = BeautifulSoup(body, "lxml")
        for anchor in soup.select("a[href]"):
            try:
                candidate = self._join_known_host(
                    anchor.get("href") or "",
                    relative_base_url=base_url,
                )
            except IndexerSecurityError:
                continue
            path = urlsplit(candidate).path.lower()
            if path.startswith("/dlt/") or path.endswith(".torrent"):
                return candidate
        return ""

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
