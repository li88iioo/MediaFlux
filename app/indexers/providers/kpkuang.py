from __future__ import annotations

import asyncio
import base64
import binascii
import html
import json
import re
import time
from types import MappingProxyType
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from app.concurrency import CrossLoopAsyncLock

from ..errors import (
    IndexerChallengeRequired,
    IndexerError,
    IndexerInvalidResponse,
    IndexerRateLimited,
    IndexerResponseTooLarge,
    IndexerSecurityError,
    IndexerTimeout,
    IndexerUnavailable,
)
from ..models import IndexerCapabilities, IndexerItem, IndexerMediaSearchRequest, IndexerPage, IndexerProviderError, IndexerSearchRequest
from ..ranking import candidate_sort_key, rank_item
from .base import DirectResultAdapter, fixed_host_join, is_likely_challenge_page, magnet_infohash, parse_size_bytes, report_page

# Base64 仅用于避免在源码中直写站点和接口地址，不提供保密性。
_CONFIG_B64 = (
    "eyJiYXNlX3VybCI6Imh0dHBzOi8vd3d3Lmtwa3Vhbmcub3JnLyIsImFwaV9zZWFyY2hfdXJsIjoiaHR0cHM6Ly9rcGRhdGEuZmxpeGZpZW5kLnRvcC9lc2VhcmNoL2luZGV4IiwiZGV0YWlsX3BhdGhfcHJlZml4IjoiL3ZvZGRldGFpbC8iLCJ2b2Rkb3duX3BhdGhfcHJlZml4IjoiL3ZvZGRvd24vIiwibGVnYWN5X2Rvd25sb2FkX3BhdGhfcHJlZml4IjoiL2Rvd25sb2FkLyJ9"
)
_CONFIG = json.loads(base64.b64decode(_CONFIG_B64, validate=True))
_BASE_URL = _CONFIG["base_url"]
_API_URL = _CONFIG["api_search_url"]
_BASE_HOST = urlsplit(_BASE_URL).hostname
KPKUANG_HOST_CONFIG = MappingProxyType({
    "base_url": _BASE_URL,
    "api_search_url": _API_URL,
    "allowed_hosts": frozenset({_BASE_HOST, urlsplit(_API_URL).hostname}),
    "detail_path_prefix": _CONFIG["detail_path_prefix"],
    "voddown_path_prefix": _CONFIG["voddown_path_prefix"],
    "legacy_download_path_prefix": _CONFIG["legacy_download_path_prefix"],
})

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_CANDIDATES = 3
_SEARCH_INTERVAL_SECONDS = 5.5
_STOP_CANDIDATE_ERRORS = (IndexerRateLimited, IndexerChallengeRequired, IndexerSecurityError)
_ID = re.compile(r"^\d{1,12}$")


class KPkuangAdapter(DirectResultAdapter):
    site_id = "kpkuang"
    site_name = "看片狂人"
    base_url = _BASE_URL
    default_enabled = False
    capabilities = IndexerCapabilities(pagination_supported=False, download_kinds=("magnet",))

    def __init__(self, *, http):
        self.http = http
        self._search_lock = CrossLoopAsyncLock()
        self._search_finished: float | None = None

    def search_timeout_overhead_seconds(self) -> float:
        return _SEARCH_INTERVAL_SECONDS

    async def search(self, request: IndexerSearchRequest) -> IndexerPage:
        if request.page > 1:
            return self._page(request, [])
        # 实站偶发返回code=0/js空串，稍后同词却有资源；不能当成“无资源”。
        # 仅对这类可恢复响应补查一次，仍受service整轮时间预算约束。
        candidates = None
        for _ in range(2):
            async with self._search_lock:
                if self._search_finished is not None:
                    delay = _SEARCH_INTERVAL_SECONDS - (time.monotonic() - self._search_finished)
                    if delay > 0:
                        await asyncio.sleep(delay)
                try:
                    response = await self._get(
                        _API_URL,
                        params={"kw": request.query, "ts": int(time.time() * 1000), "callback": "cb"},
                        headers={"Referer": self.base_url},
                    )
                finally:
                    self._search_finished = time.monotonic()
            self._check_status(response)
            candidates = self._parse_candidates(self._body(response.body))
            if candidates is not None:
                break
        if candidates is None:
            raise IndexerUnavailable("KPkuang returned an inconclusive search response", public_message="站点暂未返回可核验的搜索结果，请稍后重试")
        media = IndexerMediaSearchRequest.create(
            title=request.query,
            year=None,  # 目录年份取API字段，不能把数字片名误判为发行年份。
            media_type=request.media_type,
            sort_mode=request.sort_mode,
            season=request.season,
            episode=request.episode,
        )
        ranked = []
        for index, candidate in enumerate(candidates):
            title = str(candidate["title"])
            probe = rank_item(
                IndexerItem(site_id=self.site_id, site_name=self.site_name, title=title),
                media=media,
                fallback_query=request.query,
            )
            if not set(probe.match_reasons) & {"title_exact", "title_contains", "title_similar"}:
                continue
            ranked.append((index, candidate, probe))
        if request.year is not None:
            exact_year = [entry for entry in ranked if entry[1]["year"] == request.year]
            ranked = exact_year or [entry for entry in ranked if entry[1]["year"] is None]
        ranked.sort(key=lambda entry: ("title_exact" not in entry[2].match_reasons, candidate_sort_key((0, entry[0], entry[2]), request.sort_mode)))
        candidates = [entry[1] for entry in ranked]
        if not candidates:
            return self._page(request, [])

        items: list[IndexerItem] = []
        errors: list[IndexerError] = []
        successful_details = 0
        fatal_error: IndexerError | None = None
        seen_hashes: set[str] = set()
        for candidate in candidates[:_MAX_CANDIDATES]:
            detail_url = fixed_host_join(self.base_url, f"{_CONFIG['detail_path_prefix']}{candidate['id']}/")
            try:
                detail = await self._get(detail_url, headers={"Referer": self.base_url})
                self._check_status(detail)
                self._check_page_url(detail.url, candidate["id"], _CONFIG["detail_path_prefix"])
                detail_soup = self._parse_main_page(self._body(detail.body), "detail")
                successful_details += 1
            except IndexerError as exc:
                errors.append(exc)
                if isinstance(exc, _STOP_CANDIDATE_ERRORS):
                    fatal_error = exc
                    break
                continue

            download_url = self._download_url(detail_soup, candidate["id"])
            if download_url:
                try:
                    download = await self._get(download_url, headers={"Referer": detail_url})
                    self._check_status(download)
                    self._check_download_url(download.url, candidate["id"])
                    download_soup = self._parse_main_page(self._body(download.body), "download")
                    items.extend(self._parse_magnets(download_soup, candidate, detail_url, seen_hashes))
                    if request.episode is not None and any(
                        {"episode_exact", "episode_range"} & set(rank_item(item, media=media, fallback_query=request.query).match_reasons)
                        for item in items
                    ):
                        break  # 精确季集已在匹配作品中找到，无需再抓相似分册的巨型目录。
                except IndexerError as exc:
                    errors.append(exc)
                    if isinstance(exc, _STOP_CANDIDATE_ERRORS):
                        fatal_error = exc
                        break

            if items:
                report_page(self.site_id, self._page(
                    request,
                    list(items),
                    errors=tuple(
                        IndexerProviderError(self.site_id, error.code, "KPkuang 部分详情暂不可用")
                        for error in errors
                    ),
                    complete=False,
                ))

        if errors and successful_details == 0:
            raise fatal_error or errors[0]
        provider_errors = tuple(
            IndexerProviderError(self.site_id, error.code, "KPkuang 部分详情暂不可用")
            for error in errors
        )
        return self._page(
            request,
            items,
            errors=provider_errors,
            complete=not errors and len(candidates) <= _MAX_CANDIDATES,
        )

    async def _get(self, url: str, **kwargs):
        try:
            return await self.http.get(url, **kwargs)
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise IndexerTimeout("KPkuang request timed out") from exc
        except IndexerError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise IndexerUnavailable("KPkuang request failed") from exc

    @staticmethod
    def _check_status(response) -> None:
        status = int(response.status_code)
        if str(response.headers.get("cf-mitigated") or "").lower() == "challenge" or status in {401, 403}:
            raise IndexerChallengeRequired("KPkuang requires verification")
        if status == 429:
            raise IndexerRateLimited("KPkuang returned HTTP 429")
        if status == 404 or status >= 500:
            raise IndexerUnavailable(f"KPkuang returned HTTP {status}")
        if status != 200:
            raise IndexerInvalidResponse(f"KPkuang returned HTTP {status}")

    @staticmethod
    def _body(body: bytes) -> bytes:
        if len(body) > MAX_RESPONSE_BYTES:
            raise IndexerResponseTooLarge("KPkuang response exceeded the parsing limit")
        return body

    @staticmethod
    def _parse_candidates(body: bytes) -> list[dict[str, object]] | None:
        try:
            text = body.decode("utf-8-sig")
            wrapper = re.fullmatch(r"\s*cb\s*\((.*)\)\s*;?\s*", text, re.S)
            payload = json.loads(wrapper.group(1)) if wrapper else None
            if isinstance(payload, dict) and payload.get("code") == 0 and payload.get("js") == "":
                return None
            if not isinstance(payload, dict) or payload.get("code") != 1:
                raise ValueError("invalid JSONP payload")
            encoded = payload["js"]
            decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
            values = json.loads(decoded.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError, binascii.Error) as exc:
            if is_likely_challenge_page(body, usable_markers=("fed-main-info", "fed-part-case")):
                raise IndexerChallengeRequired("KPkuang search requires verification") from exc
            raise IndexerInvalidResponse("KPkuang search response was malformed") from exc
        if not isinstance(values, list):
            raise IndexerInvalidResponse("KPkuang search results were not a list")

        candidates = []
        for value in values:
            data = value.get("data") if isinstance(value, dict) else None
            candidate_id = str(value.get("id") or "").strip() if isinstance(value, dict) else ""
            if not isinstance(data, dict) or not _ID.fullmatch(candidate_id):
                continue
            title = html.unescape(str(data.get("vod_name") or "")).strip()
            if not title:
                continue
            try:
                year = int(data.get("vod_year"))
            except (TypeError, ValueError):
                year = None
            candidates.append({"id": candidate_id, "title": title, "year": year})
        if values and not candidates:
            raise IndexerInvalidResponse("KPkuang search results contained no valid candidates")
        return candidates

    @staticmethod
    def _parse_main_page(body: bytes, stage: str) -> BeautifulSoup:
        soup = BeautifulSoup(body, "lxml")
        if soup.select_one(".fed-main-info > .fed-part-case") is None:
            if is_likely_challenge_page(body, usable_markers=("fed-main-info", "fed-part-case")):
                raise IndexerChallengeRequired(f"KPkuang {stage} requires verification")
            raise IndexerInvalidResponse(f"KPkuang {stage} page omitted main content")
        return soup

    @staticmethod
    def _download_url(soup: BeautifulSoup, candidate_id: str) -> str | None:
        main = soup.select_one(".fed-main-info > .fed-part-case")
        play_data = main.find(class_="fed-play-data", recursive=False)
        if play_data is None:
            return None
        for key in ("voddown_path_prefix", "legacy_download_path_prefix"):
            prefix = str(_CONFIG[key])
            for anchor in play_data.select("a[href]"):
                href = html.unescape(str(anchor.get("href") or "").strip())
                try:
                    url = fixed_host_join(_BASE_URL, href)
                except IndexerSecurityError:
                    continue
                if _belongs_to(urlsplit(url).path, prefix, candidate_id):
                    return url
        return None

    @staticmethod
    def _parse_magnets(soup, candidate, detail_url, seen_hashes):
        main = soup.select_one(".fed-main-info > .fed-part-case")
        play_data = main.find(class_="fed-play-data", recursive=False)
        if play_data is None:
            return []
        items = []
        for node in play_data.select("[data-clipboard-text]"):
            magnet = _decode_magnet(node.get("data-clipboard-text"))
            infohash = magnet_infohash(magnet)
            if not magnet or not infohash or infohash in seen_hashes:
                continue
            row = node.find_parent(["li", "tr"])
            label = row.select_one("a[title]") if row else None
            title = (html.unescape(str(label.get("title") or "")).strip() if label
                     else row.get_text(" ", strip=True) if row else candidate["title"])
            seen_hashes.add(infohash)
            size = re.search(r"\[\s*(\d+(?:\.\d+)?\s*[KMGTPE]?i?B)\s*\]", title, re.I)
            size_text = size.group(1).strip() if size else None
            items.append(IndexerItem(
                site_id=KPkuangAdapter.site_id,
                site_name=KPkuangAdapter.site_name,
                title=title[:512] or candidate["title"],
                detail_url=detail_url,
                category="磁力资源",
                size_text=size_text,
                size_bytes=parse_size_bytes(size_text),
                download_state="ready",
                download_kinds=("magnet",),
                magnet=magnet,
            ))
        return items

    @staticmethod
    def _check_page_url(response_url: str, candidate_id: str, prefix: str) -> None:
        path = urlsplit(response_url).path
        if urlsplit(response_url).hostname != _BASE_HOST or not re.fullmatch(re.escape(prefix) + re.escape(candidate_id) + r"/?", path):
            raise IndexerInvalidResponse("KPkuang detail response did not match its candidate")

    @staticmethod
    def _check_download_url(response_url: str, candidate_id: str) -> None:
        path = urlsplit(response_url).path
        if urlsplit(response_url).hostname != _BASE_HOST or not any(
            _belongs_to(path, str(_CONFIG[key]), candidate_id)
            for key in ("voddown_path_prefix", "legacy_download_path_prefix")
        ):
            raise IndexerInvalidResponse("KPkuang download response did not match its detail")

    @staticmethod
    def _page(request, items, *, errors=(), complete=True):
        return IndexerPage(
            items=items, page=request.page, has_more=False,
            pagination_supported=False, errors=errors, complete=complete,
        )


def _belongs_to(path: str, prefix: str, candidate_id: str) -> bool:
    start = prefix + candidate_id
    return path.startswith(start) and len(path) > len(start) and path[len(start)] in "-/"


def _decode_magnet(value: object) -> str | None:
    raw = html.unescape(str(value or "")).strip()
    if not raw.lower().startswith("magnet:"):
        try:
            raw = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True).decode().strip()
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
    return raw if raw.lower().startswith("magnet:?") and magnet_infohash(raw) else None
