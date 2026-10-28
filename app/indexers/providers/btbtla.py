from __future__ import annotations

import html
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from urllib.parse import quote, unquote, urlsplit
from weakref import WeakValueDictionary

from bs4 import BeautifulSoup

from app.clients.http import HttpError, HttpInvalidResponse, HttpSecurityError
from app.concurrency import CrossLoopAsyncLock

from ..config import BTBTLA_MIRROR_URL, BTBTLA_URL
from ..errors import IndexerRateLimited, IndexerResultExpired, IndexerUnavailable
from ..models import (
    IndexerCapabilities,
    IndexerItem,
    IndexerMediaSearchRequest,
    IndexerPage,
    IndexerProviderError,
    IndexerSearchRequest,
    ResolvedDownload,
)
from ..ranking import candidate_sort_key, rank_item
from .base import (
    IndexerAdapter,
    SearchRequestPacer,
    fixed_host_join,
    is_likely_challenge_page,
    magnet_infohash,
    parse_size_bytes,
    report_page,
    require_html_response,
)

_MAGNET_CANDIDATE = re.compile(r"magnet:\?[^\"'\s<>]+", re.IGNORECASE)
_RESOURCE_SIZE = re.compile(r"\[\s*([0-9]+(?:\.[0-9]+)?\s*[KMGTPE]?i?B)\s*\]", re.IGNORECASE)
_EXCLUDED_RESOURCE_TABS = frozenset({"720p", "480p", "other"})
_MAX_CATALOG_PAGES = 100  # 与公共搜索page上限一致；不缓存无法请求的页面。
_TRAILING_YEAR = re.compile(r"^(.+?)[ ._\-(（]*((?:18|19|20|21)\d{2}|2200)[)）]?(?:年)?$")
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


@dataclass(slots=True)
class _ResourcePages:
    expires_at: float
    pages: list[IndexerPage]
    next_native_page: int | None = 1
    truncated_count: int = 0



class BTBtlaAdapter(IndexerAdapter):
    site_id = "btbtla"
    site_name = "综合"
    base_url = BTBTLA_URL
    mirror_base_urls: tuple[str, ...] = (BTBTLA_MIRROR_URL,)
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
        page_size: int = 40,
        cache_ttl_seconds: float = 120,
        mirror_base_urls: tuple[str, ...] | None = None,
    ):
        self.http = http
        self.page_size = max(1, min(page_size, 100))
        self.cache_ttl_seconds = max(1.0, float(cache_ttl_seconds))
        self._cache_lock = threading.Lock()
        self._catalog_locks: WeakValueDictionary[IndexerSearchRequest, CrossLoopAsyncLock] = WeakValueDictionary()
        self._catalogs: OrderedDict[IndexerSearchRequest, _ResourcePages] = OrderedDict()
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._monotonic = monotonic or time.monotonic
        self._request_lock = CrossLoopAsyncLock()
        self._request_pacer = SearchRequestPacer(self.min_interval_seconds, monotonic=self._monotonic, sleeper=sleeper)
        self.max_detail_candidates = max(1, min(max_detail_candidates, 3))
        self.mirror_base_urls = tuple(
            dict.fromkeys(self.mirror_base_urls if mirror_base_urls is None else mirror_base_urls)
        )
        self._host_bases = tuple(dict.fromkeys((self.base_url, *self.mirror_base_urls)))

    def search_timeout_overhead_seconds(self) -> float:
        # 每个入口至多原词+去年份检索两次，再读取有界详情；等待不占网络预算。
        paced_gaps = (self.max_detail_candidates + 2) * len(self._host_bases) - 1
        return self.min_interval_seconds * paced_gaps

    async def search(self, request: IndexerSearchRequest) -> IndexerPage:
        # 站点一张影片详情含数百个版本。先排序再分页，不能让服务层40条窗口
        # 截断整部作品；原生影片搜索页只在当前页的所有资源窗口用完后读取。
        # 排序范围为已取回的影片搜索页；不会为“全站全局排序”后台遍历所有影片。
        key = replace(request, page=1)
        # 同键分页共享读取；不同作品不能拿着全站缓存锁等待网络。
        # 调用者持有强引用，进行中的锁不会随缓存淘汰；结束后弱引用自动释放。
        with self._cache_lock:
            lock = self._catalog_locks.setdefault(key, CrossLoopAsyncLock())
        async with lock:
            with self._cache_lock:
                now = self._monotonic()
                for old_key in list(self._catalogs):
                    if self._catalogs[old_key].expires_at <= now:
                        del self._catalogs[old_key]
                catalog = self._catalogs.get(key) or _ResourcePages(now + self.cache_ttl_seconds, [])
            fetches = 0
            while len(catalog.pages) < request.page and catalog.next_native_page is not None:
                if fetches == 2:
                    raise IndexerResultExpired("BTBtla resource page requires earlier pages", public_message="资源分页已过期，请重新搜索后继续加载")
                native = await self._search_native(
                    replace(request, page=catalog.next_native_page), progressive=request.page == 1,
                )
                ranked = self._rank_items(request, native.items)
                known = {item.detail_url for resource_page in catalog.pages for item in resource_page.items if item.detail_url}
                ranked = [item for item in ranked if not item.detail_url or item.detail_url not in known]
                fetches += 1
                room = (_MAX_CATALOG_PAGES - len(catalog.pages)) * self.page_size
                catalog.truncated_count += max(0, len(ranked) - room)
                ranked = ranked[:room]
                catalog.pages.extend(replace(native, items=ranked[offset:offset + self.page_size]) for offset in range(0, len(ranked), self.page_size))
                # 含错误的空页直接交付；仅无错误空页在两次读取预算内补取下一原生页。
                if not ranked and (native.errors or not native.has_more or fetches == 2):
                    catalog.pages.append(replace(native, items=[]))
                if native.errors:
                    catalog.expires_at = min(catalog.expires_at, self._monotonic() + 15)
                elif not any(resource_page.items for resource_page in catalog.pages):
                    catalog.expires_at = min(catalog.expires_at, self._monotonic() + 8)
                catalog.next_native_page = (
                    catalog.next_native_page + 1
                    if native.has_more and len(catalog.pages) < _MAX_CATALOG_PAGES else None
                )
                with self._cache_lock:
                    self._catalogs[key] = catalog
                    self._catalogs.move_to_end(key)
                    while len(self._catalogs) > 16:
                        self._catalogs.popitem(last=False)
            if request.page > len(catalog.pages):
                return IndexerPage([], request.page, False, True)
            resource_page = catalog.pages[request.page - 1]
            return replace(
                resource_page, items=[replace(item) for item in resource_page.items],
                page=request.page,
                has_more=request.page < len(catalog.pages) or catalog.next_native_page is not None,
                total_items=(len(resource_page.items) + catalog.truncated_count)
                if request.page == len(catalog.pages) and catalog.truncated_count else None,
            )

    @staticmethod
    def _rank_items(request: IndexerSearchRequest, items: list[IndexerItem]) -> list[IndexerItem]:
        media = IndexerMediaSearchRequest.create(
            title=request.query, year=request.year, media_type=request.media_type,
            season=request.season, episode=request.episode,
        )
        ranked = [rank_item(item, media=media, fallback_query=request.query) for item in items]
        return [entry[1] for entry in sorted(enumerate(ranked), key=lambda entry: candidate_sort_key((0, entry[0], entry[1]), request.sort_mode))]

    async def _search_native(self, request: IndexerSearchRequest, *, progressive: bool = False) -> IndexerPage:
        last_error: IndexerRateLimited | IndexerUnavailable | None = None
        for base_url in self._host_bases:
            try:
                return await self._search_base(base_url, request, progressive=progressive)
            except (IndexerRateLimited, IndexerUnavailable) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def _search_base(
        self,
        base_url: str,
        request: IndexerSearchRequest,
        *,
        progressive: bool = False,
    ) -> IndexerPage:
        # 原词优先：数字可能本就是片名（银翼杀手2049、1917）。仅原生明确空结果
        # 才拆出末尾年份；年份用于选详情而非继续拼进只索引片名的站点查询。
        queries = [(request.query, request.year)]
        if match := _TRAILING_YEAR.fullmatch(request.query):
            title = match[1].strip(" ._-(（")
            if title and not title.isdigit():
                queries.append((title, request.year or int(match[2])))
        for query, year in queries:
            search_path = f"/search/{quote(query, safe='')}"
            if request.page > 1:
                search_path = f"{search_path}/{request.page}"
            response = await self._get(fixed_host_join(base_url, search_path))
            try:
                self._validate_response("search", response)
                require_html_response(response)
                response_base_url = self._base_for_url(response.url)
                soup = BeautifulSoup(response.body, "lxml")
                candidates = self._parse_search_candidates(soup, base_url=response_base_url)
                has_more = self._has_search_page_link(
                    soup, query=query, page=request.page + 1, base_url=response_base_url,
                )
                if candidates:
                    break
                zero_summary = any(
                    node.get_text(strip=True) == "0"
                    and (heading := node.find_parent("h2")) is not None
                    and re.search(r"^搜索.*找到\s*0\s*部影视作品", heading.get_text(" ", strip=True))
                    for node in soup.select("h2 strong.mac_total")
                )
                known_empty = any(marker in soup.get_text(" ", strip=True).lower()
                                  for marker in ("暂无", "无结果", "no result"))
                if not (known_empty or zero_summary) or is_likely_challenge_page(response.body):
                    raise HttpInvalidResponse("BTBtla search page structure is invalid")
            except HttpInvalidResponse as exc:
                raise IndexerUnavailable("BTBtla search endpoint returned an invalid page") from exc

        if year is not None:
            exact = [candidate for candidate in candidates
                     if self._search_candidate_score(query, candidate[0])[0] == 3]
            candidates = exact or candidates  # 有同名作品时，不把其它含关键词的作品冒充指定年份版本。
        ranked_candidates = sorted(
            (candidate for candidate in candidates if year is None or candidate[3] in (None, year)),
            key=lambda candidate: (self._search_candidate_score(query, candidate[0])[0],
                                   candidate[3] == year if year is not None else False,
                                   self._search_candidate_score(query, candidate[0])[1]),
            reverse=True,
        )[:self.max_detail_candidates]
        items: dict[str | None, IndexerItem] = {}
        errors: list[HttpError] = []
        matched_tier = None
        for title, detail_url, category, candidate_year in ranked_candidates:
            tier = (self._search_candidate_score(query, title)[0], year is not None and candidate_year == year)
            if items and matched_tier is not None and tier < matched_tier:
                break  # 不为已精确命中的作品额外请求弱匹配；同名作品不能提前截断。
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
                    raise HttpInvalidResponse("BTBtla detail page omitted the resource list")
            except HttpSecurityError:
                raise
            except HttpError as exc:
                errors.append(exc)
                if not isinstance(exc, (HttpInvalidResponse, IndexerResultExpired)):
                    if not items:
                        raise
                    break
                continue
            for item in detail_items:
                items.setdefault(item.detail_url, item)
            matched_tier = tier
            if progressive and items:
                # 复用服务层的渐进通道与最终排序；只发首个资源窗口，后续页不能混入第一页。
                ranked = self._rank_items(request, list(items.values()))
                report_page(self.site_id, IndexerPage(
                    ranked[:self.page_size], 1, has_more or len(ranked) > self.page_size, True,
                    complete=False,
                    errors=tuple(IndexerProviderError(self.site_id, error.code, error.public_message) for error in errors),
                ))
        if not items and errors:
            raise errors[0]
        return IndexerPage(
            list(items.values()), request.page, has_more, True,
            errors=tuple(IndexerProviderError(self.site_id, error.code, error.public_message) for error in errors),
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
                raise HttpInvalidResponse("BTBtla detail omitted download link")

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
        raise HttpInvalidResponse("BTBtla download page omitted a valid magnet or torrent")

    def _parse_search_candidates(
        self,
        soup: BeautifulSoup,
        *,
        base_url: str,
    ) -> list[tuple[str, str, str | None, int | None]]:
        candidates: list[tuple[str, str, str | None, int | None]] = []
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
                    str(anchor.get("href") or ""),
                    relative_base_url=base_url,
                )
            except HttpSecurityError:
                continue
            category = self._parse_search_category(node)
            caption = node.select_one("div.module-item-caption span")
            caption_text = caption.get_text(strip=True) if caption is not None else ""
            year = int(caption_text) if re.fullmatch(r"(?:18|19|20|21)\d{2}|2200", caption_text) else None
            if not any(candidate[1] == detail_url for candidate in candidates):
                candidates.append((title, detail_url, category, year))
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
        tabs = scope.select(".downtab-item [data-dropdown-value]")
        if len(tabs) == 0:
            tabs = soup.select(".downtab-item [data-dropdown-value]")
        panels = scope.select(".module-downlist")
        excluded = {
            id(panel) for tab, panel in zip(tabs, panels)
            if str(tab.get("data-dropdown-value") or tab.get_text(strip=True)).strip().casefold() in _EXCLUDED_RESOURCE_TABS
        } if len(tabs) == len(panels) else set()
        modern_rows = scope.select(".module-row-info")
        legacy_rows = scope.select(".module-row-one")
        # 线上页面会同时保留少量旧 Tab 容器和全部现代资源行。旧实现用
        # ``old_rows or modern_rows``，只要旧容器存在就会丢掉绝大多数资源。
        # 现代行优先，再兼容旧容器。旧容器可能把 module-row-info 仅作为
        # 元数据块、把真正的下载链接放在外层；两类节点都扫描并由 URL 去重。
        rows = [*modern_rows, *legacy_rows]
        for row in rows:
            if any(id(parent) in excluded for parent in row.parents):
                continue
            resource_anchor = None
            resource_url = ""
            anchors = row.select("a.module-row-text[href]")
            if len(anchors) == 0:
                anchors = row.find_all("a", href=True)
            for anchor in anchors:
                try:
                    candidate = self._join_known_host(
                        str(anchor.get("href") or ""),
                        relative_base_url=base_url,
                    )
                except HttpSecurityError:
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
            size_match = _RESOURCE_SIZE.search(visible_title)
            size_text = size_match.group(1).strip() if size_match else None
            title = _RESOURCE_SIZE.sub("", visible_title, count=1).strip()
            if not title:
                title = str(resource_anchor.get("title") or "").strip()
                title = re.sub(r"\.torrent$", "", title, flags=re.IGNORECASE).strip()
            if not title:
                continue
            # 源站title属性给出明确作品名，可补回纯英文发行标题的匹配证据。
            if canonical := re.match(r"《([^》]+)》", str(resource_anchor.get("title") or "")):
                if canonical[1] not in title:
                    title = f"{canonical[1]} / {title}"

            downloads = None
            download_counters = list(row.select("a.btn-down[href]"))
            if not download_counters:
                legacy_parent = row.find_parent(class_="module-row-one")
                if legacy_parent is not None:
                    download_counters = list(legacy_parent.select("a.btn-down[href]"))
            for download_counter in download_counters:
                try:
                    counter_url = self._join_known_host(
                        str(download_counter.get("href") or ""),
                        relative_base_url=base_url,
                    )
                except HttpSecurityError:
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
                    str(anchor.get("href") or ""),
                    relative_base_url=base_url,
                )
            except HttpSecurityError:
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
        # 下游浏览器传输也会排队；串行覆盖发送，防止已放行请求积压后突发。
        async with self._request_lock:
            await self._request_pacer.wait()
            return await self.http.get(url)

    def _select_download_url(self, soup: BeautifulSoup, *, base_url: str) -> str:
        candidates: list[str] = []
        for anchor in soup.select("a.btn-down[href]"):
            try:
                candidate = self._join_known_host(
                    str(anchor.get("href") or ""),
                    relative_base_url=base_url,
                )
            except HttpSecurityError:
                continue
            candidates.append(candidate)
        candidates.sort(key=lambda value: 0 if urlsplit(value).path.startswith("/tdown/") else 1)
        return candidates[0] if candidates else ""

    def _validate_stored_result(self, stored_result: IndexerItem) -> None:
        if stored_result.site_id != self.site_id:
            raise HttpSecurityError("result provider mismatch")
        if stored_result.download_state != "resolvable" or not set(
            stored_result.download_kinds
        ).intersection({"magnet", "torrent"}):
            raise HttpInvalidResponse("result is not resolvable as magnet or torrent")
        if not stored_result.detail_url:
            raise HttpInvalidResponse("result detail URL is missing")


    def _base_for_url(self, candidate: str) -> str:
        safe_url = self._join_known_host(candidate)
        host = urlsplit(safe_url).hostname
        for base_url in self._host_bases:
            if urlsplit(base_url).hostname == host:
                return base_url
        raise HttpSecurityError("provider result escaped its registered host")

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
            raise HttpInvalidResponse("BTBtla download page omitted a valid magnet")
        return ""

    def _extract_torrent_url(self, body: bytes, *, base_url: str) -> str:
        soup = BeautifulSoup(body, "lxml")
        for anchor in soup.select("a[href]"):
            try:
                candidate = self._join_known_host(
                    str(anchor.get("href") or ""),
                    relative_base_url=base_url,
                )
            except HttpSecurityError:
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
            raise HttpInvalidResponse(f"BTBtla returned HTTP {status_code}")
