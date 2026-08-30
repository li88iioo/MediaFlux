from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
import httpx

from ..errors import (
    IndexerError,
    IndexerInvalidResponse,
    IndexerRateLimited,
    IndexerSecurityError,
    IndexerTimeout,
    IndexerUnavailable,
)
from ..models import IndexerCapabilities, IndexerItem, IndexerPage, IndexerSearchRequest, ResolvedDownload
from .base import DirectResultAdapter, fixed_host_join, magnet_infohash, parse_size_bytes, require_html_response


_DETAIL_PATH = re.compile(r"^/view/\d+$")
_SIZE_TEXT = re.compile(r"^[0-9]+(?:\.[0-9]+)?\s*[KMGTPE]?i?B$", re.IGNORECASE)


class NyaaAdapter(DirectResultAdapter):
    capabilities = IndexerCapabilities(pagination_supported=True, download_kinds=("magnet", "torrent"))

    def __init__(
        self,
        *,
        site_id: str,
        site_name: str,
        base_url: str,
        http,
        default_enabled: bool,
        mirror_base_urls: tuple[str, ...] = (),
        endpoint_timeout_seconds: float | None = None,
    ):
        self.site_id = site_id
        self.site_name = site_name
        self.base_url = base_url
        self.http = http
        self.default_enabled = bool(default_enabled)
        # 主站可能按来源 IP 限流（nyaa.si 常见）；镜像与主站同引擎、
        # 布局仅有类名差异，主站限流/不可用时逐个回落。
        self.mirror_base_urls = tuple(mirror_base_urls)
        self._base_urls = tuple(dict.fromkeys((self.base_url, *self.mirror_base_urls)))
        self.endpoint_timeout_seconds = (
            max(0.1, float(endpoint_timeout_seconds))
            if endpoint_timeout_seconds is not None and len(self._base_urls) > 1
            else None
        )
        # 最近成功的端点优先，避免主站已 429/卡顿时每次搜索仍先重复施压。
        self._preferred_base_url = self.base_url

    async def search(self, request: IndexerSearchRequest) -> IndexerPage:
        last_error: IndexerError | None = None
        for base_url in self._ordered_base_urls():
            try:
                operation = self._search_base(base_url, request)
                page = (
                    await asyncio.wait_for(operation, timeout=self.endpoint_timeout_seconds)
                    if self.endpoint_timeout_seconds is not None
                    else await operation
                )
            except asyncio.TimeoutError:
                last_error = IndexerTimeout(f"Nyaa endpoint timed out: {base_url}")
                continue
            except httpx.TimeoutException:
                last_error = IndexerTimeout(f"Nyaa endpoint timed out: {base_url}")
                continue
            except (
                IndexerInvalidResponse,
                IndexerRateLimited,
                IndexerSecurityError,
                IndexerUnavailable,
            ) as exc:
                last_error = exc
                continue
            self._preferred_base_url = base_url
            return page
        if last_error is None:
            raise IndexerUnavailable("Nyaa has no configured endpoint")
        raise last_error

    def _ordered_base_urls(self) -> tuple[str, ...]:
        preferred = self._preferred_base_url
        if preferred not in self._base_urls:
            return self._base_urls
        return (preferred, *(base_url for base_url in self._base_urls if base_url != preferred))

    async def resolve(self, stored_result: IndexerItem) -> ResolvedDownload:
        if stored_result.site_id != self.site_id:
            raise IndexerSecurityError("result provider mismatch")
        if stored_result.magnet and magnet_infohash(stored_result.magnet):
            return ResolvedDownload(kind="magnet", value=stored_result.magnet)
        if stored_result.torrent_url:
            # 镜像返回的 torrent 直链落在镜像域名上，宿主校验覆盖全部注册域名。
            last_error: IndexerSecurityError | None = None
            for base_url in self._base_urls:
                try:
                    safe_url = fixed_host_join(base_url, stored_result.torrent_url)
                except IndexerSecurityError as exc:
                    last_error = exc
                    continue
                return ResolvedDownload(kind="torrent", value=safe_url)
            assert last_error is not None
            raise last_error
        raise IndexerInvalidResponse("result has no downloadable candidate")

    async def _search_base(self, base_url: str, request: IndexerSearchRequest) -> IndexerPage:
        last_page: IndexerPage | None = None
        for category in self._search_categories(request):
            page = await self._search_category(base_url, request, category=category)
            if page.items:
                return page
            last_page = page
        assert last_page is not None
        return last_page

    def _search_categories(self, request: IndexerSearchRequest) -> tuple[str, ...]:
        if self.site_id == "sukebei":
            return ("2_2", "0_0")
        media_type = str(getattr(request, "media_type", "") or "").strip().lower()
        if media_type in {"anime", "tv"}:
            return ("1_0", "0_0")
        if media_type == "movie":
            return ("4_0", "0_0")
        return ("0_0",)

    async def _search_category(
        self,
        base_url: str,
        request: IndexerSearchRequest,
        *,
        category: str,
    ) -> IndexerPage:
        response = await self.http.get(
            base_url,
            params={
                "f": "0",
                "c": category,
                "q": request.query,
                **_search_sort_params(getattr(request, "sort_mode", "relevance_desc")),
                "p": str(request.page),
            },
        )
        self._validate_status(response.status_code)
        require_html_response(response)
        soup = BeautifulSoup(response.body, "lxml")
        rows = self._select_rows(soup)
        if not rows and "no results found" not in soup.get_text(" ", strip=True).lower():
            if soup.select_one("table.torrent-list") is None:
                raise IndexerInvalidResponse("Nyaa search page structure is invalid")
        items: list[IndexerItem] = []
        for row in rows:
            try:
                item = self._parse_row(row, base_url)
            except (IndexerSecurityError, ValueError):
                item = None
            if item is not None:
                items.append(item)
        has_more = any(
            _search_page_number(anchor.get("href"), base_url=base_url) > request.page
            for anchor in soup.select(".pagination a[href]")
        )
        return IndexerPage(
            items=items,
            page=request.page,
            has_more=has_more,
            pagination_supported=True,
        )

    @staticmethod
    def _select_rows(soup: BeautifulSoup) -> list:
        rows = soup.select("table.torrent-list tbody tr")
        if rows:
            return rows
        # 镜像（nyaa.net 等）可能不带 torrent-list 类名，按行内 /view/ 链接识别。
        return [
            row
            for row in soup.select("table tbody tr")
            if any(_is_detail_href(anchor.get("href")) for anchor in row.find_all("a", href=True))
        ]

    def _parse_row(self, row, base_url: str) -> IndexerItem | None:
        cells = row.find_all("td", recursive=False)
        if len(cells) < 4:
            return None
        title_anchor = next(
            (
                anchor
                for anchor in row.find_all("a", href=True)
                if _is_detail_href(anchor.get("href"))
            ),
            None,
        )
        if title_anchor is None:
            return None
        title = str(title_anchor.get("title") or title_anchor.get_text(" ", strip=True)).strip()
        if not title:
            return None
        detail_url = fixed_host_join(base_url, title_anchor.get("href"))
        category_anchor = cells[0].find("a")
        category = None
        if category_anchor is not None:
            category = category_anchor.get("title") or category_anchor.get_text(" ", strip=True) or None
        magnet_anchor = row.find("a", href=lambda href: isinstance(href, str) and href.startswith("magnet:"))
        torrent_anchor = row.find(
            "a", href=lambda href: isinstance(href, str) and href.lower().endswith(".torrent")
        )
        magnet = magnet_anchor.get("href") if magnet_anchor is not None else None
        if magnet and magnet_infohash(magnet) is None:
            magnet = None
        torrent_url = None
        if torrent_anchor is not None:
            try:
                torrent_url = fixed_host_join(base_url, torrent_anchor.get("href"))
            except IndexerSecurityError:
                torrent_url = None
        size_text = self._select_size_text(cells)
        published_at = self._select_published_at(row)
        seeders, leechers, downloads = self._select_counters(row, cells)
        kinds = tuple(kind for kind, present in (("magnet", magnet), ("torrent", torrent_url)) if present)
        return IndexerItem(
            site_id=self.site_id,
            site_name=self.site_name,
            title=title,
            detail_url=detail_url,
            category=category,
            size_text=size_text,
            size_bytes=parse_size_bytes(size_text),
            seeders=seeders,
            leechers=leechers,
            downloads=downloads,
            published_at=published_at,
            download_state="ready" if kinds else "unavailable",
            download_kinds=kinds,
            magnet=magnet,
            torrent_url=torrent_url,
        )

    @staticmethod
    def _select_size_text(cells) -> str | None:
        for cell in cells:
            text = cell.get_text(" ", strip=True)
            if text and _SIZE_TEXT.fullmatch(text):
                return text
        return None

    @staticmethod
    def _select_published_at(row) -> datetime | None:
        stamp_cell = row.find("td", attrs={"data-timestamp": True})
        if stamp_cell is not None:
            try:
                return datetime.fromtimestamp(int(stamp_cell["data-timestamp"]), tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                return None
        date_cell = row.find("td", class_="col-date")
        if date_cell is not None:
            raw = str(date_cell.get("title") or date_cell.get_text(" ", strip=True)).strip()
            raw = raw.replace("+00:00", "Z").replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        return None

    @staticmethod
    def _select_counters(row, cells) -> tuple[int | None, int | None, int | None]:
        by_class = tuple(
            _integer(cell.get_text(strip=True)) if cell is not None else None
            for cell in (
                row.find("td", class_=re.compile(r"\bnum-s\b")),
                row.find("td", class_=re.compile(r"\bnum-l\b")),
                row.find("td", class_=re.compile(r"\bnum-c\b")),
            )
        )
        if any(value is not None for value in by_class):
            return by_class
        if len(cells) >= 8:
            return (
                _integer(cells[5].get_text(strip=True)),
                _integer(cells[6].get_text(strip=True)),
                _integer(cells[7].get_text(strip=True)),
            )
        # 布局未知时取行尾连续的纯数字单元格（seeders/leechers/downloads）。
        trailing = [
            _integer(cell.get_text(strip=True))
            for cell in cells[-3:]
            if _integer(cell.get_text(strip=True)) is not None
        ]
        if len(trailing) == 3:
            return (trailing[0], trailing[1], trailing[2])
        return (None, None, None)

    @staticmethod
    def _validate_status(status_code: int) -> None:
        if status_code == 429:
            raise IndexerRateLimited("Nyaa returned HTTP 429")
        if status_code >= 500:
            raise IndexerUnavailable(f"Nyaa returned HTTP {status_code}")
        if status_code != 200:
            raise IndexerInvalidResponse(f"Nyaa returned HTTP {status_code}")


def _is_detail_href(value: object) -> bool:
    parsed = urlsplit(str(value or "").strip())
    return not parsed.fragment and not parsed.query and bool(_DETAIL_PATH.fullmatch(parsed.path))


def _integer(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _search_sort_params(sort_mode: str) -> dict[str, str]:
    mappings = {
        "published_desc": ("id", "desc"),
        "episode_desc": ("id", "desc"),
        "seeders_desc": ("seeders", "desc"),
        "size_desc": ("size", "desc"),
        "size_asc": ("size", "asc"),
    }
    field, order = mappings.get(str(sort_mode or ""), ("seeders", "desc"))
    return {"s": field, "o": order}


def _page_number(href: str | None) -> int:
    if not href:
        return 0
    try:
        value = urlsplit(href).query
        for pair in value.split("&"):
            name, separator, raw = pair.partition("=")
            if separator and name == "p":
                return int(raw)
    except (TypeError, ValueError):
        pass
    return 0


def _search_page_number(href: str | None, *, base_url: str) -> int:
    try:
        candidate = fixed_host_join(base_url, str(href or ""))
    except IndexerSecurityError:
        return 0
    base_path = urlsplit(base_url).path.rstrip("/")
    if urlsplit(candidate).path.rstrip("/") != base_path:
        return 0
    return _page_number(candidate)
