from __future__ import annotations

import html
from datetime import datetime, timezone
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..errors import IndexerInvalidResponse, IndexerRateLimited, IndexerSecurityError, IndexerUnavailable
from ..models import IndexerCapabilities, IndexerItem, IndexerPage, IndexerSearchRequest, ResolvedDownload
from .base import DirectResultAdapter, fixed_host_join, magnet_infohash, parse_size_bytes, require_html_response


_MIKAN_DATE_FORMATS = (
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d",
    "%Y-%m-%d",
)


class MikanAdapter(DirectResultAdapter):
    site_id = "mikan"
    site_name = "Mikan"
    base_url = "https://mikanani.me/"
    mirror_base_urls = ("https://mikanime.tv/",)
    default_enabled = True
    capabilities = IndexerCapabilities(pagination_supported=False, download_kinds=("magnet", "torrent"))

    def __init__(self, *, http, mirror_base_urls: tuple[str, ...] | None = None):
        self.http = http
        self.mirror_base_urls = tuple(
            dict.fromkeys(self.mirror_base_urls if mirror_base_urls is None else mirror_base_urls)
        )
        self._host_bases = tuple(dict.fromkeys((self.base_url, *self.mirror_base_urls)))

    async def search(self, request: IndexerSearchRequest) -> IndexerPage:
        if request.page > 1:
            return IndexerPage(items=[], page=request.page, has_more=False, pagination_supported=False)
        last_error: IndexerInvalidResponse | IndexerRateLimited | IndexerUnavailable | None = None
        for base_url in self._host_bases:
            try:
                return await self._search_base(base_url, request)
            except (IndexerInvalidResponse, IndexerRateLimited, IndexerUnavailable) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def resolve(self, stored_result: IndexerItem) -> ResolvedDownload:
        if stored_result.site_id != self.site_id:
            raise IndexerSecurityError("result provider mismatch")
        if stored_result.magnet and magnet_infohash(stored_result.magnet):
            return ResolvedDownload(kind="magnet", value=stored_result.magnet)
        if stored_result.torrent_url:
            return ResolvedDownload(kind="torrent", value=self._join_known_host(stored_result.torrent_url))
        raise IndexerInvalidResponse("result has no downloadable candidate")

    async def _search_base(self, base_url: str, request: IndexerSearchRequest) -> IndexerPage:
        response = await self.http.get(
            fixed_host_join(base_url, "/Home/Search"),
            params={"searchstr": request.query},
            headers={"Referer": base_url},
        )
        self._validate_status(response.status_code)
        require_html_response(response)
        response_base_url = self._base_for_url(response.url)
        soup = BeautifulSoup(response.body, "lxml")
        rows = soup.select("tr.js-search-results-row")
        text = soup.get_text(" ", strip=True).lower()
        if (
            not rows
            and not soup.select_one("table")
            and not any(marker in text for marker in ("没有", "无结果", "no result"))
        ):
            raise IndexerInvalidResponse("Mikan search page structure is invalid")
        items: list[IndexerItem] = []
        for row in rows:
            item = self._parse_row(row, base_url=response_base_url)
            if item is not None:
                items.append(item)
        return IndexerPage(items=items, page=1, has_more=False, pagination_supported=False)

    def _parse_row(self, row, *, base_url: str) -> IndexerItem | None:
        title_anchor = row.select_one('a.magnet-link-wrap[href]') or row.select_one(
            'a[href*="/Home/Episode/"]'
        )
        if title_anchor is None:
            title_anchor = next(
                (
                    anchor
                    for anchor in row.find_all("a", href=True)
                    if not str(anchor.get("href") or "").lower().startswith("magnet:?")
                    and "/download/" not in str(anchor.get("href") or "").lower()
                ),
                None,
            )
        if title_anchor is None:
            return None
        title = title_anchor.get_text(" ", strip=True)
        if not title:
            return None

        magnet = None
        magnet_nodes = [
            row.select_one("[data-magnet]"),
            row.select_one("[data-clipboard-text]"),
            row.select_one('a[href^="magnet:?"]'),
        ]
        for node in magnet_nodes:
            if node is None:
                continue
            candidate = html.unescape(
                str(
                    node.get("data-magnet")
                    or node.get("data-clipboard-text")
                    or node.get("href")
                    or ""
                ).strip()
            )
            if magnet_infohash(candidate):
                magnet = candidate
                break

        torrent_url = None
        torrent_anchor = row.select_one('a[href*="/Download/"][href]')
        if torrent_anchor is not None:
            try:
                torrent_url = self._join_known_host(
                    torrent_anchor.get("href") or "",
                    relative_base_url=base_url,
                )
            except IndexerSecurityError:
                torrent_url = None

        kinds = tuple(kind for kind, value in (("magnet", magnet), ("torrent", torrent_url)) if value)
        if not kinds:
            return None
        size_text, size_bytes = self._extract_size(row)
        try:
            detail_url = self._join_known_host(
                title_anchor.get("href") or "",
                relative_base_url=base_url,
            )
        except IndexerSecurityError:
            return None
        return IndexerItem(
            site_id=self.site_id,
            site_name=self.site_name,
            title=title,
            detail_url=detail_url,
            size_text=size_text,
            size_bytes=size_bytes,
            published_at=self._extract_published_at(row),
            download_state="ready",
            download_kinds=kinds,
            magnet=magnet,
            torrent_url=torrent_url,
        )

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
    def _extract_size(row) -> tuple[str | None, int | None]:
        for cell in row.find_all("td"):
            value = cell.get_text(" ", strip=True)
            size_bytes = parse_size_bytes(value)
            if size_bytes is not None:
                return value, size_bytes
        return None, None

    @staticmethod
    def _extract_published_at(row) -> datetime | None:
        timestamp_node = row.select_one("[data-timestamp]")
        if timestamp_node is not None:
            try:
                return datetime.fromtimestamp(int(timestamp_node.get("data-timestamp") or ""), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError, OSError):
                pass
        for cell in row.find_all("td"):
            value = cell.get_text(" ", strip=True)
            for date_format in _MIKAN_DATE_FORMATS:
                try:
                    return datetime.strptime(value, date_format).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
        return None

    @staticmethod
    def _validate_status(status_code: int) -> None:
        if status_code == 429:
            raise IndexerRateLimited("Mikan returned HTTP 429")
        if status_code >= 500:
            raise IndexerUnavailable(f"Mikan returned HTTP {status_code}")
        if status_code != 200:
            raise IndexerInvalidResponse(f"Mikan returned HTTP {status_code}")
