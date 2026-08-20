from __future__ import annotations

from bs4 import BeautifulSoup

from ..errors import IndexerInvalidResponse, IndexerRateLimited, IndexerUnavailable
from ..models import IndexerCapabilities, IndexerItem, IndexerPage, IndexerSearchRequest
from .base import DirectResultAdapter, fixed_host_join, magnet_infohash, require_html_response


class MikanAdapter(DirectResultAdapter):
    site_id = "mikan"
    site_name = "Mikan"
    base_url = "https://mikanani.me/"
    default_enabled = True
    capabilities = IndexerCapabilities(pagination_supported=False, download_kinds=("magnet",))

    def __init__(self, *, http):
        self.http = http

    async def search(self, request: IndexerSearchRequest) -> IndexerPage:
        if request.page > 1:
            return IndexerPage(items=[], page=request.page, has_more=False, pagination_supported=False)
        response = await self.http.get(
            fixed_host_join(self.base_url, "/Home/Search"),
            params={"searchstr": request.query},
            headers={"Referer": self.base_url},
        )
        self._validate_status(response.status_code)
        require_html_response(response)
        soup = BeautifulSoup(response.body, "lxml")
        rows = soup.select("tr.js-search-results-row")
        text = soup.get_text(" ", strip=True).lower()
        if not rows and not soup.select_one("table") and not any(marker in text for marker in ("没有", "无结果", "no result")):
            raise IndexerInvalidResponse("Mikan search page structure is invalid")
        items: list[IndexerItem] = []
        for row in rows:
            magnet_node = row.select_one("input.js-episode-select[data-magnet]")
            title_anchor = row.find("a", href=True)
            if magnet_node is None or title_anchor is None:
                continue
            magnet = magnet_node.get("data-magnet")
            if magnet_infohash(magnet) is None:
                continue
            title = title_anchor.get_text(" ", strip=True)
            if not title:
                continue
            items.append(
                IndexerItem(
                    site_id=self.site_id,
                    site_name=self.site_name,
                    title=title,
                    detail_url=fixed_host_join(self.base_url, title_anchor.get("href")),
                    download_state="ready",
                    download_kinds=("magnet",),
                    magnet=magnet,
                )
            )
        return IndexerPage(items=items, page=1, has_more=False, pagination_supported=False)

    @staticmethod
    def _validate_status(status_code: int) -> None:
        if status_code == 429:
            raise IndexerRateLimited("Mikan returned HTTP 429")
        if status_code >= 500:
            raise IndexerUnavailable(f"Mikan returned HTTP {status_code}")
        if status_code != 200:
            raise IndexerInvalidResponse(f"Mikan returned HTTP {status_code}")
