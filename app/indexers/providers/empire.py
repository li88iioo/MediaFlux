from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from urllib.parse import parse_qs, quote_from_bytes, urlsplit

from bs4 import BeautifulSoup

from app.concurrency import CrossLoopAsyncLock

from ..errors import (
    IndexerError,
    IndexerChallengeRequired,
    IndexerQueryRejected,
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
from ..query_plan import encode_empire_keyword
from .base import (
    DirectResultAdapter,
    is_likely_challenge_page,
    magnet_infohash,
    require_html_response,
)

_SEARCH_INTERVAL_SECONDS = 5.5
_SEARCH_TIMEOUT_SECONDS = 8.0
_DETAIL_TIMEOUT_SECONDS = 6.0
_DETAIL_CONCURRENCY = 3
_DETAIL_CANDIDATE_LIMIT = 6
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
        self._search_lock = CrossLoopAsyncLock()
        self._search_finished: float | None = None

    def search_timeout_overhead_seconds(self) -> float:
        return _SEARCH_INTERVAL_SECONDS

    async def search(self, request: IndexerSearchRequest) -> IndexerPage:
        if request.page > 1:
            return IndexerPage(
                items=[], page=request.page, has_more=False, pagination_supported=False,
            )

        keyword = quote_from_bytes(encode_empire_keyword(request.query), safe="")

        loop = asyncio.get_running_loop()
        # 按表单响应完成时间留出间隔，避免首次DNS/TLS耗时吞掉服务器要求的5秒。
        # 只串行搜索POST；详情页仍沿用有界并发，不阻塞其它站点。
        async with self._search_lock:
            if self._search_finished is not None:
                delay = _SEARCH_INTERVAL_SECONDS - (monotonic() - self._search_finished)
                if delay > 0:
                    await asyncio.sleep(delay)
            deadline = loop.time() + _SEARCH_TIMEOUT_SECONDS
            try:
                response = await asyncio.wait_for(
                    self.http.post_form(
                        self._join_known_host("/e/search/index.php"),
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
            finally:
                self._search_finished = monotonic()

        _body, soup = self._read_page(response)
        result_url = self._join_known_host(response.url)
        results = self._parse_results(soup, result_url)
        if not results:
            return IndexerPage(items=[], page=1, has_more=False, pagination_supported=False)

        detail_deadline = min(deadline, loop.time() + _DETAIL_TIMEOUT_SECONDS)
        items: list[IndexerItem] = []
        errors: list[IndexerProviderError] = []
        seen_infohashes: set[str] = set()
        stop_detail_search = False

        for offset in range(0, len(results), _DETAIL_CONCURRENCY):
            batch = results[offset : offset + _DETAIL_CONCURRENCY]
            if loop.time() >= detail_deadline:
                errors.append(self._detail_error(IndexerTimeout("detail budget exhausted")))
                break
            tasks = [
                asyncio.create_task(self._fetch_detail(result, result_url))
                for result in batch
            ]
            done: set[asyncio.Task] = set()
            try:
                done, _ = await asyncio.wait(
                    tasks,
                    timeout=max(0.0, detail_deadline - loop.time()),
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

            batch_timed_out = len(done) < len(tasks)
            for result, task in zip(batch, tasks):
                if task not in done:
                    errors.append(self._detail_error(IndexerTimeout("detail timed out")))
                    continue
                found, error, hard_stop = task.result()
                if error is not None:
                    errors.append(error)
                stop_detail_search |= hard_stop
                for item in found:
                    infohash = magnet_infohash(item.magnet)
                    if infohash is None or infohash.lower() in seen_infohashes:
                        continue
                    seen_infohashes.add(infohash.lower())
                    items.append(item)

            if items or stop_detail_search or batch_timed_out:
                break

        return IndexerPage(
            items=items,
            page=1,
            has_more=False,
            pagination_supported=False,
            errors=tuple(errors),
        )

    def _form_body(self, keyword: str) -> bytes:
        submit = "Submit" if self.site_id == "dygang" else "submit"
        return f"tempid=1&tbname=article&keyboard={keyword}&show=title%2Csmalltext&{submit}=".encode("ascii")

    def _read_page(self, response) -> tuple[str, BeautifulSoup]:
        body = response.body.decode("gbk", errors="replace")
        if response.status_code != 429 and (
            is_likely_challenge_page(body)
            or str(response.headers.get("cf-mitigated") or "").lower() == "challenge"
        ):
            raise IndexerChallengeRequired("upstream returned a verification page")
        self._validate_status(response.status_code)
        require_html_response(response)
        soup = BeautifulSoup(body, "lxml")
        if soup.title and soup.title.get_text(strip=True) in {"信息提示", "系统提示"}:
            message = soup.get_text(" ", strip=True)
            if any(marker in message for marker in ("验证码", "人机验证", "安全验证")):
                raise IndexerChallengeRequired("upstream requires verification")
            if any(marker in message for marker in ("搜索间隔", "搜索时间间隔", "频繁搜索", "搜索太频繁", "操作过于频繁", "两次搜索")):
                raise IndexerRateLimited("upstream search frequency notice")
            if any(marker in message for marker in ("搜索关键字只能", "搜索关键词只能", "关键字太长", "关键词太长", "关键字太短", "关键词太短", "关键字长度", "关键词长度")):
                raise IndexerQueryRejected("upstream rejected the search term")
            if not any(marker in message.casefold() for marker in _EMPTY_MARKERS):
                raise IndexerUnavailable("upstream service notice")
        return body, soup

    def _parse_results(self, soup: BeautifulSoup, result_url: str) -> list[_SearchResult]:
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
            if len(results) == _DETAIL_CANDIDATE_LIMIT:
                break

        if results:
            return results
        page_text = soup.get_text(" ", strip=True).casefold()
        if any(marker.casefold() in page_text for marker in _EMPTY_MARKERS):
            return []
        raise IndexerInvalidResponse(f"{self.site_name} search page structure is unrecognized")

    async def _fetch_detail(
        self, result: _SearchResult, referer: str,
    ) -> tuple[list[IndexerItem], IndexerProviderError | None, bool]:
        try:
            response = await self.http.get(
                result.detail_url,
                headers={"Referer": referer},
            )
            body, soup = self._read_page(response)
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
                return [], None, False

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
            return items, None, False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return [], self._detail_error(exc), isinstance(
                exc, (IndexerRateLimited, IndexerSecurityError, IndexerChallengeRequired),
            )

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
            site_id=self.site_id,
            code=code,
            message=f"{self.site_name}：{exc.public_message if isinstance(exc, IndexerError) else '详情页获取失败'}",
        )

    def _validate_status(self, status_code: int) -> None:
        if status_code == 429:
            raise IndexerRateLimited(f"{self.site_name} returned HTTP 429")
        if status_code >= 500:
            raise IndexerUnavailable(f"{self.site_name} returned HTTP {status_code}")
        if status_code != 200:
            raise IndexerInvalidResponse(f"{self.site_name} returned HTTP {status_code}")
