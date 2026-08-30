"""可复用的 TMDB HTTP Client。"""
from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import requests

from app import config as app_config
from app.discovery.models import (
    ProviderInvalidResponse,
    ProviderNotConfigured,
    ProviderUnavailable,
)
from app.discovery.providers.base import (
    DEFAULT_TIMEOUT,
    TimeoutValue,
    map_request_error,
    response_json,
)
from app.logger import get_logger

logger = get_logger(__name__)
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._~/-]+$")
_DEFAULT_TMDB_API_URL = "https://api.themoviedb.org/3"


def _normalize_path(path: str) -> str:
    raw = str(path or "").strip()
    if not raw.startswith("/") or "://" in raw or "?" in raw or "#" in raw:
        raise ValueError("TMDB path must be relative")
    if not _SAFE_PATH_RE.fullmatch(raw):
        raise ValueError("TMDB path contains unsupported characters")
    segments = [segment for segment in raw.split("/") if segment and segment != "."]
    if not segments or any(segment == ".." for segment in segments):
        raise ValueError("TMDB path must be relative")
    return "/" + "/".join(segments)


def _config_get(config: Mapping[str, Any] | Callable[[str, str], Any] | None,
                key: str, default: str = "") -> str:
    if config is None:
        return str(app_config.get(key, default) or "")
    if callable(config):
        return str(config(key, default) or "")
    return str(config.get(key, default) or "")


def _normalize_base_url(value: object) -> tuple[str, str]:
    raw = str(value or "").strip()
    if not raw:
        return _DEFAULT_TMDB_API_URL, ""
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return raw.rstrip("/"), "TMDB API URL 必须是 http:// 或 https:// 地址"
    return raw.rstrip("/"), ""


class TMDBClient:
    """服务端 TMDB 客户端；认证参数不会进入调用方返回值。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        language: str = "zh-CN",
        proxy_url: str | None = None,
        timeout: TimeoutValue = DEFAULT_TIMEOUT,
        retries: int = 1,
        session: requests.Session | Any | None = None,
        config: Mapping[str, Any] | Callable[[str, str], Any] | None = None,
    ):
        self.api_key = (
            _config_get(config, "TMDB_API_KEY") if api_key is None else str(api_key or "")
        ).strip()
        configured_url = _config_get(
            config, "TMDB_API_URL", _DEFAULT_TMDB_API_URL
        ) if base_url is None else str(base_url or "")
        self.base_url, self.config_error = _normalize_base_url(configured_url)
        self.language = str(language or "zh-CN")
        self.timeout = timeout
        self.retries = max(0, min(int(retries), 2))
        self.session = session or requests.Session()
        self._close_lock = threading.Lock()
        self._closed = False

        proxy = (
            _config_get(config, "PROXY_URL") if proxy_url is None else str(proxy_url or "")
        ).strip()
        if proxy:
            normalized = proxy if proxy.startswith(("http://", "https://")) else f"http://{proxy}"
            self.session.proxies.update({"http": normalized, "https": normalized})

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        deadline_at: float | None = None,
        retries: int | None = None,
    ) -> dict[str, Any]:
        with self._close_lock:
            if self._closed:
                raise ProviderUnavailable("TMDB Client 已关闭")
        if self.config_error:
            raise ProviderNotConfigured(self.config_error)
        if not self.api_key:
            raise ProviderNotConfigured("未配置 TMDB_API_KEY")
        clean_path = _normalize_path(path)
        logger.debug("TMDB request path=%s", clean_path)
        query = dict(params or {})
        query["api_key"] = self.api_key
        query.setdefault("language", self.language)

        attempts = (self.retries if retries is None else max(0, min(int(retries), 2))) + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                request_timeout: TimeoutValue = self.timeout
                if deadline_at is not None:
                    remaining = float(deadline_at) - time.monotonic()
                    if remaining <= 0:
                        raise ProviderUnavailable("全库巡检截止时间已到")
                    configured = (
                        max(self.timeout)
                        if isinstance(self.timeout, tuple)
                        else float(self.timeout)
                    )
                    request_timeout = min(configured, remaining)
                response = self.session.get(
                    f"{self.base_url}{clean_path}", params=query, timeout=request_timeout,
                    allow_redirects=False,
                )
                status = int(getattr(response, "status_code", 0) or 0)
                if 300 <= status < 400:
                    raise ProviderUnavailable("TMDB 重定向被拒绝")
                return response_json(response, expected_type=dict)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    continue
                raise map_request_error(exc) from exc
            except requests.RequestException as exc:
                raise map_request_error(exc) from exc
        raise map_request_error(last_error or requests.RequestException("request failed"))

    def search(
        self,
        title: str,
        year: str,
        media_type: str,
        *,
        deadline_at: float | None = None,
        retries: int | None = None,
    ) -> list[dict[str, Any]]:
        normalized_type = "tv" if media_type == "tv" else "movie"
        params: dict[str, Any] = {"query": str(title or "")}
        if year:
            key = "first_air_date_year" if normalized_type == "tv" else "year"
            params[key] = str(year)
        payload = self.get(
            f"/search/{normalized_type}",
            params,
            deadline_at=deadline_at,
            retries=retries,
        )
        results = payload.get("results", [])
        if not isinstance(results, list):
            raise ProviderInvalidResponse("TMDB 搜索响应结构无效")
        return [item for item in results if isinstance(item, dict)][:10]

    @staticmethod
    def _numeric_id(value: object) -> str:
        normalized = str(value or "").strip()
        if not normalized.isascii() or not normalized.isdigit() or not 1 <= len(normalized) <= 10:
            raise ValueError("TMDB ID 必须是 1 到 10 位数字")
        return normalized

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        close = getattr(self.session, "close", None)
        if callable(close):
            close()

    def detail(
        self,
        tmdb_id: str,
        media_type: str,
        *,
        deadline_at: float | None = None,
        retries: int | None = None,
    ) -> dict[str, Any]:
        normalized_type = "tv" if media_type == "tv" else "movie"
        path = f"/{normalized_type}/{self._numeric_id(tmdb_id)}"
        if deadline_at is None and retries is None:
            return self.get(path)
        return self.get(path, deadline_at=deadline_at, retries=retries)

    def detail_with_alternative_titles(
        self, tmdb_id: str, media_type: str
    ) -> dict[str, Any]:
        normalized_type = "tv" if media_type == "tv" else "movie"
        return self.get(
            f"/{normalized_type}/{self._numeric_id(tmdb_id)}",
            {"append_to_response": "alternative_titles,translations"},
        )

    def tv_season_detail(
        self,
        tmdb_id: str,
        season_number: int,
        *,
        deadline_at: float | None = None,
        retries: int | None = None,
    ) -> dict[str, Any]:
        if isinstance(season_number, bool) or not isinstance(season_number, int):
            raise ValueError("season_number 必须是整数")
        if not 0 <= season_number <= 100:
            raise ValueError("season_number 必须在 0 到 100 之间")
        path = f"/tv/{self._numeric_id(tmdb_id)}/season/{season_number}"
        if deadline_at is None and retries is None:
            return self.get(path)
        return self.get(path, deadline_at=deadline_at, retries=retries)
