from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote_from_bytes, urlsplit

from bs4 import BeautifulSoup

from ..errors import (
    IndexerError,
    IndexerInvalidResponse,
    IndexerRateLimited,
    IndexerSecurityError,
    IndexerTimeout,
    IndexerUnavailable,
)
from ..models import (
    IndexerCapabilities,
    IndexerItem,
    IndexerPage,
    IndexerProviderError,
    IndexerSearchRequest,
)
from .base import (
    DirectResultAdapter,
    is_likely_challenge_page,
    magnet_infohash,
    require_html_response,
)

_SEARCH_TIMEOUT_SECONDS = 8.0
_DETAIL_TIMEOUT_SECONDS = 6.0
_DETAIL_LIMIT = 3
_MAGNET_CANDIDATE = re.compile(r"magnet:\?[^\s\"'<>]+", re.IGNORECASE)
_DATE = re.compile(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}")
_DYGANG_DETAIL_PATH = re.compile(r"^/[a-z0-9]+/\d{4,8}/[a-z0-9_-]+\.html?$", re.IGNORECASE)
_EMPTY_MARKERS = (
    "没有找到",
    "没有搜索到",
    "暂无相关",
    "无相关影片",
    "无相关结果",
    "没有相关",
    "no results",
    "no result",
)


@dataclass(frozen=True, slots=True)
class _SearchResult:
    title: str
    detail_url: str


class EmpireAdapter(DirectResultAdapter):
    """帝国 CMS 搜索表单适配器，用于电影港与 5266 影视。"""

    default_enabled = False
    capabilities = IndexerCapabilities(False, ("magnet",))

    def __init__(self, *, site_id: str, site_name: str, base_url: str, http) -> None:
        if site_id not in {"dygang", "ys5266"}:
            raise ValueError("EmpireAdapter only supports dygang and ys5266")
        self.site_id = site_id
        self.site_name = site_name
        self.base_url = base_url.rstrip("/") + "/"
        self.http = http

    async def search(self, request: IndexerSearchRequest) -> IndexerPage:
        if request.page > 1:
            return IndexerPage(
                items=[], page=request.page, has_more=False, pagination_supported=False,
            )

        try:
            keyword = quote_from_bytes(request.query.encode("gbk"), safe="")
        except UnicodeEncodeError as exc:
            raise IndexerInvalidResponse("search term cannot be encoded as GBK") from exc

        response_url = self._join_known_host("/e/search/index.php")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _SEARCH_TIMEOUT_SECONDS
        try:
            response = await asyncio.wait_for(
                self.http.post_form(
                    response_url,
                    content=self._form_body(keyword),
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Referer": self.base_url,
                    },
                ),
                timeout=_SEARCH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise IndexerTimeout(f"{self.site_name} search timed out") from exc

        self._validate_status(response.status_code)
        require_html_response(response)
        result_url = self._join_known_host(response.url)
        result_body = response.body.decode("gbk", errors="replace")
        if is_likely_challenge_page(result_body):
            raise IndexerInvalidResponse(f"{self.site_name} search returned a challenge page")
        results = self._parse_results(result_body, result_url)
        if not results:
            return IndexerPage(items=[], page=1, has_more=False, pagination_supported=False)

        tasks = [
            asyncio.create_task(self._fetch_detail(result, result_url))
            for result in results[:_DETAIL_LIMIT]
        ]
        done: set[asyncio.Task] = set()
        try:
            done, _ = await asyncio.wait(
                tasks,
                timeout=min(
                    _DETAIL_TIMEOUT_SECONDS,
                    max(0.0, deadline - loop.time()),
                ),
            )
        finally:
            unfinished = [task for task in tasks if not task.done()]
            for task in unfinished:
                task.cancel()
            if unfinished:
                cleanup = asyncio.gather(*unfinished, return_exceptions=True)
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise

        items: list[IndexerItem] = []
        errors: list[IndexerProviderError] = []
        seen_infohashes: set[str] = set()
        for result, task in zip(results[:_DETAIL_LIMIT], tasks):
            if task not in done:
                errors.append(self._detail_error(IndexerTimeout("detail timed out")))
                continue
            found, error = task.result()
            if error is not None:
                errors.append(error)
            for item in found:
                infohash = magnet_infohash(item.magnet)
                if infohash is None or infohash.lower() in seen_infohashes:
                    continue
                seen_infohashes.add(infohash.lower())
                items.append(item)

        return IndexerPage(
            items=items,
            page=1,
            has_more=False,
            pagination_supported=False,
            errors=tuple(errors),
        )

    def _form_body(self, keyword: str) -> bytes:
        if self.site_id == "dygang":
            fields = f"tempid=1&tbname=article&keyboard={keyword}&show=title%2Csmalltext&Submit="
        else:
            fields = f"show=title%2Csmalltext&tempid=1&tbname=article&keyboard={keyword}&submit="
        return fields.encode("ascii")

    def _parse_results(self, body: str, result_url: str) -> list[_SearchResult]:
        soup = BeautifulSoup(body, "lxml")
        if self.site_id == "dygang":
            anchors = soup.select("a.classlinkclass[href]")
        else:
            anchors = soup.select("h3 a[href]")

        results: list[_SearchResult] = []
        seen_urls: set[str] = set()
        for anchor in anchors:
            title = anchor.get_text(" ", strip=True)
            if not title:
                continue
            try:
                detail_url = self._join_known_host(
                    anchor.get("href", ""), relative_base_url=result_url,
                )
            except IndexerSecurityError:
                continue
            if self.site_id == "dygang" and not _DYGANG_DETAIL_PATH.fullmatch(
                urlsplit(detail_url).path
            ):
                continue
            if detail_url in seen_urls:
                continue
            seen_urls.add(detail_url)
            results.append(_SearchResult(title=title, detail_url=detail_url))
            if len(results) == _DETAIL_LIMIT:
                break

        if results:
            return results
        page_text = soup.get_text(" ", strip=True).casefold()
        if any(marker.casefold() in page_text for marker in _EMPTY_MARKERS):
            return []
        raise IndexerInvalidResponse(f"{self.site_name} search page structure is unrecognized")

    async def _fetch_detail(
        self, result: _SearchResult, referer: str,
    ) -> tuple[list[IndexerItem], IndexerProviderError | None]:
        try:
            response = await self.http.get(
                result.detail_url,
                headers={"Referer": referer},
            )
            self._validate_status(response.status_code)
            require_html_response(response)
            body = response.body.decode("gbk", errors="replace")
            if is_likely_challenge_page(body):
                raise IndexerInvalidResponse("detail page is a challenge page")
            soup = BeautifulSoup(body, "lxml")
            if not body.strip() or not (
                soup.get_text(" ", strip=True)
                or soup.select_one("a[href], script, [data-magnet], [data-clipboard-text]")
            ):
                raise IndexerInvalidResponse("detail page structure is empty")
            candidates = self._extract_magnets(body, soup)
            valid = [
                (magnet, link_text)
                for magnet, link_text in candidates
                if magnet_infohash(magnet)
            ]
            if not valid:
                # ed2k、网盘及纯介绍页不是解析失败，只是不提供本适配器支持的磁力下载。
                return [], None

            published_at = self._extract_date(body)
            items = [
                IndexerItem(
                    site_id=self.site_id,
                    site_name=self.site_name,
                    title=self._resource_title(result.title, magnet, link_text),
                    detail_url=result.detail_url,
                    published_at=published_at,
                    download_state="ready",
                    download_kinds=("magnet",),
                    magnet=magnet,
                )
                for magnet, link_text in valid
            ]
            return items, None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return [], self._detail_error(exc)

    @staticmethod
    def _extract_magnets(body: str, soup: BeautifulSoup) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        seen: set[str] = set()

        def add(candidate: str, label: str = "") -> None:
            value = html.unescape(candidate).strip().rstrip(".,;:)]}")
            if not value.lower().startswith("magnet:?") or value in seen:
                return
            seen.add(value)
            found.append((value, label.strip()))

        for node in soup.select("a[href], [data-magnet], [data-clipboard-text]"):
            candidate = (
                node.get("data-magnet")
                or node.get("data-clipboard-text")
                or node.get("href")
                or ""
            )
            if str(candidate).strip().lower().startswith("magnet:?"):
                add(str(candidate), node.get_text(" ", strip=True))
        for match in _MAGNET_CANDIDATE.finditer(body):
            add(match.group(0))
        return found

    @staticmethod
    def _resource_title(site_title: str, magnet: str, link_text: str) -> str:
        dn = parse_qs(urlsplit(magnet).query).get("dn", [""])[0].strip()
        resource_title = dn or link_text.strip() or site_title
        if site_title.casefold() not in resource_title.casefold():
            return f"{resource_title} — {site_title}"
        return resource_title

    @staticmethod
    def _extract_date(body: str) -> datetime | None:
        match = _DATE.search(body)
        if match is None:
            return None
        for date_format in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(match.group(0), date_format).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    def _detail_error(self, exc: Exception) -> IndexerProviderError:
        code = exc.code if isinstance(exc, IndexerError) else "unavailable"
        return IndexerProviderError(
            site_id="btbtla",
            code=code,
            message=f"{self.site_name}详情页获取失败",
        )

    def _validate_status(self, status_code: int) -> None:
        if status_code == 429:
            raise IndexerRateLimited(f"{self.site_name} returned HTTP 429")
        if status_code >= 500:
            raise IndexerUnavailable(f"{self.site_name} returned HTTP {status_code}")
        if status_code != 200:
            raise IndexerInvalidResponse(f"{self.site_name} returned HTTP {status_code}")
