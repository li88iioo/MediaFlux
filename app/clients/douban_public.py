"""豆瓣电影站公共 JSON/HTML 客户端。

只访问固定的 ``movie.douban.com`` 主机，不使用账号、Cookie 或 Frodo 凭据。
上游响应在此边界完成限速、大小/类型/结构校验和纯文本归一化。
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from app.discovery.models import (
    ProviderError,
    ProviderInvalidResponse,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.discovery.providers.base import DEFAULT_TIMEOUT, TimeoutValue

_BASE_URL = "https://movie.douban.com"
_JSON_PATH = "/j/search_subjects"
_HTML_PATHS = {
    "movie_showing": "/cinema/nowplaying/",
    "movie_soon": "/cinema/later/",
    "movie_top250": "/top250",
}
_JSON_CATEGORIES = {
    "recommend",
    "discover",
    "movie_hot",
    "tv_hot",
    "tv_chinese_weekly",
    "tv_global_weekly",
}
_CATEGORY_MEDIA_TYPES = {
    "movie_hot": "movie",
    "movie_showing": "movie",
    "movie_soon": "movie",
    "movie_top250": "movie",
    "tv_hot": "tv",
    "tv_chinese_weekly": "tv",
    "tv_global_weekly": "tv",
}
_CATEGORY_TAGS = {
    "movie_hot": "热门",
    "tv_hot": "热门",
    "tv_chinese_weekly": "国产剧",
    "tv_global_weekly": "美剧",
}
_GLOBAL_WEEKLY_CATEGORIES = {"tv_global_weekly"}
_GLOBAL_WEEKLY_TAGS = ("美剧", "英剧", "日剧", "韩剧")
_CATEGORY_SORTS = {
    "tv_chinese_weekly": "rank",
    "tv_global_weekly": "rank",
}
_SORTS = {
    "": "recommend",
    "recommend": "recommend",
    "rank": "rank",
    "time": "time",
}
_IMAGE_HOSTS = {
    "img1.doubanio.com",
    "img2.doubanio.com",
    "img3.doubanio.com",
    "img9.doubanio.com",
    "qnmob3.doubanio.com",
}
_SUBJECT_RE = re.compile(r"(?:^|/)subject/(\d+)(?:/|$)")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_SPACE_RE = re.compile(r"\s+")
_CHARSET_RE = re.compile(r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", re.I)
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 2
_IGNORED_ELEMENTS = {"script", "style", "template", "noscript"}
_VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
_SENSITIVE_SESSION_HEADERS = {"authorization", "proxy-authorization", "cookie"}
_PROCESS_REQUEST_LOCK = threading.Lock()
_PROCESS_LAST_REQUEST_AT: float | None = None


def _pre_network_invalid_response(message: str) -> ProviderInvalidResponse:
    error = ProviderInvalidResponse(message)
    error.request_attempted = False
    return error


@dataclass(frozen=True)
class DoubanPublicPage:
    """公共列表的不可变页级结果；条目是已脱离上游对象的普通字典。"""

    items: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    has_more: bool = False
    source: str = "public"

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(dict(item) for item in (self.items or ())))
        object.__setattr__(self, "has_more", bool(self.has_more))
        object.__setattr__(self, "source", str(self.source or "public"))


class _PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() in {"script", "style", "template", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "template", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


class _ListHTMLParser(HTMLParser):
    """只识别固定榜单中带 subject ID 的有限语义节点。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []
        self.has_more = False
        self._ignored_depth = 0

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {str(key).lower(): str(value or "") for key, value in attrs}

    @staticmethod
    def _classes(attrs: Mapping[str, str]) -> set[str]:
        return {value for value in attrs.get("class", "").split() if value}

    def _current_candidate(self) -> dict[str, Any] | None:
        for frame in reversed(self.stack):
            candidate = frame.get("candidate")
            if isinstance(candidate, dict):
                return candidate
        return None

    def _candidate_frame(self) -> dict[str, Any] | None:
        for frame in reversed(self.stack):
            if frame.get("tag") in {"tr", "li", "div"}:
                return frame
        return self.stack[-1] if self.stack else None

    def _inside_class(self, *names: str) -> bool:
        wanted = set(names)
        return any(frame.get("classes", set()) & wanted for frame in self.stack)

    def _inside_subject_link(self) -> bool:
        return any(frame.get("subject_link") for frame in self.stack)

    @staticmethod
    def _append(candidate: dict[str, Any], key: str, value: str) -> None:
        clean = _plain_text(value)
        if not clean:
            return
        previous = str(candidate.get(key) or "")
        if clean == previous:
            return
        candidate[key] = f"{previous} {clean}".strip() if previous else clean

    def _finish_frame(self, frame: Mapping[str, Any]) -> None:
        if frame.get("tag") in _IGNORED_ELEMENTS and self._ignored_depth:
            self._ignored_depth -= 1
        candidate = frame.get("candidate")
        if isinstance(candidate, dict) and candidate.get("id"):
            self.items.append(candidate)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = self._attrs(attrs)
        classes = self._classes(values)
        frame: dict[str, Any] = {"tag": tag, "attrs": values, "classes": classes}

        if tag in _IGNORED_ELEMENTS:
            self._ignored_depth += 1

        data_subject = values.get("data-subject", "").strip()
        recognized_container = (
            data_subject.isdigit()
            or "list-item" in classes
            or "item" in classes
            or tag == "tr"
        )
        if recognized_container:
            candidate: dict[str, Any] = {}
            if data_subject.isdigit():
                candidate["id"] = data_subject
            for attr, key in (
                ("data-title", "title"),
                ("data-score", "rating"),
                ("data-release", "release_date"),
            ):
                if values.get(attr):
                    candidate[key] = values[attr]
            frame["candidate"] = candidate

        href = values.get("href", "")
        subject_id = _subject_id_from_url(href)
        if tag == "a" and subject_id:
            candidate = self._current_candidate()
            if candidate is None:
                owner = self._candidate_frame()
                if owner is not None:
                    owner["candidate"] = {}
                    candidate = owner["candidate"]
            if candidate is not None:
                candidate["id"] = subject_id
            frame["subject_link"] = True

        if tag == "a" and (
            values.get("rel", "").lower() == "next"
            or "next" in classes
            or any("next" in parent.get("classes", set()) for parent in self.stack)
        ):
            self.has_more = True

        if tag not in _VOID_ELEMENTS:
            self.stack.append(frame)

        if tag == "img":
            candidate = self._current_candidate()
            if candidate is not None:
                poster = values.get("data-original") or values.get("src")
                if poster and not candidate.get("poster_url"):
                    candidate["poster_url"] = poster
                if values.get("alt") and not candidate.get("title"):
                    candidate["title"] = values["alt"]

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_ELEMENTS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        clean = _plain_text(data)
        if not clean:
            return
        candidate = self._current_candidate()
        if candidate is None:
            return
        if self._inside_class("title"):
            self._append(candidate, "title", clean)
        elif self._inside_class("other"):
            self._append(candidate, "original_title", clean)
        elif self._inside_class("rating_num", "rating_nums", "rating"):
            self._append(candidate, "rating", clean)
        elif self._inside_class("release_date"):
            self._append(candidate, "release_date", clean)
        elif self._inside_class("abstract"):
            self._append(candidate, "overview", clean)
        elif any(frame.get("tag") == "p" for frame in self.stack):
            self._append(candidate, "overview", clean)
        elif self._inside_subject_link() and not candidate.get("title"):
            self._append(candidate, "title", clean)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        match_index = next(
            (index for index in range(len(self.stack) - 1, -1, -1)
             if self.stack[index].get("tag") == tag),
            None,
        )
        if match_index is None:
            return
        closing = self.stack[match_index:]
        del self.stack[match_index:]
        for frame in reversed(closing):
            self._finish_frame(frame)


class _DetailHTMLParser(HTMLParser):
    """采集 JSON-LD、OpenGraph 与少量固定语义字段。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[dict[str, Any]] = []
        self.meta: dict[str, str] = {}
        self.semantic: dict[str, str] = {}
        self.json_ld: list[str] = []
        self._json_ld_buffer: list[str] | None = None
        self._ignored_depth = 0

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {str(key).lower(): str(value or "") for key, value in attrs}

    @staticmethod
    def _append(values: dict[str, str], key: str, value: str) -> None:
        clean = _plain_text(value)
        if not clean:
            return
        previous = values.get(key, "")
        values[key] = f"{previous} {clean}".strip() if previous else clean

    def _semantic_key(self) -> str:
        for frame in reversed(self.stack):
            attrs = frame["attrs"]
            classes = frame["classes"]
            prop = attrs.get("property", "").lower()
            if prop == "v:itemreviewed":
                return "title"
            if prop == "v:average" or "rating_num" in classes:
                return "rating"
            if prop == "v:summary":
                return "overview"
            if "year" in classes:
                return "year"
        return ""

    def _finish_frame(self, frame: Mapping[str, Any]) -> None:
        if frame.get("json_ld"):
            payload = "".join(self._json_ld_buffer or []).strip()
            if payload:
                self.json_ld.append(payload)
            self._json_ld_buffer = None
        elif frame.get("tag") in _IGNORED_ELEMENTS and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = self._attrs(attrs)
        classes = {value for value in values.get("class", "").split() if value}
        frame: dict[str, Any] = {"tag": tag, "attrs": values, "classes": classes}

        if tag == "meta":
            key = (values.get("property") or values.get("name") or "").strip().lower()
            content = values.get("content", "")
            if key and content and key not in self.meta:
                self.meta[key] = content

        prop = values.get("property", "").lower()
        if prop == "v:initialreleasedate" and values.get("content"):
            self.semantic.setdefault("release_date", values["content"])

        is_json_ld = (
            tag == "script"
            and values.get("type", "").split(";", 1)[0].strip().lower() == "application/ld+json"
        )
        if is_json_ld:
            frame["json_ld"] = True
            self._json_ld_buffer = []
        elif tag in _IGNORED_ELEMENTS:
            self._ignored_depth += 1

        if tag not in _VOID_ELEMENTS:
            self.stack.append(frame)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_ELEMENTS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._json_ld_buffer is not None:
            self._json_ld_buffer.append(data)
            return
        if self._ignored_depth:
            return
        key = self._semantic_key()
        if key:
            self._append(self.semantic, key, data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        match_index = next(
            (index for index in range(len(self.stack) - 1, -1, -1)
             if self.stack[index].get("tag") == tag),
            None,
        )
        if match_index is None:
            return
        closing = self.stack[match_index:]
        del self.stack[match_index:]
        for frame in reversed(closing):
            self._finish_frame(frame)


def _plain_text(value: Any) -> str:
    if isinstance(value, (Mapping, list, tuple, set)):
        return ""
    raw = str(value or "")
    if not raw:
        return ""
    parser = _PlainTextParser()
    try:
        parser.feed(raw)
        parser.close()
        text = " ".join(parser.parts)
    except (ValueError, TypeError):
        text = raw
    return _SPACE_RE.sub(" ", text).strip()


def _subject_id_from_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != "movie.douban.com":
            return ""
        path = parsed.path
    else:
        path = parsed.path or raw
    match = _SUBJECT_RE.search(path)
    return match.group(1) if match else ""


def _safe_poster_url(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("url") or value.get("contentUrl")
    if isinstance(value, (list, tuple)):
        value = next((item for item in value if item), "")
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or host not in _IMAGE_HOSTS
        or parsed.username
        or parsed.password
        or port not in (None, 80, 443)
    ):
        return ""
    path = parsed.path or ""
    if not path.startswith("/") or ".." in path.split("/"):
        return ""
    return urlunsplit((parsed.scheme, host, path, "", ""))


def _score(value: Any) -> float | None:
    if isinstance(value, Mapping):
        values = value
        value = values.get("ratingValue")
        if value is None:
            value = values.get("value") if values.get("value") is not None else values.get("score")
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and 0 <= result <= 10 else None


def _year(*values: Any) -> str:
    for value in values:
        match = _YEAR_RE.search(_plain_text(value))
        if match:
            return match.group(0)
    return ""


def _normalize_title(value: Any) -> str:
    title = _plain_text(value)
    for suffix in (" (豆瓣)", " - 豆瓣电影", "_豆瓣电影"):
        if title.endswith(suffix):
            title = title[: -len(suffix)].rstrip()
    return title


def _normalize_item(raw: Mapping[str, Any], media_type: str) -> dict[str, Any] | None:
    scalar_fields = (
        "id", "subject_id", "title", "name", "original_title", "original_name",
        "year", "release_date", "date", "card_subtitle", "overview",
        "description", "intro", "abstract", "poster_url", "cover", "pic",
        "image", "rating", "rate", "url", "is_new", "episodes_info",
    )
    if any(
        key in raw and raw.get(key) is not None
        and not isinstance(raw.get(key), (str, int, float, bool))
        for key in scalar_fields
    ):
        return None

    external_id = str(raw.get("id") or raw.get("subject_id") or "").strip()
    title = _normalize_title(raw.get("title") or raw.get("name"))
    if not external_id.isdigit() or not title:
        return None
    release_date = _plain_text(raw.get("release_date") or raw.get("date"))
    overview = _plain_text(
        raw.get("overview") or raw.get("description") or raw.get("intro") or raw.get("abstract")
    )
    raw_is_new = raw.get("is_new", False)
    if isinstance(raw_is_new, str):
        is_new = raw_is_new.strip().lower() in {"1", "true", "yes", "on"}
    else:
        is_new = bool(raw_is_new)
    return {
        "id": external_id,
        "media_type": media_type,
        "title": title,
        "original_title": _plain_text(raw.get("original_title") or raw.get("original_name")),
        "year": _year(raw.get("year"), release_date, raw.get("card_subtitle"), overview),
        "overview": overview,
        "poster_url": _safe_poster_url(
            raw.get("poster_url") or raw.get("cover") or raw.get("pic") or raw.get("image")
        ),
        "rating": _score(raw.get("rating") if raw.get("rating") is not None else raw.get("rate")),
        "release_date": release_date,
        "is_new": is_new,
        "episodes_info": _plain_text(raw.get("episodes_info")),
    }


def _normalize_html_item(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    item = _normalize_item(raw, "movie")
    if item is None:
        return None
    original = item["original_title"].lstrip("/ ").strip()
    item["original_title"] = original
    return item


def _iter_json_ld_objects(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from _iter_json_ld_objects(item)
    elif isinstance(value, dict):
        graph = value.get("@graph")
        if isinstance(graph, (list, dict)):
            yield from _iter_json_ld_objects(graph)
        yield value


def _json_ld_detail(parser: _DetailHTMLParser) -> dict[str, Any]:
    for raw_payload in parser.json_ld:
        try:
            payload = json.loads(raw_payload)
        except (TypeError, ValueError):
            continue
        for candidate in _iter_json_ld_objects(payload):
            raw_type = candidate.get("@type", "")
            types = raw_type if isinstance(raw_type, list) else [raw_type]
            normalized_types = {str(value).lower() for value in types}
            if normalized_types and not normalized_types & {
                "movie", "tvseries", "tvshow", "creativework"
            }:
                continue
            title = _normalize_title(candidate.get("name") or candidate.get("headline"))
            if not title:
                continue
            release_date = _plain_text(candidate.get("datePublished"))
            return {
                "title": title,
                "original_title": _plain_text(candidate.get("alternateName")),
                "overview": _plain_text(candidate.get("description")),
                "poster_url": _safe_poster_url(candidate.get("image")),
                "rating": _score(candidate.get("aggregateRating")),
                "release_date": release_date,
                "year": _year(release_date),
            }
    return {}


def _html_detail(parser: _DetailHTMLParser) -> dict[str, Any]:
    title = _normalize_title(
        parser.semantic.get("title")
        or parser.meta.get("og:title")
        or parser.meta.get("twitter:title")
    )
    release_date = _plain_text(parser.semantic.get("release_date"))
    return {
        "title": title,
        "original_title": "",
        "overview": _plain_text(
            parser.semantic.get("overview")
            or parser.meta.get("og:description")
            or parser.meta.get("description")
        ),
        "poster_url": _safe_poster_url(
            parser.meta.get("og:image") or parser.meta.get("twitter:image")
        ),
        "rating": _score(parser.semantic.get("rating")),
        "release_date": release_date,
        "year": _year(parser.semantic.get("year"), release_date),
    }


def _header(headers: Mapping[str, Any], name: str) -> str:
    wanted = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == wanted:
            return str(value or "")
    return ""


def _validate_timeout(timeout: TimeoutValue) -> TimeoutValue:
    values = timeout if isinstance(timeout, tuple) else (timeout,)
    if not values or any(
        not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) <= 0
        for value in values
    ):
        raise ValueError("timeout must contain finite positive values")
    if isinstance(timeout, tuple) and len(timeout) != 2:
        raise ValueError("timeout tuple must contain connect/read values")
    return timeout


class DoubanPublicClient:
    """固定主机的豆瓣公共数据客户端。"""

    def __init__(
        self,
        *,
        session: requests.Session | Any | None = None,
        timeout: TimeoutValue = DEFAULT_TIMEOUT,
        page_size: int = 20,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        min_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.session = session or requests.Session()
        self.timeout = _validate_timeout(timeout)
        self.page_size = max(1, min(int(page_size), 20))
        self.max_response_bytes = int(max_response_bytes)
        self.min_interval = float(min_interval)
        if self.max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        if not math.isfinite(self.min_interval) or self.min_interval < 0:
            raise ValueError("min_interval must be finite and non-negative")
        self.clock = clock
        self.sleeper = sleeper
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

    def _wait_for_slot(self) -> None:
        global _PROCESS_LAST_REQUEST_AT
        now = float(self.clock())
        if _PROCESS_LAST_REQUEST_AT is not None and now < _PROCESS_LAST_REQUEST_AT:
            _PROCESS_LAST_REQUEST_AT = None
        if _PROCESS_LAST_REQUEST_AT is not None:
            delay = self.min_interval - (now - _PROCESS_LAST_REQUEST_AT)
            if delay > 0:
                self.sleeper(delay)
                now = float(self.clock())
        _PROCESS_LAST_REQUEST_AT = now

    @staticmethod
    def _redirect_target(current_url: str, location: str) -> str:
        target = urljoin(current_url, str(location or "").strip())
        parsed = None
        invalid_port = False
        try:
            parsed = urlsplit(target)
            port = parsed.port
        except ValueError:
            port = None
            invalid_port = True
        if parsed is None or invalid_port:
            raise ProviderUnavailable("豆瓣公共服务重定向不可用")
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() != "movie.douban.com"
            or parsed.username
            or parsed.password
            or port not in (None, 443)
        ):
            raise ProviderUnavailable("豆瓣公共服务重定向不可用")
        return urlunsplit(("https", "movie.douban.com", parsed.path or "/", parsed.query, ""))

    def _read_body(self, response: Any, expected: str) -> bytes:
        headers = getattr(response, "headers", {}) or {}
        content_type = _header(headers, "Content-Type").split(";", 1)[0].strip().lower()
        if expected == "json":
            valid_type = content_type == "application/json" or content_type.endswith("+json")
        else:
            valid_type = content_type in {"text/html", "application/xhtml+xml"}
        if not valid_type:
            raise ProviderInvalidResponse("豆瓣公共响应 Content-Type 无效")

        content_length = _header(headers, "Content-Length")
        if content_length:
            try:
                length = int(content_length)
            except ValueError as exc:
                raise ProviderInvalidResponse("豆瓣公共响应长度无效") from exc
            if length < 0 or length > self.max_response_bytes:
                raise ProviderInvalidResponse("豆瓣公共响应过大")

        chunks: list[bytes] = []
        size = 0
        iterator = getattr(response, "iter_content", None)
        if not callable(iterator):
            raise ProviderInvalidResponse("豆瓣公共响应不可读取")
        for chunk in iterator(chunk_size=64 * 1024):
            if not chunk:
                continue
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            if not isinstance(chunk, (bytes, bytearray)):
                raise ProviderInvalidResponse("豆瓣公共响应块无效")
            size += len(chunk)
            if size > self.max_response_bytes:
                raise ProviderInvalidResponse("豆瓣公共响应过大")
            chunks.append(bytes(chunk))
        return b"".join(chunks)

    def _request(self, path: str, *, params: Mapping[str, Any] | None, expected: str) -> tuple[bytes, str]:
        url = f"{_BASE_URL}{path}"
        accept = "application/json" if expected == "json" else "text/html,application/xhtml+xml"
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": accept,
            "Referer": f"{_BASE_URL}/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        query: Mapping[str, Any] | None = dict(params or {})

        with _PROCESS_REQUEST_LOCK:
            for redirect_count in range(_MAX_REDIRECTS + 1):
                self._wait_for_slot()
                self._sanitize_session_credentials()
                response = None
                converted_error: ProviderTimeout | ProviderUnavailable | None = None
                result: tuple[bytes, str] | None = None
                try:
                    response = self.session.get(
                        url,
                        params=query,
                        headers=headers,
                        timeout=self.timeout,
                        allow_redirects=False,
                        stream=True,
                    )
                    status = int(getattr(response, "status_code", 0) or 0)
                    if status in _REDIRECT_STATUSES:
                        if redirect_count >= _MAX_REDIRECTS:
                            raise ProviderUnavailable("豆瓣公共服务重定向过多")
                        url = self._redirect_target(url, _header(response.headers, "Location"))
                        continue
                    if status == 429:
                        retry_after_raw = _header(response.headers, "Retry-After")
                        try:
                            retry_after = int(retry_after_raw or 0)
                        except ValueError:
                            retry_after = 0
                        raise ProviderRateLimited("豆瓣公共请求受限", retry_after=retry_after)
                    if status in {403, 418}:
                        raise ProviderUnavailable("豆瓣公共服务暂不可用")
                    if status < 200 or status >= 300:
                        raise ProviderUnavailable("豆瓣公共服务暂不可用")
                    result = (
                        self._read_body(response, expected),
                        _header(response.headers, "Content-Type"),
                    )
                except ProviderInvalidResponse:
                    raise
                except (ProviderRateLimited, ProviderUnavailable):
                    raise
                except requests.Timeout:
                    converted_error = ProviderTimeout("豆瓣公共请求超时")
                except requests.ConnectionError:
                    converted_error = ProviderUnavailable("豆瓣公共连接失败")
                except requests.RequestException:
                    converted_error = ProviderUnavailable("豆瓣公共请求失败")
                finally:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
                if converted_error is not None:
                    raise converted_error
                if result is not None:
                    return result
        raise ProviderUnavailable("豆瓣公共服务重定向不可用")


    @staticmethod
    def _decode(body: bytes, content_type: str) -> str:
        charset_match = _CHARSET_RE.search(content_type or "")
        charset = charset_match.group(1) if charset_match else "utf-8"
        try:
            return body.decode(charset, errors="replace")
        except LookupError:
            return body.decode("utf-8", errors="replace")

    @staticmethod
    def _validate_list_request(category: str, media_type: str, page: int) -> tuple[str, str, int]:
        normalized_category = str(category or "").strip().lower()
        normalized_type = str(media_type or "").strip().lower()
        try:
            normalized_page = int(page)
        except (TypeError, ValueError) as exc:
            raise _pre_network_invalid_response("豆瓣公共页码无效") from exc
        if normalized_type not in {"movie", "tv"}:
            raise _pre_network_invalid_response("豆瓣公共媒体类型无效")
        if normalized_page < 1:
            raise _pre_network_invalid_response("豆瓣公共页码无效")
        if normalized_category not in _JSON_CATEGORIES and normalized_category not in _HTML_PATHS:
            raise _pre_network_invalid_response("不支持的豆瓣公共分类")
        required_type = _CATEGORY_MEDIA_TYPES.get(normalized_category)
        if required_type and normalized_type != required_type:
            raise _pre_network_invalid_response("豆瓣公共分类与媒体类型不匹配")
        return normalized_category, normalized_type, normalized_page

    def list_items(
        self,
        category: str,
        media_type: str,
        page: int,
        filters: Mapping[str, Any] | None,
    ) -> DoubanPublicPage:
        category, media_type, page = self._validate_list_request(category, media_type, page)
        if category in _HTML_PATHS:
            return self._list_html(category, page)
        return self._list_json(category, media_type, page, filters or {})

    def _json_items(
        self,
        media_type: str,
        params: Mapping[str, Any],
    ) -> tuple[tuple[dict[str, Any], ...], int]:
        body, content_type = self._request(_JSON_PATH, params=params, expected="json")
        try:
            payload = json.loads(self._decode(body, content_type))
        except (TypeError, ValueError) as exc:
            raise ProviderInvalidResponse("豆瓣公共 JSON 无效") from exc
        if not isinstance(payload, dict):
            raise ProviderInvalidResponse("豆瓣公共 JSON 结构无效")
        raw_items = payload.get("subjects")
        if raw_items is None:
            raw_items = payload.get("data")
            if isinstance(raw_items, dict):
                raw_items = raw_items.get("subjects") or raw_items.get("items")
        if not isinstance(raw_items, list):
            raise ProviderInvalidResponse("豆瓣公共列表结构无效")
        items = tuple(
            normalized
            for raw in raw_items
            if isinstance(raw, Mapping)
            if (normalized := _normalize_item(raw, media_type)) is not None
        )
        if raw_items and not items:
            raise ProviderInvalidResponse("豆瓣公共列表条目无效")
        return items, len(raw_items)

    def _list_global_weekly(
        self,
        media_type: str,
        page: int,
        sort: str,
    ) -> DoubanPublicPage:
        region_limit = max(1, math.ceil(self.page_size / len(_GLOBAL_WEEKLY_TAGS)))
        page_start = (page - 1) * region_limit
        combined: list[dict[str, Any]] = []
        failures: list[ProviderError] = []
        successful_regions = 0
        has_more = False
        for tag in _GLOBAL_WEEKLY_TAGS:
            try:
                items, raw_count = self._json_items(
                    media_type,
                    {
                        "type": media_type,
                        "tag": tag,
                        "sort": sort,
                        "page_limit": region_limit,
                        "page_start": page_start,
                    },
                )
            except ProviderError as exc:
                failures.append(exc)
                continue
            successful_regions += 1
            combined.extend(items)
            has_more = has_more or raw_count >= region_limit

        if not successful_regions and failures:
            raise failures[0].with_traceback(None)

        deduplicated: list[tuple[int, dict[str, Any]]] = []
        seen: set[str] = set()
        for order, item in enumerate(combined):
            external_id = str(item.get("id") or "")
            if not external_id or external_id in seen:
                continue
            seen.add(external_id)
            deduplicated.append((order, item))

        def rating_key(entry: tuple[int, dict[str, Any]]) -> tuple[bool, float, int]:
            order, item = entry
            rating = item.get("rating")
            return rating is None, -(float(rating) if rating is not None else 0.0), order

        ranked = tuple(item for _, item in sorted(deduplicated, key=rating_key))
        return DoubanPublicPage(
            items=ranked[: self.page_size],
            has_more=has_more,
            source=(
                "public-json-aggregate-partial"
                if failures
                else "public-json-aggregate"
            ),
        )

    def _list_json(
        self,
        category: str,
        media_type: str,
        page: int,
        filters: Mapping[str, Any],
    ) -> DoubanPublicPage:
        raw_sort = str(filters.get("sort") or "").strip()
        sort = _CATEGORY_SORTS.get(category) or _SORTS.get(raw_sort)
        if sort is None:
            raise _pre_network_invalid_response("豆瓣公共排序值无效")
        if category in _GLOBAL_WEEKLY_CATEGORIES:
            return self._list_global_weekly(media_type, page, sort)
        tag_value = filters.get("tags")
        if isinstance(tag_value, (list, tuple, set)):
            tag = ",".join(_plain_text(value) for value in tag_value if _plain_text(value))
        else:
            tag = _plain_text(tag_value)
        tag = tag or _CATEGORY_TAGS.get(category, "热门")
        items, raw_count = self._json_items(
            media_type,
            {
                "type": media_type,
                "tag": tag,
                "sort": sort,
                "page_limit": self.page_size,
                "page_start": (page - 1) * self.page_size,
            },
        )
        return DoubanPublicPage(
            items=items[: self.page_size],
            has_more=raw_count >= self.page_size,
            source="public-json",
        )

    def _list_html(self, category: str, page: int) -> DoubanPublicPage:
        params: dict[str, Any] | None = None
        if category == "movie_top250":
            params = {"start": (page - 1) * self.page_size, "filter": ""}
        elif page != 1:
            raise _pre_network_invalid_response("该豆瓣公共榜单不支持分页")
        body, content_type = self._request(_HTML_PATHS[category], params=params, expected="html")
        parser = _ListHTMLParser()
        try:
            parser.feed(self._decode(body, content_type))
            parser.close()
        except (TypeError, ValueError) as exc:
            raise ProviderInvalidResponse("豆瓣公共 HTML 无效") from exc
        deduplicated: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in parser.items:
            normalized = _normalize_html_item(raw)
            if normalized is None or normalized["id"] in seen:
                continue
            seen.add(normalized["id"])
            deduplicated.append(normalized)
        if not deduplicated:
            raise ProviderInvalidResponse("豆瓣公共榜单结构无效")
        has_more = parser.has_more or len(deduplicated) > self.page_size
        return DoubanPublicPage(
            items=tuple(deduplicated[: self.page_size]),
            has_more=has_more,
            source="public-html",
        )

    def get_detail(self, external_id: str, media_type: str) -> dict[str, Any]:
        subject_id = str(external_id or "").strip()
        normalized_type = str(media_type or "").strip().lower()
        if not subject_id.isdigit():
            raise _pre_network_invalid_response("豆瓣公共条目 ID 无效")
        if normalized_type not in {"movie", "tv"}:
            raise _pre_network_invalid_response("豆瓣公共媒体类型无效")
        body, content_type = self._request(
            f"/subject/{subject_id}/", params=None, expected="html"
        )
        parser = _DetailHTMLParser()
        try:
            parser.feed(self._decode(body, content_type))
            parser.close()
        except (TypeError, ValueError) as exc:
            raise ProviderInvalidResponse("豆瓣公共详情 HTML 无效") from exc
        detail = _json_ld_detail(parser) or _html_detail(parser)
        if not detail.get("title"):
            raise ProviderInvalidResponse("豆瓣公共详情结构无效")
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
