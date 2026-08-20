"""可选的豆瓣 Frodo 回退客户端。

该客户端只访问固定 Frodo 主机和受控 API 路径。凭据仅用于服务端签名，
不会写入日志或返回给调用方。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote

import requests

from app.discovery.models import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderInvalidResponse,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.discovery.providers.base import (
    DEFAULT_TIMEOUT,
    TimeoutValue,
)
from app.logger import get_logger

try:
    from app.clients.douban_public import DoubanPublicPage
except ModuleNotFoundError as exc:  # Task 1 可在独立工作树中并行实现。
    if exc.name != "app.clients.douban_public":
        raise

    @dataclass(frozen=True)
    class DoubanPublicPage:  # type: ignore[no-redef]
        items: tuple[dict[str, Any], ...] = field(default_factory=tuple)
        has_more: bool = False
        source: str = "frodo"

        def __post_init__(self) -> None:
            object.__setattr__(self, "items", tuple(self.items or ()))


logger = get_logger(__name__)

_BASE_URL = "https://frodo.douban.com"
_NAS_COMPAT_API_KEY = '0dad551ec0f84ed02907ff5c42e8ec70'
_NAS_COMPAT_API_SECRET = 'bf7dddc7c9cfe6f7'
# Frodo 会对通用服务端 UA 返回 403；使用不含设备唯一标识的客户端兼容 UA。
_USER_AGENT = (
    "Rexxar-Core/0.1.3 api-client/1 com.douban.frodo/7.96.0(230) "
    "iPhone/15.1.1 iPhone13,2 network/wifi model/unknown"
)
_DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_SENSITIVE_SESSION_HEADERS = {"authorization", "proxy-authorization", "cookie"}
_CREDENTIAL_SOURCES = frozenset({"explicit", "environment", "compatibility_default"})
_COLLECTIONS = {
    "movie_showing": "movie_showing",
    "movie_soon": "movie_soon",
    "movie_hot": "movie_hot_gaia",
    "movie_top250": "movie_top250",
    "tv_hot": "tv_hot",
    "tv_chinese_weekly": "tv_chinese_best_weekly",
    "tv_global_weekly": "tv_global_best_weekly",
}
_RECOMMEND_FILTERS = {"sort", "genres", "tags", "year_range", "countries"}
_EXTERNAL_ID_RE = re.compile(r"^[0-9]+$")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_TAG_RE = re.compile(r"<[^>]*>")
_DETAIL_FIELDS = (
    "id",
    "media_type",
    "title",
    "original_title",
    "year",
    "overview",
    "poster_url",
    "rating",
    "release_date",
)



def _resolved_credentials(
    api_key: str | None,
    api_secret: str | None,
) -> tuple[str, str, str]:
    """按来源成对解析凭据，禁止环境值与内置兼容值交叉混用。"""
    if api_key is not None or api_secret is not None:
        return (
            str(api_key or "").strip(),
            str(api_secret or "").strip(),
            "explicit",
        )

    key_in_environment = "DOUBAN_FRODO_API_KEY" in os.environ
    secret_in_environment = "DOUBAN_FRODO_API_SECRET" in os.environ
    if key_in_environment or secret_in_environment:
        return (
            str(os.environ.get("DOUBAN_FRODO_API_KEY") or "").strip(),
            str(os.environ.get("DOUBAN_FRODO_API_SECRET") or "").strip(),
            "environment",
        )

    return _NAS_COMPAT_API_KEY, _NAS_COMPAT_API_SECRET, "compatibility_default"


def _safe_credential_source(value: Any) -> str:
    source = str(value or "").strip()
    return source if source in _CREDENTIAL_SOURCES else "unknown"


def _credential_identity(api_key: str, api_secret: str, user_agent: str) -> str:
    """生成凭据与 UA 的认证配置指纹，仅用于进程内熔断匹配。"""
    if not api_key or not api_secret:
        return ""
    payload = f"{api_key}\0{api_secret}\0{user_agent}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _finite_timeout(value: TimeoutValue | None) -> TimeoutValue:
    """只接受有限且大于零的单值或 connect/read 超时。"""
    if isinstance(value, bool) or value is None:
        raise ValueError("timeout must be finite and positive")
    if isinstance(value, (int, float)):
        timeout = float(value)
        if math.isfinite(timeout) and timeout > 0:
            return timeout
        raise ValueError("timeout must be finite and positive")
    if isinstance(value, tuple) and len(value) == 2:
        try:
            connect, read = float(value[0]), float(value[1])
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout must be finite and positive") from exc
        if all(math.isfinite(part) and part > 0 for part in (connect, read)):
            return connect, read
    raise ValueError("timeout must be finite and positive")


def _validated_user_agent(value: str | None) -> str:
    raw = _USER_AGENT if value is None else str(value)
    if any(ord(char) < 0x20 or ord(char) > 0x7E for char in raw):
        raise ValueError("user_agent must contain visible ASCII characters only")
    user_agent = raw.strip()
    if not 8 <= len(user_agent) <= 256:
        raise ValueError("user_agent must be 8-256 characters")
    return user_agent


def _header(headers: Mapping[str, Any], name: str) -> str:
    wanted = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == wanted:
            return str(value or "")
    return ""


def _detached_provider_error(error: ProviderError) -> ProviderError:
    """重建安全错误，彻底丢弃上游异常链和潜在敏感 traceback。"""
    if isinstance(error, ProviderAuthenticationError):
        clean: ProviderError = ProviderAuthenticationError("豆瓣 Frodo 认证失败")
    elif isinstance(error, ProviderRateLimited):
        clean = ProviderRateLimited(
            "豆瓣 Frodo 请求受限",
            retry_after=error.retry_after,
        )
    elif isinstance(error, ProviderTimeout):
        clean = ProviderTimeout("豆瓣 Frodo 请求超时")
    elif isinstance(error, ProviderInvalidResponse):
        clean = ProviderInvalidResponse("豆瓣 Frodo 响应无效")
    elif isinstance(error, ProviderNotConfigured):
        clean = ProviderNotConfigured("豆瓣 Frodo 回退凭据未完整配置")
    else:
        clean = ProviderUnavailable("豆瓣 Frodo 服务不可用")
    clean.__cause__ = None
    clean.__context__ = None
    clean.__traceback__ = None
    return clean


def _plain_text(value: Any) -> str:
    raw = str(value or "")
    without_tags = _TAG_RE.sub(" ", raw)
    return " ".join(without_tags.split())


def _poster_url(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("large") or value.get("normal") or value.get("small")
    return str(value or "").strip()


def _rating(value: Any) -> float | None:
    if isinstance(value, Mapping):
        value = value.get("value") if value.get("value") is not None else value.get("score")
    try:
        result = float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return result if result is not None and math.isfinite(result) else None


def _normalize_subject(raw: Mapping[str, Any], fallback_type: str) -> dict[str, Any] | None:
    nested = raw.get("subject")
    if isinstance(nested, Mapping):
        merged = dict(raw)
        merged.update(nested)
        raw = merged

    external_id = str(raw.get("id") or raw.get("subject_id") or "").strip()
    if not external_id:
        return None

    raw_type = str(raw.get("type") or fallback_type).strip().lower()
    media_type = "tv" if raw_type in {"tv", "tvshow", "series"} else "movie"
    release_date = str(raw.get("release_date") or raw.get("date") or "").strip()
    explicit_year = str(raw.get("year") or "").strip()
    year_match = _YEAR_RE.search(
        explicit_year or release_date or str(raw.get("card_subtitle") or "")
    )

    return {
        "id": external_id,
        "media_type": media_type,
        "title": _plain_text(raw.get("title") or raw.get("name")),
        "original_title": _plain_text(raw.get("original_title") or raw.get("original_name")),
        "year": year_match.group(0) if year_match else "",
        "overview": _plain_text(
            raw.get("intro") or raw.get("abstract") or raw.get("description")
        ),
        "poster_url": _poster_url(raw.get("pic") or raw.get("cover") or raw.get("image")),
        "rating": _rating(raw.get("rating") if raw.get("rating") is not None else raw.get("rate")),
        "release_date": release_date,
        "is_new": bool(raw.get("is_new", False)),
        "episodes_info": _plain_text(raw.get("episodes_info")),
    }


class DoubanFrodoClient:
    """使用服务端兼容默认值或环境覆盖的 Frodo 回退客户端。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        session: requests.Session | Any | None = None,
        clock: Callable[[], float] | None = None,
        timeout: TimeoutValue = DEFAULT_TIMEOUT,
        page_size: int = 20,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        user_agent: str | None = None,
    ):
        self.api_key, self.api_secret, self.credential_source = _resolved_credentials(
            api_key, api_secret
        )
        self.user_agent = _validated_user_agent(user_agent)
        self._credential_identity = _credential_identity(
            self.api_key, self.api_secret, self.user_agent
        )
        self.session = session or requests.Session()
        self.clock = clock or time.time
        self.timeout = _finite_timeout(timeout)
        try:
            self.max_response_bytes = int(max_response_bytes)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("max_response_bytes must be positive") from exc
        if self.max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        try:
            normalized_page_size = int(page_size)
        except (TypeError, ValueError) as exc:
            raise ValueError("page_size must be an integer") from exc
        self.page_size = max(1, min(normalized_page_size, 100))
        self._sanitize_session_credentials()

    def _clear_cookies(self) -> None:
        cookies = getattr(self.session, "cookies", None)
        clear = getattr(cookies, "clear", None)
        if callable(clear):
            clear()

    def _sanitize_session_credentials(self) -> None:
        headers = getattr(self.session, "headers", None)
        if headers is not None:
            for key in list(headers):
                if str(key).lower() in _SENSITIVE_SESSION_HEADERS:
                    headers.pop(key, None)
        if hasattr(self.session, "auth"):
            self.session.auth = None
        if hasattr(self.session, "trust_env"):
            self.session.trust_env = False
        self._clear_cookies()

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _ensure_configured(self) -> None:
        if not self.configured:
            raise ProviderNotConfigured("豆瓣 Frodo 回退凭据未完整配置")

    def _signed_params(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._ensure_configured()
        timestamp = float(self.clock())
        if not math.isfinite(timestamp):
            raise ProviderInvalidResponse("豆瓣 Frodo 签名时间无效")
        date_stamp = datetime.fromtimestamp(timestamp).strftime("%Y%m%d")
        raw = f"{method.upper()}&{quote(path, safe='')}&{date_stamp}"
        signature = base64.b64encode(
            hmac.new(
                self.api_secret.encode("utf-8"),
                raw.encode("utf-8"),
                hashlib.sha1,
            ).digest()
        ).decode("ascii")
        signed = dict(params or {})
        signed.update({"apiKey": self.api_key, "_ts": date_stamp, "_sig": signature})
        return signed

    def _read_json(self, response: Any) -> dict[str, Any]:
        headers = getattr(response, "headers", {}) or {}
        content_type = _header(headers, "Content-Type").split(";", 1)[0].strip().lower()
        if content_type != "application/json" and not content_type.endswith("+json"):
            raise ProviderInvalidResponse("豆瓣 Frodo 响应 Content-Type 无效")

        content_length = _header(headers, "Content-Length")
        if content_length:
            try:
                length = int(content_length)
            except ValueError as exc:
                raise ProviderInvalidResponse("豆瓣 Frodo 响应长度无效") from exc
            if length < 0 or length > self.max_response_bytes:
                raise ProviderInvalidResponse("豆瓣 Frodo 响应过大")

        iterator = getattr(response, "iter_content", None)
        if not callable(iterator):
            raise ProviderInvalidResponse("豆瓣 Frodo 响应不可读取")
        chunks: list[bytes] = []
        size = 0
        for chunk in iterator(chunk_size=64 * 1024):
            if not chunk:
                continue
            if not isinstance(chunk, bytes):
                raise ProviderInvalidResponse("豆瓣 Frodo 响应字节流无效")
            size += len(chunk)
            if size > self.max_response_bytes:
                raise ProviderInvalidResponse("豆瓣 Frodo 响应过大")
            chunks.append(chunk)
        try:
            payload = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, TypeError, ValueError) as exc:
            raise ProviderInvalidResponse("豆瓣 Frodo 返回无效 JSON") from exc
        if not isinstance(payload, dict):
            raise ProviderInvalidResponse("豆瓣 Frodo 响应结构无效")
        return payload

    @staticmethod
    def _status_error(response: Any) -> ProviderError | None:
        status = int(getattr(response, "status_code", 0) or 0)
        if 300 <= status < 400:
            return ProviderUnavailable("豆瓣 Frodo 上游重定向被拒绝")
        if status in {401, 403}:
            return ProviderAuthenticationError("豆瓣 Frodo 认证失败")
        if status == 429:
            try:
                retry_after = int(_header(getattr(response, "headers", {}) or {}, "Retry-After") or 0)
            except ValueError:
                retry_after = 0
            return ProviderRateLimited("豆瓣 Frodo 请求受限", retry_after=retry_after)
        if status < 200 or status >= 300:
            return ProviderUnavailable("豆瓣 Frodo 服务不可用")
        return None

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._ensure_configured()
        signed = self._signed_params("GET", path, params)
        response = None
        payload: dict[str, Any] | None = None
        failure: ProviderError | None = None
        try:
            self._sanitize_session_credentials()
            response = self.session.get(
                f"{_BASE_URL}{path}",
                params=signed,
                headers={"Accept": "application/json", "User-Agent": self.user_agent},
                timeout=self.timeout,
                allow_redirects=False,
                stream=True,
            )
            failure = self._status_error(response)
            if failure is None:
                payload = self._read_json(response)
        except ProviderError as exc:
            failure = exc
        except requests.Timeout:
            failure = ProviderTimeout("豆瓣 Frodo 请求超时")
        except requests.RequestException:
            failure = ProviderUnavailable("豆瓣 Frodo 连接失败")
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

        if failure is not None:
            clean = _detached_provider_error(failure)
            # 鉴权失败由 Provider 统一记录一次并熔断；客户端层降为 DEBUG，
            # 避免同一次 401/403 连续产生两条 WARNING。
            log = (
                logger.debug
                if isinstance(clean, ProviderAuthenticationError)
                else logger.warning
            )
            log(
                "Douban Frodo request failed path=%s error=%s credential_source=%s",
                path,
                clean.code,
                _safe_credential_source(self.credential_source),
            )
            raise clean from None
        if payload is None:
            clean = ProviderInvalidResponse("豆瓣 Frodo 响应无效")
            raise clean from None
        return payload

    def list_items(
        self,
        category: str,
        media_type: str,
        page: int,
        filters: Mapping[str, Any] | None,
    ) -> DoubanPublicPage:
        self._ensure_configured()
        normalized_category = str(category or "").strip().lower()
        normalized_type = "tv" if str(media_type or "").strip().lower() == "tv" else "movie"
        try:
            normalized_page = max(1, int(page))
        except (TypeError, ValueError) as exc:
            raise ProviderInvalidResponse("豆瓣 Frodo 页码无效") from exc
        start = (normalized_page - 1) * self.page_size
        params: dict[str, Any] = {"start": start, "count": self.page_size}

        if normalized_category in _COLLECTIONS:
            collection = _COLLECTIONS[normalized_category]
            path = f"/api/v2/subject_collection/{collection}/items"
        elif normalized_category in {"recommend", "discover"}:
            path = f"/api/v2/{normalized_type}/recommend"
            params.update(
                {
                    key: value
                    for key, value in (filters or {}).items()
                    if key in _RECOMMEND_FILTERS and value not in (None, "")
                }
            )
        else:
            raise ProviderInvalidResponse("不支持的豆瓣 Frodo 分类")

        payload = self._get(path, params)
        raw_items = payload.get("subject_collection_items")
        if raw_items is None:
            raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise ProviderInvalidResponse("豆瓣 Frodo 列表响应结构无效")

        items = tuple(
            normalized
            for item in raw_items
            if isinstance(item, Mapping)
            if (normalized := _normalize_subject(item, normalized_type)) is not None
        )
        try:
            total = int(payload.get("total") or payload.get("count") or 0)
        except (TypeError, ValueError):
            total = 0
        has_more = (
            start + len(raw_items) < total
            if total
            else len(raw_items) >= self.page_size
        )
        return DoubanPublicPage(items=items, has_more=has_more, source="frodo")

    def get_detail(self, external_id: str, media_type: str) -> dict[str, Any]:
        self._ensure_configured()
        subject_id = str(external_id or "").strip()
        if not _EXTERNAL_ID_RE.fullmatch(subject_id):
            raise ProviderInvalidResponse("豆瓣 Frodo 条目 ID 无效")
        normalized_type = "tv" if str(media_type or "").strip().lower() == "tv" else "movie"
        payload = self._get(f"/api/v2/{normalized_type}/{subject_id}")
        normalized = _normalize_subject(payload, normalized_type)
        if normalized is None:
            raise ProviderInvalidResponse("豆瓣 Frodo 详情响应结构无效")
        return {key: normalized[key] for key in _DETAIL_FIELDS}
