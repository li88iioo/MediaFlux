from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from app.concurrency import CrossLoopAsyncLock
from ..errors import IndexerInvalidResponse, IndexerRateLimited, IndexerSecurityError, IndexerUnavailable
from .base import fixed_host_join, require_html_response

_GOOGLE_SEARCH_URL = "https://www.google.com/search"
_GOOGLE_BLOCK_MARKERS = (
    "unusual traffic",
    "our systems have detected",
    "captcha",
    "异常流量",
    "人机验证",
)
_GOOGLE_INTERSTITIAL_MARKERS = (
    "enable javascript",
    "启用 javascript",
    "如果您在几秒钟内没有被重定向",
    "httpservice/retry/enablejs",
    "emsg=sg_rel",
)
_GOOGLE_EMPTY_MARKERS = (
    "did not match any documents",
    "找不到和您查询的",
    "没有找到与您查询",
)


@dataclass(frozen=True, slots=True)
class GoogleSiteResult:
    title: str
    url: str


class GoogleSiteSearch:
    """Best-effort Google site search with a local cooldown and strict result hosts."""

    def __init__(
        self,
        *,
        http,
        cooldown_seconds: float = 300,
        timeout_seconds: float = 3,
        monotonic: Callable[[], float] | None = None,
    ):
        self.http = http
        self.cooldown_seconds = max(1.0, float(cooldown_seconds))
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self._monotonic = monotonic or time.monotonic
        self._disabled_until = 0.0
        self._lock = CrossLoopAsyncLock()

    async def search(
        self,
        query: str,
        *,
        site_domain: str,
        allowed_bases: Iterable[str],
    ) -> list[GoogleSiteResult] | None:
        async with self._lock:
            if self._disabled_until > self._monotonic():
                return None
            try:
                response = await asyncio.wait_for(
                    self.http.get(
                        _GOOGLE_SEARCH_URL,
                        params={
                            "q": f"{query} site:{site_domain}",
                            "num": "50",
                            "hl": "zh-CN",
                            "filter": "0",
                        },
                        headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
                        max_redirects=0,
                    ),
                    timeout=self.timeout_seconds,
                )
                results = self._parse_response(response, tuple(allowed_bases))
            except Exception:
                self._disabled_until = self._monotonic() + self.cooldown_seconds
                return None
            self._disabled_until = 0.0
            return results

    @staticmethod
    def _parse_response(response, allowed_bases: tuple[str, ...]) -> list[GoogleSiteResult]:
        status_code = int(response.status_code)
        if status_code in {403, 429}:
            raise IndexerRateLimited(f"Google returned HTTP {status_code}")
        if status_code >= 500:
            raise IndexerUnavailable(f"Google returned HTTP {status_code}")
        if status_code != 200:
            raise IndexerInvalidResponse(f"Google returned HTTP {status_code}")
        require_html_response(response)
        html = response.text
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(" ", strip=True).casefold()
        raw = html[:256 * 1024].casefold()
        if any(marker in text or marker in raw for marker in _GOOGLE_BLOCK_MARKERS):
            raise IndexerRateLimited("Google returned a traffic challenge")
        if any(marker in text or marker in raw for marker in _GOOGLE_INTERSTITIAL_MARKERS):
            raise IndexerUnavailable("Google returned a JavaScript interstitial")
        if any(marker in text for marker in _GOOGLE_EMPTY_MARKERS):
            return []

        results: list[GoogleSiteResult] = []
        seen: set[str] = set()
        for anchor in soup.select("a[href]"):
            heading = anchor.find("h3")
            if heading is None:
                continue
            candidate = GoogleSiteSearch._unwrap_google_url(str(anchor.get("href") or ""))
            safe_url = GoogleSiteSearch._safe_result_url(candidate, allowed_bases)
            title = heading.get_text(" ", strip=True)
            if not safe_url or not title or safe_url in seen:
                continue
            seen.add(safe_url)
            results.append(GoogleSiteResult(title=title, url=safe_url))
            if len(results) >= 50:
                break
        if not results:
            raise IndexerInvalidResponse("Google site search returned no parseable results")
        return results

    @staticmethod
    def _unwrap_google_url(href: str) -> str:
        candidate = str(href or "").strip()
        parsed = urlsplit(candidate)
        if candidate.startswith("/url?") or (
            (parsed.hostname or "").lower() in {"google.com", "www.google.com"}
            and parsed.path == "/url"
        ):
            query = parse_qs(parsed.query)
            return str((query.get("q") or query.get("url") or [""])[0]).strip()
        return candidate

    @staticmethod
    def _safe_result_url(candidate: str, allowed_bases: tuple[str, ...]) -> str | None:
        if not str(candidate or "").strip().lower().startswith("https://"):
            return None
        for base_url in allowed_bases:
            try:
                return fixed_host_join(base_url, candidate)
            except IndexerSecurityError:
                continue
        return None
