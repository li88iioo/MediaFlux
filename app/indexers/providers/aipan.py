from __future__ import annotations

import asyncio
import html
import json
import re
import unicodedata
from urllib.parse import quote

import httpx

from ..errors import (
    IndexerError,
    IndexerInvalidResponse,
    IndexerRateLimited,
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
    fixed_host_join,
    is_likely_challenge_page,
    magnet_infohash,
    parse_size_bytes,
)

_MOVIE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_DETAIL_CANDIDATES = 3


class AipanAdapter(DirectResultAdapter):
    site_id = "aipan"
    site_name = "爱盼"
    base_url = "https://www.aipan.me/"
    search_url = "https://www.aipan.me/api/movies/search"
    default_enabled = True
    capabilities = IndexerCapabilities(pagination_supported=False, download_kinds=("magnet",))

    def __init__(self, *, http):
        self.http = http

    async def search(self, request: IndexerSearchRequest) -> IndexerPage:
        if request.page > 1:
            return IndexerPage(items=[], page=request.page, has_more=False, pagination_supported=False)

        response = await self._get(
            self.search_url,
            params={"q": request.query},
            headers={"Referer": self.base_url},
        )
        self._validate_response(response, json_response=True)
        payload = self._parse_json(response.body, "Aipan search")
        movies = payload.get("movies") if isinstance(payload, dict) else None
        if not isinstance(movies, list):
            raise IndexerInvalidResponse("Aipan search response omitted movies")
        if not movies:
            return self._page(request, [])

        candidates = []
        for movie in movies:
            if not isinstance(movie, dict):
                continue
            movie_id = str(movie.get("id") or "").strip()
            title = str(movie.get("title") or "").strip()
            if _MOVIE_ID.fullmatch(movie_id) and title:
                candidates.append((movie_id, title))
            if len(candidates) == _MAX_DETAIL_CANDIDATES:
                break
        if not candidates:
            raise IndexerInvalidResponse("Aipan search response contained no valid movie candidates")

        outcomes = await self._load_details(candidates)
        items: list[IndexerItem] = []
        failures: list[IndexerError] = []
        successes = 0
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                failures.append(
                    outcome if isinstance(outcome, IndexerError)
                    else IndexerInvalidResponse("Aipan detail response could not be parsed")
                )
                continue
            successes += 1
            items.extend(outcome)

        if failures and successes == 0:
            raise failures[0]
        errors = tuple(
            IndexerProviderError("btbtla", error.code, "Aipan 子站详情获取失败")
            for error in failures
        )
        return self._page(request, items, errors=errors)

    async def _load_details(self, candidates: list[tuple[str, str]]) -> list[object]:
        semaphore = asyncio.Semaphore(_MAX_DETAIL_CANDIDATES)

        async def load(candidate: tuple[str, str]) -> list[IndexerItem]:
            async with semaphore:
                return await self._load_detail(*candidate)

        tasks = [asyncio.create_task(load(candidate)) for candidate in candidates[:_MAX_DETAIL_CANDIDATES]]
        try:
            return await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            pending = [task for task in tasks if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def _load_detail(self, movie_id: str, movie_title: str) -> list[IndexerItem]:
        try:
            detail_url = fixed_host_join(self.base_url, f"/api/movies/detail/{quote(movie_id, safe='')}")
            response = await self._get(detail_url, headers={"Referer": self.base_url})
            self._validate_response(response, json_response=True)
            payload = self._parse_json(response.body, "Aipan detail")
            resources = payload.get("resources") if isinstance(payload, dict) else None
            if not isinstance(resources, list):
                raise IndexerInvalidResponse("Aipan detail response omitted resources")

            page_url = fixed_host_join(self.base_url, f"/movie/{quote(movie_id, safe='')}")
            items: list[IndexerItem] = []
            for resource in resources:
                if not isinstance(resource, dict):
                    continue
                raw_url = html.unescape(str(resource.get("url") or "").strip())
                if str(resource.get("kind") or "").lower() != "magnet" and not raw_url.lower().startswith("magnet:"):
                    continue
                if not magnet_infohash(raw_url):
                    continue
                release_title = str(resource.get("name") or "").strip()
                work_title = movie_title.strip()
                if release_title and work_title:
                    release_folded = unicodedata.normalize("NFKC", release_title).casefold()
                    work_folded = unicodedata.normalize("NFKC", work_title).casefold()
                    title = release_title if work_folded in release_folded else f"{release_title} — {work_title}"
                else:
                    title = release_title or work_title
                if not title:
                    continue
                size_text = str(resource.get("sizeLabel") or "").strip() or None
                items.append(IndexerItem(
                    site_id=self.site_id,
                    site_name=self.site_name,
                    title=title,
                    detail_url=page_url,
                    category=str(resource.get("category") or "Aipan"),
                    size_text=size_text,
                    size_bytes=parse_size_bytes(size_text),
                    download_state="ready",
                    download_kinds=("magnet",),
                    magnet=raw_url,
                ))
            return items
        except IndexerError:
            raise
        except Exception as exc:
            raise IndexerInvalidResponse("Aipan detail response could not be parsed") from exc

    async def _get(self, url: str, **kwargs):
        try:
            return await self.http.get(url, **kwargs)
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise IndexerTimeout("Aipan request timed out") from exc
        except IndexerError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise IndexerUnavailable("Aipan request failed") from exc
        except Exception as exc:
            raise IndexerUnavailable("Aipan request failed") from exc

    @staticmethod
    def _parse_json(body: bytes, stage: str):
        try:
            return json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IndexerInvalidResponse(f"Aipan {stage} response was malformed") from exc

    @staticmethod
    def _validate_response(response, *, json_response: bool) -> None:
        body = response.body
        if str(response.headers.get("cf-mitigated") or "").lower() == "challenge" or is_likely_challenge_page(body):
            raise IndexerUnavailable("Aipan returned a verification challenge")
        status = int(response.status_code)
        if status == 429:
            raise IndexerRateLimited("Aipan returned HTTP 429")
        if status >= 500 or status in {403, 404}:
            raise IndexerUnavailable(f"Aipan returned HTTP {status}")
        if status != 200:
            raise IndexerInvalidResponse(f"Aipan returned HTTP {status}")
        if json_response:
            content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].lower()
            if content_type != "application/json" and not content_type.endswith("+json"):
                raise IndexerInvalidResponse("Aipan returned a non-JSON response")

    @staticmethod
    def _page(
        request: IndexerSearchRequest,
        items: list[IndexerItem],
        *,
        errors: tuple[IndexerProviderError, ...] = (),
    ) -> IndexerPage:
        return IndexerPage(
            items=items,
            page=request.page,
            has_more=False,
            pagination_supported=False,
            errors=errors,
        )
