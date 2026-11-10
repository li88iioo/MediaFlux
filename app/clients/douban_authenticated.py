"""只携带 dbcl2、固定访问 movie.douban.com 的豆瓣认证网页回退客户端。"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from http.cookies import SimpleCookie
from typing import Any

import requests

from app.clients.douban_public import (
    _CATEGORY_SORTS,
    _CATEGORY_TAGS,
    _SORTS,
    DoubanPublicPage,
    _DetailHTMLParser,
    _html_detail,
    _json_ld_detail,
    _normalize_item,
    _normalize_title,
    _plain_text,
    _safe_poster_url,
    header_value,
    sanitize_session,
    _score,
    _year,
)
from app.discovery.models import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderInvalidResponse,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.discovery.providers.base import DEFAULT_TIMEOUT, TimeoutValue
from app.logger import get_logger

logger = get_logger(__name__)

_BASE_URL = "https://movie.douban.com"
_JSON_PATH = "/j/search_subjects"
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_MAX_DBCL2_LENGTH = 512
_DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_EXTERNAL_ID_RE = re.compile(r"^[0-9]+$")
_AUTH_STATUS_PATH = "/mine"
_AUTH_LOGIN_MARKER_RE = re.compile(
    r'<[^>]+class=["\'][^"\']*\bnav-login\b[^"\']*["\'][^>]*>',
    re.IGNORECASE,
)
_AUTHENTICATED_MARKER_RE = re.compile(
    r"(?:"
    r'<[^>]+class=["\'][^"\']*\bnav-user-name\b[^"\']*["\'][^>]*>'
    r"|"
    r'<[^>]+class=["\'][^"\']*\bnav-user-account\b[^"\']*["\'][^>]*>'
    r"|"
    r'<[^>]+href=["\']https://www\.douban\.com/mine/?(?:["\'][^>]*|\?[^"\']*)["\']'
    r")",
    re.IGNORECASE,
)


def normalize_dbcl2(value: str | None) -> str:
    """接受原始值或 Cookie 字符串，但只返回 dbcl2 的值。"""
    raw = (value or "").strip()
    if not raw:
        return ""
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw):
        raise ValueError("DOUBAN_DBCL2 不能包含控制字符或换行")

    normalized = raw
    if "=" in raw or ";" in raw:
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception as exc:
            raise ValueError("DOUBAN_DBCL2 Cookie 格式无效") from exc
        morsel = cookie.get("dbcl2")
        if morsel is None:
            raise ValueError("DOUBAN_DBCL2 Cookie 中缺少 dbcl2")
        normalized = str(morsel.value or "")
    elif len(raw) >= 2 and raw[0] == raw[-1] == '"':
        normalized = raw[1:-1]

    normalized = normalized.strip()
    if len(normalized) > _MAX_DBCL2_LENGTH:
        raise ValueError("DOUBAN_DBCL2 不能超过 512 个字符")
    if any(ord(char) < 0x21 or ord(char) > 0x7E for char in normalized):
        raise ValueError("DOUBAN_DBCL2 只能包含可见 ASCII 字符")
    if ";" in normalized:
        raise ValueError("DOUBAN_DBCL2 值格式无效")
    return normalized


def _finite_timeout(value: TimeoutValue | None) -> TimeoutValue:
    if isinstance(value, bool) or value is None:
        raise ValueError("timeout must be finite and positive")
    if isinstance(value, (int, float)):
        timeout = float(value)
        if math.isfinite(timeout) and timeout > 0:
            return timeout
        raise ValueError("timeout must be finite and positive")
    if isinstance(value, tuple) and len(value) == 2:
        connect, read = float(value[0]), float(value[1])
        if all(math.isfinite(part) and part > 0 for part in (connect, read)):
            return connect, read
    raise ValueError("timeout must be finite and positive")


def _safe_error(error: ProviderError) -> ProviderError:
    if isinstance(error, ProviderAuthenticationError):
        clean: ProviderError = ProviderAuthenticationError(
            "豆瓣 dbcl2 登录态已失效，请在设置中更新 Cookie"
        )
    elif isinstance(error, ProviderRateLimited):
        clean = ProviderRateLimited("豆瓣认证请求受限", retry_after=error.retry_after)
    elif isinstance(error, ProviderTimeout):
        clean = ProviderTimeout("豆瓣认证请求超时")
    elif isinstance(error, ProviderInvalidResponse):
        clean = ProviderInvalidResponse("豆瓣认证响应无效")
    elif isinstance(error, ProviderNotConfigured):
        clean = ProviderNotConfigured("豆瓣 dbcl2 回退未配置")
    else:
        clean = ProviderUnavailable("豆瓣认证网页不可用")
    clean.__cause__ = None
    clean.__context__ = None
    clean.__traceback__ = None
    return clean


class DoubanAuthenticatedClient:
    """使用独立 Session 和域限定 Cookie 的豆瓣网页客户端。"""

    def __init__(
        self,
        *,
        dbcl2: str | None,
        session: requests.Session | Any | None = None,
        timeout: TimeoutValue = DEFAULT_TIMEOUT,
        page_size: int = 20,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        clock: Callable[[], float] | None = None,
    ):
        del clock
        self.dbcl2 = normalize_dbcl2(dbcl2)
        self.session = session or requests.Session()
        self.timeout = _finite_timeout(timeout)
        self.page_size = max(1, min(page_size, 100))
        self.max_response_bytes = max_response_bytes
        if self.max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self._prepare_session()

    @property
    def configured(self) -> bool:
        return bool(self.dbcl2)

    def _prepare_session(self) -> None:
        sanitize_session(self.session)
        set_cookie = getattr(getattr(self.session, "cookies", None), "set", None)
        if self.dbcl2 and callable(set_cookie):
            set_cookie(
                "dbcl2",
                self.dbcl2,
                domain="movie.douban.com",
                path="/",
                secure=True,
            )

    def _ensure_configured(self) -> None:
        if not self.configured:
            raise ProviderNotConfigured("豆瓣 dbcl2 回退未配置")

    @staticmethod
    def _status_error(
        response: Any,
        *,
        classify_authentication: bool = True,
    ) -> ProviderError | None:
        status = int(getattr(response, "status_code", 0) or 0)
        if 300 <= status < 400:
            # dbcl2 过期时豆瓣把受保护页面 302 到登录页；这是登录态失效，
            # 不是服务不可用，必须让用户看到可操作的原因。
            location = header_value(getattr(response, "headers", {}) or {}, "Location").lower()
            if not classify_authentication and "sec.douban" in location:
                # 安全验证/风控跳转没有足够证据判断 Cookie 是否失效。
                return ProviderUnavailable("豆瓣认证状态暂时无法确认")
            if "login" in location or "accounts.douban" in location or "sec.douban" in location:
                return ProviderAuthenticationError("豆瓣 dbcl2 登录态已失效，请更新 Cookie")
            return ProviderUnavailable("豆瓣认证上游重定向被拒绝")
        if status in {401, 403}:
            if classify_authentication:
                return ProviderAuthenticationError("豆瓣登录态已失效")
            # 豆瓣会用 403 表示风控/访问权限不足；认证探测不能把它误报成失效。
            return ProviderUnavailable("豆瓣认证状态暂时无法确认")
        if status == 429:
            try:
                retry_after = int(header_value(response.headers or {}, "Retry-After") or 0)
            except ValueError:
                retry_after = 0
            return ProviderRateLimited("豆瓣认证请求受限", retry_after=retry_after)
        if status < 200 or status >= 300:
            return ProviderUnavailable("豆瓣认证网页不可用")
        return None

    def _read(self, response: Any, expected: str) -> tuple[bytes, str]:
        content_type = header_value(response.headers or {}, "Content-Type").split(";", 1)[0].strip().lower()
        valid_type = (
            content_type == "application/json" or content_type.endswith("+json")
            if expected == "json"
            else content_type in {"text/html", "application/xhtml+xml"}
        )
        if not valid_type:
            raise ProviderInvalidResponse("豆瓣认证响应 Content-Type 无效")
        content_length = header_value(response.headers or {}, "Content-Length")
        if content_length:
            try:
                length = int(content_length)
            except ValueError as exc:
                raise ProviderInvalidResponse("豆瓣认证响应长度无效") from exc
            if length < 0 or length > self.max_response_bytes:
                raise ProviderInvalidResponse("豆瓣认证响应过大")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            if not isinstance(chunk, bytes):
                raise ProviderInvalidResponse("豆瓣认证响应字节流无效")
            total += len(chunk)
            if total > self.max_response_bytes:
                raise ProviderInvalidResponse("豆瓣认证响应过大")
            chunks.append(chunk)
        return b"".join(chunks), content_type

    def _request(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None,
        expected: str,
        classify_authentication: bool = True,
    ) -> tuple[bytes, str]:
        self._ensure_configured()
        response = None
        failure: ProviderError | None = None
        result: tuple[bytes, str] | None = None
        try:
            self._prepare_session()
            response = self.session.get(
                f"{_BASE_URL}{path}",
                params=dict(params or {}),
                headers={
                    "Accept": "application/json" if expected == "json" else "text/html,application/xhtml+xml",
                    "Referer": "https://movie.douban.com/",
                    "User-Agent": _USER_AGENT,
                },
                timeout=self.timeout,
                allow_redirects=False,
                stream=True,
            )
            failure = self._status_error(
                response,
                classify_authentication=classify_authentication,
            )
            if failure is None:
                result = self._read(response, expected)
        except ProviderError as exc:
            failure = exc
        except requests.Timeout:
            failure = ProviderTimeout("豆瓣认证请求超时")
        except requests.RequestException:
            failure = ProviderUnavailable("豆瓣认证网页连接失败")
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if failure is not None:
            clean = _safe_error(failure)
            logger.warning("Douban authenticated request failed path=%s error=%s", path, clean.code)
            raise clean from None
        if result is None:
            raise ProviderInvalidResponse("豆瓣认证响应无效")
        return result

    def check_authentication(self) -> str:
        """通过固定 host 的受保护页面确认 dbcl2，不把公共页 200 当成登录成功。"""
        if not self.configured:
            return "unconfigured"
        try:
            body, _ = self._request(
                _AUTH_STATUS_PATH,
                params=None,
                expected="html",
                classify_authentication=False,
            )
        except ProviderAuthenticationError:
            # 受保护页面明确跳回登录页时，才认定 Cookie 已失效。
            return "invalid"
        except ProviderError:
            # 超时、风控、429、403、上游异常都保持灰色“未确认”。
            return "unknown"

        try:
            html = body.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, AttributeError):
            return "unknown"
        if _AUTH_LOGIN_MARKER_RE.search(html):
            return "invalid"
        if _AUTHENTICATED_MARKER_RE.search(html):
            return "valid"
        return "unknown"

    def list_items(
        self,
        category: str,
        media_type: str,
        page: int,
        filters: Mapping[str, Any] | None,
    ) -> DoubanPublicPage:
        self._ensure_configured()
        normalized_category = (category or "").strip().lower()
        normalized_type = (media_type or "").strip().lower()
        if normalized_type not in {"movie", "tv"}:
            raise ProviderInvalidResponse("豆瓣认证媒体类型无效")

        if page < 1:
            raise ProviderInvalidResponse("豆瓣认证页码无效")
        raw_filters = dict(filters or {})
        raw_sort = str(raw_filters.get("sort") or "").strip()
        sort = _CATEGORY_SORTS.get(normalized_category) or _SORTS.get(raw_sort)
        if sort is None:
            raise ProviderInvalidResponse("豆瓣认证排序值无效")
        tag_value = raw_filters.get("tags")
        if isinstance(tag_value, (list, tuple, set)):
            tag = ",".join(_plain_text(value) for value in tag_value if _plain_text(value))
        else:
            tag = _plain_text(tag_value)
        tag = tag or _CATEGORY_TAGS.get(normalized_category, "热门")
        body, _ = self._request(
            _JSON_PATH,
            params={
                "type": normalized_type,
                "tag": tag,
                "sort": sort,
                "page_limit": self.page_size,
                "page_start": (page - 1) * self.page_size,
            },
            expected="json",
        )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (TypeError, ValueError) as exc:
            logger.warning("Douban authenticated list invalid error=invalid_response")
            raise ProviderInvalidResponse("豆瓣认证 JSON 无效") from exc
        raw_items = payload.get("subjects") if isinstance(payload, dict) else None
        if not isinstance(raw_items, list):
            logger.warning("Douban authenticated list invalid error=invalid_response")
            raise ProviderInvalidResponse("豆瓣认证列表结构无效")
        items = tuple(
            normalized
            for raw in raw_items
            if isinstance(raw, Mapping)
            if (normalized := _normalize_item(raw, normalized_type)) is not None
        )
        if raw_items and not items:
            logger.warning("Douban authenticated list invalid error=invalid_response")
            raise ProviderInvalidResponse("豆瓣认证列表条目无效")
        return DoubanPublicPage(
            items=items[: self.page_size],
            has_more=len(raw_items) >= self.page_size,
            source="authenticated-json",
        )

    def get_detail(self, external_id: str, media_type: str) -> dict[str, Any]:
        self._ensure_configured()
        subject_id = (external_id or "").strip()
        normalized_type = (media_type or "").strip().lower()
        if not _EXTERNAL_ID_RE.fullmatch(subject_id):
            raise ProviderInvalidResponse("豆瓣认证条目 ID 无效")
        if normalized_type not in {"movie", "tv"}:
            raise ProviderInvalidResponse("豆瓣认证媒体类型无效")
        body, content_type = self._request(
            f"/subject/{subject_id}/", params=None, expected="html"
        )
        charset = "utf-8"
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].strip() or "utf-8"
        try:
            html = body.decode(charset, errors="strict")
            parser = _DetailHTMLParser()
            parser.feed(html)
            parser.close()
        except (LookupError, TypeError, ValueError) as exc:
            raise ProviderInvalidResponse("豆瓣认证详情 HTML 无效") from exc
        detail = _json_ld_detail(parser) or _html_detail(parser)
        if not detail.get("title"):
            raise ProviderInvalidResponse("豆瓣认证详情结构无效")
        return {
            "id": subject_id,
            "media_type": normalized_type,
            "title": _normalize_title(detail.get("title")),
            "original_title": _plain_text(detail.get("original_title")),
            "year": _year(detail.get("year"), detail.get("release_date")),
            "overview": _plain_text(detail.get("overview")),
            "poster_url": _safe_poster_url(detail.get("poster_url")),
            "rating": _score(detail.get("rating")),
            "release_date": _plain_text(detail.get("release_date")),
        }
